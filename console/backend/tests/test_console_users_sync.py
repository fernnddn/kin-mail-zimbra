"""Console user sync: Promoted-node writes, replica-first, fail closed."""

from __future__ import annotations

import unittest
from pathlib import Path

from kin_console.users import (
    AUTH_AD,
    AUTH_LOCAL,
    ConsoleUser,
    apply_create_user,
    apply_delete_user,
    apply_set_local_password,
)
from kin_privhelper import console_users_sync as cus
from kin_privhelper.console_users_sync import (
    SYNC_FAIL,
    apply_mutation_to_users,
    commit_users_store,
    plan_console_users_commit,
)
from kin_privhelper.rbac import ROLE_CUSTOMER_ADMIN, ROLE_SUPER_ADMIN


def _admin() -> ConsoleUser:
    return ConsoleUser(
        username="admin",
        role=ROLE_SUPER_ADMIN,
        auth_type=AUTH_LOCAL,
        password_hash="$2b$12$not-a-real-hash-value-placeholderxxxx",
        ad_username="",
        disabled=False,
    )


class PlanConsoleUsersCommitTests(unittest.TestCase):
    def test_1vm_is_local_only(self) -> None:
        plan = plan_console_users_commit(
            topology="1vm",
            local_host="mail.example.test",
            promoted=None,
            replica_ip="",
            replica_name="",
            promoted_ip="",
        )
        self.assertEqual(plan["action"], "commit_local")

    def test_unknown_topology_without_promoted_is_local(self) -> None:
        plan = plan_console_users_commit(
            topology="",
            local_host="mail.example.test",
            promoted=None,
            replica_ip="192.0.2.14",
            replica_name="mail2.example.test",
            promoted_ip="",
        )
        self.assertEqual(plan["action"], "commit_local")

    def test_2vm_without_promoted_refuses(self) -> None:
        plan = plan_console_users_commit(
            topology="2vm",
            local_host="mail.example.test",
            promoted=None,
            replica_ip="192.0.2.14",
            replica_name="mail2.example.test",
            promoted_ip="",
        )
        self.assertEqual(plan["action"], "refuse")
        self.assertIn("Promoted", plan["error"])

    def test_promoted_pushes_replica_first(self) -> None:
        plan = plan_console_users_commit(
            topology="2vm",
            local_host="mail.example.test",
            promoted="mail",
            replica_ip="192.0.2.14",
            replica_name="mail2.example.test",
            promoted_ip="192.0.2.15",
        )
        self.assertEqual(plan["action"], "push_peer_then_commit")
        self.assertEqual(plan["replica_ip"], "192.0.2.14")

    def test_unpromoted_forwards_to_promoted(self) -> None:
        plan = plan_console_users_commit(
            topology="2vm",
            local_host="mail2.example.test",
            promoted="mail.example.test",
            replica_ip="192.0.2.15",
            replica_name="mail.example.test",
            promoted_ip="192.0.2.15",
        )
        self.assertEqual(plan["action"], "forward_to_promoted")
        self.assertEqual(plan["promoted_ip"], "192.0.2.15")

    def test_promoted_without_replica_ip_refuses(self) -> None:
        plan = plan_console_users_commit(
            topology="2vm",
            local_host="mail.example.test",
            promoted="mail.example.test",
            replica_ip="",
            replica_name="mail2.example.test",
            promoted_ip="192.0.2.15",
        )
        self.assertEqual(plan["action"], "refuse")


class CommitUsersStoreTests(unittest.TestCase):
    def test_push_failure_leaves_local_unchanged(self) -> None:
        writes: list[str] = []
        pushes: list[str] = []

        def write_local() -> None:
            writes.append("local")

        def push_fail() -> tuple[bool, str]:
            pushes.append("peer")
            return False, SYNC_FAIL

        ok, err = commit_users_store(
            action="push_peer_then_commit",
            write_local=write_local,
            push_replica=push_fail,
        )
        self.assertFalse(ok)
        self.assertEqual(err, SYNC_FAIL)
        self.assertEqual(pushes, ["peer"])
        self.assertEqual(writes, [])

    def test_push_success_then_local(self) -> None:
        order: list[str] = []

        def write_local() -> None:
            order.append("local")

        def push_ok() -> tuple[bool, str]:
            order.append("peer")
            return True, "ok"

        ok, reason = commit_users_store(
            action="push_peer_then_commit",
            write_local=write_local,
            push_replica=push_ok,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "synced")
        self.assertEqual(order, ["peer", "local"])

    def test_local_write_failure_after_successful_push_reports_disagreement(self) -> None:
        # The replica already has the new state by the time write_local()
        # runs. A bare exception here used to hide that the two nodes now
        # disagree until the next successful Promoted write (re-audit,
        # 25 Aug 2026) - it must come back as a clear (False, message), not
        # propagate as an unhandled OSError.
        def write_local_fails() -> None:
            raise OSError("disk full")

        def push_ok() -> tuple[bool, str]:
            return True, "ok"

        ok, reason = commit_users_store(
            action="push_peer_then_commit",
            write_local=write_local_fails,
            push_replica=push_ok,
        )
        self.assertFalse(ok)
        self.assertIn("disk full", reason)
        self.assertIn("disagree", reason)

    def test_commit_local_writes_without_push(self) -> None:
        writes: list[str] = []
        ok, reason = commit_users_store(
            action="commit_local",
            write_local=lambda: writes.append("local"),
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "local")
        self.assertEqual(writes, ["local"])


class ApplyMutationTests(unittest.TestCase):
    def test_create_delete_and_password_hash_only(self) -> None:
        users = [_admin()]
        created, users = apply_create_user(
            users,
            "alice",
            ROLE_CUSTOMER_ADMIN,
            auth_type=AUTH_LOCAL,
            password_hash="$2b$12$alice-hash-placeholder-not-realxxxx",
        )
        self.assertEqual(created.username, "alice")
        self.assertEqual(len(users), 2)
        updated, users = apply_set_local_password(
            users,
            "alice",
            password_hash="$2b$12$alice-rotated-hash-placeholderxxxx",
        )
        self.assertEqual(updated.password_hash, "$2b$12$alice-rotated-hash-placeholderxxxx")
        remaining = apply_delete_user(users, "alice", actor="admin")
        self.assertEqual([u.username for u in remaining], ["admin"])

    def test_ad_role_assignment_has_no_password(self) -> None:
        users = [_admin()]
        created, users = apply_create_user(
            users,
            "bob",
            ROLE_CUSTOMER_ADMIN,
            auth_type=AUTH_AD,
            ad_username="bob@example.test",
        )
        self.assertEqual(created.auth_type, AUTH_AD)
        self.assertEqual(created.password_hash, "")

    def test_mutation_file_rejects_plaintext_password(self) -> None:
        with self.assertRaises(ValueError):
            apply_mutation_to_users(
                [_admin()],
                {"op": "set_password", "username": "admin", "password": "SuperSecret9"},
            )

    def test_cannot_delete_last_super_admin(self) -> None:
        with self.assertRaises(ValueError):
            apply_delete_user([_admin()], "admin", actor="someone-else")


class HaSyncWiringTests(unittest.TestCase):
    def test_peer_console_sync_pushes_users_before_any_completion_marker(self) -> None:
        # users.json must land (or the whole sync must fail) before any
        # ha-setup-complete/setup-complete/topology marker is written to the
        # peer. Users used to be pushed last: a failure there after markers
        # already landed left the peer durably "done" while this node's own
        # ha-setup-complete was never written (platform bug audit, 25 Aug
        # 2026) - the local wizard stayed on the anonymous pre-deploy Super
        # Admin identity even though mail was already live on both nodes.
        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "orchestration.py"
        ).read_text(encoding="utf-8")
        self.assertIn("push_local_users_to_peer", text)
        users_idx = text.find("push_local_users_to_peer")
        noop_idx = text.find("Peer console already has TOPOLOGY=2vm")
        ha_marker_idx = text.find("Wrote ha-setup-complete on the peer")
        self.assertGreater(users_idx, 0)
        self.assertGreater(noop_idx, 0)
        self.assertGreater(ha_marker_idx, 0)
        self.assertLess(users_idx, noop_idx)
        self.assertLess(users_idx, ha_marker_idx)
        self.assertIn("getent passwd kin-console", text)
        self.assertIn(
            "useradd --system --home /var/lib/kin-mail-console",
            text,
        )

    def test_forward_mutation_uses_password_sudo_wrapper(self) -> None:
        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "console_users_sync.py"
        ).read_text(encoding="utf-8")
        self.assertIn("wrap_privileged_remote", text)
        self.assertIn("stdin_text=password", text)
        self.assertNotIn(
            "sudo -n env PYTHONPATH=/opt/kin-mail-console/backend",
            text,
        )

    def test_live_push_then_write_paths_guard_the_local_write(self) -> None:
        # run_mutate and _apply_file each duplicate commit_users_store's
        # push_peer_then_commit logic inline (neither actually calls it in
        # production) - the OSError guard on write_local() has to be wired
        # into both of these, not just the tested-but-dead shared helper.
        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "console_users_sync.py"
        ).read_text(encoding="utf-8")
        occurrences = text.count("The two nodes now disagree")
        self.assertEqual(
            occurrences,
            3,
            "expected the disagreement message in commit_users_store, "
            "run_mutate, and _apply_file (one each)",
        )


class ApplyFileSymlinkGuardTests(unittest.IsolatedAsyncioTestCase):
    """apply-file runs as root on a path an unprivileged SSH user just scp'd -
    a symlink planted there between the scp and this step must never be
    followed, or root ends up reading an attacker-chosen file."""

    async def test_refuses_symlink_mutation_path(self) -> None:
        import json
        import os
        import tempfile

        from kin_privhelper.console_users_sync import MUTATION_REMOTE_TMP, _apply_file

        # _apply_file only accepts the exact fixed path the real scp flow
        # uses (_MUTATION_FILE_RE), so the attack has to be simulated there.
        mut_path = Path(MUTATION_REMOTE_TMP)
        if mut_path.exists() or mut_path.is_symlink():
            mut_path.unlink()
        with tempfile.TemporaryDirectory() as tmpdir:
            victim = Path(tmpdir) / "victim.json"
            victim.write_text(
                json.dumps({"op": "create", "username": "attacker", "role": ROLE_SUPER_ADMIN})
            )
            os.symlink(victim, mut_path)
            try:
                rc = await _apply_file(str(mut_path))

                self.assertEqual(rc, 2)
                self.assertFalse(mut_path.exists())  # symlink itself is cleaned up
                self.assertTrue(victim.exists())  # target file untouched
            finally:
                if mut_path.exists() or mut_path.is_symlink():
                    mut_path.unlink()


class UsersMutationLockTests(unittest.TestCase):
    """apply-file runs as its own standalone process outside privhelperd's
    _running lock (platform bug audit, 25 Aug 2026). This flock is the only
    thing serializing it against an in-daemon console-user commit racing the
    same users.json - prove contention is actually detected, not just wired.
    """

    def setUp(self) -> None:
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._old = cus.USERS_MUTATION_LOCK
        cus.USERS_MUTATION_LOCK = Path(self._td.name) / "users-mutation.lock"
        self.addCleanup(setattr, cus, "USERS_MUTATION_LOCK", self._old)

    def test_second_acquire_fails_while_first_is_held(self) -> None:
        first = cus._lock_users_mutation(timeout=1.0)
        self.assertIsNotNone(first)
        second = cus._lock_users_mutation(timeout=0.3)
        self.assertIsNone(second, "a concurrent holder must not also acquire the lock")
        cus._unlock_users_mutation(first)

    def test_lock_is_reusable_after_release(self) -> None:
        first = cus._lock_users_mutation(timeout=1.0)
        cus._unlock_users_mutation(first)
        second = cus._lock_users_mutation(timeout=1.0)
        self.assertIsNotNone(second, "lock must be acquirable again once released")
        cus._unlock_users_mutation(second)


if __name__ == "__main__":
    unittest.main()
