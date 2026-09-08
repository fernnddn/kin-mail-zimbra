"""The helper runs one privileged job at a time. It has to say which one.

Before this, the console's only notion of "busy" was React state. A page
reloaded during a long operation forgot it, re-offered Enter Maintenance and
Remove Host, and the daemon answered each click with a bare "another
privileged execution is in progress". The operator wrote it down as
"it should still remember when refreshed so it's not busy like this"
(live QA, 8 Sep 2026).

Two things fix that and both are asserted here: the daemon reports what holds
the slot, and the message names it.
"""

from __future__ import annotations

import unittest

from kin_privhelper import daemon
from kin_privhelper import protocol as proto


class RunningJobTests(unittest.TestCase):
    def setUp(self) -> None:
        daemon.reset_run_slot_for_tests()

    def tearDown(self) -> None:
        daemon.reset_run_slot_for_tests()

    def test_idle_reports_nothing_running(self) -> None:
        got = daemon.running_job()
        self.assertEqual(got["running"], False)
        self.assertEqual(got["command"], "")
        self.assertEqual(got["seconds"], 0)

    def test_it_names_the_command_that_holds_the_slot(self) -> None:
        daemon.mark_run_slot_busy_for_tests(proto.CMD_REMOVE_HOST)
        got = daemon.running_job()
        self.assertTrue(got["running"])
        self.assertEqual(got["command"], proto.CMD_REMOVE_HOST)
        self.assertGreaterEqual(got["seconds"], 1)

    def test_releasing_clears_the_command(self) -> None:
        """A stale command name would have the console disabling buttons for a
        job that finished."""

        import asyncio

        token = daemon.mark_run_slot_busy_for_tests(proto.CMD_MAINTENANCE)
        asyncio.run(daemon.release_run_slot(token))
        got = daemon.running_job()
        self.assertFalse(got["running"])
        self.assertEqual(got["command"], "")

    def test_the_status_snapshot_never_raises(self) -> None:
        """Cluster status is the page's lifeline. It must not fail on this."""
        from kin_privhelper.maintenance import _running_job_snapshot

        daemon.mark_run_slot_busy_for_tests(proto.CMD_ADD_HOST)
        got = _running_job_snapshot()
        self.assertEqual(got["command"], proto.CMD_ADD_HOST)


class BusyMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        daemon.reset_run_slot_for_tests()

    def tearDown(self) -> None:
        daemon.reset_run_slot_for_tests()

    def test_it_names_the_operation_in_words_the_operator_recognises(
        self,
    ) -> None:
        daemon.mark_run_slot_busy_for_tests(proto.CMD_RUN_HA_ORCHESTRATION)
        msg = daemon._busy_message()
        self.assertIn("Build HA pair", msg)
        self.assertIn("Wait for it to finish", msg)
        # The raw command id is not something to hand an operator.
        self.assertNotIn("run_ha_orchestration", msg)

    def test_it_says_how_long(self) -> None:
        daemon.mark_run_slot_busy_for_tests(proto.CMD_REMOVE_OBSERVABILITY)
        msg = daemon._busy_message()
        self.assertIn("Remove Observability", msg)
        self.assertRegex(msg, r"for \d+ (seconds|minutes)")

    def test_an_unknown_command_still_produces_a_sentence(self) -> None:
        daemon.mark_run_slot_busy_for_tests("some_future_command")
        msg = daemon._busy_message()
        self.assertIn("some_future_command", msg)
        self.assertIn("Only one privileged operation", msg)

    def test_idle_falls_back_to_the_old_wording(self) -> None:
        self.assertEqual(
            daemon._busy_message(), "another privileged execution is in progress"
        )

    def test_every_slot_taking_command_has_a_label(self) -> None:
        """A command with no label prints its raw id at the operator."""
        bypass = {
            proto.CMD_CANCEL_FIREWALL_DEADMAN,
            proto.CMD_GET_AUDIT_LOG,
            proto.CMD_GET_DEPLOY_LOG,
            proto.CMD_CLEAR_INITIAL_CONSOLE_PASSWORD,
            proto.CMD_HA_DISK_PREFLIGHT,
            proto.CMD_GET_STATUS,
            proto.CMD_RUN_HARDENING_STATUS,
            proto.CMD_STORE_PROVISIONING_SECRETS,
            proto.CMD_STORE_OBSERVABILITY_SECRETS,
        }
        missing = sorted(
            c
            for c in proto.ALLOWED_COMMANDS
            if c not in bypass and c not in daemon._CMD_LABELS
        )
        self.assertEqual(missing, [], "no busy label for: " + str(missing))


if __name__ == "__main__":
    unittest.main()
