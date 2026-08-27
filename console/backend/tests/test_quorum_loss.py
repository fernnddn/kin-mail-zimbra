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
