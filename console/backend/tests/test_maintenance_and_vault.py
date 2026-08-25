"""Unit tests for maintenance parsers and the provisioning vault (no live cluster)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kin_console.draft import HOST_PROVISION_KEYS, WizardDraft, public_draft, valid_ipv4
from kin_privhelper.maintenance import (
    drbd_both_uptodate,
    drbd_sync_percent,
    node_ips_for_status,
    parse_corosync_ring_addrs,
    parse_failcount_value,
    parse_offline_nodes,
    parse_online_nodes,
    parse_promoted,
    parse_promoted_names,
    parse_resource_node,
    parse_standby_nodes,
    parse_unpromoted,
    qdevice_voting,
    release_maintenance_lock,
    try_lock_maintenance,
    validate_node_name,
)
from kin_privhelper.provisioning_secrets import load_secrets, rotate_key, store_secrets
from kin_privhelper.rbac import (
    ROLE_CUSTOMER_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT_OPS,
    command_allowed,
)


PCS_NODES = """
Pacemaker Nodes:
 Online: mail.example.test mail2.example.test
"""

PCS_STANDBY = """
Pacemaker Nodes:
 Online: mail2.example.test
 Standby: mail.example.test
"""

# Ubuntu 22.04 pcs 0.10 (RHBZ 1619253): a node still draining resources is
# NOT on the Standby: line. The previous parser only matched Standby:.
PCS_DRAINING = """
Pacemaker Nodes:
 Online: mail2.example.test
 Standby:
 Standby with resource(s) running: mail.example.test
 Maintenance:
 Offline:
"""

PCS_QUOTED_STAR = """
Pacemaker Nodes:
  * Online: 'mail2.example.test'
  * Standby: 'mail.example.test'
"""

PCS_OFFLINE = """
Pacemaker Nodes:
 Online: mail.example.test
 Standby:
 Offline: mail2.example.test
"""

CRM = """
  * Clone Set: kin-drbd-clone [kin-drbd] (promotable):
    * Promoted: [ mail2.example.test ]
    * Unpromoted: [ mail.example.test ]
  * kin-vip\t(ocf::heartbeat:IPaddr2):\t Started mail2.example.test
"""

CRM_MASTERS = """
 Master/Slave Set: kin-drbd-clone [kin-drbd]
     Masters: [ mail2.example.test ]
     Slaves: [ mail.example.test ]
"""

CRM_QUOTED = """
  * Clone Set: kin-drbd-clone [kin-drbd] (promotable):
    * Promoted: [ 'mail2.example.test' ]
    * Unpromoted: [ 'mail.example.test' ]
"""

CRM_DUAL_PROMOTED = """
  * Clone Set: kin-drbd-clone [kin-drbd] (promotable):
    * Promoted: [ mail.example.test mail2.example.test ]
    * Unpromoted: [ ]
  * kin-vip\t(ocf::heartbeat:IPaddr2):\t Started mail.example.test
  * kin-zimbra\t(ocf:kin:zimbra):\t Started mail.example.test
"""

DRBD_OK = """
kin-zimbra role:Primary
  disk:UpToDate
  mail.example.test role:Secondary
    peer-disk:UpToDate
"""

QUORUM_OK = """
Quorate:          Yes
Flags:            Quorate Qdevice

    Nodeid      Votes    Qdevice Name
         1          1   A,V,NMW  mail.example.test (local)
         2          1   A,V,NMW  mail2.example.test
         0          1            Qdevice
"""

COROSYNC = """
nodelist {
    node {
        ring0_addr: 192.0.2.15
        name: mail.example.test
    }
    node {
        ring0_addr: 192.0.2.14
        name: mail2.example.test
    }
}
"""


class MaintenanceParseTests(unittest.TestCase):
    def test_validate_node_name(self) -> None:
        self.assertEqual(validate_node_name("mail.example.test"), "mail.example.test")
        with self.assertRaises(ValueError):
            validate_node_name("mail; rm -rf /")
        with self.assertRaises(ValueError):
            validate_node_name("../../etc/passwd")

    def test_nodes_and_standby(self) -> None:
        self.assertEqual(
            parse_online_nodes(PCS_NODES),
            ["mail.example.test", "mail2.example.test"],
        )
        self.assertEqual(parse_standby_nodes(PCS_STANDBY), ["mail.example.test"])
        self.assertEqual(parse_promoted(CRM), "mail2.example.test")
        self.assertEqual(
            parse_standby_nodes(PCS_DRAINING),
            ["mail.example.test"],
        )
        self.assertEqual(
            parse_online_nodes(PCS_DRAINING),
            ["mail2.example.test", "mail.example.test"],
        )
        self.assertEqual(parse_standby_nodes(PCS_QUOTED_STAR), ["mail.example.test"])
        self.assertEqual(parse_promoted(CRM_MASTERS), "mail2.example.test")
        self.assertEqual(parse_unpromoted(CRM_MASTERS), ["mail.example.test"])
        self.assertEqual(parse_promoted(CRM_QUOTED), "mail2.example.test")
        self.assertEqual(parse_unpromoted(CRM_QUOTED), ["mail.example.test"])
        self.assertIsNone(parse_promoted(CRM_DUAL_PROMOTED))
        self.assertEqual(
            parse_promoted_names(CRM_DUAL_PROMOTED),
            ["mail.example.test", "mail2.example.test"],
        )
        from kin_privhelper.maintenance import zimbra_started_on

        self.assertTrue(zimbra_started_on("    * kin-zimbra\t(ocf:kin:zimbra):\t Started mail2.example.test", "mail2.example.test"))
        self.assertFalse(zimbra_started_on("    * kin-zimbra Started mail2.example.test", "mail.example.test"))

    def test_parse_resource_node(self) -> None:
        self.assertEqual(parse_resource_node(CRM, "kin-vip"), "mail2.example.test")
        self.assertEqual(parse_resource_node(CRM_DUAL_PROMOTED, "kin-zimbra"), "mail.example.test")
        self.assertIsNone(parse_resource_node(CRM, "kin-fs"))
        self.assertIsNone(
            parse_resource_node(
                "  * kin-vip\t(ocf::heartbeat:IPaddr2):\t Stopped", "kin-vip"
            )
        )

    def test_offline_nodes(self) -> None:
        self.assertEqual(parse_offline_nodes(PCS_OFFLINE), ["mail2.example.test"])
        self.assertEqual(parse_online_nodes(PCS_OFFLINE), ["mail.example.test"])
        self.assertEqual(parse_offline_nodes(PCS_NODES), [])

    def test_drbd_and_qdevice(self) -> None:
        self.assertTrue(drbd_both_uptodate(DRBD_OK))
        self.assertFalse(drbd_both_uptodate("disk:Inconsistent\npeer-disk:UpToDate"))
        self.assertTrue(qdevice_voting(QUORUM_OK))
        self.assertFalse(qdevice_voting("Quorate: No\n"))

    def test_drbd_sync_percent(self) -> None:
        # Real output from the live 2vm practice run (25 Aug 2026) that
        # prompted this: a fresh HA pair's first full sync, mid-progress.
        syncing = (
            "kin-zimbra role:Primary\n"
            "  disk:UpToDate\n"
            "  peer role:Secondary\n"
            "    replication:SyncSource peer-disk:Inconsistent done:32.50\n"
        )
        self.assertEqual(drbd_sync_percent(syncing), 32.50)
        self.assertIsNone(drbd_sync_percent(DRBD_OK))
        self.assertIsNone(drbd_sync_percent(""))

    def test_corosync_and_failcount(self) -> None:
        addrs = parse_corosync_ring_addrs(COROSYNC)
        self.assertEqual(addrs["mail.example.test"], "192.0.2.15")
        self.assertEqual(parse_failcount_value("scope=status  name=fail-count-kin-zimbra value=0"), 0)
        self.assertEqual(parse_failcount_value("value=3"), 3)
        self.assertIsNone(parse_failcount_value("garbage without a count"))
        self.assertEqual(parse_failcount_value("value=INFINITY"), 999)

    def test_node_ips_corosync_wins_over_server_ip(self) -> None:
        out = node_ips_for_status(
            {"mail.example.test": "192.0.2.15"},
            local_host="mail.example.test",
            local_ip="10.0.0.1",
        )
        self.assertEqual(out["mail.example.test"], "192.0.2.15")

    def test_node_ips_server_ip_fills_empty_corosync(self) -> None:
        out = node_ips_for_status(
            {},
            local_host="mail.example.test",
            local_ip="192.0.2.80",
        )
        self.assertEqual(out, {"mail.example.test": "192.0.2.80"})


class MaintenanceLockTests(unittest.TestCase):
    def test_second_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maintenance.lock"
            first = try_lock_maintenance(path)
            self.assertIsNotNone(first)
            second = try_lock_maintenance(path)
            self.assertIsNone(second)
            release_maintenance_lock(first)
            third = try_lock_maintenance(path)
            self.assertIsNotNone(third)
            release_maintenance_lock(third)


class MaintenanceCleanupOpTests(unittest.IsolatedAsyncioTestCase):
    """maintenance op=cleanup: the console-native replacement for having to
    SSH in and run `pcs resource cleanup` by hand to clear a stale Pacemaker
    fail-count (live 2vm practice run, 25 Aug 2026 - a single self-resolved
    monitor blip during first bring-up needed exactly this).
    """

    async def _run(self, op: str = "cleanup") -> list[dict]:
        from kin_privhelper.maintenance import cmd_maintenance

        return [ev async for ev in cmd_maintenance({"op": op})]

    def _healthy_status(self, **overrides: object) -> dict:
        st: dict = {
            "failcount_ok": True,
            "failcount_lines": [],
            "promoted_conflict": False,
            "promoted_names": ["mail.example.test"],
            "promoted": "mail.example.test",
            "topology": "2vm",
        }
        st.update(overrides)
        return st

    async def test_runs_cluster_wide_cleanup_and_reports_failcount(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        async def capture_side(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "kin-assert-no-dual-primary" in joined:
                return 0, "NO_DUAL_PRIMARY_OK\n", ""
            if argv[:3] == ["pcs", "resource", "cleanup"]:
                return 0, "Cleaned up kin-zimbra on mail.example.test", ""
            return 1, "", f"unexpected argv: {argv}"

        assert_path = MagicMock()
        assert_path.is_file.return_value = True
        assert_path.__str__.return_value = "/usr/local/sbin/kin-assert-no-dual-primary.sh"

        with (
            patch(
                "kin_privhelper.maintenance.try_lock_maintenance",
                return_value=object(),
            ),
            patch("kin_privhelper.maintenance.release_maintenance_lock"),
            patch("kin_privhelper.maintenance.ASSERT_SCRIPT", assert_path),
            patch(
                "kin_privhelper.maintenance._capture",
                new=AsyncMock(side_effect=capture_side),
            ) as capture,
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(return_value=self._healthy_status()),
            ),
        ):
            events = await self._run()

        cleanup_calls = [
            c for c in capture.await_args_list if c.args and c.args[0][:3] == ["pcs", "resource", "cleanup"]
        ]
        self.assertEqual(len(cleanup_calls), 1)
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 0)
        self.assertTrue(any("failcount_ok=True" in str(ev.get("data")) for ev in events))

    async def test_refuses_cleanup_on_promoted_conflict(self) -> None:
        from unittest.mock import AsyncMock, patch

        with (
            patch(
                "kin_privhelper.maintenance.try_lock_maintenance",
                return_value=object(),
            ),
            patch("kin_privhelper.maintenance.release_maintenance_lock"),
            patch(
                "kin_privhelper.maintenance._capture",
                new=AsyncMock(),
            ) as capture,
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(
                    return_value=self._healthy_status(
                        promoted_conflict=True,
                        promoted=None,
                        promoted_names=["mail.example.test", "mail2.example.test"],
                    )
                ),
            ),
        ):
            events = await self._run()

        capture.assert_not_awaited()
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 1)
        self.assertTrue(any("dual Promoted" in str(ev.get("data")) for ev in events))

    async def test_refuses_when_maintenance_is_already_locked(self) -> None:
        from unittest.mock import AsyncMock, patch

        with (
            patch("kin_privhelper.maintenance.try_lock_maintenance", return_value=None),
            patch("kin_privhelper.maintenance._capture", new=AsyncMock()) as capture,
        ):
            events = await self._run()

        capture.assert_not_awaited()
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 1)

    async def test_unknown_op_still_rejected(self) -> None:
        events = await self._run(op="not-a-real-op")
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 2)


class VaultTests(unittest.TestCase):
    def test_roundtrip_and_no_plaintext_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            vault = root / "vault.json"
            marker = root / "marker.json"
            store_secrets(
                {
                    "host_root_pass": "rootpass99",
                    "kin_user_pass": "kinpass99",
                    "chap_pass": "chapsecret12345",
                },
                key_path=key,
                vault_path=vault,
                marker_path=marker,
            )
            raw = vault.read_text(encoding="utf-8")
            self.assertNotIn("rootpass99", raw)
            self.assertNotIn("kinpass99", raw)
            self.assertNotIn("chapsecret12345", raw)
            loaded = load_secrets(key_path=key, vault_path=vault)
            self.assertEqual(loaded["host_root_pass"], "rootpass99")
            self.assertEqual(loaded["kin_user_pass"], "kinpass99")
            self.assertEqual(loaded["chap_pass"], "chapsecret12345")
            rotate_key(key_path=key, vault_path=vault, marker_path=marker)
            again = load_secrets(key_path=key, vault_path=vault)
            self.assertEqual(again["host_root_pass"], "rootpass99")
            self.assertEqual(again["chap_pass"], "chapsecret12345")


class DraftPublicTests(unittest.TestCase):
    def test_public_draft_strips_host_passwords(self) -> None:
        d = WizardDraft(host_root_pass="should-not-leak", kin_user_pass="also-secret")
        pub = public_draft(d)
        self.assertEqual(pub["host_root_pass"], "")
        self.assertEqual(pub["kin_user_pass"], "")
        self.assertTrue(HOST_PROVISION_KEYS)

    def test_public_draft_never_returns_admin_pass(self) -> None:
        secret = "kin-test-admin-pass-NOTREAL-4c2e"
        d = WizardDraft(admin_pass=secret, mail_domain="example.test")
        pub = public_draft(d)
        self.assertEqual(pub["admin_pass"], "")
        self.assertTrue(pub["admin_pass_set"])
        blob = json.dumps(pub)
        self.assertNotIn(secret, blob)
        empty = public_draft(WizardDraft(admin_pass="", mail_domain="example.test"))
        self.assertFalse(empty["admin_pass_set"])
        self.assertEqual(empty["admin_pass"], "")

    def test_apply_patch_keeps_admin_pass_when_client_sends_empty(self) -> None:
        from kin_console.draft import DraftPatch, apply_patch

        current = WizardDraft(admin_pass="stored-admin-secret", mail_domain="old.example")
        wiped = apply_patch(
            current,
            DraftPatch(admin_pass="", mail_domain="new.example"),
        )
        self.assertEqual(wiped.admin_pass, "stored-admin-secret")
        self.assertEqual(wiped.mail_domain, "new.example")
        replaced = apply_patch(
            current,
            DraftPatch(admin_pass="replacement-admin-9"),
        )
        self.assertEqual(replaced.admin_pass, "replacement-admin-9")
        pub = public_draft(replaced)
        self.assertEqual(pub["admin_pass"], "")
        self.assertNotIn("replacement-admin-9", json.dumps(pub))

    def test_public_draft_never_returns_ad_bind_or_test_pass(self) -> None:
        bind = "kin-test-ad-bind-NOTREAL-8a1c"
        test = "kin-test-ad-test-NOTREAL-9b2d"
        d = WizardDraft(
            ad_search_bind_password=bind,
            ad_test_pass=test,
            ad_auth_enabled=True,
        )
        pub = public_draft(d)
        self.assertEqual(pub["ad_search_bind_password"], "")
        self.assertEqual(pub["ad_test_pass"], "")
        self.assertTrue(pub["ad_search_bind_password_set"])
        self.assertTrue(pub["ad_test_pass_set"])
        blob = json.dumps(pub)
        self.assertNotIn(bind, blob)
        self.assertNotIn(test, blob)
        empty = public_draft(WizardDraft())
        self.assertFalse(empty["ad_search_bind_password_set"])
        self.assertFalse(empty["ad_test_pass_set"])
        self.assertEqual(empty["ad_search_bind_password"], "")
        self.assertEqual(empty["ad_test_pass"], "")

    def test_apply_patch_keeps_ad_passwords_when_client_sends_empty(self) -> None:
        from kin_console.draft import DraftPatch, apply_patch

        current = WizardDraft(
            ad_search_bind_password="stored-ad-bind",
            ad_test_pass="stored-ad-test",
            ad_ldap_url="ldap://old.example",
        )
        wiped = apply_patch(
            current,
            DraftPatch(
                ad_search_bind_password="",
                ad_test_pass="",
                ad_ldap_url="ldap://new.example",
            ),
        )
        self.assertEqual(wiped.ad_search_bind_password, "stored-ad-bind")
        self.assertEqual(wiped.ad_test_pass, "stored-ad-test")
        self.assertEqual(wiped.ad_ldap_url, "ldap://new.example")
        replaced = apply_patch(
            current,
            DraftPatch(ad_search_bind_password="new-bind-9", ad_test_pass="new-test-9"),
        )
        self.assertEqual(replaced.ad_search_bind_password, "new-bind-9")
        self.assertEqual(replaced.ad_test_pass, "new-test-9")
        pub = public_draft(replaced)
        self.assertEqual(pub["ad_search_bind_password"], "")
        self.assertEqual(pub["ad_test_pass"], "")
        blob = json.dumps(pub)
        self.assertNotIn("new-bind-9", blob)
        self.assertNotIn("new-test-9", blob)

    def test_ipv4(self) -> None:
        self.assertTrue(valid_ipv4("192.0.2.12"))
        self.assertFalse(valid_ipv4("not-an-ip"))
        self.assertFalse(valid_ipv4("999.1.1.1"))


class RbacTests(unittest.TestCase):
    def test_customer_cannot_enter(self) -> None:
        self.assertTrue(command_allowed(ROLE_CUSTOMER_ADMIN, "maintenance", args={"op": "status"}))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "maintenance", args={"op": "enter"}))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "maintenance", args={"op": "enter"}))

    def test_customer_cannot_cleanup_failcounts(self) -> None:
        # Same OPS_ROLES gate as enter/exit - clearing Pacemaker fail-counts
        # still mutates cluster resource state, even though it doesn't
        # stop/start/move anything.
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "maintenance", args={"op": "cleanup"}))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "maintenance", args={"op": "cleanup"}))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "maintenance", args={"op": "cleanup"}))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "remove_host", args={"op": "apply"}))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "remove_host", args={"op": "apply"}))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "remove_observability"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "remove_observability"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "add_observability"))


if __name__ == "__main__":
    unittest.main()
