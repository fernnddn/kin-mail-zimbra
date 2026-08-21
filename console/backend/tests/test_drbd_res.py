"""Parse live kin-zimbra.res disk paths (no live drbdadm)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.drbd_res import (
    node_device,
    node_disk,
    node_meta_disk,
    parse_first_meta_disk,
    prefer_live_path,
)

SURVIVOR = "mail.example.test"
RETIRED = "mail2.example.test"

LIVE_PAIR = """
resource kin-zimbra {
  protocol C;
  on mail.example.test {
    device    /dev/drbd0;
    disk      /dev/sdb1;
    address   192.0.2.15:7788;
    meta-disk /dev/sdb2;
  }
  on mail2.example.test {
    device    /dev/drbd0;
    disk      /dev/sdb1;
    address   192.0.2.14:7788;
    meta-disk /dev/sdb2;
  }
}
"""

LOOP_PAIR = """
resource kin-zimbra {
  on mail.example.test {
    device    /dev/drbd0;
    disk      /dev/sdb1;
    address   192.0.2.15:7788;
    meta-disk /dev/loop21;
  }
}
"""

REPO_ROOT = Path(__file__).resolve().parents[3]
REMOVE_HOST = REPO_ROOT / "ansible" / "roles" / "cluster_remove_host"


class DrbdResParseTests(unittest.TestCase):
    def test_rebuild_pair_reads_sdb2_not_loop21(self) -> None:
        self.assertEqual(node_disk(LIVE_PAIR, SURVIVOR), "/dev/sdb1")
        self.assertEqual(node_meta_disk(LIVE_PAIR, SURVIVOR), "/dev/sdb2")
        self.assertEqual(node_device(LIVE_PAIR, SURVIVOR), "/dev/drbd0")
        self.assertEqual(parse_first_meta_disk(LIVE_PAIR), "/dev/sdb2")
        self.assertEqual(node_disk(LIVE_PAIR, RETIRED), "/dev/sdb1")

    def test_live_path_wins_over_loop21_inventory(self) -> None:
        self.assertEqual(prefer_live_path("/dev/sdb2", "/dev/loop21"), "/dev/sdb2")
        self.assertEqual(prefer_live_path("", "/dev/sdb2"), "/dev/sdb2")
        self.assertEqual(prefer_live_path("", ""), "")

    def test_old_lab_loop_is_still_readable(self) -> None:
        self.assertEqual(node_meta_disk(LOOP_PAIR, SURVIVOR), "/dev/loop21")

    def test_unknown_node_is_empty(self) -> None:
        self.assertEqual(node_meta_disk(LIVE_PAIR, "other.example.test"), "")

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = REMOVE_HOST / "filter_plugins" / "drbd_res.py"
        spec = importlib.util.spec_from_file_location("ansible_drbd_res_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.node_meta_disk, node_meta_disk)
        filters = mod.FilterModule().filters()
        self.assertEqual(filters["kin_drbd_node_meta_disk"](LIVE_PAIR, SURVIVOR), "/dev/sdb2")
        self.assertEqual(
            filters["kin_drbd_prefer_live_path"]("/dev/sdb2", "/dev/loop21"),
            "/dev/sdb2",
        )

    def test_role_reads_live_file_and_refuses_loop_default(self) -> None:
        drop = (REMOVE_HOST / "tasks" / "drbd_drop_peer.yml").read_text(encoding="utf-8")
        defaults = (REMOVE_HOST / "defaults" / "main.yml").read_text(encoding="utf-8")
        self.assertIn("kin_drbd_node_meta_disk", drop)
        self.assertIn("kin_drbd_prefer_live_path", drop)
        self.assertIn("cluster_remove_host_res_text", drop)
        self.assertNotIn("default('/dev/loop21')", defaults)
        self.assertNotIn("cluster_remove_host_res_grep", drop)
        self.assertIn(
            "cluster_remove_host_retired_address in cluster_remove_host_res_text",
            drop,
        )


if __name__ == "__main__":
    unittest.main()
