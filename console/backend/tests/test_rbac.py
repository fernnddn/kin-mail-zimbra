"""rbac.command_allowed: unclassified commands default-allow for every role.

That default is intentional for the handful of commands actually meant to be
open to every role (read-only status, and create_mailbox's self-service path
for customer_admin, gated separately by the quota check) — but it means a new
privhelper command added without an explicit rbac.py classification silently
inherits "allowed for everyone, including customer_admin" rather than failing
closed. This pins the exact set audited and accepted as safe, so adding a new
HANDLERS entry (kin_privhelper/commands.py) without deciding its RBAC bucket
breaks this test instead of silently widening access.
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

        # Audited 2026-08-24: read-only status/log commands, plus
        # create_mailbox (deliberately open to customer_admin for
        # self-service, bounded by the quota gate) and
        # clear_initial_console_password (a global idempotent one-time
        # flag with no per-user targeting, so nothing to escalate).
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
            "A privhelper command's RBAC exposure changed without review — "
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
