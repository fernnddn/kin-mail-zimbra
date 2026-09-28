"""The README is the first thing anyone reads, including a security reviewer.

It stated, under "Deliberately out of scope", that "no host firewall is
enabled; perimeter control belongs on the network device". That stopped being
true when 10-host-firewall.sh joined the pipeline - the same README documents
that stage two tables higher up. So the front page of a product being sold
disclaimed a control the product has, and the stage table listed stages that
had been superseded while omitting five that shipped.

Documentation drifts silently because nothing fails when it does. These are the
claims worth pinning: the list of stages, and the statements about security
posture that a reader would act on.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
README = REPO / "README.md"
INSTALL = REPO / "install"


class TheStageTableMatchesTheStagesOnDisk(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(README.is_file(), "README.md is missing")
        self.text = README.read_text(encoding="utf-8")

    def _stages_on_disk(self) -> set[str]:
        out = set()
        for path in sorted(INSTALL.glob("*.sh")):
            name = path.name
            if not re.match(r"^\d{2}-", name):
                continue
            # 00-config.sh is a sourced library, and the README says so.
            out.add(name)
        return out

    def test_every_stage_is_documented(self) -> None:
        missing = sorted(s for s in self._stages_on_disk() if s not in self.text)
        self.assertEqual(
            missing,
            [],
            "stages that ship but the README never mentions: "
            f"{missing}. A reader cannot run what is not written down, and an "
            "auditor reads the short list as the whole product.",
        )

    def test_the_split_builder_is_documented(self) -> None:
        self.assertIn("kin-mail-split.sh", self.text)

    def test_no_stage_is_documented_that_does_not_exist(self) -> None:
        named = set(re.findall(r"`install/(\d{2}-[a-z0-9-]+\.sh)`", self.text))
        ghosts = sorted(n for n in named if not (INSTALL / n).is_file())
        self.assertEqual(ghosts, [], f"README names stages that do not exist: {ghosts}")


class TheSecurityClaimsAreTrue(unittest.TestCase):
    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")

    def test_it_does_not_claim_there_is_no_host_firewall(self) -> None:
        """The appliance configures ufw, prompts, and arms a dead-man's switch.

        The old sentence is allowed to remain only inside the paragraph that
        corrects it - deleting it silently would leave anyone who read the
        earlier version holding a wrong conclusion with nothing to find.
        """
        self.assertTrue((INSTALL / "10-host-firewall.sh").is_file())
        for match in re.finditer(r"[Nn]o host firewall is enabled", self.text):
            window = self.text[max(0, match.start() - 400) : match.end() + 400]
            self.assertTrue(
                "has not been true" in window or "corrected" in window,
                "the README claims no host firewall is enabled, and this "
                "product applies one. Either correct the claim or stop "
                "shipping the stage.",
            )

    def test_high_availability_is_not_promised(self) -> None:
        # HA is in the tree and hidden in the console. A README that reads as
        # though it ships is a promise nobody can keep during an outage.
        self.assertRegex(
            self.text,
            r"(?is)high availability is not in this release"
            r"|not part of (this release|what 0\.1\.\d+ promises)",
        )

    def test_split_is_not_described_as_high_availability(self) -> None:
        self.assertRegex(
            self.text,
            r"(?is)split (deployment )?(is not|separates).{0,200}(not )?survive",
            "the README must say plainly that split is not HA: it separates "
            "the mail store from the perimeter and does not survive a machine "
            "being lost.",
        )

    def test_the_release_badge_matches_the_scope_paragraph(self) -> None:
        """A badge two releases behind is how a customer deploys the wrong tag."""
        badge = re.search(r"badge/release-(\d+\.\d+\.\d+)-", self.text)
        self.assertIsNotNone(badge, "no release badge in the README")
        assert badge is not None
        version = badge.group(1)
        self.assertIn(
            version,
            self.text[self.text.index("Scope, stated plainly") :][:900],
            f"the badge says {version} but the scope paragraph names a "
            "different release",
        )


if __name__ == "__main__":
    unittest.main()
