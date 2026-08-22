"""Ansible filter wrapping kin_privhelper.observability_lifecycle.boot_id_unchanged."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_backend() -> None:
    try:
        import kin_privhelper.observability_lifecycle  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    tree_root = here.parents[4]
    for candidate in (tree_root / "console" / "backend", tree_root / "backend"):
        if (candidate / "kin_privhelper" / "observability_lifecycle.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_backend()
from kin_privhelper.observability_lifecycle import (  # noqa: E402
    boot_id_unchanged,
)


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {"kin_boot_id_unchanged": boot_id_unchanged}
