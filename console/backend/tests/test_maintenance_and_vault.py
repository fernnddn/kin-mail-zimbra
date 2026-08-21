"""Unit tests for maintenance parsers and the provisioning vault (no live cluster)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kin_console.draft import HOST_PROVISION_KEYS, WizardDraft, public_draft, valid_ipv4
from kin_privhelper.maintenance import (
    drbd_both_uptodate,
    parse_corosync_ring_addrs,
    parse_failcount_value,
    parse_offline_nodes,
    parse_online_nodes,
    parse_promoted,
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
 Online: mail.gits-it.site mail2.gits-it.site
"""

PCS_STANDBY = """
Pacemaker Nodes:
 Online: mail2.gits-it.site
 Standby: mail.gits-it.site
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
    * Promoted: [ mail2.gits-it.site ]
    * Unpromoted: [ mail.gits-it.site ]
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

DRBD_OK = """
kin-zimbra role:Primary
  disk:UpToDate
  mail.gits-it.site role:Secondary
    peer-disk:UpToDate
"""

QUORUM_OK = """
Quorate:          Yes
Flags:            Quorate Qdevice

    Nodeid      Votes    Qdevice Name
         1          1   A,V,NMW  mail.gits-it.site (local)
         2          1   A,V,NMW  mail2.gits-it.site
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
        self.assertEqual(validate_node_name("mail.gits-it.site"), "mail.gits-it.site")
        with self.assertRaises(ValueError):
            validate_node_name("mail; rm -rf /")
        with self.assertRaises(ValueError):
            validate_node_name("../../etc/passwd")

    def test_nodes_and_standby(self) -> None:
        self.assertEqual(
            parse_online_nodes(PCS_NODES),
            ["mail.gits-it.site", "mail2.gits-it.site"],
        )
        self.assertEqual(parse_standby_nodes(PCS_STANDBY), ["mail.gits-it.site"])
        self.assertEqual(parse_promoted(CRM), "mail2.gits-it.site")
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
        from kin_privhelper.maintenance import zimbra_started_on

        self.assertTrue(zimbra_started_on("    * kin-zimbra\t(ocf:kin:zimbra):\t Started mail2.gits-it.site", "mail2.gits-it.site"))
        self.assertFalse(zimbra_started_on("    * kin-zimbra Started mail2.gits-it.site", "mail.gits-it.site"))

    def test_offline_nodes(self) -> None:
        self.assertEqual(parse_offline_nodes(PCS_OFFLINE), ["mail2.example.test"])
        self.assertEqual(parse_online_nodes(PCS_OFFLINE), ["mail.example.test"])
        self.assertEqual(parse_offline_nodes(PCS_NODES), [])

    def test_drbd_and_qdevice(self) -> None:
        self.assertTrue(drbd_both_uptodate(DRBD_OK))
        self.assertFalse(drbd_both_uptodate("disk:Inconsistent\npeer-disk:UpToDate"))
        self.assertTrue(qdevice_voting(QUORUM_OK))
        self.assertFalse(qdevice_voting("Quorate: No\n"))

    def test_corosync_and_failcount(self) -> None:
        addrs = parse_corosync_ring_addrs(COROSYNC)
        self.assertEqual(addrs["mail.example.test"], "192.0.2.15")
        self.assertEqual(parse_failcount_value("scope=status  name=fail-count-kin-zimbra value=0"), 0)
        self.assertEqual(parse_failcount_value("value=3"), 3)


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


class VaultTests(unittest.TestCase):
    def test_roundtrip_and_no_plaintext_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            vault = root / "vault.json"
            marker = root / "marker.json"
            store_secrets(
                {"host_root_pass": "rootpass99", "kin_user_pass": "kinpass99"},
                key_path=key,
                vault_path=vault,
                marker_path=marker,
            )
            raw = vault.read_text(encoding="utf-8")
            self.assertNotIn("rootpass99", raw)
            self.assertNotIn("kinpass99", raw)
            loaded = load_secrets(key_path=key, vault_path=vault)
            self.assertEqual(loaded["host_root_pass"], "rootpass99")
            self.assertEqual(loaded["kin_user_pass"], "kinpass99")
            rotate_key(key_path=key, vault_path=vault, marker_path=marker)
            again = load_secrets(key_path=key, vault_path=vault)
            self.assertEqual(again["host_root_pass"], "rootpass99")


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
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "remove_host", args={"op": "apply"}))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "remove_host", args={"op": "apply"}))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "remove_observability"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "remove_observability"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "add_observability"))


if __name__ == "__main__":
    unittest.main()
