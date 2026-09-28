"""Nothing written for us may reach a product clone.

This repository is public and is cloned onto customer appliances. Anything
written for internal use - handover notes, audit boards, working notes, deploy
transcripts, the licence signing kit, a customer's real addresses - is written
with a different reader in mind. It names what is unfinished, what is untested,
what we got wrong, and how this company works.

The rule existed in .gitignore and was still broken: cursor-handover/ reached
both public remotes in d62cf6c on 11 September 2026, because a new directory
nobody had thought about was not covered by any pattern and nothing checked.

So this does not check patterns. It checks the actual index - what is really
tracked right now - because that is the thing that ships.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


# Path shapes that are internal by nature. Each is a category, not one file, so
# a new file of the same kind is caught without anyone remembering this test.
FORBIDDEN_PATHS = (
    (re.compile(r"(^|/)cursor-handover/"), "handover material written for another tool"),
    (re.compile(r"(?i)(^|/)[^/]*handover[^/]*\.md$"), "a handover document"),
    (re.compile(r"(?i)(^|/)WORKING-NOTES"), "personal working notes"),
    (re.compile(r"\.canvas\.tsx$"), "a Cursor canvas audit board"),
    (re.compile(r"(^|/)licensing-generator/"), "the licence signing kit"),
    (re.compile(r"(^|/)kin-mail-design01/"), "the local visual design reference"),
    (re.compile(r"(?i)\.(docx|pdf|pptx|xlsx)$"), "an internal office document"),
    (re.compile(r"(?i)(^|/)log-.*\.txt$"), "a deployment transcript"),
    (re.compile(r"(?i).*-phase\d+.*\.txt$"), "a QA phase transcript"),
    (re.compile(r"(^|/)ansible/inventory/lab\.yml$"), "a real lab inventory"),
    (re.compile(r"(?i)\.(pem|key|p12|pfx)$"), "a private key or certificate"),
    (re.compile(r"(?i)(^|/)cloudflare\.ini$"), "a Cloudflare API credential"),
    (re.compile(r"(?i)register_hosts\.local\.json$"), "a real host register"),
)

# Files whose name matches a shape above but which are genuinely product code.
ALLOWED = {
    # Public keys shipped so apt can verify the packages this installs.
    "install/lib/ondrej-php.asc",
    "install/lib/zimbra-key.asc",
}


class NothingInternalIsTracked(unittest.TestCase):
    def setUp(self) -> None:
        self.tracked = _tracked()
        self.assertGreater(len(self.tracked), 100, "git ls-files returned almost nothing")

    def test_no_internal_material_is_in_the_index(self) -> None:
        offences: list[str] = []
        for path in self.tracked:
            if path in ALLOWED:
                continue
            for pattern, why in FORBIDDEN_PATHS:
                if pattern.search(path):
                    offences.append(f"{path} ({why})")
                    break
        self.assertEqual(
            offences,
            [],
            "these are internal and must not be in a product clone. Remove them with "
            "`git rm -r --cached <path>` and add a rule to .gitignore: "
            + "; ".join(offences),
        )

    def test_the_ignore_rules_that_back_this_up_are_still_present(self) -> None:
        """The test is the gate; the ignore rules stop the mistake happening."""
        rules = (REPO / ".gitignore").read_text(encoding="utf-8")
        for needle in (
            "cursor-handover/",
            "WORKING-NOTES-*.md",
            "licensing-generator/",
            "kin-mail-design01/",
            "*.docx",
            "log-*.txt",
            "*.canvas.tsx",
        ):
            with self.subTest(rule=needle):
                self.assertIn(needle, rules)

    def test_the_public_documents_we_do_want_are_still_here(self) -> None:
        """A rule broad enough to catch everything would also catch the product."""
        for wanted in (
            "README.md",
            "MAIL-GATEWAY.md",
            "DEPLOY-WITH-GATEWAY.md",
            "HA-RUNBOOK.md",
            "NOTICE",
        ):
            with self.subTest(doc=wanted):
                self.assertIn(wanted, self.tracked)


if __name__ == "__main__":
    unittest.main()


class TheLeakGateCoversTheWholeProduct(unittest.TestCase):
    """The CI scan must look at the console's source, not only the installer.

    The gate that forbids real addresses and domains listed .sh, .md, .yml,
    .j2, .py and NOTICE. The console frontend - the largest body of source in
    this repository, and the part an operator reads on screen - was not in it.
    A customer's real domain-controller address sat in
    console/frontend/src/wizard/adLdapUrl.ts through every green build until
    28 September 2026, on a public repository.

    It also has to cover console/frontend/dist, because that committed bundle
    is what bootstrap.sh rsyncs onto the appliance: a leak there ships.
    """

    WORKFLOW = REPO / ".github/workflows/checks.yml"

    def setUp(self) -> None:
        self.assertTrue(self.WORKFLOW.is_file(), "CI workflow is missing")
        self.text = self.WORKFLOW.read_text(encoding="utf-8")
        block = self.text[self.text.index("includes=("):]
        self.includes = block[: block.index(")")]

    def test_the_scan_covers_the_frontend_and_the_shipped_bundle(self) -> None:
        for suffix in ("*.ts", "*.tsx", "*.mjs", "*.js", "*.html", "*.css"):
            self.assertIn(
                f"--include='{suffix}'",
                self.includes,
                f"the leaked-data scan does not look at {suffix} files",
            )

    def test_the_scan_still_covers_what_it_always_did(self) -> None:
        for suffix in ("*.sh", "*.md", "*.yml", "*.yaml", "*.j2", "*.py"):
            self.assertIn(f"--include='{suffix}'", self.includes)
        self.assertIn("--include='NOTICE'", self.includes)

    def test_no_real_address_or_domain_is_in_the_frontend_source(self) -> None:
        """The check itself, not only that CI would run it.

        Test fixtures are exempt in CI because a classifier cannot be proved
        without naming a private address. Ordinary source has no such excuse:
        documentation space exists for examples.
        """
        private = re.compile(
            r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
            r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
            r"|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"
        )
        bases = re.compile(r"10\.0\.0\.0/8|192\.168\.0\.0/16|172\.16\.0\.0/12")
        offenders = []
        for rel in _tracked():
            if not rel.startswith("console/frontend/"):
                continue
            if not rel.endswith((".ts", ".tsx", ".mjs", ".js", ".css", ".html")):
                continue
            # Same exemption CI grants, and for the same reason.
            if rel.endswith((".test.mjs", ".check.ts")) or "/dist/" in rel:
                continue
            path = REPO / rel
            if not path.is_file():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if private.search(bases.sub("", line)):
                    offenders.append(f"{rel}:{n}: {line.strip()[:80]}")
        self.assertEqual(
            offenders,
            [],
            "real or private addresses in shipped frontend source:\n  "
            + "\n  ".join(offenders),
        )

    def test_no_customer_domain_anywhere_in_the_console(self) -> None:
        domains = re.compile(
            r"nisaroti\.my\.id|gits-it\.site|helgaalan\.my\.id"
            r"|fernnddn\.my\.id|kampretos\.my\.id"
        )
        offenders = []
        for rel in _tracked():
            if not rel.startswith("console/"):
                continue
            path = REPO / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                # A line asserting the name is ABSENT is the point of the
                # exercise; that is what keeps generated output clean.
                if domains.search(line) and "assertNot" not in line:
                    offenders.append(f"{rel}:{n}")
        self.assertEqual(offenders, [], f"customer domains in the console: {offenders}")
