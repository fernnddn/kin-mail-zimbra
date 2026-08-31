"""Replication can be dead while every other signal reads healthy.

After a partition heals badly DRBD can sit StandAlone with BOTH sides
reporting UpToDate. `drbd_both_uptodate` returns True, there is no resync
percentage, and no resource has failed, so the console called it healthy while
nothing written on the serving node was reaching the other one. Losing that
node then loses every message since the split.
"""

from __future__ import annotations

import unittest

from kin_privhelper.maintenance import drbd_both_uptodate, drbd_link_state

HEALTHY_PEER_DISK = (
    "kin-zimbra role:Primary\n"
    "  disk:UpToDate\n"
    "  mail2.example.test role:Secondary\n"
    "    peer-disk:UpToDate\n"
)
HEALTHY_REPLICATION = (
    "kin-zimbra role:Primary\n"
    "  disk:UpToDate\n"
    "  peer role:Secondary\n"
    "    replication:Established peer-disk:UpToDate\n"
)
RESYNCING = (
    "kin-zimbra role:Primary\n"
    "  disk:UpToDate\n"
    "  peer role:Secondary\n"
    "    replication:SyncSource peer-disk:Inconsistent done:32.50\n"
)
SPLIT_BRAIN_BOTH_UPTODATE = (
    "kin-zimbra role:Primary\n"
    "  disk:UpToDate\n"
    "  mail2.example.test connection:StandAlone role:Secondary\n"
    "    peer-disk:UpToDate\n"
)


class LinkStateTests(unittest.TestCase):
    def test_a_connected_pair_is_connected(self) -> None:
        self.assertEqual(drbd_link_state(HEALTHY_PEER_DISK), "connected")
        self.assertEqual(drbd_link_state(HEALTHY_REPLICATION), "connected")

    def test_a_resync_is_not_a_fault(self) -> None:
        # A new peer syncs for as long as the disk takes. That is the link
        # working, and calling it down would alarm every fresh build.
        self.assertEqual(drbd_link_state(RESYNCING), "syncing")

    def test_standalone_is_down_even_when_both_say_uptodate(self) -> None:
        # The case this whole module exists for.
        self.assertTrue(drbd_both_uptodate(SPLIT_BRAIN_BOTH_UPTODATE))
        self.assertEqual(drbd_link_state(SPLIT_BRAIN_BOTH_UPTODATE), "down")

    def test_a_peer_being_searched_for_is_down(self) -> None:
        for state in ("Connecting", "Unconnected", "Disconnecting", "StandAlone"):
            with self.subTest(state=state):
                text = f"kin-zimbra role:Primary\n  disk:UpToDate\n  peer connection:{state}\n"
                self.assertEqual(drbd_link_state(text), "down")

    def test_replication_off_is_down(self) -> None:
        text = "kin-zimbra role:Primary\n  disk:UpToDate\n  peer role:Unknown\n    replication:Off\n"
        self.assertEqual(drbd_link_state(text), "down")

    def test_a_resource_with_no_peer_at_all_is_down(self) -> None:
        self.assertEqual(
            drbd_link_state("kin-zimbra role:Primary\n  disk:UpToDate\n"), "down"
        )

    def test_no_drbd_at_all_is_not_a_fault(self) -> None:
        # A single-node appliance has no resource. Alerting there would put a
        # permanent red banner on every 1vm install.
        for text in ("", "\n", "drbdadm: command not found", "no resources defined"):
            with self.subTest(text=text):
                self.assertEqual(drbd_link_state(text), "")

    def test_it_never_throws(self) -> None:
        for text in ("\x00\xff", "role:" * 5000, "connection:", "replication:", "a" * 100000):
            with self.subTest(text=text[:20]):
                drbd_link_state(text)


class PreflightTests(unittest.TestCase):
    def test_move_master_refuses_while_the_link_is_down(self) -> None:
        import inspect

        from kin_privhelper.maintenance import run_preflight

        src = inspect.getsource(run_preflight)
        self.assertIn("drbd_replicating", src)
        # Unknown ("") must pass: a single-node host has no link to check.
        self.assertIn('("connected", "syncing", "")', src)

    def test_the_console_shows_it(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "console/frontend/src"
        alerts = (root / "tasks/alerts.ts").read_text(encoding="utf-8")
        cluster = (root / "pages/Cluster.tsx").read_text(encoding="utf-8")
        self.assertIn('cluster.drbd_link === "down"', alerts)
        self.assertIn('severity: "danger"', alerts)
        self.assertIn('cluster.drbd_link === "down"', cluster)


if __name__ == "__main__":
    unittest.main()
