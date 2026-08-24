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
    def test_peer_console_sync_always_copies_users_json(self) -> None:
        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "orchestration.py"
        ).read_text(encoding="utf-8")
        self.assertIn("push_local_users_to_peer", text)
        noop_idx = text.find("Peer console already has TOPOLOGY=2vm")
        users_idx = text.find("push_local_users_to_peer")
        self.assertGreater(noop_idx, 0)
        self.assertGreater(users_idx, noop_idx)
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


if __name__ == "__main__":
    unittest.main()
