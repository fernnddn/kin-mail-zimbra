"""Guards on the Install monitoring command.

It is additive and low stakes in itself, but it runs ansible on a live mail
node and an operator can press it at any moment, including in the middle of a
Build HA pair. That is what these tests are about.
"""

from __future__ import annotations

import unittest

from kin_privhelper import protocol as proto
from kin_privhelper.monitoring_install import (
    MONITORING_WORK_DIR,
    local_inventory,
)
from kin_privhelper.orchestration import WORK_DIR
from kin_privhelper.rbac import command_allowed


async def _drain(gen) -> list[dict]:
    return [ev async for ev in gen]


class InventoryTests(unittest.TestCase):
    def test_names_only_this_host_over_a_local_connection(self) -> None:
        inv = local_inventory("mail.example.test")
        self.assertIn("ansible_connection: local", inv)
        self.assertIn("mail_nodes:", inv)
        self.assertIn("        mail.example.test:", inv)
        # No peer, no witness: this command configures the node it runs on.
        self.assertNotIn("monitoring:", inv)
        self.assertNotIn("ansible_host", inv)

    def test_carries_no_credentials(self) -> None:
        inv = local_inventory("mail.example.test")
        for forbidden in ("ansible_password", "ansible_user", "lookup(", "CHAP"):
            self.assertNotIn(forbidden, inv)

    def test_a_trailing_newline_cannot_smuggle_yaml_in(self) -> None:
        """Python's `$` matches before a trailing newline, so the regex alone
        would accept "host\n". The strip is what makes that safe, and anything
        after an embedded newline is refused outright."""
        self.assertIn(
            "        mail.example.test:", local_inventory("mail.example.test\n")
        )
        with self.assertRaises(ValueError):
            local_inventory("mail.example.test\nevil_var: true")

    def test_refuses_anything_that_is_not_a_hostname(self) -> None:
        for bad in (
            "",
            "   ",
            "-leading-dash",
            "has space",
            "semi;colon",
            "new\nline",
            "quote'inject",
            "a" * 300,
        ):
            with self.assertRaises(ValueError, msg=bad):
                local_inventory(bad)


class IsolationTests(unittest.TestCase):
    def test_work_dir_is_not_the_shared_orchestration_one(self) -> None:
        """Build HA pair writes its inventory ONCE and runs every later
        playbook against that file, and it takes no maintenance lock. A second
        writer to the same path would silently repoint the rest of the build at
        this node alone, so the paths must differ.
        """
        self.assertNotEqual(MONITORING_WORK_DIR, WORK_DIR)
        self.assertFalse(
            str(MONITORING_WORK_DIR).startswith(str(WORK_DIR) + "/"),
            "monitoring work dir must not live inside the orchestration one",
        )


class LockingTests(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_while_another_cluster_operation_holds_the_lock(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper import maintenance
        from kin_privhelper.monitoring_install import cmd_install_monitoring

        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "maint.lock"
            held = maintenance.try_lock_maintenance(lock_path)
            self.assertIsNotNone(held, "could not take the lock to set the test up")
            try:
                with patch.object(maintenance, "MAINT_LOCK_PATH", lock_path):
                    events = await _drain(cmd_install_monitoring({}))
            finally:
                maintenance.release_maintenance_lock(held)

        done = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertNotEqual(done[0].get("exit_code"), 0)
        text = " ".join(str(e.get("data") or "") for e in events)
        self.assertIn("another cluster operation", text)


class RbacTests(unittest.TestCase):
    def test_ops_roles_only(self) -> None:
        self.assertTrue(
            command_allowed("kin_super_admin", proto.CMD_INSTALL_MONITORING)
        )
        self.assertTrue(
            command_allowed("kin_support_ops", proto.CMD_INSTALL_MONITORING)
        )
        self.assertFalse(
            command_allowed("customer_admin", proto.CMD_INSTALL_MONITORING)
        )


if __name__ == "__main__":
    unittest.main()
