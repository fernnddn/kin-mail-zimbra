"""Unit tests for DRBD disk preflight (no live lsblk / SSH)."""

from __future__ import annotations

import json
import unittest

from kin_privhelper.ha_disk import (
    DEFAULT_DATA_DISK,
    combine_results,
    evaluate_node,
    parse_remote_probe,
    plan_auto_partition,
)


def _lsblk(
    sda_root: bool = True,
    sdb: str | None = "unpartitioned",
    extra: list[dict] | None = None,
) -> dict:
    sda = {
        "name": "sda",
        "path": "/dev/sda",
        "type": "disk",
        "size": 32212254720,
        "pttype": "gpt",
        "children": [
            {
                "name": "sda2",
                "path": "/dev/sda2",
                "type": "part",
                "size": 30064771072,
                "mountpoint": "/",
            }
        ],
    }
    devices = [sda] if sda_root else []
    if sdb == "unpartitioned":
        devices.append(
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "type": "disk",
                "size": 107374182400,
                "pttype": None,
            }
        )
    elif sdb == "ready":
        devices.append(
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "type": "disk",
                "size": 107374182400,
                "pttype": "gpt",
                "children": [
                    {
                        "name": "sdb1",
                        "path": "/dev/sdb1",
                        "type": "part",
                        "size": 106954752000,
                        "fstype": "ext4",
                        "mountpoint": "/opt/zimbra",
                    },
                    {
                        "name": "sdb2",
                        "path": "/dev/sdb2",
                        "type": "part",
                        "size": 267386880,
                        "fstype": "",
                        "mountpoint": "",
                    },
                ],
            }
        )
    elif sdb == "tiny":
        devices.append(
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "type": "disk",
                "size": 1073741824,
                "pttype": "gpt",
                "children": [
                    {
                        "name": "sdb1",
                        "path": "/dev/sdb1",
                        "type": "part",
                        "size": 805306368,
                    },
                    {
                        "name": "sdb2",
                        "path": "/dev/sdb2",
                        "type": "part",
                        "size": 268435456,
                    },
                ],
            }
        )
    elif sdb == "tiny_blank":
        devices.append(
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "type": "disk",
                "size": 1073741824,
                "pttype": None,
            }
        )
    return {"blockdevices": devices + list(extra or [])}


class HaDiskTests(unittest.TestCase):
    def test_missing_second_disk(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb=None),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="this server",
        )
        self.assertFalse(r["ok"])
        self.assertFalse(r["build_allowed"])
        self.assertTrue(
            any(
                "no spare unpartitioned disk" in e or "second disk not found" in e
                for e in r["errors"]
            )
        )

    def test_unpartitioned_second_disk(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="this server",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(r["can_auto_partition"])
        self.assertTrue(r["build_allowed"])
        self.assertEqual(r["will_auto_partition"]["disk"], "/dev/sdb")
        self.assertTrue(any("Zimbra is on" in e for e in r["errors"]))

    def test_unpartitioned_peer_without_zimbra_is_build_allowed(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=True,
            label="peer",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(r["can_auto_partition"])
        self.assertTrue(r["build_allowed"])
        self.assertEqual(r["errors"], [])

    def test_zimbra_on_root_blocks_even_if_partitions_exist(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb="ready"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="this server",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("Zimbra is on" in e for e in r["errors"]))

    def test_ready_layout_passes(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb="ready"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sdb1",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="this server",
        )
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["ok"])
        self.assertEqual(r["data_disk"], DEFAULT_DATA_DISK)

    def test_peer_without_zimbra_passes_when_partitioned(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb="ready"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=True,
            label="peer",
        )
        self.assertTrue(r["ok"])

    def test_tiny_data_partition_fails(self) -> None:
        r = evaluate_node(
            lsblk=_lsblk(sdb="tiny"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=False,
            label="peer",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("below the" in e for e in r["errors"]))

    def test_combine_fails_closed(self) -> None:
        a = evaluate_node(
            lsblk=_lsblk(sdb="ready"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sdb1",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="a",
        )
        b = evaluate_node(
            lsblk=_lsblk(sdb=None),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=False,
            label="b",
        )
        comb = combine_results(a, b)
        self.assertTrue(a["ok"])
        self.assertFalse(comb["ok"])
        self.assertFalse(comb["build_allowed"])
        self.assertIn("fail-closed", comb["instructions"])

    def test_data_partition_on_os_disk_fails(self) -> None:
        lsblk = _lsblk(sdb=None)
        lsblk["blockdevices"][0]["children"].insert(
            0,
            {
                "name": "sda1",
                "path": "/dev/sda1",
                "type": "part",
                "size": 40 * 1024**3,
            },
        )
        r = evaluate_node(
            lsblk=lsblk,
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            data_disk="/dev/sda1",
            meta_disk="/dev/sda3",
            label="this server",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("OS disk" in e for e in r["errors"]))

    def test_parse_remote_probe(self) -> None:
        blob = (
            "---LSBLK---\n"
            + json.dumps(_lsblk(sdb="ready"))
            + "\n---ROOT---\n/dev/sda2\n---ZIMBRA---\n/dev/sdb1\n---ZIMBRA_DIR---\nyes\n"
        )
        facts = parse_remote_probe(blob)
        self.assertTrue(facts["lsblk_ok"])
        self.assertEqual(facts["root_source"], "/dev/sda2")
        self.assertEqual(facts["zimbra_source"], "/dev/sdb1")
        self.assertTrue(facts["zimbra_exists"])

    def test_zero_candidates_fail_closed(self) -> None:
        plan = plan_auto_partition(
            _lsblk(sdb=None),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("no spare unpartitioned disk" in e for e in plan["errors"]))

    def test_tiny_unpartitioned_is_not_a_candidate(self) -> None:
        plan = plan_auto_partition(
            _lsblk(sdb="tiny_blank"),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("no spare unpartitioned disk" in e for e in plan["errors"]))

    def test_multi_candidates_fail_closed(self) -> None:
        extra = [
            {
                "name": "sdc",
                "path": "/dev/sdc",
                "type": "disk",
                "size": 107374182400,
                "pttype": None,
            }
        ]
        plan = plan_auto_partition(
            _lsblk(sdb="unpartitioned", extra=extra),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("more than one spare" in e for e in plan["errors"]))
        self.assertIn("/dev/sdb", plan["errors"][0])
        self.assertIn("/dev/sdc", plan["errors"][0])

    def test_single_candidate_plans_partition(self) -> None:
        plan = plan_auto_partition(
            _lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "partition")
        self.assertEqual(plan["disk"], "/dev/sdb")
        self.assertEqual(plan["data_disk"], "/dev/sdb1")
        self.assertEqual(plan["meta_disk"], "/dev/sdb2")
        self.assertIn("mklabel", plan["parted_argv"])
        self.assertIn("Selected /dev/sdb", plan["message"])

    def test_ready_layout_skips_partition(self) -> None:
        plan = plan_auto_partition(
            _lsblk(sdb="ready"),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "skip")
        self.assertIn("already present", plan["message"])

    def test_mounted_spare_is_not_a_candidate(self) -> None:
        lsblk = _lsblk(sdb="unpartitioned")
        lsblk["blockdevices"][1]["mountpoint"] = "/mnt/data"
        plan = plan_auto_partition(
            lsblk,
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("no spare unpartitioned disk" in e for e in plan["errors"]))

    def test_existing_table_is_not_rewritten(self) -> None:
        lsblk = _lsblk(sdb="unpartitioned")
        lsblk["blockdevices"][1]["pttype"] = "gpt"
        plan = plan_auto_partition(
            lsblk,
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(any("partition table" in s["reason"] for s in plan["skipped"]))


if __name__ == "__main__":
    unittest.main()
