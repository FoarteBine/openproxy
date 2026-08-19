import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

UPSTREAM = "https://opencode.ai/zen/v1/chat/completions"
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
VALID_PROXIES = ["", "direct"] + list(SUPPORTED_SCHEMES) + ["wg"]  # short names usable without ://

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openai_proxy.json")
LOCK = threading.Lock()

STATE = {
    "proxy": "",       # current effective proxy string ("" = direct)
    "wg_iface": None,  # resolved wireguard interface name
    "insecure": False,
    "session": None,
}


def resolve_model(raw):
    m = raw or DEFAULT_MODEL
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    if m in FREE_MODELS:
        return m
    return DEFAULT_MODEL


def list_wg_interfaces():
    """Return list of WireGuard interface names (Windows: 'WireGuard Tunnel'; POSIX: wg show)."""
    ifaces = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetAdapter | Where-Object { $_.InterfaceDescription -like '*WireGuard*' -or $_.Name -like '*WireGuard*' } | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                line = line.strip()
                if line:
                    ifaces.append(line)
        else:
            try:
                out = subprocess.run(["wg", "show", "interfaces"], capture_output=True, text=True, timeout=10).stdout
                for line in out.split():
                    if line.strip():
                        ifaces.append(line.strip())
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return ifaces


def wg_interface_exists(name):
    try:
        if os.name == "nt":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True).stdout
            return bool(out and name.lower() in out.lower())
        out = subprocess.run(["ip", "link", "show", name], capture_output=True, text=True)
        return out.returncode == 0
    except Exception:
        return False


def resolve_wg(name):
    """If name given, verify it. Else pick first active WireGuard interface."""
    if name:
        if wg_interface_exists(name):
            return name, None
        return None, "interface '%s' not found" % name
    ifaces = list_wg_interfaces()
    if ifaces:
        return ifaces[0], None
    return None, "no active WireGuard interface found"


def build_session(proxy, insecure):
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Authorization": "Bearer " + UPSTREAM_KEY,
        "User-Agent": UPSTREAM_UA,
        "x-opencode-client": "desktop",
    })
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    if insecure:
        import urllib3
        s.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


def parse_proxy_spec(spec, insecure):
    """Normalize a proxy spec to ('proxy_string_or_None', 'wg_iface', 'error')."""
    spec = (spec or "").strip()
    if spec in ("", "direct", "none"):
        return None, None, None
    if spec == "wg":
        iface, err = resolve_wg(None)
        if err:
            return None, None, err
        return None, iface, None
    if spec.startswith("wg://"):
        iface, err = resolve_wg(spec[len("wg://"):].rstrip("/"))
        if err:
            return None, None, err
        return None, iface, None
    scheme = spec.split("://", 1)[0].lower()
    if scheme in SUPPORTED_SCHEMES:
        return spec, None, None
    return None, None, "unsupported proxy scheme '%s' (use http/https/socks4/socks5/wg)" % scheme


def load_config():
    cfg = {"proxy": "", "insecure": False}
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


def apply_proxy(spec, insecure):
    """Set proxy on the running server. Returns (ok, message)."""
    with LOCK:
        if insecure is not None:
            STATE["insecure"] = insecure
        proxy, iface, err = parse_proxy_spec(spec, STATE["insecure"])
        if err:
            return False, err
        STATE["proxy"] = proxy or ""
        STATE["wg_iface"] = iface
        STATE["session"] = build_session(proxy, STATE["insecure"])
        save_config({"proxy": STATE["proxy"], "wg": iface, "insecure": STATE["insecure"]})
        return True, describe_proxy()


def describe_proxy():
    if STATE["wg_iface"]:
        return "WireGuard interface '%s' (direct, tunnel at OS level)" % STATE["wg_iface"]
    if STATE["proxy"]:
        return "proxy %s" % STATE["proxy"]
    return "direct (no proxy)"


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
                "current": STATE["proxy"] or ("wg://%s" % STATE["wg_iface"] if STATE["wg_iface"] else "direct"),
                "description": describe_proxy(),
                "wg_interfaces": list_wg_interfaces(),
                "supported": list(SUPPORTED_SCHEMES) + ["wg"],
                "usage": "/admin/proxy?set=http://host:port | socks5://host:port | wg | direct",
            })
        elif path == "/admin/proxy/set":
            spec = q.get("set", [""])[0]
            ok, msg = apply_proxy(spec, None)
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
            ok, msg = apply_proxy(payload.get("proxy", ""), payload.get("insecure", None))
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

        try:
            with STATE["session"].post(
                UPSTREAM,
                data=json.dumps(upstream),
                headers={"Accept": "text/event-stream" if is_stream else "application/json"},
                stream=is_stream,
                timeout=300,
            ) as resp:
                if is_stream:
                    self.send_response(resp.status_code)
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
                    self.send_response(resp.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
        except requests.exceptions.RequestException as e:
            self._send_error(502, "Upstream error (%s): %r" % (describe_proxy(), e))
        except Exception as e:
            self._send_error(502, "Upstream error: %r" % e)


def parse_args():
    p = argparse.ArgumentParser(description="OpenAI-compatible proxy for free opencode models")
    sub = p.add_subparsers(dest="cmd")
    ps = sub.add_parser("set", help="Switch routing of a running server (no server restart needed)")
    ps.add_argument("proxy", help="http://... | https://... | socks4://... | socks5://... | wg://IFACE | wg | direct")
    ps.add_argument("--host", default=os.environ.get("OPENAI_PROXY_HOST", "127.0.0.1"))
    ps.add_argument("--port", type=int, default=int(os.environ.get("OPENAI_PROXY_PORT", "8089")))
    ps.add_argument("--insecure", action="store_true", default=os.environ.get("OPENAI_PROXY_INSECURE") == "1",
                    help="Skip TLS certificate verification for the proxy (self-signed HTTPS proxies).")
    pg = p.add_argument_group("server")
    pg.add_argument("--host", default=os.environ.get("OPENAI_PROXY_HOST", "127.0.0.1"))
    pg.add_argument("--port", type=int, default=int(os.environ.get("OPENAI_PROXY_PORT", "8089")))
    pg.add_argument("--proxy", default=os.environ.get("OPENAI_PROXY", ""),
                    help="Initial proxy. http:// https:// socks4:// socks4a:// socks5:// socks5h:// wg://IFACE, wg (auto), or direct. Empty = direct.")
    pg.add_argument("--insecure", action="store_true", default=os.environ.get("OPENAI_PROXY_INSECURE") == "1",
                    help="Skip TLS certificate verification for the proxy (self-signed HTTPS proxies).")
    pg.add_argument("--config", default=CONFIG_PATH, help="Path to config file (default: next to script)")
    return p.parse_args()


def cmd_set(args):
    import urllib.request
    import urllib.error
    from urllib.parse import quote
    url = "http://%s:%d/admin/proxy/set?set=%s" % (args.host, args.port, quote(args.proxy, safe=""))
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            print(r.read().decode(), flush=True)
    except urllib.error.HTTPError as e:
        print("ERROR %d: %s" % (e.code, e.read().decode()), flush=True)
    except Exception as e:
        print("ERROR: cannot reach server at %s:%d (%r)" % (args.host, args.port, e), flush=True)


def main():
    global CONFIG_PATH
    args = parse_args()

    if args.cmd == "set":
        cmd_set(args)
        return

    CONFIG_PATH = args.config

    cfg = load_config()
    initial = args.proxy if args.proxy else cfg.get("proxy", "")
    if initial == "" and cfg.get("wg"):
        initial = "wg://%s" % cfg["wg"]
    insecure = args.insecure or bool(cfg.get("insecure", False))

    ok, msg = apply_proxy(initial, insecure)
    if not ok:
        print("[!] %s — starting direct." % msg, flush=True)
        apply_proxy("", insecure)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("OpenAI-compatible free API proxy", flush=True)
    print("Base URL: http://%s:%d/v1" % (args.host, args.port), flush=True)
    print("API key: any (e.g. 'sk-proxy')", flush=True)
    print("Models:", ", ".join(FREE_MODELS), flush=True)
    print("Routing: %s" % describe_proxy(), flush=True)
    print("Switch at runtime:", flush=True)
    print("  GET  /admin/proxy?set=http://host:port | socks5://host:port | wg | direct", flush=True)
    print("  POST /admin/proxy  body: {\"proxy\": \"socks5://...\", \"insecure\": false}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()