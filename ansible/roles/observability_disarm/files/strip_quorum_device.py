#!/usr/bin/env python3
"""Strip quorum.device from corosync.conf (Observability remove)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_QUORUM_WITHOUT_DEVICE = """quorum {
    provider: corosync_votequorum
}"""


def _block_from(text: str, keyword: str) -> tuple[int, int, str]:
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


def strip_quorum_device(corosync_conf: str) -> tuple[str, bool]:
    text = corosync_conf or ""
    start, end, block = _block_from(text, "quorum")
    if start < 0 or not re.search(r"(?m)^\s*model:\s*net\b", block):
        return text, False
    return text[:start] + _QUORUM_WITHOUT_DEVICE + text[end:], True


def main() -> int:
    conf = Path(os.environ.get("COROSYNC_CONF", "/etc/corosync/corosync.conf"))
    text = conf.read_text()
    new, changed = strip_quorum_device(text)
    if not changed:
        print("ALREADY_CLEAR")
        return 0
    conf.write_text(new)
    print("UPDATED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
