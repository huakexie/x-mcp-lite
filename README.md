# x-mcp-lite

Safety-focused lite fork of [`lord-dubious/x-mcp`](https://github.com/lord-dubious/x-mcp).

> **Note on AI involvement**: This fork was authored by Claude Code (Anthropic) under human direction. The human collaborator (`strobekiss`) made all product decisions — scope of cuts, naming, design tradeoffs (in-memory vs SQLite, active cooldown vs passive sleep, retry vs no-retry on 429, cookie-based auth flow, set_proxy/sing-box integration, etc.) — and reviewed/verified the code at each step. Claude Code did the source reading, pattern analysis, and mechanical refactoring (commenting decorators, wrapping calls with `with_rate_limit`, writing the `throttle.py` / `twikit_patch.py` / `singbox.py` modules). The anti-rate-limit design is informed by reading [`DataWhisker/x-mcp-server`](https://github.com/DataWhisker/x-mcp-server)'s official-API rate-limit module and adapting its "learn from real 429 + active intercept" pattern to twikit's reverse-engineered endpoints.

Keeps the read-only tools + bookmark/like management, **cuts** the high-risk write tools (post/delete tweets, DM, follow/block/mute, groups, cookie ops), and adds:

- An **anti-rate-limit layer** the original project lacks entirely
- **twikit 2.3.3 patches** for three upstream bugs it hasn't shipped fixes for: the `KEY_BYTE indices` breakage (x.com changed homepage HTML on 2026-03-18), `KeyError` crashes when parsing users whose accounts omit optional `legacy.*` fields (e.g. no bio link → `KeyError: 'urls'`), and `KeyError: 'itemContent'` in `get_tweet_by_id` from x.com's flattened cursor entries
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
├── twikit_patch.py   # Three twikit 2.3.3 monkey-patches, applied at import:
│                     # - ClientTransaction.get_indices (2026-03-18 x.com
│                     #   HTML format change)
│                     # - User.__init__ (KeyError on optional legacy.*
│                     #   fields x.com omits, e.g. no-bio-link accounts)
│                     # - GQLClient.tweet_detail (KeyError itemContent from
│                     #   x.com's flattened cursor entries)
│                     # Loaded BEFORE `import twikit` via twitter.py.
├── throttle.py       # Anti-rate-limit layer (state persisted to
│                     # ~/.x-mcp/throttle_state.json):
│                     # - Throttler (2-5s random pacing, persisted)
│                     # - with_rate_limit (rolling request budget +
│                     #   active cooldown + bounded 429 backoff/retry;
│                     #   distinct AccountLocked/Suspended handling;
│                     #   get_cookie hint on other TwitterException)
│                     # - paginate_all (3-8s inter-page delay, budget-aware)
├── singbox.py        # Archived: historical sing-box management module.
│                     # No longer imported by twitter.py. Kept in the repo
│                     # (and git history) as a record of the proxy/sing-box
│                     # approach. See ARCHIVE.md for details.
└── twitter.py        # 39 MCP tools, all wrapped with throttler.wait()
                      # + with_rate_limit(). get_twitter_client handles
                      # cookie loading / login.
```

### Anti-rate-limit layer (`throttle.py`)

1. **`Throttler`** — random-pacing throttler. Every tool call awaits `throttler.wait()` first, which sleeps to enforce a 2–5s random interval since the last call. Randomization avoids fixed-pattern detection. The last-call timestamp is **persisted**, so pacing holds even when the host spawns the server fresh per call.

2. **`with_rate_limit(endpoint, fn)`** — DataWhisker-style active cooldown tracking, plus a request budget and bounded backoff. In order:
   - **Rolling budget**: if we've already made `X_MCP_MAX_CALLS_PER_WINDOW` calls (default 250) in the last 15 min, refuse with a clear "try again in Ns" error instead of adding fuel to a burst that could trip detection. Set to `0` to disable.
   - **Active cooldown**: if a previous 429 recorded a reset time for this endpoint, either sleep it out (if within `X_MCP_MAX_BACKOFF`) or refuse with a retry hint (if further out) — active intercept, no wasted request.
   - On `TooManyRequests`: read `e.rate_limit_reset` (from the `x-rate-limit-reset` response header, confirmed in twikit 2.3.3 `errors.py`), record it (persisted), then **sleep + retry once only if the wait is within `X_MCP_MAX_BACKOFF`** (default 60s); otherwise refuse with a retry hint. This caps in-call blocking so a single call can't hang for the full 900s window and blow past your MCP client's timeout — the cooldown is still honored on the next call.
   - On `AccountLocked` / `AccountSuspended`: convert to a `RuntimeError` that says re-cookieing **won't** help and the account must be fixed manually in a browser (no misleading `get_cookie()` hint).
   - On any other `TwitterException` subtype (`BadRequest` / `Unauthorized` / `Forbidden` / `NotFound` / etc): convert to `RuntimeError` with the short "Call get_cookie()" hint, do not retry.

3. **`paginate_all`** — for `get_all_bookmarks`. Iterates `Result.next()` with 3–8s random delay between pages; each page goes through `with_rate_limit` so the budget, cooldown and 429 backoff all apply, and any refusal cleanly stops pagination and returns what was collected so far. Default `max_pages=50` (~1000 bookmarks).

**Persistence**: pacing (`last_call`), per-endpoint 429 cooldowns (`resets`), and the rolling request log (`calls`) are stored in a small JSON file (default `~/.x-mcp/throttle_state.json`, override with `X_MCP_STATE_PATH`; atomic writes, tolerant of a missing/corrupt file). This is what lets a cooldown survive across per-session server spawns (e.g. mcphub over stdio), instead of being lost every time the process restarts.

### Remaining limitations

Documented so you know what the layer still does **not** cover:

- **Budget/pacing count tool calls, not underlying HTTP requests.** Some tools issue several requests internally (e.g. `get_user` does `settings` + `get_user_by_screen_name` + `get_user_by_id`) but count as one. The budget is a coarse safety cap, not an exact request meter.
- **No fingerprint hardening.** The client uses twikit's default User-Agent unless you set `USER_AGENT`. If you exported cookies from a browser, setting `USER_AGENT` to match that browser is the most consistent choice.
- **`get_user` / `get_user_id` depend on a flaky endpoint.** They call `/1.1/account/settings.json`, which x.com currently returns `404` for intermittently. This is an x.com/twikit issue, not a rate-limit one — retry, or use `get_user_by_screen_name` / `get_bookmarks` which don't hit that endpoint.
- **Concurrent writers race.** The state file is best-effort: two servers running against the same file could lose an update. Fine for the normal single-host case.

### twikit patches (`twikit_patch.py`)

`twikit_patch.py` applies three independent monkey-patches at import time (must run before `from twikit import Client`). Remove each once twikit ships a release that fixes the corresponding bug.

**1. `ClientTransaction.get_indices` — `Couldn't get KEY_BYTE indices`**

twikit 2.3.3 raises this because x.com changed its homepage HTML on 2026-03-18 — the `ondemand.s` filename and its hash are now split into two separate `,<N>:"..."` entries instead of one inline `"ondemand.s":"<hash>"`. Upstream issue: [`d60/twikit#408`](https://github.com/d60/twikit/issues/408). Fix is upstream in [`iSarabjitDhiman/XClientTransaction`](https://github.com/iSarabjitDhiman/XClientTransaction) commit `2ff8438`, but twikit hasn't pulled it in. Patched using the regex from [`@audioeng89`'s comment](https://github.com/d60/twikit/issues/408#issuecomment-4089055868).

**2. `User.__init__` — `KeyError` on optional `legacy.*` fields**

twikit 2.3.3's `User.__init__` hard-indexes many optional `legacy.*` fields that x.com omits for some accounts, so any call that parses a User object crashes. Observed in the wild:

- `legacy['entities']['description']['urls']` for accounts with **no link in their bio** (x.com sends `entities == {"description": {}}`) → `KeyError: 'urls'`
- `legacy['withheld_in_countries']` → `KeyError: 'withheld_in_countries'`

This hit `get_user`, `get_user_id`, `get_bookmarks`, timelines, follower lists — anything returning users. Same class of bug as [`d60/twikit#341`](https://github.com/d60/twikit/pull/341) (`can_media_tag`). Rather than whack-a-mole each field, the patch wraps the incoming `data` in a recursive lenient dict so a missing key at any depth degrades to an empty value instead of raising; present keys keep their real values, so normal accounts parse unchanged.

**3. `tweet_detail` cursors — `KeyError: 'itemContent'`**

twikit 2.3.3 reads reply/next-page cursors from the tweet-detail response at `entries[-1]['content']['itemContent']['value']` and `reply['item']['itemContent']['value']` (in `Client.get_tweet_by_id` and `Client._get_more_replies`). x.com **flattened** cursor entries — the value now sits directly on the cursor object (`{'entryType': 'TimelineTimelineCursor', 'cursorType': ..., 'value': ...}`) with no `itemContent` nesting → `KeyError: 'itemContent'`. This broke `get_tweet_by_id`, `get_tweet_details`, and `get_conversation_thread`. All the crash sites parse the response from `GQLClient.tweet_detail`, so the patch wraps that one method and re-nests each flattened cursor's value back under `itemContent`, leaving the original code unchanged.

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
| `USER_AGENT` | No | twikit default | Custom User-Agent. Recommended: match the browser you exported cookies from. |
| `CAPSOLVER_API_KEY` | No | — | Capsolver API key (only needed if you hit Arkose challenges) |
| `X_MCP_STATE_PATH` | No | `~/.x-mcp/throttle_state.json` | Where the throttle layer persists pacing / cooldowns / request log |
| `X_MCP_MAX_BACKOFF` | No | `60` | Max seconds a single call will block waiting out a cooldown before it refuses with a retry hint (the cooldown is still persisted) |
| `X_MCP_MAX_CALLS_PER_WINDOW` | No | `250` | Client-side request budget per 15-min window. `0` disables the cap |

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

### `Account locked` (Arkose challenge) / `Account suspended`

The account got flagged by Twitter's anti-automation. Re-cookieing will **not** help. Stop all automated calls, log into x.com in a browser, complete the challenge (locked) or appeal (suspended), then — only for a lock — re-run `get_cookie` to refresh cookies.

### `Client-side request budget exceeded`

You've hit the built-in safety cap (`X_MCP_MAX_CALLS_PER_WINDOW`, default 250 calls / 15 min). Wait the stated number of seconds, or raise/disable the cap via the env var. This is a *client-side* guard, not an X rate limit.

### `... is rate-limited; cooldown ends in Ns`

X returned a 429 and the reset is further out than `X_MCP_MAX_BACKOFF` (default 60s), so the call refused instead of blocking. The cooldown is persisted (`X_MCP_STATE_PATH`) and honored automatically — just retry after the stated time. Raise `X_MCP_MAX_BACKOFF` if you'd rather have calls block and auto-retry.

### `KeyError: 'urls'` / `KeyError` while reading users or bookmarks

Fixed by the `User.__init__` patch in `twikit_patch.py` (see [twikit patches](#twikit-patches-twikit_patchpy)). If you still see it, the patch isn't loaded — confirm `twitter.py` imports `twikit_patch` before `import twikit`.

### `get_user` / `get_user_id` return `NotFound (404 "page does not exist")`

These go through `/1.1/account/settings.json`, which x.com returns 404 for intermittently. It's not a rate-limit or cookie problem — retry, or use `get_user_by_screen_name` / `get_bookmarks`, which don't hit that endpoint.

---

## License

MIT, inherited from the upstream project.
