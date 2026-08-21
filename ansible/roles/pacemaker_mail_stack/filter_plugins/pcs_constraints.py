"""Ansible filter wrapping kin_privhelper.pcs_constraints."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_backend() -> None:
    try:
        import kin_privhelper.pcs_constraints  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    tree_root = here.parents[4]
    for candidate in (tree_root / "console" / "backend", tree_root / "backend"):
        if (candidate / "kin_privhelper" / "pcs_constraints.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_backend()
from kin_privhelper.pcs_constraints import (  # noqa: E402
    listing_has_promote_then_start,
    listing_has_promoted_colocation,
)


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "kin_pcs_has_promoted_colocation": listing_has_promoted_colocation,
            "kin_pcs_has_promote_then_start": listing_has_promote_then_start,
        }
