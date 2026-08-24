"""rbac.command_allowed: unclassified commands default-allow for every role.

Pins the set of commands intentionally left unclassified (and therefore
default-allowed) so a new privhelper command added without an explicit RBAC
decision breaks this test instead of silently widening access.
"""

from __future__ import annotations

import unittest

from kin_privhelper import commands, rbac


class UnclassifiedCommandsTests(unittest.TestCase):
    def test_only_known_safe_commands_default_allow(self) -> None:
        classified = (
            rbac.SENSITIVE_OPS_COMMANDS
            | rbac.SUPER_ONLY_COMMANDS
            | {"mutate_console_users", "maintenance"}
        )
        all_cmds = set(commands.HANDLERS.keys())
        unclassified = all_cmds - classified

        # Reviewed and accepted as intentionally open to every role.
        expected = {
            "get_status",
            "run_script:09-hardening.sh --status",
            "get_deploy_log",
            "create_mailbox",
            "clear_initial_console_password",
        }
        self.assertEqual(
            unclassified,
            expected,
            "A privhelper command's RBAC exposure changed without review - "
            "either classify the new/removed command in rbac.py's "
            "SENSITIVE_OPS_COMMANDS/SUPER_ONLY_COMMANDS (or the "
            "mutate_console_users/maintenance special-cases), or add it to "
            "`expected` here after confirming default-allow is intentional.",
        )

    def test_customer_admin_denied_every_classified_command(self) -> None:
        classified = (
            rbac.SENSITIVE_OPS_COMMANDS
            | rbac.SUPER_ONLY_COMMANDS
            | {"apply_appliance_settings", "get_audit_log"}
        )
        for cmd in sorted(classified):
            with self.subTest(cmd=cmd):
                self.assertFalse(
                    rbac.command_allowed(rbac.ROLE_CUSTOMER_ADMIN, cmd),
                    f"customer_admin should not be allowed to run {cmd!r}",
                )

    def test_unknown_role_denied_everything(self) -> None:
        for cmd in list(commands.HANDLERS.keys()) + ["not_a_real_command"]:
            with self.subTest(cmd=cmd):
                self.assertFalse(rbac.command_allowed("not_a_real_role", cmd))


if __name__ == "__main__":
    unittest.main()
