"""A single server must never be rendered as a broken cluster.

On 1vm there is no Pacemaker, no DRBD, no qdevice and no VIP. That is the
design, not an outage, so the Cluster page must not report their absence as
failure. The overview cards already returned early for anything that is not
explicitly "2vm".

The health panel underneath did not. It tested `cluster.topology === "1vm"`,
so an UNKNOWN topology fell through to the pair branch while the card above it
had already printed "Single server". The page contradicted itself, and a
healthy appliance whose topology could not be read from the marker, the config
or the draft showed DRBD not UpToDate, qdevice not voting and VIP not
configured.

Unknown topology is not rare enough to ignore: saved_wizard_topology() reads
three sources and returns "" when none of them can be read, which is exactly
the situation on a host with a permissions problem, and a permissions problem
is when an operator can least afford a page that lies about the cluster.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CLUSTER = REPO / "console/frontend/src/pages/Cluster.tsx"


def _strip_comments(src: str) -> str:
    """Drop // and /* */ comments.

    The assertions below look for code, and the code they guard is explained
    by a comment that necessarily quotes the wrong version it replaced. Without
    this, the guard matches its own rationale and passes or fails on prose.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _function_body(src: str, name: str) -> str:
    """Source of exactly one function, found by balancing braces.

    Matching "up to the next top-level declaration" silently ran off the end of
    the file when the next thing was not one of the keywords guessed at, which
    made every assertion below read the whole module instead of the function
    it names. A guard that quietly widens its own scope is worse than no guard:
    it passes for reasons unrelated to what it claims to check.
    """
    clean = _strip_comments(src)
    start = clean.index(f"function {name}(")
    open_brace = clean.index("{", start)
    depth = 0
    for i in range(open_brace, len(clean)):
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
            if depth == 0:
                return clean[start : i + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


class TheHealthPanelOnlyJudgesARealPair(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(CLUSTER.is_file(), f"missing {CLUSTER}")
        self.src = CLUSTER.read_text(encoding="utf-8")
        self.body = _function_body(self.src, "healthLines")

    def test_it_gates_on_an_explicit_two_node_topology(self) -> None:
        # Not `=== "1vm"`. Anything that is not provably a pair is a single
        # server as far as cluster health is concerned.
        self.assertIn('topology !== "2vm"', self.body)
        self.assertNotIn('cluster.topology === "1vm"', self.body)

    def test_it_is_given_the_same_topology_the_cards_use(self) -> None:
        # One normalisation, one answer. Two sources is how the card and the
        # panel disagreed in the first place.
        self.assertIn("healthLines(cluster, topology)", _strip_comments(self.src))
        self.assertIn(
            'const topology = cluster.topology === "2vm"',
            _strip_comments(self.src),
            "the page must normalise topology once, before either consumer",
        )

    def test_cluster_only_checks_sit_behind_that_gate(self) -> None:
        # DRBD, qdevice and VIP must be unreachable for a single server.
        head, _, tail = self.body.partition('topology !== "2vm"')
        self.assertTrue(tail, "the single-server early return is gone")
        for term in ("drbd_uptodate", "qdevice_ok", "vip_ip"):
            with self.subTest(term=term):
                self.assertNotIn(
                    term,
                    head,
                    f"{term} is judged before the single-server gate",
                )

    def test_an_unrecorded_topology_says_so_rather_than_inventing_one(self) -> None:
        # Claiming a confident "Single server" for a box we could not read is
        # the same class of lie in the other direction.
        self.assertIn("topology not recorded", self.body)


class TheOverviewCardsAgree(unittest.TestCase):
    def test_the_cards_use_the_same_rule(self) -> None:
        src = CLUSTER.read_text(encoding="utf-8")
        body = _function_body(src, "clusterOverviewCards")
        self.assertIn('if (topology !== "2vm") return cards;', body)
        head, _, _tail = body.partition('if (topology !== "2vm") return cards;')
        for term in ("drbd_uptodate", "qdevice_ok", "vip_ip"):
            with self.subTest(term=term):
                self.assertNotIn(term, head, f"{term} card renders on a single server")


if __name__ == "__main__":
    unittest.main()
