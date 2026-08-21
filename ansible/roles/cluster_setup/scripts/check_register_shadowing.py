#!/usr/bin/env python3
"""Fail if detect.yml reuses a register on both a gated and ungated task.

Skipped Ansible tasks with register: still overwrite the variable with a
skip stub (no .rc / .stat). That shadowing broke greenfield cluster_setup
when no Debian stub was present.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    # stdlib-only fallback: crude scan is enough for this guard
    yaml = None


def _tasks(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text) or []
        return [t for t in data if isinstance(t, dict)]
    # Fallback without PyYAML: pair "register:" lines with a preceding when:
    tasks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("- name:"):
            block = [lines[i]]
            i += 1
            while i < len(lines) and not lines[i].startswith("- name:"):
                block.append(lines[i])
                i += 1
            reg = None
            has_when = False
            for L in block:
                s = L.strip()
                if s.startswith("register:"):
                    reg = s.split(":", 1)[1].strip()
                if s.startswith("when:") or s == "when:":
                    has_when = True
                if s.startswith("- ") and any(
                    p in block[0:1] or True for p in ()
                ):
                    # list-form when under the task
                    pass
            # detect when: key anywhere in block
            has_when = any(L.strip().startswith("when:") for L in block)
            if reg:
                tasks.append({"register": reg, "when": has_when, "name": block[0]})
            continue
        i += 1
    return tasks


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "tasks" / "detect.yml"
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 2
    if yaml is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        tasks = []
        for t in raw:
            if not isinstance(t, dict) or "register" not in t:
                continue
            tasks.append(
                {
                    "register": t["register"],
                    "when": "when" in t,
                    "name": t.get("name", "?"),
                }
            )
    else:
        tasks = _tasks(path)

    by_reg: dict[str, list[dict]] = {}
    for t in tasks:
        by_reg.setdefault(t["register"], []).append(t)

    bad = []
    for reg, items in sorted(by_reg.items()):
        gated = [i for i in items if i["when"]]
        ungated = [i for i in items if not i["when"]]
        if gated and ungated:
            bad.append((reg, ungated, gated))

    if bad:
        print("register shadowing in detect.yml:", file=sys.stderr)
        for reg, ungated, gated in bad:
            print(f"  {reg}:", file=sys.stderr)
            for i in ungated:
                print(f"    ungated: {i['name']}", file=sys.stderr)
            for i in gated:
                print(f"    gated:   {i['name']}", file=sys.stderr)
        return 1
    print(f"ok: no gated/ungated register shadowing in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
