"""Ansible filter wrapping kin_privhelper.corosync_stub (single implementation)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_backend() -> None:
    try:
        import kin_privhelper.corosync_stub  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    tree_root = here.parents[4]
    for candidate in (tree_root / "console" / "backend", tree_root / "backend"):
        if (candidate / "kin_privhelper" / "corosync_stub.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_backend()
from kin_privhelper.corosync_stub import (  # noqa: E402
    is_debian_corosync_stub,
    parse_corosync_cluster_name,
)


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "kin_is_debian_corosync_stub": is_debian_corosync_stub,
            "kin_parse_corosync_cluster_name": parse_corosync_cluster_name,
        }
