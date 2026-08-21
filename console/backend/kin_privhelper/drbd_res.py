"""Parse a kin-zimbra.res file (no live drbdadm).

Remove-host rewrites this file on the survivor. Role defaults still say
/dev/loop21. The live rebuild uses a partition (sdb2). Prefer paths from
the file that is already on the node.
"""

from __future__ import annotations

import re

_DEV = re.compile(r"^/dev/[A-Za-z0-9/_.+-]+$")
_ON_BLOCK = re.compile(r"on\s+(\S+)\s*\{(.*?)\n\s*\}", re.S)
_META_ANY = re.compile(r"meta-disk\s+(\S+);")


def _clean_dev(raw: str) -> str:
    disk = (raw or "").strip().rstrip(";").strip("'\"")
    if not disk or disk.lower() == "internal":
        return ""
    if not _DEV.match(disk):
        return ""
    return disk


def parse_meta_disks(text: str) -> list[str]:
    found: list[str] = []
    for raw in _META_ANY.findall(text or ""):
        disk = _clean_dev(raw)
        if disk and disk not in found:
            found.append(disk)
    return found


def parse_first_meta_disk(text: str) -> str:
    disks = parse_meta_disks(text)
    return disks[0] if disks else ""


def _node_block(text: str, node: str) -> str:
    if not node:
        return ""
    for name, body in _ON_BLOCK.findall(text or ""):
        if name.strip().strip("'\"") == node:
            return body
    return ""


def _field(block: str, key: str) -> str:
    if not block:
        return ""
    if key == "disk":
        match = re.search(r"(?<![A-Za-z-])disk\s+(\S+);", block)
    else:
        match = re.search(rf"{re.escape(key)}\s+(\S+);", block)
    if not match:
        return ""
    return _clean_dev(match.group(1))


def node_disk(text: str, node: str) -> str:
    return _field(_node_block(text, node), "disk")


def node_meta_disk(text: str, node: str) -> str:
    return _field(_node_block(text, node), "meta-disk")


def node_device(text: str, node: str) -> str:
    return _field(_node_block(text, node), "device")


def prefer_live_path(live: str, inventory: str) -> str:
    """Use the live .res path when present so loop21 inventory cannot win."""
    return (live or "").strip() or (inventory or "").strip()
