#!/usr/bin/env python3
"""Idempotently write quorum.device into corosync.conf (Phase 2 / no-pcsd qnetd)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def main() -> int:
    conf = Path(os.environ.get("COROSYNC_CONF", "/etc/corosync/corosync.conf"))
    host = os.environ["QNETD_HOST"]
    port = os.environ["QNETD_PORT"]
    algo = os.environ["QNETD_ALGORITHM"]
    tie = os.environ["QNETD_TIE_BREAKER"]
    text = conf.read_text()
    match = re.search(r"quorum\s*\{(?:[^{}]|\{[^{}]*\})*\}", text, flags=re.S)
    if not match:
        print("failed to locate quorum block in corosync.conf", file=sys.stderr)
        return 1
    block = match.group(0)
    already = (
        f"host: {host}" in block
        and f"algorithm: {algo}" in block
        and "model: net" in block
        and "two_node:" not in block
    )
    if already:
        print("ALREADY_CONFIGURED")
        return 0
    device = f"""quorum {{
    provider: corosync_votequorum
    device {{
        votes: 1
        model: net
        net {{
            tls: on
            host: {host}
            port: {port}
            algorithm: {algo}
            tie_breaker: {tie}
        }}
    }}
}}"""
    new, n = re.subn(
        r"quorum\s*\{(?:[^{}]|\{[^{}]*\})*\}",
        device,
        text,
        count=1,
        flags=re.S,
    )
    if n != 1:
        print("failed to replace quorum block in corosync.conf", file=sys.stderr)
        return 1
    conf.write_text(new)
    print("UPDATED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
