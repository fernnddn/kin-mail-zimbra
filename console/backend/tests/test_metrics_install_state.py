"""A detached metrics install has to leave evidence of what it did.

The install is deliberately not awaited: it is an asyncio task so that an SSE
drop cannot kill it (see run_after_install). The cost of that is that its exit
code has nowhere to return to. Before this state file existed the outcome went
into the deploy transcript and then nowhere at all, and the deploy had already
reported success, so a failed or timed-out metrics install produced an
appliance that looked finished and healthy and had permanently empty Monitoring
and Reports tabs. That is the failure the boss called out: silent background
failure after "Deploy succeeded".

These tests pin the contract the console reads:

  ok       proven installed, and only after a real (non --check) run
  failed   it ran and did not work
  timeout  it was still going after the ceiling and was given up on
  error    it could not be started at all
  running  started, not yet finished, and NOT counted as installed
  unknown  no record either way, which is also not counted as installed
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kin_privhelper import monitoring_install as mi


class _StateFile:
    """Point the module at a scratch file for the duration of a test."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "metrics-state.json"
        self._patch = mock.patch.object(mi, "METRICS_STATE_FILE", self.path)

    def __enter__(self) -> "_StateFile":
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()
        self._tmp.cleanup()


class RecordingTests(unittest.TestCase):
    def test_a_successful_run_reads_back_as_installed(self) -> None:
        with _StateFile():
            self.assertTrue(mi.record_metrics_state("ok", exit_code=0))
            state = mi.read_metrics_state()
        self.assertEqual(state["phase"], "ok")
        self.assertTrue(state["installed"])

    def test_a_failure_is_recorded_with_its_exit_code_and_a_reason(self) -> None:
        with _StateFile():
            mi.record_metrics_state("failed", exit_code=4, reason="playbook died")
            state = mi.read_metrics_state()
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(state["exit_code"], 4)
        self.assertIn("playbook died", state["reason"])
        self.assertFalse(state["installed"], "a failed install is not an install")

    def test_running_is_not_installed(self) -> None:
        # The window that matters: the deploy has already said it succeeded.
        with _StateFile():
            mi.record_metrics_state("running")
            state = mi.read_metrics_state()
        self.assertEqual(state["phase"], "running")
        self.assertFalse(state["installed"])

    def test_no_file_at_all_is_unknown_and_not_installed(self) -> None:
        with _StateFile():
            state = mi.read_metrics_state()
        self.assertEqual(state["phase"], "unknown")
        self.assertFalse(state["installed"])

    def test_a_corrupt_file_is_unknown_rather_than_an_exception(self) -> None:
        # This is read while rendering a report. It must never be the reason a
        # page 500s.
        with _StateFile() as sf:
            sf.path.write_text("{not json", encoding="utf-8")
            self.assertEqual(mi.read_metrics_state()["phase"], "unknown")
            sf.path.write_text(json.dumps(["a list"]), encoding="utf-8")
            self.assertEqual(mi.read_metrics_state()["phase"], "unknown")
            sf.path.write_text(json.dumps({"phase": "excellent"}), encoding="utf-8")
            self.assertEqual(mi.read_metrics_state()["phase"], "unknown")

    def test_an_unwritable_location_is_reported_not_raised(self) -> None:
        # Called from a background task and from a finally path. A full disk
        # must not turn a metrics problem into a deploy crash.
        with mock.patch.object(mi, "METRICS_STATE_FILE", Path("/proc/x/y/state.json")):
            self.assertFalse(mi.record_metrics_state("ok", exit_code=0))
            self.assertEqual(mi.read_metrics_state()["phase"], "unknown")

    def test_a_bogus_phase_is_refused(self) -> None:
        with _StateFile():
            self.assertFalse(mi.record_metrics_state("probably-fine"))
            self.assertEqual(mi.read_metrics_state()["phase"], "unknown")

    def test_every_phase_the_module_advertises_round_trips(self) -> None:
        # Derived from the tuple rather than restated, so a phase added later
        # is covered without anyone editing this list.
        for phase in mi.METRICS_PHASES:
            with self.subTest(phase=phase), _StateFile():
                self.assertTrue(mi.record_metrics_state(phase))
                self.assertEqual(mi.read_metrics_state()["phase"], phase)


class TheManualButtonUpdatesTheSameState(unittest.TestCase):
    def test_the_repair_button_and_the_deploy_share_one_record(self) -> None:
        # The Monitoring tab's Install monitoring button is the repair path.
        # If it did not write the same state, a fixed appliance would stay
        # marked failed for ever and the warning would become furniture.
        import inspect

        src = inspect.getsource(mi.cmd_install_monitoring)
        self.assertIn("record_metrics_state", src)

    def test_a_dry_run_never_claims_an_install(self) -> None:
        # --check converges nothing. Stamping "ok" from a rehearsal would mark
        # an appliance as collecting metrics on the strength of a plan.
        src = __import__("inspect").getsource(mi.cmd_install_monitoring)
        self.assertIn("check_only", src)
        self.assertIn("if not check_only", src)

    def test_a_refusal_does_not_downgrade_a_working_node(self) -> None:
        # Lock held, or a hostname it will not build an inventory for: the node
        # is untouched, so a node already collecting metrics must keep saying
        # so. Those paths return before any state is written.
        src = __import__("inspect").getsource(mi.cmd_install_monitoring)
        head = src.split("_write_work_files")[0]
        self.assertNotIn("record_metrics_state", head)


class TheDetachedRunRecordsWhatItCannotSeeFromInside(unittest.TestCase):
    def test_it_marks_running_before_it_starts(self) -> None:
        # A privhelperd killed mid-install leaves "running" rather than
        # nothing, and "running" is not installed.
        import inspect

        src = inspect.getsource(mi.run_after_install)
        self.assertIn('record_metrics_state("running")', src)

    def test_a_timeout_and_a_failure_to_start_are_both_recorded(self) -> None:
        import inspect

        src = inspect.getsource(mi.run_after_install)
        self.assertIn('"timeout"', src)
        self.assertIn('"error"', src)

    def test_no_event_loop_is_recorded_as_an_error(self) -> None:
        with _StateFile():
            result = mi.run_after_install()
            state = mi.read_metrics_state()
        self.assertIn("no event loop", result)
        self.assertEqual(state["phase"], "error")
        self.assertFalse(state["installed"])


class TheConsoleCanActuallyReadTheRecord(unittest.TestCase):
    """privhelperd writes this as root; the console reads it as kin-console.

    That crossing is the whole risk. If the console cannot read the file, the
    read fails, the failure used to look identical to "no record", and a
    no-record state is deliberately silent so that appliances from before this
    existed are not covered in warnings. The result would have been a failed
    metrics install that warns nowhere at all, which is precisely the lie the
    record was added to prevent.
    """

    def test_the_record_is_not_kept_in_the_privhelper_secrets_directory(self) -> None:
        # /var/lib/kin-mail-privhelper is privhelperd's HOME and holds
        # provisioning-secrets.json and observability-secrets.json at 0600. It
        # is created by whichever code path gets there first, with no chmod,
        # so nothing guarantees the console can traverse it.
        self.assertNotIn(
            "kin-mail-privhelper",
            str(mi.METRICS_STATE_FILE),
            "the console-readable record must not live in the secrets vault",
        )
        self.assertIn("kin-mail-console", str(mi.METRICS_STATE_FILE))

    def test_the_record_is_world_readable_because_it_holds_no_secret(self) -> None:
        # privhelperd writes it as root and the console reads it as
        # kin-console. 0644 is what makes that crossing work without granting
        # the console anything else. It carries a phase, an exit code and an
        # English sentence, so there is nothing in it to protect.
        with _StateFile() as sf:
            mi.record_metrics_state("ok", exit_code=0)
            self.assertEqual(sf.path.stat().st_mode & 0o777, 0o644)

    def test_writing_never_widens_the_console_data_directory(self) -> None:
        # /var/lib/kin-mail-console is installed 0750 kin-console:kin-console
        # by bootstrap.sh and holds admin.hash, session.secret and
        # tls/key.pem. The console owns it and can already read it, so
        # "making sure" by chmodding it to 0755 would publish that material to
        # every local user: a far worse bug than the one this record fixes.
        with _StateFile() as sf:
            os.chmod(sf.path.parent, 0o750)
            mi.record_metrics_state("ok", exit_code=0)
            self.assertEqual(
                sf.path.parent.stat().st_mode & 0o777,
                0o750,
                "the metrics record must not relax the console data directory",
            )

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_an_unreadable_record_is_never_reported_as_no_record(self) -> None:
        # The exact silent path. "unknown" is not warned about anywhere, so a
        # permission problem must not be allowed to look like one.
        with _StateFile() as sf:
            mi.record_metrics_state("failed", exit_code=2, reason="playbook died")
            os.chmod(sf.path.parent, 0o000)
            try:
                state = mi.read_metrics_state()
            finally:
                os.chmod(sf.path.parent, 0o755)
        self.assertEqual(state["phase"], "unreadable")
        self.assertFalse(state["installed"])
        self.assertIn("cannot read", state["reason"])

    def test_an_unreadable_record_produces_an_operator_warning(self) -> None:
        # End to end: the phase has to survive into what the operator sees, or
        # distinguishing it from "unknown" bought nothing.
        from kin_console import reporting

        notes = reporting.data_quality_notes(
            {"metrics_install": {"phase": "unreadable", "reason": "The console cannot read it."}}
        )
        self.assertIn("The console cannot read it.", notes)

    def test_every_bad_phase_the_operator_must_see_produces_a_warning(self) -> None:
        # failed / timeout / error / not-started all have to reach the page.
        # Derived from the phase list so a new one cannot be added silently.
        from kin_console import reporting

        for phase in ("failed", "timeout", "error", "unreadable"):
            with self.subTest(phase=phase):
                notes = reporting.data_quality_notes(
                    {"metrics_install": {"phase": phase, "reason": f"{phase} happened"}}
                )
                self.assertTrue(notes, f"{phase} must warn the operator")


class AStaleRunningRecordIsNotBelievedForEver(unittest.TestCase):
    """privhelperd dying mid-install takes the detached task with it.

    Nothing is then left to write the outcome, so the record stays "running".
    Believing that for ever leaves the console waiting on a result that is
    never coming, and "running" is not warned about as an error.
    """

    def test_a_fresh_running_record_is_still_running(self) -> None:
        with _StateFile():
            mi.record_metrics_state("running")
            state = mi.read_metrics_state()
        self.assertEqual(state["phase"], "running")

    def test_a_running_record_older_than_the_ceiling_becomes_a_timeout(self) -> None:
        with _StateFile():
            mi.record_metrics_state("running")
            later = datetime.now(timezone.utc) + timedelta(
                seconds=mi.INSTALL_TIMEOUT_SEC + 60
            )
            state = mi.read_metrics_state(now=later)
        self.assertEqual(state["phase"], "timeout")
        self.assertFalse(state["installed"])
        self.assertIn("interrupted", state["reason"])

    def test_a_completed_record_is_never_aged_out(self) -> None:
        # Only "running" is reconciled. A months-old successful install is
        # still a successful install.
        with _StateFile():
            mi.record_metrics_state("ok", exit_code=0)
            later = datetime.now(timezone.utc) + timedelta(days=400)
            state = mi.read_metrics_state(now=later)
        self.assertEqual(state["phase"], "ok")
        self.assertTrue(state["installed"])

    def test_an_unparsable_timestamp_does_not_age_anything_out(self) -> None:
        with _StateFile() as sf:
            sf.path.write_text(
                json.dumps({"phase": "running", "at": "not a date"}), encoding="utf-8"
            )
            self.assertEqual(mi.read_metrics_state()["phase"], "running")


class TheDeployAlwaysSaysWhatHappenedToMetrics(unittest.TestCase):
    """A 1vm deploy must never end without a metrics verdict in the transcript.

    The console reads KIN_METRICS_END out of the deploy log to decide whether
    this appliance has monitoring. A run that emits no marker at all is
    indistinguishable from a transcript written before the stage existed, so
    the checklist completes and the operator is told the deploy is finished.
    That is the exact shape of the bug being closed: a deploy that could not
    start monitoring has to say so in the transcript's own vocabulary.
    """

    def test_a_metrics_install_that_never_starts_still_emits_an_end_marker(self) -> None:
        import inspect

        from kin_privhelper import commands

        src = inspect.getsource(commands.cmd_run_full_install)
        self.assertIn("KIN_METRICS_END exit=1 reason=not-started", src)
        self.assertIn('started != "started"', src)

    def test_both_failure_paths_are_covered(self) -> None:
        # run_after_install can decline to start (no event loop) or the import
        # and call can raise. Neither may leave the transcript silent.
        import inspect

        from kin_privhelper import commands

        src = inspect.getsource(commands.cmd_run_full_install)
        tail = src.split("if install_exit == 0:")[1]
        self.assertEqual(
            tail.count("KIN_METRICS_END exit=1 reason=not-started"),
            2,
            "the declined path and the raising path must both mark the transcript",
        )

    def test_the_deploy_exit_code_is_still_the_deploy_s_own(self) -> None:
        # Metrics must not turn a good mail install into a failed deploy. Mail
        # working is the thing the customer bought.
        import inspect

        from kin_privhelper import commands

        src = inspect.getsource(commands.cmd_run_full_install)
        self.assertIn("yield proto.event_done(install_exit)", src)


if __name__ == "__main__":
    unittest.main()
