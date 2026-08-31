"""A fresh cloud image holds the dpkg lock. That must not fail a deployment.

apt-daily and unattended-upgrades start within seconds of first boot and hold
/var/lib/dpkg/lock-frontend for minutes. apt-get does not wait by default; it
prints "Could not get lock" and exits. The first Phase 9 attempt died in
stage 02 thirty seconds after it started, on a lock that had cleared by the
time the operator ran the same commands by hand.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
# apt-get preceded by start-of-line, a shell operator, or `!` - i.e. actually
# being run, rather than named in a comment or a warning.
BARE_APT = re.compile(r"(?:^\s*|[;&|(]\s*|!\s+)apt-get\s", re.M)


def _stage(name: str) -> str:
    return (REPO / "install" / name).read_text(encoding="utf-8")


class ShellStagesTests(unittest.TestCase):
    STAGES = [
        "01-preflight.sh",
        "02-prepare-os.sh",
        "03-install-zimbra.sh",
        "04-tls-dkim.sh",
        "07-zpush.sh",
        "09-hardening.sh",
        "10-host-firewall.sh",
    ]

    # Lines whose first word only prints something. They name apt-get in their
    # message text, and matching those reports the installer's own help as a
    # bug - the same mistake that made two earlier guards flag their own advice.
    MESSAGE_HELPERS = ("warn", "info", "fail", "ok", "say", "echo", "printf", "p", "b", "f")

    def test_no_stage_runs_apt_get_without_the_lock_wrapper(self) -> None:
        offenders = []
        for name in self.STAGES:
            for line in _stage(name).splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                first = stripped.split(" ", 1)[0] if stripped else ""
                if first in self.MESSAGE_HELPERS:
                    continue
                if BARE_APT.search(line):
                    offenders.append(f"{name}: {stripped}")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_every_stage_that_uses_apt_sources_the_library(self) -> None:
        for name in self.STAGES:
            text = _stage(name)
            if "kin_apt" not in text:
                continue
            with self.subTest(stage=name):
                self.assertIn("lib/apt-lock.sh", text)

    def test_the_wrapper_actually_sets_a_timeout(self) -> None:
        lib = (REPO / "install/lib/apt-lock.sh").read_text(encoding="utf-8")
        self.assertIn("DPkg::Lock::Timeout", lib)

    def test_it_stops_services_rather_than_killing_dpkg(self) -> None:
        lib = (REPO / "install/lib/apt-lock.sh").read_text(encoding="utf-8")
        # SIGKILL mid-transaction leaves the package database half-written and
        # every later apt-get refuses until someone runs dpkg --configure -a.
        self.assertNotIn("kill -9", lib)
        self.assertNotIn("pkill", lib)
        self.assertIn("systemctl stop", lib)

    def test_what_it_paused_is_restored(self) -> None:
        lib = (REPO / "install/lib/apt-lock.sh").read_text(encoding="utf-8")
        self.assertIn("apt_resume_background_upgrades", lib)
        stage = _stage("02-prepare-os.sh")
        self.assertIn("kin_stage3_cleanup", stage)
        self.assertIn("trap 'kin_stage3_cleanup' EXIT", stage)

    def test_the_long_zimbra_install_is_protected_too(self) -> None:
        text = _stage("03-install-zimbra.sh")
        # Twenty minutes of dpkg work is the widest window in the whole deploy.
        self.assertIn("apt_prepare", text)
        self.assertIn("apt_resume_background_upgrades", text)


class AnsibleTests(unittest.TestCase):
    def test_every_play_gives_apt_time_to_get_the_lock(self) -> None:
        missing = []
        for play in sorted((REPO / "ansible/playbooks").glob("*.yml")):
            text = play.read_text(encoding="utf-8")
            plays = text.count("\n- name:") + (1 if text.lstrip().startswith("- name:") else 0)
            got = text.count("lock_timeout:")
            if got < plays:
                missing.append(f"{play.name}: {got} of {plays} plays")
        self.assertEqual(missing, [], "; ".join(missing))

    def test_the_timeout_is_longer_than_ansibles_default(self) -> None:
        # The module default is 60s, which is shorter than unattended-upgrades
        # takes on a first boot.
        play = (REPO / "ansible/playbooks/mail-cluster-setup.yml").read_text(encoding="utf-8")
        m = re.search(r"lock_timeout:\s*(\d+)", play)
        self.assertIsNotNone(m)
        self.assertGreater(int(m.group(1)), 60)


if __name__ == "__main__":
    unittest.main()
