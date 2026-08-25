"""Remove-host planner and inventory (no live Pacemaker / SSH)."""

from __future__ import annotations

import json
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
    _unstandby_best_effort,
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

    def test_refuse_removing_self_always(self) -> None:
        plan = plan_remove_host(
            local_host=LOCAL,
            target=LOCAL,
            members=[LOCAL, PEER],
            offline=[],
            stale_peers=[],
            known_peer=PEER,
            promoted=LOCAL,
            target_ssh_ok=True,
            survivor_ssh_ok=True,
        )
        self.assertTrue(plan.local_is_target)
        self.assertTrue(plan.errors)
        self.assertTrue(any("serving this console" in e for e in plan.errors))

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
        self._old_ha_marker = ds.HA_SETUP_COMPLETE_MARKER
        self._old_draft = ds.WIZARD_DRAFT_FILE
        ac.CONF_DIR = root / "kin-mail"
        ac.CONF_FILE = ac.CONF_DIR / "config"
        ds.TOPOLOGY_MARKER = root / "kin-mail" / "topology"
        ds.HA_SETUP_COMPLETE_MARKER = root / "kin-mail" / "ha-setup-complete"
        ds.WIZARD_DRAFT_FILE = root / "wizard-draft.json"
        # privhelperd always runs as root in production; this sandbox does not.
        chown_patch = patch.object(ac.os, "chown")
        self.addCleanup(chown_patch.stop)
        chown_patch.start()

    def tearDown(self) -> None:
        ac.CONF_DIR = self._old_conf_dir
        ac.CONF_FILE = self._old_conf_file
        ds.TOPOLOGY_MARKER = self._old_topo_marker
        ds.HA_SETUP_COMPLETE_MARKER = self._old_ha_marker
        ds.WIZARD_DRAFT_FILE = self._old_draft

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
        ds.WIZARD_DRAFT_FILE.write_text(
            json.dumps(
                {
                    "topology": "2vm",
                    "peer_host_ip": "192.0.2.14",
                    "peer_host_name": PEER,
                    "observability_vm_ip": "192.0.2.53",
                    "cluster_vip_ip": "192.0.2.16",
                }
            ),
            encoding="utf-8",
        )
        ds.HA_SETUP_COMPLETE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        ds.HA_SETUP_COMPLETE_MARKER.write_text("pair\n", encoding="utf-8")
        plan = RemovePlan(
            mode="graceful",
            target=PEER,
            survivor=LOCAL,
            local_is_survivor=True,
            local_is_target=False,
            errors=(),
            notes=(),
        )
        ok, notes = await _demote_survivor_topology_to_1vm(
            plan=plan,
            survivor_host=OrchHost(LOCAL, "192.0.2.15", "mail"),
            ssh_user="kin",
            ssh_pass="unused",
            secrets=[],
        )
        self.assertTrue(ok)
        self.assertTrue(any("Demoted local" in n for n in notes))
        self.assertTrue(any("wizard draft" in n for n in notes))
        body = ac.CONF_FILE.read_text(encoding="utf-8")
        self.assertIn('TOPOLOGY="1vm"', body)
        self.assertIn('PEER_HOST_IP=""', body)
        self.assertIn('PEER_HOST_NAME=""', body)
        self.assertIn('CLUSTER_VIP_IP="192.0.2.16"', body)
        self.assertIn('OBSERVABILITY_VM_IP="192.0.2.53"', body)
        self.assertEqual(ds.TOPOLOGY_MARKER.read_text(encoding="utf-8").strip(), "1vm")
        self.assertFalse(ds.HA_SETUP_COMPLETE_MARKER.is_file())
        draft = json.loads(ds.WIZARD_DRAFT_FILE.read_text(encoding="utf-8"))
        self.assertEqual(draft.get("topology"), "1vm")
        self.assertEqual(draft.get("peer_host_ip"), "")
        self.assertEqual(draft.get("peer_host_name"), "")
        self.assertEqual(draft.get("cluster_vip_ip"), "192.0.2.16")
        self.assertEqual(draft.get("observability_vm_ip"), "192.0.2.53")

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
        ok, notes = await _demote_survivor_topology_to_1vm(
            plan=plan,
            survivor_host=OrchHost(LOCAL, "192.0.2.15", "mail"),
            ssh_user="kin",
            ssh_pass="unused",
            secrets=[],
        )
        self.assertTrue(ok)
        self.assertTrue(any("Demoted local" in n for n in notes))
        self.assertEqual(ds.TOPOLOGY_MARKER.read_text(encoding="utf-8").strip(), "1vm")

    async def test_local_survivor_read_failure_reports_not_ok(self) -> None:
        # A real unreadable file (0o000) - read must fail cleanly and the
        # caller must be told False, not just handed an empty notes list.
        ac.write_config_file({"TOPOLOGY": "2vm", "PEER_HOST_IP": "192.0.2.14"})
        ac.CONF_FILE.chmod(0o000)
        self.addCleanup(ac.CONF_FILE.chmod, 0o600)
        plan = RemovePlan(
            mode="graceful",
            target=PEER,
            survivor=LOCAL,
            local_is_survivor=True,
            local_is_target=False,
            errors=(),
            notes=(),
        )
        ok, notes = await _demote_survivor_topology_to_1vm(
            plan=plan,
            survivor_host=OrchHost(LOCAL, "192.0.2.15", "mail"),
            ssh_user="kin",
            ssh_pass="unused",
            secrets=[],
        )
        self.assertFalse(ok)
        self.assertTrue(any("could not read local config" in n for n in notes))


class UnstandbyBestEffortTests(unittest.IsolatedAsyncioTestCase):
    """A failed remove-host after `pcs node standby` used to leave the target
    stuck in Standby forever - no rollback existed (re-audit, 25 Aug 2026).
    """

    async def test_success_reports_rolled_back(self) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "kin_privhelper.maintenance._capture",
            new=AsyncMock(return_value=(0, "", "")),
        ) as capture:
            note = await _unstandby_best_effort(PEER)
        self.assertIn("Rolled back", note)
        capture.assert_awaited_once()
        self.assertEqual(capture.await_args.args[0], ["pcs", "node", "unstandby", PEER])

    async def test_failure_warns_instead_of_raising(self) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "kin_privhelper.maintenance._capture",
            new=AsyncMock(return_value=(1, "", "node busy")),
        ):
            note = await _unstandby_best_effort(PEER)
        self.assertIn("WARNING", note)
        self.assertIn("node busy", note)


class RemoveHostSourceInvariantTests(unittest.TestCase):
    """Structural checks on cmd_remove_host for the fixes that are hard to
    exercise behaviorally without a live Pacemaker/SSH environment - same
    style as the rest of this test suite (no live SSH).
    """

    def setUp(self) -> None:
        self.text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "remove_host.py"
        ).read_text(encoding="utf-8")

    def test_demote_failure_short_circuits_before_success_json(self) -> None:
        demote_call_idx = self.text.find("demote_ok, demote_notes = await _demote_survivor_topology_to_1vm")
        not_ok_branch_idx = self.text.find("if not demote_ok:")
        early_done_idx = self.text.find('"demote_ok": False')
        success_json_idx = self.text.find('"demote_ok": True')
        self.assertGreater(demote_call_idx, 0)
        self.assertGreater(not_ok_branch_idx, demote_call_idx)
        self.assertGreater(early_done_idx, not_ok_branch_idx)
        self.assertGreater(success_json_idx, early_done_idx)

    def test_departing_node_uninstall_uses_password_sudo_wrapper(self) -> None:
        self.assertIn("wrap_privileged_remote(f\"{script} --decommission\")", self.text)
        self.assertIn("stdin_text=ssh_pass", self.text)
        # The remote (non-local_is_target) branch must not fall back to a
        # bare `sudo -n` that silently no-ops without NOPASSWD sudo.
        remote_branch_idx = self.text.find("wrap_privileged_remote")
        bare_sudo_n_idx = self.text.find('remote_cmd = f"sudo -n')
        self.assertGreater(remote_branch_idx, 0)
        self.assertEqual(bare_sudo_n_idx, self.text.rfind('remote_cmd = f"sudo -n'))
        # The only remaining bare-sudo-n use is the local_is_target branch,
        # which runs as root inside privhelperd already (no sudo needed in
        # practice, kept for symmetry with the operator-facing log line).
        self.assertLess(bare_sudo_n_idx, remote_branch_idx)

    def test_standby_rollback_wired_into_both_failure_points(self) -> None:
        stood_by_set_idx = self.text.find("stood_by_target = True")
        secret_failure_idx = self.text.find("internal error: inventory would contain a secret")
        ansible_failure_idx = self.text.find("survivor-side remove-host playbook failed")
        self.assertGreater(stood_by_set_idx, 0)
        rollback_calls = [
            m for m in range(len(self.text))
            if self.text.startswith("await _unstandby_best_effort(plan.target)", m)
        ]
        self.assertEqual(len(rollback_calls), 2, "one rollback call per failure point")
        self.assertGreater(rollback_calls[0], secret_failure_idx)
        self.assertGreater(rollback_calls[1], ansible_failure_idx)

    def test_uninstall_failure_short_circuits_before_success_json(self) -> None:
        refuse_idx = self.text.find(
            "Refusing to report success: departing-node uninstall failed"
        )
        fail_json_idx = self.text.find('"uninstall_ok": False')
        success_json_idx = self.text.find('"uninstall_ok": True')
        self.assertGreater(refuse_idx, 0)
        self.assertGreater(fail_json_idx, refuse_idx)
        self.assertGreater(success_json_idx, fail_json_idx)
        self.assertNotIn(
            "already succeeded so this is not blocking",
            self.text,
        )


if __name__ == "__main__":
    unittest.main()
