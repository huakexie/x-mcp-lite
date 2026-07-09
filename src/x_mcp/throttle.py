"""Anti-rate-limit layer for x-mcp-lite.

Three pieces:
  - Throttler: random-pacing throttler (memory) to spread out calls.
  - with_rate_limit: DataWhisker-style active cooldown tracking + retry on 429,
    with explicit handling of AccountLocked/AccountSuspended (no retry).
  - get_all_bookmarks helper: paginated read with inter-page delay.

No persistence: in-memory dicts only. Twitter's rate-limit window is 15 min,
low-frequency usage means restart intervals exceed the window anyway.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar

from twikit.errors import (
    AccountLocked,
    AccountSuspended,
    Forbidden,
    TooManyRequests,
    Unauthorized,
)

logger = logging.getLogger(__name__)

_throttler: Optional["Throttler"] = None
_rate_limit_resets: dict[str, float] = {}

T = TypeVar("T")

# Cookie setup guidance shown when auth fails. Shared between with_rate_limit
# (which raises RuntimeError carrying this) and get_cookie (which returns it).
COOKIE_GUIDE = """\
To set up cookies, choose one of these two paths:

PATH A: Get cookies on a residential-IP machine (e.g. your laptop), then deploy
  1. On a machine with residential IP (optionally behind a VPN/proxy):
     - Set X_MCP_PROXY to a residential proxy URL (http:// or socks5://)
       if you have one; otherwise direct connection works if the machine
       is already on a residential IP.
     - Set TWITTER_USERNAME / TWITTER_EMAIL / TWITTER_PASSWORD
     - Call get_cookie() with no args
  2. Copy the saved file (default ~/.x-mcp/cookies.json) to this machine
  3. Set X_MCP_COOKIES_PATH to its absolute path here
  No proxy needed on this machine after that.

PATH B: Provide a proxy here, let this machine auto-login
  1. Set X_MCP_PROXY to a residential proxy URL
     (e.g. http://user:pass@residential-proxy:8080 or socks5://...)
  2. Set TWITTER_USERNAME / TWITTER_EMAIL / TWITTER_PASSWORD
  3. Call get_cookie() with no args
  Cookies will be saved to X_MCP_COOKIES_PATH here.
"""


def get_throttler(min_interval: float = 2.0, max_interval: float = 5.0) -> "Throttler":
    global _throttler
    if _throttler is None:
        _throttler = Throttler(min_interval=min_interval, max_interval=max_interval)
    return _throttler


class Throttler:
    """Random-pacing throttler. Serializes calls to spread them out."""

    def __init__(self, min_interval: float = 2.0, max_interval: float = 5.0):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            elapsed = time.time() - self._last_call
            target = random.uniform(self.min_interval, self.max_interval)
            if elapsed < target:
                await asyncio.sleep(target - elapsed)
            self._last_call = time.time()


async def with_rate_limit(
    endpoint: str,
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 1,
) -> T:
    """Wrap a twikit call with active cooldown tracking + 429 retry.

    - Before calling: if we previously recorded a cooldown for this endpoint,
      sleep until that reset time (active intercept, no wasted request).
    - On TooManyRequests: record the real reset timestamp from the exception
      (fallback 900s), sleep, then retry once.
    - On AccountLocked/AccountSuspended: convert to RuntimeError, do not retry.
    """
    now = time.time()
    reset_at = _rate_limit_resets.get(endpoint, 0)
    if now < reset_at:
        wait_s = reset_at - now
        logger.info(f"[throttle] {endpoint} cooling down, sleeping {wait_s:.1f}s")
        await asyncio.sleep(wait_s)

    try:
        return await fn()
    except TooManyRequests as e:
        reset = e.rate_limit_reset or (time.time() + 900)
        _rate_limit_resets[endpoint] = reset
        wait_s = max(reset - time.time(), 10)
        logger.warning(
            f"[throttle] {endpoint} hit 429, recorded reset @ {reset}, sleeping {wait_s:.1f}s"
        )
        if max_retries <= 0:
            raise RuntimeError(
                f"{endpoint} rate-limited; reset at {reset} (in {wait_s:.0f}s)"
            )
        await asyncio.sleep(wait_s)
        return await with_rate_limit(endpoint, fn, max_retries=max_retries - 1)
    except AccountLocked as e:
        raise RuntimeError(
            f"Account locked, requires manual verification (Arkose challenge): {e}\n\n"
            f"Run get_cookie() to refresh cookies after verifying the account.\n\n"
            f"{COOKIE_GUIDE}"
        )
    except AccountSuspended as e:
        raise RuntimeError(f"Account suspended: {e}")
    except Unauthorized as e:
        raise RuntimeError(
            f"Unauthorized (401): {e}\n\n"
            f"Cookies are missing or expired. Run get_cookie() to set up cookies.\n\n"
            f"{COOKIE_GUIDE}"
        )
    except Forbidden as e:
        raise RuntimeError(
            f"Forbidden (403): {e}\n\n"
            f"This usually means cookies are missing/expired OR this machine is on "
            f"a datacenter IP. Run get_cookie() to set up cookies.\n\n"
            f"{COOKIE_GUIDE}"
        )


async def paginate_all(
    first_call: Callable[[], Awaitable[Any]],
    page_delay_range: tuple[float, float] = (3.0, 8.0),
    max_pages: int = 200,
) -> list[Any]:
    """Read all pages of a twikit Result object.

    `first_call` returns the first Result. Each Result has a `.next()` coroutine
    that returns the next page (or an empty result). We iterate until either:
      - result is empty / None
      - max_pages reached
      - unrecoverable error
    """
    all_items: list[Any] = []
    result = await with_rate_limit("get_bookmarks", first_call)
    all_items.extend(result)
    pages = 1

    while result and pages < max_pages:
        await asyncio.sleep(random.uniform(*page_delay_range))
        try:
            result = await with_rate_limit("get_bookmarks", result.next)
            if not result:
                break
            all_items.extend(result)
            pages += 1
        except RuntimeError as e:
            logger.error(f"[paginate] stopping after {pages} pages: {e}")
            break

    return all_items
