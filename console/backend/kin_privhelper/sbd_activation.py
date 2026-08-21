"""SBD activation and fence_sbd timeout decisions (no live cluster).

Ubuntu's sbd.service uses RefuseManualStart + RequiredBy=corosync/pacemaker.
Enabling the unit only creates Requires symlinks; it does not start SBD while
corosync/pacemaker are already running. Activation therefore needs a local
cluster stack recycle, but only when this node is not hosting the mail stack.
"""

from __future__ import annotations

import re


def sbd_activation_plan(*, sbd_active: bool, local_mail_busy: bool) -> str:
    """Decide how to bring sbd.service up on this node.

    Returns:
      already_active - sbd is running; do nothing
      recycle_local  - safe to pcs-cluster-stop/start this node to pull SBD in
      refuse_busy    - mail stack is live here; do not bounce the node
    """
    if sbd_active:
        return "already_active"
    if local_mail_busy:
        return "refuse_busy"
    return "recycle_local"


def pcs_status_shows_local_mail_busy(status: str, node_names: list[str]) -> bool:
    """True when pcs status shows Promoted DRBD or Started kin-* on this node."""
    names = [n.strip().lower() for n in node_names if n and str(n).strip()]
    if not names or not status:
        return False
    # Match both "Started node" and "Promoted node" resource lines.
    busy_re = re.compile(
        r"\b(?:Started|Promoted)\s+(\S+)",
        re.IGNORECASE,
    )
    for raw in status.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Only care about KIN Mail resources, not unrelated Started lines.
        lower = line.lower()
        if not any(
            key in lower
            for key in (
                "kin-fs",
                "kin-zimbra",
                "kin-vip",
                "kin-drbd",
                "kin-mail-svc",
            )
        ):
            continue
        for match in busy_re.finditer(line):
            host = match.group(1).rstrip(")").lower()
            if host in names:
                return True
            # FQDN vs short name
            short = host.split(".", 1)[0]
            if short in names or any(n.split(".", 1)[0] == short for n in names):
                return True
    return False


def fence_sbd_power_timeout_ok(power_timeout: int | str, msgwait: int | str) -> bool:
    """fence_sbd requires power_timeout strictly greater than SBD msgwait."""
    try:
        pt = int(str(power_timeout).strip().rstrip("s"))
        mw = int(str(msgwait).strip().rstrip("s"))
    except ValueError:
        return False
    return pt > mw > 0


def stonith_config_has_attr(text: str, name: str, *values: str) -> bool:
    """True when pcs stonith config lists name=value (any of values)."""
    if not name or not values:
        return False
    wanted = {str(v).strip().lower() for v in values if str(v).strip()}
    if not wanted:
        return False
    name_l = name.strip().lower()
    for raw in (text or "").splitlines():
        for token in raw.replace(",", " ").split():
            if "=" not in token:
                continue
            key, _, got = token.partition("=")
            if key.strip().lower() != name_l:
                continue
            if got.strip().strip("'\"").lower() in wanted:
                return True
    return False
