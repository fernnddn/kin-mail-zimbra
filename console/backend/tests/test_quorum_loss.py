"""Both-down scenario: peer mail node AND the Observability witness gone.

2 mail nodes (1 vote each) + qdevice (1 vote) = 3 votes, quorum needs 2. Losing
the peer and the witness at the same time leaves the survivor on 1 vote, so
no-quorum-policy=stop correctly stops mail on a node that is otherwise
perfectly healthy. That default is what prevents split-brain and must stay -
but the console showed nothing at all about it, so the operator had no cause
and no way back (Phase 4-1 extreme-scenario review, 26 Aug 2026).
"""

from __future__ import annotations

import unittest

from kin_privhelper.maintenance import (
    parse_quorate,
    parse_vote_totals,
    quorum_recovery_hint,
)

QUORATE = """
Quorum information
------------------
Quorate:          Yes

Votequorum information
----------------------
Expected votes:   3
Total votes:      3
Quorum:           2
"""

LOST = """
Quorum information
------------------
Quorate:          No

Votequorum information
----------------------
Expected votes:   3
Total votes:      1
Quorum:           2 Activity blocked
"""


class QuorumParseTests(unittest.TestCase):
    def test_quorate_yes_no_and_unreadable(self) -> None:
        self.assertIs(parse_quorate(QUORATE), True)
        self.assertIs(parse_quorate(LOST), False)
        # corosync down => `pcs quorum status` prints nothing. "Cannot tell"
        # must never render as healthy.
        self.assertIsNone(parse_quorate(""))
        self.assertIsNone(parse_quorate("Error: cluster is not currently running"))

    def test_vote_totals(self) -> None:
        self.assertEqual(parse_vote_totals(QUORATE), (3, 2))
        self.assertEqual(parse_vote_totals(LOST), (1, 2))
        self.assertEqual(parse_vote_totals(""), (None, None))


class QuorumHintTests(unittest.TestCase):
    def test_healthy_cluster_gets_no_hint(self) -> None:
        self.assertEqual(
            quorum_recovery_hint(quorate=True, peer_offline=False, observability_reachable=True),
            "",
        )

    def test_unknown_quorum_does_not_invent_a_hint(self) -> None:
        self.assertEqual(
            quorum_recovery_hint(quorate=None, peer_offline=True, observability_reachable=False),
            "",
        )

    def test_both_down_names_the_cause_and_the_safe_way_back(self) -> None:
        hint = quorum_recovery_hint(
            quorate=False, peer_offline=True, observability_reachable=False
        )
        low = hint.lower()
        self.assertIn("1 of 3 votes", low)
        # The recoverable path comes first: bring either one back.
        self.assertIn("bring back either", low)
        # And the dangerous override must carry its split-brain warning.
        self.assertIn("quorum unblock", low)
        self.assertIn("split-brain", low)

    def test_peer_only_and_witness_only_are_distinct(self) -> None:
        peer = quorum_recovery_hint(
            quorate=False, peer_offline=True, observability_reachable=True
        )
        witness = quorum_recovery_hint(
            quorate=False, peer_offline=False, observability_reachable=False
        )
        both = quorum_recovery_hint(
            quorate=False, peer_offline=True, observability_reachable=False
        )
        partition = quorum_recovery_hint(
            quorate=False, peer_offline=False, observability_reachable=True
        )
        msgs = [peer, witness, both, partition]
        self.assertEqual(len(set(msgs)), 4, f"hints collapsed: {msgs}")
        for m in msgs:
            self.assertTrue(m.strip())

    def test_only_the_both_down_case_offers_the_forced_override(self) -> None:
        # Suggesting `quorum unblock` while the witness is still alive would be
        # wrong advice: fixing the witness is the safe fix there.
        for peer_offline, obs_ok in ((True, True), (False, False), (False, True)):
            hint = quorum_recovery_hint(
                quorate=False,
                peer_offline=peer_offline,
                observability_reachable=obs_ok,
            )
            self.assertNotIn("unblock", hint.lower())


if __name__ == "__main__":
    unittest.main()


class SurviveAlonePolicyTests(unittest.TestCase):
    """The appliance must keep delivering mail on the last node standing.

    Operator decision (27 Aug 2026): if the peer AND the Observability witness
    are both gone, host A must keep serving rather than stopping. That is
    no-quorum-policy=ignore, set by the sbd_stonith role once fencing is armed.
    The console must then describe the situation as degraded-but-serving, not
    as an outage.
    """

    def test_parses_the_live_policy(self) -> None:
        from kin_privhelper.maintenance import parse_no_quorum_policy

        self.assertEqual(
            parse_no_quorum_policy("Cluster Properties:\n no-quorum-policy: ignore\n"),
            "ignore",
        )
        self.assertEqual(parse_no_quorum_policy(" no-quorum-policy=stop"), "stop")
        self.assertEqual(parse_no_quorum_policy("stonith-enabled: true"), "")
        self.assertEqual(parse_no_quorum_policy(""), "")

    def test_alone_with_ignore_reads_as_still_serving(self) -> None:
        hint = quorum_recovery_hint(
            quorate=False,
            peer_offline=True,
            observability_reachable=False,
            no_quorum_policy="ignore",
        )
        low = hint.lower()
        self.assertIn("keeps running", low)
        # It must NOT tell the operator mail has stopped, and must not send
        # them to a forced override they no longer need.
        self.assertNotIn("has stopped mail", low)
        self.assertNotIn("unblock", low)
        # But it must still say redundancy and fencing are gone.
        self.assertIn("no failover target", low)
        self.assertIn("fencing", low)

    def test_stop_policy_still_reports_the_outage_and_the_override(self) -> None:
        hint = quorum_recovery_hint(
            quorate=False,
            peer_offline=True,
            observability_reachable=False,
            no_quorum_policy="stop",
        )
        low = hint.lower()
        self.assertIn("stopped mail", low)
        self.assertIn("unblock", low)

    def test_default_argument_keeps_the_conservative_wording(self) -> None:
        # An older node whose `pcs property` could not be read must not be
        # described as "still serving" when we cannot prove it is.
        hint = quorum_recovery_hint(
            quorate=False, peer_offline=True, observability_reachable=False
        )
        self.assertIn("stopped mail", hint.lower())

    def test_ansible_sets_the_policy_where_fencing_is_armed(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        stonith = (root / "ansible/roles/sbd_stonith/tasks/stonith.yml").read_text()
        defaults = (root / "ansible/roles/sbd_stonith/defaults/main.yml").read_text()
        self.assertIn("no-quorum-policy={{ sbd_stonith_no_quorum_policy }}", stonith)
        self.assertIn("sbd_stonith_no_quorum_policy: ignore", defaults)
        # The trade-off must stay documented next to the value.
        self.assertIn("after-sb-2pri", defaults)
