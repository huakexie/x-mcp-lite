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

from twikit.errors import AccountLocked, AccountSuspended, TooManyRequests

logger = logging.getLogger(__name__)

_throttler: Optional["Throttler"] = None
_rate_limit_resets: dict[str, float] = {}

T = TypeVar("T")


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
        raise RuntimeError(f"账号被锁定，需要人工验证 (Arkose challenge): {e}")
    except AccountSuspended as e:
        raise RuntimeError(f"账号被封禁: {e}")


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
