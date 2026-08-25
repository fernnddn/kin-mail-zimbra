"""SBD iSCSI LUN by-id selector (no live iscsiadm)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _load():
    path = (
        Path(__file__).resolve().parents[3]
        / "ansible"
        / "roles"
        / "iscsi_initiator"
        / "files"
        / "select_sbd_device.py"
    )
    spec = importlib.util.spec_from_file_location("select_sbd_device", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


IQN = "iqn.2026-08.example.test:kin-mail-sbd"
PATH_NAME = f"ip-192.0.2.12:3260-iscsi-{IQN}-lun-0"
SCSI_ID = "scsi-36001405794acfc6ef564471952d8017b"


class SelectSbdDeviceTests(unittest.TestCase):
    def _tree(self, links: dict[str, dict[str, str]]) -> Path:
        root = Path(self._tmpdir.name)
        for child in root.iterdir():
            if child.is_dir():
                for leftover in child.iterdir():
                    leftover.unlink()
                child.rmdir()
        nodes = root / "dev"
        nodes.mkdir()
        for kind, mapping in links.items():
            d = root / kind
            d.mkdir()
            for name, kernel in mapping.items():
                target = nodes / kernel
                if not target.exists():
                    target.write_bytes(b"")
                (d / name).symlink_to(target)
        return root

    def setUp(self) -> None:
        self.mod, self.script = _load()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="kin-sbd-")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_picks_scsi_by_id_for_the_iqn(self) -> None:
        root = self._tree(
            {
                "by-path": {PATH_NAME: "sdc"},
                "by-id": {
                    SCSI_ID: "sdc",
                    "scsi-0ATA_OSDISK": "sda",
                    "wwn-0x5000": "sda",
                },
            }
        )
        plan = self.mod.select_sbd_device(target_iqn=IQN, disk_root=str(root))
        self.assertEqual(plan["action"], "ok")
        self.assertEqual(plan["device"], f"/dev/disk/by-id/{SCSI_ID}")
        self.assertTrue(plan["kernel"].endswith("/sdc"))

    def test_ignores_scsi_0ata_on_the_lun(self) -> None:
        root = self._tree(
            {
                "by-path": {PATH_NAME: "sdc"},
                "by-id": {"scsi-0ATA_LUN": "sdc"},
            }
        )
        plan = self.mod.select_sbd_device(target_iqn=IQN, disk_root=str(root))
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("scsi-0ATA" in e for e in plan["errors"]))

    def test_refuses_two_kernel_disks(self) -> None:
        root = self._tree(
            {
                "by-path": {
                    PATH_NAME: "sdc",
                    f"ip-192.0.2.12:3260-iscsi-{IQN}-lun-1": "sdd",
                },
                "by-id": {SCSI_ID: "sdc", "scsi-3600other": "sdd"},
            }
        )
        plan = self.mod.select_sbd_device(target_iqn=IQN, disk_root=str(root))
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("more than one kernel" in e for e in plan["errors"]))

    def test_two_portals_same_lun_are_ok(self) -> None:
        root = self._tree(
            {
                "by-path": {
                    PATH_NAME: "sdc",
                    f"ip-192.0.2.12:3261-iscsi-{IQN}-lun-0": "sdc",
                },
                "by-id": {SCSI_ID: "sdc"},
            }
        )
        plan = self.mod.select_sbd_device(target_iqn=IQN, disk_root=str(root))
        self.assertEqual(plan["action"], "ok")
        self.assertEqual(plan["device"], f"/dev/disk/by-id/{SCSI_ID}")

    def test_missing_iqn_fails(self) -> None:
        root = self._tree({"by-path": {}, "by-id": {}})
        plan = self.mod.select_sbd_device(target_iqn=IQN, disk_root=str(root))
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("by-path" in e for e in plan["errors"]))

    def test_cli_emits_json_on_failure(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(self.script), "--iqn", IQN, "--disk-root", "/tmp"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        body = json.loads(proc.stdout)
        self.assertEqual(body["action"], "fail")
        self.assertTrue(body["errors"])


if __name__ == "__main__":
    unittest.main()
