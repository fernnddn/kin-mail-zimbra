"""A role must create the directories it writes into, not inherit them.

The mail flow collector is installed to /usr/local/libexec. That directory does
not exist on a stock Ubuntu and ansible.builtin.copy does not create parent
directories, so the copy failed with:

    "Destination directory /usr/local/libexec does not exist"

It had been created by corosync_qdevice, a role that runs only in the two
server HA path. On a 2vm appliance the HA build made the directory first and
monitoring worked by accident. On the single server this company actually
sells, corosync_qdevice never runs, the directory never exists, and the
collector was never installed, so the Reports tab had nothing to count for the
life of the box (live QA, 9 Sep 2026).

That is the whole failure in one sentence: it worked on the topology that
happened to be tested. A role that depends on another role having run first is
not a role, it is a coincidence, and the coincidence broke the moment the
product changed which topology it ships.

mail-monitoring.yml runs monitoring_stack alone, so that role has to stand on
its own. This test pins the directories it must make for itself.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[3]
ROLE = REPO / "ansible/roles/monitoring_stack/tasks/main.yml"
DEFAULTS = REPO / "ansible/roles/monitoring_stack/defaults/main.yml"

# Paths the OS or an installed package provides. Everything else a role writes
# into, it must create.
PROVIDED_PREFIXES = (
    "/etc/systemd/system",
    "/etc/default",
    "/etc/prometheus",
)


def _bare(expr: str) -> str:
    """Strip the Jinja wrapper so a path and its reference compare equal.

    "{{ monitoring_stack_textfile_dir }}" and the variable name are the same
    directory as far as this check is concerned.
    """
    return expr.strip().removeprefix("{{").removesuffix("}}").strip()


def _role_defaults() -> dict[str, str]:
    """Scalar variables from the role's defaults, for resolving paths."""
    out: dict[str, str] = {}
    for line in DEFAULTS.read_text(encoding="utf-8").splitlines():
        found = re.match(r"^([a-z_][a-z0-9_]*):\s*(\S.*?)\s*$", line)
        if found and not found.group(2).startswith(("#", "{", "[")):
            out[found.group(1)] = found.group(2).strip().strip("\"'")
    return out


def _resolve(expr: str, variables: dict[str, str]) -> str:
    """Turn a Jinja path expression into the literal path it names."""
    text = _bare(expr)
    dirname = text.endswith("| dirname")
    if dirname:
        text = text[: -len("| dirname")].strip()
    for name, value in variables.items():
        text = text.replace("{{ " + name + " }}", value).replace(name, value)
    if dirname:
        text = str(PurePosixPath(text).parent)
    return text


def _created_dirs(text: str) -> set[str]:
    made = set()
    for m in re.finditer(r"ansible\.builtin\.file:\s*\n((?:[ \t]+\S.*\n)+)", text):
        body = m.group(1)
        path = re.search(r"^\s+path:\s*[\"']?([^\"'\n]+)", body, re.M)
        state = re.search(r"^\s+state:\s*(\S+)", body, re.M)
        if path and state and state.group(1).strip() == "directory":
            made.add(_bare(path.group(1)))
    return made


def _copy_dests(text: str) -> list[str]:
    out = []
    for m in re.finditer(
        r"ansible\.builtin\.(?:copy|template):\s*\n((?:[ \t]+\S.*\n)+)", text
    ):
        dest = re.search(r"^\s+dest:\s*[\"']?([^\"'\n]+)", m.group(1), re.M)
        if dest:
            out.append(dest.group(1).strip())
    return out


class MonitoringStackStandsAlone(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(ROLE.is_file(), f"missing {ROLE}")
        self.text = ROLE.read_text(encoding="utf-8")
        self.made = _created_dirs(self.text)

    def test_it_creates_the_collector_directory(self) -> None:
        # The exact failure. /usr/local/libexec is not on a stock Ubuntu.
        self.assertIn(
            "monitoring_stack_mailflow_script | dirname",
            self.text,
            "the role must create the directory its collector lives in",
        )

    def test_it_creates_the_directory_before_writing_into_it(self) -> None:
        # Order is the fix. Creating it after the copy is no fix at all.
        create_at = self.text.index("monitoring_stack_mailflow_script | dirname")
        copy_at = self.text.index("src: kin-mail-flow-metrics.py")
        self.assertLess(create_at, copy_at, "the copy runs before the directory exists")

    def test_it_creates_the_textfile_directory(self) -> None:
        self.assertIn("monitoring_stack_textfile_dir", self.made)

    def test_every_destination_is_provided_or_created_by_this_role(self) -> None:
        # Derived, so a destination added later is covered without anyone
        # remembering to come back here. Variables are resolved from defaults
        # to their real paths first: comparing raw Jinja would let
        # "{{ monitoring_stack_config }}" look unowned while it is in fact
        # /etc/prometheus, a directory the package provides.
        variables = _role_defaults()
        made_dirs = {_resolve(d, variables) for d in self.made}
        unowned: list[str] = []
        for dest in _copy_dests(self.text):
            path = _resolve(_bare(dest), variables)
            if path.startswith(PROVIDED_PREFIXES):
                continue
            parent = str(PurePosixPath(path).parent)
            if parent in made_dirs:
                continue
            unowned.append(f"{dest} (parent {parent})")
        self.assertEqual(
            unowned,
            [],
            "these are written into directories this role never creates, so they "
            "depend on some other role having run first: " + ", ".join(unowned),
        )

    def test_the_collector_path_is_still_the_one_the_units_use(self) -> None:
        # The unit template and the default must not drift apart, or the timer
        # would start a script that is not there.
        self.assertIn("monitoring_stack_mailflow_script:", DEFAULTS.read_text(encoding="utf-8"))
        unit = REPO / "ansible/roles/monitoring_stack/templates/kin-mail-flow.service.j2"
        self.assertIn("{{ monitoring_stack_mailflow_script }}", unit.read_text(encoding="utf-8"))


class TheRoleDoesNotDependOnTheHaPath(unittest.TestCase):
    """mail-monitoring.yml runs monitoring_stack and nothing else."""

    def test_the_playbook_runs_this_role_alone(self) -> None:
        play = (REPO / "ansible/playbooks/mail-monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("monitoring_stack", play)
        for ha_role in ("corosync_qdevice", "cluster_setup", "drbd_resource", "pacemaker_mail_stack"):
            with self.subTest(role=ha_role):
                self.assertNotIn(
                    ha_role,
                    play,
                    "single-server monitoring must not need an HA role to have run",
                )


if __name__ == "__main__":
    unittest.main()
