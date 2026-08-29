"""Make the deploy log readable without making it say less.

Two things made the Phase 8 runs hard to follow, and neither was a fault:

Ansible pads every banner to 79 columns with asterisks, and floors that width
internally, so no terminal setting can turn it off. A deploy log read in a
browser ends up mostly punctuation, and the task name - the part that says what
is happening - is buried in the middle of it.

Worse, Ansible prints the word FAILED on every poll of a task that is waiting
for something. A resource group that takes two minutes to come up produces ten
lines of "FAILED - RETRYING", and an operator watching a deployment they have
already seen break twice has no way to read that as normal. It cost a real
scare on 29 Aug 2026: the run was healthy the whole time.

Nothing here drops a line. Skipped tasks, ok lines and recaps all stay, because
the ask was for more detail and less decoration, not less information.
"""

from __future__ import annotations

import re
from typing import Sequence

_TRAILING_STARS = re.compile(r"\s*\*{3,}\s*$")
_BANNER = re.compile(r"^(PLAY|TASK|RUNNING HANDLER)\s*\[(?P<body>.*?)\]\s*\**\s*$")
_RECAP = re.compile(r"^PLAY RECAP\s*\**\s*$")
_RETRY = re.compile(
    r"^FAILED - RETRYING:\s*\[(?P<host>[^\]]*)\]:\s*(?P<task>.*?)\s*"
    r"\((?P<left>\d+)\s+retries left\)\.?\s*$"
)


def tidy_ansible_line(line: str) -> str:
    """Rewrite one line of ansible output. Returns it unchanged if it is not one."""
    newline = "\n" if line.endswith("\n") else ""
    body = line.rstrip("\n")
    if not body.strip():
        return line

    m = _RETRY.match(body.strip())
    if m:
        # The task has not failed. It is being polled, which is what `until`
        # does, and saying FAILED ten times in a row while a healthy cluster
        # starts up is how a good run gets mistaken for a broken one.
        left = int(m.group("left"))
        more = "1 more attempt" if left == 1 else f"{left} more attempts"
        return f"  waiting   {m.group('task')} ({more})" + newline

    if _RECAP.match(body.strip()):
        return "PLAY RECAP" + newline

    m = _BANNER.match(body.strip())
    if m:
        kind = body.strip().split("[", 1)[0].strip()
        label = {"RUNNING HANDLER": "HANDLER"}.get(kind, kind)
        inner = m.group("body").strip()
        # "role : Task name" reads better as "role: Task name".
        inner = re.sub(r"\s+:\s+", ": ", inner, count=1)
        return f"{label}  {inner}" + newline

    stripped = _TRAILING_STARS.sub("", body)
    if stripped != body:
        return stripped + newline
    return line


def tidy_ansible_text(text: str) -> str:
    """Apply tidy_ansible_line to every line of a chunk."""
    if not text:
        return text
    out = [tidy_ansible_line(part + "\n") for part in text.split("\n")[:-1]]
    tail = text.split("\n")[-1]
    if tail:
        out.append(tidy_ansible_line(tail))
    return "".join(out)


def _short_playbook(argv: Sequence[str]) -> str:
    parts: list[str] = []
    tags = ""
    skip = ""
    play = ""
    it = iter(range(len(argv)))
    for i in it:
        a = argv[i]
        if a == "--tags" and i + 1 < len(argv):
            tags = argv[i + 1]
        elif a == "--skip-tags" and i + 1 < len(argv):
            skip = argv[i + 1]
        elif a.endswith(".yml") and "/playbooks/" in a:
            play = a.rsplit("/", 1)[-1]
        elif a == "--check":
            parts.append("check-mode")
    name = play or "playbook"
    if tags:
        parts.insert(0, f"tags={tags}")
    if skip:
        parts.insert(0, f"skip-tags={skip}")
    return name + ("  " + "  ".join(parts) if parts else "")


def format_command_banner(argv: Sequence[str], when: str) -> str:
    """One short line naming what is about to run, and when.

    The old header was the full argv - two absolute paths, every ssh option,
    and a microsecond timestamp - repeated fourteen times in a deploy log the
    operator actually reads.
    """
    argv = list(argv)
    stamp = when.strip()
    if "T" in stamp:
        stamp = stamp.split("T", 1)[1]
    stamp = stamp.split(".", 1)[0].split("+", 1)[0]
    if not argv:
        return f"> command  {stamp}"

    exe = argv[0].rsplit("/", 1)[-1]
    if exe == "ansible-playbook":
        return f"> {_short_playbook(argv)}  {stamp}"
    if exe in ("sshpass", "ssh", "scp"):
        target = ""
        for a in argv:
            if "@" in a and not a.startswith("-"):
                target = a
                break
        return f"> ssh {target or 'peer'}  {stamp}"
    # Anything else keeps its full argv: these are the one-off commands whose
    # exact flags are what you need when something goes wrong, and they are not
    # the ones that repeat fourteen times in a deploy log.
    return "> " + " ".join(argv) + f"  {stamp}"
