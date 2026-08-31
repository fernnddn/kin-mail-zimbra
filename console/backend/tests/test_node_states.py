"""Node state must be right in every topology, not just the two-node one.

A state that is wrong on a single-node appliance is wrong on every appliance
KIN Mail ships to a customer who bought one server.
"""

from __future__ import annotations

import unittest

from kin_privhelper.maintenance import (
    crm_node_list_as_pcs_nodes,
    parse_maintenance_provenance,
    parse_offline_nodes,
    parse_online_nodes,
    parse_rejoining_nodes,
    parse_standby_nodes,
)


class RejoiningTests(unittest.TestCase):
    def _f(self, **kw):
        base = dict(
            nodes=["a", "b"],
            promoted_names=["a"],
            unpromoted=["b"],
            standby=[],
            offline=[],
        )
        base.update(kw)
        return parse_rejoining_nodes(**base)

    def test_a_healthy_pair_has_nobody_rejoining(self) -> None:
        self.assertEqual(self._f(), [])

    def test_a_node_that_is_back_but_has_no_role_yet_is_rejoining(self) -> None:
        # The power-on window: Pacemaker counts it as a member, DRBD has not
        # given it a role. Neither healthy nor failed.
        self.assertEqual(self._f(unpromoted=[]), ["b"])

    def test_a_single_node_appliance_is_never_rejoining(self) -> None:
        # No promotable clone exists at all, so "has no role" is the normal
        # and permanent state. Reporting it as rejoining would leave every 1vm
        # customer looking at a node that is forever coming back from nowhere.
        self.assertEqual(
            parse_rejoining_nodes(
                nodes=["a"], promoted_names=[], unpromoted=[], standby=[], offline=[]
            ),
            [],
        )

    def test_a_pair_being_built_is_not_rejoining(self) -> None:
        # Before the clone is promoted for the first time, neither node has a
        # role, and neither is coming back from anywhere.
        self.assertEqual(self._f(promoted_names=[], unpromoted=[]), [])

    def test_maintenance_beats_rejoining(self) -> None:
        self.assertEqual(self._f(unpromoted=[], standby=["b"]), [])

    def test_offline_beats_rejoining(self) -> None:
        self.assertEqual(self._f(nodes=["a"], unpromoted=[], offline=["b"]), [])


class LostNodeFallbackTests(unittest.TestCase):
    """`pcs status nodes` fails while Pacemaker is starting. What then?"""

    CRM_NODE_L = (
        "1084752425 mail.example.test member\n"
        "1084752423 mail2.example.test lost\n"
    )

    def test_a_lost_node_is_reported_offline_not_online(self) -> None:
        shaped = crm_node_list_as_pcs_nodes(self.CRM_NODE_L)
        self.assertEqual(parse_online_nodes(shaped), ["mail.example.test"])
        self.assertEqual(parse_offline_nodes(shaped), ["mail2.example.test"])
        self.assertEqual(parse_standby_nodes(shaped), [])

    def test_junk_yields_nothing_rather_than_a_wrong_answer(self) -> None:
        for junk in ("", "\n", "not a node list", "1 2 3", "x y member"):
            with self.subTest(junk=junk):
                self.assertEqual(crm_node_list_as_pcs_nodes(junk), "")

    def test_a_short_line_is_ignored(self) -> None:
        self.assertEqual(crm_node_list_as_pcs_nodes("123\n"), "")

    def test_states_other_than_member_are_treated_as_down(self) -> None:
        # Anything Pacemaker does not call `member` has not joined.
        for state in ("lost", "unclean", "pending", ""):
            with self.subTest(state=state):
                shaped = crm_node_list_as_pcs_nodes(f"1 a.example.test {state}\n")
                self.assertEqual(parse_online_nodes(shaped), [] if state != "member" else ["a.example.test"])


class MaintenanceProvenanceTests(unittest.TestCase):
    LOG = (
        "=== enter mail2.example.test - 2026-08-30 09:02:10 UTC ===\n"
        "pcs node standby mail2.example.test\n"
        "=== enter finished, exit 0 ===\n"
    )

    def test_a_console_entered_maintenance_is_dated(self) -> None:
        since, source = parse_maintenance_provenance(self.LOG, ["mail2.example.test"])
        self.assertEqual(source, "console")
        self.assertEqual(since, "2026-08-30 09:02:10 UTC")

    def test_standby_with_no_record_is_called_out(self) -> None:
        # Standby survives a reboot, so this is how a leftover is told apart
        # from a deliberate one.
        since, source = parse_maintenance_provenance("", ["mail2.example.test"])
        self.assertEqual((since, source), ("", "unknown"))

    def test_an_exited_node_that_is_standby_again_is_unexplained(self) -> None:
        log = self.LOG + "=== exit mail2.example.test - 2026-08-30 09:40:00 UTC ===\n"
        _since, source = parse_maintenance_provenance(log, ["mail2.example.test"])
        self.assertEqual(source, "unknown")

    def test_nothing_in_standby_reports_nothing(self) -> None:
        self.assertEqual(parse_maintenance_provenance(self.LOG, []), ("", ""))

    def test_a_corrupt_transcript_does_not_throw(self) -> None:
        for junk in ("=== ===", "=== enter ===", "\x00\xff", "=" * 10000):
            with self.subTest(junk=junk):
                parse_maintenance_provenance(junk, ["a"])


if __name__ == "__main__":
    unittest.main()
