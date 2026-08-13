#!/usr/bin/env python3
"""Idempotent nginx HTTPS-template patch for /kin-brand/ static assets.

Assets live under /etc/kin-mail/brand/ (not jetty skins/). The generated
location is inserted immediately before the catch-all ``location /`` so ``^~``
wins, matching install/11-admin-path-lockdown.sh.

Prints ``unchanged`` or ``changed:<backup>`` on stdout. Exit 0 on success.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

MARKER_BEGIN = "# KIN Mail — customer brand static assets"
MARKER_END = "# KIN Mail — end customer brand"


def location_block(snippet_path: str) -> str:
    return (
        f"    {MARKER_BEGIN}\n"
        "    location ^~ /kin-brand/\n"
        "    {\n"
        f"        include {snippet_path};\n"
        "    }\n"
        f"    {MARKER_END}\n"
    )


def _strip_marked_block(text: str) -> str:
    if MARKER_BEGIN not in text or MARKER_END not in text:
        return text
    a = text.find(MARKER_BEGIN)
    line_start = text.rfind("\n", 0, a) + 1
    b = text.find(MARKER_END)
    b = text.find("\n", b)
    if b < 0:
        b = len(text)
    else:
        b += 1
    return text[:line_start] + text[b:]


def _find_catchall(text: str) -> int:
    for needle in ("\n    location /\n", "\n    location / {"):
        idx = text.find(needle)
        if idx >= 0:
            return idx
    raise SystemExit("could not find catch-all location / in template")


def patch_https_template(text: str, snippet_path: str) -> tuple[str, bool]:
    """Return (new_text, changed). Idempotent when the include is already present."""
    include_line = f"include {snippet_path};"
    if (
        MARKER_BEGIN in text
        and MARKER_END in text
        and "location ^~ /kin-brand/" in text
        and include_line in text
    ):
        return text, False

    text = _strip_marked_block(text)
    idx = _find_catchall(text)
    block = "\n" + location_block(snippet_path)
    return text[:idx] + block + text[idx:], True


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: kin_brand_nginx.py <nginx-https-template> <snippet-path>", file=sys.stderr)
        return 2
    tpl = Path(argv[1])
    snippet = argv[2]
    original = tpl.read_text()
    new, changed = patch_https_template(original, snippet)
    if not changed:
        print("unchanged")
        return 0
    bak = Path(
        str(tpl) + ".bak.pre-kin-brand." + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    )
    bak.write_text(original)
    tpl.write_text(new)
    print(f"changed:{bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
