"""Mailbox argv validation and trusted-IP parsing (no live zmprov)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from kin_privhelper.appliance_settings import parse_admin_ips
from kin_privhelper.commands import validate_create_mailbox_args
from kin_privhelper.protocol import ALLOWED_COMMANDS, CMD_APPLY_APPLIANCE_SETTINGS


class MailboxArgTests(unittest.TestCase):
    @patch("kin_privhelper.commands._mail_domain_from_config", return_value="mail.example.test")
    def test_create_includes_optional_fields(self, _mock: object) -> None:
        op, argv = validate_create_mailbox_args(
            {
                "op": "create",
                "local_part": "jane.doe",
                "password": "password1",
                "display_name": "Jane Doe",
                "given_name": "Jane",
                "surname": "Doe",
                "account_status": "locked",
            }
        )
        self.assertEqual(op, "create")
        self.assertEqual(argv[0], "jane.doe@mail.example.test")
        self.assertEqual(argv[1], "password1")
        self.assertEqual(argv[2], "Jane Doe")
        self.assertIn("--given", argv)
        self.assertIn("Jane", argv)
        self.assertIn("--sn", argv)
        self.assertIn("--account-status", argv)
        self.assertIn("locked", argv)

    def test_list_delete_rename(self) -> None:
        self.assertEqual(validate_create_mailbox_args({"op": "list"})[1], ["--list"])
        self.assertEqual(
            validate_create_mailbox_args({"op": "delete", "email": "a@b.test"})[1],
            ["--delete", "a@b.test"],
        )
        op, argv = validate_create_mailbox_args(
            {"op": "rename", "email": "a@b.test", "new_local_part": "ann"}
        )
        self.assertEqual(op, "rename")
        self.assertEqual(argv, ["--rename", "a@b.test", "ann"])

    def test_rejects_bad_local_part(self) -> None:
        with self.assertRaises(ValueError):
            validate_create_mailbox_args(
                {"op": "create", "local_part": "bad@host", "password": "password1"}
            )


class AdminIpTests(unittest.TestCase):
    def test_parses_ipv4_and_cidr(self) -> None:
        self.assertEqual(
            parse_admin_ips("203.0.113.10, 198.51.100.0/24"),
            ["203.0.113.10", "198.51.100.0/24"],
        )

    def test_rejects_junk(self) -> None:
        with self.assertRaises(ValueError):
            parse_admin_ips("not-an-ip")
        with self.assertRaises(ValueError):
            parse_admin_ips("203.0.113.10/99")


class ProtocolTests(unittest.TestCase):
    def test_appliance_settings_is_whitelisted(self) -> None:
        self.assertIn(CMD_APPLY_APPLIANCE_SETTINGS, ALLOWED_COMMANDS)


if __name__ == "__main__":
    unittest.main()
