"""The tuning table in MAIL-GATEWAY.md has to match what apply actually sends.

A documented value that the code does not send is worse than no documentation:
somebody reads it, believes the gateway is configured that way, and does not
check. Every number below is derived from the script and the document, so a
change to one without the other fails here rather than in production.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "install/lib/mail-gateway.sh"
DESIGN = REPO / "MAIL-GATEWAY.md"


def _code() -> str:
    """The script with comment lines removed.

    A guard that can be satisfied by the comment explaining it is not a guard;
    this repository has been caught by that three times.
    """
    return "\n".join(
        ln
        for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )


class TheSettingsApplySendsAreTheOnesDocumented(unittest.TestCase):
    def setUp(self) -> None:
        self.code = _code()
        self.doc = DESIGN.read_text(encoding="utf-8")

    def test_the_spam_detector_settings_are_sent(self) -> None:
        for field in ("use_bayes=1", "use_awl=1", "use_razor=1", "rbl_checks=1", "extract_text=1"):
            with self.subTest(field=field):
                self.assertIn(field, self.code)
                self.assertIn(field.split("=")[0], self.doc)

    def test_encrypted_archives_are_flagged(self) -> None:
        """"Password is in the next email" is how a lot of ransomware arrives."""
        self.assertIn("archiveblockencrypted=1", self.code)
        self.assertIn("archiveblockencrypted", self.doc)

    def test_the_scanner_limits_are_raised_not_lowered(self) -> None:
        # Data past a scanner limit is skipped UNSCANNED and delivered, so a
        # small limit is not caution.
        def const(name: str) -> int:
            m = re.search(rf'^{name}="\$\{{[A-Z_]+:-(\d+)\}}"', self.code, re.M)
            self.assertIsNotNone(m, f"{name} is not defined as expected")
            return int(m.group(1))

        self.assertGreater(const("PMG_ARCHIVE_MAX_SIZE"), 25_000_000, "stock is 25 MB")
        self.assertGreater(const("PMG_ARCHIVE_MAX_RECURSION"), 5, "stock is 5")
        self.assertGreater(const("PMG_ARCHIVE_MAX_FILES"), 1000, "stock is 1000")
        self.assertGreater(const("PMG_MAX_SCAN_SIZE"), 100_000_000, "stock is 100 MB")
        self.assertGreater(const("PMG_MAX_SPAM_SCAN_BYTES"), 262_144, "stock is 256 KiB")

    def test_quarantine_lifetimes_are_longer_than_stock(self) -> None:
        spam = re.search(r'PMG_SPAMQUAR_DAYS="\$\{[A-Z_]+:-(\d+)\}"', self.code)
        virus = re.search(r'PMG_VIRUSQUAR_DAYS="\$\{[A-Z_]+:-(\d+)\}"', self.code)
        self.assertIsNotNone(spam)
        self.assertIsNotNone(virus)
        # A week does not cover somebody's holiday, and a false positive that
        # expires unnoticed is a lost message.
        self.assertGreaterEqual(int(spam.group(1)), 14)
        self.assertGreaterEqual(int(virus.group(1)), 30)
        self.assertIn("14 days", self.doc)
        self.assertIn("30 days", self.doc)

    def test_the_quarantine_view_does_not_attack_the_reviewer(self) -> None:
        self.assertIn("allowhrefs=0", self.code)
        self.assertIn("viewimages=0", self.code)
        self.assertIn("tracking pixels", self.doc)

    def test_greylisting_defaults_off_and_the_document_says_so(self) -> None:
        self.assertRegex(
            self.code,
            r"GW_GREYLIST=\$\(conf_get GATEWAY_GREYLIST .*\|\| printf '0'\)",
        )
        self.assertIn("off by default", self.doc)

    def test_reject_unknown_is_still_not_imposed(self) -> None:
        """It refuses senders with no reverse DNS. That is the customer's call."""
        self.assertNotIn("rejectunknown=1", self.code)
        self.assertIn("rejectunknown", self.doc)

    def test_the_document_does_not_claim_a_setting_the_code_never_sends(self) -> None:
        # Every `option` named in the tuning tables must appear in the script.
        section = self.doc[self.doc.index("## 9a."):self.doc.index("## 10a.")]
        claimed = set(re.findall(r"\| `([a-z_]+)` \|", section))
        self.assertGreater(len(claimed), 8, "the tuning tables were not found")
        missing = sorted(c for c in claimed if c not in self.code)
        self.assertEqual(
            missing, [], f"documented but never sent by apply: {missing}"
        )


class TheRuleDatabaseIsReadNotRewritten(unittest.TestCase):
    def setUp(self) -> None:
        self.code = _code()

    def test_it_only_activates_never_creates_or_deletes(self) -> None:
        """The rule database is the one part of PMG a customer customises."""
        rules = self.code[self.code.index("check_rules() {") :]
        rules = rules[: rules.index("\n# ---")] if "\n# ---" in rules else rules
        self.assertIn('api_put "/config/ruledb/rules/${rid}" "active=1"', rules)
        self.assertNotIn("api_post /config/ruledb", rules)
        self.assertNotIn("api_del /config/ruledb", rules)

    def test_a_rule_problem_never_fails_the_apply(self) -> None:
        # Mail still flows. Failing here would make an operator think the whole
        # link was broken.
        rules = self.code[self.code.index("check_rules() {") :]
        self.assertNotIn("return 2", rules[: rules.index("\n}")])


if __name__ == "__main__":
    unittest.main()
