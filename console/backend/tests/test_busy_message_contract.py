"""The busy message is a contract with the console, not just prose.

Pressing "Install monitoring" while the automatic install is already running
used to report a failure. It is not a failure - it is the same work, already
underway - and an operator who sees "failed" on a box where monitoring is busy
succeeding reasonably concludes the product is broken. It happened in live QA
(Phase 15, 11 September 2026): the button said failed, a reload showed
monitoring present and healthy.

The console now recognises that case and waits for the result instead. It does
so by matching the daemon's wording, which makes the wording load-bearing. This
pins both ends so a reworded sentence cannot silently reintroduce the bug.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from kin_privhelper import daemon, protocol as proto

REPO = Path(__file__).resolve().parents[3]
TAB = REPO / "console/frontend/src/monitoring/MonitoringTab.tsx"


class TheBusyMessageSaysWhatIsRunning(unittest.TestCase):
    def _message_for(self, cmd: str, seconds: int = 10) -> str:
        with mock.patch.object(
            daemon,
            "running_job",
            return_value={"running": True, "command": cmd, "seconds": seconds},
        ):
            return daemon._busy_message()

    def test_it_names_the_operation_rather_than_saying_something_is_running(self) -> None:
        msg = self._message_for(proto.CMD_INSTALL_MONITORING)
        self.assertIn("Install monitoring", msg)
        self.assertIn("is already running", msg)

    def test_a_different_operation_produces_a_different_sentence(self) -> None:
        """Otherwise the console cannot tell its own work from somebody else's."""
        mon = self._message_for(proto.CMD_INSTALL_MONITORING)
        grow = self._message_for(proto.CMD_GROW_DISK)
        self.assertNotEqual(mon, grow)
        self.assertNotIn("Install monitoring", grow)

    def test_an_idle_daemon_does_not_claim_something_is_running(self) -> None:
        with mock.patch.object(daemon, "running_job", return_value={"running": False}):
            self.assertNotIn("already running", daemon._busy_message())


class TheConsoleMatchesThatWording(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(TAB.is_file(), f"missing {TAB}")
        self.tab = TAB.read_text(encoding="utf-8")

    def test_the_console_looks_for_the_already_running_case(self) -> None:
        self.assertIn("alreadyRunning", self.tab)
        self.assertRegex(self.tab, r"Install monitoring is already running")

    def test_the_pattern_in_the_console_really_matches_the_daemon(self) -> None:
        """The whole point: both ends must agree on the sentence."""
        found = re.search(r"/([^/\n]*already running[^/\n]*)/i", self.tab)
        self.assertIsNotNone(found, "no already-running pattern in MonitoringTab.tsx")
        pattern = found.group(1)
        with mock.patch.object(
            daemon,
            "running_job",
            return_value={
                "running": True,
                "command": proto.CMD_INSTALL_MONITORING,
                "seconds": 30,
            },
        ):
            msg = daemon._busy_message()
        self.assertRegex(msg, re.compile(pattern, re.I))

    def test_it_does_not_match_an_unrelated_busy_operation(self) -> None:
        """A rule broad enough to match anything would swallow real failures."""
        found = re.search(r"/([^/\n]*already running[^/\n]*)/i", self.tab)
        assert found is not None
        pattern = re.compile(found.group(1), re.I)
        with mock.patch.object(
            daemon,
            "running_job",
            return_value={"running": True, "command": proto.CMD_GROW_DISK, "seconds": 30},
        ):
            other = daemon._busy_message()
        self.assertIsNone(pattern.search(other))

    def test_an_already_running_install_is_reported_as_success_not_failure(self) -> None:
        block = self.tab[self.tab.index("if (alreadyRunning)") :][:240]
        self.assertIn('setInstallDone("ok")', block)
        self.assertIn("setSettling(true)", block)


if __name__ == "__main__":
    unittest.main()
