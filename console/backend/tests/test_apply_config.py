"""Unit tests for peer install config (no live SSH, no /etc/kin-mail writes)."""

from __future__ import annotations

import unittest

from kin_privhelper.apply_config import (
    ensure_topology_2vm,
    format_config,
    iface_for_ipv4,
    parse_config,
    parse_ip_dash_o_v4,
    peer_install_config,
)
from kin_privhelper.orchestration import (
    OrchHost,
    build_peer_install_payload,
    parse_peer_prep_readiness,
)


_PRIMARY = {
    "MAIL_DOMAIN": "example.test",
    "MAIL_HOST": "mail.example.test",
    "SERVER_IP": "192.0.2.15",
    "NET_IFACE": "ens33",
    "TIMEZONE": "Asia/Jakarta",
    "ADMIN_PASS": "PrimaryAdminPass9",
    "LE_EMAIL": "admin@example.test",
    "TLS_METHOD": "cloudflare",
    "CF_CREDS": "/etc/letsencrypt/cloudflare.ini",
    "AD_AUTH_ENABLED": "yes",
    "AD_LDAP_URL": "ldap://dc.example.test:389",
    "AD_SEARCH_BIND_PASSWORD": "AdBindSecret",
    "CONTRACTED_SEATS": "25",
    "KIN_ADMIN_IPS": "198.51.100.10",
    "ZPUSH_ENABLED": "yes",
    "ZCS_FILE": "zcs-10.1.18_GA_4200001.UBUNTU22_64.20260801175919.tgz",
    "PEER_HOST_IP": "192.0.2.14",
    "PEER_HOST_NAME": "mail2.example.test",
    "CLUSTER_VIP_IP": "192.0.2.16",
}

_PEER_IP_ADDR = (
    "2: ens34    inet 192.0.2.14/24 brd 192.0.2.255 scope global ens34\n"
    "3: ens33    inet 192.0.2.15/24 brd 192.0.2.255 scope global ens33\n"
)


class IfaceParseTests(unittest.TestCase):
    def test_matches_full_ipv4_not_a_prefix(self) -> None:
        text = (
            "2: ens33    inet 192.0.2.1/24 brd 192.0.2.255 scope global ens33\n"
            "3: ens34    inet 192.0.2.14/24 brd 192.0.2.255 scope global ens34\n"
        )
        self.assertEqual(iface_for_ipv4(text, "192.0.2.14"), "ens34")
        self.assertEqual(iface_for_ipv4(text, "192.0.2.1"), "ens33")
        self.assertEqual(iface_for_ipv4(text, "192.0.2.99"), "")
        self.assertEqual(parse_ip_dash_o_v4(text)[1], ("192.0.2.14", "ens34"))


class PeerInstallConfigTests(unittest.TestCase):
    def test_host_identity_is_not_copied_from_primary(self) -> None:
        out = peer_install_config(
            _PRIMARY,
            peer_host="mail2.example.test",
            peer_ip="192.0.2.14",
            peer_iface="ens34",
        )
        self.assertEqual(out["MAIL_HOST"], "mail2.example.test")
        self.assertEqual(out["SERVER_IP"], "192.0.2.14")
        self.assertEqual(out["NET_IFACE"], "ens34")
        self.assertEqual(out["MAIL_DOMAIN"], "example.test")
        self.assertEqual(out["ADMIN_PASS"], "PrimaryAdminPass9")
        self.assertEqual(out["TLS_METHOD"], "cloudflare")
        self.assertEqual(out["AD_SEARCH_BIND_PASSWORD"], "AdBindSecret")
        self.assertEqual(out["ZCS_FILE"], _PRIMARY["ZCS_FILE"])
        self.assertEqual(out["CLUSTER_VIP_IP"], "192.0.2.16")
        self.assertEqual(out["PEER_HOST_IP"], "192.0.2.15")
        self.assertEqual(out["PEER_HOST_NAME"], "mail.example.test")
        self.assertEqual(out["TOPOLOGY"], "2vm")

    def test_rejects_cloned_hostname_or_ip(self) -> None:
        with self.assertRaises(ValueError):
            peer_install_config(
                _PRIMARY,
                peer_host="mail.example.test",
                peer_ip="192.0.2.14",
                peer_iface="ens34",
            )
        with self.assertRaises(ValueError):
            peer_install_config(
                _PRIMARY,
                peer_host="mail2.example.test",
                peer_ip="192.0.2.15",
                peer_iface="ens34",
            )

    def test_round_trip_does_not_keep_primary_server_ip_as_self(self) -> None:
        out = peer_install_config(
            _PRIMARY,
            peer_host="mail2.example.test",
            peer_ip="192.0.2.14",
            peer_iface="ens34",
        )
        parsed = parse_config(format_config(out))
        self.assertEqual(parsed["SERVER_IP"], "192.0.2.14")
        self.assertEqual(parsed["MAIL_HOST"], "mail2.example.test")
        self.assertNotEqual(parsed["SERVER_IP"], _PRIMARY["SERVER_IP"])
        self.assertNotEqual(parsed["MAIL_HOST"], _PRIMARY["MAIL_HOST"])


class TopologyUpsertTests(unittest.TestCase):
    def test_empty_config_gains_2vm(self) -> None:
        out, changed = ensure_topology_2vm({})
        self.assertTrue(changed)
        self.assertEqual(out["TOPOLOGY"], "2vm")
        body = format_config(out)
        self.assertIn('TOPOLOGY="2vm"', body)

    def test_already_2vm_is_noop(self) -> None:
        values = {"TOPOLOGY": "2vm", "MAIL_HOST": "mail2.example.test"}
        out, changed = ensure_topology_2vm(values)
        self.assertFalse(changed)
        self.assertEqual(out["MAIL_HOST"], "mail2.example.test")

    def test_numeric_2_is_already_2vm(self) -> None:
        out, changed = ensure_topology_2vm({"TOPOLOGY": "2"})
        self.assertFalse(changed)


class PeerPayloadTests(unittest.TestCase):
    def test_payload_uses_peer_iface_and_omits_secrets_from_logs(self) -> None:
        token = "dns_cloudflare_api_token = TESTTOKEN_not_a_real_key"
        peer = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        code, logs, payload = build_peer_install_payload(
            primary=_PRIMARY,
            peer=peer,
            ip_addr_text=_PEER_IP_ADDR,
            cloudflare_text=token + "\n",
        )
        self.assertEqual(code, 0)
        joined = "\n".join(logs)
        self.assertIn("MAIL_HOST=mail2.example.test", joined)
        self.assertIn("SERVER_IP=192.0.2.14", joined)
        self.assertIn("NET_IFACE=ens34", joined)
        self.assertNotIn("PrimaryAdminPass9", joined)
        self.assertNotIn("AdBindSecret", joined)
        self.assertNotIn("TESTTOKEN_not_a_real_key", joined)
        parsed = parse_config(payload["body"])
        self.assertEqual(parsed["MAIL_HOST"], "mail2.example.test")
        self.assertEqual(parsed["SERVER_IP"], "192.0.2.14")
        self.assertEqual(parsed["NET_IFACE"], "ens34")
        self.assertEqual(parsed["ADMIN_PASS"], "PrimaryAdminPass9")
        self.assertEqual(parsed["TOPOLOGY"], "2vm")
        self.assertEqual(payload["topology"], "2vm")
        self.assertEqual(payload["cf_body"].strip(), token)

    def test_cloudflare_missing_creds_refuses(self) -> None:
        peer = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        code, logs, payload = build_peer_install_payload(
            primary=_PRIMARY,
            peer=peer,
            ip_addr_text=_PEER_IP_ADDR,
            cloudflare_text=None,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload, {})
        self.assertTrue(any("cloudflare" in line.lower() for line in logs))

    def test_manual_tls_does_not_need_cloudflare_file(self) -> None:
        primary = dict(_PRIMARY)
        primary["TLS_METHOD"] = "manual"
        peer = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        code, _logs, payload = build_peer_install_payload(
            primary=primary,
            peer=peer,
            ip_addr_text=_PEER_IP_ADDR,
            cloudflare_text=None,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("cf_body", payload)
        self.assertEqual(payload["TLS_METHOD"], "manual")


class PeerReadinessParseTests(unittest.TestCase):
    def test_ready(self) -> None:
        ok, missing = parse_peer_prep_readiness("KIN_PEER_READY\n", 0)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_reports_both_gaps(self) -> None:
        ok, missing = parse_peer_prep_readiness(
            "KIN_PEER_NOT_READY:deploy-tree sudo-n \n", 4
        )
        self.assertFalse(ok)
        self.assertEqual(len(missing), 2)
        self.assertTrue(any("kin-mail.sh" in m for m in missing))
        self.assertTrue(any("sudo -n" in m for m in missing))


if __name__ == "__main__":
    unittest.main()
