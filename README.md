# x-mcp-lite

Safety-focused lite fork of [`lord-dubious/x-mcp`](https://github.com/lord-dubious/x-mcp).

> **Note on AI involvement**: This fork was authored by Claude Code (Anthropic) under human direction. The human collaborator (`strobekiss`) made all product decisions — scope of cuts, naming, design tradeoffs (in-memory vs SQLite, active cooldown vs passive sleep, retry vs no-retry on 429, cookie-based auth flow, set_proxy/sing-box integration, etc.) — and reviewed/verified the code at each step. Claude Code did the source reading, pattern analysis, and mechanical refactoring (commenting decorators, wrapping calls with `with_rate_limit`, writing the `throttle.py` / `twikit_patch.py` / `singbox.py` modules). The anti-rate-limit design is informed by reading [`DataWhisker/x-mcp-server`](https://github.com/DataWhisker/x-mcp-server)'s official-API rate-limit module and adapting its "learn from real 429 + active intercept" pattern to twikit's reverse-engineered endpoints.

Keeps the read-only tools + bookmark/like management, **cuts** the high-risk write tools (post/delete tweets, DM, follow/block/mute, groups, cookie ops), and adds:

- An **anti-rate-limit layer** the original project lacks entirely
- A **twikit 2.3.3 patch** for the upstream `KEY_BYTE indices` breakage (x.com changed homepage HTML format on 2026-03-18, twikit hasn't shipped a fix)
- A **cookie-based auth flow** (`get_cookie`) so the server can run on datacenter IPs without triggering Cloudflare blocks

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

### Kept (39 tools)

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
| Cookie setup | `get_cookie` |

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
├── singbox.py        # Archived: historical sing-box management module.
│                     # No longer imported by twitter.py. Kept in the repo
│                     # (and git history) as a record of the proxy/sing-box
│                     # approach. See ARCHIVE.md for details.
└── twitter.py        # 39 MCP tools, all wrapped with throttler.wait()
                      # + with_rate_limit(). get_twitter_client handles
                      # cookie loading / login.
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

### Why there is no proxy support

`x-mcp-lite` previously supported HTTP/SOCKS5 proxies via `X_MCP_PROXY`/`proxy=` and local sing-box forwarding for trojan/anytls/ss/vmess nodes via `set_proxy()`. In practice, **every tested proxy path was blocked by Cloudflare before login could succeed** on the deployment machine (datacenter/VPS IP). The sing-box binary download and startup were eventually fixed, but the outbound nodes themselves could not reach x.com from the server environment.

To keep the auth surface simple and reliable, proxy support was removed. Only cookie-based auth remains. You generate cookies on a network that x.com trusts (e.g. a home browser or residential machine) and copy them to the MCP server. See [ARCHIVE.md](ARCHIVE.md) for the historical proxy/sing-box implementation.

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
| `X_MCP_COOKIES_PATH` | No | `~/.x-mcp/cookies.json` | Path to cookies file. Set this on both the login machine and the deployment machine. |
| `USER_AGENT` | No | twikit default | Custom User-Agent |
| `CAPSOLVER_API_KEY` | No | — | Capsolver API key (only needed if you hit Arkose challenges) |

¹ Only required if you ever use `get_cookie()` Strategy 3 (auto-login). If you only ever paste cookies via `get_cookie(cookie_json=...)`, credentials aren't needed.

### 3. Start

The MCP server starts automatically when your MCP host launches. `uvx` will pull the latest code from GitHub on each startup.

---

## Cookie setup

**This server only supports cookie-based auth.** Proxy-based login was removed because Cloudflare blocks every proxy path we tested from datacenter/VPS IPs (residential proxies, datacenter proxies, trojan/vless nodes, etc.).

**Credentials (`TWITTER_USERNAME` / `TWITTER_PASSWORD`) must be set in the MCP server env at startup** — agents can't pass them at runtime (security: don't let LLMs handle plaintext passwords). They are only used when the server performs auto-login, which usually only makes sense from a residential/uncensored IP.

Use the `get_cookie` MCP tool. It picks a strategy based on what you pass:

### Strategy 1: Paste cookie JSON (no login, no credentials needed)

Best when you already have cookies exported from a browser or received from another machine.

1. Use a browser extension (e.g. EditThisCookie) on x.com while logged in, export as JSON — OR — read the cookies file saved by another x-mcp-lite instance.
2. Call `get_cookie(cookie_json="<the JSON string>")`.

Writes directly to `X_MCP_COOKIES_PATH`. Validates JSON is a non-empty object; atomic write (tmp + rename) won't corrupt an existing file.

### Strategy 2: Copy a local cookie file (no login, no credentials needed)

Best when cookies are already saved somewhere on this machine.

1. Call `get_cookie(cookie_file="/path/to/cookies.json")`.

Reads, validates, and atomically writes to `X_MCP_COOKIES_PATH`. Refuses if source equals target.

### Strategy 3: Auto-login (needs credentials; only reliable from residential IPs)

Best when you don't have cookies yet and the MCP server is running on a network that x.com trusts (e.g. home, office, or a VPN exit that x.com accepts).

1. Ensure `TWITTER_USERNAME` / `TWITTER_PASSWORD` are set in MCP server env.
2. Call `get_cookie()` with no args.
3. twikit logs in directly (no proxy), saves cookies to `X_MCP_COOKIES_PATH`.

If this fails with 403/ConnectTimeout, your server's IP is blocked — use Strategy 1 or 2 instead.

### Agent self-service flow

When an agent calls any tool (e.g. `get_bookmarks`) and cookies are missing/expired, the error message tells the agent to call `get_cookie` (one-line hint). The agent then either:

- Asks the user to paste cookies from elsewhere (browser export or another machine) and calls `get_cookie(cookie_json="<JSON>")`
- Asks the user to copy a local cookies file and calls `get_cookie(cookie_file="<path>")`
- Asks the user to restart the MCP server with `TWITTER_USERNAME` / `TWITTER_PASSWORD` on a trusted network and calls `get_cookie()`

### After cookies are saved

Cookies live at `X_MCP_COOKIES_PATH` (default `~/.x-mcp/cookies.json`). To deploy to another machine:

1. Copy the cookies file to that machine.
2. Set `X_MCP_COOKIES_PATH` to its absolute path there.
3. No proxy is needed — cookies are reused without proxy.

If cookies later expire (Twitter requires re-verification), API calls fail with `AccountLocked` or `Unauthorized`. Re-run `get_cookie` to refresh.

### Default behavior (no `get_cookie` call)

If you skip the `get_cookie` flow entirely, the server will attempt auto-login on first run with `TWITTER_USERNAME` / `TWITTER_PASSWORD` and save cookies to `~/.x-mcp/cookies.json`. This works if the server's IP can reach x.com (residential or trusted VPN); fails with 403 on blocked datacenter IPs — use the cookie flow above for server deployments.

---

## Troubleshooting

### `Couldn't get KEY_BYTE indices`

This means the twikit patch isn't loaded. Check that `twikit_patch.py` is in `src/x_mcp/` and that `twitter.py` imports it before `import twikit`:

```python
from . import twikit_patch  # noqa: F401  must run before `import twikit`
import twikit
```

### `Forbidden (403)` or `NotFound (404 "page does not exist")` on login

The server's IP is blocked by Cloudflare. x-mcp-lite no longer supports proxy-based login (see [ARCHIVE.md](ARCHIVE.md) for the historical reason). Generate cookies on a network that x.com trusts (home, office, or a VPN exit that x.com accepts) and deploy them via `get_cookie(cookie_json=...)` or `get_cookie(cookie_file=...)`.

### `Unauthorized (401)` on API calls

Cookies are missing or expired. Call `get_cookie()` to refresh (see Cookie setup above).

### `Account locked` (Arkose challenge)

The account got flagged by Twitter's anti-automation. Stop, log into x.com in a browser, complete the challenge, then re-run `get_cookie` to refresh cookies.

---

## License

MIT, inherited from the upstream project.
