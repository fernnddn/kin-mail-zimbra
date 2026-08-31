"""Secrets pushed to the peer must not be readable by anyone else on it.

license.token and session.secret travel through /tmp on the peer. scp writes
that copy under the SSH user's umask - 0644 on a stock Ubuntu - and the
cleanup used to be chained with && behind a successful install, so a failed
install left the console's cookie-signing key world-readable in /tmp
indefinitely.
"""

from __future__ import annotations

import inspect
import unittest

from kin_privhelper import orchestration


class ScpModeTests(unittest.TestCase):
    def test_scp_preserves_the_local_mode(self) -> None:
        src = inspect.getsource(orchestration._scp_put)
        # -p carries the local 0600 across instead of taking the remote umask.
        self.assertIn('"-p",', src)

    def test_the_local_temp_is_created_private(self) -> None:
        src = inspect.getsource(orchestration._write_secure_temp)
        self.assertIn("0o600", src)

    def test_the_local_temp_is_always_removed(self) -> None:
        src = inspect.getsource(orchestration._push_peer_text_file)
        self.assertIn("finally:", src)
        self.assertIn("local.unlink()", src)


class StagedFileCleanupTests(unittest.TestCase):
    def _inner(self) -> str:
        return inspect.getsource(orchestration._install_staged_peer_file)

    def test_the_staged_copy_is_removed_even_when_install_fails(self) -> None:
        src = self._inner()
        # && would skip the removal on failure, which is exactly the path that
        # leaves a secret behind.
        self.assertNotIn("{dest} && \"\n        f\"rm -f", src)
        self.assertIn("rm -f {remote_tmp}; ", src)
        self.assertIn("exit $rc", src)

    def test_the_staged_copy_is_locked_down_before_install(self) -> None:
        self.assertIn("chmod 600 {remote_tmp}", self._inner())

    def test_a_failed_install_still_reports_failure(self) -> None:
        # Cleanup must not swallow the exit code.
        src = self._inner()
        self.assertIn("|| rc=$?", src)

    def test_unsafe_modes_owners_and_paths_are_refused(self) -> None:
        src = self._inner()
        self.assertIn('refusing unsafe install mode', src)
        self.assertIn('refusing unsafe install owner', src)
        self.assertIn('refusing unsafe install path', src)

    def test_a_planted_symlink_is_refused(self) -> None:
        self.assertIn("is a symlink", self._inner())


class BootEnableTests(unittest.TestCase):
    """A node that loses power has to rejoin without a human."""

    def _form(self) -> str:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        return (
            repo / "ansible/roles/cluster_setup/tasks/form.yml"
        ).read_text(encoding="utf-8")

    def test_boot_enable_is_not_gated_on_first_formation(self) -> None:
        text = self._form()
        head = text.index("Enable cluster start at boot")
        block = text[head : head + 600]
        # Gated on formation, a pair built by an earlier version kept corosync
        # and pacemaker disabled at boot, and a rebooted node never came back.
        self.assertNotIn("cluster_setup_formed_this_run", block)
        self.assertIn("pcs cluster enable --all", block)

    def test_it_is_verified_not_just_attempted(self) -> None:
        text = self._form()
        self.assertIn("systemctl is-enabled", text)
        self.assertIn("rejoins by itself", text)

    def test_the_check_only_fails_on_states_that_do_not_start(self) -> None:
        text = self._form()
        # enabled-runtime, indirect, alias and generated all start the unit.
        # Requiring the literal "enabled" would fail healthy hosts.
        self.assertIn("['disabled', 'masked', 'linked']", text)


if __name__ == "__main__":
    unittest.main()
