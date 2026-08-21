"""Ansible filter wrapping kin_privhelper.drbd_res."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_backend() -> None:
    try:
        import kin_privhelper.drbd_res  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    tree_root = here.parents[4]
    for candidate in (tree_root / "console" / "backend", tree_root / "backend"):
        if (candidate / "kin_privhelper" / "drbd_res.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_backend()
from kin_privhelper.drbd_res import (  # noqa: E402
    node_device,
    node_disk,
    node_meta_disk,
    prefer_live_path,
)


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "kin_drbd_node_disk": node_disk,
            "kin_drbd_node_meta_disk": node_meta_disk,
            "kin_drbd_node_device": node_device,
            "kin_drbd_prefer_live_path": prefer_live_path,
        }
