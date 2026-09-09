"""Playbooks must run on the ansible the appliance actually has.

console/bootstrap.sh installs ansible-core from apt. Ubuntu 22.04 ships 2.12.
CI pinned ansible-core >= 2.16 and syntax-checked everything clean, so a module
that does not exist on 2.12 passed every gate here and failed on the customer's
box.

That is exactly what happened to monitoring. monitoring_stack used
ansible.builtin.systemd_service, added in ansible-core 2.15, and on the
appliance the role died with:

    ERROR! couldn't resolve module/action 'ansible.builtin.systemd_service'
    ... exit 4

Monitoring therefore failed on every single-server appliance, both on the
automatic install during first deploy and again when the operator pressed
Install monitoring, and the Reports tab stayed empty because the mail flow
collector in that same role is what fills it (live QA Phase 12, 9 Sep 2026).

This is the same shape as the asyncio.TimeoutError fault: a runtime newer in CI
than on the appliance, and a check that therefore proved nothing. The
authoritative gate is the ansible-core 2.12 leg in checks.yml, which resolves
module names during --syntax-check and reproduces the failure. This test is the
cheap one that runs in the normal suite with no ansible installed at all.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ANSIBLE = REPO / "ansible"
WORKFLOW = REPO / ".github/workflows/checks.yml"

# ansible.builtin modules that do NOT exist on ansible-core 2.12, with the
# release that introduced them and what to use instead on 2.12.
TOO_NEW = {
    "systemd_service": ("2.15", "ansible.builtin.systemd"),
    "deb822_repository": ("2.15", "ansible.builtin.apt_repository"),
    "dnf5": ("2.15", "ansible.builtin.dnf"),
}

MODULE_RE = re.compile(r"^\s*ansible\.builtin\.([a-z0-9_]+)\s*:", re.M)


def _yaml_files() -> list[Path]:
    return sorted(
        p
        for pattern in ("*.yml", "*.yaml")
        for p in ANSIBLE.rglob(pattern)
    )


class NoModuleNewerThanTheAppliance(unittest.TestCase):
    def test_the_scan_actually_reads_the_playbooks(self) -> None:
        # A scanner that matches nothing is a test that always passes.
        files = _yaml_files()
        self.assertGreater(len(files), 30, "ansible tree not found")
        joined = "\n".join(f.read_text(encoding="utf-8") for f in files)
        self.assertIn("ansible.builtin.", joined)

    def test_no_playbook_uses_a_module_the_appliance_cannot_resolve(self) -> None:
        offenders: list[str] = []
        for path in _yaml_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                # Skip comments: the fix above explains the wrong name on
                # purpose, and a guard that matches its own rationale is prose.
                if line.lstrip().startswith("#"):
                    continue
                found = MODULE_RE.match(line)
                if not found:
                    continue
                name = found.group(1)
                if name in TOO_NEW:
                    since, instead = TOO_NEW[name]
                    offenders.append(
                        f"{path.relative_to(REPO)}:{lineno} uses "
                        f"ansible.builtin.{name} (needs ansible-core {since}; "
                        f"the appliance has 2.12). Use {instead}."
                    )
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_systemd_is_the_spelling_the_monitoring_role_uses(self) -> None:
        # Named directly, because this is the role that broke and the reason
        # this file exists.
        role = ANSIBLE / "roles/monitoring_stack/tasks/main.yml"
        self.assertTrue(role.is_file(), f"missing {role}")
        body = "\n".join(
            line
            for line in role.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIn("ansible.builtin.systemd:", body)
        self.assertNotIn("ansible.builtin.systemd_service:", body)


class CiChecksTheApplianceVersion(unittest.TestCase):
    """The guard above is a denylist. The CI leg is the real check."""

    def setUp(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), f"missing {WORKFLOW}")
        self.src = WORKFLOW.read_text(encoding="utf-8")

    def test_ansible_is_syntax_checked_on_the_appliance_version(self) -> None:
        # Without this leg, a module added in a later ansible-core passes every
        # gate in this repository and fails on the customer's appliance.
        self.assertIn('ansible: "==2.12.10"', self.src)

    def test_the_newer_version_is_still_checked_too(self) -> None:
        self.assertIn('ansible: ">=2.16,<2.19"', self.src)

    def test_the_appliance_leg_runs_a_python_that_core_supports(self) -> None:
        # ansible-core 2.12 does not run on a Python 3.11 controller, so the
        # leg would fail to install rather than check anything.
        block = self.src.split('ansible: "==2.12.10"')[1][:200]
        self.assertIn('python: "3.10"', block)


if __name__ == "__main__":
    unittest.main()
