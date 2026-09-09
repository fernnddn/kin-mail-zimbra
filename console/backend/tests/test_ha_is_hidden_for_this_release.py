"""The two-server path is hidden in the UI, and still present in the tree.

0.1.9 is the single-server release. The HA pair is real code and it is not
mature enough to sell, and an operator who finds a Build HA pair button,
presses it and hits a rough edge reasonably concludes the whole product is
rough. Hiding the entry points is the honest form of "not yet"; leaving them
visible is a promise this release cannot keep.

Two failure modes are guarded here, and they pull in opposite directions:

  a flag nothing reads    every entry point must actually consult it, or the
                          button is still there and the flag is decoration
  deleted instead of      the HA code must stay in the build, because the plan
  hidden                  is to finish it, not to rewrite it from history

What is deliberately NOT hidden is the running of an existing pair. An
appliance that is already two nodes keeps its status, health, maintenance and
removal screens. Hiding the controls for a cluster somebody is already
operating would be a far worse failure than showing a button too early.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "console/frontend/src"
FLAGS = SRC / "featureFlags.ts"

# Every screen that could START building a pair.
GATED = {
    "wizard/steps/TopologyStep.tsx": "the wizard's choice of layout",
    "wizard/deployPipeline.ts": "the Build HA pair offer",
    "pages/ClusterTopology.tsx": "Add a second server on the Cluster page",
}


class TheFlagExistsAndIsOff(unittest.TestCase):
    def test_the_flag_is_declared(self) -> None:
        self.assertTrue(FLAGS.is_file(), f"missing {FLAGS}")
        self.assertIn("HA_TOPOLOGY_OFFERED", FLAGS.read_text(encoding="utf-8"))

    def test_it_is_off_for_this_release(self) -> None:
        text = FLAGS.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"export const HA_TOPOLOGY_OFFERED\s*=\s*false",
            "the two-server path must be hidden in the release that ships single-server",
        )

    def test_it_explains_itself(self) -> None:
        # A bare boolean nobody can date or justify is how a temporary flag
        # becomes permanent.
        text = FLAGS.read_text(encoding="utf-8")
        self.assertIn("0.1.9", text)
        self.assertIn("must not be deleted", text)


class EveryEntryPointConsultsIt(unittest.TestCase):
    def test_each_screen_imports_and_uses_the_flag(self) -> None:
        for rel, what in GATED.items():
            with self.subTest(screen=rel):
                path = SRC / rel
                self.assertTrue(path.is_file(), f"missing {path} ({what})")
                text = path.read_text(encoding="utf-8")
                self.assertIn("HA_TOPOLOGY_OFFERED", text, f"{what} does not check the flag")
                self.assertRegex(
                    text,
                    r'import \{[^}]*HA_TOPOLOGY_OFFERED[^}]*\} from "[^"]*featureFlags"',
                    f"{what} references the flag without importing it",
                )

    def test_build_ha_pair_returns_false_while_hidden(self) -> None:
        # The single gate every caller goes through.
        text = (SRC / "wizard/deployPipeline.ts").read_text(encoding="utf-8")
        # From the signature to the closing brace of the function body. The
        # parameter object contains its own "}", so splitting on the first one
        # stops inside the signature and reads nothing.
        after = text.split("export function shouldOfferHaPair(")[1]
        # The parameter object closes with its own "\n}", which sits BEFORE the
        # signature ends. Anchor on the return type first, then look for the
        # function's closing brace after that point, or the slice is empty and
        # the assertion below passes on nothing.
        opens = after.index("): boolean {")
        body = after[opens : after.index("\n}", opens)]
        self.assertIn("if (!HA_TOPOLOGY_OFFERED) return false;", body)

    def test_the_wizard_selects_the_only_layout_on_offer(self) -> None:
        # With one option, refusing to continue because nothing was chosen
        # would block the operator on a choice never shown to them.
        text = (SRC / "wizard/steps/TopologyStep.tsx").read_text(encoding="utf-8")
        self.assertIn('!HA_TOPOLOGY_OFFERED && draft.topology !== "1vm"', text)


class TheCodeItselfStays(unittest.TestCase):
    """Hidden, not removed. These must still be here to be un-hidden later."""

    def test_the_two_server_paths_are_still_in_the_tree(self) -> None:
        for rel in (
            "ansible/playbooks/mail-drbd.yml",
            "ansible/playbooks/mail-pacemaker.yml",
            "ansible/playbooks/mail-qdevice.yml",
            "ansible/playbooks/mail-add-host.yml",
            "console/backend/kin_privhelper/orchestration.py",
        ):
            with self.subTest(path=rel):
                self.assertTrue((REPO / rel).is_file(), f"{rel} was deleted, not parked")

    def test_the_wizard_still_knows_what_2vm_is(self) -> None:
        # The branch is hidden behind the flag, not stripped out.
        text = (SRC / "wizard/steps/TopologyStep.tsx").read_text(encoding="utf-8")
        self.assertIn('"2vm"', text)

    def test_an_existing_pair_can_still_be_operated(self) -> None:
        # Status, health and maintenance for a live pair must not be gated.
        cluster = (SRC / "pages/Cluster.tsx").read_text(encoding="utf-8")
        stripped = re.sub(r"//.*$", "", cluster, flags=re.M)
        self.assertNotIn(
            "HA_TOPOLOGY_OFFERED",
            stripped,
            "the Cluster page must keep working for an appliance that is already a pair",
        )


if __name__ == "__main__":
    unittest.main()
