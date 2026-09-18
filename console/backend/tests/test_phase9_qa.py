"""Fixes from the Phase 9 QA round, 31 Aug 2026.

Deployment, failover, Move Master, maintenance and self-healing all passed on a
live pair. These are the things that did not.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


class RemoveHostDrbdTests(unittest.TestCase):
    """`drbdadm adjust` rejected the file remove-host wrote, mid-removal.

        /etc/drbd.d/kin-zimbra.res:5: in resource kin-zimbra:
            Missing section 'on <PEER> { ... }'.
        resource kin-zimbra: cannot configure network without knowing my peer.

    The node was already out of Corosync by then, so the run stopped with the
    survivor still calling itself a pair and no way forward on the page.
    """

    def _template(self) -> str:
        return (
            REPO / "ansible/roles/cluster_remove_host/templates/kin-zimbra-single.res.j2"
        ).read_text(encoding="utf-8")

    def _tasks(self) -> str:
        return (
            REPO / "ansible/roles/cluster_remove_host/tasks/drbd_drop_peer.yml"
        ).read_text(encoding="utf-8")

    def test_a_one_host_resource_carries_no_network_configuration(self) -> None:
        body = "\n".join(
            line for line in self._template().splitlines() if not line.strip().startswith("#")
        )
        # drbdadm refuses to parse network settings when only one node is named.
        self.assertNotIn("net {", body)
        self.assertNotIn("address", body)
        self.assertNotIn("protocol", body)

    def test_it_still_names_exactly_one_host_and_its_disks(self) -> None:
        body = self._template()
        self.assertEqual(body.count("  on {{"), 1)
        for needed in ("device", "disk", "meta-disk"):
            with self.subTest(needed=needed):
                self.assertIn(needed, body)

    def test_the_peer_is_disconnected_before_the_file_is_rewritten(self) -> None:
        tasks = self._tasks()
        self.assertLess(
            tasks.index("drbdadm disconnect"), tasks.index("Deploy single-node DRBD resource")
        )

    def test_the_previous_file_is_kept(self) -> None:
        self.assertIn(".removehost.bak", self._tasks())

    def test_a_repeat_run_does_not_overwrite_the_good_backup(self) -> None:
        tasks = self._tasks()
        # Backing up unconditionally means the second run saves the ALREADY
        # rewritten one-node file over the two-node original it exists to
        # protect. Disconnect, backup and rewrite all hang off one condition.
        self.assertEqual(tasks.count("cluster_remove_host_peer_in_res"), 4)
        backup = tasks.index("Back up the live DRBD resource file")
        window = tasks[backup : backup + 400]
        self.assertIn("cluster_remove_host_peer_in_res | bool", window)

    def test_adjust_failing_does_not_abort_the_removal(self) -> None:
        tasks = self._tasks()
        adjust = tasks.index("Adjust DRBD after peer drop")
        window = tasks[adjust : adjust + 700]
        # Stopping here leaves a node out of Corosync and a survivor that still
        # thinks it is a pair. What matters is whether it is still serving.
        self.assertIn("failed_when: false", window)

    def test_it_fails_only_when_the_survivor_stopped_serving(self) -> None:
        # Collapse whitespace: these messages are YAML folded scalars, so the
        # sentence is split across source lines.
        tasks = " ".join(self._tasks().split())
        self.assertIn("no longer DRBD Primary", tasks)
        self.assertIn("Put the previous resource file back", tasks)


class LicenseApplyTests(unittest.TestCase):
    def test_the_peer_host_is_built_with_all_three_fields(self) -> None:
        src = (
            REPO / "console/backend/kin_privhelper/license_sync.py"
        ).read_text(encoding="utf-8")
        # OrchHost(name, ip, iqn_suffix). Two arguments raised TypeError on
        # every license apply on a pair.
        self.assertIn("_iqn_suffix", src)
        m = re.search(r"OrchHost\(([^)]*)\)", src)
        self.assertIsNotNone(m)
        self.assertEqual(len(m.group(1).split(",")), 3)


class SingleNodeReportingTests(unittest.TestCase):
    """A one-node appliance must not be told its replication is broken."""

    def test_link_state_is_not_reported_without_a_second_node(self) -> None:
        src = (
            REPO / "console/backend/kin_privhelper/maintenance.py"
        ).read_text(encoding="utf-8")
        head = src.index('"drbd_link":')
        window = src[head : head + 260]
        self.assertIn("stale_peers", window)
        self.assertIn("> 1", window)

    def test_the_replication_card_agrees_with_the_banner(self) -> None:
        src = (
            REPO / "console/frontend/src/pages/Cluster.tsx"
        ).read_text(encoding="utf-8")
        # "Unknown / No live sync data" beside a red "replication is down"
        # banner reads as the page arguing with itself.
        self.assertIn('"Link down"', src)
        self.assertIn("Nothing is being copied to the other node", src)

    def test_the_topology_card_never_contradicts_itself(self) -> None:
        src = (
            REPO / "console/frontend/src/pages/Cluster.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("HA pair, one node", src)
        self.assertIn("Configured for two; only this node is in the cluster", src)

    def test_a_stalled_removal_tells_the_operator_what_to_do(self) -> None:
        src = (
            REPO / "console/frontend/src/pages/Cluster.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("no second node is in", src)
        self.assertIn("Remove Host", src)


class MoveMasterOutcomeTests(unittest.TestCase):
    def test_the_move_is_judged_on_the_whole_stack(self) -> None:
        src = (
            REPO / "console/backend/kin_privhelper/maintenance.py"
        ).read_text(encoding="utf-8")
        head = src.index("Settle reported incomplete, but stack is already on")
        window = " ".join(src[head : head + 2600].split())
        # Judging on `promoted` alone reported a completed Move Master as
        # FAILED, because that one field reads stale while Pacemaker
        # recomputes placement after the ban is cleared.
        self.assertIn("_stack_on_target(st_after, target)", window)
        self.assertIn("did not stay there", window)


class MonitoringDefaultsTests(unittest.TestCase):
    def test_the_default_window_is_the_live_one(self) -> None:
        """An hour-wide window averages away the spike that made someone open
        the tab. Operator asked for the live view on open (QA, 18 Sep 2026)."""
        from kin_console import monitoring

        src = (
            REPO / "console/backend/kin_console/monitoring.py"
        ).read_text(encoding="utf-8")
        self.assertIn('DEFAULT_RANGE = "now"', src)
        self.assertEqual(monitoring.DEFAULT_RANGE, "now")

    def test_both_sides_open_on_the_same_window(self) -> None:
        """The API's default and the tab's opening view have to be one value.

        They disagreed once: the tab opened on a range the server's default no
        longer matched, and the catalogue fetch that was meant to adopt the
        server default compared against a literal that had moved on, so it
        silently stopped adopting anything.
        """
        from kin_console import monitoring

        src = (
            REPO / "console/frontend/src/monitoring/MonitoringTab.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn(f'const INITIAL_RANGE = "{monitoring.DEFAULT_RANGE}";', src)
        self.assertIn("useState(INITIAL_RANGE)", src)
        self.assertIn("r === INITIAL_RANGE", src)
        # No stray literal left behind to drift again.
        self.assertNotIn('useState("1h")', src)

    def test_there_is_a_live_window(self) -> None:
        from kin_console import monitoring

        self.assertIn("now", monitoring.RANGES)
        span, step = monitoring.RANGES["now"]
        spans = {name: s for name, (s, _st) in monitoring.RANGES.items()}
        # It is the shortest window on offer, which is what makes it the live
        # one.
        self.assertEqual(span, min(spans.values()))
        # And it samples at the rate the data actually arrives. Prometheus
        # scrapes every 15s, so a finer step repeats the same sample rather
        # than showing anything new.
        self.assertLessEqual(step, 15, "coarser than the scrape interval")
        self.assertGreater(
            monitoring.expected_points("now"), 100, "too few points to draw"
        )

    def test_the_ui_refreshes_the_live_window_quickly(self) -> None:
        src = (
            REPO / "console/frontend/src/monitoring/MonitoringTab.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('range === "now"', src)

    def test_reports_open_on_the_current_period(self) -> None:
        """A report that opens on a closed period reads as current until
        somebody checks the dates (QA, 18 Sep 2026)."""
        src = (
            REPO / "console/frontend/src/monitoring/ReportsTab.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('useState("this_month")', src)
        self.assertNotIn('useState("last_month")', src)


if __name__ == "__main__":
    unittest.main()
