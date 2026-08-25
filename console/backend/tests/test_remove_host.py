"""Remove-host planner and inventory (no live Pacemaker / SSH)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kin_privhelper import apply_config as ac
from kin_privhelper import deploy_state as ds
from kin_privhelper.orchestration import OrchHost
from kin_privhelper.remove_host import (
    RemovePlan,
    _demote_survivor_topology_to_1vm,
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


class DemoteSurvivorTopologyTests(unittest.IsolatedAsyncioTestCase):
    """The critical piece the platform bug audit flagged: a successful
    remove-host used to leave the survivor at TOPOLOGY=2vm with a stale
    PEER_HOST_IP/NAME pointing at the retired node.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self._old_conf_dir = ac.CONF_DIR
        self._old_conf_file = ac.CONF_FILE
        self._old_topo_marker = ds.TOPOLOGY_MARKER
        ac.CONF_DIR = root / "kin-mail"
        ac.CONF_FILE = ac.CONF_DIR / "config"
        ds.TOPOLOGY_MARKER = root / "kin-mail" / "topology"
        # privhelperd always runs as root in production; this sandbox does not.
        chown_patch = patch.object(ac.os, "chown")
        self.addCleanup(chown_patch.stop)
        chown_patch.start()

    def tearDown(self) -> None:
        ac.CONF_DIR = self._old_conf_dir
        ac.CONF_FILE = self._old_conf_file
        ds.TOPOLOGY_MARKER = self._old_topo_marker

    async def test_local_survivor_demotes_config_and_topology_marker(self) -> None:
        ac.write_config_file(
            {
                "TOPOLOGY": "2vm",
                "PEER_HOST_IP": "192.0.2.14",
                "PEER_HOST_NAME": PEER,
                "OBSERVABILITY_VM_IP": "192.0.2.53",
                "CLUSTER_VIP_IP": "192.0.2.16",
                "MAIL_HOST": LOCAL,
            }
        )
        plan = RemovePlan(
            mode="graceful",
            target=PEER,
            survivor=LOCAL,
            local_is_survivor=True,
            local_is_target=False,
            errors=(),
            notes=(),
        )
        notes = await _demote_survivor_topology_to_1vm(
            plan=plan,
            survivor_host=OrchHost(LOCAL, "192.0.2.15", "mail"),
            ssh_user="kin",
            ssh_pass="unused",
            secrets=[],
        )
        self.assertTrue(any("Demoted local" in n for n in notes))
        body = ac.CONF_FILE.read_text(encoding="utf-8")
        self.assertIn('TOPOLOGY="1vm"', body)
        self.assertIn('PEER_HOST_IP=""', body)
        self.assertIn('PEER_HOST_NAME=""', body)
        self.assertEqual(ds.TOPOLOGY_MARKER.read_text(encoding="utf-8").strip(), "1vm")

    async def test_local_survivor_already_1vm_is_noop_but_still_returns(self) -> None:
        ac.write_config_file({"TOPOLOGY": "1vm", "MAIL_HOST": LOCAL})
        plan = RemovePlan(
            mode="graceful",
            target=PEER,
            survivor=LOCAL,
            local_is_survivor=True,
            local_is_target=False,
            errors=(),
            notes=(),
        )
        notes = await _demote_survivor_topology_to_1vm(
            plan=plan,
            survivor_host=OrchHost(LOCAL, "192.0.2.15", "mail"),
            ssh_user="kin",
            ssh_pass="unused",
            secrets=[],
        )
        self.assertTrue(any("Demoted local" in n for n in notes))
        self.assertEqual(ds.TOPOLOGY_MARKER.read_text(encoding="utf-8").strip(), "1vm")


if __name__ == "__main__":
    unittest.main()
