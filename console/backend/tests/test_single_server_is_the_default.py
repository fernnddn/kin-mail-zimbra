"""One server is what this company sells, so it is what the wizard recommends.

Product direction changed at an operator meeting: the current offering is a
single node, hardened and monitored, and the dual-node path is deferred. The
wizard was still telling every customer "2 servers (recommended)", which is the
console arguing for a second machine the business is not selling.

Nothing is deleted for this. The 2vm path stays in the tree, still selectable,
still validated; it is relabelled as the optional layout rather than the
default one. These tests pin the recommendation so it cannot drift back
silently, and pin that choosing 1vm clears the peer, observability and VIP
fields, because a stale peer IP left in a single-node draft is a real
misconfiguration and not just untidy copy.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FRONTEND_SRC = REPO / "console/frontend/src"
TOPOLOGY_STEP = FRONTEND_SRC / "wizard/steps/TopologyStep.tsx"


class TheWizardRecommendsOneServer(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(TOPOLOGY_STEP.is_file(), f"missing {TOPOLOGY_STEP}")
        self.src = TOPOLOGY_STEP.read_text(encoding="utf-8")

    def test_one_server_is_the_recommended_option(self) -> None:
        self.assertIn("1 server (recommended)", self.src)

    def test_two_servers_is_no_longer_recommended(self) -> None:
        self.assertNotIn("2 servers (recommended)", self.src)

    def test_two_servers_is_still_offered(self) -> None:
        # Parked, not removed. Ripping the path out would be a much larger and
        # much less reversible decision than the one that was actually made.
        self.assertIn("2 servers", self.src)
        self.assertIn('"2vm"', self.src)

    def test_only_one_option_claims_to_be_recommended(self) -> None:
        self.assertEqual(
            self.src.count("(recommended)"),
            1,
            "two recommended layouts is not a recommendation",
        )

    def test_picking_one_server_clears_the_two_server_fields(self) -> None:
        # A peer IP or VIP left behind in a 1vm draft is a live
        # misconfiguration, not cosmetic residue.
        pick = self.src.split("async function pick(")[1].split("}\n")[0]
        for field in (
            "peer_host_ip",
            "peer_host_name",
            "observability_vm_ip",
            "cluster_vip_ip",
        ):
            self.assertIn(f'{field}: ""', pick, f"1vm must clear {field}")


class ProductCopyUsesPlainPunctuation(unittest.TestCase):
    """No em dashes or en dashes anywhere in the operator-facing UI.

    A standing rule from the operator. It is enforced here rather than in a
    style note because a style note is not something anyone runs, and these
    characters arrive invisibly by copy and paste from a document.
    """

    DASHES = re.compile("[–—]")

    def test_no_em_or_en_dashes_in_the_console_ui(self) -> None:
        offenders: list[str] = []
        files = sorted(
            p
            for ext in ("*.tsx", "*.ts")
            for p in FRONTEND_SRC.rglob(ext)
        )
        self.assertGreater(len(files), 20, "frontend sources not found")
        for path in files:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if self.DASHES.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}")
        self.assertEqual(
            offenders,
            [],
            "use a comma, colon, period, parentheses or 'to' instead: "
            + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
