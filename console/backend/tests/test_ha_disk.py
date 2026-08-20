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
    elif sdb == "gpt_fresh":
        # GPT written by disk_prep; no mkfs yet (live mail.nisaroti after a
        # play that partitioned this host then failed on the peer).
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
                        "fstype": "",
                        "mountpoint": "",
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
        self.assertFalse(r["build_allowed"])
        self.assertEqual(r["plan_action"], "skip")
        self.assertTrue(any("Zimbra is on" in e for e in r["errors"]))
        self.assertTrue(
            any(
                "HA-RUNBOOK §13" in e and "migrate-zimbra-to-drbd-disk.sh" in e
                for e in r["errors"]
            )
        )

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
        self.assertTrue(r["build_allowed"])
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

    def test_asymmetric_gpt_plus_blank_peer_allows_disk_prep(self) -> None:
        """Live nisaroti: local already GPT, Zimbra still on root; peer still blank."""
        local = evaluate_node(
            lsblk=_lsblk(sdb="gpt_fresh"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="mail.nisaroti.my.id",
        )
        peer = evaluate_node(
            lsblk=_lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=True,
            label="mail2.nisaroti.my.id",
        )
        comb = combine_results(local, peer)
        self.assertFalse(local["ok"])
        self.assertFalse(local["build_allowed"])
        self.assertEqual(local["plan_action"], "skip")
        self.assertTrue(any("Zimbra is on" in e for e in local["errors"]))
        self.assertTrue(peer["can_auto_partition"])
        self.assertTrue(peer["build_allowed"])
        self.assertFalse(comb["ok"])
        self.assertTrue(comb["build_allowed"])
        self.assertEqual(len(comb["will_auto_partition"]), 1)
        self.assertEqual(comb["will_auto_partition"][0]["disk"], "/dev/sdb")

    def test_both_gpt_zimbra_on_root_blocks_drbd(self) -> None:
        """After both disks are GPT, Zimbra still on root must block Build HA."""
        local = evaluate_node(
            lsblk=_lsblk(sdb="gpt_fresh"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="mail.nisaroti.my.id",
        )
        peer = evaluate_node(
            lsblk=_lsblk(sdb="gpt_fresh"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=True,
            label="mail2.nisaroti.my.id",
        )
        comb = combine_results(local, peer)
        self.assertFalse(local["ok"])
        self.assertTrue(peer["ok"])
        self.assertEqual(comb["will_auto_partition"], [])
        self.assertFalse(comb["ok"])
        self.assertFalse(comb["build_allowed"])
        self.assertTrue(any("Zimbra is on" in e for e in comb["errors"]))

        peer_zimbra = evaluate_node(
            lsblk=_lsblk(sdb="gpt_fresh"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="mail2.nisaroti.my.id",
        )
        comb_both = combine_results(local, peer_zimbra)
        self.assertFalse(peer_zimbra["ok"])
        self.assertFalse(peer_zimbra["build_allowed"])
        self.assertEqual(comb_both["will_auto_partition"], [])
        self.assertFalse(comb_both["ok"])
        self.assertFalse(comb_both["build_allowed"])

    def test_both_ready_zimbra_on_data_allows_build(self) -> None:
        local = evaluate_node(
            lsblk=_lsblk(sdb="ready"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sdb1",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="a",
        )
        peer = evaluate_node(
            lsblk=_lsblk(sdb="ready"),
            root_source="/dev/sda2",
            zimbra_source="",
            zimbra_exists=False,
            require_zimbra_on_data=True,
            label="b",
        )
        comb = combine_results(local, peer)
        self.assertTrue(local["ok"])
        self.assertTrue(peer["ok"])
        self.assertEqual(comb["will_auto_partition"], [])
        self.assertTrue(comb["ok"])
        self.assertTrue(comb["build_allowed"])

    def test_pending_gpt_plus_inspect_failure_stays_blocked(self) -> None:
        """A blank local disk must not override a peer we could not inspect."""
        local = evaluate_node(
            lsblk=_lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            zimbra_source="/dev/sda2",
            zimbra_exists=True,
            require_zimbra_on_data=True,
            label="this server",
        )
        peer = {
            "ok": False,
            "label": "second server",
            "errors": ["second server: could not inspect disks (ssh exit 255)"],
            "seen": [],
            "build_allowed": False,
        }
        comb = combine_results(local, peer)
        self.assertTrue(local["can_auto_partition"])
        self.assertFalse(comb["ok"])
        self.assertFalse(comb["build_allowed"])

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
        self.assertNotIn("parted_argv", plan)

    def test_fresh_gpt_without_filesystem_skips_repartition(self) -> None:
        """disk_prep GPT with no mkfs must skip, not rewrite, on the next run."""
        plan = plan_auto_partition(
            _lsblk(sdb="gpt_fresh"),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(plan["action"], "skip")
        self.assertEqual(plan["disk"], "/dev/sdb")
        self.assertIn("already present", plan["message"])
        self.assertNotIn("parted_argv", plan)

    def test_asymmetric_pair_skip_plus_partition(self) -> None:
        """Next real run: already-GPT host skips; blank peer still partitions."""
        already = plan_auto_partition(
            _lsblk(sdb="gpt_fresh"),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        peer = plan_auto_partition(
            _lsblk(sdb="unpartitioned"),
            root_source="/dev/sda2",
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        self.assertEqual(already["action"], "skip")
        self.assertEqual(peer["action"], "partition")
        self.assertEqual(peer["disk"], "/dev/sdb")

    def test_selector_stdin_stays_quoted_oneline(self) -> None:
        """ansible-core 2.12 turns a >- folded to_json expr into str(dict)."""
        from pathlib import Path

        tasks = (
            Path(__file__).resolve().parents[3]
            / "ansible/roles/drbd_disk_prep/tasks/main.yml"
        )
        text = tasks.read_text()
        self.assertNotIn("stdin: >-", text)
        self.assertIn(
            'stdin: "{{ {\'lsblk\': drbd_disk_prep_lsblk.stdout | from_json, '
            "'root_source': drbd_disk_prep_root.stdout | trim, "
            "'data_disk': drbd_disk_prep_data, "
            "'meta_disk': drbd_disk_prep_meta} | to_json }}\"",
            text,
        )

    def test_selector_emits_json_even_on_bad_stdin(self) -> None:
        """Empty stdout means the process never ran, not a selector logic miss."""
        import subprocess
        import sys
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[3]
            / "ansible/roles/drbd_disk_prep/files/select_drbd_disk.py"
        )
        self.assertTrue(script.is_file(), msg=str(script))
        proc = subprocess.run(
            [sys.executable, str(script)],
            input="not-json",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertTrue(proc.stdout.strip(), msg=proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["action"], "fail")
        self.assertTrue(any("invalid JSON" in e for e in body["errors"]))

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
