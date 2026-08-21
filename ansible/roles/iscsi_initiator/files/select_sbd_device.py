#!/usr/bin/env python3
"""Pick the SBD iSCSI LUN by-id path from udev links.

Stdlib only. Runs on mail VMs via the iscsi_initiator role after login.
Never returns a local ATA/NVMe disk. The target IQN must appear in a
by-path symlink that resolves to the same kernel node as exactly one
preferred scsi-* by-id (not scsi-0ATA).

CLI: --iqn IQN [--disk-root /dev/disk] -> JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _links(dirpath: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not dirpath.is_dir():
        return out
    for entry in dirpath.iterdir():
        if not entry.is_symlink():
            continue
        try:
            out[entry.name] = os.path.realpath(entry)
        except OSError:
            continue
    return out


def _preferred_scsi_ids(names: list[str]) -> list[str]:
    preferred: list[str] = []
    fallback: list[str] = []
    for name in names:
        if name.startswith("scsi-0") or name.startswith("scsi-1ATA"):
            continue
        if name.startswith("scsi-3") or name.startswith("scsi-36"):
            preferred.append(name)
        elif name.startswith("scsi-"):
            fallback.append(name)
    return preferred or fallback


def select_sbd_device(
    *,
    target_iqn: str,
    disk_root: str = "/dev/disk",
) -> dict[str, Any]:
    iqn = (target_iqn or "").strip()
    if not iqn or "iqn." not in iqn:
        return {"action": "fail", "errors": ["target IQN is missing or not an iqn."]}

    root = Path(disk_root)
    by_path = _links(root / "by-path")
    by_id = _links(root / "by-id")
    marker = f"-iscsi-{iqn}-lun-"
    kernels = sorted(
        {
            kernel
            for name, kernel in by_path.items()
            if marker in name and kernel
        }
    )
    if not kernels:
        return {
            "action": "fail",
            "errors": [
                f"no /dev/disk/by-path link contains {marker}. "
                "Login to the SBD target first, then retry."
            ],
        }
    if len(kernels) > 1:
        return {
            "action": "fail",
            "errors": [
                "SBD IQN matched more than one kernel disk ("
                + ", ".join(kernels)
                + "). Set iscsi_initiator_sbd_by_id explicitly."
            ],
        }
    kernel = kernels[0]
    scsi_names = _preferred_scsi_ids(
        [name for name, target in by_id.items() if target == kernel]
    )
    if not scsi_names:
        return {
            "action": "fail",
            "errors": [
                f"iSCSI LUN is {kernel} but no scsi-* by-id link points at it "
                "(ignored scsi-0ATA). Set iscsi_initiator_sbd_by_id explicitly."
            ],
        }
    if len(scsi_names) > 1:
        return {
            "action": "fail",
            "errors": [
                "multiple scsi-* by-id names for "
                + kernel
                + " ("
                + ", ".join(sorted(scsi_names))
                + "). Set iscsi_initiator_sbd_by_id explicitly."
            ],
        }
    name = scsi_names[0]
    return {
        "action": "ok",
        "device": f"/dev/disk/by-id/{name}",
        "kernel": kernel,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iqn", required=True)
    parser.add_argument("--disk-root", default="/dev/disk")
    args = parser.parse_args()
    try:
        plan = select_sbd_device(target_iqn=args.iqn, disk_root=args.disk_root)
    except Exception as exc:  # noqa: BLE001
        plan = {"action": "fail", "errors": [str(exc)]}
    sys.stdout.write(json.dumps(plan) + "\n")
    return 0 if plan.get("action") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
