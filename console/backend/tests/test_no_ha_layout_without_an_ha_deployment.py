"""DRBD, qdevice and SBD must never be drawn for a deployment that is not one.

QA, 28 September 2026, on a multi deployment. The Cluster page showed:

    TOPOLOGY
    Single server

and directly beneath it a diagram captioned "DRBD between mail nodes. qdevice
and SBD through Observability", with OBSERVABILITY and QDEVICE + SBD boxes.

Two answers on one screen, and the wrong one describes an active-passive
replicated pair - a layout this release does not ship and the operator has
explicitly said is out of scope. The product is mailbox separation: an edge
that faces the network and a mailbox machine that holds every message. Nothing
fails over and nothing is replicated.

The cause was a fallback. The render branch read

    topology === "split" ? multi : topology === "1vm" ? single : <2vm HA>

so "2vm" was not only the HA case, it was also the *default* - reached by any
value the console did not recognise, including the empty string it uses when
the cluster state cannot be read at all. On that day it could not be read,
because the console had lost access to its own privhelper.

A guess is worse than an absence here: the operator sees a diagram of a system
they do not have, during an incident, and has to work out that it is fiction.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CLUSTER = REPO / "console/frontend/src/pages/Cluster.tsx"
FLAGS = REPO / "console/frontend/src/featureFlags.ts"


class TheHaDiagramNeedsAnHaDeployment(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(CLUSTER.is_file(), "Cluster.tsx is missing")
        self.src = CLUSTER.read_text(encoding="utf-8")

    def test_the_2vm_diagram_is_conditioned_on_the_2vm_topology(self) -> None:
        """Not reached by falling through, which is how it drew for "".

        The condition has to appear immediately before the element, so a later
        edit that moves the element out from under it fails here.
        """
        idx = self.src.index('topology="2vm"')
        before = self.src[max(0, idx - 700) : idx]
        self.assertRegex(
            before,
            r'topology === "2vm" \?',
            "the HA topology diagram is not gated on an explicit 2vm topology, "
            "so an unknown or unreadable cluster state draws DRBD, qdevice and "
            "SBD for a deployment that has none of them",
        )

    def test_an_unknown_topology_draws_nothing_and_says_so(self) -> None:
        self.assertIn(
            "The deployment layout could not be read",
            self.src,
            "an unreadable cluster state must say so rather than render a guess",
        )

    def test_the_three_known_layouts_are_still_handled(self) -> None:
        # The fix must not have removed a case. Each of these renders a real
        # layout and must keep doing so.
        self.assertIn("MultiDeploymentTopology", self.src)
        self.assertIn('topology="1vm"', self.src)
        self.assertIn('topology="2vm"', self.src)

    def test_the_overview_card_and_the_diagram_cannot_disagree(self) -> None:
        """The card prints "Single server" for anything that is not 2vm.

        So the diagram must use the same test. If the card's rule ever widens
        without the diagram's rule widening too, they contradict each other on
        screen again - which is exactly what was reported.
        """
        card = re.search(
            r'topology !== "2vm"\s*\n?\s*\?\s*"Single server"', self.src
        )
        self.assertIsNotNone(
            card,
            "the overview card no longer decides 'Single server' by "
            "topology !== '2vm'; check the diagram agrees with whatever it "
            "does now",
        )


class HighAvailabilityIsStillNotOffered(unittest.TestCase):
    def test_the_flag_is_off(self) -> None:
        """Turning this on is a product decision, never a side effect.

        Everything DRBD/Pacemaker/SBD stays in the tree behind it; this is the
        one switch that would make it reachable again.
        """
        src = FLAGS.read_text(encoding="utf-8")
        self.assertIn("export const HA_TOPOLOGY_OFFERED = false;", src)

    def test_split_is_offered(self) -> None:
        src = FLAGS.read_text(encoding="utf-8")
        self.assertIn("export const SPLIT_TOPOLOGY_OFFERED = true;", src)


if __name__ == "__main__":
    unittest.main()
