"""Arming SBD must be as guarded as disarming it was.

The disarm path stops sbd one node at a time and checks /proc boot_id either
side of the watchdog window, so a node that self-fenced stops the loop instead
of being followed by its peer. That guard was written after a real fence
incident (Phase 8, 29 Aug 2026).

The rearm path had no equivalent. `start_one.yml` was a single
`systemctl start sbd` with no readiness wait and no boot_id check, so if
arming the new LUN panicked the first mail node, the loop went straight on to
arm the second and took mail down on both - during the very operation meant to
restore fencing.

These tests pin the guard, and pin the filter_plugins symlink that makes
`kin_boot_id_unchanged` resolvable from the rearm role. Ansible only loads a
role's plugin directory for roles used in the play, and the add-observability
playbook does not include observability_disarm, so without that link the
assertion below would fail at runtime with an undefined-filter error rather
than protecting anything.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROLES = REPO / "ansible/roles"
START_ONE = ROLES / "observability_rearm/tasks/start_one.yml"
STOP_ONE = ROLES / "observability_disarm/tasks/stop_one.yml"
REARM_FILTER = ROLES / "observability_rearm/filter_plugins/boot_id.py"
DISARM_FILTER = ROLES / "observability_disarm/filter_plugins/boot_id.py"
ADD_PLAYBOOK = REPO / "ansible/playbooks/mail-add-observability.yml"
REARM_DEFAULTS = ROLES / "observability_rearm/defaults/main.yml"


class ArmingIsGuarded(unittest.TestCase):
    def setUp(self) -> None:
        self.start = START_ONE.read_text()

    def test_boot_id_is_read_either_side_of_arming(self) -> None:
        self.assertEqual(
            self.start.count("/proc/sys/kernel/random/boot_id"),
            2,
            "arming must read boot_id before and after, as the stop loop does",
        )

    def test_a_self_fence_stops_the_loop(self) -> None:
        self.assertIn("kin_boot_id_unchanged", self.start)
        self.assertIn("ansible.builtin.assert", self.start)

    def test_the_next_node_is_not_armed_after_a_reboot(self) -> None:
        # The failure message is what the operator acts on at 2am; it has to
        # say that the remaining node was deliberately left alone.
        self.assertIn("Refusing to arm the remaining node", self.start)

    def test_arming_waits_for_sbd_to_be_active(self) -> None:
        self.assertIn("until:", self.start)
        self.assertIn("is-active sbd", self.start)

    def test_the_wait_is_long_enough_for_sbd_delay_start(self) -> None:
        # SBD_DELAY_START can hold the unit activating for ~70s. A ceiling
        # below that would fail a healthy rearm.
        text = REARM_DEFAULTS.read_text()
        retries = int(text.split("observability_arm_retries:")[1].split("\n")[0])
        delay = int(text.split("observability_rearm_wait_sec:")[1].split("\n")[0])
        self.assertGreaterEqual(
            retries * delay, 90, "the arming wait must exceed SBD_DELAY_START"
        )


class FilterIsResolvableFromRearm(unittest.TestCase):
    def test_rearm_has_its_own_filter_entry(self) -> None:
        self.assertTrue(
            REARM_FILTER.exists(),
            "observability_rearm needs boot_id.py; the add playbook does not "
            "include observability_disarm, so its plugins are not loaded",
        )

    def test_it_is_the_same_file_not_a_copy(self) -> None:
        self.assertTrue(REARM_FILTER.is_symlink(), "link it, do not copy it")
        self.assertEqual(
            REARM_FILTER.resolve(),
            DISARM_FILTER.resolve(),
            "the two roles must share one boot_id filter",
        )

    def test_the_link_target_exists(self) -> None:
        self.assertTrue(REARM_FILTER.resolve().is_file())

    def test_the_add_playbook_does_not_load_the_disarm_role(self) -> None:
        # If this ever changes, the symlink above stops being load-bearing -
        # but so does the reason this test file exists, so make it explicit.
        self.assertNotIn("observability_disarm", ADD_PLAYBOOK.read_text())

    def test_the_filter_still_fails_closed(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "kin_boot_id_rearm_probe", REARM_FILTER.resolve()
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = mod.FilterModule().filters()["kin_boot_id_unchanged"]
        same = "11111111-2222-4333-8444-555555555555"
        self.assertTrue(fn(same, same))
        self.assertFalse(fn(same, "11111111-2222-4333-8444-555555555556"))
        # An unreadable boot_id must not be treated as "did not reboot".
        self.assertFalse(fn("", same))
        self.assertFalse(fn(same, ""))


class DisarmGuardStillStands(unittest.TestCase):
    """The guard this one was modelled on must not quietly disappear."""

    def test_stop_loop_still_checks_boot_id(self) -> None:
        text = STOP_ONE.read_text()
        self.assertIn("kin_boot_id_unchanged", text)
        self.assertEqual(text.count("/proc/sys/kernel/random/boot_id"), 2)


if __name__ == "__main__":
    unittest.main()
