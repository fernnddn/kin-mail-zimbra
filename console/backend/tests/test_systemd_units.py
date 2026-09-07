"""Guards on the shipped systemd units.

privhelperd runs the DRBD failover path and is the parent of the full install.
Two of its settings have cost this product live outages, so they are asserted
here rather than left to review.
"""

from __future__ import annotations

import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
PRIVHELPERD = DEPLOY / "kin-mail-privhelperd.service"
CONSOLE = DEPLOY / "kin-mail-console.service"


def _settings(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


class PrivhelperdUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(PRIVHELPERD.is_file(), f"missing {PRIVHELPERD}")
        self.settings = _settings(PRIVHELPERD)

    def test_private_tmp_is_off(self) -> None:
        """A private /tmp gives this unit its own mount namespace.

        Two live faults came from that. A mount surviving in the namespace
        keeps the DRBD backing device open so `drbdadm secondary` cannot
        release it during failover, and the private tmpfs is torn down when
        02-prepare-os.sh upgrades systemd, killing apt inside the still-running
        installer child with `mkstemp /tmp/...: No such file or directory`.
        """
        self.assertEqual(
            self.settings.get("PrivateTmp"),
            "false",
            "privhelperd must not run with a private /tmp or mount namespace",
        )

    def test_kill_mode_keeps_the_installer_alive(self) -> None:
        """Restarting the daemon must not kill a running full install."""
        self.assertEqual(self.settings.get("KillMode"), "process")

    def test_privhelperd_declares_no_tcp_socket(self) -> None:
        # Settings, not raw text: the unit carries a comment telling the next
        # reader not to add one, and grepping the file would match that.
        self.assertNotIn("ListenStream", self.settings)

    def test_console_stays_unprivileged(self) -> None:
        console = _settings(CONSOLE)
        self.assertEqual(console.get("User"), "kin-console")
        self.assertEqual(console.get("NoNewPrivileges"), "true")


if __name__ == "__main__":
    unittest.main()
