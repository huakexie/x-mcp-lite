"""Anti-rate-limit / anti-detection layer for x-mcp-lite.

Pieces:
  - Throttler: random-pacing throttler to spread out calls.
  - with_rate_limit: active cooldown tracking + retry on 429, a client-side
    rolling-window request budget, a bounded backoff, and explicit handling
    of AccountLocked/AccountSuspended (no retry, no misleading get_cookie hint).
  - paginate_all: paginated read with inter-page delay.

State persistence
-----------------
Pacing (`last_call`), per-endpoint 429 cooldowns (`resets`) and the rolling
request log (`calls`) are persisted to a small JSON file (default
`~/.x-mcp/throttle_state.json`, override with `X_MCP_STATE_PATH`). This matters
because MCP hosts commonly spawn the server per session (e.g. mcphub over
stdio): without persistence a cooldown recorded in one session would be lost in
the next, and the first call of a fresh session would immediately re-hit a 429
that was already being waited out. Reads/writes are best-effort and tolerant of
a missing/corrupt file; writes are atomic (tmp + rename).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar

from twikit.errors import (
    AccountLocked,
    AccountSuspended,
    TooManyRequests,
    TwitterException,
)

logger = logging.getLogger(__name__)

_throttler: Optional["Throttler"] = None

T = TypeVar("T")

# Cookie setup guidance shown when auth fails. Kept intentionally minimal:
# just tell the agent to call get_cookie; the full path selection happens
# inside that tool (in its docstring + no-arg return value).
COOKIE_GUIDE = "Call get_cookie() to set up cookies. See that tool's description for options."

# Twitter's rate-limit window.
WINDOW = 15 * 60  # 15 minutes in seconds

# Persisted state file. Survives per-session server spawns.
STATE_PATH = Path(
    os.getenv("X_MCP_STATE_PATH", str(Path.home() / ".x-mcp" / "throttle_state.json"))
)

# Max seconds we're willing to block *inside a single call* waiting out a
# cooldown. If the real reset is further out, we persist the cooldown and raise
# a clear "try again in Ns" error instead of blocking (which would otherwise
# risk hitting the MCP client's request timeout — up to the full 900s window).
MAX_BACKOFF = float(os.getenv("X_MCP_MAX_BACKOFF", "60"))

# Client-side rolling-window request budget. A hard cap on how many API calls
# we make per WINDOW, independent of what X allows, to avoid tripping
# anti-automation during a burst. 0 disables the cap.
MAX_CALLS_PER_WINDOW = int(os.getenv("X_MCP_MAX_CALLS_PER_WINDOW", "250"))


def _load_state() -> dict:
    """Load persisted throttle state; tolerant of missing/corrupt file."""
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state is not a dict")
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
    resets = state.get("resets")
    calls = state.get("calls")
    state["resets"] = resets if isinstance(resets, dict) else {}
    state["calls"] = calls if isinstance(calls, list) else []
    try:
        state["last_call"] = float(state.get("last_call", 0.0))
    except (TypeError, ValueError):
        state["last_call"] = 0.0
    return state


def _save_state(state: dict) -> None:
    """Atomically persist throttle state; best-effort (never raises)."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, str(STATE_PATH))
    except OSError as e:
        logger.debug(f"[throttle] could not persist state: {e}")


def get_throttler(min_interval: float = 2.0, max_interval: float = 5.0) -> "Throttler":
    global _throttler
    if _throttler is None:
        _throttler = Throttler(min_interval=min_interval, max_interval=max_interval)
    return _throttler


class Throttler:
    """Random-pacing throttler. Spreads calls out using a persisted last-call
    timestamp so pacing holds across per-session server spawns too."""

    def __init__(self, min_interval: float = 2.0, max_interval: float = 5.0):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            state = _load_state()
            elapsed = time.time() - state["last_call"]
            target = random.uniform(self.min_interval, self.max_interval)
            if 0 < elapsed < target:
                await asyncio.sleep(target - elapsed)
            state["last_call"] = time.time()
            _save_state(state)


async def with_rate_limit(
    endpoint: str,
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 1,
) -> T:
    """Wrap a twikit call with budget + active cooldown + bounded 429 retry.

    Order of checks:
    1. Rolling budget: if we've already made MAX_CALLS_PER_WINDOW calls in the
       last WINDOW, refuse (don't add fuel to a burst that may trip detection).
    2. Active cooldown: if a previous 429 recorded a reset for this endpoint,
       either sleep it out (<= MAX_BACKOFF) or refuse with a clear retry hint.
    3. Call fn. On 429, record the real reset (persisted) and retry once if the
       wait is within MAX_BACKOFF; otherwise refuse with a retry hint.
    4. AccountLocked/AccountSuspended -> RuntimeError telling the user to fix it
       manually in a browser (re-cookieing won't help). Other TwitterException
       -> RuntimeError with the get_cookie hint.
    """
    now = time.time()
    state = _load_state()

    # 1. Rolling-window request budget.
    if MAX_CALLS_PER_WINDOW > 0:
        window_start = now - WINDOW
        recent = [t for t in state["calls"] if t > window_start]
        if len(recent) >= MAX_CALLS_PER_WINDOW:
            retry_in = min(recent) + WINDOW - now
            raise RuntimeError(
                f"[ERROR] Client-side request budget exceeded "
                f"({MAX_CALLS_PER_WINDOW} calls / {int(WINDOW / 60)} min). "
                f"Try again in {retry_in:.0f}s. This is a safety cap to avoid "
                f"tripping X's anti-automation; raise X_MCP_MAX_CALLS_PER_WINDOW "
                f"to change it."
            )
        state["calls"] = recent

    # 2. Active cooldown from a previously recorded 429.
    reset_at = state["resets"].get(endpoint, 0)
    if now < reset_at:
        wait_s = reset_at - now
        if wait_s > MAX_BACKOFF:
            raise RuntimeError(
                f"[ERROR] {endpoint} is rate-limited; cooldown ends in "
                f"{wait_s:.0f}s (persisted across restarts). Try again later."
            )
        logger.info(f"[throttle] {endpoint} cooling down, sleeping {wait_s:.1f}s")
        await asyncio.sleep(wait_s)

    # Record this attempt against the budget before firing it.
    state["calls"].append(time.time())
    _save_state(state)

    try:
        return await fn()
    except TooManyRequests as e:
        reset = e.rate_limit_reset or (time.time() + WINDOW)
        state = _load_state()
        state["resets"][endpoint] = reset
        _save_state(state)
        wait_s = max(reset - time.time(), 10)
        if wait_s > MAX_BACKOFF or max_retries <= 0:
            raise RuntimeError(
                f"[ERROR] {endpoint} rate-limited (429); cooldown ends in "
                f"{wait_s:.0f}s. Cooldown recorded and will be honored on the "
                f"next call. Try again later."
            )
        logger.warning(
            f"[throttle] {endpoint} hit 429, reset @ {reset}, sleeping {wait_s:.1f}s"
        )
        await asyncio.sleep(wait_s)
        return await with_rate_limit(endpoint, fn, max_retries=max_retries - 1)
    except (AccountLocked, AccountSuspended) as e:
        raise RuntimeError(
            f"[ERROR] {type(e).__name__}: {e}. Your X account is locked or "
            f"suspended — re-cookieing will NOT help. Log into x.com in a "
            f"browser, resolve the challenge/appeal, then retry. Stop automated "
            f"calls until it's resolved."
        )
    except TwitterException as e:
        # Any other twikit Twitter API error: 400/401/403/404/etc.
        raise RuntimeError(f"[ERROR] {type(e).__name__}: {e}. " + COOKIE_GUIDE)


async def paginate_all(
    first_call: Callable[[], Awaitable[Any]],
    page_delay_range: tuple[float, float] = (3.0, 8.0),
    max_pages: int = 50,
) -> list[Any]:
    """Read all pages of a twikit Result object.

    `first_call` returns the first Result. Each Result has a `.next()` coroutine
    that returns the next page (or an empty result). We iterate until either:
      - result is empty / None
      - max_pages reached
      - unrecoverable error (budget/cooldown/429/auth) -> stop, return so far
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
