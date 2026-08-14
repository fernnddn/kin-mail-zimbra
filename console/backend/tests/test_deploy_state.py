"""Tests for the console login gate (is_mail_deployed)."""

from __future__ import annotations

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
        self.pidfile = self.root / "zmmailboxd_pid"
        self._old_root = ds.ZIMBRA_ROOT
        self._old_marker = ds.SETUP_COMPLETE_MARKER
        ds.ZIMBRA_ROOT = self.zimbra
        ds.SETUP_COMPLETE_MARKER = self.marker
        os.environ["KIN_MAILBOXD_PID"] = str(self.pidfile)
        if self.pidfile.exists():
            self.pidfile.unlink()
        self._ps = patch.object(ds, "_ps_matches", return_value=False)
        self._ps.start()
        self.addCleanup(self._ps.stop)

    def tearDown(self) -> None:
        ds.ZIMBRA_ROOT = self._old_root
        ds.SETUP_COMPLETE_MARKER = self._old_marker
        os.environ.pop("KIN_MAILBOXD_PID", None)

    def test_no_zimbra_tree_is_not_deployed(self) -> None:
        self.assertFalse(self.zimbra.exists())
        self.assertFalse(ds.is_mail_deployed())

    def test_partial_zimbra_tree_without_mailboxd_is_not_deployed(self) -> None:
        """Failed 03 leaves /opt/zimbra; that must not flip the login gate."""
        self.zimbra.mkdir()
        (self.zimbra / "bin").mkdir()
        self.assertFalse(ds.mailboxd_running())
        self.assertFalse(ds.is_mail_deployed())

    def test_setup_complete_marker_is_deployed(self) -> None:
        self.marker.write_text("complete\n", encoding="utf-8")
        self.assertTrue(ds.is_mail_deployed())

    def test_install_in_progress_is_not_deployed_even_with_marker(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
