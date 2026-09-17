"""Tests for the console login gate (is_mail_deployed)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kin_privhelper import deploy_state as ds


class MailDeployedGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.zimbra = self.root / "zimbra"
        self.marker = self.root / "setup-complete"
        self.ha_marker = self.root / "ha-setup-complete"
        self.topo = self.root / "topology"
        self.config = self.root / "config"
        self.draft = self.root / "wizard-draft.json"
        self.pidfile = self.root / "zmmailboxd_pid"
        self._old_root = ds.ZIMBRA_ROOT
        self._old_marker = ds.SETUP_COMPLETE_MARKER
        self._old_ha = ds.HA_SETUP_COMPLETE_MARKER
        self._old_topo = ds.TOPOLOGY_MARKER
        self._old_conf = ds.KIN_MAIL_CONFIG
        self._old_draft = ds.WIZARD_DRAFT_FILE
        ds.ZIMBRA_ROOT = self.zimbra
        ds.SETUP_COMPLETE_MARKER = self.marker
        ds.HA_SETUP_COMPLETE_MARKER = self.ha_marker
        ds.TOPOLOGY_MARKER = self.topo
        ds.KIN_MAIL_CONFIG = self.config
        ds.WIZARD_DRAFT_FILE = self.draft
        os.environ["KIN_MAILBOXD_PID"] = str(self.pidfile)
        if self.pidfile.exists():
            self.pidfile.unlink()
        self._ps = patch.object(ds, "_ps_matches", return_value=False)
        self._ps.start()
        self.addCleanup(self._ps.stop)

    def tearDown(self) -> None:
        ds.ZIMBRA_ROOT = self._old_root
        ds.SETUP_COMPLETE_MARKER = self._old_marker
        ds.HA_SETUP_COMPLETE_MARKER = self._old_ha
        ds.TOPOLOGY_MARKER = self._old_topo
        ds.KIN_MAIL_CONFIG = self._old_conf
        ds.WIZARD_DRAFT_FILE = self._old_draft
        os.environ.pop("KIN_MAILBOXD_PID", None)

    def _deny_config_read(self):
        real = Path.read_text

        def wrapped(path_self: Path, *args: object, **kwargs: object) -> str:
            if path_self == ds.KIN_MAIL_CONFIG:
                raise PermissionError("denied")
            return real(path_self, *args, **kwargs)

        return patch.object(Path, "read_text", wrapped)

    def _deny_is_file(self, *targets: Path):
        real = Path.is_file
        wanted = set(targets)

        def wrapped(path_self: Path) -> bool:
            if path_self in wanted:
                raise PermissionError("denied")
            return real(path_self)

        return patch.object(Path, "is_file", wrapped)

    def _write_topology(self, topology: str, *, via: str = "config") -> None:
        if via == "config":
            self.config.write_text(f'TOPOLOGY="{topology}"\n', encoding="utf-8")
        elif via == "draft":
            self.draft.write_text(
                json.dumps({"topology": topology}),
                encoding="utf-8",
            )
        elif via == "marker":
            self.topo.write_text(f"{topology}\n", encoding="utf-8")
        else:
            raise AssertionError(via)

    def test_no_zimbra_tree_is_not_deployed(self) -> None:
        self.assertFalse(self.zimbra.exists())
        self.assertFalse(ds.is_mail_deployed())
        self.assertFalse(ds.is_full_install_complete())
        self.assertFalse(ds.is_ha_setup_complete())

    def test_partial_zimbra_tree_without_mailboxd_is_not_deployed(self) -> None:
        """Failed 03 leaves /opt/zimbra; that must not flip the login gate."""
        self.zimbra.mkdir()
        (self.zimbra / "bin").mkdir()
        self.assertFalse(ds.mailboxd_running())
        self.assertFalse(ds.is_mail_deployed())

    def test_1vm_setup_complete_marker_is_deployed(self) -> None:
        self._write_topology("1vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertTrue(ds.is_mail_deployed())

    def test_install_in_progress_is_not_deployed_even_with_marker(self) -> None:
        self._write_topology("1vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        with patch.object(ds, "full_install_in_progress", return_value=True):
            self.assertFalse(ds.is_mail_deployed())

    def test_mailboxd_pidfile_is_legacy_deployed(self) -> None:
        self.zimbra.mkdir()
        self.pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.assertTrue(ds.mailboxd_running())
        self.assertTrue(ds.is_mail_deployed())

    def test_stale_pidfile_is_not_deployed(self) -> None:
        self.zimbra.mkdir()
        self.pidfile.write_text("99999999\n", encoding="utf-8")
        self.assertFalse(ds.mailboxd_running())
        self.assertFalse(ds.is_mail_deployed())

    def test_2vm_setup_complete_alone_is_not_deployed(self) -> None:
        """Single-node full-install is not the end of 2-server setup."""
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertFalse(ds.is_mail_deployed())
        self.assertTrue(ds.is_full_install_complete())
        self.assertFalse(ds.is_ha_setup_complete())

    def test_full_install_complete_ignores_last_log_text(self) -> None:
        """HA orchestration truncates deploy-last.log; the marker still stands."""
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertTrue(ds.is_full_install_complete())
        self.assertFalse(ds.transcript_has_terminal_outcome("HA orchestration Slice 2\nORCH_FAILED\n"))

    def test_2vm_mailboxd_is_not_a_proxy_for_ha_complete(self) -> None:
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.zimbra.mkdir()
        self.pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.assertTrue(ds.mailboxd_running())
        self.assertFalse(ds.is_mail_deployed())

    def test_2vm_both_markers_is_deployed(self) -> None:
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.ha_marker.write_text("complete\n", encoding="utf-8")
        self.assertTrue(ds.is_mail_deployed())

    def test_2vm_ha_marker_without_setup_complete_is_not_deployed(self) -> None:
        self._write_topology("2vm")
        self.ha_marker.write_text("complete\n", encoding="utf-8")
        self.assertFalse(ds.is_mail_deployed())

    def test_2vm_install_in_progress_with_both_markers_is_not_deployed(self) -> None:
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.ha_marker.write_text("complete\n", encoding="utf-8")
        with patch.object(ds, "full_install_in_progress", return_value=True):
            self.assertFalse(ds.is_mail_deployed())

    def test_2vm_from_draft_when_config_missing(self) -> None:
        self._write_topology("2vm", via="draft")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertEqual(ds.saved_wizard_topology(), "2vm")
        self.assertFalse(ds.is_mail_deployed())

    def test_marker_wins_over_config_and_draft(self) -> None:
        self._write_topology("1vm", via="config")
        self._write_topology("1vm", via="draft")
        self._write_topology("2vm", via="marker")
        self.assertEqual(ds.saved_wizard_topology(), "2vm")

    def test_unreadable_config_uses_topology_marker(self) -> None:
        """Live .52: config is 0600 and kin-console cannot read it."""
        self._write_topology("1vm", via="config")
        self._write_topology("2vm", via="marker")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.ha_marker.write_text("complete\n", encoding="utf-8")
        with self._deny_config_read():
            with self.assertLogs("kin_privhelper.deploy_state", level="WARNING") as cm:
                self.assertEqual(ds._topology_from_config(), "")
            self.assertTrue(any("permission denied" in line.lower() for line in cm.output))
            self.assertEqual(ds.saved_wizard_topology(), "2vm")
            self.assertTrue(ds.is_mail_deployed())

    def test_unreadable_config_without_marker_matches_live_bug(self) -> None:
        """Both completion markers present, but topology unreadable => wizard."""
        self._write_topology("2vm", via="config")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.ha_marker.write_text("complete\n", encoding="utf-8")
        with self._deny_config_read():
            with self.assertLogs("kin_privhelper.deploy_state", level="WARNING"):
                self.assertEqual(ds.saved_wizard_topology(), "")
                self.assertFalse(ds.is_mail_deployed())

    def test_missing_config_is_not_logged_as_permission_denied(self) -> None:
        with self.assertNoLogs("kin_privhelper.deploy_state", level="WARNING"):
            self.assertEqual(ds._topology_from_config(), "")

    def test_write_topology_marker_is_0644_and_idempotent(self) -> None:
        self.assertTrue(ds.write_topology_marker("2vm"))
        self.assertEqual(self.topo.read_text(encoding="utf-8"), "2vm\n")
        self.assertEqual(self.topo.stat().st_mode & 0o777, 0o644)
        first = self.topo.read_text(encoding="utf-8")
        self.assertTrue(ds.write_topology_marker("2vm"))
        self.assertEqual(self.topo.read_text(encoding="utf-8"), first)

    def test_write_topology_marker_accepts_split(self) -> None:
        # apply_wizard_draft treats a False here as "invalid TOPOLOGY" and
        # stops Deploy before install. A docstring that still said "1vm/2vm"
        # is how that refusal would come back.
        self.assertTrue(ds.write_topology_marker("split"))
        self.assertEqual(self.topo.read_text(encoding="utf-8"), "split\n")
        self.assertEqual(ds._normalize_topology(self.topo.read_text(encoding="utf-8")), "split")

    def test_backfill_writes_marker_from_readable_config(self) -> None:
        self._write_topology("2vm", via="config")
        self.assertTrue(ds.backfill_topology_marker_from_config())
        self.assertEqual(self.topo.read_text(encoding="utf-8"), "2vm\n")
        self.assertFalse(ds.backfill_topology_marker_from_config())

    def test_unknown_topology_with_setup_complete_stays_pre_deploy(self) -> None:
        """Unreadable topology: do not lock the operator out of the wizard."""
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertEqual(ds.saved_wizard_topology(), "")
        self.assertFalse(ds.is_mail_deployed())

    def test_apply_success_writes_ha_marker(self) -> None:
        self.assertTrue(ds.record_ha_orchestration_success(join_mode="apply"))
        self.assertTrue(self.ha_marker.is_file())
        body = self.ha_marker.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("complete "))
        self.assertEqual(self.topo.read_text(encoding="utf-8"), "2vm\n")

    def test_apply_success_does_not_rewrite_existing_ha_marker(self) -> None:
        self.ha_marker.write_text("complete 2026-01-01T00:00:00Z\n", encoding="utf-8")
        self.assertTrue(ds.record_ha_orchestration_success(join_mode="apply"))
        self.assertEqual(
            self.ha_marker.read_text(encoding="utf-8"),
            "complete 2026-01-01T00:00:00Z\n",
        )

    def test_peer_plan_writes_topology_and_markers_on_empty_peer(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text="",
            ha_marker_present=False,
            setup_marker_present=False,
        )
        # Empty peer must not get a TOPOLOGY-only skeleton config.
        self.assertTrue(plan["refuse_incomplete_config"])
        self.assertFalse(plan["write_config"])
        self.assertEqual(plan["config_body"], "")
        self.assertTrue(plan["write_ha_marker"])
        self.assertTrue(plan["write_setup_marker"])
        self.assertTrue(plan["write_topology_marker"])
        self.assertEqual(plan["topology_marker_body"], "2vm\n")
        self.assertTrue(plan["ha_marker_body"].startswith("complete "))
        self.assertFalse(plan["noop"])

    def test_peer_plan_writes_config_when_peer_has_identity_keys(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text=(
                'TOPOLOGY="1vm"\n'
                'MAIL_HOST="mail2.example.test"\n'
                'SERVER_IP="192.0.2.14"\n'
                'MAIL_DOMAIN="example.test"\n'
            ),
            ha_marker_present=False,
            setup_marker_present=False,
        )
        self.assertFalse(plan["refuse_incomplete_config"])
        self.assertTrue(plan["write_config"])
        self.assertIn('TOPOLOGY="2vm"', plan["config_body"])
        self.assertIn('SERVER_IP="192.0.2.14"', plan["config_body"])
        self.assertTrue(plan["write_ha_marker"])
        self.assertFalse(plan["noop"])

    def test_peer_plan_is_noop_when_already_marked(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text=(
                'TOPOLOGY="2vm"\n'
                'MAIL_HOST="mail2.example.test"\n'
                'SERVER_IP="192.0.2.14"\n'
                'MAIL_DOMAIN="example.test"\n'
            ),
            ha_marker_present=True,
            ha_marker_text="complete 2026-08-21T00:00:00Z\n",
            setup_marker_present=True,
            topology_marker_text="2vm\n",
        )
        self.assertTrue(plan["noop"])
        self.assertFalse(plan["refuse_incomplete_config"])
        self.assertFalse(plan["write_config"])
        self.assertFalse(plan["write_ha_marker"])
        self.assertFalse(plan["write_setup_marker"])
        self.assertFalse(plan["write_topology_marker"])

    def test_peer_plan_writes_topology_marker_when_only_that_is_missing(self) -> None:
        """Live backfill: peer already has 2vm config + completion markers."""
        plan = ds.plan_peer_ha_console_state(
            config_text=(
                'TOPOLOGY="2vm"\n'
                'MAIL_HOST="mail2.example.test"\n'
                'SERVER_IP="192.0.2.14"\n'
                'MAIL_DOMAIN="example.test"\n'
            ),
            ha_marker_present=True,
            ha_marker_text="complete 2026-08-21T00:00:00Z\n",
            setup_marker_present=True,
            topology_marker_text="",
        )
        self.assertFalse(plan["noop"])
        self.assertFalse(plan["refuse_incomplete_config"])
        self.assertFalse(plan["write_config"])
        self.assertFalse(plan["write_ha_marker"])
        self.assertFalse(plan["write_setup_marker"])
        self.assertTrue(plan["write_topology_marker"])
        self.assertEqual(plan["topology_marker_body"], "2vm\n")

    def test_peer_plan_adds_ha_marker_only_when_topology_already_2vm(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text=(
                'TOPOLOGY="2vm"\n'
                'MAIL_HOST="mail2.example.test"\n'
                'SERVER_IP="192.0.2.14"\n'
                'MAIL_DOMAIN="example.test"\n'
            ),
            ha_marker_present=False,
            setup_marker_present=True,
            topology_marker_text="2vm\n",
        )
        self.assertFalse(plan["refuse_incomplete_config"])
        self.assertFalse(plan["write_config"])
        self.assertTrue(plan["write_ha_marker"])
        self.assertFalse(plan["write_setup_marker"])
        self.assertFalse(plan["write_topology_marker"])
        self.assertFalse(plan["noop"])

    def test_peer_plan_refuses_incomplete_even_when_topology_already_2vm(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text='TOPOLOGY="2vm"\nMAIL_HOST="mail2.example.test"\n',
            ha_marker_present=False,
            setup_marker_present=True,
            topology_marker_text="2vm\n",
        )
        self.assertTrue(plan["refuse_incomplete_config"])
        self.assertFalse(plan["write_config"])
        self.assertFalse(plan["noop"])

    def test_check_mode_does_not_write_ha_marker(self) -> None:
        self.assertFalse(ds.record_ha_orchestration_success(join_mode="check"))
        self.assertFalse(self.ha_marker.exists())

    def test_failed_run_does_not_call_record(self) -> None:
        """ORCH_FAILED returns before record_ha_orchestration_success - marker stays absent."""
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertFalse(self.ha_marker.exists())
        self.assertFalse(ds.is_mail_deployed())

    def test_unreadable_setup_marker_is_file_is_not_deployed(self) -> None:
        """Live 2026-08-22: /etc/kin-mail not traversable makes Path.is_file raise."""
        self._write_topology("1vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        with self._deny_is_file(self.marker):
            with self.assertLogs("kin_privhelper.deploy_state", level="WARNING") as cm:
                self.assertFalse(ds.is_full_install_complete())
                self.assertFalse(ds.is_mail_deployed())
            self.assertTrue(any("permission denied" in line.lower() for line in cm.output))

    def test_unreadable_ha_marker_is_file_is_not_complete(self) -> None:
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.ha_marker.write_text("complete\n", encoding="utf-8")
        with self._deny_is_file(self.ha_marker):
            with self.assertLogs("kin_privhelper.deploy_state", level="WARNING"):
                self.assertFalse(ds.is_ha_setup_complete())
                self.assertFalse(ds.is_mail_deployed())

    def test_setup_status_payload_survives_is_mail_deployed_raise(self) -> None:
        with patch.object(ds, "is_mail_deployed", side_effect=PermissionError("denied")):
            with self.assertLogs("kin_privhelper.deploy_state", level="ERROR"):
                payload = ds.setup_status_payload()
        self.assertFalse(payload["deployed"])
        self.assertFalse(payload["full_install_complete"])
        self.assertFalse(payload["ha_setup_complete"])
        self.assertFalse(payload["install_in_progress"])
        self.assertFalse(payload["zimbra_tree_present"])

    def test_ensure_kin_mail_dir_sets_0755(self) -> None:
        d = self.root / "kin-mail-dir"
        d.mkdir()
        os.chmod(d, 0o700)
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        ds.ensure_kin_mail_dir(d)
        self.assertEqual(d.stat().st_mode & 0o777, 0o755)

    def test_write_topology_marker_makes_parent_0755(self) -> None:
        parent = self.topo.parent
        os.chmod(parent, 0o700)
        self.assertTrue(ds.write_topology_marker("1vm"))
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)


class UnexpectedInstallStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.running = self.root / "full-install.running"
        self.ha_running = self.root / "ha-orchestration.running"
        self.log = self.root / "deploy-last.log"
        self._old_m = ds.FULL_INSTALL_RUNNING_MARKER
        self._old_ha = ds.HA_ORCH_RUNNING_MARKER
        self._old_l = ds.DEPLOY_LAST_LOG
        ds.FULL_INSTALL_RUNNING_MARKER = self.running
        ds.HA_ORCH_RUNNING_MARKER = self.ha_running
        ds.DEPLOY_LAST_LOG = self.log

    def tearDown(self) -> None:
        ds.FULL_INSTALL_RUNNING_MARKER = self._old_m
        ds.HA_ORCH_RUNNING_MARKER = self._old_ha
        ds.DEPLOY_LAST_LOG = self._old_l

    def test_transcript_detects_success_and_fail(self) -> None:
        self.assertFalse(ds.transcript_has_terminal_outcome("telemetry -> No\n"))
        self.assertTrue(ds.transcript_has_terminal_outcome("Full install complete\n"))
        self.assertTrue(
            ds.transcript_has_terminal_outcome("Full install complete, with warnings\n")
        )
        self.assertTrue(ds.transcript_has_terminal_outcome("Pipeline stopped at 03-install-zimbra.sh\n"))
        self.assertTrue(ds.transcript_has_terminal_outcome("[FAIL] something\n"))

    def test_reclaim_stamps_fail_when_process_gone(self) -> None:
        self.running.write_text("started\n", encoding="utf-8")
        self.log.write_text("        telemetry -> No\n", encoding="utf-8")
        with patch.object(ds, "full_install_in_progress", return_value=False):
            self.assertTrue(ds.reclaim_stale_full_install_marker())
        body = self.log.read_text(encoding="utf-8")
        self.assertIn("Install stopped unexpectedly", body)
        self.assertFalse(self.running.exists())

    def test_reclaim_leaves_marker_if_install_still_running(self) -> None:
        self.running.write_text("started\n", encoding="utf-8")
        self.log.write_text("apply\n", encoding="utf-8")
        with patch.object(ds, "full_install_in_progress", return_value=True):
            self.assertFalse(ds.reclaim_stale_full_install_marker())
        self.assertTrue(self.running.exists())
        self.assertNotIn("unexpectedly", self.log.read_text(encoding="utf-8"))

    def test_reclaim_does_not_overwrite_successful_transcript(self) -> None:
        self.running.write_text("started\n", encoding="utf-8")
        self.log.write_text("Full install complete\n", encoding="utf-8")
        with patch.object(ds, "full_install_in_progress", return_value=False):
            self.assertTrue(ds.reclaim_stale_full_install_marker())
        self.assertNotIn("Install stopped unexpectedly", self.log.read_text(encoding="utf-8"))
        self.assertFalse(self.running.exists())

    def test_ha_marker_makes_pipeline_busy(self) -> None:
        self.assertFalse(ds.ha_orchestration_in_progress())
        ds.mark_ha_orchestration_started()
        self.assertTrue(ds.ha_orchestration_in_progress())
        with patch.object(ds, "full_install_in_progress", return_value=False):
            self.assertTrue(ds.pipeline_in_progress())
            payload = ds.setup_status_payload()
        self.assertTrue(payload["install_in_progress"])
        self.assertTrue(payload["busy"])
        ds.mark_ha_orchestration_finished()
        self.assertFalse(ds.ha_orchestration_in_progress())

    def test_reclaim_stale_ha_stamps_orch_failed(self) -> None:
        ds.mark_ha_orchestration_started()
        self.log.write_text("=== HA orchestration Slice 2 ===\n", encoding="utf-8")
        self.assertTrue(ds.reclaim_stale_ha_orchestration_marker())
        body = self.log.read_text(encoding="utf-8")
        self.assertIn("ORCH_FAILED step=interrupted", body)
        self.assertFalse(self.ha_running.exists())

    def test_reclaim_stale_ha_keeps_existing_orch_failed(self) -> None:
        ds.mark_ha_orchestration_started()
        self.log.write_text("ORCH_FAILED step=disk_preflight join_mode=apply\n", encoding="utf-8")
        self.assertTrue(ds.reclaim_stale_ha_orchestration_marker())
        self.assertEqual(
            self.log.read_text(encoding="utf-8").count("ORCH_FAILED"),
            1,
        )
        self.assertFalse(self.ha_running.exists())


class FullInstallRefuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_when_setup_complete(self) -> None:
        from unittest.mock import AsyncMock, patch

        from kin_privhelper.commands import cmd_run_full_install

        with patch(
            "kin_privhelper.maintenance.gather_status",
            new=AsyncMock(return_value={"standby": []}),
        ), patch(
            "kin_privhelper.commands.is_full_install_complete",
            return_value=True,
        ):
            events = [ev async for ev in cmd_run_full_install()]
        self.assertTrue(any("Refusing:" in str(ev.get("data") or "") for ev in events))
        self.assertEqual(events[-1], {"type": "done", "exit_code": 1})


class DemoteWizardDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.draft = Path(self._td.name) / "wizard-draft.json"
        self._old = ds.WIZARD_DRAFT_FILE
        ds.WIZARD_DRAFT_FILE = self.draft

    def tearDown(self) -> None:
        ds.WIZARD_DRAFT_FILE = self._old

    def test_clears_peer_keeps_vip_and_obs(self) -> None:
        self.draft.write_text(
            json.dumps(
                {
                    "topology": "2vm",
                    "peer_host_ip": "192.0.2.14",
                    "peer_host_name": "mail2.example.test",
                    "observability_vm_ip": "192.0.2.12",
                    "cluster_vip_ip": "192.0.2.16",
                }
            ),
            encoding="utf-8",
        )
        notes = ds.demote_wizard_draft_after_remove_host()
        self.assertTrue(any("wizard draft" in n for n in notes))
        data = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertEqual(data["topology"], "1vm")
        self.assertEqual(data["peer_host_ip"], "")
        self.assertEqual(data["peer_host_name"], "")
        self.assertEqual(data["cluster_vip_ip"], "192.0.2.16")
        self.assertEqual(data["observability_vm_ip"], "192.0.2.12")

    def test_missing_draft_is_noop(self) -> None:
        self.assertEqual(ds.demote_wizard_draft_after_remove_host(), [])


if __name__ == "__main__":
    unittest.main()
