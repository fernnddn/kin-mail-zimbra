"""An additive step must never be able to fail a Build HA pair.

mail_monitoring installs Prometheus. It touches no cluster state and runs last
of the applying steps, so a package or service hiccup there must be reported
loudly and then stepped over - stranding the operator with a half-built pair
and no mail because the metrics charts would not install is the wrong trade
(pre-Phase-5 audit, 27 Aug 2026).
"""

from __future__ import annotations

import unittest

from kin_privhelper.orchestration import STEPS


class OptionalStepTests(unittest.TestCase):
    def test_monitoring_is_the_only_optional_step(self) -> None:
        optional = [s.step_id for s in STEPS if getattr(s, "optional", False)]
        self.assertEqual(optional, ["mail_monitoring"])

    def test_every_cluster_step_is_still_mandatory(self) -> None:
        # Anything that builds or joins the cluster must keep aborting the run.
        for step in STEPS:
            if step.step_id == "mail_monitoring":
                continue
            self.assertFalse(
                getattr(step, "optional", False),
                f"{step.step_id} must not be optional",
            )

    def test_monitoring_runs_after_the_cluster_is_built(self) -> None:
        ids = [s.step_id for s in STEPS]
        self.assertLess(ids.index("mail_pacemaker_stack"), ids.index("mail_monitoring"))
        self.assertLess(ids.index("mail_drbd_activate"), ids.index("mail_monitoring"))

    def test_monitoring_is_not_a_cluster_join_step(self) -> None:
        step = next(s for s in STEPS if s.step_id == "mail_monitoring")
        self.assertFalse(step.cluster_join)
        self.assertEqual(step.playbook, "playbooks/mail-monitoring.yml")

    def test_the_optional_branch_clears_the_exit_code(self) -> None:
        # The pipeline reports proto.event_done(exit_code) at the end; leaving a
        # non-zero exit_code behind would report the whole run as failed even
        # though it was allowed to continue.
        import inspect

        from kin_privhelper import orchestration

        src = inspect.getsource(orchestration)
        marker = "if exit_code != 0 and step.optional:"
        self.assertIn(marker, src)
        after = src.split(marker, 1)[1].split("if exit_code != 0:", 1)[0]
        self.assertIn("exit_code = 0", after)
        self.assertIn("continue", after)


if __name__ == "__main__":
    unittest.main()
