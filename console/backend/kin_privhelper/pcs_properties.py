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


def parse_stonith_enabled(text: str) -> bool | None:
    """True/False when `pcs property` states stonith-enabled, None when it does not.

    Tri-state on purpose. A failed capture returns an empty string, and
    reporting that as "fencing is off" would raise a false alarm on a healthy
    cluster - which is worse than saying nothing, because the alarm it raises
    is the one that means "your data can diverge".
    """
    if listing_has_property(text, "stonith-enabled", "true"):
        return True
    if listing_has_property(text, "stonith-enabled", "false"):
        return False
    return None
