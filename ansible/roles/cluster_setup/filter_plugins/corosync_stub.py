"""Detect the Debian/Ubuntu corosync package-default stub config.

Installing corosync.deb enables and starts corosync with a generic example
file (cluster_name debian, loopback-only node, often no node name). That is
not a real cluster. A genuine membership (LAN IPs, extra nodes, a name other
than debian) must not match.
"""

from __future__ import annotations

import re
from typing import Any

_CLUSTER_NAME_RE = re.compile(r"^cluster_name:\s*(\S+)", re.IGNORECASE)
_RING_ADDR_RE = re.compile(r"^ring\d+_addr:\s*(\S+)", re.IGNORECASE)
_NODE_NAME_RE = re.compile(r"^name:\s*(\S+)", re.IGNORECASE)


def _active_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if stripped:
            lines.append(stripped)
    return lines


def _is_loopback_addr(addr: str) -> bool:
    value = addr.strip().lower().strip("[]").rstrip(";")
    if value in {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}:
        return True
    parts = value.split(".")
    if len(parts) == 4 and parts[0] == "127":
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False
    return False


def is_debian_corosync_stub(text: Any) -> bool:
    """True only for the untouched Debian/Ubuntu package example config."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str) or not text.strip():
        return False

    cluster_names: list[str] = []
    ring_addrs: list[str] = []
    for line in _active_lines(text):
        name_match = _CLUSTER_NAME_RE.match(line)
        if name_match:
            cluster_names.append(name_match.group(1).strip().strip("\"'"))
            continue
        ring_match = _RING_ADDR_RE.match(line)
        if ring_match:
            ring_addrs.append(ring_match.group(1).strip().strip("\"'"))

    if cluster_names != ["debian"]:
        return False
    if any(not _is_loopback_addr(addr) for addr in ring_addrs):
        return False
    if len(ring_addrs) > 1:
        return False
    return True


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {"kin_is_debian_corosync_stub": is_debian_corosync_stub}
