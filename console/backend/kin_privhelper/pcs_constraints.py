"""Parse pcs constraint listings (no live Pacemaker).

Ubuntu pcs 0.10 prints `promote kin-drbd-clone then start kin-mail-svc`.
Newer pcs (this lab's location lines use `resource 'kin-drbd-clone'`) prints
`promote resource 'kin-drbd-clone' then start resource 'kin-mail-svc'`.
The stack role must treat both as "already present" so a retry does not
run `pcs constraint add` again.
"""

from __future__ import annotations


def listing_has_promoted_colocation(text: str, group: str, clone: str) -> bool:
    """True when a colocation line already binds group to Promoted clone."""
    if not group or not clone:
        return False
    for line in (text or "").splitlines():
        if group in line and clone in line and "Promoted" in line:
            return True
    return False


def listing_has_promote_then_start(text: str, clone: str, group: str) -> bool:
    """True when an order line already promotes clone then starts group."""
    if not clone or not group:
        return False
    for line in (text or "").splitlines():
        lower = line.lower()
        if clone not in line or group not in line:
            continue
        if "promote" in lower and "then start" in lower:
            return True
    return False
