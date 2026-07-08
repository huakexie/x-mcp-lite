# x-mcp-lite

Safety-focused lite fork of [`lord-dubious/x-mcp`](https://github.com/lord-dubious/x-mcp).

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

   Optional env vars:
   - `USER_AGENT` — custom user agent
   - `CAPSOLVER_API_KEY` — for CAPTCHA solving (only needed if you hit Arkose challenges)

3. The MCP server starts automatically when your MCP host launches.

## Cookie handling

On first run, the server logs in with username/email/password and saves cookies to `~/.x-mcp/cookies.json`. Subsequent runs load cookies directly. If cookies expire (e.g., Twitter requires re-verification), the login will fail with an error — delete the cookie file and re-run to re-login. **Do not** retry login automatically; repeated login attempts raise account-lock risk.

## License

MIT, inherited from the upstream project.
