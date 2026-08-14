"""Unit tests for DRBD disk preflight (no live lsblk / SSH)."""

from __future__ import annotations

import json
import unittest

from kin_privhelper.ha_disk import (
    DEFAULT_DATA_DISK,
    combine_results,
    evaluate_node,
    parse_remote_probe,
)


def _lsblk(sda_root: bool = True, sdb: str | None = "unpartitioned") -> dict:
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
    return {"blockdevices": devices}


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
        self.assertTrue(any("second disk not found" in e for e in r["errors"]))

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
        self.assertTrue(any("not partitioned" in e for e in r["errors"]))

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
            lsblk=_lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=False,
            label="b",
        )
        comb = combine_results(a, b)
        self.assertTrue(a["ok"])
        self.assertFalse(comb["ok"])
        self.assertIn("will not partition", comb["instructions"])

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


if __name__ == "__main__":
    unittest.main()
