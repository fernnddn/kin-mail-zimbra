"""Every address the console's UI calls must be a route the backend serves.

A typo in a URL is invisible until somebody presses the button. The frontend
compiles, the bundle builds, CI is green, and the failure arrives as a 404 in
front of a customer - on whichever page happens to hold the wrong string. The
same is true in reverse for a route that is renamed on the backend while a
caller keeps the old spelling.

Nothing else checks this. tsc checks types, not strings; the build checks
imports, not addresses; and the backend tests exercise the routes the tests
know about rather than the ones the UI asks for.

This reads both sides out of the source and compares them, so the pair can
never drift silently again.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FRONTEND_SRC = REPO / "console/frontend/src"
BACKEND_APP = REPO / "console/backend/kin_console"

# Path parameters are spelled differently on each side - `${name}` in a
# template literal, `{name}` in a FastAPI route - so both collapse to this.
PARAM = ":p"

# preview.tsx is a gitignored per-session harness that stubs the API for
# rendering a page offline. It names endpoints that deliberately do not exist.
SKIP_FILES = {"preview.tsx"}


def _normalise(path: str) -> str:
    # The query string is the caller's business, not the route's. Strip it
    # first: `?metrics=${...}` otherwise leaves half a template literal in the
    # path and every charted endpoint reads as missing.
    path = path.split("?", 1)[0]
    # A substitution glued onto the end of a segment - `/api/monitoring/host${q}`
    # where q is "" or "?name=..." - builds a query at runtime, not a path
    # segment. Cut there; treating it as a segment invents a route
    # (/api/monitoring/host:p) that nothing serves and nothing meant.
    path = re.sub(r"(?<![/])\$\{[^}]*\}.*$", "", path)
    path = re.sub(r"\$\{[^}]*\}", PARAM, path)       # `/${id}/` (a real segment)
    path = re.sub(r"\{[^}]*\}", PARAM, path)          # `{id}`   (FastAPI)
    path = re.sub(r"/\d+(?=/|$)", f"/{PARAM}", path)  # a literal id in a fixture
    return path.rstrip("/") or "/"


def frontend_calls() -> dict[str, set[str]]:
    """Every /api/... string the UI mentions, and where it is written."""
    found: dict[str, set[str]] = {}
    for src in FRONTEND_SRC.rglob("*.ts*"):
        if src.name in SKIP_FILES:
            continue
        text = src.read_text(encoding="utf-8")
        for raw in re.findall(r"[\"'`](/api/[^\"'`\s]*)[\"'`]", text):
            found.setdefault(_normalise(raw), set()).add(
                str(src.relative_to(REPO))
            )
    return found


def backend_routes() -> set[str]:
    routes: set[str] = set()
    for src in BACKEND_APP.glob("*.py"):
        text = src.read_text(encoding="utf-8")
        for raw in re.findall(
            r"^@[A-Za-z_][A-Za-z0-9_]*\.(?:get|post|put|delete|patch)\(\s*[\"']([^\"']+)",
            text,
            re.MULTILINE,
        ):
            routes.add(_normalise(raw))
    return routes


class TheTwoHalvesAgree(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = frontend_calls()
        self.routes = backend_routes()
        # If either side reads as empty the comparison below passes for the
        # wrong reason, which is worse than no test at all.
        self.assertGreater(len(self.calls), 20, "frontend API calls were not found")
        self.assertGreater(len(self.routes), 20, "backend routes were not found")

    def test_every_url_the_ui_calls_is_served(self) -> None:
        missing = []
        for call, where in sorted(self.calls.items()):
            if call in self.routes:
                continue
            # A query string or a sub-path built at runtime: accept it when a
            # route is a prefix of it, which is how /api/x/:p/y is reached.
            if any(call.startswith(r + "/") or r.startswith(call + "/") for r in self.routes):
                continue
            missing.append(f"{call}  <- {', '.join(sorted(where))}")
        self.assertEqual(
            missing,
            [],
            "the UI calls addresses the backend does not serve:\n  "
            + "\n  ".join(missing),
        )

    def test_the_api_prefix_is_not_accidentally_doubled(self) -> None:
        # /api/api/... has shipped in other products for exactly one reason:
        # a helper that already prefixes, called with a prefixed path.
        doubled = [c for c in self.calls if c.startswith("/api/api/")]
        self.assertEqual(doubled, [], f"doubled prefix: {doubled}")

    def test_no_call_carries_a_hardcoded_host(self) -> None:
        # An absolute URL would point the customer's browser at whatever host
        # was in the developer's head, and would not be caught above.
        bad = []
        for src in FRONTEND_SRC.rglob("*.ts*"):
            if src.name in SKIP_FILES:
                continue
            for line in src.read_text(encoding="utf-8").splitlines():
                if re.search(r"[\"'`]https?://[^\"'`]*/api/", line):
                    bad.append(f"{src.relative_to(REPO)}: {line.strip()[:90]}")
        self.assertEqual(bad, [], f"absolute API URLs: {bad}")


if __name__ == "__main__":
    unittest.main()
