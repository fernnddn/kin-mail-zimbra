"""Parse pcs constraint location --full listings (no live Pacemaker).

Ubuntu 22.04 pcs 0.10 prints `(id: location-...)`. Newer listings quote the
resource and may put `id:` without wrapping parentheses. Remove-host must
collect ids in either shape so a retry does not miss a leftover pin.
"""

from __future__ import annotations

import re

_ID_PAREN = re.compile(r"\(id:\s*([^)]+)\)")
_ID_BARE = re.compile(r"\bid:\s*([A-Za-z0-9_.:-]+)")


def location_ids_for_node(text: str, node: str) -> list[str]:
    """Return location constraint ids whose line mentions node."""
    if not node:
        return []
    ids: list[str] = []
    for line in (text or "").splitlines():
        if node not in line:
            continue
        found = [m.group(1).strip().strip("'\"") for m in _ID_PAREN.finditer(line)]
        if not found:
            bare = _ID_BARE.search(line)
            if bare:
                found = [bare.group(1).strip().strip("'\"")]
        for ident in found:
            if ident and ident not in ids:
                ids.append(ident)
    return ids
