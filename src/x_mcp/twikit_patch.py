"""Monkey-patch for twikit 2.3.3 ClientTransaction breakage.

As of 2026-03-18, x.com changed its homepage HTML format: the ondemand.s
filename and its hash are now split into two separate `,<N>:"..."` entries
instead of one inline `"ondemand.s":"<hash>"`. twikit 2.3.3's regex no longer
matches, causing `Exception: Couldn't get KEY_BYTE indices` on every API call.

Upstream issue: https://github.com/d60/twikit/issues/408
Fix is upstream in https://github.com/iSarabjitDhiman/XClientTransaction
(commit 2ff8438) but twikit has not pulled it in as of 2.3.3.

This monkey-patch (from @audioeng89, issue #408 comment 4089055868) patches
`ClientTransaction.get_indices` at import time. Must run BEFORE
`from twikit import Client`.

Remove this whole module once twikit ships a fixed release.
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
