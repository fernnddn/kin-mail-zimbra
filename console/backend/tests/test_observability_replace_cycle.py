"""Remove an unreachable Observability VM, then add a replacement.

The operator's requirement, in their words: if the witness is unreachable we
must be able to delete it and add a new one. That is a cycle, and each step
gates on the state the previous one left behind, so it is worth walking the
whole loop rather than testing the two planners apart.

The awkward state is the middle one: qnetd down but the VM still running. The
console gates the Remove button on the qnetd probe alone, so the button is
enabled and the Cluster page says "unreachable" - and the command still has to
refuse, because forced cleanup against a live VM leaves it holding a stale SBD
LUN and qnetd identity. What it must not do is refuse with a sentence that
contradicts what the operator is looking at.
"""

from __future__ import annotations

import unittest

from kin_privhelper.observability_lifecycle import (
    plan_add_observability,
    plan_remove_observability,
)
from kin_privhelper.observability_status import snapshot_from_texts

WITNESS_IP = "198.51.100.53"
NEW_IP = "198.51.100.54"

WITH_QNETD = (
    "quorum {\n"
    "    provider: corosync_votequorum\n"
    "    device {\n"
    "        model: net\n"
    "        net {\n"
    f"            host: {WITNESS_IP}\n"
    "        }\n"
    "    }\n"
    "}\n"
)
WITHOUT_QNETD = "quorum {\n    provider: corosync_votequorum\n}\n"


class TheCycle(unittest.TestCase):
    def test_a_healthy_witness_can_be_neither_added_nor_removed(self) -> None:
        snap = snapshot_from_texts(
            config_ip=WITNESS_IP, corosync_conf=WITH_QNETD, reachable=True
        )
        self.assertEqual(snap["status"], "healthy")
        self.assertFalse(snap["can_remove"])
        self.assertFalse(snap["can_add"])

    def test_step_1_an_unreachable_witness_offers_remove(self) -> None:
        snap = snapshot_from_texts(
            config_ip=WITNESS_IP, corosync_conf=WITH_QNETD, reachable=False
        )
        self.assertEqual(snap["status"], "unreachable")
        self.assertTrue(snap["can_remove"])
        self.assertFalse(snap["can_add"], "cannot add while one is still configured")

    def test_step_2_remove_proceeds_once_the_vm_is_really_gone(self) -> None:
        plan = plan_remove_observability(
            identity=WITNESS_IP, reachable=False, ssh_ok=False
        )
        self.assertEqual(plan.errors, ())
        self.assertTrue(plan.notes, "the operator should be told what is about to run")
        self.assertIn("forced cleanup", " ".join(plan.notes).lower())

    def test_step_3_after_remove_the_console_offers_add(self) -> None:
        # What the disarm play leaves behind: no quorum.device, no IP.
        snap = snapshot_from_texts(
            config_ip="", corosync_conf=WITHOUT_QNETD, reachable=False
        )
        self.assertEqual(snap["status"], "absent")
        self.assertTrue(snap["can_add"])
        self.assertFalse(snap["can_remove"])

    def test_step_4_a_replacement_can_be_added(self) -> None:
        plan = plan_add_observability(
            current_identity="",
            current_reachable=False,
            new_ip=NEW_IP,
            hostname="observability.example.test",
            has_credentials=True,
        )
        self.assertEqual(plan.errors, ())
        self.assertEqual(plan.new_ip, NEW_IP)

    def test_the_whole_loop_returns_to_where_it_started(self) -> None:
        after = snapshot_from_texts(
            config_ip=NEW_IP,
            corosync_conf=WITH_QNETD.replace(WITNESS_IP, NEW_IP),
            reachable=True,
        )
        self.assertEqual(after["status"], "healthy")
        self.assertEqual(after["ip"], NEW_IP)
        self.assertFalse(after["can_add"])
        self.assertFalse(after["can_remove"])


class RefusalsSayWhatIsActuallyTrue(unittest.TestCase):
    def _only_error(self, **kw: object) -> str:
        plan = plan_remove_observability(**kw)  # type: ignore[arg-type]
        self.assertEqual(len(plan.errors), 1, f"expected one message, got {plan.errors}")
        return plan.errors[0]

    def test_a_live_witness_is_refused_for_being_live(self) -> None:
        msg = self._only_error(identity=WITNESS_IP, reachable=True, ssh_ok=True)
        self.assertIn("still answering on the qnetd port", msg)
        self.assertIn(WITNESS_IP, msg)

    def test_qnetd_down_but_vm_up_does_not_claim_qnetd_is_answering(self) -> None:
        # The console has already told the operator this witness is
        # unreachable. A refusal saying it "still answers on the qnetd port"
        # reads as a contradiction and leaves them with nowhere to go.
        msg = self._only_error(identity=WITNESS_IP, reachable=False, ssh_ok=True)
        self.assertIn("qnetd is down", msg)
        self.assertIn("still running", msg)
        self.assertNotIn("still answering on the qnetd port", msg)
        self.assertIn("Power the VM off", msg)

    def test_nothing_configured_gives_exactly_one_reason(self) -> None:
        # This used to produce two errors, the second of which claimed a
        # witness that does not exist was still answering.
        msg = self._only_error(identity="", reachable=False, ssh_ok=False)
        self.assertIn("nothing to remove", msg)

    def test_every_refusal_names_an_action(self) -> None:
        for kw in (
            {"identity": WITNESS_IP, "reachable": True, "ssh_ok": True},
            {"identity": WITNESS_IP, "reachable": False, "ssh_ok": True},
        ):
            with self.subTest(**kw):
                msg = self._only_error(**kw)
                self.assertRegex(msg, r"Power (off|the VM off)")


class AddIsGatedOnRemoveHavingHappened(unittest.TestCase):
    def test_add_is_refused_while_a_witness_is_still_configured(self) -> None:
        for reachable in (True, False):
            with self.subTest(reachable=reachable):
                plan = plan_add_observability(
                    current_identity=WITNESS_IP,
                    current_reachable=reachable,
                    new_ip=NEW_IP,
                    has_credentials=True,
                )
                self.assertTrue(plan.errors)
                self.assertIn("Remove it first", " ".join(plan.errors))

    def test_add_refuses_a_bad_address(self) -> None:
        for bad in ("", "not-an-ip", "999.1.1.1", "198.51.100.54/24", "::1"):
            with self.subTest(ip=bad):
                plan = plan_add_observability(
                    current_identity="",
                    current_reachable=False,
                    new_ip=bad,
                    has_credentials=True,
                )
                self.assertTrue(plan.errors, f"accepted {bad!r}")

    def test_add_refuses_without_credentials(self) -> None:
        plan = plan_add_observability(
            current_identity="",
            current_reachable=False,
            new_ip=NEW_IP,
            has_credentials=False,
        )
        self.assertTrue(any("password" in e for e in plan.errors))

    def test_add_refuses_a_bad_hostname(self) -> None:
        plan = plan_add_observability(
            current_identity="",
            current_reachable=False,
            new_ip=NEW_IP,
            hostname="not a hostname!",
            has_credentials=True,
        )
        self.assertTrue(any("hostname" in e for e in plan.errors))


if __name__ == "__main__":
    unittest.main()
