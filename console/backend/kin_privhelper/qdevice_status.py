"""Parse corosync-qdevice-tool -s (no live qdevice).

Ubuntu 22.04 corosync-qdevice-tool (jammy man page) prints a `State:` line
and, when up, also `Connected since:`. Searching the whole blob for the
substring Connected is true for both. Match the State: field instead.
"""

from __future__ import annotations

import re

_STATE_RE = re.compile(r"(?im)^\s*State:\s*(.+?)\s*$")


def qdevice_tool_state(text: str) -> str:
    """Return the model-net State: value, or empty if the line is missing."""
    last = ""
    for match in _STATE_RE.finditer(text or ""):
        last = match.group(1).strip()
    return last


def qdevice_tool_is_connected(text: str, qnetd_ip: str = "") -> bool:
    """True only when State is exactly Connected and the qnetd IP is listed."""
    blob = text or ""
    if qnetd_ip and qnetd_ip not in blob:
        return False
    return qdevice_tool_state(blob).lower() == "connected"
