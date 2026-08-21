"""Parse corosync-qdevice-tool -s and corosync.conf quorum.device (no live qdevice).

Ubuntu 22.04 corosync-qdevice-tool (jammy man page) prints a `State:` line
and, when up, also `Connected since:`. Searching the whole blob for the
substring Connected is true for both. Match the State: field instead.
"""

from __future__ import annotations

import re

_STATE_RE = re.compile(r"(?im)^\s*State:\s*(.+?)\s*$")
_QUORUM_WITHOUT_DEVICE = """quorum {
    provider: corosync_votequorum
}"""


def _block_from(text: str, keyword: str) -> tuple[int, int, str]:
    """Return (start, end, body) for `keyword { ... }` with nested braces counted."""
    blob = text or ""
    match = re.search(rf"{keyword}\s*\{{", blob)
    if not match:
        return -1, -1, ""
    start = match.start()
    brace = blob.find("{", match.start())
    depth = 0
    for i in range(brace, len(blob)):
        ch = blob[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1, blob[start : i + 1]
    return -1, -1, ""


def quorum_block(corosync_conf: str) -> str:
    _start, _end, body = _block_from(corosync_conf or "", "quorum")
    return body


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


def parse_qnetd_host(corosync_conf: str) -> str:
    """Return quorum.device net host, or empty when no net device is configured."""
    block = quorum_block(corosync_conf)
    if not block or not re.search(r"(?m)^\s*model:\s*net\b", block):
        return ""
    last = ""
    for match in re.finditer(r"(?m)^\s*host:\s*(\S+)\s*$", block):
        last = match.group(1).strip()
    return last


def strip_quorum_device(corosync_conf: str) -> tuple[str, bool]:
    """Replace quorum.device with votequorum only (no two_node).

    two_node:1 would let a single mail node stay quorate while the witness is
    gone, which HA-RUNBOOK section 7 forbids.
    """
    text = corosync_conf or ""
    start, end, block = _block_from(text, "quorum")
    if start < 0 or not re.search(r"(?m)^\s*model:\s*net\b", block):
        return text, False
    return text[:start] + _QUORUM_WITHOUT_DEVICE + text[end:], True


_EXPECTED_RE = re.compile(r"(?im)^\s*Expected votes:\s*(\d+)\s*$")
_TOTAL_RE = re.compile(r"(?im)^\s*Total votes:\s*(\d+)\s*$")
_QUORUM_RE = re.compile(r"(?im)^\s*Quorum:\s*(\d+)\s*$")


def parse_quorum_votes(quorum_text: str) -> tuple[int | None, int | None, int | None]:
    """Return (expected, total, quorum) from `pcs quorum status`."""
    def _one(pattern: re.Pattern[str]) -> int | None:
        match = pattern.search(quorum_text or "")
        if not match:
            return None
        return int(match.group(1))

    return _one(_EXPECTED_RE), _one(_TOTAL_RE), _one(_QUORUM_RE)


def quorum_votes_match_rebuild(quorum_text: str) -> bool:
    """HA-RUNBOOK section 7: expected 3 / total 3 / quorum 2."""
    expected, total, quorum = parse_quorum_votes(quorum_text)
    return expected == 3 and total == 3 and quorum == 2
