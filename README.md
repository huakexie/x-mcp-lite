# x-mcp-lite

Safety-focused lite fork of [`lord-dubious/x-mcp`](https://github.com/lord-dubious/x-mcp).

> **Note on AI involvement**: This fork was authored by Claude Code (Anthropic) under human direction. The human collaborator (`huakexie`) made all product decisions — scope of cuts, naming, design tradeoffs (in-memory vs SQLite, active cooldown vs passive sleep, retry vs no-retry on 429, etc.) — and reviewed/verified the code at each step. Claude Code did the source reading, pattern analysis, and mechanical refactoring (commenting decorators, wrapping calls with `with_rate_limit`, writing the `throttle.py` module). The anti-rate-limit design is informed by reading [`DataWhisker/x-mcp-server`](https://github.com/DataWhisker/x-mcp-server)'s official-API rate-limit module and adapting its "learn from real 429 + active intercept" pattern to twikit's reverse-engineered endpoints.

Keeps the read-only tools + bookmark/like management, **cuts** the high-risk write tools (post/delete tweets, DM, follow/block/mute, groups, cookie ops), and adds an **anti-rate-limit layer** the original project lacks entirely.

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

## What's kept vs cut

### Kept (37 tools)

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

### Cut (39 tools, decorators commented — function bodies retained for diff clarity)

Post/delete tweets, polls, scheduled tweets, retweets, all DM write operations, all group operations, follow/unfollow/block/unblock/mute/unmute, `set_delegate_account`, `update_user`, all cookie management, `delete_all_bookmarks`, geo (`reverse_geocode`/`search_geo`/`get_place`), media metadata, bookmark folder create/edit, `vote`/`vote_on_poll`.

## Anti-rate-limit layer

Three pieces, all in `src/x_mcp/throttle.py`:

1. **`Throttler`** — random-pacing throttler (memory). Every tool call awaits `throttler.wait()` first, which sleeps to enforce a 2–5s random interval since the last call. Randomization avoids fixed-pattern detection.

2. **`with_rate_limit(endpoint, fn)`** — DataWhisker-style active cooldown tracking (in-memory dict).
   - Before calling: if a previous 429 recorded a reset time for this endpoint, sleep until that time (active intercept — no wasted request).
   - On `TooManyRequests`: read `e.rate_limit_reset` (from the `x-rate-limit-reset` response header, confirmed in twikit 2.3.3 `errors.py`), record it, sleep, retry once.
   - On `AccountLocked` / `AccountSuspended`: convert to `RuntimeError`, **do not retry** — these need human intervention.

3. **`paginate_all`** — for `get_all_bookmarks`. Iterates `Result.next()` with 3–8s random delay between pages, auto-backoff on 429.

**No persistence**: in-memory dicts only. Twitter's rate-limit window is 15 min; for low-frequency usage, restart intervals exceed the window anyway. Memory is also faster (no disk I/O).

## Setup

1. Clone:
   ```bash
   git clone https://github.com/huake/x-mcp-lite
   cd x-mcp-lite
   ```

2. Configure in your MCP host (e.g., Claude):
   ```json
   {
       "x-mcp-lite": {
           "command": "uvx",
           "args": ["--from", "git+https://github.com/huake/x-mcp-lite", "x-mcp-lite"],
           "env": {
               "TWITTER_USERNAME": "@your_username",
               "TWITTER_EMAIL": "your_email@example.com",
               "TWITTER_PASSWORD": "your_password"
           }
       }
   }
   ```

   Optional env vars (see `.env.example` for full list):
   - `X_MCP_COOKIES_PATH` — path to cookies file (defaults to `~/.x-mcp/cookies.json`)
   - `X_MCP_PROXY` — residential proxy URL for auto-login only (see Cookie setup below)
   - `X_MCP_COOKIES_PATH` — path to cookies file (defaults to `~/.x-mcp/cookies.json`)
   - `X_MCP_PROXY` — residential proxy URL fallback for `get_cookie(proxy=...)`. Optional; if not set, agents can pass `proxy` at call time, or direct connection is used.
   - `USER_AGENT` — custom user agent
   - `CAPSOLVER_API_KEY` — for CAPTCHA solving (only needed if you hit Arkose challenges)

3. The MCP server starts automatically when your MCP host launches.

## Cookie setup

Twitter's Cloudflare blocks login requests from datacenter IPs with HTTP 403. **Credentials (`TWITTER_USERNAME` / `TWITTER_PASSWORD`) must be set in the MCP server env at startup** — agents can't pass them at runtime. The proxy, however, can be passed at runtime via the `proxy` argument.

Use the `get_cookie` MCP tool. It picks a strategy based on what you pass:

### Strategy 1: Paste cookie JSON (no login, no proxy, no credentials needed)

Best when you already have cookies exported from a browser or received from another machine.

1. Use a browser extension (e.g. EditThisCookie) on x.com while logged in, export as JSON — OR — read the cookies file saved by another x-mcp-lite instance
2. Call `get_cookie(cookie_json="<the JSON string>")`

Writes directly to `X_MCP_COOKIES_PATH`. Validates JSON is a non-empty object; atomic write (tmp + rename) won't corrupt an existing file.

### Strategy 2: Copy a local cookie file (no login, no proxy, no credentials needed)

Best when cookies are already saved somewhere on this machine.

1. Call `get_cookie(cookie_file="/path/to/cookies.json")`

Reads, validates, and atomically writes to `X_MCP_COOKIES_PATH`. Refuses if source equals target.

### Strategy 3: Auto-login (needs credentials + optional proxy)

Best when you don't have cookies yet and want to log into Twitter fresh.

1. Ensure `TWITTER_USERNAME` / `TWITTER_PASSWORD` are set in MCP server env (required — agents can't pass these at runtime)
2. Either:
   - Call `get_cookie(proxy="<residential HTTP proxy URL>")` — for HTTP/SOCKS5 proxies (e.g. linktube residential)
   - Call `set_proxy(outbound="<sing-box outbound JSON>")` first, then `get_cookie()` with no args — for non-HTTP protocols (trojan/anytls/ss/etc). sing-box is auto-downloaded, started as a local HTTP proxy, and auto-cleaned after `get_cookie` succeeds.

twikit logs in via the chosen proxy (or direct), saves cookies to `X_MCP_COOKIES_PATH`. On residential IPs, direct works. On datacenter IPs, you need a residential proxy or you'll get 403.

#### `set_proxy` for non-HTTP protocols

If you only have trojan/anytls/ss nodes (no HTTP proxy URL), use `set_proxy`:

```python
# Agent passes a sing-box outbound JSON
await set_proxy(outbound='{"type":"trojan","server":"...","server_port":443,"password":"...","tls":{"enabled":true,"server_name":"..."}}')

# Then call get_cookie() — it picks up the local proxy automatically
await get_cookie()
```

sing-box binary is downloaded to `~/.x-mcp/singbox-bin/` on first use (tries github.com first, then mirrors, then a user-installed `sing-box` in PATH). After `get_cookie` succeeds, sing-box is stopped and the downloaded binary deleted. Pass `None` to `set_proxy` to stop manually.

### Agent self-service flow

When an agent calls any tool (e.g. `get_bookmarks`) and cookies are missing/expired, the error message tells the agent to call `get_cookie`. The agent then either:
- Asks the user for an HTTP/SOCKS5 proxy URL and calls `get_cookie(proxy="<url>")`
- Asks the user for a non-HTTP node config and calls `set_proxy(outbound="<sing-box JSON>")` then `get_cookie()`
- Asks the user to paste cookies from elsewhere (browser export or another machine) and calls `get_cookie(cookie_json="<JSON>")`

If `TWITTER_USERNAME` / `TWITTER_PASSWORD` are missing, the error tells the user to restart the MCP server with these env vars configured.

### After cookies are saved

Cookies live at `X_MCP_COOKIES_PATH` (default `~/.x-mcp/cookies.json`). To deploy to another machine:
1. Copy the cookies file to that machine
2. Set `X_MCP_COOKIES_PATH` to its absolute path there
3. Don't set `X_MCP_PROXY` there — cookies are reused without proxy

If cookies later expire (Twitter requires re-verification), API calls fail with `AccountLocked` or `Unauthorized`. Re-run `get_cookie` to refresh.

## License

MIT, inherited from the upstream project.
