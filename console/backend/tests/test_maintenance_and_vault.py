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

    def test_customer_cannot_failback(self) -> None:
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "maintenance", args={"op": "failback"}))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "maintenance", args={"op": "failback"}))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "maintenance", args={"op": "failback"}))


class MaintenanceFailbackOpTests(unittest.IsolatedAsyncioTestCase):
    """Controlled Master move: ban current Promoted, wait, clear."""

    async def _run(self, target: str = "mail.example.test") -> list[dict]:
        from kin_privhelper.maintenance import cmd_maintenance

        return [ev async for ev in cmd_maintenance({"op": "failback", "target": target})]

    def _status(self, **overrides: object) -> dict:
        st: dict = {
            "local_host": "mail2.example.test",
            "topology": "2vm",
            "nodes": ["mail.example.test", "mail2.example.test"],
            "standby": [],
            "offline": [],
            "promoted": "mail2.example.test",
            "promoted_names": ["mail2.example.test"],
            "promoted_conflict": False,
            "unpromoted": ["mail.example.test"],
            "zimbra_node": "mail2.example.test",
            "vip_ip": "192.0.2.16",
            "vip_node": "mail2.example.test",
            "drbd_uptodate": True,
            "drbd_sync_percent": None,
            "qdevice_ok": True,
            "failcount_ok": True,
            "failcount_lines": [],
            # Move Master now runs the same pre-flight the Check button runs,
            # so the fake status has to carry what that reads: the addresses it
            # probes over HTTPS, and the two "cluster is not acting" states.
            "addrs": {
                "mail.example.test": "192.0.2.11",
                "mail2.example.test": "192.0.2.12",
            },
            "bans": [],
            "maintenance_mode": False,
        }
        st.update(overrides)
        # Built after the overrides so a state that moves zimbra_node also
        # moves what crm_mon would say. The pre-flight reads this to decide
        # whether Zimbra is actually up on the node that is serving mail, and
        # a blank crm would fail that check for the wrong reason.
        st["raw"] = {
            "crm": (
                "  * kin-zimbra\t(ocf:kin:zimbra):\t Started "
                f"{st['zimbra_node']}\n"
                if st.get("zimbra_node")
                else ""
            ),
            "pcs_nodes": "",
            "drbd": "",
            "quorum": "",
        }
        return st

    async def test_bans_current_then_clears_after_settle(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        target = "mail.example.test"
        current = "mail2.example.test"
        calls: list[list[str]] = []

        async def capture_side(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
            calls.append(list(argv))
            joined = " ".join(argv)
            if "kin-assert-no-dual-primary" in joined:
                return 0, "NO_DUAL_PRIMARY_OK\n", ""
            if argv[:4] == ["pcs", "resource", "ban", "--help"]:
                # pcs 0.11 advertises --promoted.
                return 0, "Usage: pcs resource ban <resource id> [node] [--promoted]\n", ""
            if argv[:3] == ["pcs", "resource", "ban"]:
                return 0, "ban ok\n", ""
            if argv[:3] == ["pcs", "resource", "clear"]:
                return 0, "clear ok\n", ""
            if argv[0] == "curl":
                return 0, "200", ""
            return 0, "", ""

        settle_states = [
            self._status(),  # initial gate
            self._status(),  # after sync wait (already uptodate)
            self._status(),  # pre-flight health gate
            # mid settle: still on current
            self._status(),
            # settled on target
            self._status(
                promoted=target,
                promoted_names=[target],
                vip_node=target,
                zimbra_node=target,
            ),
            # post-clear verify
            self._status(
                promoted=target,
                promoted_names=[target],
                vip_node=target,
                zimbra_node=target,
            ),
        ]
        status_iter = iter(settle_states)

        async def gather_side() -> dict:
            try:
                return next(status_iter)
            except StopIteration:
                return self._status(
                    promoted=target,
                    promoted_names=[target],
                    vip_node=target,
                    zimbra_node=target,
                )

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
                "kin_privhelper.maintenance.resolve_target",
                new=AsyncMock(return_value=target),
            ),
            patch(
                "kin_privhelper.maintenance._capture",
                new=AsyncMock(side_effect=capture_side),
            ),
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(side_effect=gather_side),
            ),
            patch("kin_privhelper.maintenance.FAILBACK_POLL_SEC", 0),
        ):
            events = await self._run(target)

        ban = [
            c
            for c in calls
            if c[:3] == ["pcs", "resource", "ban"] and "--help" not in c
        ]
        clear = [c for c in calls if c[:3] == ["pcs", "resource", "clear"]]
        self.assertEqual(len(ban), 1)
        self.assertEqual(ban[0][3:], ["kin-drbd-clone", current, "--promoted"])
        self.assertEqual(len(clear), 1)
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(done[-1].get("exit_code"), 0)
        self.assertTrue(any("Master move to" in str(ev.get("data")) for ev in events))

    async def _run_with(
        self,
        *,
        states: list[dict],
        capture_side,
        target: str = "mail.example.test",
    ) -> tuple[list[dict], list[list[str]]]:
        """Drive one failback with a scripted status sequence."""
        from unittest.mock import AsyncMock, MagicMock, patch

        status_iter = iter(states)
        last = states[-1]

        async def gather_side() -> dict:
            try:
                return next(status_iter)
            except StopIteration:
                return last

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
                "kin_privhelper.maintenance.resolve_target",
                new=AsyncMock(return_value=target),
            ),
            patch(
                "kin_privhelper.maintenance._capture",
                new=AsyncMock(side_effect=capture_side),
            ),
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(side_effect=gather_side),
            ),
            patch("kin_privhelper.maintenance.FAILBACK_POLL_SEC", 0),
            patch("kin_privhelper.maintenance.FAILBACK_TIMEOUT_SEC", 0),
        ):
            events = [
                ev
                async for ev in __import__(
                    "kin_privhelper.maintenance", fromlist=["cmd_maintenance"]
                ).cmd_maintenance({"op": "failback", "target": target})
            ]
        return events, self._calls

    async def test_refuses_when_the_target_is_not_healthy(self) -> None:
        """The Phase 7 failure: Move Master ran onto a node whose check failed.

        Nothing may be banned. Banning the only working Promoted node so the
        stack can land somewhere it cannot run takes mail down on both nodes,
        and that is precisely what happened live on 28 Aug 2026.
        """
        self._calls = []

        async def capture_side(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
            self._calls.append(list(argv))
            joined = " ".join(argv)
            if "kin-assert-no-dual-primary" in joined:
                return 0, "NO_DUAL_PRIMARY_OK\n", ""
            if argv[:4] == ["pcs", "resource", "ban", "--help"]:
                return 0, "Usage: pcs resource ban <resource id> [node] [--promoted]\n", ""
            if argv[0] == "curl":
                return 0, "200", ""
            return 0, "", ""

        # Zimbra is not Started anywhere, so peer_zmcontrol fails.
        unhealthy = self._status(zimbra_node=None)
        events, calls = await self._run_with(
            states=[unhealthy, unhealthy, unhealthy],
            capture_side=capture_side,
        )
        bans = [c for c in calls if c[:3] == ["pcs", "resource", "ban"] and "--help" not in c]
        self.assertEqual(bans, [], "refused move must not touch the cluster")
        text = " ".join(str(ev.get("data") or "") for ev in events)
        self.assertIn("Refusing failback", text)
        self.assertIn("peer_zmcontrol", text)
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(done[-1].get("exit_code"), 1)

    async def test_refuses_when_a_stale_ban_is_still_in_the_cib(self) -> None:
        """A ban left by an interrupted move forbids promotion cluster-wide.

        Issuing a second ban on top of it is how you end up with no node
        allowed to be Promoted at all.
        """
        self._calls = []

        async def capture_side(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
            self._calls.append(list(argv))
            if "kin-assert-no-dual-primary" in " ".join(argv):
                return 0, "NO_DUAL_PRIMARY_OK\n", ""
            if argv[:4] == ["pcs", "resource", "ban", "--help"]:
                return 0, "Usage: pcs resource ban <resource id> [node] [--promoted]\n", ""
            if argv[0] == "curl":
                return 0, "200", ""
            return 0, "", ""

        stale = self._status(
            bans=[
                {
                    "id": "cli-ban-kin-drbd-clone-on-mail.example.test",
                    "resource": "kin-drbd-clone",
                    "node": "mail.example.test",
                    "role": "Master",
                    "score": "-INFINITY",
                }
            ]
        )
        events, calls = await self._run_with(
            states=[stale, stale, stale], capture_side=capture_side
        )
        bans = [c for c in calls if c[:3] == ["pcs", "resource", "ban"] and "--help" not in c]
        self.assertEqual(bans, [])
        text = " ".join(str(ev.get("data") or "") for ev in events)
        self.assertIn("no_stale_ban", text)

    async def test_refuses_in_pacemaker_maintenance_mode(self) -> None:
        self._calls = []

        async def capture_side(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
            self._calls.append(list(argv))
            if "kin-assert-no-dual-primary" in " ".join(argv):
                return 0, "NO_DUAL_PRIMARY_OK\n", ""
            if argv[:4] == ["pcs", "resource", "ban", "--help"]:
                return 0, "Usage: pcs resource ban <resource id> [node] [--promoted]\n", ""
            if argv[0] == "curl":
                return 0, "200", ""
            return 0, "", ""

        frozen = self._status(maintenance_mode=True)
        events, calls = await self._run_with(
            states=[frozen, frozen, frozen], capture_side=capture_side
        )
        bans = [c for c in calls if c[:3] == ["pcs", "resource", "ban"] and "--help" not in c]
        self.assertEqual(bans, [])
        text = " ".join(str(ev.get("data") or "") for ev in events)
        self.assertIn("not_maintenance_mode", text)

    async def test_a_move_that_never_settles_removes_its_own_ban(self) -> None:
        """The ban must never outlive the operation that placed it.

        A -INFINITY Promoted ban on the node that was serving mail means no
        node may be promoted: DRBD has no Primary, the group cannot start, and
        nothing on screen names the constraint responsible. Telling the
        operator to SSH in and run `pcs resource clear` is not a fix.
        """
        self._calls = []

        async def capture_side(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
            self._calls.append(list(argv))
            if "kin-assert-no-dual-primary" in " ".join(argv):
                return 0, "NO_DUAL_PRIMARY_OK\n", ""
            if argv[:4] == ["pcs", "resource", "ban", "--help"]:
                return 0, "Usage: pcs resource ban <resource id> [node] [--promoted]\n", ""
            if argv[:3] == ["pcs", "resource", "ban"]:
                return 0, "ban ok\n", ""
            if argv[:3] == ["pcs", "resource", "clear"]:
                return 0, "clear ok\n", ""
            if argv[0] == "curl":
                return 0, "200", ""
            return 0, "", ""

        # Healthy enough to start, but the stack never lands on the target.
        healthy = self._status()
        events, calls = await self._run_with(
            states=[healthy] * 8, capture_side=capture_side
        )
        bans = [c for c in calls if c[:3] == ["pcs", "resource", "ban"] and "--help" not in c]
        clears = [c for c in calls if c[:3] == ["pcs", "resource", "clear"]]
        self.assertEqual(len(bans), 1, "the move should have been attempted")
        self.assertEqual(
            len(clears), 1, "the ban it placed must be removed when it fails"
        )
        text = " ".join(str(ev.get("data") or "") for ev in events)
        self.assertIn("Removing the temporary ban", text)
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(done[-1].get("exit_code"), 1)


    async def test_refuses_when_already_promoted(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        target = "mail.example.test"
        assert_path = MagicMock()
        assert_path.is_file.return_value = True

        with (
            patch(
                "kin_privhelper.maintenance.try_lock_maintenance",
                return_value=object(),
            ),
            patch("kin_privhelper.maintenance.release_maintenance_lock"),
            patch("kin_privhelper.maintenance.ASSERT_SCRIPT", assert_path),
            patch(
                "kin_privhelper.maintenance.resolve_target",
                new=AsyncMock(return_value=target),
            ),
            patch(
                "kin_privhelper.maintenance._capture",
                new=AsyncMock(return_value=(0, "NO_DUAL_PRIMARY_OK\n", "")),
            ),
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(
                    return_value=self._status(
                        promoted=target,
                        promoted_names=[target],
                        vip_node=target,
                        zimbra_node=target,
                    )
                ),
            ),
        ):
            events = await self._run(target)

        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(done[-1].get("exit_code"), 1)
        self.assertTrue(any("already Promoted" in str(ev.get("data")) for ev in events))

    async def test_refuses_when_drbd_not_uptodate(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        target = "mail.example.test"
        assert_path = MagicMock()
        assert_path.is_file.return_value = True

        with (
            patch(
                "kin_privhelper.maintenance.try_lock_maintenance",
                return_value=object(),
            ),
            patch("kin_privhelper.maintenance.release_maintenance_lock"),
            patch("kin_privhelper.maintenance.ASSERT_SCRIPT", assert_path),
            patch(
                "kin_privhelper.maintenance.resolve_target",
                new=AsyncMock(return_value=target),
            ),
            patch(
                "kin_privhelper.maintenance._capture",
                new=AsyncMock(return_value=(0, "NO_DUAL_PRIMARY_OK\n", "")),
            ),
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(
                    return_value=self._status(
                        drbd_uptodate=False,
                        drbd_sync_percent=42.0,
                    )
                ),
            ),
            patch("kin_privhelper.maintenance.FAILBACK_TIMEOUT_SEC", 0),
            patch("kin_privhelper.maintenance.FAILBACK_POLL_SEC", 0),
        ):
            events = await self._run(target)

        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(done[-1].get("exit_code"), 1)
        self.assertTrue(
            any("DRBD did not reach UpToDate" in str(ev.get("data")) for ev in events)
        )


class FailbackSettleHttpsTests(unittest.IsolatedAsyncioTestCase):
    def test_https_probe_ok_accepts_redirects(self) -> None:
        from kin_privhelper.maintenance import https_probe_ok

        self.assertTrue(https_probe_ok("200"))
        self.assertTrue(https_probe_ok("301"))
        self.assertTrue(https_probe_ok(302))
        self.assertFalse(https_probe_ok("000"))
        self.assertFalse(https_probe_ok("503"))

    async def test_settle_soft_accepts_when_stack_on_target_despite_https(self) -> None:
        from unittest.mock import AsyncMock, patch

        from kin_privhelper.maintenance import wait_failback_settled

        target = "mail2.example.test"
        st = {
            "promoted": target,
            "promoted_names": [target],
            "promoted_conflict": False,
            "vip_node": target,
            "zimbra_node": target,
            "vip_ip": "192.0.2.16",
        }
        polls = 0

        async def gather_side() -> dict:
            nonlocal polls
            polls += 1
            return dict(st)

        with (
            patch(
                "kin_privhelper.maintenance.gather_status",
                new=AsyncMock(side_effect=gather_side),
            ),
            patch(
                "kin_privhelper.maintenance._https_code",
                new=AsyncMock(return_value=("000", 7)),
            ),
            patch(
                "kin_privhelper.maintenance.ASSERT_SCRIPT",
                new=type("P", (), {"is_file": lambda self: False})(),
            ),
            patch("kin_privhelper.maintenance.FAILBACK_POLL_SEC", 0),
            patch("kin_privhelper.maintenance.FAILBACK_TIMEOUT_SEC", 30),
        ):
            ok, logs = await wait_failback_settled(target)

        self.assertTrue(ok)
        self.assertTrue(any("accepting without hard HTTPS fail" in ln for ln in logs))
        self.assertGreaterEqual(polls, 3)


if __name__ == "__main__":
    unittest.main()


class BanAndMaintenanceParseTests(unittest.TestCase):
    def test_maintenance_mode_property(self) -> None:
        from kin_privhelper.maintenance import parse_maintenance_mode

        self.assertTrue(
            parse_maintenance_mode("Cluster Properties:\n maintenance-mode: true\n")
        )
        self.assertFalse(
            parse_maintenance_mode("Cluster Properties:\n maintenance-mode: false\n")
        )
        self.assertFalse(parse_maintenance_mode("Cluster Properties:\n stonith-enabled: true\n"))

    def test_only_cli_ban_constraints_count(self) -> None:
        from kin_privhelper.maintenance import parse_ban_constraints

        xml = (
            "<constraints>"
            '<rsc_location id="cli-ban-kin-drbd-clone-on-mail1" rsc="kin-drbd-clone"'
            ' role="Master" node="mail1" score="-INFINITY"/>'
            '<rsc_location id="location-kin-vip-mail1-100" rsc="kin-vip" node="mail1"'
            ' score="100"/>'
            "</constraints>"
        )
        bans = parse_ban_constraints(xml)
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]["resource"], "kin-drbd-clone")
        self.assertEqual(bans[0]["node"], "mail1")

    def test_unreadable_cib_is_not_a_ban(self) -> None:
        from kin_privhelper.maintenance import parse_ban_constraints

        # cibadmin failing must not invent constraints that would then block
        # every Move Master with an error nobody can act on.
        self.assertEqual(parse_ban_constraints(""), [])
        self.assertEqual(parse_ban_constraints("cibadmin: connection failed"), [])


class ClusterOpsTranscriptTests(unittest.IsolatedAsyncioTestCase):
    """The record of what a cluster operation did must outlive the browser tab."""

    async def test_a_failed_move_is_still_on_disk_afterwards(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper import maintenance as m

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "sub" / "cluster-ops.log"

            async def fake_events(args):
                yield m.proto.event_stdout("Controlled Master move to mail2\n")
                yield m.proto.event_stderr("Refusing failback: peer_zmcontrol\n")
                yield m.proto.event_done(1)

            with (
                patch.object(m, "CLUSTER_OPS_LOG", log),
                patch.object(m, "_maintenance_events", fake_events),
            ):
                events = [
                    ev
                    async for ev in m.cmd_maintenance(
                        {"op": "failback", "target": "mail2.example.test"}
                    )
                ]

            self.assertEqual(events[-1]["exit_code"], 1)
            text = log.read_text(encoding="utf-8")
            # The parent directory did not exist: a transcript that needs the
            # operator to mkdir first is a transcript that is never there when
            # it matters.
            self.assertIn("failback mail2.example.test", text)
            self.assertIn("Controlled Master move to mail2", text)
            self.assertIn("Refusing failback: peer_zmcontrol", text)
            self.assertIn("finished, exit 1", text)

    async def test_reading_status_is_not_recorded(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper import maintenance as m

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cluster-ops.log"

            async def fake_events(args):
                yield m.proto.event_stdout("CLUSTER_STATUS_JSON:{}\n")
                yield m.proto.event_done(0)

            with (
                patch.object(m, "CLUSTER_OPS_LOG", log),
                patch.object(m, "_maintenance_events", fake_events),
            ):
                [ev async for ev in m.cmd_maintenance({"op": "status"})]

            # Status is polled every few seconds. Recording it would bury the
            # operations the transcript exists to preserve.
            self.assertFalse(log.exists())

    def test_an_unwritable_transcript_never_breaks_the_operation(self) -> None:
        from pathlib import Path

        from kin_privhelper.maintenance import append_cluster_ops_log

        # Under a path that cannot be created. Losing the record is bad;
        # aborting a Master move because of it would be far worse.
        append_cluster_ops_log("x", path=Path("/proc/definitely/not/here.log"))

    def test_transcript_is_trimmed_but_keeps_the_latest_entries(self) -> None:
        import tempfile
        from pathlib import Path

        from kin_privhelper.maintenance import append_cluster_ops_log

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ops.log"
            log.write_text("old\n" * 5000, encoding="utf-8")
            append_cluster_ops_log("newest line\n", path=log)
            import kin_privhelper.maintenance as m

            m._trim_ops_log(log, limit=1000)
            text = log.read_text(encoding="utf-8")
            self.assertLess(len(text), 2000)
            self.assertIn("newest line", text)
            self.assertIn("earlier entries trimmed", text)


class SingleHostStatusTests(unittest.IsolatedAsyncioTestCase):
    """A 1-host install has no Pacemaker, and the Cluster page must still load."""

    async def test_missing_cluster_binaries_do_not_break_status(self) -> None:
        from unittest.mock import AsyncMock, patch

        from pathlib import Path

        from kin_privhelper import maintenance as m

        # Every cluster tool absent, the way _capture reports it.
        async def not_installed(argv, timeout: float = 30.0):
            return 127, "", f"command not found: {argv[0]}\n"

        with (
            patch.object(m, "_capture", new=AsyncMock(side_effect=not_installed)),
            patch.object(m, "COROSYNC_CONF", Path("/nonexistent/corosync.conf")),
        ):
            st = await m.gather_status()

        # cibadmin and pcs property were both added to this call for the ban
        # and maintenance-mode checks. On a single-host appliance neither
        # exists, and inventing a ban there would block every operation with an
        # error the operator cannot act on.
        self.assertEqual(st["bans"], [])
        self.assertFalse(st["maintenance_mode"])
        self.assertEqual(st["nodes"], [])
