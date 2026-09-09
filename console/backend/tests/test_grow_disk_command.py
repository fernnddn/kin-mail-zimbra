"""Growing a filesystem is the most destructive thing this console can do.

It moves a partition boundary on a live mail server. The decisions that could
destroy data are all in install/lib/grow-disk.sh and have their own test suite;
these tests cover the parts only the daemon can enforce, and the shape of the
request that reaches it.

The single most important property here is that the console sends a NAME, not
a path. A request that can name an arbitrary mountpoint is a request that can
be aimed at the wrong disk, and this command is reachable over HTTP.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest import mock

from kin_privhelper import commands, daemon, deploy_state, protocol as proto, rbac


def _drain(gen) -> list[dict]:
    async def go() -> list[dict]:
        return [ev async for ev in gen]

    return asyncio.run(go())


def _text(events: list[dict]) -> str:
    return " ".join(str(e.get("data") or "") for e in events)


def _exit(events: list[dict]) -> int | None:
    done = [e for e in events if e.get("type") == "done"]
    return done[-1].get("exit_code") if done else None


class TheConsoleNamesATargetItDoesNotChooseAPath(unittest.TestCase):
    def test_only_two_targets_exist(self) -> None:
        self.assertEqual(set(commands.GROW_TARGETS), {"system", "mail"})
        self.assertEqual(commands.GROW_TARGETS["system"], "/")

    def test_an_unknown_target_is_refused(self) -> None:
        events = _drain(commands.cmd_grow_disk({"op": "plan", "target": "/etc"}))
        self.assertNotEqual(_exit(events), 0)
        self.assertIn("system or mail", _text(events))

    def test_a_path_cannot_be_smuggled_in_as_a_target(self) -> None:
        for hostile in ("/", "/opt/zimbra", "../../dev/sda", "/dev/sda2", ""):
            with self.subTest(target=hostile):
                events = _drain(commands.cmd_grow_disk({"op": "apply", "target": hostile}))
                self.assertNotEqual(_exit(events), 0, f"{hostile!r} was accepted as a target")

    def test_an_unknown_operation_is_refused(self) -> None:
        events = _drain(commands.cmd_grow_disk({"op": "shrink", "target": "system"}))
        self.assertNotEqual(_exit(events), 0)
        self.assertIn("plan or apply", _text(events))

    def test_the_http_layer_validates_before_the_helper_is_reached(self) -> None:
        # Defence in depth: the console refuses a bad op or target itself, so a
        # crafted request never becomes a privhelper call at all.
        src = inspect.getsource(__import__("kin_console.app", fromlist=["app"]))
        block = src.split("elif cmd == proto.CMD_GROW_DISK:")[1].split("elif cmd in (")[0]
        self.assertIn('op not in ("plan", "apply")', block)
        self.assertIn('target not in ("system", "mail")', block)


class PlanningIsReadOnlyApplyingIsNot(unittest.TestCase):
    def test_planning_does_not_take_the_maintenance_lock(self) -> None:
        # Looking at what WOULD happen while a deploy runs is exactly when an
        # operator wants to look, so the read-only path must not be blocked.
        src = inspect.getsource(commands.cmd_grow_disk)
        plan_block = src.split('if op == "plan":')[1].split("lock_fh =")[0]
        self.assertNotIn("try_lock_maintenance", plan_block)

    def test_applying_takes_the_lock_before_running_anything(self) -> None:
        src = inspect.getsource(commands.cmd_grow_disk)
        self.assertLess(
            src.index("try_lock_maintenance"),
            src.index('"apply", mountpoint'),
            "the resize starts before the lock is taken",
        )

    def test_a_busy_appliance_refuses_to_resize(self) -> None:
        from kin_privhelper import maintenance

        with mock.patch.object(commands, "resolve_grow_disk", lambda: __import__("pathlib").Path("/bin/true")), \
             mock.patch.object(maintenance, "try_lock_maintenance", lambda: None):
            events = _drain(commands.cmd_grow_disk({"op": "apply", "target": "system"}))
        self.assertNotEqual(_exit(events), 0)
        self.assertIn("another operation is already running", _text(events))

    def test_the_lock_is_released_even_when_the_client_disappears(self) -> None:
        src = inspect.getsource(commands.cmd_grow_disk)
        self.assertIn("finally:", src)
        self.assertIn("release_maintenance_lock(lock_fh)", src)


class ItIsRegisteredEverywhereItHasToBe(unittest.TestCase):
    """The 0.1.5 class of bug: a command wired into three lists out of four."""

    def test_the_daemon_will_dispatch_it(self) -> None:
        self.assertIn(proto.CMD_GROW_DISK, proto.ALLOWED_COMMANDS)
        self.assertIn(proto.CMD_GROW_DISK, commands.HANDLERS)

    def test_it_is_an_ops_operation(self) -> None:
        self.assertIn(proto.CMD_GROW_DISK, rbac.SENSITIVE_OPS_COMMANDS)

    def test_a_customer_admin_cannot_resize_a_disk(self) -> None:
        for role in rbac.ALL_ROLES:
            allowed = rbac.command_allowed(role, proto.CMD_GROW_DISK)
            with self.subTest(role=role):
                if role in rbac.OPS_ROLES:
                    self.assertTrue(allowed, f"{role} should be able to extend a disk")
                else:
                    self.assertFalse(allowed, f"{role} must not be able to move a partition")

    def test_the_anonymous_wizard_cannot_resize_a_disk(self) -> None:
        # Before the first deploy there is no login at all. Moving a partition
        # boundary is not something an unauthenticated caller may ever do.
        self.assertNotIn(proto.CMD_GROW_DISK, deploy_state.SETUP_ALLOWED_COMMANDS)

    def test_it_has_a_busy_label(self) -> None:
        # Without one the operator is shown a raw command id when it clashes.
        self.assertIn(proto.CMD_GROW_DISK, daemon._CMD_LABELS)

    def test_the_audit_log_records_what_was_grown(self) -> None:
        src = inspect.getsource(daemon._handle) if hasattr(daemon, "_handle") else ""
        source = src or open(daemon.__file__, encoding="utf-8").read()
        self.assertIn("proto.CMD_GROW_DISK", source)
        self.assertIn('f"{cmd}:{gop}"', source)


class TheHelperScriptIsShippedAndSane(unittest.TestCase):
    def test_the_script_exists_and_is_executable(self) -> None:
        import os
        from pathlib import Path

        script = Path(__file__).resolve().parents[3] / "install/lib/grow-disk.sh"
        self.assertTrue(script.is_file(), "grow-disk.sh is not in the tree")
        self.assertTrue(os.access(script, os.X_OK), "grow-disk.sh is not executable")

    def test_it_is_resolved_from_the_deploy_tree_not_an_arbitrary_path(self) -> None:
        self.assertEqual(
            commands.GROW_DISK_CANDIDATES,
            ("install/lib/grow-disk.sh", "lib/grow-disk.sh"),
        )

    def test_the_script_has_no_way_to_shrink(self) -> None:
        from pathlib import Path

        body = (
            Path(__file__).resolve().parents[3] / "install/lib/grow-disk.sh"
        ).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        for dangerous in ("mkfs", "sfdisk", "wipefs", "resizepart", "--force"):
            with self.subTest(term=dangerous):
                self.assertNotIn(dangerous, code)


if __name__ == "__main__":
    unittest.main()


class BothDiskLayoutsAreHandled(unittest.TestCase):
    """A single-disk appliance and a two-disk one are different machines.

    On one disk /opt/zimbra is a directory on the root filesystem, so the two
    storage cards show identical figures and extending either grows both. On
    two disks the data disk carries a 256 MiB replication meta partition at its
    very end, so the mail partition is never the last one and new space lands
    behind the meta rather than behind the data.

    Both are correct layouts. What matters is that the console says which one
    it is looking at, because an operator who reads a single disk as two will
    size the next one wrongly, and one who is told only "something is in the
    way" will think the feature is broken.
    """

    def test_the_host_facts_say_when_the_two_mounts_are_one_filesystem(self) -> None:
        from kin_console import monitoring

        self.assertTrue(monitoring.same_filesystem("/", "/"))
        self.assertFalse(monitoring.same_filesystem("/", "/proc"))

    def test_an_unreadable_path_is_not_reported_as_shared(self) -> None:
        # Guessing "shared" for a path that cannot be read would put a note on
        # a two-disk appliance saying it has one disk.
        from kin_console import monitoring

        self.assertFalse(monitoring.same_filesystem("/", "/definitely-not-here"))

    def test_the_flag_reaches_the_payload(self) -> None:
        from kin_console import monitoring

        src = inspect.getsource(monitoring)
        self.assertIn('"disks_share_a_filesystem": same_filesystem(', src)

    def test_the_console_renders_the_note(self) -> None:
        from pathlib import Path

        tab = (
            Path(__file__).resolve().parents[3]
            / "console/frontend/src/monitoring/MonitoringTab.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("disks_share_a_filesystem", tab)
        self.assertIn("not a separate volume", tab)

    def test_the_helper_names_the_meta_partition_rather_than_guessing(self) -> None:
        # The two-disk refusal has to name what is in the way. Its own test
        # suite drives this against a fixture; this pins that the reason code
        # exists at all, so it cannot be quietly folded back into the generic
        # not-last-partition message.
        from pathlib import Path

        lib = (
            Path(__file__).resolve().parents[3] / "install/lib/grow-disk.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("meta-partition-in-the-way", lib)
        self.assertIn("drbd-meta", lib)
