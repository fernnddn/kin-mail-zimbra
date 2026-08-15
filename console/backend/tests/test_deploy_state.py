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

    def test_check_mode_does_not_write_ha_marker(self) -> None:
        self.assertFalse(ds.record_ha_orchestration_success(join_mode="check"))
        self.assertFalse(self.ha_marker.exists())

    def test_failed_run_does_not_call_record(self) -> None:
        """ORCH_FAILED returns before record_ha_orchestration_success — marker stays absent."""
        self._write_topology("2vm")
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertFalse(self.ha_marker.exists())
        self.assertFalse(ds.is_mail_deployed())


if __name__ == "__main__":
    unittest.main()
