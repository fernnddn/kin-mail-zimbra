"""The console as a multi deployment operator sees it.

Five things the QA of 18 Sep 2026 asked for, each pinned where it can regress:
the page must say Multi deployment and not Single server, it must not describe
the deployment with DRBD, qdevice, SBD or an observability VM, the rail must
keep alerts and the account at the bottom, the tab must have a favicon, and
Monitoring must be able to show the mailbox as well as this machine.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

CLUSTER = REPO / "console/frontend/src/pages/Cluster.tsx"
TOPOLOGY = REPO / "console/frontend/src/pages/ClusterTopology.tsx"
CHROME = REPO / "console/frontend/src/ConsoleChrome.tsx"
INDEX = REPO / "console/frontend/index.html"
MONITORING = REPO / "console/frontend/src/monitoring/MonitoringTab.tsx"


def _no_comments(src: str) -> str:
    """Code only.

    These assertions are about what the page DOES. Prose explaining why DRBD is
    absent mentions DRBD, and matching that would fail a file for documenting
    the very decision under test.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    return src


def _no_docstring(src: str) -> str:
    """Python source with its leading docstring removed, same reason."""
    parts = src.split('"""')
    return parts[0] + "".join(parts[2:]) if len(parts) > 2 else src


def _body(path: Path, name: str) -> str:
    """The source of one top-level function, brace-matched."""
    src = path.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


class ItIsCalledWhatItIs(unittest.TestCase):
    def setUp(self) -> None:
        self.src = CLUSTER.read_text(encoding="utf-8")

    def test_a_split_is_normalised_rather_than_falling_through(self) -> None:
        """It used to land in the empty string, which every branch below reads
        as "not a pair" and therefore prints as one machine."""
        self.assertIn('cluster.topology === "split"', self.src)

    def test_the_topology_card_says_multi_deployment(self) -> None:
        cards = _body(CLUSTER, "clusterOverviewCards")
        self.assertIn('topology === "split"', cards)
        self.assertIn('"Multi deployment"', cards)
        head = cards[: cards.index('"Single server"')]
        self.assertIn(
            '"Multi deployment"',
            head,
            "the split branch must be tested before the single-server default",
        )

    def test_the_page_stops_calling_it_a_cluster(self) -> None:
        self.assertIn('"Deployment Overview"', self.src)
        self.assertIn('"Deployment Healthy"', self.src)

    def test_health_is_the_links_not_pacemaker(self) -> None:
        lines = _body(CLUSTER, "healthLines")
        split = _no_comments(lines)
        split = split[: split.index('topology !== "2vm"')]
        self.assertIn('topology === "split"', split)
        for term in ("drbd", "qdevice", "vip_ip", "observability"):
            with self.subTest(term=term):
                self.assertNotIn(term, split.lower())


class NothingReplicatedIsDrawnForIt(unittest.TestCase):
    def test_the_multi_deployment_diagram_knows_nothing_about_replication(self) -> None:
        body = _no_comments(_body(TOPOLOGY, "MultiDeploymentTopology"))
        for term in ("drbd", "qdevice", "sbd", "observability", "promoted", "vip"):
            with self.subTest(term=term):
                self.assertNotIn(term, body.lower())

    def test_it_is_a_separate_component_from_the_pair(self) -> None:
        """Not another branch inside the pair's renderer. A branch is one `if`
        away from drawing DRBD again; a separate component is not."""
        src = TOPOLOGY.read_text(encoding="utf-8")
        self.assertIn("export function MultiDeploymentTopology(", src)
        self.assertIn("export function ClusterTopology(", src)

    def test_the_pair_renderer_is_unreachable_on_a_split(self) -> None:
        src = CLUSTER.read_text(encoding="utf-8")
        chain = src[src.index('{topology === "split" ? (') :][:400]
        self.assertIn("MultiDeploymentTopology", chain)
        self.assertLess(
            chain.index("MultiDeploymentTopology"),
            chain.index("ClusterTopology"),
            "a split must reach its own diagram first",
        )

    def test_add_observability_is_not_offered_to_a_split(self) -> None:
        """The buttons live in the pair's renderer, which a split never reaches,
        and the cards return before the observability card is built."""
        cards = _body(CLUSTER, "clusterOverviewCards")
        split_return = cards.index('if (topology === "split") return')
        self.assertLess(
            split_return,
            cards.index("cluster.observability"),
            "a split reaches the observability card",
        )


class TheRailKeepsItsShape(unittest.TestCase):
    def test_the_nav_wrapper_takes_the_slack(self) -> None:
        """Alerts and the account button sit at the bottom because the nav
        wrapper grows. It carried no flex, so they bunched up under the last
        nav item instead - the rail's own `flex: 1 1 auto` was on the element
        inside the wrapper, which the rail does not lay out."""
        src = CHROME.read_text(encoding="utf-8")
        wrap = src[src.index("const RailNavWrap = styled.div`") :]
        wrap = wrap[: wrap.index("`;")]
        self.assertIn("flex: 1 1 auto", wrap)

    def test_the_account_and_alerts_still_come_after_the_nav(self) -> None:
        src = CHROME.read_text(encoding="utf-8")
        nav = src.index("<RailNavWrap")
        self.assertLess(nav, src.index("<RailGroup>"))
        self.assertLess(src.index("<RailGroup>"), src.index("<RailFoot>"))


class TheTabHasAnIcon(unittest.TestCase):
    def test_a_favicon_is_declared(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('rel="icon"', html)

    def test_it_travels_inside_the_document(self) -> None:
        """Only /assets is mounted as static by the console; every other path
        falls through to the SPA route and is served as index.html, so a
        /favicon.svg would come back as HTML and be ignored."""
        html = INDEX.read_text(encoding="utf-8")
        icon = re.search(r'<link rel="icon"[^>]*href="([^"]+)"', html)
        self.assertIsNotNone(icon)
        assert icon is not None
        self.assertTrue(icon.group(1).startswith("data:image/svg+xml,"))


class MonitoringCanSeeTheMailbox(unittest.TestCase):
    def test_the_backend_offers_the_nodes_it_actually_scrapes(self) -> None:
        from kin_console import monitoring

        self.assertTrue(hasattr(monitoring, "scraped_nodes"))
        self.assertEqual(monitoring.ROLE_LABELS["mailbox"], "Mailbox")
        self.assertEqual(monitoring.ROLE_LABELS["edge"], "This server")

    def test_a_remote_node_is_read_through_prometheus_not_proc(self) -> None:
        """/proc belongs to the process's own machine. Facts for another one
        can only come from what it publishes."""
        src = (
            REPO / "console/backend/kin_console/monitoring.py"
        ).read_text(encoding="utf-8")
        head = src.index("def host_facts_for_instance(")
        body = _no_docstring(src[head : src.index("\ndef ", head + 10)])
        self.assertNotIn("/proc", body)
        self.assertIn("node_memory_MemTotal_bytes", body)

    def test_an_instance_name_cannot_be_smuggled_into_a_query(self) -> None:
        from kin_console import monitoring

        self.assertEqual(
            monitoring._label_selector('x" or up{a="b'), 'instance="xorupab"'
        )

    def test_the_api_refuses_a_node_it_does_not_scrape(self) -> None:
        src = (
            REPO / "console/backend/kin_console/app.py"
        ).read_text(encoding="utf-8")
        head = src.index("async def monitoring_host(")
        body = src[head : head + 1800]
        self.assertIn("scraped_nodes", body)
        self.assertIn("Unknown node", body)

    def test_the_tab_filters_charts_to_one_machine(self) -> None:
        """Every chart draws series[0] and Prometheus returns one series per
        instance, sorted by name - so with two nodes scraped an unfiltered
        chart is a coin toss between two machines on a card labelled neither."""
        src = MONITORING.read_text(encoding="utf-8")
        self.assertIn("sx.instance === forNode", src)
        self.assertIn('api<NodesResp>("/api/monitoring/nodes")', src)

    def test_the_switcher_is_hidden_when_there_is_one_machine(self) -> None:
        src = MONITORING.read_text(encoding="utf-8")
        self.assertIn("nodes.length > 1 ?", src)

    def test_edge_only_cards_are_not_drawn_for_a_remote_node(self) -> None:
        """The gateway is the edge's business. An "Unknown" gateway card under
        the mailbox's charts reports a fault that belongs to another machine."""
        src = MONITORING.read_text(encoding="utf-8")
        self.assertIn("host.remote ? null : <GatewayCard", src)


class TheScrapeConfigLabelsEachMachine(unittest.TestCase):
    def test_every_target_carries_its_own_instance_label(self) -> None:
        """A shared relabel rule stamped every target with this host's name.
        Correct while loopback was the only target, and silently wrong the
        moment a second node was added: both machines drew as one."""
        tpl = (
            REPO / "ansible/roles/monitoring_stack/templates/prometheus.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("monitoring_stack_scrape_nodes", tpl)
        self.assertIn("instance:", tpl)
        self.assertNotIn("relabel_configs", tpl)

    def test_the_port_is_one_number_in_three_places(self) -> None:
        from kin_privhelper import monitoring_install

        defaults = (
            REPO / "ansible/roles/monitoring_stack/defaults/main.yml"
        ).read_text(encoding="utf-8")
        stage = (REPO / "install/12-node-metrics.sh").read_text(encoding="utf-8")
        port = monitoring_install.NODE_EXPORTER_PORT
        self.assertIn(f"127.0.0.1:{port}", defaults)
        self.assertIn(f"KIN_NODE_EXPORTER_PORT:-{port}", stage)

    def test_the_mailbox_is_scraped_only_on_a_multi_deployment(self) -> None:
        src = (
            REPO / "console/backend/kin_privhelper/monitoring_install.py"
        ).read_text(encoding="utf-8")
        head = src.index("def mailbox_scrape_target(")
        body = _no_docstring(src[head : src.index("\ndef ", head + 10)])
        self.assertIn('!= "split"', body)

    def test_a_scrape_target_without_a_name_is_refused(self) -> None:
        """Prometheus would still record it, under whatever label was there,
        and two machines would draw as one."""
        from kin_privhelper import monitoring_install as mi

        with self.assertRaises(ValueError):
            mi.local_inventory("mail.example.test", ("not a hostname!", "192.0.2.11:9100"))
        with self.assertRaises(ValueError):
            mi.local_inventory("mail.example.test", ("store.example.test", "nonsense"))

    def test_the_mailbox_is_a_scrape_target_not_a_host_to_configure(self) -> None:
        """This playbook runs with a local connection and no credentials for
        any other machine. A mailbox in mail_nodes would make it try to
        apt-install on a host it cannot reach."""
        from kin_privhelper import monitoring_install as mi

        inv = mi.local_inventory(
            "mail.example.test", ("store.example.test", "192.0.2.11:9100")
        )
        hosts = inv[inv.index("hosts:") :]
        self.assertNotIn("store.example.test", hosts)
        self.assertIn("store.example.test", inv)


if __name__ == "__main__":
    unittest.main()
