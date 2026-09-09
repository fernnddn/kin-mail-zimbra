"""Monitoring must arrive with the first deploy and never need a second visit.

That was the intent already: the deploy starts the metrics install by itself.
What was missing is what happens when that attempt fails. The only remaining
route was a person pressing Install monitoring on the Monitoring tab, and when
the cause of the failure was the product itself, that operator was pressing a
button that could not work no matter how many times they tried.

That is exactly what Phase 12 QA hit. monitoring_stack used
ansible.builtin.systemd_service, which the appliance's ansible-core 2.12 cannot
resolve, so the automatic install failed with exit 4 and the manual one failed
identically. Fixing the module fixes new deployments. Appliances that already
carry the failed record need something to pick it up again.

The daemon now does, on startup, and the whole value is in how narrow it is:
never before the deploy has finished, never while an install is in flight,
never on a node that already has metrics, and never endlessly.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kin_privhelper import deploy_state
from kin_privhelper import monitoring_install as mi


class _State:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "metrics-state.json"
        self._patch = mock.patch.object(mi, "METRICS_STATE_FILE", self.path)

    def __enter__(self) -> "_State":
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()
        self._tmp.cleanup()


def _decide(*, deployed: bool = True):
    with mock.patch.object(deploy_state, "is_full_install_complete", lambda: deployed):
        return mi.auto_install_decision()


class ItOnlyRunsWhenItShould(unittest.TestCase):
    def test_it_does_not_run_before_the_first_deploy_finishes(self) -> None:
        # Mid-install the deploy is already driving its own metrics step.
        with _State():
            go, why = _decide(deployed=False)
        self.assertFalse(go)
        self.assertIn("has not finished", why)

    def test_it_repairs_a_failed_install(self) -> None:
        # The Phase 12 appliance: deploy finished, metrics failed, operator
        # left pressing a button.
        with _State():
            mi.record_metrics_state("failed", exit_code=4, reason="module not found")
            go, why = _decide()
        self.assertTrue(go)
        self.assertIn("failed", why)

    def test_it_repairs_a_timeout_and_a_failure_to_start(self) -> None:
        for phase in ("timeout", "error"):
            with self.subTest(phase=phase), _State():
                mi.record_metrics_state(phase)
                go, _why = _decide()
                self.assertTrue(go, f"{phase} must be picked up")

    def test_it_installs_on_an_appliance_that_never_recorded_anything(self) -> None:
        # Upgraded from a build that predates the record. Deploy is done and
        # nothing proves metrics exist, so one attempt is right.
        with _State():
            go, _why = _decide()
        self.assertTrue(go)

    def test_it_never_touches_a_working_node(self) -> None:
        with _State():
            mi.record_metrics_state("ok", exit_code=0)
            go, why = _decide()
        self.assertFalse(go)
        self.assertIn("already installed", why)

    def test_it_does_not_start_a_second_install_over_a_running_one(self) -> None:
        with _State():
            mi.record_metrics_state("running")
            go, why = _decide()
        self.assertFalse(go)
        self.assertIn("already running", why)


class ItGivesUpRatherThanRetryingForEver(unittest.TestCase):
    """A hopeless install must not hold the maintenance lock on every restart.

    The install takes the lock for its whole run and the ceiling is thirty
    minutes. A crash looping daemon retrying that endlessly would block Enter
    Maintenance and every other cluster operation, which is a worse failure
    than the missing metrics.
    """

    def test_the_budget_is_spent_and_then_it_stops(self) -> None:
        with _State():
            for n in range(mi.MAX_AUTO_ATTEMPTS):
                mi.record_metrics_state("failed", exit_code=4, attempts=n)
                go, _why = _decide()
                self.assertTrue(go, f"attempt {n + 1} should still be allowed")
            mi.record_metrics_state("failed", exit_code=4, attempts=mi.MAX_AUTO_ATTEMPTS)
            go, why = _decide()
        self.assertFalse(go)
        self.assertIn("Install monitoring", why, "it must say what a person can do")

    def test_the_attempt_is_counted_before_the_run_not_after(self) -> None:
        # A daemon that dies mid-install still has to consume its budget,
        # otherwise a crash loop never exhausts it.
        with _State():
            mi.record_metrics_state("failed", exit_code=4, attempts=0)
            with mock.patch.object(
                deploy_state, "is_full_install_complete", lambda: True
            ), mock.patch.object(mi, "run_after_install", lambda: "started"):
                result = mi.resume_after_restart()
            after = mi.read_metrics_state()
        self.assertIn("starting", result)
        self.assertEqual(after["auto_attempts"], 1)
        self.assertEqual(after["phase"], "running")

    def test_a_success_clears_the_budget(self) -> None:
        with _State():
            mi.record_metrics_state("failed", exit_code=4, attempts=2)
            mi.record_metrics_state("ok", exit_code=0, attempts=0)
            state = mi.read_metrics_state()
        self.assertEqual(state["auto_attempts"], 0)
        self.assertTrue(state["installed"])

    def test_recording_an_outcome_does_not_silently_reset_the_budget(self) -> None:
        with _State():
            mi.record_metrics_state("failed", exit_code=4, attempts=2)
            mi.record_metrics_state("failed", exit_code=4)  # no explicit count
            self.assertEqual(mi.read_metrics_state()["auto_attempts"], 2)


class TheDaemonActuallyCallsIt(unittest.TestCase):
    def test_startup_runs_the_check(self) -> None:
        import inspect

        from kin_privhelper import daemon

        src = inspect.getsource(daemon.run)
        self.assertIn("resume_after_restart", src)

    def test_it_runs_after_the_socket_is_listening(self) -> None:
        # It must not delay accepting connections; the console polls this
        # daemon and a slow startup reads as an outage.
        import inspect

        from kin_privhelper import daemon

        src = inspect.getsource(daemon.run)
        self.assertLess(
            src.index("start_unix_server"),
            src.index("resume_after_restart"),
            "the metrics check must not run before the socket is up",
        )

    def test_startup_survives_the_check_failing(self) -> None:
        import inspect

        from kin_privhelper import daemon

        src = inspect.getsource(daemon.run)
        tail = src[src.index("resume_after_restart") - 400 :]
        self.assertIn("except Exception", tail)


if __name__ == "__main__":
    unittest.main()
