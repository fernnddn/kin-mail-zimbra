"""Unit tests for HA orchestration inventory + RBAC (no live Ansible)."""

from __future__ import annotations

import unittest

from kin_privhelper.orchestration import (
    STEPS,
    OrchHost,
    redact_text,
    render_inventory,
    resolve_topology,
    valid_ipv4,
)
from kin_privhelper.rbac import ROLE_CUSTOMER_ADMIN, ROLE_SUPER_ADMIN, command_allowed


class InventoryTests(unittest.TestCase):
    def test_inventory_has_no_plaintext_password(self) -> None:
        inv = render_inventory(
            [OrchHost("mail.example.test", "192.0.2.15", "mail")],
            OrchHost("mon.example.test", "192.0.2.12", "mon"),
        )
        self.assertIn("lookup", inv)
        self.assertIn("KIN_ANSIBLE_PASSWORD", inv)
        self.assertNotIn("ansible_password: 'secret", inv)
        self.assertNotIn("s3cret", inv)
        self.assertIn("192.0.2.15", inv)
        self.assertIn("192.0.2.12", inv)

    def test_redact(self) -> None:
        self.assertEqual(redact_text("pass=hunter2 extra", ["hunter2"]), "pass=*** extra")

    def test_resolve_topology(self) -> None:
        local, peer, mon = resolve_topology(
            draft={
                "peer_host_ip": "192.0.2.14",
                "peer_host_name": "mail2.example.test",
                "observability_vm_ip": "192.0.2.12",
            },
            config={"MAIL_HOST": "mail.example.test", "SERVER_IP": "192.0.2.15"},
        )
        self.assertEqual(peer.ip, "192.0.2.14")
        self.assertEqual(mon.ip, "192.0.2.12")
        self.assertEqual(local.name, "mail.example.test")
        with self.assertRaises(ValueError):
            resolve_topology(
                draft={"peer_host_ip": "not-an-ip", "observability_vm_ip": "192.0.2.12"},
                config={"MAIL_HOST": "mail.example.test", "SERVER_IP": "192.0.2.15"},
            )

    def test_step_order(self) -> None:
        ids = [s.step_id for s in STEPS]
        self.assertEqual(
            ids[:6],
            [
                "peer_os_prep",
                "os_hardening",
                "mon_qnetd",
                "mail_qdevice",
                "mon_iscsi",
                "mail_fencing",
            ],
        )
        self.assertIn("mail_drbd_activate", ids)
        self.assertIn("mail_pacemaker_stack", ids)
        self.assertEqual(ids[-1], "live_join_check")

    def test_event_exit_code_zero_is_success(self) -> None:
        from kin_privhelper.orchestration import _event_exit_code

        self.assertEqual(_event_exit_code({"type": "done", "exit_code": 0}), 0)
        self.assertEqual(_event_exit_code({"type": "done", "exit_code": 2}), 2)
        self.assertEqual(_event_exit_code({"type": "done"}), 1)

    def test_ansible_cfg_uses_absolute_roles_path(self) -> None:
        from pathlib import Path

        from kin_privhelper.orchestration import render_ansible_cfg

        cfg = render_ansible_cfg(
            local_tmp=Path("/var/lib/example/.ansible/tmp"),
            roles_path=Path("/opt/kin-mail-console/ansible/roles"),
        )
        self.assertIn("roles_path = /opt/kin-mail-console/ansible/roles", cfg)
        self.assertIn("local_tmp = /var/lib/example/.ansible/tmp", cfg)


class OrchRbacTests(unittest.TestCase):
    def test_customer_cannot_orchestrate(self) -> None:
        from kin_privhelper.rbac import ROLE_SUPPORT_OPS

        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "run_ha_orchestration"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "run_ha_orchestration"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "run_ha_orchestration"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "store_provisioning_secrets"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "store_provisioning_secrets"))


class CheckModeSafetyTests(unittest.TestCase):
    def test_cluster_join_steps_are_flagged(self) -> None:
        join_ids = {s.step_id for s in STEPS if s.cluster_join}
        self.assertEqual(join_ids, {"mail_drbd_activate", "mail_pacemaker_stack"})

    def test_check_mode_skip_tags_keep_peer_off_live_sbd_and_pcs(self) -> None:
        by_id = {s.step_id: s for s in STEPS}
        self.assertEqual(
            set(by_id["mail_fencing"].skip_tags),
            {"login", "pcs", "verify"},
        )
        self.assertEqual(
            set(by_id["mail_qdevice"].skip_tags),
            {"configure", "service", "verify"},
        )
        self.assertTrue(by_id["os_hardening"].peer_only_on_join_check)
        self.assertTrue(by_id["mail_drbd_install"].peer_only_on_join_check)
        self.assertTrue(by_id["mon_qnetd"].check_on_join_check)
        self.assertTrue(by_id["mon_iscsi"].check_on_join_check)


if __name__ == "__main__":
    unittest.main()
