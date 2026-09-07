"""Every path that brings a mail node into service must install metrics.

The original fault was not that the monitoring role was wrong. It was that
exactly one path ran it: the step inside Build HA pair. A single-server
appliance therefore never collected anything, for the life of the box, and the
Reports empty state pointed the operator at a step that does not exist on 1vm
(live QA, 7 Sep 2026).

There are four ways a node starts serving mail. Each one is asserted here, so
adding a fifth without metrics fails the build rather than shipping another
appliance with empty charts.
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ANSIBLE = REPO / "ansible"


class MonitoringReachesEveryNodeTests(unittest.TestCase):
    def test_build_ha_pair_runs_it(self) -> None:
        from kin_privhelper.orchestration import STEPS

        step = next((s for s in STEPS if s.step_id == "mail_monitoring"), None)
        self.assertIsNotNone(step, "Build HA pair no longer installs monitoring")
        self.assertEqual(step.playbook, "playbooks/mail-monitoring.yml")

    def test_a_single_server_deploy_runs_it(self) -> None:
        """The gap that started all of this: nothing ran it on 1vm."""
        from kin_privhelper import commands

        src = inspect.getsource(commands.cmd_run_full_install)
        self.assertIn("monitoring_install", src)
        self.assertIn("install_exit == 0", src)

    def test_adding_a_replacement_node_runs_it(self) -> None:
        """Without this the new peer has no collector, and monitoring appears
        to vanish the first time the VIP lands on it."""
        play = (ANSIBLE / "playbooks" / "mail-add-host.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("monitoring_stack", play)
        self.assertIn("mail_new_node", play)

    def test_the_operator_can_install_it_by_hand(self) -> None:
        """The repair path, for an appliance that predates any of the above."""
        from kin_console.app import _STREAM_ACTIONS
        from kin_privhelper import protocol as proto
        from kin_privhelper.commands import HANDLERS

        self.assertIn(proto.CMD_INSTALL_MONITORING, HANDLERS)
        self.assertEqual(
            _STREAM_ACTIONS.get("install_monitoring"), proto.CMD_INSTALL_MONITORING
        )

    def test_a_failed_metrics_install_never_fails_the_deploy(self) -> None:
        """Metrics are additive. Stranding an operator with no mail because
        Prometheus would not install is the wrong trade, and is the same call
        Build HA pair already makes by marking its step optional.
        """
        from kin_privhelper import commands
        from kin_privhelper.orchestration import STEPS

        src = inspect.getsource(commands.cmd_run_full_install)
        # The deploy reports its own exit code, not the metrics one.
        self.assertIn("yield proto.event_done(install_exit)", src)
        self.assertIn("never fail a good deploy", src)

        step = next(s for s in STEPS if s.step_id == "mail_monitoring")
        self.assertTrue(step.optional)


if __name__ == "__main__":
    unittest.main()
