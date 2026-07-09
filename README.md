# x-mcp-lite

Safety-focused lite fork of [`lord-dubious/x-mcp`](https://github.com/lord-dubious/x-mcp).

> **Note on AI involvement**: This fork was authored by Claude Code (Anthropic) under human direction. The human collaborator (`strobekiss`) made all product decisions — scope of cuts, naming, design tradeoffs (in-memory vs SQLite, active cooldown vs passive sleep, retry vs no-retry on 429, cookie-based auth flow, set_proxy/sing-box integration, etc.) — and reviewed/verified the code at each step. Claude Code did the source reading, pattern analysis, and mechanical refactoring (commenting decorators, wrapping calls with `with_rate_limit`, writing the `throttle.py` / `twikit_patch.py` / `singbox.py` modules). The anti-rate-limit design is informed by reading [`DataWhisker/x-mcp-server`](https://github.com/DataWhisker/x-mcp-server)'s official-API rate-limit module and adapting its "learn from real 429 + active intercept" pattern to twikit's reverse-engineered endpoints.

Keeps the read-only tools + bookmark/like management, **cuts** the high-risk write tools (post/delete tweets, DM, follow/block/mute, groups, cookie ops), and adds:

- An **anti-rate-limit layer** the original project lacks entirely
- A **twikit 2.3.3 patch** for the upstream `KEY_BYTE indices` breakage (x.com changed homepage HTML format on 2026-03-18, twikit hasn't shipped a fix)
- A **cookie-based auth flow** (`get_cookie` + `set_proxy`) so the server can run on datacenter IPs without triggering Cloudflare blocks
- A **sing-box integration** (`set_proxy`) for users with only non-HTTP/SOCKS5 proxy protocols (trojan/anytls/ss/vmess/etc)

---

## ⚠️ Risk warning

This is an unofficial reverse-engineering library (via `twikit`) that talks to Twitter's internal endpoints. It is **not** affiliated with Twitter/X. Use of such libraries may violate Twitter's Terms of Service and can lead to:

- Account being **rate-limited** (transient, recovers in ~15 min)
- Account being **locked** (requires Arkose challenge / phone verification)
- Account being **suspended** (permanent)

**Use at your own risk.** Recommendations:
- Use a secondary account, not your main one
- Keep call frequency low (the built-in throttler defaults to 2–5s random intervals)
- Do not run unattended long-running jobs
- If the account gets locked, stop and verify manually before resuming

---

## What's kept vs cut

### Kept (40 tools)

| Category | Tools |
|----------|-------|
| Bookmarks | `get_bookmarks` / `get_all_bookmarks` / `get_bookmark_folders` / `bookmark_tweet` / `delete_bookmark` |
| Tweet search/detail | `get_tweet_by_id` / `search_twitter` / `get_tweet_details` / `get_conversation_thread` / `get_similar_tweets` |
| User info | `get_user_id` / `get_user` / `get_user_by_screen_name` / `get_user_by_id` / `get_user_profile` / `get_user_mentions` / `get_user_followers_you_know` |
| Follower/following lists | `get_user_followers` / `get_latest_followers` / `get_user_following` / `get_latest_friends` / `get_user_verified_followers` / `get_user_subscriptions` / `get_followers_ids` / `get_friends_ids` |
| Timeline/search | `get_timeline` / `get_latest_timeline` / `get_trends` / `get_highlights_tweets` / `search_user` / `get_user_tweets` |
| DM read-only | `get_dm_history` |
| Retweeters/favoriters | `get_retweeters` / `get_favoriters` |
| Community note | `get_community_note` |
| Scheduled (read) | `get_scheduled_tweets` |
| Likes (low-risk write) | `favorite_tweet` / `unfavorite_tweet` |
| Cookie/proxy setup | `get_cookie` / `set_proxy` |

### Cut (37 tools, `@mcp.tool()` decorators commented — function bodies retained for diff clarity)

Post/delete tweets, polls, scheduled tweets, retweets, all DM write operations, all group operations, follow/unfollow/block/unblock/mute/unmute, `set_delegate_account`, `update_user`, all cookie management (`get_cookies` / `save_cookies` / `set_cookies` / `load_cookies` / `logout` / `unlock`), `delete_all_bookmarks`, geo (`reverse_geocode` / `search_geo` / `get_place`), media metadata, bookmark folder create/edit, `vote` / `vote_on_poll`.

---

## Architecture

```
src/x_mcp/
├── __init__.py
├── twikit_patch.py   # Patches twikit 2.3.3 ClientTransaction.get_indices
│                     # for the 2026-03-18 x.com HTML format change.
│                     # Loaded BEFORE `import twikit` via twitter.py.
├── throttle.py       # Anti-rate-limit layer:
│                     # - Throttler (2-5s random pacing, memory)
│                     # - with_rate_limit (active cooldown tracking,
│                     #   429 retry with real reset time, no-retry on
│                     #   other TwitterException subtypes — just attach
│                     #   the "Call get_cookie()" hint)
│                     # - paginate_all (3-8s inter-page delay)
├── singbox.py        # sing-box binary download (httpx, github + 3
│                     # mirrors), config builder (HTTP inbound + user
│                     # outbound), process management (start/stop/kill),
│                     # auto-cleanup on success or exit. Config file is
│                     # kept until stop_singbox (not deleted right after
│                     # Popen, since Popen is async and sing-box may
│                     # still be reading it).
└── twitter.py        # 40 MCP tools, all wrapped with throttler.wait()
                      # + with_rate_limit(). get_twitter_client handles
                      # cookie loading / login / proxy resolution.
```

### Anti-rate-limit layer (`throttle.py`)

1. **`Throttler`** — random-pacing throttler (in-memory). Every tool call awaits `throttler.wait()` first, which sleeps to enforce a 2–5s random interval since the last call. Randomization avoids fixed-pattern detection.

2. **`with_rate_limit(endpoint, fn)`** — DataWhisker-style active cooldown tracking (in-memory dict).
   - Before calling: if a previous 429 recorded a reset time for this endpoint, sleep until that time (active intercept — no wasted request).
   - On `TooManyRequests`: read `e.rate_limit_reset` (from the `x-rate-limit-reset` response header, confirmed in twikit 2.3.3 `errors.py`), record it, sleep, retry once.
   - On any other `TwitterException` subtype (`BadRequest` / `Unauthorized` / `Forbidden` / `NotFound` / `AccountLocked` / `AccountSuspended` / etc): convert to `RuntimeError` with a short "Call get_cookie()" hint, do not retry. Catching the base class means we don't have to enumerate every subtype — x.com sometimes returns 404 on datacenter-IP login, which twikit raises as `NotFound`; the base catch handles it.

3. **`paginate_all`** — for `get_all_bookmarks`. Iterates `Result.next()` with 3–8s random delay between pages, auto-backoff on 429.

**No persistence**: in-memory dicts only. Twitter's rate-limit window is 15 min; for low-frequency usage, restart intervals exceed the window anyway. Memory is also faster (no disk I/O).

### twikit patch (`twikit_patch.py`)

twikit 2.3.3's `ClientTransaction.get_indices` raises `Couldn't get KEY_BYTE indices` because x.com changed its homepage HTML on 2026-03-18 — the `ondemand.s` filename and its hash are now split into two separate `,<N>:"..."` entries instead of one inline `"ondemand.s":"<hash>"`. Upstream issue: [`d60/twikit#408`](https://github.com/d60/twikit/issues/408). Fix is upstream in [`iSarabjitDhiman/XClientTransaction`](https://github.com/iSarabjitDhiman/XClientTransaction) commit `2ff8438`, but twikit hasn't pulled it in.

`twikit_patch.py` monkey-patches `ClientTransaction.get_indices` at import time using the regex from [`@audioeng89`'s comment](https://github.com/d60/twikit/issues/408#issuecomment-4089055868). Must run before `from twikit import Client`. Remove this module once twikit ships a fixed release.

### sing-box integration (`singbox.py`)

Used by `set_proxy(outbound=...)` to turn non-HTTP proxy protocols (trojan/anytls/ss/etc) into a local HTTP proxy that twikit/httpx can consume directly. Flow:

1. Resolve binary: try `sing-box` in PATH first; if not found, download via `httpx` from github.com, then 3 mirrors (120s timeout each, all fail → tell user to install manually). We switched from `urllib.request` to `httpx` because `urllib` silently failed in containers (GitHub 302 redirects weren't followed).
2. Build config: HTTP inbound on `127.0.0.1:0` (OS picks port) + user's outbound + route everything through it.
3. Write config to a tmp file. The path is stored in a module-level `_cfg_path` and cleaned up in `stop_singbox()` — **not** immediately after `Popen`, because `subprocess.Popen` is async and returns before sing-box has read the file.
4. Start subprocess; read actual port from stderr (10s timeout).
5. Return `http://127.0.0.1:<port>` as the active proxy.
6. On `stop_singbox()`: terminate → 5s → kill; delete downloaded binary if we managed it (PATH-installed binary is left alone); delete the tmp config file.

`atexit.register(singbox.stop_singbox)` ensures cleanup even if MCP server crashes.

---

## Setup

### 1. Clone

```bash
git clone https://github.com/strobekiss/x-mcp-lite
cd x-mcp-lite
```

### 2. Configure in your MCP host (e.g., Claude Desktop, mcphub, Cursor)

```json
{
    "x-mcp-lite": {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/strobekiss/x-mcp-lite", "x-mcp-lite"],
        "env": {
            "TWITTER_USERNAME": "your_username",
            "TWITTER_PASSWORD": "your_password"
        }
    }
}
```

### Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TWITTER_USERNAME` | Yes¹ | — | X username (used by auto-login) |
| `TWITTER_PASSWORD` | Yes¹ | — | X password (used by auto-login) |
| `TWITTER_EMAIL` | No | — | Email if your account has one; omitted if not set (twikit accepts `auth_info_2=None`) |
| `X_MCP_COOKIES_PATH` | No | `~/.x-mcp/cookies.json` | Path to cookies file |
| `X_MCP_PROXY` | No | — | HTTP/HTTPS/SOCKS5 proxy URL fallback for `get_cookie()` auto-login. Agents can also pass `proxy=` at call time, or use `set_proxy(outbound=...)` for non-HTTP/SOCKS5 protocols. |
| `USER_AGENT` | No | twikit default | Custom User-Agent |
| `CAPSOLVER_API_KEY` | No | — | Capsolver API key (only needed if you hit Arkose challenges) |

¹ Only required if you ever use `get_cookie()` Strategy 3 (auto-login). If you only ever paste cookies via `get_cookie(cookie_json=...)`, credentials aren't needed.

### 3. Start

The MCP server starts automatically when your MCP host launches. `uvx` will pull the latest code from GitHub on each startup.

---

## Cookie setup

Twitter's Cloudflare blocks login requests from datacenter IPs with HTTP 403. **Credentials (`TWITTER_USERNAME` / `TWITTER_PASSWORD`) must be set in the MCP server env at startup** — agents can't pass them at runtime (security: don't let LLMs handle plaintext passwords). The proxy, however, can be passed at runtime.

Any proxy that can reach x.com works for login. Residential IPs are **recommended** (less likely to be blocked by Cloudflare), but datacenter proxies, VPN nodes, trojan/anytls/ss nodes etc all work — if Cloudflare blocks one, try another.

Use the `get_cookie` MCP tool. It picks a strategy based on what you pass:

### Strategy 1: Paste cookie JSON (no login, no proxy, no credentials needed)

Best when you already have cookies exported from a browser or received from another machine.

1. Use a browser extension (e.g. EditThisCookie) on x.com while logged in, export as JSON — OR — read the cookies file saved by another x-mcp-lite instance.
2. Call `get_cookie(cookie_json="<the JSON string>")`.

Writes directly to `X_MCP_COOKIES_PATH`. Validates JSON is a non-empty object; atomic write (tmp + rename) won't corrupt an existing file. **Note**: this transfers login tokens — prefer Strategy 3 when possible.

### Strategy 2: Copy a local cookie file (no login, no proxy, no credentials needed)

Best when cookies are already saved somewhere on this machine.

1. Call `get_cookie(cookie_file="/path/to/cookies.json")`.

Reads, validates, and atomically writes to `X_MCP_COOKIES_PATH`. Refuses if source equals target.

### Strategy 3: Auto-login (needs credentials + optional proxy)

Best when you don't have cookies yet and want to log into Twitter fresh.

1. Ensure `TWITTER_USERNAME` / `TWITTER_PASSWORD` are set in MCP server env.
2. Pick a proxy method:
   - **HTTP/HTTPS/SOCKS5 proxy URL** (any proxy that can reach x.com): call `get_cookie(proxy="http://user:pass@host:port")` or `socks5://host:port`.
   - **Other protocols** (trojan/anytls/ss/vmess/etc): call `set_proxy(outbound="<sing-box outbound JSON>")` first, then `get_cookie()` with no args.
   - **No proxy** (server already on a residential IP): call `get_cookie()` with no args.

twikit logs in via the chosen proxy (or direct), saves cookies to `X_MCP_COOKIES_PATH`. On success, sing-box (if started) is auto-stopped and its binary deleted — one-shot pattern.

#### `set_proxy` for non-HTTP/SOCKS5 protocols

If you only have trojan/anytls/ss/vmess nodes (no HTTP/SOCKS5 proxy URL), use `set_proxy`:

```python
# Agent passes a sing-box outbound JSON
await set_proxy(outbound='{"type":"trojan","server":"tw.example.com","server_port":443,"password":"xxx","tls":{"enabled":true,"server_name":"tw.example.com"}}')

# Then call get_cookie() — it picks up the local proxy automatically
await get_cookie()
```

sing-box binary is downloaded to `~/.x-mcp/singbox-bin/` on first use (tries github.com first, then 3 mirrors, then a user-installed `sing-box` in PATH). After `get_cookie` succeeds, sing-box is stopped and the downloaded binary deleted. Pass `None` to `set_proxy` to stop manually.

#### `get_cookie` with no args and no proxy configured

If you call `get_cookie()` with no args and there's no `set_proxy`-started sing-box and no `X_MCP_PROXY` env var, the tool returns a short prompt asking for a proxy URL or sing-box outbound JSON. Agents see this and know what to do next.

### Agent self-service flow

When an agent calls any tool (e.g. `get_bookmarks`) and cookies are missing/expired, the error message tells the agent to call `get_cookie` (one-line hint). The agent then either:

- Asks the user for an HTTP/HTTPS/SOCKS5 proxy URL and calls `get_cookie(proxy="<url>")`
- Asks the user for a trojan/anytls/ss/vmess node config, converts to sing-box outbound JSON, calls `set_proxy(outbound="<JSON>")` then `get_cookie()`
- Asks the user to paste cookies from elsewhere (browser export or another machine) and calls `get_cookie(cookie_json="<JSON>")`

If `TWITTER_USERNAME` / `TWITTER_PASSWORD` are missing, the error tells the user to restart the MCP server with these env vars configured.

### After cookies are saved

Cookies live at `X_MCP_COOKIES_PATH` (default `~/.x-mcp/cookies.json`). To deploy to another machine:

1. Copy the cookies file to that machine.
2. Set `X_MCP_COOKIES_PATH` to its absolute path there.
3. Don't set `X_MCP_PROXY` there — cookies are reused without proxy.

If cookies later expire (Twitter requires re-verification), API calls fail with `AccountLocked` or `Unauthorized`. Re-run `get_cookie` to refresh.

### Default behavior (no `get_cookie` call)

If you skip the `get_cookie` flow entirely, the server will attempt auto-login on first run with `TWITTER_USERNAME` / `TWITTER_PASSWORD` and save cookies to `~/.x-mcp/cookies.json`. This works if the server's IP can reach x.com (residential or any proxy); fails with 403 on blocked datacenter IPs — use the `get_cookie` flow above for server deployments.

---

## Example: server deployment with trojan node

This is the recommended path if your MCP server runs on a datacenter IP (VPS, container) and you only have trojan/anytls/ss nodes.

1. **On your local machine** (residential IP, or behind a VPN):
   - Configure `x-mcp-lite` with `TWITTER_USERNAME` / `TWITTER_PASSWORD`.
   - In your MCP client, call `set_proxy(outbound="<sing-box outbound JSON for your trojan node>")`.
   - Call `get_cookie()` with no args. sing-box starts, twikit logs in via it, cookies are saved to `~/.x-mcp/cookies.json`, sing-box stops and binary is deleted.

2. **Deploy to the server**:
   - Copy `~/.x-mcp/cookies.json` from your local machine to the server (e.g. `/opt/mcphub/data/x-mcp-lite/cookies.json`).
   - Configure the MCP server with `X_MCP_COOKIES_PATH=/opt/mcphub/data/x-mcp-lite/cookies.json` and `TWITTER_USERNAME` / `TWITTER_PASSWORD` (the latter are only used if cookies are missing/expired).
   - Don't set `X_MCP_PROXY` — the server reuses cookies without proxy.

3. **When cookies expire**:
   - Re-run step 1 on your local machine to refresh `~/.x-mcp/cookies.json`.
   - Re-copy the file to the server.

---

## Troubleshooting

### `Couldn't get KEY_BYTE indices`

This means the twikit patch isn't loaded. Check that `twikit_patch.py` is in `src/x_mcp/` and that `twitter.py` imports it before `import twikit`:

```python
from . import twikit_patch  # noqa: F401  must run before `import twikit`
import twikit
```

### `Forbidden (403)` or `NotFound (404 "page does not exist")` on login

Both mean the server's IP is blocked by Cloudflare — 403 is the explicit block, 404 is x.com returning a generic "page does not exist" body for blocked datacenter IPs. Use `get_cookie(proxy="<proxy URL>")` (HTTP/HTTPS/SOCKS5) or `set_proxy(outbound="<sing-box JSON>")` + `get_cookie()` (trojan/anytls/ss/etc) to route login through any proxy that can reach x.com. Residential proxies are most reliable but any proxy works.

### `Unauthorized (401)` on API calls

Cookies are missing or expired. Call `get_cookie()` to refresh (see Cookie setup above).

### `Account locked` (Arkose challenge)

The account got flagged by Twitter's anti-automation. Stop, log into x.com in a browser, complete the challenge, then re-run `get_cookie` to refresh cookies.

### sing-box tmp config file: "no such file or directory"

This was a bug where the tmp config was deleted immediately after `Popen` (Popen is async, so sing-box hadn't read it yet). Fixed in commit `44715b2` — config is now kept in `_cfg_path` and cleaned up by `stop_singbox()`. If you still see this error, your `uvx` cache is stale; restart the MCP host to re-pull.

### sing-box binary download fails

The `~/.x-mcp/singbox-bin/` directory ends up empty after `set_proxy`. This was a bug with `urllib.request` not following GitHub 302 redirects in some containers; fixed in commit `285bbfd` by switching to `httpx`. If you still see this, install manually:

- macOS: `brew install sing-box`
- Linux: download from [github.com/SagerNet/sing-box/releases](https://github.com/SagerNet/sing-box/releases), put the binary in your `PATH`.

`set_proxy` will detect the system-installed `sing-box` and use it without downloading.

---

## License

MIT, inherited from the upstream project.
