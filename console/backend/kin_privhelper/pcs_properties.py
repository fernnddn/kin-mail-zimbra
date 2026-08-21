"""Parse pcs property config listings (no live Pacemaker).

Ubuntu 22.04 pcs 0.10 prints `stonith-enabled: true`. Newer pcs may print
`stonith-enabled=true`. A verify that only looks for the equals form refuses
a correctly configured jammy cluster and forces a retry.
"""

from __future__ import annotations


def listing_has_property(text: str, name: str, *values: str) -> bool:
    """True when a property line lists name as one of the given values."""
    if not name or not values:
        return False
    wanted = {str(v).strip().lower() for v in values if str(v).strip()}
    if not wanted:
        return False
    name_l = name.strip().lower()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("cluster properties"):
            continue
        key = ""
        got = ""
        if "=" in line and not line.lower().startswith(name_l + ":"):
            key, _, got = line.partition("=")
        elif ":" in line:
            key, _, got = line.partition(":")
        else:
            continue
        if key.strip().lower() != name_l:
            continue
        value = got.strip().strip("'\"")
        if value.lower() in wanted:
            return True
    return False
