# Archived: proxy / sing-box support

> Status: **removed** as of the commit that introduced this file.  
> The implementation remains in git history and in `src/x_mcp/singbox.py` (no longer imported by `twitter.py`).

## Why it was added

The original `x-mcp-lite` fork inherited `set_proxy()` from `lord-dubious/x-mcp`. The idea was to let users who only had non-HTTP/SOCKS5 proxy nodes (trojan/anytls/ss/vmess/etc) start a local sing-box HTTP inbound on the MCP server, then route twikit's login through that node. This avoided forcing users to find or buy a plain HTTP/SOCKS5 proxy.

The flow was:

1. User calls `set_proxy(outbound="<sing-box outbound JSON>")`.
2. `singbox.py` downloads `sing-box` to `~/.x-mcp/singbox-bin/` (or uses a system one), builds a config with an HTTP inbound on `127.0.0.1:0`, and starts it.
3. `get_cookie()` picks up the local proxy and runs twikit login through it.
4. On success, `stop_singbox()` kills the process and keeps the binary cached.

A plain `proxy=` argument and `X_MCP_PROXY` env var for HTTP/SOCKS5 proxies were also supported.

## Why it was removed

In real-world use, **every tested proxy path was blocked by Cloudflare before login could succeed**:

- Residential proxy URLs (`X_MCP_PROXY` / `proxy=`) worked in theory but the available nodes were either rate-limited or rejected.
- Datacenter/VPS trojan/vless nodes were blocked outright (the server could not even open TCP to the node endpoint).
- The local sing-box startup itself was fixed, but once started the outbound connection could not reach x.com reliably from the deployment environment.

Because the proxy support added binary downloads, temp config management, stderr parsing, and a non-trivial attack surface without delivering a working login path, we decided to drop it and rely exclusively on cookie-based auth. Cookies are generated on a machine/network that x.com trusts (e.g. a home browser) and copied to the MCP server, which then reuses them without any network proxy.

## What is preserved

- `src/x_mcp/singbox.py` — the full sing-box management module. It is no longer imported by `twitter.py`, but kept in the repo for reference and in case we ever revisit the approach.
- `set_proxy()` in `src/x_mcp/twitter.py` — its `@mcp.tool()` decorator is commented out, so MCP clients no longer see it. The function body is preserved to avoid unnecessary code churn and to keep the archived logic readable.
- Git history — every iteration, including the stderr-read fix and the binary-cache fix, is in the commit log.

## Current recommended auth flow

1. On a machine/network that x.com trusts (home, office, VPN that x.com accepts), run `get_cookie()` with `TWITTER_USERNAME`/`TWITTER_PASSWORD` set. It will try direct auto-login and save cookies to `X_MCP_COOKIES_PATH`.
2. Copy the cookies file to the MCP server.
3. Set `X_MCP_COOKIES_PATH` on the server and restart the MCP host. No proxy, no login, no sing-box.
4. If direct auto-login also fails, paste the cookies JSON via `get_cookie(cookie_json=...)` or copy a file via `get_cookie(cookie_file=...)`.
