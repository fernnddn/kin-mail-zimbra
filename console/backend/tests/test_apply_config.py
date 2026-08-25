"""Unit tests for peer install config (no live SSH, no /etc/kin-mail writes)."""

from __future__ import annotations

import unittest

from kin_privhelper.apply_config import (
    cloudflare_creds_error,
    ensure_topology_1vm,
    ensure_topology_2vm,
    format_config,
    iface_for_ipv4,
    parse_config,
    parse_ip_dash_o_v4,
    peer_install_config,
    pick_host_network,
)
from kin_privhelper.orchestration import (
    OrchHost,
    build_peer_install_payload,
    parse_peer_prep_readiness,
    should_refresh_peer_deploy_tree,
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
        self.assertEqual(
            pick_host_network(
                text, "default via 192.0.2.254 dev ens34 proto static\n"
            ),
            ("192.0.2.14", "ens34"),
        )
        self.assertEqual(pick_host_network(text, ""), ("192.0.2.1", "ens33"))


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
        self.assertEqual(out["TLS_METHOD"], "customer")
        self.assertEqual(out["ZPUSH_ENABLED"], "no")
        self.assertEqual(out["AD_AUTH_ENABLED"], "no")
        self.assertEqual(out["KIN_HA_PEER_INSTALL"], "yes")
        self.assertEqual(out["EXTERNAL_TEST_ADDRESS"], "")
        self.assertEqual(out["AD_SEARCH_BIND_PASSWORD"], "AdBindSecret")
        self.assertEqual(out["ZCS_FILE"], _PRIMARY["ZCS_FILE"])
        self.assertEqual(out["CLUSTER_VIP_IP"], "192.0.2.16")
        self.assertEqual(out["PEER_HOST_IP"], "192.0.2.15")
        self.assertEqual(out["PEER_HOST_NAME"], "mail.example.test")
        self.assertEqual(out["TOPOLOGY"], "2vm")

    def test_peer_drops_external_test_address(self) -> None:
        primary = dict(_PRIMARY)
        primary["EXTERNAL_TEST_ADDRESS"] = "ops@example.test"
        out = peer_install_config(
            primary,
            peer_host="mail2.example.test",
            peer_ip="192.0.2.14",
            peer_iface="ens34",
        )
        self.assertEqual(out["EXTERNAL_TEST_ADDRESS"], "")

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

    def test_demote_clears_peer_keeps_vip_and_observability(self) -> None:
        values = {
            "TOPOLOGY": "2vm",
            "PEER_HOST_IP": "192.0.2.14",
            "PEER_HOST_NAME": "mail2.example.test",
            "OBSERVABILITY_VM_IP": "192.0.2.53",
            "CLUSTER_VIP_IP": "192.0.2.16",
            "MAIL_HOST": "mail.example.test",
        }
        out, changed = ensure_topology_1vm(values)
        self.assertTrue(changed)
        self.assertEqual(out["TOPOLOGY"], "1vm")
        self.assertEqual(out["PEER_HOST_IP"], "")
        self.assertEqual(out["PEER_HOST_NAME"], "")
        # VIP and Observability stay - Pacemaker / qdevice still use them.
        self.assertEqual(out["OBSERVABILITY_VM_IP"], "192.0.2.53")
        self.assertEqual(out["CLUSTER_VIP_IP"], "192.0.2.16")
        self.assertEqual(out["MAIL_HOST"], "mail.example.test")

    def test_demote_already_1vm_and_clean_is_noop(self) -> None:
        values = {"TOPOLOGY": "1vm", "MAIL_HOST": "mail.example.test"}
        out, changed = ensure_topology_1vm(values)
        self.assertFalse(changed)
        self.assertEqual(out["MAIL_HOST"], "mail.example.test")

    def test_demote_1vm_with_leftover_peer_ip_still_writes(self) -> None:
        # A host that reached 1vm some other way but still carries a stale
        # PEER_HOST_IP must still be cleaned up, not treated as a no-op.
        values = {"TOPOLOGY": "1vm", "PEER_HOST_IP": "192.0.2.14"}
        out, changed = ensure_topology_1vm(values)
        self.assertTrue(changed)
        self.assertEqual(out["PEER_HOST_IP"], "")


class ConfigFileWriteTests(unittest.TestCase):
    """write_config_file() - the remove-host demote path needs a real write,
    not just the pure merge, so exercise it against a scratch directory.
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path as _Path
        from unittest.mock import patch

        from kin_privhelper import apply_config as ac

        self._ac = ac
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._old_dir = ac.CONF_DIR
        self._old_file = ac.CONF_FILE
        ac.CONF_DIR = _Path(self._td.name) / "kin-mail"
        ac.CONF_FILE = ac.CONF_DIR / "config"
        # os.chown(uid 0) needs real root; privhelperd always runs as root in
        # production, this test only cares that the right chown call happens.
        chown_patch = patch.object(ac.os, "chown")
        self._chown = chown_patch.start()
        self.addCleanup(chown_patch.stop)

    def tearDown(self) -> None:
        self._ac.CONF_DIR = self._old_dir
        self._ac.CONF_FILE = self._old_file

    def test_writes_fresh_file_root_owned_mode_0600(self) -> None:
        self._ac.write_config_file({"TOPOLOGY": "1vm", "MAIL_HOST": "mail.example.test"})
        self.assertTrue(self._ac.CONF_FILE.is_file())
        body = self._ac.CONF_FILE.read_text(encoding="utf-8")
        self.assertIn('TOPOLOGY="1vm"', body)
        self.assertEqual(self._ac.CONF_FILE.stat().st_mode & 0o777, 0o600)

    def test_rewrite_backs_up_previous_file(self) -> None:
        self._ac.write_config_file({"TOPOLOGY": "2vm"})
        self._ac.write_config_file({"TOPOLOGY": "1vm"})
        backups = list(self._ac.CONF_DIR.glob("config.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertIn('TOPOLOGY="2vm"', backups[0].read_text(encoding="utf-8"))
        self.assertIn('TOPOLOGY="1vm"', self._ac.CONF_FILE.read_text(encoding="utf-8"))


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
        self.assertEqual(parsed["KIN_HA_PEER_INSTALL"], "yes")
        self.assertEqual(payload["topology"], "2vm")
        self.assertEqual(payload["TLS_METHOD"], "customer")
        self.assertNotIn("cf_body", payload)

    def test_peer_does_not_need_cloudflare_creds(self) -> None:
        peer = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        code, logs, payload = build_peer_install_payload(
            primary=_PRIMARY,
            peer=peer,
            ip_addr_text=_PEER_IP_ADDR,
            cloudflare_text=None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["TLS_METHOD"], "customer")
        self.assertNotIn("cf_body", payload)
        self.assertFalse(any("cloudflare" in line.lower() and "usable" in line.lower() for line in logs))

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
        self.assertEqual(payload["TLS_METHOD"], "customer")


class CloudflareCredsTests(unittest.TestCase):
    def test_missing_file_is_an_error(self) -> None:
        err = cloudflare_creds_error("/etc/letsencrypt/cloudflare.ini", None)
        self.assertIsNotNone(err)
        self.assertIn("cloudflare.ini", err or "")
        self.assertIn("Manual", err or "")

    def test_paste_placeholder_is_an_error(self) -> None:
        err = cloudflare_creds_error(
            "/etc/letsencrypt/cloudflare.ini",
            "dns_cloudflare_api_token = PASTE_TOKEN_HERE\n",
        )
        self.assertIsNotNone(err)
        self.assertIn("PASTE", err or "")

    def test_real_token_is_ok(self) -> None:
        self.assertIsNone(
            cloudflare_creds_error(
                "/etc/letsencrypt/cloudflare.ini",
                "dns_cloudflare_api_token = TESTTOKEN_not_a_real_key\n",
            )
        )


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

    def test_current_readiness_cmd_does_not_require_nopasswd(self) -> None:
        from kin_privhelper.orchestration import PEER_OS_PREP_READINESS_CMD

        self.assertNotIn("sudo-n", PEER_OS_PREP_READINESS_CMD)
        self.assertIn("deploy-tree", PEER_OS_PREP_READINESS_CMD)


class PeerDeployTreeRefreshTests(unittest.TestCase):
    def test_apply_always_refreshes_even_when_tree_already_exists(self) -> None:
        self.assertTrue(
            should_refresh_peer_deploy_tree(
                join_mode="apply", ready_ok=True, missing=[]
            )
        )

    def test_check_does_not_copy_when_tree_is_already_present(self) -> None:
        self.assertFalse(
            should_refresh_peer_deploy_tree(
                join_mode="check", ready_ok=True, missing=[]
            )
        )

    def test_check_still_copies_when_the_tree_is_missing(self) -> None:
        self.assertTrue(
            should_refresh_peer_deploy_tree(
                join_mode="check",
                ready_ok=False,
                missing=["missing /opt/kin-mail-deploy/install/kin-mail.sh"],
            )
        )


class PrivilegedRemoteWrapTests(unittest.TestCase):
    def test_password_sudo_fallback_is_in_the_wrapper(self) -> None:
        from kin_privhelper.orchestration import wrap_privileged_remote

        wrapped = wrap_privileged_remote("mkdir -p /etc/kin-mail")
        self.assertIn("sudo -n", wrapped)
        self.assertIn("sudo -S", wrapped)
        self.assertIn("mkdir -p /etc/kin-mail", wrapped)
        self.assertNotIn("NOPASSWD", wrapped)
        self.assertIn("sudo -n bash -c", wrapped)
        self.assertIn("</dev/null", wrapped)
        self.assertRegex(wrapped, r"sudo -n bash -c '.*' </dev/null")
        self.assertNotRegex(wrapped, r"sudo -S[^;]*</dev/null")


class UpdateConfigKeysTests(unittest.TestCase):
    def test_sets_contracted_seats(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper.apply_config import parse_config, update_config_keys

        tmp = Path(tempfile.mkdtemp())
        conf = tmp / "config"
        conf.write_text(
            "MAIL_DOMAIN=example.test\nCONTRACTED_SEATS=PLACEHOLDER_UNSET\nKIN_ADMIN_IPS=\n",
            encoding="utf-8",
        )
        os.chmod(conf, 0o600)
        with patch("kin_privhelper.apply_config.CONF_FILE", conf), patch(
            "kin_privhelper.apply_config.CONF_DIR", tmp
        ):
            code, _lines = update_config_keys({"CONTRACTED_SEATS": "32"})
        self.assertEqual(code, 0)
        self.assertEqual(parse_config(conf.read_text(encoding="utf-8")).get("CONTRACTED_SEATS"), "32")


if __name__ == "__main__":
    unittest.main()
