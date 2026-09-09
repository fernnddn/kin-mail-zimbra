"""The metrics verdict has to survive nobody listening.

The install that matters most is the one no one is watching. The deploy starts
it detached so that an SSE drop cannot kill it, and the draining loop then
discards every event it produces.

maintenance._emit only builds an event for the stream. It does not write to the
deploy transcript; only _stream_redacted does, and only for the subprocess
output it is wrapping. So on an automatic install the two markers the console
reads, KIN_METRICS_BEGIN and KIN_METRICS_END, were thrown away, and the deploy
log contained the ansible output with no verdict anywhere in it.

The console then saw no marker, correctly concluded that this transcript
predated the metrics stage, and showed a finished green deploy over an install
that had failed. Phase 13 QA is exactly that: mail-monitoring.yml runs at
12:48:26 in the deploy log, fails on the collector, and there is no
KIN_METRICS_END to be found.

These tests drive the command with a canned playbook result and assert on the
FILE, because the file is what the console reads on a reload, in a second tab,
and every time anybody asks later what happened.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kin_privhelper import deploy_state
from kin_privhelper import monitoring_install as mi


class _Appliance:
    """A scratch transcript and metrics record, wired where the code looks."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.transcript = root / "deploy-last.log"
        self.state = root / "metrics-state.json"
        self._patches = [
            mock.patch.object(deploy_state, "DEPLOY_LAST_LOG", self.transcript),
            mock.patch.object(mi, "METRICS_STATE_FILE", self.state),
        ]

    def __enter__(self) -> "_Appliance":
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def text(self) -> str:
        return self.transcript.read_text(encoding="utf-8") if self.transcript.is_file() else ""


def _run_install(exit_code: int, *, listener: bool) -> None:
    """Drive cmd_install_monitoring with a canned ansible result.

    listener=False reproduces the detached deploy: the events go nowhere.
    """

    async def fake_stream(*_a: object, **_k: object):
        yield {"type": "stdout", "data": "PLAY RECAP\n"}
        yield {"type": "done", "exit_code": exit_code}

    async def go() -> None:
        async for _ev in mi.cmd_install_monitoring({}):
            if listener:
                pass  # a console is watching; still nothing to do here

    # Patched on the SOURCE modules, not on monitoring_install. Every one of
    # these is imported inside the command body, so it is looked up on its own
    # module at call time; patching the importer does nothing at all and the
    # test passes or fails for reasons unrelated to what it claims to check.
    from kin_privhelper import maintenance, orchestration

    with mock.patch.object(orchestration, "_stream_redacted", fake_stream), mock.patch.object(
        maintenance, "try_lock_maintenance", lambda: object()
    ), mock.patch.object(
        maintenance, "release_maintenance_lock", lambda _fh: None
    ), mock.patch.object(
        maintenance, "this_hostname", lambda: "mail.example.test"
    ), mock.patch.object(
        orchestration, "_write_work_files", lambda _inv, work: Path(work)
    ), mock.patch.object(
        orchestration, "_playbook_path", lambda _p: Path("/nonexistent/mail-monitoring.yml")
    ), mock.patch.object(
        orchestration, "_ansible_bin", lambda: "/bin/true"
    ), mock.patch.object(
        orchestration, "kin_mail_deploy_dir", lambda: "/opt/kin-mail-deploy"
    ):
        asyncio.run(go())


class TheMarkersAreWrittenNotJustYielded(unittest.TestCase):
    def test_emit_writes_the_line_into_the_transcript(self) -> None:
        with _Appliance() as box:
            asyncio.run(mi._emit("KIN_METRICS_BEGIN host=mail.example.test"))
            self.assertIn("KIN_METRICS_BEGIN host=mail.example.test", box.text())

    def test_it_appends_rather_than_replacing_the_deploy_log(self) -> None:
        # The install runs after the deploy has written its own transcript.
        # Truncating it would erase the record of the install that just ran.
        with _Appliance() as box:
            box.transcript.write_text("==> Full install complete\n", encoding="utf-8")
            asyncio.run(mi._emit("KIN_METRICS_BEGIN host=mail.example.test"))
            text = box.text()
        self.assertIn("Full install complete", text)
        self.assertIn("KIN_METRICS_BEGIN", text)

    def test_a_line_always_ends_with_a_newline(self) -> None:
        # The console parses whole lines; a marker glued to the next line is a
        # marker it cannot match.
        with _Appliance() as box:
            asyncio.run(mi._emit("KIN_METRICS_END exit=0"))
            self.assertTrue(box.text().endswith("\n"))

    def test_an_unwritable_transcript_does_not_stop_the_install(self) -> None:
        with mock.patch.object(
            deploy_state, "DEPLOY_LAST_LOG", Path("/proc/x/y/deploy.log")
        ):
            event = asyncio.run(mi._emit("KIN_METRICS_END exit=0"))
        self.assertEqual(event.get("type"), "stdout")


class ADetachedInstallStillLeavesAVerdict(unittest.TestCase):
    """The Phase 13 case: nobody is listening and the install fails."""

    def test_a_failed_detached_install_writes_begin_and_end(self) -> None:
        with _Appliance() as box:
            _run_install(2, listener=False)
            text = box.text()
        self.assertIn("KIN_METRICS_BEGIN", text)
        self.assertIn("KIN_METRICS_END exit=2", text)

    def test_a_successful_detached_install_writes_exit_zero(self) -> None:
        with _Appliance() as box:
            _run_install(0, listener=False)
            text = box.text()
        self.assertIn("KIN_METRICS_END exit=0", text)

    def test_the_transcript_says_the_same_thing_whether_or_not_anyone_watched(self) -> None:
        # The whole point: an automatic install and a button press must leave
        # the same record, because the record is what the console reads.
        with _Appliance() as unwatched:
            _run_install(2, listener=False)
            detached = [ln for ln in unwatched.text().splitlines() if "KIN_METRICS" in ln]
        with _Appliance() as watched:
            _run_install(2, listener=True)
            manual = [ln for ln in watched.text().splitlines() if "KIN_METRICS" in ln]
        self.assertEqual(detached, manual)
        self.assertTrue(detached, "no markers were recorded at all")


class TheConsoleCanReadTheVerdictBack(unittest.TestCase):
    """What the frontend parser would make of the resulting transcript."""

    @staticmethod
    def _stage(log: str) -> str:
        # Mirrors parseMetricsStage in deployPipeline.ts.
        import re

        end = re.search(r"KIN_METRICS_END exit=(\d+)", log)
        if end:
            return "ok" if end.group(1) == "0" else "failed"
        return "pending" if "KIN_METRICS_BEGIN" in log else "absent"

    def test_a_failed_install_reads_as_failed_not_absent(self) -> None:
        # "absent" is treated as a transcript from before this stage existed,
        # which completes the checklist. That is what turned a failed install
        # into a green deploy.
        with _Appliance() as box:
            box.transcript.write_text("==> Full install complete\n", encoding="utf-8")
            _run_install(2, listener=False)
            stage = self._stage(box.text())
        self.assertEqual(stage, "failed")

    def test_a_good_install_reads_as_ok(self) -> None:
        with _Appliance() as box:
            box.transcript.write_text("==> Full install complete\n", encoding="utf-8")
            _run_install(0, listener=False)
            stage = self._stage(box.text())
        self.assertEqual(stage, "ok")


if __name__ == "__main__":
    unittest.main()
