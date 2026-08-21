"""Ansible filter wrapping kin_privhelper.sbd_activation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_backend() -> None:
    try:
        import kin_privhelper.sbd_activation  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    tree_root = here.parents[4]
    for candidate in (tree_root / "console" / "backend", tree_root / "backend"):
        if (candidate / "kin_privhelper" / "sbd_activation.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_backend()
from kin_privhelper.sbd_activation import (  # noqa: E402
    fence_sbd_power_timeout_ok,
    pcs_status_shows_local_mail_busy,
    sbd_activation_plan,
    sbd_device_needs_create,
    sbd_fresh_lun_plan,
    stonith_config_has_attr,
)


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "kin_sbd_activation_plan": (
                lambda sbd_active, local_busy: sbd_activation_plan(
                    sbd_active=bool(sbd_active),
                    local_mail_busy=bool(local_busy),
                )
            ),
            "kin_pcs_status_local_mail_busy": pcs_status_shows_local_mail_busy,
            "kin_fence_sbd_power_timeout_ok": fence_sbd_power_timeout_ok,
            "kin_stonith_config_has_attr": stonith_config_has_attr,
            "kin_sbd_device_needs_create": sbd_device_needs_create,
            "kin_sbd_fresh_lun_plan": (
                lambda dump_rc, expect_fresh: sbd_fresh_lun_plan(
                    dump_rc=dump_rc,
                    expect_fresh=bool(expect_fresh),
                )
            ),
        }
