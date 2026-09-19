"""Extending the mail disk of a multi deployment.

The console runs on the edge and the operator never logs into the mailbox -
that is the shape of a multi deployment, not an inconvenience. But the mail
store is on the mailbox, so "Extend a disk" for mail could not reach the only
disk that fills up, and the refusal told the operator to go and run it by hand
on the machine this product exists to keep them out of.

Nothing about the decision moved. grow-disk.sh runs on the mailbox, with the
mailbox's own view of its disks, and every refusal it makes is made there.
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest import mock

from kin_privhelper import commands

REPO = Path(__file__).resolve().parents[3]


def _collect(gen) -> tuple[list[str], int]:
    import asyncio

    async def run() -> tuple[list[str], int]:
        out: list[str] = []
        code = -1
        async for ev in gen:
            if ev.get("type") == "done":
                code = int(ev.get("exit_code", -1))
            elif isinstance(ev.get("data"), str):
                out.append(ev["data"])
        return out, code

    return asyncio.run(run())


async def _copy_ok(argv: list[str], env: dict[str, str]) -> int:
    """The push step succeeded. Tests about what runs afterwards should not
    also be testing scp."""
    return 0


SPLIT_CFG = {
    "TOPOLOGY": "split",
    "MAILBOX_IP": "192.0.2.11",
    "MAILBOX_HOST": "store.example.test",
    "MAILBOX_SSH_USER": "kin",
    "KIN_USER_PASS": "s3cret-not-in-argv",
}


class TheMailDiskIsGrownWhereItLives(unittest.TestCase):
    def test_a_split_sends_the_request_to_the_mailbox(self) -> None:
        seen: dict[str, object] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            yield {"type": "stdout", "data": "KIN_GROW_NOTHING_TO_DO=1\n"}
            yield {"type": "done", "exit_code": 1}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT_CFG), \
             mock.patch.object(commands, "_local_ipv4s", return_value=["192.0.2.10"]), \
             mock.patch.object(commands, "shutil") as sh, \
             mock.patch.object(commands, "resolve_grow_disk", return_value=Path("/x/grow-disk.sh")), \
             mock.patch.object(commands, "_run_quiet", _copy_ok), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            sh.which.return_value = "/usr/bin/sshpass"
            out, code = _collect(commands.cmd_grow_disk({"op": "plan", "target": "mail"}))

        argv = seen["argv"]
        self.assertEqual(argv[0], "sshpass")
        self.assertIn("kin@192.0.2.11", argv)
        self.assertTrue(
            any("grow-disk.sh plan /opt/zimbra" in a for a in argv),
            f"the remote command is not the one expected: {argv}",
        )
        self.assertEqual(code, 1)
        self.assertTrue(any("store.example.test" in line for line in out))

    def test_the_password_never_reaches_the_command_line(self) -> None:
        """argv is readable by every process on the box for as long as the
        command runs. It goes in the environment and on stdin, nowhere else."""
        seen: dict[str, object] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT_CFG), \
             mock.patch.object(commands, "_local_ipv4s", return_value=["192.0.2.10"]), \
             mock.patch.object(commands, "shutil") as sh, \
             mock.patch.object(commands, "resolve_grow_disk", return_value=Path("/x/grow-disk.sh")), \
             mock.patch.object(commands, "_run_quiet", _copy_ok), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            sh.which.return_value = "/usr/bin/sshpass"
            _collect(commands.cmd_grow_disk({"op": "plan", "target": "mail"}))

        argv = seen["argv"]
        for arg in argv:
            self.assertNotIn(SPLIT_CFG["KIN_USER_PASS"], arg)
        self.assertEqual(seen["kw"]["extra_env"]["SSHPASS"], SPLIT_CFG["KIN_USER_PASS"])
        self.assertIn(SPLIT_CFG["KIN_USER_PASS"], seen["kw"]["secrets"])

    def test_the_reclaim_flag_is_carried_but_never_invented(self) -> None:
        seen: dict[str, list[str]] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = argv
            yield {"type": "done", "exit_code": 0}

        for reclaim, expect in ((False, False), (True, True)):
            with mock.patch.object(commands, "_appliance_config", return_value=SPLIT_CFG), \
                 mock.patch.object(commands, "_local_ipv4s", return_value=["192.0.2.10"]), \
                 mock.patch.object(commands, "shutil") as sh, \
                 mock.patch.object(commands, "_stream_subprocess", fake_stream), \
                 mock.patch.object(commands, "_run_quiet", _copy_ok), \
                 mock.patch.object(commands, "try_lock_maintenance", create=True):
                sh.which.return_value = "/usr/bin/sshpass"
                _collect(
                    commands._grow_disk_on_mailbox(
                    SPLIT_CFG, "apply", reclaim, Path("/x/grow-disk.sh"), "/opt/zimbra"
                )
                )
            joined = " ".join(seen["argv"])
            self.assertEqual("--reclaim-reserved" in joined, expect)

    def test_no_password_is_a_refusal_not_a_prompt(self) -> None:
        """Without it ssh would sit waiting for input nobody can type, and the
        console would show a spinner that never ends."""
        cfg = dict(SPLIT_CFG)
        cfg.pop("KIN_USER_PASS")
        out, code = _collect(commands._grow_disk_on_mailbox(
            cfg, "plan", False, Path("/x/grow-disk.sh"), "/opt/zimbra"
        ))
        self.assertEqual(code, 2)
        self.assertTrue(any("password" in line.lower() for line in out))

    def test_a_missing_address_still_refuses_outright(self) -> None:
        cfg = {"TOPOLOGY": "split", "MAILBOX_HOST": "store.example.test"}
        with mock.patch.object(commands, "_appliance_config", return_value=cfg), \
             mock.patch.object(commands, "_local_ipv4s", return_value=["192.0.2.10"]):
            out, code = _collect(
                commands.cmd_grow_disk({"op": "plan", "target": "mail"})
            )
        self.assertEqual(code, 2)
        self.assertTrue(any("mail store is on" in line for line in out))

    def test_a_single_appliance_still_runs_locally(self) -> None:
        """Nothing about the one-machine case changes."""
        seen: dict[str, list[str]] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = argv
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value={"TOPOLOGY": "1vm"}), \
             mock.patch.object(commands, "resolve_grow_disk", return_value=Path("/x/grow-disk.sh")), \
             mock.patch.object(commands, "_run_quiet", _copy_ok), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            _collect(commands.cmd_grow_disk({"op": "plan", "target": "mail"}))
        self.assertNotIn("sshpass", seen["argv"][0])
        self.assertIn("/opt/zimbra", seen["argv"])

    def test_the_system_disk_is_always_this_machine(self) -> None:
        """Only the mail store moved. The system disk of whichever node the
        console runs on is still that node's, and routing it over SSH would
        grow the wrong machine."""
        seen: dict[str, list[str]] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = argv
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT_CFG), \
             mock.patch.object(commands, "_local_ipv4s", return_value=["192.0.2.10"]), \
             mock.patch.object(commands, "resolve_grow_disk", return_value=Path("/x/grow-disk.sh")), \
             mock.patch.object(commands, "_run_quiet", _copy_ok), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            _collect(commands.cmd_grow_disk({"op": "plan", "target": "system"}))
        self.assertNotIn("sshpass", seen["argv"][0])
        self.assertIn("/", seen["argv"])


    def test_the_console_pushes_its_own_helper_first(self) -> None:
        """The mailbox's copy was left there by the last deploy, so it is
        whatever version that was - and this is the path that deletes a
        partition on the machine holding every message. Version skew is not a
        risk worth carrying here."""
        pushed: dict[str, object] = {}

        async def fake_quiet(argv, env):
            pushed["argv"] = argv
            pushed["env"] = env
            return 0

        async def fake_stream(argv, **kw):
            pushed["run"] = argv
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "shutil") as sh, \
             mock.patch.object(commands, "_run_quiet", fake_quiet), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            sh.which.return_value = "/usr/bin/sshpass"
            _collect(
                commands._grow_disk_on_mailbox(
                    SPLIT_CFG, "plan", False, Path("/x/grow-disk.sh"), "/opt/zimbra"
                )
            )
        self.assertEqual(pushed["argv"][0], "sshpass")
        self.assertIn("scp", pushed["argv"])
        self.assertIn("/x/grow-disk.sh", pushed["argv"])
        # And the pushed copy is what runs, installed root-owned first so the
        # login account cannot swap it in between.
        run = " ".join(pushed["run"])
        self.assertIn("install -m 0700 -o root -g root", run)
        self.assertIn("/usr/local/lib/kin-grow-disk.sh", run)

    def test_a_failed_push_changes_nothing(self) -> None:
        async def fake_quiet(argv, env):
            return 1

        ran = {"called": False}

        async def fake_stream(argv, **kw):
            ran["called"] = True
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "shutil") as sh, \
             mock.patch.object(commands, "_run_quiet", fake_quiet), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            sh.which.return_value = "/usr/bin/sshpass"
            out, code = _collect(
                commands._grow_disk_on_mailbox(
                    SPLIT_CFG, "apply", True, Path("/x/grow-disk.sh"), "/opt/zimbra"
                )
            )
        self.assertEqual(code, 2)
        self.assertFalse(ran["called"], "ran the resize after failing to copy it")
        self.assertTrue(any("Nothing on either machine" in line for line in out))


class TheRemoteCommandIsFixed(unittest.TestCase):
    def test_no_path_from_the_browser_can_reach_it(self) -> None:
        """The console sends the NAME of a target; this file maps that to one of
        two mountpoints. A path arriving from outside would be a path being run
        as root on another machine."""
        src = inspect.getsource(commands._grow_disk_on_mailbox)
        self.assertIn("mountpoint not in GROW_TARGETS.values()", src)
        self.assertNotIn("args", src)

    def test_a_mountpoint_outside_the_table_is_refused(self) -> None:
        """cmd_grow_disk already maps a NAME to one of two mountpoints. This is
        the second lock: a future caller cannot turn this into "run a path as
        root on the other machine"."""
        out, code = _collect(
            commands._grow_disk_on_mailbox(
                SPLIT_CFG, "apply", True, Path("/x/g.sh"), "/etc"
            )
        )
        self.assertEqual(code, 2)
        self.assertTrue(any("unknown mountpoint" in line for line in out))

    def test_the_system_disk_of_the_mailbox_is_reachable(self) -> None:
        """node=mailbox with target=system. Without it the 50 GB added to the
        mailbox's system disk could not be collected from the console at all."""
        seen: dict[str, list[str]] = {}

        async def fake_stream(argv, **kw):
            seen["argv"] = list(argv)
            yield {"type": "done", "exit_code": 0}

        with mock.patch.object(commands, "_appliance_config", return_value=SPLIT_CFG), \
             mock.patch.object(commands, "_local_ipv4s", return_value=["192.0.2.10"]), \
             mock.patch.object(commands, "resolve_grow_disk", return_value=Path("/x/g.sh")), \
             mock.patch.object(commands, "shutil") as sh, \
             mock.patch.object(commands, "_run_quiet", _copy_ok), \
             mock.patch.object(commands, "_stream_subprocess", fake_stream):
            sh.which.return_value = "/usr/bin/sshpass"
            _collect(
                commands.cmd_grow_disk(
                    {"op": "plan", "target": "system", "node": "mailbox"}
                )
            )
        run = " ".join(seen["argv"])
        self.assertIn("kin@192.0.2.11", run)
        self.assertIn("kin-grow-disk.sh plan /", run)
        self.assertNotIn("/opt/zimbra", run)

    def test_a_single_appliance_has_no_mailbox_to_address(self) -> None:
        with mock.patch.object(commands, "_appliance_config", return_value={"TOPOLOGY": "1vm"}), \
             mock.patch.object(commands, "resolve_grow_disk", return_value=Path("/x/g.sh")):
            out, code = _collect(
                commands.cmd_grow_disk(
                    {"op": "plan", "target": "system", "node": "mailbox"}
                )
            )
        self.assertEqual(code, 2)
        self.assertTrue(any("single server" in line for line in out))

    def test_it_does_not_run_whatever_the_last_deploy_left_there(self) -> None:
        """Running the mailbox's own copy would mean running whatever version
        the last deploy happened to put there, on the code path that deletes a
        partition."""
        src = inspect.getsource(commands._grow_disk_on_mailbox)
        self.assertNotIn("/opt/kin-mail-deploy", src)
        self.assertIn("/usr/local/lib/kin-grow-disk.sh", src)

    def test_known_hosts_is_somewhere_privhelperd_can_write(self) -> None:
        """ProtectHome=true puts /root out of reach, so ssh cannot use its
        default location; the orchestrator already uses this file."""
        self.assertEqual(
            commands.MAILBOX_KNOWN_HOSTS, "/etc/kin-mail/split-known-hosts"
        )
        # The orchestrator builds the same path from CONF_DIR rather than
        # spelling it out; what matters is that both use the one file.
        orch = (REPO / "install/kin-mail-split.sh").read_text(encoding="utf-8")
        self.assertIn("split-known-hosts", orch)


if __name__ == "__main__":
    unittest.main()
