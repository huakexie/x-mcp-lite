"""Monkey-patches for twikit 2.3.3 bugs that break on current x.com.

Two independent upstream bugs, each patched at import time. This module must
be imported BEFORE `from twikit import Client` (see twitter.py).

1. ClientTransaction.get_indices — `Couldn't get KEY_BYTE indices`
   As of 2026-03-18, x.com changed its homepage HTML format: the ondemand.s
   filename and its hash are now split into two separate `,<N>:"..."` entries
   instead of one inline `"ondemand.s":"<hash>"`. twikit 2.3.3's regex no
   longer matches, causing the exception on every API call.
   Upstream issue: https://github.com/d60/twikit/issues/408
   Fix is upstream in https://github.com/iSarabjitDhiman/XClientTransaction
   (commit 2ff8438) but twikit has not pulled it in as of 2.3.3. Patch below
   is from @audioeng89, issue #408 comment 4089055868.

2. User.__init__ — `KeyError` on optional legacy.* fields
   twikit 2.3.3 hard-indexes many optional `legacy.*` fields that x.com omits
   for some accounts (e.g. `entities.description.urls` for accounts with no
   bio link -> KeyError: 'urls'; `withheld_in_countries`; etc). This crashes
   every call that parses a User object (get_user, get_bookmarks, timelines,
   followers, ...). Same class of bug as d60/twikit PR #341 (can_media_tag).
   Patched by wrapping the incoming data so missing keys degrade to empty.

Remove each patch once twikit ships a release that fixes the corresponding bug.
"""
from __future__ import annotations

import re

_tx_mod = __import__(
    "twikit.x_client_transaction.transaction", fromlist=["ClientTransaction"]
)
_tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
    r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE)
)
_tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'


async def _patched_get_indices(self, home_page_response, session, headers):
    key_byte_indices: list[str] = []
    response = self.validate_response(home_page_response) or self.home_page_response
    on_demand_file_match = _tx_mod.ON_DEMAND_FILE_REGEX.search(str(response))
    if not on_demand_file_match:
        raise Exception("Couldn't get KEY_BYTE indices (patched: ondemand.s index not found)")
    on_demand_file_index = on_demand_file_match.group(1)
    regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index))
    hash_match = regex.search(str(response))
    if not hash_match:
        raise Exception("Couldn't get KEY_BYTE indices (patched: ondemand hash not found)")
    filename = hash_match.group(1)
    on_demand_file_url = (
        f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
    )
    on_demand_file_response = await session.request(
        method="GET", url=on_demand_file_url, headers=headers
    )
    key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(
        str(on_demand_file_response.text)
    )
    for item in key_byte_indices_match:
        key_byte_indices.append(item.group(2))
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")
    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]


_tx_mod.ClientTransaction.get_indices = _patched_get_indices


# --- User parsing: KeyError for optional legacy.* fields x.com omits ---
#
# twikit 2.3.3 `User.__init__` hard-indexes many optional `legacy.*` fields
# that x.com omits for some accounts, each raising a KeyError on every call
# that parses a User object (get_user, get_user_by_id/screen_name,
# get_bookmarks, timelines, followers, ...). Observed in the wild:
#   - legacy['entities']['description']['urls']  (accounts with no bio link;
#     entities == {"description": {}})  -> KeyError: 'urls'
#   - legacy['withheld_in_countries']            -> KeyError: 'withheld_in_countries'
# and there are more hard-indexed optional keys where those came from. This is
# the same class of bug as d60/twikit PR #341 (can_media_tag).
#
# Rather than whack-a-mole each field, we wrap the incoming `data` so that any
# missing key at any depth degrades to an empty lenient dict instead of
# raising. Present keys keep their real values, so normal accounts parse
# unchanged; only the omitted optional fields end up empty.
#
# Remove once twikit ships a release that `.get`-guards these accesses.
_user_mod = __import__("twikit.user", fromlist=["User"])
_orig_user_init = _user_mod.User.__init__


class _LenientDict(dict):
    def __missing__(self, key):
        return _LenientDict()


def _lenient(obj):
    if isinstance(obj, dict):
        return _LenientDict((k, _lenient(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_lenient(v) for v in obj]
    return obj


def _patched_user_init(self, client, data):
    if isinstance(data, dict):
        data = _lenient(data)
    _orig_user_init(self, client, data)


_user_mod.User.__init__ = _patched_user_init


# --- tweet_detail: KeyError 'itemContent' from flattened cursor entries ---
#
# twikit 2.3.3 reads reply/next cursors from the tweet_detail response at
#   entries[-1]['content']['itemContent']['value']  and
#   reply['item']['itemContent']['value']
# (in Client.get_tweet_by_id and Client._get_more_replies). x.com flattened
# cursor entries: the value now sits directly on the cursor object as
# {'entryType': 'TimelineTimelineCursor', 'cursorType': ..., 'value': ...} with
# no 'itemContent' nesting -> KeyError: 'itemContent'. This breaks the
# get_tweet_by_id / get_tweet_details / get_conversation_thread tools.
#
# All three crash sites parse the response returned by GQLClient.tweet_detail,
# so we patch that one method: walk the response and re-nest each flattened
# cursor's value back under 'itemContent' so the original code works unchanged.
#
# Remove once twikit handles the flattened cursor shape.
_gql_mod = __import__("twikit.client.gql", fromlist=["GQLClient"])
_orig_tweet_detail = _gql_mod.GQLClient.tweet_detail


def _restore_cursor_itemcontent(obj) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _restore_cursor_itemcontent(v)
        if "cursorType" in obj and "value" in obj and "itemContent" not in obj:
            obj["itemContent"] = {
                "cursorType": obj.get("cursorType"),
                "value": obj.get("value"),
            }
    elif isinstance(obj, list):
        for v in obj:
            _restore_cursor_itemcontent(v)


async def _patched_tweet_detail(self, tweet_id, cursor):
    result = await _orig_tweet_detail(self, tweet_id, cursor)
    try:
        _restore_cursor_itemcontent(result[0])
    except (TypeError, IndexError, KeyError):
        pass
    return result


_gql_mod.GQLClient.tweet_detail = _patched_tweet_detail
