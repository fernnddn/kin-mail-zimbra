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
        self.config = self.root / "config"
        self.draft = self.root / "wizard-draft.json"
        self.pidfile = self.root / "zmmailboxd_pid"
        self._old_root = ds.ZIMBRA_ROOT
        self._old_marker = ds.SETUP_COMPLETE_MARKER
        self._old_ha = ds.HA_SETUP_COMPLETE_MARKER
        self._old_conf = ds.KIN_MAIL_CONFIG
        self._old_draft = ds.WIZARD_DRAFT_FILE
        ds.ZIMBRA_ROOT = self.zimbra
        ds.SETUP_COMPLETE_MARKER = self.marker
        ds.HA_SETUP_COMPLETE_MARKER = self.ha_marker
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
        ds.KIN_MAIL_CONFIG = self._old_conf
        ds.WIZARD_DRAFT_FILE = self._old_draft
        os.environ.pop("KIN_MAILBOXD_PID", None)

    def _write_topology(self, topology: str, *, via: str = "config") -> None:
        if via == "config":
            self.config.write_text(f'TOPOLOGY="{topology}"\n', encoding="utf-8")
        elif via == "draft":
            self.draft.write_text(
                json.dumps({"topology": topology}),
                encoding="utf-8",
            )
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
        self.assertTrue(plan["write_config"])
        self.assertIn('TOPOLOGY="2vm"', plan["config_body"])
        self.assertTrue(plan["write_ha_marker"])
        self.assertTrue(plan["write_setup_marker"])
        self.assertTrue(plan["ha_marker_body"].startswith("complete "))
        self.assertFalse(plan["noop"])

    def test_peer_plan_is_noop_when_already_marked(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text='TOPOLOGY="2vm"\nMAIL_HOST="mail2.example.test"\n',
            ha_marker_present=True,
            ha_marker_text="complete 2026-08-21T00:00:00Z\n",
            setup_marker_present=True,
        )
        self.assertTrue(plan["noop"])
        self.assertFalse(plan["write_config"])
        self.assertFalse(plan["write_ha_marker"])
        self.assertFalse(plan["write_setup_marker"])

    def test_peer_plan_adds_ha_marker_only_when_topology_already_2vm(self) -> None:
        plan = ds.plan_peer_ha_console_state(
            config_text='TOPOLOGY="2vm"\n',
            ha_marker_present=False,
            setup_marker_present=True,
        )
        self.assertFalse(plan["write_config"])
        self.assertTrue(plan["write_ha_marker"])
        self.assertFalse(plan["write_setup_marker"])
        self.assertFalse(plan["noop"])

    def test_check_mode_does_not_write_ha_marker(self) -> None:
        self.assertFalse(ds.record_ha_orchestration_success(join_mode="check"))
        self.assertFalse(self.ha_marker.exists())

    def test_failed_run_does_not_call_record(self) -> None:
        """ORCH_FAILED returns before record_ha_orchestration_success — marker stays absent."""
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertFalse(self.ha_marker.exists())
        self.assertFalse(ds.is_mail_deployed())


class UnexpectedInstallStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.running = self.root / "full-install.running"
        self.log = self.root / "deploy-last.log"
        self._old_m = ds.FULL_INSTALL_RUNNING_MARKER
        self._old_l = ds.DEPLOY_LAST_LOG
        ds.FULL_INSTALL_RUNNING_MARKER = self.running
        ds.DEPLOY_LAST_LOG = self.log

    def tearDown(self) -> None:
        ds.FULL_INSTALL_RUNNING_MARKER = self._old_m
        ds.DEPLOY_LAST_LOG = self._old_l

    def test_transcript_detects_success_and_fail(self) -> None:
        self.assertFalse(ds.transcript_has_terminal_outcome("telemetry -> No\n"))
        self.assertTrue(ds.transcript_has_terminal_outcome("Full install complete\n"))
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


if __name__ == "__main__":
    unittest.main()
