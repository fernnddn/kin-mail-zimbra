"""Remove-host planner and inventory (no live Pacemaker / SSH)."""

from __future__ import annotations

import unittest

from kin_privhelper.orchestration import OrchHost
from kin_privhelper.remove_host import (
    names_match,
    plan_remove_host,
    render_remove_host_inventory,
)


LOCAL = "mail.example.test"
PEER = "mail2.example.test"


class NamesMatchTests(unittest.TestCase):
    def test_short_and_fqdn(self) -> None:
        self.assertTrue(names_match("mail.example.test", "mail"))
        self.assertTrue(names_match(LOCAL, LOCAL))
        self.assertFalse(names_match(LOCAL, PEER))


class PlanRemoveHostTests(unittest.TestCase):
    def test_graceful_when_target_answers_ssh(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=PEER,
            members=[LOCAL, PEER],
            offline=[],
            stale_peers=[],
            known_peer=PEER,
            promoted=LOCAL,
            target_ssh_ok=True,
            survivor_ssh_ok=True,
        )
        self.assertEqual(plan.mode, "graceful")
        self.assertEqual(plan.survivor, LOCAL)
        self.assertTrue(plan.local_is_survivor)
        self.assertEqual(plan.errors, ())

    def test_forced_when_target_unreachable(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=PEER,
            members=[LOCAL],
            offline=[PEER],
            stale_peers=[],
            known_peer=PEER,
            promoted=LOCAL,
            target_ssh_ok=False,
            survivor_ssh_ok=True,
        )
        self.assertEqual(plan.mode, "forced")
        self.assertEqual(plan.survivor, LOCAL)
        self.assertEqual(plan.errors, ())
        self.assertTrue(any("forced path" in n for n in plan.notes))

    def test_forced_stale_corosync_peer(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=PEER,
            members=[LOCAL],
            offline=[],
            stale_peers=[PEER],
            known_peer=PEER,
            promoted=LOCAL,
            target_ssh_ok=False,
            survivor_ssh_ok=True,
        )
        self.assertEqual(plan.mode, "forced")
        self.assertEqual(plan.errors, ())

    def test_refuse_forced_remove_of_promoted_when_local_is_not(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=PEER,
            members=[LOCAL, PEER],
            offline=[],
            stale_peers=[],
            known_peer=PEER,
            promoted=PEER,
            target_ssh_ok=False,
            survivor_ssh_ok=True,
        )
        self.assertTrue(plan.errors)
        self.assertTrue(any("ambiguous" in e for e in plan.errors))

    def test_refuse_removing_self_when_survivor_unreachable(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=LOCAL,
            members=[LOCAL, PEER],
            offline=[],
            stale_peers=[],
            known_peer=PEER,
            promoted=PEER,
            target_ssh_ok=True,
            survivor_ssh_ok=False,
        )
        self.assertTrue(plan.errors)
        self.assertTrue(any("survivor does not answer SSH" in e for e in plan.errors))

    def test_refuse_last_node(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=LOCAL,
            members=[LOCAL],
            offline=[],
            stale_peers=[],
            known_peer="",
            promoted=LOCAL,
            target_ssh_ok=True,
            survivor_ssh_ok=True,
        )
        self.assertTrue(any("last remaining" in e for e in plan.errors))


class RemoveHostInventoryTests(unittest.TestCase):
    def test_inventory_uses_lookups_and_rfc5737(self) -> None:
        inv = render_remove_host_inventory(
            survivor=OrchHost(LOCAL, "192.0.2.15", "mail"),
            retired=OrchHost(PEER, "192.0.2.14", "mail2"),
            monitoring=OrchHost("mon.example.test", "192.0.2.12", "mon"),
            local_connection=True,
        )
        self.assertIn("lookup", inv)
        self.assertIn("KIN_ANSIBLE_PASSWORD", inv)
        self.assertIn("192.0.2.15", inv)
        self.assertIn("192.0.2.14", inv)
        self.assertIn("192.0.2.12", inv)
        self.assertIn("mail_survivor:", inv)
        self.assertIn("ansible_connection: local", inv)
        self.assertIn(f"cluster_remove_host_retired_name: {PEER}", inv)
        self.assertNotIn("ansible_password: 'secret", inv)
        self.assertNotIn("s3cret", inv)

    def test_remote_survivor_uses_ssh(self) -> None:
        inv = render_remove_host_inventory(
            survivor=OrchHost(PEER, "192.0.2.14", "mail2"),
            retired=OrchHost(LOCAL, "192.0.2.15", "mail"),
            monitoring=OrchHost("mon.example.test", "192.0.2.12", "mon"),
            local_connection=False,
        )
        self.assertIn("ansible_connection: ssh", inv)


if __name__ == "__main__":
    unittest.main()
