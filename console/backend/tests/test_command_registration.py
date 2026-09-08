"""A privileged command is only usable if it is registered in every list.

There are four of them, in three modules, and they are not near each other:

  protocol.CMD_*            the name
  protocol.ALLOWED_COMMANDS the daemon's whitelist, checked before dispatch
  commands.HANDLERS         the dispatch table
  rbac                      which roles may call it

install_monitoring shipped in 0.1.5 registered in three of the four. The daemon
rejected every call with "command not in whitelist", so the Install monitoring
button did nothing at all on a live appliance and the operator found it rather
than the test suite (live QA, 8 Sep 2026). The tests written at the time
checked RBAC and the handler table, which is exactly why they passed.

These tests derive the command list from the constants instead of restating it,
so a command added tomorrow is checked automatically.
"""

from __future__ import annotations

import unittest

from kin_console.app import _STREAM_ACTIONS
from kin_privhelper import protocol as proto
from kin_privhelper.commands import HANDLERS
from kin_privhelper.rbac import ALL_ROLES, command_allowed


def _declared_commands() -> dict[str, str]:
    """Every CMD_* constant on the protocol module, as {attr: value}."""
    return {
        name: getattr(proto, name)
        for name in dir(proto)
        if name.startswith("CMD_") and isinstance(getattr(proto, name), str)
    }


class CommandRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declared = _declared_commands()
        self.assertGreater(len(self.declared), 15, "command constants vanished")

    def test_every_declared_command_is_whitelisted(self) -> None:
        """The daemon checks this before dispatch. Miss it and the command is
        dead on a real appliance while every in-process test still passes."""
        missing = sorted(
            attr
            for attr, value in self.declared.items()
            if value not in proto.ALLOWED_COMMANDS
        )
        self.assertEqual(
            missing,
            [],
            "declared but not in protocol.ALLOWED_COMMANDS, so the daemon will "
            "refuse them: " + ", ".join(missing),
        )

    def test_every_declared_command_has_a_handler(self) -> None:
        missing = sorted(
            attr for attr, value in self.declared.items() if value not in HANDLERS
        )
        self.assertEqual(missing, [], "declared with no handler: " + ", ".join(missing))

    def test_every_whitelisted_command_has_a_handler(self) -> None:
        """A whitelisted command with no handler is accepted and then fails
        somewhere less obvious than the front door."""
        orphans = sorted(c for c in proto.ALLOWED_COMMANDS if c not in HANDLERS)
        self.assertEqual(orphans, [], "whitelisted with no handler: " + str(orphans))

    def test_every_handler_is_whitelisted(self) -> None:
        orphans = sorted(c for c in HANDLERS if c not in proto.ALLOWED_COMMANDS)
        self.assertEqual(orphans, [], "handler but not whitelisted: " + str(orphans))

    # Two commands decide on their args rather than their name, so asking
    # "may anyone call this" without args answers no for a reason that has
    # nothing to do with registration.
    _ARGS_FOR = {
        "mutate_console_users": {"op": "create"},
        "maintenance": {"op": "status"},
    }

    def test_every_command_is_reachable_by_some_role(self) -> None:
        """A command no role may call is dead code with a security surface."""
        unreachable = sorted(
            c
            for c in proto.ALLOWED_COMMANDS
            if not any(
                command_allowed(r, c, args=self._ARGS_FOR.get(c))
                for r in ALL_ROLES
            )
        )
        self.assertEqual(unreachable, [], "no role may call: " + str(unreachable))

    def test_every_stream_action_maps_to_a_real_command(self) -> None:
        """The console's action aliases are the browser's only way in."""
        for action, cmd in sorted(_STREAM_ACTIONS.items()):
            with self.subTest(action=action):
                self.assertIn(
                    cmd,
                    proto.ALLOWED_COMMANDS,
                    f"stream action {action!r} points at a command the daemon "
                    "will refuse",
                )
                self.assertIn(
                    cmd, HANDLERS, f"stream action {action!r} has no handler"
                )

    def test_install_monitoring_specifically(self) -> None:
        """The one that shipped broken. Named so a regression is unmistakable."""
        cmd = proto.CMD_INSTALL_MONITORING
        self.assertIn(cmd, proto.ALLOWED_COMMANDS)
        self.assertIn(cmd, HANDLERS)
        self.assertEqual(_STREAM_ACTIONS.get("install_monitoring"), cmd)
        self.assertTrue(command_allowed("kin_super_admin", cmd))
        self.assertTrue(command_allowed("kin_support_ops", cmd))
        self.assertFalse(command_allowed("customer_admin", cmd))


if __name__ == "__main__":
    unittest.main()
