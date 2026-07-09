from typing import Any, Dict, List, Optional, Union
import atexit
import time
import json
import logging
from fastmcp import FastMCP
from fastmcp.server import Context
from . import twikit_patch  # noqa: F401  must run before `import twikit`
import twikit
import os
from pathlib import Path
import logging
import time
import json

from .throttle import get_throttler, with_rate_limit, paginate_all, COOKIE_GUIDE
from . import singbox

# Create an MCP server with proper metadata
mcp = FastMCP()
throttler = get_throttler()

# Clean up any running singbox process when the MCP server exits.
atexit.register(singbox.stop_singbox)

logger = logging.getLogger(__name__)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)

USERNAME = os.getenv("TWITTER_USERNAME")
EMAIL = os.getenv("TWITTER_EMAIL")
PASSWORD = os.getenv("TWITTER_PASSWORD")
USER_AGENT = os.getenv("USER_AGENT")
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY")
COOKIES_PATH = Path(
    os.getenv("X_MCP_COOKIES_PATH", str(Path.home() / ".x-mcp" / "cookies.json"))
)
PROXY_URL = os.getenv("X_MCP_PROXY")  # None = direct connection

# Rate limit tracking
RATE_LIMITS: Dict[str, List[float]] = {}
RATE_LIMIT_WINDOW = 15 * 60  # 15 minutes in seconds


def convert_tweets_to_markdown(tweets: Any) -> str:
    """Convert tweet objects to markdown format."""
    try:
        tweet_list = []
        for tweet in tweets:
            tweet_text = tweet.text.replace("\n", "\n> ")
            tweet_list.append(f"Tweet ID: {tweet.id}\n> {tweet_text}\n")
        return "\n".join(tweet_list)
    except Exception as e:
        logger.error(f"Failed to convert tweets to markdown: {e}")
        return str(tweets)


async def get_twitter_client(proxy: Optional[str] = None) -> twikit.Client:
    """Initialize and return an authenticated Twitter client.

    Behavior:
    - If X_MCP_COOKIES_PATH exists: load cookies from it (no login, no proxy needed).
    - Otherwise: login with TWITTER_USERNAME/PASSWORD, optionally via a proxy.
      EMAIL is optional (X accounts don't always have one); omitted if not set.

    Args:
      proxy: optional proxy URL for this call. Overrides X_MCP_PROXY env var.
              Used by get_cookie() to let agents pass a proxy at runtime.

    Login failures are wrapped with a cookie-setup guide so that agents
    seeing 403/401/AccountLocked can self-serve via get_cookie(proxy=...).
    """
    from twikit.errors import (
        AccountLocked,
        AccountSuspended,
        Forbidden,
        Unauthorized,
    )

    effective_proxy = proxy if proxy is not None else PROXY_URL
    captcha_solver = None
    if CAPSOLVER_API_KEY:
        captcha_solver = twikit._captcha.capsolver.Capsolver(api_key=CAPSOLVER_API_KEY)
    client = twikit.Client(
        "en-US",
        user_agent=USER_AGENT,
        captcha_solver=captcha_solver,
        proxy=effective_proxy,
    )

    if COOKIES_PATH.exists():
        client.load_cookies(str(COOKIES_PATH))
    else:
        if not USERNAME or not PASSWORD:
            raise RuntimeError(
                "[ERROR] Missing TWITTER_USERNAME/TWITTER_PASSWORD in MCP server env. "
                "The user must restart the MCP server with these env vars configured."
            )
        try:
            login_kwargs: dict = {"auth_info_1": USERNAME, "password": PASSWORD}
            if EMAIL:
                login_kwargs["auth_info_2"] = EMAIL
            await client.login(**login_kwargs)
        except (AccountLocked, AccountSuspended, Forbidden, Unauthorized) as e:
            # Auth-related failures: tell the agent to call get_cookie.
            logger.error(f"Login failed ({type(e).__name__}): {e}")
            raise RuntimeError(
                f"[ERROR] Login failed ({type(e).__name__}): {e}. "
                + COOKIE_GUIDE
            )
        except Exception as e:
            # Unknown failure: don't wrap, but prefix so it's clearly an error.
            logger.error(f"Failed to login: {e}")
            raise RuntimeError(f"[ERROR] Login failed: {type(e).__name__}: {e}")
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        client.save_cookies(str(COOKIES_PATH))

    return client


def check_rate_limit(endpoint: str) -> bool:
    """Check if we're within rate limits for a given endpoint."""
    now = time.time()
    if endpoint not in RATE_LIMITS:
        RATE_LIMITS[endpoint] = []

    # Remove old timestamps
    RATE_LIMITS[endpoint] = [
        t for t in RATE_LIMITS[endpoint] if now - t < RATE_LIMIT_WINDOW
    ]

    # Check limits based on endpoint
    limits = {
        "tweet": 300,  # 300 tweets per 15 minutes
        "dm": 1000,  # 1000 DMs per 15 minutes
        "follow": 400,  # 400 follows per 24 hours
        "like": 1000,  # 1000 likes per 24 hours
    }

    limit = limits.get(endpoint, 100)  # Default to 100 for unknown endpoints
    return len(RATE_LIMITS[endpoint]) < limit


@mcp.tool()
async def get_cookie(
    cookie_json: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
) -> str:
    """Set up Twitter cookies for this MCP server. Pick one strategy by passing the corresponding argument.

    STRATEGY 1 (recommended): proxy="<residential proxy URL>"
      Performs an automated twikit login routed through a residential proxy.
      Does NOT involve handling user credentials — TWITTER_USERNAME and
      TWITTER_PASSWORD stay inside the MCP server env; you only pass a
      network proxy address (e.g. http://user:pass@host:port or socks5://host:port).
      Use this when the server is on a datacenter IP and login is blocked
      by Cloudflare. Falls back to X_MCP_PROXY env var if `proxy` is None,
      then to direct connection.

    STRATEGY 2 (only if user can't provide a proxy): cookie_json="<JSON string>"
      Writes the given JSON directly to the cookies path. Use this when the
      user has exported cookies from a browser (e.g. EditThisCookie on x.com)
      or has run get_cookie() on another machine and pasted the resulting
      JSON here. NOTE: this transfers login tokens — prefer STRATEGY 1 when
      possible.

    STRATEGY 3: cookie_file="<local file path>"
      Copies a local file to the cookies path. Same caveat as STRATEGY 2.

    If neither argument is passed: returns a short prompt asking for a
    proxy URL (so the agent knows what to do next).
    """
    import tempfile

    def _write_json_atomic(parsed: Any) -> Optional[str]:
        """Write parsed JSON to COOKIES_PATH atomically. Returns error str or None."""
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(COOKIES_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(parsed, f, indent=2)
            os.replace(tmp, str(COOKIES_PATH))
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return f"[ERROR] Failed to write cookies: {e}"
        return None

    def _validate_cookie_dict(parsed: Any) -> Optional[str]:
        """Validate parsed JSON is a non-empty dict. Returns error str or None."""
        if not isinstance(parsed, dict):
            return f"[ERROR] Cookies JSON must be a JSON object (dict), got {type(parsed).__name__}."
        if not parsed:
            return "[ERROR] Cookies JSON is empty. Expected an object with cookie keys."
        return None

    # Strategy 1: paste JSON
    if cookie_json is not None:
        try:
            parsed = json.loads(cookie_json)
        except json.JSONDecodeError as e:
            return f"[ERROR] Invalid JSON: {e}. Cookies file left untouched."
        if err := _validate_cookie_dict(parsed):
            return err
        if err := _write_json_atomic(parsed):
            return err
        return (
            f"Cookies written to {COOKIES_PATH} from pasted JSON. "
            f"On a deployment machine, copy this file there and set X_MCP_COOKIES_PATH to its path."
        )

    # Strategy 2: copy local file
    if cookie_file is not None:
        src = Path(cookie_file)
        if not src.is_file():
            return f"[ERROR] Source file not found (or not a regular file): {cookie_file}"
        try:
            src_resolved = src.resolve()
        except OSError:
            src_resolved = src
        try:
            target_resolved = COOKIES_PATH.resolve()
        except OSError:
            target_resolved = COOKIES_PATH
        if src_resolved == target_resolved:
            return (
                f"[ERROR] Source path is the same as the target cookies path "
                f"({COOKIES_PATH}). Nothing to copy."
            )
        try:
            parsed = json.loads(src.read_text())
        except json.JSONDecodeError as e:
            return f"[ERROR] Source file is not valid JSON: {e}"
        if err := _validate_cookie_dict(parsed):
            return err
        if err := _write_json_atomic(parsed):
            return err
        return (
            f"Cookies copied from {cookie_file} to {COOKIES_PATH}. "
            f"On a deployment machine, copy {COOKIES_PATH} there and set X_MCP_COOKIES_PATH accordingly."
        )

    # Strategy 3: auto-login (no cookie_json / cookie_file)
    if not USERNAME or not PASSWORD:
        return (
            "[ERROR] Missing TWITTER_USERNAME / TWITTER_PASSWORD in MCP server env. "
            "The user must restart the MCP server with these env vars configured."
        )
    # Priority: explicit proxy arg > singbox active proxy (from set_proxy) >
    # X_MCP_PROXY env var > direct connection.
    active = singbox.get_active_proxy()
    if proxy is not None:
        effective_proxy = proxy
    elif active is not None:
        effective_proxy = active
    elif PROXY_URL:
        effective_proxy = PROXY_URL
    else:
        return (
            "Provide a residential proxy URL to route the login through. "
            "Format: http://user:pass@host:port or socks5://host:port.\n"
            "Pass it as the `proxy` argument: get_cookie(proxy=\"<the URL>\").\n"
            "Or use set_proxy(outbound=\"<sing-box outbound JSON>\") to start a "
            "local HTTP proxy from a non-HTTP protocol (trojan/anytls/ss/etc).\n"
            "This is a network proxy address, not an X account credential — "
            "TWITTER_USERNAME and TWITTER_PASSWORD stay inside the MCP server env."
        )
    try:
        client = await get_twitter_client(proxy=effective_proxy)
    except Exception as e:
        return f"[ERROR] Auto-login failed: {e}"
    # Auto-cleanup singbox if it was started by set_proxy (one-shot pattern).
    if singbox.get_active_proxy() is not None:
        singbox.stop_singbox()
    return (
        f"Cookies saved to {COOKIES_PATH} via auto-login"
        f"{' via proxy ' + effective_proxy if effective_proxy else ' (direct)'}.\n"
        f"On a deployment machine, copy this file there and set X_MCP_COOKIES_PATH to its path. "
        f"No proxy needed on the deployment side."
    )


@mcp.tool()
async def set_proxy(
    outbound: Optional[str] = None,
) -> str:
    """Configure the proxy used by get_cookie() for the next auto-login.

    Pass an `outbound` JSON string (a sing-box outbound config, e.g. trojan/
    anytls/ss). This will:
      1. Download the sing-box binary to ~/.x-mcp/singbox-bin/ if not already
         installed (tries github.com first, then mirrors; falls back to a
         user-installed `sing-box` in PATH).
      2. Start sing-box as a local HTTP inbound (127.0.0.1:<random port>),
         routing through the given outbound.
      3. Set the local HTTP URL as the active proxy for get_cookie().

    Pass None (or call with no args) to stop sing-box, delete the downloaded
    binary, and clear the active proxy.

    Typical agent flow:
      1. Call set_proxy(outbound="<sing-box outbound JSON>")
      2. Call get_cookie() — uses the local proxy automatically, logs into
         Twitter, saves cookies, and auto-stops sing-box.
      3. Retry the original call (e.g. get_bookmarks).
    """
    if outbound is None:
        singbox.stop_singbox()
        return "Proxy cleared. sing-box stopped, binary deleted (if managed)."
    try:
        proxy_url = singbox.start_singbox(outbound)
    except Exception as e:
        # Make sure no half-state remains.
        singbox.stop_singbox()
        return f"[ERROR] {e}"
    return (
        f"Local HTTP proxy started at {proxy_url}. "
        f"Call get_cookie() now to login via this proxy; it will auto-stop sing-box on success."
    )


@mcp.tool()
async def search_user(
    query: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Searches for users based on the provided query."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        users = await with_rate_limit(
            "search_user", lambda: client.search_user(query, count=count, cursor=cursor)
        )
        return str(users)  # Assuming you want to return the raw result for now
    except Exception as e:
        logger.error(f"Failed to search users: {e}")
        return f"[ERROR] Failed to search users: {e}"


@mcp.tool(
    name="search_twitter",
    description="Search twitter with a query. Sort by 'Top' or 'Latest'"
)
async def search_twitter(ctx: Context, query: str, product: str = "Top", count: int = 20, cursor: Optional[str] = None) -> str:
    """Search twitter with a query. Sort by 'Top' or 'Latest'"""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "search_twitter", lambda: client.search_tweet(query, product=product, count=count, cursor=cursor)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to search tweets: {e}")
        return f"[ERROR] Failed to search tweets: {e}"


@mcp.tool(
    name="get_user_tweets",
    description="Get tweets from a specific user's timeline. Takes user_id, tweet_type (default: Tweets), count (default: 40), and cursor (optional)"
)
async def get_user_tweets(
    ctx: Context,
    user_id: str,
    tweet_type: str = "Tweets",
    count: int = 40,
    cursor: Optional[str] = None,
) -> str:
    """Get tweets from a specific user's timeline."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "get_user_tweets", lambda: client.get_user_tweets(user_id=user_id, tweet_type=tweet_type, count=count, cursor=cursor)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get user tweets: {e}")
        return f"[ERROR] Failed to get user tweets: {e}"


@mcp.tool(
    name="get_timeline",
    description="Get tweets from your home timeline (For You). Takes count (default: 20), seen_tweet_ids (optional), and cursor (optional)"
)
async def get_timeline(
    ctx: Context,
    count: int = 20,
    seen_tweet_ids: Optional[List[str]] = None,
    cursor: Optional[str] = None,
) -> str:
    """Get tweets from your home timeline (For You)."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "get_timeline", lambda: client.get_timeline(count=count, seen_tweet_ids=seen_tweet_ids, cursor=cursor)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get timeline: {e}")
        return f"[ERROR] Failed to get timeline: {e}"


@mcp.tool(
    name="get_latest_timeline",
    description="Get tweets from your home timeline (Following). Takes count (default: 20), seen_tweet_ids (optional), and cursor (optional)"
)
async def get_latest_timeline(
    ctx: Context,
    count: int = 20,
    seen_tweet_ids: Optional[List[str]] = None,
    cursor: Optional[str] = None,
) -> str:
    """Get tweets from your home timeline (Following)."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "get_latest_timeline", lambda: client.get_latest_timeline(count=count, seen_tweet_ids=seen_tweet_ids, cursor=cursor)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get latest timeline: {e}")
        return f"[ERROR] Failed to get latest timeline: {e}"


# @mcp.tool()
async def post_tweet(
    text: str,
    media_paths: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> str:
    """Post a tweet with optional media, reply, and tags."""
    try:
        if not check_rate_limit("tweet"):
            return "Rate limit exceeded for tweets. Please wait before posting again."

        client = await get_twitter_client()

        # Handle tags by converting to mentions
        if tags:
            mentions = " ".join(f"@{tag.lstrip('@')}" for tag in tags)
            text = f"{text}\n{mentions}"

        # Upload media if provided
        media_ids = []
        if media_paths:
            for path in media_paths:
                media_id = await client.upload_media(path, wait_for_completion=True)
                media_ids.append(media_id)

        # Create the tweet
        tweet = await client.create_tweet(
            text=text, media_ids=media_ids if media_ids else None, reply_to=reply_to
        )
        RATE_LIMITS["tweet"].append(time.time())
        return f"Successfully posted tweet: {tweet.id}"
    except Exception as e:
        logger.error(f"Failed to post tweet: {e}")
        return f"[ERROR] Failed to post tweet: {e}"


# @mcp.tool()
async def delete_tweet(tweet_id: str) -> str:
    """Delete a tweet by its ID."""
    try:
        client = await get_twitter_client()
        await client.delete_tweet(tweet_id)
        return f"Successfully deleted tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to delete tweet: {e}")
        return f"[ERROR] Failed to delete tweet: {e}"


# @mcp.tool()
async def send_dm(user_id: str, message: str, media_path: Optional[str] = None) -> str:
    """Send a direct message to a user."""
    try:
        if not check_rate_limit("dm"):
            return "Rate limit exceeded for DMs. Please wait before sending again."

        client = await get_twitter_client()

        media_id = None
        if media_path:
            media_id = await client.upload_media(media_path, wait_for_completion=True)

        await client.send_dm(user_id=user_id, text=message, media_id=media_id)
        RATE_LIMITS["dm"].append(time.time())
        return f"Successfully sent DM to user {user_id}"
    except Exception as e:
        logger.error(f"Failed to send DM: {e}")
        return f"[ERROR] Failed to send DM: {e}"


# @mcp.tool()
async def delete_dm(message_id: str) -> str:
    """Delete a direct message by its ID."""
    try:
        client = await get_twitter_client()
        await client.delete_dm(message_id)
        return f"Successfully deleted DM {message_id}"
    except Exception as e:
        logger.error(f"Failed to delete DM: {e}")
        return f"[ERROR] Failed to delete DM: {e}"


# @mcp.tool()
async def logout() -> str:
    """Logs out of the currently logged-in account."""
    try:
        client = await get_twitter_client()
        await client.logout()
        return "Successfully logged out"
    except Exception as e:
        logger.error(f"Failed to logout: {e}")
        return f"[ERROR] Failed to logout: {e}"


# @mcp.tool()
async def unlock() -> str:
    """Unlocks the account using the provided CAPTCHA solver."""
    try:
        client = await get_twitter_client()
        await client.unlock()
        return "Successfully unlocked account"
    except Exception as e:
        logger.error(f"Failed to unlock account: {e}")
        return f"[ERROR] Failed to unlock account: {e}"


# @mcp.tool()
async def get_cookies() -> str:
    """Get the cookies."""
    try:
        client = await get_twitter_client()
        cookies = client.get_cookies()
        return str(cookies)
    except Exception as e:
        logger.error(f"Failed to get cookies: {e}")
        return f"[ERROR] Failed to get cookies: {e}"


# @mcp.tool()
async def save_cookies(path: str) -> str:
    """Save cookies to file in json format."""
    try:
        client = await get_twitter_client()
        client.save_cookies(path)
        return f"Successfully saved cookies to {path}"
    except Exception as e:
        logger.error(f"Failed to save cookies: {e}")
        return f"[ERROR] Failed to save cookies: {e}"


# @mcp.tool()
async def set_cookies(
    cookies: str, clear_cookies: bool = False
) -> str:
    """Sets cookies."""
    try:
        client = await get_twitter_client()
        import json

        client.set_cookies(json.loads(cookies), clear_cookies)
        return "Successfully set cookies"
    except Exception as e:
        logger.error(f"Failed to set cookies: {e}")
        return f"[ERROR] Failed to set cookies: {e}"


# @mcp.tool()
async def load_cookies(path: str) -> str:
    """Loads cookies from a file."""
    try:
        client = await get_twitter_client()
        client.load_cookies(path)
        return f"Successfully loaded cookies from {path}"
    except Exception as e:
        logger.error(f"Failed to load cookies: {e}")
        return f"[ERROR] Failed to load cookies: {e}"


# @mcp.tool()
async def set_delegate_account(user_id: str) -> str:
    """Sets the account to act as."""
    try:
        client = await get_twitter_client()
        client.set_delegate_account(user_id)
        return f"Successfully set delegate account to {user_id}"
    except Exception as e:
        logger.error(f"Failed to set delegate account: {e}")
        return f"[ERROR] Failed to set delegate account: {e}"


@mcp.tool()
async def get_user_id() -> str:
    """Retrieves the user ID associated with the authenticated account."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        user_id = await with_rate_limit("get_user_id", lambda: client.user_id())
        return user_id
    except Exception as e:
        logger.error(f"Failed to get user ID: {e}")
        return f"[ERROR] Failed to get user ID: {e}"


@mcp.tool()
async def get_user() -> str:
    """Retrieve detailed information about the authenticated user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        user = await with_rate_limit("get_user", lambda: client.user())
        return str(user)
    except Exception as e:
        logger.error(f"Failed to get user: {e}")
        return f"[ERROR] Failed to get user: {e}"


@mcp.tool()
async def get_similar_tweets(tweet_id: str) -> str:
    """Retrieves tweets similar to the specified tweet (Twitter premium only)."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "get_similar_tweets", lambda: client.get_similar_tweets(tweet_id)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get similar tweets: {e}")
        return f"[ERROR] Failed to get similar tweets: {e}"


# @mcp.tool()
async def create_media_metadata(
    media_id: str,
    alt_text: Optional[str] = None,
    sensitive_warning: Optional[List[str]] = None,
) -> str:
    """Adds metadata to uploaded media."""
    try:
        client = await get_twitter_client()
        await client.create_media_metadata(media_id, alt_text, sensitive_warning)
        return f"Successfully created media metadata for {media_id}"
    except Exception as e:
        logger.error(f"Failed to create media metadata: {e}")
        return f"[ERROR] Failed to create media metadata: {e}"


# @mcp.tool(
#     name="create_poll_tweet",
#     description="Create a tweet with a poll. Takes text content, list of choices, and optional duration in minutes."
# )
async def create_poll_tweet(ctx: Context, text: str, choices: List[str], duration_minutes: int = 1440) -> str:
    """Create a tweet with a poll."""
    try:
        client = await get_twitter_client()
        
        # Create the poll
        card_uri = await client.create_poll(choices, duration_minutes)
        
        # Post the tweet with the poll
        tweet = await client.post_tweet(text, card_uri=card_uri)
        
        return f"Successfully created poll tweet: https://twitter.com/i/status/{tweet.id}"
    except Exception as e:
        logger.error(f"Failed to create poll tweet: {e}")
        return f"[ERROR] Failed to create poll tweet: {e}"


# @mcp.tool()
async def vote(
    selected_choice: str,
    card_uri: str,
    tweet_id: str,
    card_name: str,
) -> str:
    """Vote on a poll with the selected choice."""
    try:
        client = await get_twitter_client()
        poll = await client.vote(selected_choice, card_uri, tweet_id, card_name)
        return f"Successfully voted on poll: {poll.id}"
    except Exception as e:
        logger.error(f"Failed to vote on poll: {e}")
        return f"[ERROR] Failed to vote on poll: {e}"


# @mcp.tool()
async def create_scheduled_tweet(
    scheduled_at: int,
    text: str = "",
    media_paths: Optional[List[str]] = None,
) -> str:
    """Schedules a tweet to be posted at a specified timestamp."""
    try:
        client = await get_twitter_client()
        media_ids = []
        if media_paths:
            for path in media_paths:
                media_id = await client.upload_media(path, wait_for_completion=True)
                media_ids.append(media_id)
        tweet_id = await client.create_scheduled_tweet(scheduled_at, text, media_ids)
        return f"Successfully scheduled tweet with ID: {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to schedule tweet: {e}")
        return f"[ERROR] Failed to schedule tweet: {e}"


@mcp.tool()
async def get_user_by_screen_name(screen_name: str) -> str:
    """Fetches a user by screen name."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        user = await with_rate_limit(
            "get_user_by_screen_name", lambda: client.get_user_by_screen_name(screen_name)
        )
        return str(user)
    except Exception as e:
        logger.error(f"Failed to get user by screen name: {e}")
        return f"[ERROR] Failed to get user by screen name: {e}"


@mcp.tool()
async def get_user_by_id(user_id: str) -> str:
    """Fetches a user by ID."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        user = await with_rate_limit(
            "get_user_by_id", lambda: client.get_user_by_id(user_id)
        )
        return str(user)
    except Exception as e:
        logger.error(f"Failed to get user by ID: {e}")
        return f"[ERROR] Failed to get user by ID: {e}"


# @mcp.tool()
async def reverse_geocode(
    lat: float,
    long: float,
    accuracy: Optional[str] = None,
    granularity: Optional[str] = None,
    max_results: Optional[int] = None,
) -> str:
    """Given a latitude and a longitude, searches for up to 20 places."""
    try:
        client = await get_twitter_client()
        places = await client.reverse_geocode(
            lat, long, accuracy, granularity, max_results
        )
        return str(places)
    except Exception as e:
        logger.error(f"Failed to reverse geocode: {e}")
        return f"[ERROR] Failed to reverse geocode: {e}"


# @mcp.tool()
async def search_geo(
    lat: Optional[float] = None,
    long: Optional[float] = None,
    query: Optional[str] = None,
    ip: Optional[str] = None,
    granularity: Optional[str] = None,
    max_results: Optional[int] = None,
) -> str:
    """Search for places that can be attached to a Tweet."""
    try:
        client = await get_twitter_client()
        places = await client.search_geo(lat, long, query, ip, granularity, max_results)
        return str(places)
    except Exception as e:
        logger.error(f"Failed to search geo: {e}")
        return f"[ERROR] Failed to search geo: {e}"


# @mcp.tool()
async def get_place(place_id: str) -> str:
    """Retrieves a place by ID."""
    try:
        client = await get_twitter_client()
        place = await client.get_place(place_id)
        return str(place)
    except Exception as e:
        logger.error(f"Failed to get place: {e}")
        return f"[ERROR] Failed to get place: {e}"


@mcp.tool()
async def get_tweet_by_id(
    tweet_id: str, cursor: Optional[str] = None
) -> str:
    """Fetches a tweet by tweet ID."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweet = await with_rate_limit(
            "get_tweet_by_id", lambda: client.get_tweet_by_id(tweet_id, cursor)
        )
        return str(tweet)
    except Exception as e:
        logger.error(f"Failed to get tweet by ID: {e}")
        return f"[ERROR] Failed to get tweet by ID: {e}"


@mcp.tool()
async def get_scheduled_tweets() -> str:
    """Retrieves scheduled tweets."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "get_scheduled_tweets", lambda: client.get_scheduled_tweets()
        )
        return str(tweets)
    except Exception as e:
        logger.error(f"Failed to get scheduled tweets: {e}")
        return f"[ERROR] Failed to get scheduled tweets: {e}"


# @mcp.tool()
async def delete_scheduled_tweet(tweet_id: str) -> str:
    """Delete a scheduled tweet."""
    try:
        client = await get_twitter_client()
        await client.delete_scheduled_tweet(tweet_id)
        return f"Successfully deleted scheduled tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to delete scheduled tweet: {e}")
        return f"[ERROR] Failed to delete scheduled tweet: {e}"


@mcp.tool()
async def get_retweeters(
    tweet_id: str, count: int = 40, cursor: Optional[str] = None
) -> str:
    """Retrieve users who retweeted a specific tweet."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        retweeters = await with_rate_limit(
            "get_retweeters", lambda: client.get_retweeters(tweet_id, count, cursor)
        )
        return convert_tweets_to_markdown(retweeters)
    except Exception as e:
        logger.error(f"Failed to get retweeters: {e}")
        return f"[ERROR] Failed to get retweeters: {e}"


@mcp.tool()
async def get_favoriters(
    tweet_id: str, count: int = 40, cursor: Optional[str] = None
) -> str:
    """Retrieve users who favorited a specific tweet."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        favoriters = await with_rate_limit(
            "get_favoriters", lambda: client.get_favoriters(tweet_id, count, cursor)
        )
        return convert_tweets_to_markdown(favoriters)
    except Exception as e:
        logger.error(f"Failed to get favoriters: {e}")
        return f"[ERROR] Failed to get favoriters: {e}"


@mcp.tool()
async def get_community_note(note_id: str) -> str:
    """Fetches a community note by ID."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        note = await with_rate_limit(
            "get_community_note", lambda: client.get_community_note(note_id)
        )
        return str(note)
    except Exception as e:
        logger.error(f"Failed to get community note: {e}")
        return f"[ERROR] Failed to get community note: {e}"


@mcp.tool()
async def favorite_tweet(tweet_id: str) -> str:
    """Favorites a tweet."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        await with_rate_limit(
            "favorite_tweet", lambda: client.favorite_tweet(tweet_id)
        )
        return f"Successfully favorited tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to favorite tweet: {e}")
        return f"[ERROR] Failed to favorite tweet: {e}"


@mcp.tool()
async def unfavorite_tweet(tweet_id: str) -> str:
    """Unfavorites a tweet."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        await with_rate_limit(
            "unfavorite_tweet", lambda: client.unfavorite_tweet(tweet_id)
        )
        return f"Successfully unfavorited tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to unfavorite tweet: {e}")
        return f"[ERROR] Failed to unfavorite tweet: {e}"


# @mcp.tool()
async def retweet(tweet_id: str) -> str:
    """Retweets a tweet."""
    try:
        client = await get_twitter_client()
        await client.retweet(tweet_id)
        return f"Successfully retweeted tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to retweet: {e}")
        return f"[ERROR] Failed to retweet: {e}"


# @mcp.tool()
async def delete_retweet(tweet_id: str) -> str:
    """Deletes the retweet."""
    try:
        client = await get_twitter_client()
        await client.delete_retweet(tweet_id)
        return f"Successfully deleted retweet of tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to delete retweet: {e}")
        return f"[ERROR] Failed to delete retweet: {e}"


@mcp.tool()
async def bookmark_tweet(
    tweet_id: str, folder_id: Optional[str] = None
) -> str:
    """Adds the tweet to bookmarks."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        await with_rate_limit(
            "bookmark_tweet", lambda: client.bookmark_tweet(tweet_id, folder_id)
        )
        return f"Successfully bookmarked tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to bookmark tweet: {e}")
        return f"[ERROR] Failed to bookmark tweet: {e}"


@mcp.tool()
async def delete_bookmark(tweet_id: str) -> str:
    """Removes the tweet from bookmarks."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        await with_rate_limit(
            "delete_bookmark", lambda: client.delete_bookmark(tweet_id)
        )
        return f"Successfully deleted bookmark for tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to delete bookmark: {e}")
        return f"[ERROR] Failed to delete bookmark: {e}"


@mcp.tool()
async def get_bookmarks(
    count: int = 20,
    cursor: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> str:
    """Retrieves bookmarks from the authenticated user’s Twitter account."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        bookmarks = await with_rate_limit(
            "get_bookmarks", lambda: client.get_bookmarks(count, cursor, folder_id)
        )
        return convert_tweets_to_markdown(bookmarks)
    except Exception as e:
        logger.error(f"Failed to get bookmarks: {e}")
        return f"[ERROR] Failed to get bookmarks: {e}"


@mcp.tool()
async def get_all_bookmarks(
    folder_id: Optional[str] = None,
    page_size: int = 20,
    max_pages: int = 200,
    page_delay_range: tuple[float, float] = (3.0, 8.0),
) -> str:
    """Read all bookmarks by paginating until end or max_pages reached.

    Built-in anti-rate-limit: 3-8s random delay between pages, 429 auto-backoff.
    Returns markdown of all collected tweets.
    """
    await throttler.wait()
    try:
        client = await get_twitter_client()
        all_tweets = await paginate_all(
            first_call=lambda: client.get_bookmarks(page_size, None, folder_id),
            page_delay_range=page_delay_range,
            max_pages=max_pages,
        )
        return convert_tweets_to_markdown(all_tweets)
    except Exception as e:
        logger.error(f"Failed to get all bookmarks: {e}")
        return f"[ERROR] Failed to get all bookmarks: {e}"


# @mcp.tool()
async def delete_all_bookmarks() -> str:
    """Deleted all bookmarks."""
    try:
        client = await get_twitter_client()
        await client.delete_all_bookmarks()
        return "Successfully deleted all bookmarks"
    except Exception as e:
        logger.error(f"Failed to delete all bookmarks: {e}")
        return f"[ERROR] Failed to delete all bookmarks: {e}"


@mcp.tool()
async def get_bookmark_folders(cursor: Optional[str] = None) -> str:
    """Retrieves bookmark folders."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        folders = await with_rate_limit(
            "get_bookmark_folders", lambda: client.get_bookmark_folders(cursor)
        )
        return str(folders)
    except Exception as e:
        logger.error(f"Failed to get bookmark folders: {e}")
        return f"[ERROR] Failed to get bookmark folders: {e}"


# @mcp.tool()
async def edit_bookmark_folder(folder_id: str, name: str) -> str:
    """Edits a bookmark folder."""
    try:
        client = await get_twitter_client()
        folder = await client.edit_bookmark_folder(folder_id, name)
        return f"Successfully edited bookmark folder {folder.id}"
    except Exception as e:
        logger.error(f"Failed to edit bookmark folder: {e}")
        logger.error(f"Failed to delete bookmark folder: {e}")
        return f"[ERROR] Failed to delete bookmark folder: {e}"


# @mcp.tool()
async def create_bookmark_folder(name: str) -> str:
    """Creates a bookmark folder."""
    try:
        client = await get_twitter_client()
        folder = await client.create_bookmark_folder(name)
        return f"Successfully created bookmark folder {folder.id}"
    except Exception as e:
        logger.error(f"Failed to create bookmark folder: {e}")
        return f"[ERROR] Failed to create bookmark folder: {e}"


# @mcp.tool()
async def follow_user(user_id: str) -> str:
    """Follows a user."""
    try:
        client = await get_twitter_client()
        user = await client.follow_user(user_id)
        return f"Successfully followed user {user.id}"
    except Exception as e:
        logger.error(f"Failed to follow user: {e}")
        return f"[ERROR] Failed to follow user: {e}"


# @mcp.tool()
async def unfollow_user(user_id: str) -> str:
    """Unfollows a user."""
    try:
        client = await get_twitter_client()
        user = await client.unfollow_user(user_id)
        return f"Successfully unfollowed user {user.id}"
    except Exception as e:
        logger.error(f"Failed to unfollow user: {e}")
        return f"[ERROR] Failed to unfollow user: {e}"


# @mcp.tool()
async def block_user(user_id: str) -> str:
    """Blocks a user."""
    try:
        client = await get_twitter_client()
        user = await client.block_user(user_id)
        return f"Successfully blocked user {user.id}"
    except Exception as e:
        logger.error(f"Failed to block user: {e}")
        return f"[ERROR] Failed to block user: {e}"


# @mcp.tool()
async def unblock_user(user_id: str) -> str:
    """Unblocks a user."""
    try:
        client = await get_twitter_client()
        user = await client.unblock_user(user_id)
        return f"Successfully unblocked user {user.id}"
    except Exception as e:
        logger.error(f"Failed to unblock user: {e}")
        return f"[ERROR] Failed to unblock user: {e}"


# @mcp.tool()
async def mute_user(user_id: str) -> str:
    """Mutes a user."""
    try:
        client = await get_twitter_client()
        user = await client.mute_user(user_id)
        return f"Successfully muted user {user.id}"
    except Exception as e:
        logger.error(f"Failed to mute user: {e}")
        return f"[ERROR] Failed to mute user: {e}"


@mcp.tool(
    name="get_trends",
    description="Get trending topics on Twitter. Takes category (default: trending) and count (default: 20)"
)
async def get_trends(
    ctx: Context,
    category: str = "trending",
    count: int = 20,
    retry: bool = True,
    additional_request_params: Optional[dict] = None,
) -> str:
    """Get trending topics on Twitter."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        trends = await with_rate_limit(
            "get_trends", lambda: client.get_trends(category, count, retry, additional_request_params)
        )
        return json.dumps([{
            "name": trend.name,
            "url": trend.url,
            "tweet_volume": trend.tweet_volume
        } for trend in trends], indent=2)
    except Exception as e:
        logger.error(f"Failed to get trends: {e}")
        return f"[ERROR] Failed to get trends: {e}"


@mcp.tool()
async def get_user_followers(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Retrieves a list of followers for a given user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        followers = await with_rate_limit(
            "get_user_followers", lambda: client.get_user_followers(user_id, count, cursor)
        )
        return convert_tweets_to_markdown(followers)
    except Exception as e:
        logger.error(f"Failed to get user followers: {e}")
        return f"[ERROR] Failed to get user followers: {e}"


@mcp.tool()
async def get_latest_followers(
    user_id: Optional[str] = None,
    screen_name: Optional[str] = None,
    count: int = 200,
    cursor: Optional[str] = None,
) -> str:
    """Retrieves the latest followers."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        followers = await with_rate_limit(
            "get_latest_followers", lambda: client.get_latest_followers(user_id, screen_name, count, cursor)
        )
        return convert_tweets_to_markdown(followers)
    except Exception as e:
        logger.error(f"Failed to get latest followers: {e}")
        return f"[ERROR] Failed to get latest followers: {e}"


@mcp.tool()
async def get_latest_friends(
    user_id: Optional[str] = None,
    screen_name: Optional[str] = None,
    count: int = 200,
    cursor: Optional[str] = None,
) -> str:
    """Retrieves the latest friends (following users)."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        friends = await with_rate_limit(
            "get_latest_friends", lambda: client.get_latest_friends(user_id, screen_name, count, cursor)
        )
        return convert_tweets_to_markdown(friends)
    except Exception as e:
        logger.error(f"Failed to get latest friends: {e}")
        return f"[ERROR] Failed to get latest friends: {e}"


@mcp.tool()
async def get_user_verified_followers(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Retrieves a list of verified followers for a given user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        followers = await with_rate_limit(
            "get_user_verified_followers", lambda: client.get_user_verified_followers(user_id, count, cursor)
        )
        return convert_tweets_to_markdown(followers)
    except Exception as e:
        logger.error(f"Failed to get user verified followers: {e}")
        return f"[ERROR] Failed to get user verified followers: {e}"


@mcp.tool()
async def get_user_followers_you_know(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Retrieves a list of common followers."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        followers = await with_rate_limit(
            "get_user_followers_you_know", lambda: client.get_user_followers_you_know(user_id, count, cursor)
        )
        return convert_tweets_to_markdown(followers)
    except Exception as e:
        logger.error(f"Failed to get user followers you might know: {e}")
        return f"[ERROR] Failed to get user followers you might know: {e}"


@mcp.tool()
async def get_user_following(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Retrieves a list of users whom the given user is following."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        following = await with_rate_limit(
            "get_user_following", lambda: client.get_user_following(user_id, count, cursor)
        )
        return convert_tweets_to_markdown(following)
    except Exception as e:
        logger.error(f"Failed to get user following: {e}")
        return f"[ERROR] Failed to get user following: {e}"


@mcp.tool()
async def get_user_subscriptions(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Retrieves a list of users to which the specified user is subscribed."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        subscriptions = await with_rate_limit(
            "get_user_subscriptions", lambda: client.get_user_subscriptions(user_id, count, cursor)
        )
        return convert_tweets_to_markdown(subscriptions)
    except Exception as e:
        logger.error(f"Failed to get user subscriptions: {e}")
        return f"[ERROR] Failed to get user subscriptions: {e}"


@mcp.tool()
async def get_followers_ids(
    user_id: Optional[str] = None,
    screen_name: Optional[str] = None,
    count: int = 5000,
    cursor: Optional[str] = None,
) -> str:
    """Fetches the IDs of the followers of a specified user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        ids = await with_rate_limit(
            "get_followers_ids", lambda: client.get_followers_ids(user_id, screen_name, count, cursor)
        )
        return str(ids)
    except Exception as e:
        logger.error(f"Failed to get followers ids: {e}")
        return f"[ERROR] Failed to get followers ids: {e}"


@mcp.tool()
async def get_friends_ids(
    user_id: Optional[str] = None,
    screen_name: Optional[str] = None,
    count: int = 5000,
    cursor: Optional[str] = None,
) -> str:
    """Fetches the IDs of the friends (following users) of a specified user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        ids = await with_rate_limit(
            "get_friends_ids", lambda: client.get_friends_ids(user_id, screen_name, count, cursor)
        )
        return str(ids)
    except Exception as e:
        logger.error(f"Failed to get friends ids: {e}")
        return f"[ERROR] Failed to get friends ids: {e}"


# @mcp.tool()
async def unmute_user(user_id: str) -> str:
    """Unmutes a user."""
    try:
        client = await get_twitter_client()
        user = await client.unmute_user(user_id)
        return f"Successfully unmuted user {user.id}"
    except Exception as e:
        logger.error(f"Failed to unmute user: {e}")
        return f"[ERROR] Failed to unmute user: {e}"


@mcp.tool()
async def get_highlights_tweets(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Retrieves highlighted tweets from a user’s timeline."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweets = await with_rate_limit(
            "get_highlights_tweets", lambda: client.get_user_highlights_tweets(user_id, count, cursor)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get user highlights tweets: {e}")
        return f"[ERROR] Failed to get user highlights tweets: {e}"


# @mcp.tool()
async def update_user() -> str:
    """Updates the user."""
    try:
        client = await get_twitter_client()
        user = await client.user()
        await user.update()
        return f"Successfully updated user {user.id}"
    except Exception as e:
        logger.error(f"Failed to update user: {e}")
        return f"[ERROR] Failed to update user: {e}"


# @mcp.tool()
async def add_reaction_to_message(
    message_id: str, conversation_id: str, emoji: str
) -> str:
    """Adds a reaction emoji to a specific message in a conversation."""
    try:
        client = await get_twitter_client()
        await client.add_reaction_to_message(message_id, conversation_id, emoji)
        return f"Successfully added reaction to message {message_id}"
    except Exception as e:
        logger.error(f"Failed to add reaction to message: {e}")
        return f"[ERROR] Failed to add reaction to message: {e}"


# @mcp.tool()
async def remove_reaction_from_message(
    message_id: str, conversation_id: str, emoji: str
) -> str:
    """Remove a reaction from a message."""
    try:
        client = await get_twitter_client()
        await client.remove_reaction_from_message(message_id, conversation_id, emoji)
        return f"Successfully removed reaction from message {message_id}"
    except Exception as e:
        logger.error(f"Failed to remove reaction from message: {e}")
        return f"[ERROR] Failed to remove reaction from message: {e}"


@mcp.tool()
async def get_dm_history(
    user_id: str, max_id: Optional[str] = None
) -> str:
    """Retrieves the DM conversation history with a specific user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        messages = await with_rate_limit(
            "get_dm_history", lambda: client.get_dm_history(user_id, max_id)
        )
        return str(messages)
    except Exception as e:
        logger.error(f"Failed to get DM history: {e}")
        return f"[ERROR] Failed to get DM history: {e}"


# @mcp.tool()
async def send_dm_to_group(
    group_id: str,
    text: str,
    media_id: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> str:
    """Sends a message to a group."""
    try:
        client = await get_twitter_client()
        message = await client.send_dm_to_group(group_id, text, media_id, reply_to)
        return f"Successfully sent DM to group {group_id}: {message.id}"
    except Exception as e:
        logger.error(f"Failed to send DM to group: {e}")
        return f"[ERROR] Failed to send DM to group: {e}"


# @mcp.tool()
async def get_group_dm_history(
    group_id: str, max_id: Optional[str] = None
) -> str:
    """Retrieves the DM conversation history in a group."""
    try:
        client = await get_twitter_client()
        messages = await client.get_group_dm_history(group_id, max_id)
        return str(messages)
    except Exception as e:
        logger.error(f"Failed to get group DM history: {e}")
        return f"[ERROR] Failed to get group DM history: {e}"


# @mcp.tool()
async def get_group(group_id: str) -> str:
    """Fetches a group by ID."""
    try:
        client = await get_twitter_client()
        group = await client.get_group(group_id)
        return str(group)
    except Exception as e:
        logger.error(f"Failed to get group: {e}")
        return f"[ERROR] Failed to get group: {e}"


# @mcp.tool()
async def add_members_to_group(
    group_id: str, user_ids: List[str]
) -> str:
    """Adds members to a group."""
    try:
        client = await get_twitter_client()
        await client.add_members_to_group(group_id, user_ids)
        return f"Successfully added members to group {group_id}"
    except Exception as e:
        logger.error(f"Failed to add members to group: {e}")
        return f"[ERROR] Failed to add members to group: {e}"


# @mcp.tool()
async def change_group_name(group_id: str, name: str) -> str:
    """Changes group name."""
    try:
        client = await get_twitter_client()
        await client.change_group_name(group_id, name)
        return f"Successfully changed group name for group {group_id}"
    except Exception as e:
        logger.error(f"Failed to change group name: {e}")
        return f"[ERROR] Failed to change group name: {e}"


@mcp.tool(
    name="get_user_profile",
    description="Get detailed profile information for a user"
)
async def get_user_profile(ctx: Context, user_id: str) -> Dict[str, Any]:
    """Get detailed profile information for a user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        user = await with_rate_limit(
            "get_user_profile", lambda: client.get_user_by_id(user_id)
        )

        return {
            "id": user.id,
            "name": user.name,
            "screen_name": user.screen_name,
            "description": user.description,
            "followers_count": user.followers_count,
            "friends_count": user.friends_count,
            "statuses_count": user.statuses_count,
            "created_at": str(user.created_at),
            "verified": user.verified,
            "protected": user.protected,
            "location": user.location,
            "url": user.url,
            "profile_image_url": user.profile_image_url,
        }
    except Exception as e:
        logger.error(f"Failed to get user profile: {e}")
        return {"error": f"[ERROR] {e}"}


@mcp.tool()
async def get_tweet_details(tweet_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific tweet."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweet = await with_rate_limit(
            "get_tweet_details", lambda: client.get_tweet_by_id(tweet_id)
        )

        return {
            "success": True,
            "data": {
                "id": tweet.id,
                "text": tweet.text,
                "created_at": str(tweet.created_at),
                "author_id": tweet.author_id,
                "retweet_count": tweet.retweet_count,
                "favorite_count": tweet.favorite_count,
                "reply_count": tweet.reply_count,
                "quote_count": tweet.quote_count,
                "lang": tweet.lang
            }
        }
    except Exception as e:
        logger.error(f"Failed to get tweet details: {e}")
        return {
            "success": False,
            "error": f"[ERROR] {e}",
            "error_type": "TwitterAPIError"
        }


# @mcp.tool()
async def vote_on_poll(tweet_id: str, choice: str) -> str:
    """Vote on a poll."""
    try:
        client = await get_twitter_client()
        tweet = await client.get_tweet_by_id(tweet_id)
        if not hasattr(tweet, "card"):
            return "This tweet does not contain a poll"

        await client.vote(
            selected_choice=choice,
            card_uri=tweet.card.url,
            tweet_id=tweet_id,
            card_name=tweet.card.name,
        )
        return f"Successfully voted for '{choice}' on tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to vote on poll: {e}")
        return f"[ERROR] Failed to vote on poll: {e}"


@mcp.tool()
async def get_user_mentions(
    user_id: str, count: int = 20, cursor: Optional[str] = None
) -> str:
    """Get tweets mentioning a specific user."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        query = f"@{user_id}"
        tweets = await with_rate_limit(
            "get_user_mentions", lambda: client.search_tweet(query, product="Latest", count=count, cursor=cursor)
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get user mentions: {e}")
        return f"[ERROR] Failed to get user mentions: {e}"


@mcp.tool()
async def get_conversation_thread(tweet_id: str) -> str:
    """Get the full conversation thread for a tweet."""
    await throttler.wait()
    try:
        client = await get_twitter_client()
        tweet = await with_rate_limit(
            "get_conversation_thread", lambda: client.get_tweet_by_id(tweet_id)
        )
        conversation = []

        # Get parent tweets if this is a reply
        current_tweet = tweet
        while hasattr(current_tweet, "in_reply_to_status_id"):
            parent_id = current_tweet.in_reply_to_status_id
            if parent_id:
                await throttler.wait()
                current_tweet = await with_rate_limit(
                    "get_conversation_thread", lambda: client.get_tweet_by_id(parent_id)
                )
                conversation.insert(0, current_tweet)
            else:
                break

        # Add the main tweet
        conversation.append(tweet)

        return convert_tweets_to_markdown(conversation)
    except Exception as e:
        logger.error(f"Failed to get conversation thread: {e}")
        return f"[ERROR] Failed to get conversation thread: {e}"
