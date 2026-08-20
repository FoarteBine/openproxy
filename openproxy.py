import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

UPSTREAM = "https://opencode.ai/zen/v1/chat/completions"
CHECK_URL = "https://opencode.ai/zen/v1/models"
PROXY_REQUEST_TIMEOUT = 15
DEFAULT_PROXY_SOURCE = "https://raw.githubusercontent.com/iplocate/free-proxy-list/refs/heads/main/all-proxies.txt"
UPSTREAM_KEY = "public"
UPSTREAM_UA = "opencode/1.18.18"

FREE_MODELS = [
    "deepseek-v4-flash-free",
    "big-pickle",
    "hy3-free",
    "laguna-s-2.1-free",
    "mimo-v2.5-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
]
DEFAULT_MODEL = "deepseek-v4-flash-free"

SUPPORTED_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.json")
LOCK = threading.Lock()

STATE = {
    "proxy": "",       # current effective proxy string ("" = direct)
    "proxy_spec": "direct",
    "proxies": [],
}


def resolve_model(raw):
    m = raw or DEFAULT_MODEL
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    if m in FREE_MODELS:
        return m
    return DEFAULT_MODEL


def build_session(proxy):
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Authorization": "Bearer " + UPSTREAM_KEY,
        "User-Agent": UPSTREAM_UA,
        "x-opencode-client": "desktop",
    })
    if proxy != "direct":
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def parse_proxy_spec(spec):
    """Validate a public proxy URL."""
    spec = (spec or "").strip()
    if spec == "direct":
        return "direct", None
    scheme = spec.split("://", 1)[0].lower()
    if scheme in SUPPORTED_SCHEMES:
        return spec, None
    return None, "unsupported proxy scheme '%s'" % scheme


def load_config():
    cfg = {"proxies": []}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("[!] cannot save config: %r" % e, flush=True)


def apply_proxy(spec):
    with LOCK:
        proxy, err = parse_proxy_spec(spec)
        if err:
            return False, err
        STATE["proxy_spec"] = spec or "direct"
        active_spec = spec or "direct"
        configured = [active_spec] + [item for item in STATE["proxies"] if item != active_spec]
        STATE["proxies"] = configured
        return True, describe_proxy()


def check_proxy(spec, timeout):
    """Check one proxy against the upstream models endpoint."""
    proxy, err = parse_proxy_spec(spec)
    if err:
        return False, err
    try:
        with build_session(proxy).get(CHECK_URL, timeout=timeout) as resp:
            if resp.status_code == 200:
                return True, "HTTP 200"
            return False, "HTTP %d" % resp.status_code
    except requests.exceptions.RequestException as e:
        return False, "%s" % e


def save_proxy_list(path, proxies):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"proxies": proxies}, f, indent=2)


def load_proxy_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        proxies = data.get("proxies", [])
        if isinstance(proxies, list):
            return list(dict.fromkeys(str(proxy).strip() for proxy in proxies if str(proxy).strip()))
    except (OSError, ValueError, AttributeError):
        pass
    return []


def check_config_proxies(source_url, timeout):
    try:
        response = requests.get(source_url, timeout=30)
        response.raise_for_status()
        configured = list(dict.fromkeys(line.strip() for line in response.text.splitlines() if line.strip()))
    except requests.exceptions.RequestException as e:
        print("Cannot download public proxy list: %s" % e, flush=True)
        return 1

    directory = os.path.dirname(CONFIG_PATH)
    output_path = os.path.join(directory, "proxies.json")
    failed_path = os.path.join(directory, "failed.json")
    working = load_proxy_list(output_path)
    failed = load_proxy_list(failed_path)
    known = set(working) | set(failed)
    pending = [spec for spec in configured if spec not in known]
    skipped = len(configured) - len(pending)
    configured = pending
    total = len(configured)
    print("Skipping %d already tested proxies." % skipped, flush=True)
    print("Checking %d new proxies..." % total, flush=True)
    if not total:
        print("Nothing new to check.", flush=True)
        return 0
    for index, spec in enumerate(configured, 1):
        ok, detail = check_proxy(spec, timeout)
        percent = int(index * 100 / total)
        remaining = total - index
        state = "WORKING" if ok else "FAILED"
        print("[%3d%%] %s: %s (%s), remaining: %d" % (percent, index, spec, state, remaining), flush=True)
        if ok:
            working.append(spec)
            save_proxy_list(output_path, working)
        else:
            failed.append(spec)
            save_proxy_list(failed_path, failed)

    try:
        save_proxy_list(output_path, working)
        save_proxy_list(failed_path, failed)
    except OSError as e:
        print("[!] cannot save working config: %r" % e, flush=True)
        return 1
    print("Working proxies total: %d; checked now: %d" % (len(working), total), flush=True)
    print("Saved: %s" % output_path, flush=True)
    print("Failed proxies: %d; saved: %s" % (len(failed), failed_path), flush=True)
    return 0


def describe_proxy():
    return STATE["proxy_spec"]


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenAIFreeProxy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[proxy] %s %s -> %s" % (self.command, self.path, fmt % args), flush=True)

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, code, message):
        self._send_json(code, {"error": {"message": message, "type": "error", "code": code}})

    def _proxy_candidates(self):
        with LOCK:
            return list(STATE["proxies"] or ["direct"])

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        q = parse_qs(urlparse(self.path).query)
        if path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": m, "object": "model", "created": 1787000000, "owned_by": "opencode"} for m in FREE_MODELS],
            })
        elif path == "/admin/proxy":
            self._send_json(200, {
                "current": STATE["proxy_spec"],
                "description": describe_proxy(),
                "supported": list(SUPPORTED_SCHEMES),
                "proxies": STATE["proxies"],
                "usage": "/admin/proxy?set=http://host:port",
            })
        elif path == "/admin/proxy/set":
            spec = q.get("set", [""])[0]
            ok, msg = apply_proxy(spec)
            if ok:
                self._send_json(200, {"ok": True, "description": describe_proxy()})
            else:
                self._send_error(400, msg)
        else:
            self._send_error(404, "Not found. Available: GET /v1/models, POST /v1/chat/completions, GET /admin/proxy[/set]")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/admin/proxy":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body)
            except Exception:
                self._send_error(400, "Invalid JSON body")
                return
            ok, msg = apply_proxy(payload.get("proxy", ""))
            if ok:
                self._send_json(200, {"ok": True, "description": describe_proxy()})
            else:
                self._send_error(400, msg)
            return
        if path != "/v1/chat/completions":
            self._send_error(404, "Not found. Available: POST /v1/chat/completions, POST /admin/proxy")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except Exception:
            self._send_error(400, "Invalid JSON body")
            return

        model = resolve_model(payload.get("model"))
        is_stream = bool(payload.get("stream", False))

        upstream = dict(payload)
        upstream["model"] = model
        upstream.setdefault("stream", False)

        last_status = None
        last_data = b""
        errors = []
        candidates = self._proxy_candidates()
        for spec in candidates:
            proxy, parse_error = parse_proxy_spec(spec)
            if parse_error:
                errors.append("%s: %s" % (spec, parse_error))
                continue
            session = build_session(proxy)
            try:
                with session.post(
                    UPSTREAM,
                    data=json.dumps(upstream),
                    headers={"Accept": "text/event-stream" if is_stream else "application/json"},
                    stream=is_stream,
                    timeout=PROXY_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status_code != 200:
                        last_status = resp.status_code
                        last_data = resp.content
                        errors.append("%s: HTTP %d" % (spec, resp.status_code))
                        continue

                    if spec != STATE.get("proxy_spec"):
                        apply_proxy(spec)
                    if is_stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Connection", "keep-alive")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        try:
                            for chunk in resp.iter_content(chunk_size=2048):
                                if chunk:
                                    self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
                                    self.wfile.flush()
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        except Exception as e:
                            print("[proxy] stream error: %r" % e, flush=True)
                    else:
                        data = resp.content
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(data)
                    return
            except requests.exceptions.RequestException as e:
                errors.append("%s: %s" % (spec, e))
            except Exception as e:
                errors.append("%s: %s" % (spec, e))

        if last_status is not None:
            self.send_response(last_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(last_data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(last_data)
        else:
            self._send_error(502, "All proxies failed: %s" % "; ".join(errors))


def parse_args():
    p = argparse.ArgumentParser(description="OpenAI-compatible proxy")
    p.add_argument("mode", nargs="?", choices=("test", "proxy"), default=None,
                   help="test: check public proxies and create proxies.json; proxy: run with automatic failover")
    p.add_argument("source", nargs="?", default=DEFAULT_PROXY_SOURCE,
                   help="URL of a text proxy list, used with test")
    return p.parse_args()


def main():
    global CONFIG_PATH
    args = parse_args()
    args.mode = args.mode or "direct"

    if args.mode == "test":
        return check_config_proxies(args.source, 10)

    cfg = load_config()
    configured = cfg.get("proxies", [])
    if not isinstance(configured, list):
        configured = []
    configured = [str(spec).strip() for spec in configured if str(spec).strip()]
    configured = list(dict.fromkeys(configured))
    STATE["proxies"] = configured if args.mode == "proxy" else ["direct"]
    STATE["proxy_spec"] = STATE["proxies"][0] if STATE["proxies"] else "direct"

    srv = ThreadingHTTPServer(("127.0.0.1", 8089), Handler)
    print("OpenAI-compatible free API proxy", flush=True)
    print("Base URL: http://127.0.0.1:8089/v1", flush=True)
    print("API key: any (e.g. 'sk-proxy')", flush=True)
    print("Models:", ", ".join(FREE_MODELS), flush=True)
    print("Mode: %s; proxies: %d" % (args.mode, len(STATE["proxies"])), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
