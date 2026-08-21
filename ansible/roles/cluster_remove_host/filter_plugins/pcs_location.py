"""Ansible filter wrapping kin_privhelper.pcs_location."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_backend() -> None:
    try:
        import kin_privhelper.pcs_location  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    tree_root = here.parents[4]
    for candidate in (tree_root / "console" / "backend", tree_root / "backend"):
        if (candidate / "kin_privhelper" / "pcs_location.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_backend()
from kin_privhelper.pcs_location import location_ids_for_node  # noqa: E402


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {"kin_location_ids_for_node": location_ids_for_node}
