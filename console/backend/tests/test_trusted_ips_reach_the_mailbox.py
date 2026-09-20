"""Trusted IPs are an admin-access setting, and the admin console is elsewhere.

On a multi deployment the Zimbra admin console is served by mailboxd on port
7071, which runs on the MAILBOX. The edge does not run mailboxd at all. So
applying trusted IPs only on the machine the console happens to run on left
7071 open to the edge and to nobody else - the operator added their laptop,
watched it apply, and still could not reach the one page they wanted.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest import mock

from kin_privhelper import commands  # resolves the module cycle head-first
from kin_privhelper import appliance_settings as settings

assert commands  # imported for its side effect on import order


def _drain(gen) -> tuple[list[str], int]:
    async def run() -> tuple[list[str], int]:
        out: list[str] = []
        code = 0
        async for ev in gen:
            if ev.get("type") == "done":
                code = int(ev.get("exit_code") or 0)
            elif isinstance(ev.get("data"), str):
                out.append(ev["data"])
        return out, code

    return asyncio.run(run())


SPLIT = {
    "TOPOLOGY": "split",
    "MAILBOX_IP": "192.0.2.11",
    "MAILBOX_SSH_USER": "kin",
    "KIN_USER_PASS": "pw",
}


class TheMailboxGetsTheSameRules(unittest.TestCase):
    def test_a_split_carries_them_across(self) -> None:
        seen: dict[str, object] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = list(argv)
            seen["kw"] = kw
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT), \
             mock.patch.object(commands.shutil, "which", return_value="/usr/bin/sshpass"), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            out, _ = _drain(settings._set_firewall_on_mailbox("192.0.2.5"))

        argv = seen["argv"]
        self.assertEqual(argv[0], "sshpass")
        self.assertIn("kin@192.0.2.11", argv)
        remote = argv[-1]
        self.assertIn("10-host-firewall.sh apply", remote)
        self.assertIn("192.0.2.5", remote)
        self.assertTrue(any("reachable from those IPs" in line for line in out))

    def test_it_cancels_the_dead_man_there(self) -> None:
        """Nobody is sitting at that machine to press a button, and the proof
        the rules did not cut anything is this very connection still open."""
        seen: dict[str, list[str]] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = list(argv)
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT), \
             mock.patch.object(commands.shutil, "which", return_value="/usr/bin/sshpass"), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            _drain(settings._set_firewall_on_mailbox("192.0.2.5"))
        self.assertIn("cancel-deadman", seen["argv"][-1])

    def test_it_persists_the_value_as_well_as_passing_it(self) -> None:
        """An environment variable does not survive the next time somebody
        re-runs that stage by hand."""
        seen: dict[str, list[str]] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = list(argv)
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT), \
             mock.patch.object(commands.shutil, "which", return_value="/usr/bin/sshpass"), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            _drain(settings._set_firewall_on_mailbox("192.0.2.5"))
        remote = seen["argv"][-1]
        self.assertIn("/etc/kin-mail/config", remote)
        self.assertIn("KIN_ADMIN_IPS=", remote)

    def test_a_single_appliance_does_nothing(self) -> None:
        called = {"n": 0}

        async def fake_stream(argv, **kw):
            called["n"] += 1
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value={"TOPOLOGY": "1vm"}), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            out, _ = _drain(settings._set_firewall_on_mailbox("192.0.2.5"))
        self.assertEqual(called["n"], 0)
        self.assertEqual(out, [])

    def test_an_unreachable_mailbox_warns_and_does_not_undo(self) -> None:
        """This machine's own firewall is already applied by the time this
        runs. A mailbox that cannot be reached is worth saying, not reverting."""
        async def fake_stream(argv, **kw):
            yield {"type": "done", "exit_code": 255}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT), \
             mock.patch.object(commands.shutil, "which", return_value="/usr/bin/sshpass"), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            out, _ = _drain(settings._set_firewall_on_mailbox("192.0.2.5"))
        joined = " ".join(out)
        self.assertIn("NOT updated", joined)
        self.assertIn("admin console", joined)

    def test_missing_credentials_say_what_it_costs(self) -> None:
        cfg = {k: v for k, v in SPLIT.items() if k != "KIN_USER_PASS"}
        with mock.patch.object(commands, "_appliance_config", return_value=cfg):
            out, _ = _drain(settings._set_firewall_on_mailbox("192.0.2.5"))
        self.assertTrue(any("unreachable" in line for line in out))


class TheValueIsSafeToInterpolate(unittest.TestCase):
    def test_anything_that_is_not_an_address_is_refused_first(self) -> None:
        """The remote command builds a sed expression around this value, so the
        validation upstream is what makes that safe."""
        for bad in ("; rm -rf /", "1.2.3.4|x", "$(id)", "10.0.0.0/8 ; reboot"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    settings.parse_admin_ips(bad)

    def test_the_caller_validates_before_it_interpolates(self) -> None:
        src = inspect.getsource(settings._set_firewall)
        build = src.index("parse_admin_ips")
        hand_off = src.index("_set_firewall_on_mailbox")
        self.assertLess(build, hand_off, "interpolated before it was validated")


class TheOneWayToReachTheMailbox(unittest.TestCase):
    def test_both_callers_use_the_same_ssh_options(self) -> None:
        """Two spellings of an ssh invocation drift, and the one that drifts is
        the one nobody runs until the day it matters."""
        src = inspect.getsource(commands)
        self.assertEqual(src.count('"StrictHostKeyChecking=accept-new"'), 1)
        self.assertIn("def mailbox_ssh_argv(", src)
        settings_src = inspect.getsource(settings._set_firewall_on_mailbox)
        self.assertIn("mailbox_ssh_argv", settings_src)


if __name__ == "__main__":
    unittest.main()
