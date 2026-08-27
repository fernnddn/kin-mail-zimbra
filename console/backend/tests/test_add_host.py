"""Unit tests for console add-host planning and inventory (no live cluster)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kin_privhelper.add_host import (
    attach_peer_eligible,
    clear_last_removed_peer,
    plan_add_host,
    read_last_removed_peer,
    render_add_host_inventory,
    write_last_removed_peer,
)


class AttachPeerEligibleTests(unittest.TestCase):
    def test_fresh_single_deploy_stub_not_eligible(self) -> None:
        self.assertFalse(
            attach_peer_eligible(
                topology="1vm",
                live_nodes=["mail.example.test"],
                vip_ip="",
                vip_node=None,
                promoted=None,
                package_stub=True,
            )
        )

    def test_fresh_single_without_vip_not_eligible(self) -> None:
        self.assertFalse(
            attach_peer_eligible(
                topology="1vm",
                live_nodes=["mail.example.test"],
                vip_ip="",
                vip_node=None,
                promoted=None,
                package_stub=False,
            )
        )

    def test_survivor_after_remove_eligible(self) -> None:
        self.assertTrue(
            attach_peer_eligible(
                topology="1vm",
                live_nodes=["mail.example.test"],
                vip_ip="192.0.2.16",
                vip_node="mail.example.test",
                promoted="mail.example.test",
                package_stub=False,
            )
        )

    def test_two_nodes_finish_eligible(self) -> None:
        """Ansible joined peer but survivor still 1vm - finish peer console."""
        self.assertTrue(
            attach_peer_eligible(
                topology="1vm",
                live_nodes=["mail.example.test", "mail2.example.test"],
                vip_ip="192.0.2.16",
                vip_node="mail.example.test",
                promoted="mail.example.test",
                package_stub=False,
            )
        )
        from kin_privhelper.add_host import attach_finish_only

        self.assertTrue(
            attach_finish_only(["mail.example.test", "mail2.example.test"])
        )
        self.assertFalse(attach_finish_only(["mail.example.test"]))

    def test_three_nodes_not_eligible(self) -> None:
        self.assertFalse(
            attach_peer_eligible(
                topology="1vm",
                live_nodes=["a", "b", "c"],
                vip_ip="192.0.2.16",
                vip_node="a",
                promoted="a",
                package_stub=False,
            )
        )


class LastRemovedPeerTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-removed-peer"
            write_last_removed_peer("mail2.example.test", "192.0.2.14", path=path)
            got = read_last_removed_peer(path=path)
            self.assertEqual(got["name"], "mail2.example.test")
            self.assertEqual(got["ip"], "192.0.2.14")
            clear_last_removed_peer(path=path)
            self.assertEqual(read_last_removed_peer(path=path), {})


class PlanAddHostTests(unittest.TestCase):
    def _ok(self, **overrides: object):
        base = dict(
            local_host="mail.example.test",
            local_ip="192.0.2.15",
            new_name="mail3.example.test",
            new_ip="192.0.2.17",
            retired_name="mail2.example.test",
            retired_ip="192.0.2.14",
            obs_name="mon.example.test",
            obs_ip="192.0.2.12",
            vip_ip="192.0.2.16",
            live_nodes=["mail.example.test"],
            promoted="mail.example.test",
            standby=[],
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        base.update(overrides)
        return plan_add_host(**base)  # type: ignore[arg-type]

    def test_happy_path(self) -> None:
        plan = self._ok()
        self.assertEqual(plan.errors, ())
        self.assertEqual(plan.new_name, "mail3.example.test")

    def test_finish_path_two_live_nodes(self) -> None:
        plan = self._ok(
            new_name="mail2.example.test",
            new_ip="192.0.2.14",
            retired_name="mail-old.example.test",
            retired_ip="192.0.2.99",
            live_nodes=["mail.example.test", "mail2.example.test"],
        )
        self.assertEqual(plan.errors, ())
        self.assertTrue(any("skipping mail-add-host" in n for n in plan.notes))

    def test_finish_path_refuses_unknown_peer(self) -> None:
        plan = self._ok(
            retired_name="mail-old.example.test",
            retired_ip="192.0.2.99",
            live_nodes=["mail.example.test", "mail2.example.test"],
        )
        self.assertTrue(any("not in the Pacemaker nodelist" in e for e in plan.errors))

    def test_refuse_new_equals_vip(self) -> None:
        plan = self._ok(new_ip="192.0.2.16")
        self.assertTrue(any("VIP" in e for e in plan.errors))

    def test_refuse_missing_retired(self) -> None:
        plan = self._ok(retired_name="")
        self.assertTrue(any("previous peer" in e for e in plan.errors))

    def test_refuse_when_not_promoted_here(self) -> None:
        plan = self._ok(promoted="mail2.example.test")
        self.assertTrue(any("Promoted is" in e for e in plan.errors))


class InventoryTests(unittest.TestCase):
    def test_inventory_has_groups_and_no_plaintext_secret(self) -> None:
        plan = plan_add_host(
            local_host="mail.example.test",
            local_ip="192.0.2.15",
            new_name="mail3.example.test",
            new_ip="192.0.2.17",
            retired_name="mail2.example.test",
            retired_ip="192.0.2.14",
            obs_name="mon.example.test",
            obs_ip="192.0.2.12",
            vip_ip="192.0.2.16",
            live_nodes=["mail.example.test"],
            promoted="mail.example.test",
            standby=[],
            data_disk="/dev/sdb1",
            meta_disk="/dev/sdb2",
        )
        inv = render_add_host_inventory(plan)
        self.assertIn("mail_survivor:", inv)
        self.assertIn("mail_new_node:", inv)
        self.assertIn("cluster_add_host_new_name: mail3.example.test", inv)
        self.assertIn("KIN_HACLUSTER_PASSWORD", inv)
        self.assertIn("KIN_CHAP_PASSWORD", inv)
        self.assertNotIn("gits-it", inv)
        self.assertIn("iqn.2026-08.example.test:", inv)


class RbacAddHostTests(unittest.TestCase):
    def test_ops_only(self) -> None:
        from kin_privhelper.rbac import (
            ROLE_CUSTOMER_ADMIN,
            ROLE_SUPER_ADMIN,
            ROLE_SUPPORT_OPS,
            command_allowed,
        )

        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "add_host"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "add_host"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "add_host"))


if __name__ == "__main__":
    unittest.main()
