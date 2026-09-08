"""asyncio.wait_for must be paired with asyncio.TimeoutError, never the builtin.

On Python 3.11+ `asyncio.TimeoutError` is an alias of the builtin
`TimeoutError`, so `except TimeoutError` around an `await asyncio.wait_for(...)`
works. On Python 3.10 they are two distinct classes and the builtin catches
nothing.

CI runs 3.11 (checks.yml pins setup-python to "3.11"). The appliance and this
workstation are Ubuntu 22.04 on 3.10. That gap is the whole problem: the bug is
invisible on the machine that gates the merge and live on the machine that
serves the customer.

It shipped in _watch_log_file. The poll loop waits on the stop event with a
short timeout and treats the timeout as "not stopped yet, keep reading". With
the builtin, the first idle tick killed the follower instead, so a sidecar log
the parent process never echoes - zmsetup's, during a Zimbra install - stopped
tailing and the operator watched an apparently silent deploy.

A behavioural test cannot defend this: on 3.11 both spellings pass, because
there both names are the same object. So this reads the source instead, which
gives the same answer on every interpreter. It scans the whole backend rather
than the one known site, so a streaming path added later is covered without
anyone remembering to come back here.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PACKAGES = ("kin_privhelper", "kin_console")


def _source_files() -> list[Path]:
    files: list[Path] = []
    for pkg in PACKAGES:
        files.extend(sorted((BACKEND / pkg).rglob("*.py")))
    return files


def _awaits_asyncio_wait_for(nodes: list[ast.stmt]) -> bool:
    """True if these statements call asyncio.wait_for outside a nested try.

    A nested try gets visited in its own right, so attributing its call to the
    enclosing handler would report the same line twice under the wrong guard.
    """
    for stmt in nodes:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Try):
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "wait_for"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            ):
                return True
    return False


def _caught_names(handler: ast.ExceptHandler) -> list[ast.expr]:
    if handler.type is None:
        return []
    if isinstance(handler.type, ast.Tuple):
        return list(handler.type.elts)
    return [handler.type]


def _builtin_timeout_handlers(path: Path) -> list[str]:
    """Return 'file:line' for every bare TimeoutError guarding a wait_for."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not _awaits_asyncio_wait_for(node.body):
            continue
        for handler in node.handlers:
            for caught in _caught_names(handler):
                if isinstance(caught, ast.Name) and caught.id == "TimeoutError":
                    try:
                        rel: Path | str = path.resolve().relative_to(BACKEND)
                    except ValueError:
                        rel = path  # a copy checked from outside the tree
                    offenders.append(f"{rel}:{handler.lineno}")
    return offenders


class WaitForUsesTheAsyncioTimeout(unittest.TestCase):
    def test_no_wait_for_is_guarded_by_the_builtin_timeout(self) -> None:
        offenders: list[str] = []
        for path in _source_files():
            offenders.extend(_builtin_timeout_handlers(path))
        self.assertEqual(
            offenders,
            [],
            "these catch the builtin TimeoutError around asyncio.wait_for, "
            "which catches nothing on Python 3.10 (the appliance): "
            + ", ".join(offenders)
            + ". Use 'except asyncio.TimeoutError' - correct on 3.10 and 3.11+.",
        )

    def test_the_scan_actually_reads_the_backend(self) -> None:
        # A scanner that silently matches no files is a test that always passes.
        files = _source_files()
        self.assertGreater(len(files), 20, "backend sources not found - refusing a silent pass")
        joined = "\n".join(p.read_text(encoding="utf-8") for p in files)
        self.assertIn("asyncio.wait_for", joined)

    def test_the_scan_catches_the_bug_it_was_written_for(self) -> None:
        # Proves the detector fails on the original code, on any interpreter.
        bad = ast.parse(
            "import asyncio\n"
            "async def f(stop):\n"
            "    try:\n"
            "        await asyncio.wait_for(stop.wait(), timeout=0.05)\n"
            "    except TimeoutError:\n"
            "        pass\n"
        )
        found = [
            handler.lineno
            for node in ast.walk(bad)
            if isinstance(node, ast.Try) and _awaits_asyncio_wait_for(node.body)
            for handler in node.handlers
            for caught in _caught_names(handler)
            if isinstance(caught, ast.Name) and caught.id == "TimeoutError"
        ]
        self.assertEqual(found, [5])


class TheSidecarFollowerIsFixed(unittest.TestCase):
    def test_watch_log_file_catches_the_asyncio_timeout(self) -> None:
        # The specific regression: _watch_log_file's poll loop. Named directly
        # so the reason this file exists survives a refactor of the scan above.
        path = BACKEND / "kin_privhelper/commands.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        watchers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_watch_log_file"
        ]
        self.assertEqual(len(watchers), 1, "_watch_log_file not found")
        tries = [
            node
            for node in ast.walk(watchers[0])
            if isinstance(node, ast.Try) and _awaits_asyncio_wait_for(node.body)
        ]
        self.assertTrue(tries, "_watch_log_file no longer awaits asyncio.wait_for")
        for node in tries:
            caught = [c for handler in node.handlers for c in _caught_names(handler)]
            self.assertTrue(
                any(
                    isinstance(c, ast.Attribute)
                    and c.attr == "TimeoutError"
                    and isinstance(c.value, ast.Name)
                    and c.value.id == "asyncio"
                    for c in caught
                ),
                "the poll loop must catch asyncio.TimeoutError so it keeps polling on 3.10",
            )


if __name__ == "__main__":
    unittest.main()
