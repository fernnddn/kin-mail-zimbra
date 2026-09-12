"""Python embedded in a shell script must survive the shell's quoting.

install/lib/mail-gateway.sh runs small Python programs inline:

    summary=$(printf '%s' "$body" | "$PYTHON" -c '
    ...python...
    ')

The whole program is a SINGLE-QUOTED shell string, and a single quote anywhere
inside it - including in an English comment like "somebody else's id" - closes
that string early. The shell then reads the rest of the program as shell, and
the script dies with "unexpected EOF while looking for matching )" at load
time. Every command in the file stops working, not just the one being edited.

`bash -n` does catch it, and it caught this one. This test exists so the
failure is named rather than presented as a mystery syntax error a hundred
lines from the real cause.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SHELL_FILES = sorted((REPO / "install").rglob("*.sh"))

# `... -c '` ... `'` where the opening quote is followed by a newline: the
# multi-line inline-program shape.
BLOCK = re.compile(r"-c '\n(.*?)\n'", re.S)


class EmbeddedProgramsAreQuoteSafe(unittest.TestCase):
    def test_there_are_some_to_check(self) -> None:
        found = sum(len(BLOCK.findall(f.read_text(encoding="utf-8"))) for f in SHELL_FILES)
        self.assertGreater(found, 0, "no embedded programs found; has the shape changed?")

    def test_no_single_quote_inside_a_single_quoted_program(self) -> None:
        offences: list[str] = []
        for path in SHELL_FILES:
            text = path.read_text(encoding="utf-8")
            for match in BLOCK.finditer(text):
                body = match.group(1)
                if "'" not in body:
                    continue
                line_no = text[: match.start()].count("\n") + 1
                bad = [ln for ln in body.splitlines() if "'" in ln]
                offences.append(
                    f"{path.relative_to(REPO)}:~{line_no}: {bad[0].strip()[:70]}"
                )
        self.assertEqual(
            offences,
            [],
            "a single quote closes the shell string early and breaks the whole "
            "script at load time. Rewrite the text without an apostrophe: "
            + "; ".join(offences),
        )

    def test_every_shell_file_still_parses(self) -> None:
        """The property the rule above protects."""
        import subprocess

        broken = []
        for path in SHELL_FILES:
            res = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True
            )
            if res.returncode != 0:
                broken.append(f"{path.relative_to(REPO)}: {res.stderr.strip()[:120]}")
        self.assertEqual(broken, [], "; ".join(broken))


if __name__ == "__main__":
    unittest.main()
