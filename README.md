# OpenCode Proxy

Small OpenAI-compatible proxy for free OpenCode models.

## Install

```powershell
pip install requests pysocks
```

## Commands

Run without arguments for direct access:

```powershell
python openproxy.py
```

Download the public proxy list from IPLocate, test every entry, show progress,
and write working proxies immediately to `proxies.json`. Failed proxies are
written immediately to `failed.json`:

```powershell
python openproxy.py test
python openproxy.py test https://example.com/raw/proxies.txt
```

Run through the proxies saved in `proxies.json`. If a request gets a non-200
response or a connection error, the next proxy is tried automatically:

```powershell
python openproxy.py proxy
```

The server is available at `http://127.0.0.1:8089/v1` and accepts any API key.

## proxies.json

The file is created by `test` and has this format:

```json
{
  "proxies": [
    "http://host:port",
    "socks4://host:port",
    "socks5://host:port"
  ]
}
```

The source list is:

`https://raw.githubusercontent.com/iplocate/free-proxy-list/refs/heads/main/all-proxies.txt`

Each non-empty line in a custom source is treated as one proxy. Both files are
updated after every check, so already processed results remain saved if the
test is interrupted. On later runs, proxies already present in either
`proxies.json` or `failed.json` are skipped.

Supported formats are `http://`, `https://`, `socks4://`, `socks4a://`,
`socks5://` and `socks5h://`.
