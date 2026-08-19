# OpenCode Proxy

An OpenAI-compatible proxy for opencode's free models (`deepseek-v4-flash-free` and more)
without an API key. The server listens on `http://127.0.0.1:8089/v1` and can reach the
upstream directly, through an HTTP/HTTPS/SOCKS proxy, or through an active WireGuard
interface.

## Requirements

```bash
pip install requests pysocks
```

## Quick Start

```bash
python opencode_proxy.py
```

Point any OpenAI-compatible client to:

```
Base URL: http://127.0.0.1:8089/v1
API key:  any value (e.g. sk-proxy)
```

- `GET  /v1/models` — list of free models
- `POST /v1/chat/completions` — regular and streaming (`stream: true`) requests
- `model` accepts `deepseek-v4-flash-free` or `opencode/deepseek-v4-flash-free`;
  unknown names fall back to the default model

## Proxy at startup

```bash
# HTTP proxy with authentication
python opencode_proxy.py --proxy http://user:pass@host:port

# HTTPS proxy (certificate verified)
python opencode_proxy.py --proxy https://host:port

# HTTPS proxy with self-signed certificate
python opencode_proxy.py --proxy https://host:port --insecure

# SOCKS4
python opencode_proxy.py --proxy socks4://host:port

# SOCKS5
python opencode_proxy.py --proxy socks5://host:port

# SOCKS5 with DNS resolution on the proxy side
python opencode_proxy.py --proxy socks5h://host:port

# WireGuard: specific profile
python opencode_proxy.py --proxy wg://AmneziaVPN

# WireGuard: auto-pick active interface
python opencode_proxy.py --proxy wg
```

## Switch at runtime (no restart)

### Via arguments (recommended)

```bash
# show current state and available WG interfaces
python opencode_proxy.py set ?

# direct
python opencode_proxy.py set direct

# auto-WireGuard
python opencode_proxy.py set wg

# specific WG profile
python opencode_proxy.py set wg://AmneziaVPN

# SOCKS5
python opencode_proxy.py set socks5://host:port

# HTTP
python opencode_proxy.py set http://host:port
```

### Via HTTP

```bash
curl "http://127.0.0.1:8089/admin/proxy/set?set=wg"
curl -X POST http://127.0.0.1:8089/admin/proxy -H "Content-Type: application/json" -d '{"proxy":"wg"}'
curl http://127.0.0.1:8089/admin/proxy   # current state + WG interface list
```

## Supported proxy schemes

| Scheme | Example | Notes |
| --- | --- | --- |
| `http://` | `http://host:port` | HTTP proxy |
| `https://` | `https://host:port` | HTTPS proxy |
| `socks4://` | `socks4://host:port` | SOCKS4 |
| `socks5://` | `socks5://host:port` | SOCKS5 |
| `socks5h://` | `socks5h://host:port` | SOCKS5 with remote DNS |
| `wg://` | `wg://AmneziaVPN` | WireGuard profile (Windows: adapter name) |
| `wg` | `wg` | auto-pick active WireGuard interface |

## Configuration

Settings are saved to `opencode_proxy.json` next to the script and are applied
automatically on the next launch.

Environment variables: `OPENAI_PROXY_HOST`, `OPENAI_PROXY_PORT`,
`OPENAI_PROXY` (default proxy), `OPENAI_PROXY_INSECURE=1`.

> [!WARNING]
> Use `--insecure` only when you understand the certificate verification risks.