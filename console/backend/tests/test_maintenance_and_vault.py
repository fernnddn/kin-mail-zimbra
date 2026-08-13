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
    parse_online_nodes,
    parse_promoted,
    parse_standby_nodes,
    qdevice_voting,
    validate_node_name,
)
from kin_privhelper.provisioning_secrets import load_secrets, rotate_key, store_secrets
from kin_privhelper.rbac import ROLE_CUSTOMER_ADMIN, ROLE_SUPER_ADMIN, command_allowed


PCS_NODES = """
Pacemaker Nodes:
 Online: mail.gits-it.site mail2.gits-it.site
"""

PCS_STANDBY = """
Pacemaker Nodes:
 Online: mail2.gits-it.site
 Standby: mail.gits-it.site
"""

CRM = """
  * Clone Set: kin-drbd-clone [kin-drbd] (promotable):
    * Promoted: [ mail2.gits-it.site ]
    * Unpromoted: [ mail.gits-it.site ]
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
        from kin_privhelper.maintenance import zimbra_started_on

        self.assertTrue(zimbra_started_on("    * kin-zimbra\t(ocf:kin:zimbra):\t Started mail2.gits-it.site", "mail2.gits-it.site"))
        self.assertFalse(zimbra_started_on("    * kin-zimbra Started mail2.gits-it.site", "mail.gits-it.site"))

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

    def test_ipv4(self) -> None:
        self.assertTrue(valid_ipv4("192.0.2.12"))
        self.assertFalse(valid_ipv4("not-an-ip"))
        self.assertFalse(valid_ipv4("999.1.1.1"))


class RbacTests(unittest.TestCase):
    def test_customer_cannot_enter(self) -> None:
        self.assertTrue(command_allowed(ROLE_CUSTOMER_ADMIN, "maintenance", args={"op": "status"}))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "maintenance", args={"op": "enter"}))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "maintenance", args={"op": "enter"}))


if __name__ == "__main__":
    unittest.main()
