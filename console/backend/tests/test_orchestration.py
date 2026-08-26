"""Unit tests for HA orchestration inventory + RBAC (no live Ansible)."""

from __future__ import annotations

import unittest

from kin_privhelper.orchestration import (
    STEPS,
    OrchHost,
    _INSTALL_DEST_RE,
    kin_mail_deploy_dir,
    parse_peer_console_probe,
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
        self.assertIn("KIN_HACLUSTER_PASSWORD", inv)
        self.assertIn("KIN_CHAP_PASSWORD", inv)
        self.assertIn("iscsi_target_chap_userid: kin-sbd-chap", inv)
        self.assertIn("iscsi_initiator_chap_userid: kin-sbd-chap", inv)
        self.assertNotIn("ansible_password: 'secret", inv)
        self.assertNotIn("s3cret", inv)
        self.assertIn("192.0.2.15", inv)
        self.assertIn("192.0.2.12", inv)
        self.assertNotIn("drbd_resource_node_a_address", inv)

    def test_two_mail_hosts_emit_drbd_addresses(self) -> None:
        inv = render_inventory(
            [
                OrchHost("mail.example.test", "192.0.2.15", "mail"),
                OrchHost("mail2.example.test", "192.0.2.14", "mail2"),
            ],
            OrchHost("mon.example.test", "192.0.2.12", "mon"),
            vip_ip="192.0.2.16",
        )
        self.assertIn("drbd_resource_node_a_address: 192.0.2.15", inv)
        self.assertIn("drbd_resource_node_b_address: 192.0.2.14", inv)
        self.assertIn("pacemaker_mail_stack_vip_ip: 192.0.2.16", inv)
        self.assertIn("drbd_resource_disk: /dev/sdb1", inv)
        self.assertIn("drbd_resource_meta_disk: /dev/sdb2", inv)
        self.assertIn('pacemaker_agents_keep_meta_loop_unit: ""', inv)
        self.assertIn(
            "pacemaker_mail_stack_zimbra_service_hostname: mail.example.test", inv
        )
        self.assertIn("pacemaker_mail_stack_zimbra_service_shortname: mail", inv)
        self.assertIn("pacemaker_mail_stack_prefer_node: mail.example.test", inv)
        self.assertNotIn("gits-it", inv)
        self.assertNotIn("scsi-36001405", inv)
        self.assertNotIn("10.10.40.", inv)
        self.assertNotIn("pacemaker_mail_stack_vip_nic", inv)
        self.assertNotIn("ens33", inv)
        self.assertIn("ServerAliveInterval=30", inv)
        self.assertIn("ServerAliveCountMax=120", inv)

    def test_inventory_uses_discovered_drbd_disks(self) -> None:
        inv = render_inventory(
            [
                OrchHost("mail.example.test", "192.0.2.15", "mail"),
                OrchHost("mail2.example.test", "192.0.2.14", "mail2"),
            ],
            OrchHost("mon.example.test", "192.0.2.12", "mon"),
            vip_ip="192.0.2.16",
            data_disk="/dev/vdb1",
            meta_disk="/dev/vdb2",
        )
        self.assertIn("drbd_resource_disk: /dev/vdb1", inv)
        self.assertIn("drbd_resource_meta_disk: /dev/vdb2", inv)
        self.assertNotIn("drbd_resource_disk: /dev/sdb1", inv)

    def test_hostnames_compatible(self) -> None:
        from kin_privhelper.orchestration import hostnames_compatible

        self.assertTrue(hostnames_compatible("mail.example.test", "mail.example.test"))
        self.assertTrue(hostnames_compatible("mail.example.test", "mail"))
        self.assertTrue(hostnames_compatible("mail", "mail.example.test"))
        self.assertFalse(hostnames_compatible("mail.example.test", "ubuntu"))
        self.assertFalse(hostnames_compatible("mail2.example.test", "mail.example.test"))
        self.assertFalse(hostnames_compatible("mail.example.test", "mail.customer.test"))

    def test_optional_vip_nic_is_emitted_when_valid(self) -> None:
        inv = render_inventory(
            [
                OrchHost("mail.example.test", "192.0.2.15", "mail"),
                OrchHost("mail2.example.test", "192.0.2.14", "mail2"),
            ],
            OrchHost("mon.example.test", "192.0.2.12", "mon"),
            vip_ip="192.0.2.16",
            vip_nic="ens18",
        )
        self.assertIn("pacemaker_mail_stack_vip_nic: ens18", inv)
        self.assertNotIn("ens33", inv)

    def test_invalid_vip_nic_is_omitted(self) -> None:
        inv = render_inventory(
            [
                OrchHost("mail.example.test", "192.0.2.15", "mail"),
                OrchHost("mail2.example.test", "192.0.2.14", "mail2"),
            ],
            OrchHost("mon.example.test", "192.0.2.12", "mon"),
            vip_ip="192.0.2.16",
            vip_nic="ens18; rm -rf /",
        )
        self.assertNotIn("pacemaker_mail_stack_vip_nic", inv)

    def test_empty_vip_is_omitted_from_inventory(self) -> None:
        inv = render_inventory(
            [
                OrchHost("mail.example.test", "192.0.2.15", "mail"),
                OrchHost("mail2.example.test", "192.0.2.14", "mail2"),
            ],
            OrchHost("mon.example.test", "192.0.2.12", "mon"),
        )
        self.assertNotIn("pacemaker_mail_stack_vip_ip", inv)

    def test_redact(self) -> None:
        self.assertEqual(redact_text("pass=hunter2 extra", ["hunter2"]), "pass=*** extra")

    def test_resolve_topology(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"KIN_QNETD_INVENTORY_HOST": ""}, clear=False):
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
        self.assertEqual(mon.name, "obs-192-0-2-12")
        self.assertNotEqual(mon.name, "mon.gits-it.site")
        with self.assertRaises(ValueError):
            resolve_topology(
                draft={"peer_host_ip": "not-an-ip", "observability_vm_ip": "192.0.2.12"},
                config={"MAIL_HOST": "mail.example.test", "SERVER_IP": "192.0.2.15"},
            )

    def test_resolve_topology_uses_env_observability_name(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(
            os.environ, {"KIN_QNETD_INVENTORY_HOST": "mon.example.test"}, clear=False
        ):
            _, _, mon = resolve_topology(
                draft={
                    "peer_host_ip": "192.0.2.14",
                    "peer_host_name": "mail2.example.test",
                    "observability_vm_ip": "192.0.2.12",
                },
                config={"MAIL_HOST": "mail.example.test", "SERVER_IP": "192.0.2.15"},
            )
        self.assertEqual(mon.name, "mon.example.test")

    def test_monitoring_host_from_ssh_ident(self) -> None:
        from kin_privhelper.orchestration import monitoring_host_from_ssh_ident

        current = OrchHost("obs-192-0-2-12", "192.0.2.12", "obs")
        renamed = monitoring_host_from_ssh_ident(
            current, "mon.example.test\n", env_locked=False
        )
        self.assertEqual(renamed.name, "mon.example.test")
        locked = monitoring_host_from_ssh_ident(
            current, "mon.example.test\n", env_locked=True
        )
        self.assertEqual(locked.name, current.name)
        localhost = monitoring_host_from_ssh_ident(
            current, "localhost\n", env_locked=False
        )
        self.assertEqual(localhost.name, current.name)

    def test_resolve_cluster_vip_rejects_collisions(self) -> None:
        from kin_privhelper.orchestration import resolve_cluster_vip

        local, peer, mon = resolve_topology(
            draft={
                "peer_host_ip": "192.0.2.14",
                "peer_host_name": "mail2.example.test",
                "observability_vm_ip": "192.0.2.12",
                "cluster_vip_ip": "192.0.2.16",
            },
            config={"MAIL_HOST": "mail.example.test", "SERVER_IP": "192.0.2.15"},
        )
        self.assertEqual(
            resolve_cluster_vip(
                draft={"cluster_vip_ip": "192.0.2.16"},
                config={},
                local=local,
                peer=peer,
                monitoring=mon,
            ),
            "192.0.2.16",
        )
        with self.assertRaises(ValueError):
            resolve_cluster_vip(
                draft={"cluster_vip_ip": "192.0.2.15"},
                config={},
                local=local,
                peer=peer,
                monitoring=mon,
            )
        with self.assertRaises(ValueError):
            resolve_cluster_vip(
                draft={"cluster_vip_ip": "192.0.2.14"},
                config={},
                local=local,
                peer=peer,
                monitoring=mon,
            )

    def test_step_order(self) -> None:
        ids = [s.step_id for s in STEPS]
        self.assertEqual(
            ids[:7],
            [
                "peer_os_prep",
                "mail_zpush_snippets",
                "os_hardening",
                "mon_qnetd",
                "mail_cluster_setup",
                "mail_qdevice",
                "mon_iscsi",
            ],
        )
        self.assertIn("mail_drbd_activate", ids)
        self.assertIn("mail_pacemaker_stack", ids)
        self.assertEqual(ids[-1], "live_join_check")

    def test_frontend_ha_orch_stages_match_steps(self) -> None:
        import re
        from pathlib import Path

        ts = (
            Path(__file__).resolve().parents[3]
            / "console"
            / "frontend"
            / "src"
            / "wizard"
            / "deployPipeline.ts"
        )
        text = ts.read_text(encoding="utf-8")
        block = re.search(
            r"export const HA_ORCH_STAGES: readonly InstallStage\[\] = \[([\s\S]*?)\] as const;",
            text,
        )
        self.assertIsNotNone(block, "HA_ORCH_STAGES missing from deployPipeline.ts")
        pairs = re.findall(
            r'script:\s*"([^"]+)",\s*label:\s*"([^"]+)"',
            block.group(1),
        )
        expected = [(s.step_id, s.label) for s in STEPS]
        self.assertEqual(pairs, expected)

    def test_frontend_redeploy_requires_danger_confirm(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        step = (repo / "console/frontend/src/wizard/steps/DeployStep.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("redeployDanger", step)
        self.assertIn("countdownSeconds={10}", step)
        self.assertIn("This can destroy live mail data", step)
        self.assertIn("Mail is already installed on this host", step)
        session = (
            repo / "console/frontend/src/wizard/DeploySession.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("if (fullInstallComplete)", session)
        pipeline = (
            repo / "console/frontend/src/wizard/deployPipeline.ts"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(pipeline.count("/^Refusing:/m.test(log)"), 2)
        self.assertIn("Full install complete,\\s*with warnings", pipeline)
        self.assertIn("if (complete && !failed)", pipeline)
        cmds = (
            repo / "console/backend/kin_privhelper/commands.py"
        ).read_text(encoding="utf-8")
        self.assertIn("if is_full_install_complete():", cmds)
        self.assertIn("Do not run Deploy again", cmds)

    def test_event_exit_code_zero_is_success(self) -> None:
        from kin_privhelper.orchestration import _event_exit_code

        self.assertEqual(_event_exit_code({"type": "done", "exit_code": 0}), 0)
        self.assertEqual(_event_exit_code({"type": "done", "exit_code": 2}), 2)
        self.assertEqual(_event_exit_code({"type": "done"}), 1)

    def test_peer_console_probe_parses_markers_without_exposing_layout_bugs(self) -> None:
        blob = (
            "KIN_PEER_STATE_BEGIN\nHA=0\nSETUP=1\nCFG=1\nKIN_PEER_STATE_END\n"
            "KIN_PEER_HA_BEGIN\nKIN_PEER_HA_END\n"
            'KIN_PEER_CFG_BEGIN\nTOPOLOGY="1vm"\nMAIL_HOST="mail2.example.test"\n'
            "KIN_PEER_CFG_END\n"
        )
        parsed = parse_peer_console_probe(blob)
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["ha_present"])
        self.assertTrue(parsed["setup_present"])
        self.assertTrue(parsed["cfg_present"])
        self.assertIn('TOPOLOGY="1vm"', parsed["config_text"])
        self.assertFalse(parsed["topology_present"])
        self.assertEqual(parsed["topology_text"].strip(), "")

    def test_peer_console_probe_parses_topology_marker(self) -> None:
        blob = (
            "KIN_PEER_STATE_BEGIN\nHA=1\nSETUP=1\nCFG=1\nTOPO=1\nKIN_PEER_STATE_END\n"
            "KIN_PEER_HA_BEGIN\ncomplete 2026-08-21T00:00:00Z\nKIN_PEER_HA_END\n"
            "KIN_PEER_TOPO_BEGIN\n2vm\nKIN_PEER_TOPO_END\n"
            'KIN_PEER_CFG_BEGIN\nTOPOLOGY="2vm"\nKIN_PEER_CFG_END\n'
        )
        parsed = parse_peer_console_probe(blob)
        self.assertTrue(parsed["ok"])
        self.assertTrue(parsed["topology_present"])
        self.assertEqual(parsed["topology_text"].strip(), "2vm")

    def test_install_dest_allows_topology_marker(self) -> None:
        self.assertTrue(_INSTALL_DEST_RE.match("/etc/kin-mail/topology"))
        self.assertTrue(_INSTALL_DEST_RE.match("/etc/kin-mail/config"))
        self.assertTrue(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/users.json"))
        self.assertFalse(_INSTALL_DEST_RE.match("/etc/kin-mail/config.bak"))
        self.assertFalse(_INSTALL_DEST_RE.match("/tmp/topology"))
        self.assertFalse(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/users.json.bak"))

    def test_install_dest_allows_server_id_and_license_token_only(self) -> None:
        # Added so HA-finish can sync one cluster identity + license state to
        # the peer (re-audit, 25 Aug 2026) - exact paths only, not siblings.
        self.assertTrue(_INSTALL_DEST_RE.match("/etc/kin-mail/server-id"))
        self.assertTrue(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/license.token"))
        self.assertFalse(_INSTALL_DEST_RE.match("/etc/kin-mail/server-id.bak"))
        self.assertFalse(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/license.token.bak"))
        self.assertFalse(_INSTALL_DEST_RE.match("/etc/kin-mail/server-id-old"))
        self.assertFalse(_INSTALL_DEST_RE.match("/etc/passwd"))
        self.assertFalse(_INSTALL_DEST_RE.match("/root/.ssh/authorized_keys"))

    def test_install_dest_allows_session_secret(self) -> None:
        # Added so VIP failover does not force a fresh console login (live
        # 2vm practice run, 25 Aug 2026 raised this as a known-but-unfixed
        # gap; closed the same day while waiting on the DRBD initial sync).
        self.assertTrue(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/session.secret"))
        self.assertFalse(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/session.secret.bak"))
        self.assertFalse(_INSTALL_DEST_RE.match("/var/lib/kin-mail-console/session-secret"))

    def test_orch_done_is_after_peer_console_sync(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "orchestration.py"
        ).read_text(encoding="utf-8")
        sync_idx = text.find("sync_peer_ha_console_state(")
        done_idx = text.find('yield emit_line(f"ORCH_DONE join_mode={join_mode}")')
        fail_idx = text.find("step=peer_console_state")
        self.assertGreater(sync_idx, 0)
        self.assertGreater(done_idx, 0)
        self.assertLess(sync_idx, done_idx)
        self.assertGreater(fail_idx, 0)
        self.assertLess(fail_idx, done_idx)
        marker_idx = text.find("ha-setup-complete marker written on this node")
        self.assertGreater(marker_idx, fail_idx)
        self.assertGreater(marker_idx, sync_idx)

    def test_admin_pass_is_added_to_ha_stream_redaction_before_ansible_runs(self) -> None:
        # HA stream redaction used to cover only root/kin/hacluster/chap - the
        # peer installer's own stdout (echoed through this stream) can also
        # contain the Zimbra ADMIN_PASS it was handed (platform bug audit,
        # 25 Aug 2026).
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "orchestration.py"
        ).read_text(encoding="utf-8")
        base_idx = text.find("secrets = [root_pass, kin_pass, hacluster_pass, chap_pass]")
        admin_idx = text.find("secrets.append(admin_pass)")
        ansible_idx = text.find('yield emit_line(f"RUN {\' \'.join(argv)}")')
        self.assertGreater(base_idx, 0)
        self.assertGreater(admin_idx, 0)
        self.assertGreater(ansible_idx, 0)
        self.assertLess(base_idx, admin_idx)
        self.assertLess(admin_idx, ansible_idx)

    def test_disk_preflight_refuse_stamps_orch_failed(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "orchestration.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ORCH_FAILED step=disk_preflight", text)

    def test_ansible_cfg_uses_absolute_roles_path(self) -> None:
        from pathlib import Path

        from kin_privhelper.orchestration import render_ansible_cfg

        cfg = render_ansible_cfg(
            local_tmp=Path("/var/lib/example/.ansible/tmp"),
            roles_path=Path("/opt/kin-mail-console/ansible/roles"),
        )
        self.assertIn("roles_path = /opt/kin-mail-console/ansible/roles", cfg)
        self.assertIn("local_tmp = /var/lib/example/.ansible/tmp", cfg)
        self.assertIn("timeout = 180", cfg)

    def test_inventory_sets_kin_mail_deploy_dir(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"KIN_MAIL_DEPLOY_DIR": "/opt/kin-mail-deploy"}):
            inv = render_inventory(
                [OrchHost("mail.example.test", "192.0.2.15", "mail")],
                OrchHost("mon.example.test", "192.0.2.12", "mon"),
            )
        self.assertIn('kin_mail_deploy_dir: "/opt/kin-mail-deploy"', inv)

    def test_kin_mail_deploy_dir_follows_env(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"KIN_MAIL_DEPLOY_DIR": "/opt/custom-deploy"}, clear=False):
            self.assertEqual(kin_mail_deploy_dir(), "/opt/custom-deploy")
        with patch.dict(os.environ, {"KIN_MAIL_DEPLOY_DIR": "  "}, clear=False):
            self.assertEqual(kin_mail_deploy_dir(), "/opt/kin-mail-deploy")

    def test_ansible_roles_do_not_walk_role_path_into_install(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "ansible"
        hits: list[str] = []
        for path in root.rglob("*"):
            if path.suffix not in {".yml", ".yaml", ".j2", ".py"}:
                continue
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if "/../../../install/" in text:
                hits.append(str(path.relative_to(root)))
        self.assertEqual(hits, [])

    def test_sbd_role_defaults_do_not_ship_a_lab_by_id(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "ansible" / "roles"
        initiator = (root / "iscsi_initiator" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        sbd = (root / "sbd_stonith" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("scsi-36001405", initiator)
        self.assertNotIn("scsi-36001405", sbd)
        self.assertIn('iscsi_initiator_sbd_by_id: ""', initiator)
        self.assertIn("iscsi_initiator_sbd_by_id", sbd)


class OrchRbacTests(unittest.TestCase):
    def test_customer_cannot_orchestrate(self) -> None:
        from kin_privhelper.rbac import ROLE_SUPPORT_OPS

        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "run_ha_orchestration"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "run_ha_orchestration"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "run_ha_orchestration"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "ha_disk_preflight"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "ha_disk_preflight"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "ha_disk_preflight"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "store_provisioning_secrets"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "store_provisioning_secrets"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "run_hardening"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "run_hardening"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "run_hardening"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "remove_host"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "remove_host"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "remove_host"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "remove_observability"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "remove_observability"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "remove_observability"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "add_observability"))
        self.assertTrue(command_allowed(ROLE_SUPPORT_OPS, "add_observability"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "add_observability"))
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "apply_appliance_settings"))
        self.assertFalse(command_allowed(ROLE_SUPPORT_OPS, "apply_appliance_settings"))
        self.assertTrue(command_allowed(ROLE_SUPER_ADMIN, "apply_appliance_settings"))
        self.assertFalse(
            command_allowed(
                ROLE_CUSTOMER_ADMIN,
                "mutate_console_users",
                args={"op": "create"},
                username="alice",
            )
        )
        self.assertFalse(
            command_allowed(
                ROLE_SUPPORT_OPS,
                "mutate_console_users",
                args={"op": "delete", "username": "bob"},
                username="ops",
            )
        )
        self.assertTrue(
            command_allowed(
                ROLE_SUPER_ADMIN,
                "mutate_console_users",
                args={"op": "create"},
                username="admin",
            )
        )
        self.assertTrue(
            command_allowed(
                ROLE_CUSTOMER_ADMIN,
                "mutate_console_users",
                args={"op": "set_password", "username": "alice"},
                username="alice",
            )
        )
        self.assertFalse(
            command_allowed(
                ROLE_CUSTOMER_ADMIN,
                "mutate_console_users",
                args={"op": "set_password", "username": "admin"},
                username="alice",
            )
        )
        # Status is read-only; apply is the sensitive mutation.
        self.assertTrue(
            command_allowed(ROLE_CUSTOMER_ADMIN, "run_script:09-hardening.sh --status")
        )


class CheckModeSafetyTests(unittest.TestCase):
    def test_cluster_join_steps_are_flagged(self) -> None:
        join_ids = {s.step_id for s in STEPS if s.cluster_join}
        self.assertEqual(
            join_ids, {"mail_cluster_setup", "mail_drbd_activate", "mail_pacemaker_stack"}
        )

    def test_check_mode_skip_tags_keep_peer_off_live_sbd_and_pcs(self) -> None:
        by_id = {s.step_id: s for s in STEPS}
        self.assertEqual(
            set(by_id["mail_fencing"].skip_tags),
            {"login", "config", "activate", "pcs", "verify"},
        )
        self.assertEqual(
            set(by_id["mail_qdevice"].skip_tags),
            {"configure", "service", "verify"},
        )
        self.assertTrue(by_id["os_hardening"].peer_only_on_join_check)
        self.assertTrue(by_id["mail_zpush_snippets"].peer_only_on_join_check)
        self.assertEqual(by_id["mail_zpush_snippets"].tags, ("snippets",))
        self.assertEqual(by_id["mail_zpush_snippets"].playbook, "playbooks/mail-zpush.yml")
        self.assertTrue(by_id["mail_drbd_install"].peer_only_on_join_check)
        self.assertEqual(by_id["mail_drbd_install"].tags, ("install",))
        self.assertTrue(by_id["mail_pacemaker_agents"].peer_only_on_join_check)
        self.assertEqual(by_id["mail_pacemaker_agents"].tags, ("agents",))
        self.assertTrue(by_id["mon_qnetd"].check_on_join_check)
        self.assertTrue(by_id["mon_iscsi"].check_on_join_check)

    def test_live_join_check_skips_cluster_setup_on_a_single_live_member(self) -> None:
        from kin_privhelper.orchestration import live_join_check_playbooks

        one = [OrchHost("mail.example.test", "192.0.2.15", "mail")]
        plays = live_join_check_playbooks(one, meta_disk="/dev/sdb2")
        rels = [rel for rel, _ in plays]
        self.assertNotIn("playbooks/mail-cluster-setup.yml", rels)
        self.assertEqual(
            plays,
            [
                (
                    "playbooks/mail-drbd.yml",
                    [
                        "--skip-tags",
                        "install,verify,disk_prep",
                        "-e",
                        "drbd_resource_meta_disk=/dev/sdb2",
                    ],
                ),
                (
                    "playbooks/mail-pacemaker.yml",
                    ["--skip-tags", "agents,verify,ldap,memcached"],
                ),
            ],
        )

    def test_live_join_check_runs_cluster_setup_when_two_live_members(self) -> None:
        from kin_privhelper.orchestration import live_join_check_playbooks

        pair = [
            OrchHost("mail.example.test", "192.0.2.15", "mail"),
            OrchHost("mail2.example.test", "192.0.2.14", "mail2"),
        ]
        plays = live_join_check_playbooks(pair)
        self.assertEqual(plays[0][0], "playbooks/mail-cluster-setup.yml")
        self.assertEqual(plays[0][1], ["--skip-tags", "auth,pcs,properties"])
        self.assertEqual(plays[1][1][1], "install,verify,disk_prep")
        self.assertEqual(plays[2][1], ["--skip-tags", "agents,verify,ldap,memcached"])

    def test_disk_prep_cleanup_runs_under_check_mode(self) -> None:
        from pathlib import Path

        tasks = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "drbd_disk_prep"
            / "tasks"
            / "main.yml"
        )
        text = tasks.read_text(encoding="utf-8")
        idx = text.find("Remove the staged DRBD disk selector")
        self.assertGreater(idx, 0)
        cleanup = text[idx : idx + 400]
        self.assertIn("check_mode: false", cleanup)

    def test_jammy_drbd_modinfo_matches_install_verify_gates(self) -> None:
        # Ubuntu 22.04 in-tree module (captured from jammy 5.15 kernels).
        filename = (
            "/lib/modules/5.15.0-117-generic/kernel/drivers/block/drbd/drbd.ko"
        )
        version = "8.4.11"
        self.assertIn("/kernel/drivers/block/drbd/", filename)
        self.assertNotIn("dkms", filename.lower())
        self.assertRegex(version, r"^8\.4\.")
        dkms = "/lib/modules/5.15.0-117-generic/updates/dkms/drbd.ko"
        self.assertNotIn("/kernel/drivers/block/drbd/", dkms)

    def test_pacemaker_agents_ocf_meta_contains_resource_agent(self) -> None:
        from pathlib import Path

        ocf = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "pacemaker_agents"
            / "files"
            / "zimbra"
        )
        text = ocf.read_text(encoding="utf-8")
        self.assertIn("<resource-agent", text)
        start = text.find("zimbra_start()")
        validate = text.find("zimbra_validate()")
        stop = text.find("zimbra_stop()")
        self.assertGreater(start, validate)
        validate_block = text[validate:start]
        self.assertIn("findmnt -n", validate_block)
        self.assertIn("/dev/drbd*", validate_block)
        start_block = text[start : start + 900]
        self.assertIn("findmnt -n", start_block)
        self.assertIn("/dev/drbd*", start_block)
        self.assertIn("bin/zmcontrol", start_block)
        self.assertIn("zimbra_wait_backing_free", text)
        self.assertIn("zimbra_fail2ban_jails unmounted", text[stop : stop + 800])
        self.assertIn("fuser -m", text)
        paths = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "kin_mail_paths"
            / "tasks"
            / "main.yml"
        )
        helper = paths.read_text(encoding="utf-8")
        self.assertIn("delegate_to: localhost", helper)
        self.assertNotIn("{{ role_path }}", helper)

    def test_pacemaker_stack_waits_for_started_and_omits_lab_nic(self) -> None:
        from pathlib import Path

        root = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "pacemaker_mail_stack"
        )
        defaults = (root / "defaults" / "main.yml").read_text(encoding="utf-8")
        self.assertIn("pacemaker_mail_stack_vip_nic: \"\"", defaults)
        self.assertNotIn("pacemaker_mail_stack_vip_nic: ens33", defaults)
        self.assertIn("pacemaker_mail_stack_prefer_node: \"\"", defaults)
        self.assertIn("pacemaker_mail_stack_prefer_enabled: false", defaults)

        mail_svc = (root / "tasks" / "mail_svc.yml").read_text(encoding="utf-8")
        self.assertNotIn("nic=ens33", mail_svc)
        self.assertIn("nic=' ~ pacemaker_mail_stack_vip_nic", mail_svc)
        self.assertIn("pcs resource update", mail_svc)
        self.assertIn("Refuse VIP attr update while kin-vip is Started", mail_svc)
        self.assertIn("corosync_qdevice_qnetd_ip", mail_svc)
        self.assertIn("prefer_score", mail_svc)
        self.assertIn("op monitor interval=10s", mail_svc)
        self.assertIn("--delete=nic", mail_svc)

        constraints = (root / "tasks" / "constraints.yml").read_text(encoding="utf-8")
        enable = constraints.find("Enable the kin-mail-svc primitives after constraints")
        wait = constraints.find("Wait until kin-fs, kin-vip, and kin-zimbra are Started")
        self.assertGreater(enable, 0)
        self.assertGreater(wait, enable)
        enable_block = constraints[enable:wait]
        self.assertIn("pcs resource enable", enable_block)
        self.assertIn("target-role(=|:)", enable_block)
        self.assertIn("Stopped", enable_block)
        self.assertNotIn("[[:space:]]", enable_block)
        self.assertIn("pacemaker_mail_stack_fs", enable_block)
        self.assertIn("pacemaker_mail_stack_zimbra", enable_block)
        self.assertIn("pacemaker_mail_stack_vip", enable_block)
        wait_block = constraints[wait : wait + 900]
        self.assertIn("retries: 90", wait_block)
        self.assertNotIn("failed_when:", wait_block)

        verify = (root / "tasks" / "verify.yml").read_text(encoding="utf-8")
        self.assertIn("kin-vip[^\\n]*Started", verify)
        self.assertIn("'Promoted' not in pacemaker_mail_stack_status.stdout", verify)
        self.assertIn("promoted-resource-stickiness=", verify)
        self.assertIn("Verify kin-vip and kin-zimbra Started on the Promoted node", verify)
        self.assertIn("Verify kin-vip CIB matches inventory IP", verify)

        main = (root / "tasks" / "main.yml").read_text(encoding="utf-8")
        constraints_idx = main.find("import_tasks: constraints.yml")
        ldap_idx = main.find("import_tasks: ldap.yml")
        memcached_idx = main.find("import_tasks: memcached.yml")
        verify_idx = main.find("import_tasks: verify.yml")
        self.assertGreater(constraints_idx, 0)
        self.assertGreater(ldap_idx, constraints_idx)
        self.assertGreater(memcached_idx, ldap_idx)
        self.assertGreater(verify_idx, memcached_idx)

    def test_greenfield_iscsi_pcsd_and_dns_healthcheck_gates(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        initiator = (
            repo / "ansible/roles/iscsi_initiator/tasks/initiatorname.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Restart iscsid so login uses the inventory InitiatorName", initiator)
        auth = (repo / "ansible/roles/cluster_setup/tasks/auth.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Wait until pcsd is listening before host auth", auth)
        health = (repo / "install/05-healthcheck.sh").read_text(encoding="utf-8")
        self.assertIn('b "MX not published yet"', health)
        self.assertNotIn('f "MX not published yet"', health)
        self.assertIn('b "SPF not published yet"', health)
        self.assertIn('b "DMARC not published yet"', health)
        self.assertIn('b "Certificate is still self-signed', health)
        self.assertNotIn('f "No trusted certificate yet', health)
        self.assertIn('TLS_METHOD:-}" = "customer"', health)
        self.assertIn("No Let's Encrypt deploy hook (expected when TLS_METHOD=customer)", health)
        driver = (repo / "install/kin-mail.sh").read_text(encoding="utf-8")
        self.assertIn("Dead-man stays armed", driver)
        self.assertNotIn("elapsed without cancel", driver)
        tls = (repo / "install/04-tls-dkim.sh").read_text(encoding="utf-8")
        self.assertIn("wizard does not store it", tls)
        self.assertIn("kin_ha_peer_install", tls)
        self.assertIn("not creating a DKIM key", tls)
        health = (repo / "install/05-healthcheck.sh").read_text(encoding="utf-8")
        self.assertIn("healthcheck_host_is_not_published_mx", health)
        self.assertIn('b "DKIM key not created on this host', health)
        self.assertIn("HA peer key cannot match DNS", health)
        self.assertIn('f "DKIM key not created yet"', health)
        orch = (repo / "console/backend/kin_privhelper/orchestration.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("KIN_HA_PEER_INSTALL=1", orch)
        self.assertIn("mail-zpush.yml", orch)
        self.assertIn("tags=snippets", orch)
        self.assertIn("select_drbd_disk.py", orch)
        zpush_play = (repo / "ansible/playbooks/mail-zpush.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tasks_from: snippets.yml", zpush_play)
        agents_sys = (
            repo / "ansible/roles/pacemaker_agents/tasks/systemd.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Disable the Zimbra boot unit", agents_sys)
        health = (repo / "install/05-healthcheck.sh").read_text(encoding="utf-8")
        self.assertIn("T2 skipped on HA peer", health)
        self.assertIn('b "Cannot determine sending address', health)
        self.assertNotIn('f "Cannot determine sending address', health)
        self.assertIn('b "NOT consistent:', health)
        self.assertNotIn('f "NOT consistent:', health)
        self.assertIn("-10 minutes", health)
        prepare = (repo / "install/02-prepare-os.sh").read_text(encoding="utf-8")
        self.assertIn("useradd -m -s /bin/bash -G sudo", prepare)
        # sshd config toggling itself lives in the sourced library (also used
        # standalone by HA orchestration to re-enable password SSH on a 1vm
        # host that already reverted it - see the KIN_CHAP_PASSWORD test
        # below for the analogous CHAP-generation pinning).
        self.assertIn("lib/ssh-password-toggle.sh", prepare)
        toggle = (repo / "install/lib/ssh-password-toggle.sh").read_text(encoding="utf-8")
        self.assertIn("00-kin-mail.conf", toggle)
        self.assertIn("PasswordAuthentication yes", toggle)
        self.assertIn("PermitRootLogin yes", toggle)
        self.assertIn("PasswordAuthentication[[:space:]]+no", toggle)
        self.assertIn("enable_ssh_password", toggle)
        self.assertIn("disable_ssh_password", toggle)
        orch = (repo / "console/backend/kin_privhelper/orchestration.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("choose_shared_ssh_user", orch)
        self.assertIn("_push_peer_deploy_tree", orch)
        self.assertIn("should_refresh_peer_deploy_tree", orch)
        # Every host in the rendered inventory, including this one, connects
        # over ansible_password - a 1vm host that already ran the post-install
        # SSH revert must have it turned back on before any SSH attempt here,
        # not after.
        self.assertIn("--ensure-ssh-password", orch)
        ensure_idx = orch.index("--ensure-ssh-password")
        # The ansible_env dict entry (the actual runtime value passed to the
        # ansible-playbook subprocess), not render_inventory's template
        # string - both contain the substring "KIN_ANSIBLE_PASSWORD" but
        # only this one marks when the real connection attempt happens.
        env_assignment = '"KIN_ANSIBLE_PASSWORD": ssh_pass,'
        self.assertIn(env_assignment, orch)
        first_ssh_attempt_idx = orch.index(env_assignment)
        self.assertLess(
            ensure_idx,
            first_ssh_attempt_idx,
            "--ensure-ssh-password must run before the first SSH/ansible connection attempt",
        )
        self.assertIn(
            "secrets, extract, timeout=60, stdin_text=password",
            orch,
        )
        tls = (
            repo / "ansible/roles/corosync_qdevice/tasks/tls.yml"
        ).read_text(encoding="utf-8")
        minus_m = tls.find("corosync-qdevice-net-certutil -m")
        self.assertGreater(minus_m, 0)
        self.assertIn(
            "inventory_hostname != ansible_play_hosts[0]",
            tls[minus_m : minus_m + 280],
        )
        self.assertNotIn(
            'creates: "{{ corosync_qdevice_tls_ready_marker }}"',
            tls[minus_m : minus_m + 500],
        )
        record = tls.find("Record that this secondary imported the cluster PKCS12")
        self.assertGreater(record, minus_m)
        self.assertIn("corosync_qdevice_m.rc", tls[record : record + 500])
        self.assertIn("corosync_qdevice_tls_ready_marker", tls)
        self.assertIn("corosync_qdevice_tls_marker", tls)
        activate = (
            repo / "ansible/roles/drbd_resource/tasks/activate.yml"
        ).read_text(encoding="utf-8")
        force = activate.find("cmd: drbdadm primary --force")
        self.assertGreater(force, 0)
        force_when = activate[force : force + 900]
        self.assertIn("not (drbd_resource_pacemaker_owns | bool)", force_when)
        self.assertIn("'Primary' not in", force_when)
        self.assertNotIn("not (drbd_resource_is_diskless | bool)", force_when)
        self.assertIn("drbd_resource_pcs_clone", activate)
        self.assertIn("drbd_resource_role_pre", activate)
        needs = activate.find("drbd_resource_needs_activate:")
        self.assertGreater(needs, 0)
        needs_block = activate[needs : needs + 700]
        self.assertIn("drbd_resource_node_a_name", needs_block)
        self.assertIn("'Primary' not in", needs_block)
        qdev = (
            repo / "ansible/roles/corosync_qdevice/tasks/configure.yml"
        ).read_text(encoding="utf-8")
        flag = qdev.find("Flag whether quorum.device still needs wiring")
        self.assertGreater(flag, 0)
        flag_chunk = qdev[flag : qdev.find("Ensure helper directory", flag)]
        self.assertNotIn("run_once: true", flag_chunk)
        self.assertIn("ansible_play_hosts", qdev)
        hosts_yml = (
            repo / "ansible/roles/pacemaker_mail_stack/tasks/hosts.yml"
        ).read_text(encoding="utf-8")
        getent = hosts_yml.find("Verify the Zimbra service hostname resolves to loopback")
        self.assertGreater(getent, 0)
        self.assertIn("when: not ansible_check_mode", hosts_yml[getent : getent + 700])
        res_file = (
            repo / "ansible/roles/drbd_resource/tasks/resource_file.yml"
        ).read_text(encoding="utf-8")
        ext_meta = res_file.find(
            "Check the kin-zimbra resource definition uses external meta"
        )
        self.assertGreater(ext_meta, 0)
        self.assertIn(
            "when: not ansible_check_mode",
            res_file[ext_meta : ext_meta + 700],
        )
        ldap_yml = (
            repo / "ansible/roles/pacemaker_mail_stack/tasks/ldap.yml"
        ).read_text(encoding="utf-8")
        ldap_assert = ldap_yml.find("Verify LDAP points to 127.0.0.1")
        self.assertGreater(ldap_assert, 0)
        self.assertIn(
            "not ansible_check_mode",
            ldap_yml[ldap_assert : ldap_assert + 1600],
        )
        confirm = qdev.find("Confirm the quorum device configuration matches")
        self.assertGreater(confirm, 0)
        self.assertIn(
            "when: not ansible_check_mode",
            qdev[confirm : confirm + 700],
        )
        sbd_cfg = (
            repo / "ansible/roles/sbd_stonith/tasks/config.yml"
        ).read_text(encoding="utf-8")
        delay = sbd_cfg.find("Check SBD_DELAY_START is enabled")
        self.assertGreater(delay, 0)
        self.assertIn(
            "when: not ansible_check_mode",
            sbd_cfg[delay : delay + 500],
        )
        detect = (
            repo / "ansible/roles/cluster_setup/tasks/detect.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("cluster_setup_any_running", detect)
        self.assertIn("cluster_setup_nodes_blob", detect)
        form = (
            repo / "ansible/roles/cluster_setup/tasks/form.yml"
        ).read_text(encoding="utf-8")
        setup = form.find("Form the Corosync cluster")
        self.assertGreater(setup, 0)
        self.assertIn("cluster_setup_any_running", form[setup : setup + 900])
        self.assertIn("cluster_setup_any_configured", form[setup : setup + 900])
        auth_yml = (
            repo / "ansible/roles/cluster_setup/tasks/auth.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("cluster_setup_any_running", auth_yml)
        self.assertNotIn("not (cluster_setup_running | bool)", auth_yml)
        addrs = (
            repo / "ansible/roles/pacemaker_mail_stack/tasks/corosync_addrs.yml"
        ).read_text(encoding="utf-8")
        reload = addrs.find("Reload Corosync after ring0_addr")
        self.assertGreater(reload, 0)
        self.assertNotIn(
            "run_once: true",
            addrs[reload : addrs.find("Assert Corosync ring0_addr")],
        )
        ring0 = addrs.find("Assert Corosync ring0_addr")
        self.assertGreater(ring0, 0)
        self.assertIn(
            "when: not ansible_check_mode",
            addrs[ring0 : ring0 + 500],
        )
        for play in (
            "mail-drbd.yml",
            "mail-cluster-setup.yml",
            "mail-pacemaker.yml",
            "mail-fencing.yml",
            "mail-qdevice.yml",
        ):
            text = (repo / "ansible/playbooks" / play).read_text(encoding="utf-8")
            self.assertIn("any_errors_fatal: true", text, play)
        self.assertIn("getent passwd kin-console", orch)
        self.assertIn(
            "useradd --system --home /var/lib/kin-mail-console",
            orch,
        )
        self.assertIn("corosync_qdevice_p12_now", tls)
        self.assertIn("select_drbd_disk.py missing", orch)
        pipeline = (
            repo / "console/frontend/src/wizard/deployPipeline.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("/HA orchestration/i.test(log)", pipeline)
        self.assertNotIn("HA orchestration Slice 2", pipeline)
        deploy_step = (
            repo / "console/frontend/src/wizard/steps/DeployStep.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("haDisk.data_disk", deploy_step)
        self.assertNotIn('{"/dev/sdb1"}', deploy_step)
        app = (repo / "console/backend/kin_console/app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"data_disk": parsed.get("data_disk")', app)
        sbd_order = (
            repo / "ansible/roles/sbd_stonith/templates/kin-ordering.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("Requires=kin-softdog.service open-iscsi.service", sbd_order)
        self.assertNotIn("Wants=open-iscsi.service", sbd_order)
        luks_dropin = (
            repo
            / "ansible/roles/pacemaker_agents/templates/kin-luks-cryptsetup.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("Requires={{ pacemaker_agents_luks_cryptsetup_unit }}", luks_dropin)
        driver = (repo / "install/kin-mail.sh").read_text(encoding="utf-8")
        self.assertIn("07-zpush.sh failed - mail install continues", driver)
        self.assertNotIn('fail "Pipeline stopped at 07-zpush.sh"', driver)
        self.assertIn("11-admin-path-lockdown.sh failed - mail install continues", driver)
        self.assertNotIn('fail "Pipeline stopped at 11-admin-path-lockdown.sh"', driver)
        self.assertIn("09-hardening.sh failed - mail install continues", driver)
        self.assertNotIn('fail "Pipeline stopped at 09-hardening.sh"', driver)
        self.assertIn(
            'soft_failed_stages+=("02-prepare-os.sh --revert-ssh-password")',
            driver,
        )
        self.assertIn("Full install complete, with warnings", driver)
        self.assertIn("ServerAliveInterval=30", orch)
        self.assertIn("ServerAliveCountMax=120", orch)
        health = (repo / "install/05-healthcheck.sh").read_text(encoding="utf-8")
        self.assertIn('b "Message not found in message store yet', health)
        self.assertNotIn('f "Message not found in message store"', health)
        self.assertIn('b "Zimbra still warming after DNS-cache flush restarts', health)
        self.assertNotIn('f "Zimbra not healthy after DNS-cache flush restarts"', health)
        self.assertIn('b "AD LDAP auth not passing yet', health)
        self.assertNotIn('f "AD LDAP auth failed', health)
        preflight = (repo / "install/01-preflight.sh").read_text(encoding="utf-8")
        self.assertIn("TLS_METHOD=customer", preflight)
        self.assertIn("stopping it (Zimbra ships its own)", preflight)
        self.assertNotIn("is running - Zimbra ships its own and will conflict", preflight)
        hybrid = (repo / "install/06-hybrid-auth.sh").read_text(encoding="utf-8")
        self.assertIn("AD path not verified. Local mail still works", hybrid)
        self.assertNotIn("auth path(s) failed", hybrid)
        topo = (repo / "console/frontend/src/wizard/steps/TopologyStep.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("Mail B does not need console/bootstrap.sh", topo)
        self.assertIn("copies", topo)
        checks = (repo / ".github/workflows/checks.yml").read_text(encoding="utf-8")
        self.assertIn("--exclude='test_*.py'", checks)
        types = (repo / "console/frontend/src/wizard/types.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn("zpush_enabled: false", types)


class TranscriptRedactTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcript_file_does_not_contain_secret(self) -> None:
        """Force a secret through stdout, stderr, and argv; the on-disk log must not keep it."""
        import tempfile
        from pathlib import Path

        from kin_privhelper.commands import _stream_subprocess

        secret = "kin-test-vault-pass-NOTREAL-9f3a"
        marker = "KIN_ORCH_REDACT_PROBE"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "deploy-last.log"
            argv = [
                "python3",
                "-c",
                (
                    "import sys;"
                    f"sys.stdout.write({marker!r}+':'+{secret!r}+'\\n');"
                    f"sys.stderr.write('err-'+{secret!r}+'\\n')"
                ),
            ]
            events: list[dict] = []
            async for ev in _stream_subprocess(
                argv,
                transcript=path,
                secrets=[secret],
            ):
                events.append(ev)
            body = path.read_text(encoding="utf-8")
            self.assertTrue(path.is_file())
            self.assertNotIn(secret, body)
            self.assertIn("***", body)
            self.assertIn(marker, body)
            joined = "".join(str(e.get("data") or "") for e in events)
            self.assertNotIn(secret, joined)
            self.assertTrue(any(e.get("type") == "done" and e.get("exit_code") == 0 for e in events))

    async def test_stdin_is_fed_to_child(self) -> None:
        from kin_privhelper.commands import _stream_subprocess

        events: list[dict] = []
        async for ev in _stream_subprocess(
            ["python3", "-c", "import sys; print(sys.stdin.read().strip())"],
            stdin_text="hello-sudo",
        ):
            events.append(ev)
        joined = "".join(str(e.get("data") or "") for e in events)
        self.assertIn("hello-sudo", joined)
        self.assertTrue(any(e.get("type") == "done" and e.get("exit_code") == 0 for e in events))

    async def test_follow_log_includes_sidecar_detail_live(self) -> None:
        """Mimic 03: parent stdout stays quiet while zmsetup writes a sidecar log."""
        import asyncio
        import tempfile
        from pathlib import Path

        from kin_privhelper.commands import _stream_subprocess, _watch_log_file

        secret = "ZZSECRET_7c2e_TEST"
        with tempfile.TemporaryDirectory() as td:
            sidecar = Path(td) / "zimbra-install.log"
            sidecar.write_text("OLD RUN MUST NOT APPEAR\n", encoding="utf-8")
            queue: asyncio.Queue = asyncio.Queue()
            stop = asyncio.Event()
            task = asyncio.create_task(
                _watch_log_file(sidecar, queue, stop, poll_s=0.05)
            )
            await asyncio.sleep(0.2)
            sidecar.write_text("", encoding="utf-8")
            with sidecar.open("a", encoding="utf-8") as fh:
                fh.write(f"Configuring SMTP port {secret}\n")
                fh.flush()
            await asyncio.sleep(0.08)
            for name in ("IMAP", "POP", "web"):
                with sidecar.open("a", encoding="utf-8") as fh:
                    fh.write(f"Configuring {name} port {secret}\n")
                    fh.flush()
                await asyncio.sleep(0.08)
            stop.set()
            await task
            chunks: list[str] = []
            while not queue.empty():
                item = queue.get_nowait()
                if item is None:
                    break
                chunks.append(item[1])
            followed = "".join(chunks)
            self.assertNotIn("OLD RUN MUST NOT APPEAR", followed)
            for name in ("SMTP", "IMAP", "POP", "web"):
                self.assertIn(f"Configuring {name} port {secret}", followed)

            transcript = Path(td) / "deploy-last.log"
            child = (
                "import time, pathlib\n"
                "p = pathlib.Path(%r)\n"
                "secret = %r\n"
                "time.sleep(0.2)\n"
                "p.parent.mkdir(parents=True, exist_ok=True)\n"
                "p.write_text('')\n"
                "for name in ('SMTP', 'IMAP', 'POP', 'web'):\n"
                "    with p.open('a', encoding='utf-8') as fh:\n"
                "        fh.write('Configuring ' + name + ' port ' + secret + '\\n')\n"
                "        fh.flush()\n"
                "    time.sleep(0.08)\n"
                "print('driver: apply', flush=True)\n"
                "time.sleep(0.35)\n"
            ) % (str(sidecar.with_name("live.log")), secret)
            live = sidecar.with_name("live.log")
            events: list[dict] = []
            async for ev in _stream_subprocess(
                ["python3", "-c", child],
                transcript=transcript,
                secrets=[secret],
                follow_logs=[live],
                follow_poll_s=0.05,
            ):
                events.append(ev)
            joined = "".join(str(e.get("data") or "") for e in events)
            body = transcript.read_text(encoding="utf-8")
            for name in ("SMTP", "IMAP", "POP", "web"):
                self.assertIn(f"Configuring {name} port", joined)
                self.assertIn(f"Configuring {name} port", body)
            self.assertIn("driver: apply", joined)
            self.assertNotIn(secret, joined)
            self.assertNotIn(secret, body)
            self.assertIn("***", joined)
            self.assertTrue(any(e.get("type") == "done" and e.get("exit_code") == 0 for e in events))

    async def test_file_backed_child_writes_transcript_directly(self) -> None:
        """Full-install stdio is a file, not a pipe, so a dying parent cannot SIGPIPE it."""
        import tempfile
        from pathlib import Path

        from kin_privhelper.commands import _stream_subprocess

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "deploy-last.log"
            events: list[dict] = []
            async for ev in _stream_subprocess(
                ["python3", "-c", "print('telemetry -> No', flush=True)"],
                transcript=path,
                file_backed=True,
            ):
                events.append(ev)
            body = path.read_text(encoding="utf-8")
            self.assertIn("telemetry -> No", body)
            self.assertTrue(any(e.get("type") == "done" and e.get("exit_code") == 0 for e in events))
            joined = "".join(str(e.get("data") or "") for e in events)
            self.assertIn("telemetry -> No", joined)


class SshLoginCandidateTests(unittest.TestCase):
    def test_candidates_are_kin_then_root_never_cursor(self) -> None:
        from kin_privhelper.orchestration import ssh_password_candidates

        pairs = ssh_password_candidates("kin-secret", "root-secret")
        users = [u for u, _ in pairs]
        self.assertEqual(users, ["kin", "root"])
        self.assertNotIn("cursor", users)
        self.assertEqual(pairs[0], ("kin", "kin-secret"))
        self.assertEqual(pairs[1], ("root", "root-secret"))

    def test_shared_user_prefers_kin_when_both_hosts_accept_it(self) -> None:
        from kin_privhelper.orchestration import choose_shared_ssh_user

        ok = {
            ("peer", "kin"): True,
            ("peer", "root"): True,
            ("monitoring", "kin"): True,
            ("monitoring", "root"): True,
        }
        self.assertEqual(
            choose_shared_ssh_user(ok, hosts=("peer", "monitoring")),
            "kin",
        )

    def test_shared_user_falls_back_to_root_when_mon_has_no_kin(self) -> None:
        from kin_privhelper.orchestration import choose_shared_ssh_user

        ok = {
            ("peer", "kin"): True,
            ("peer", "root"): True,
            ("monitoring", "kin"): False,
            ("monitoring", "root"): True,
        }
        self.assertEqual(
            choose_shared_ssh_user(ok, hosts=("peer", "monitoring")),
            "root",
        )

    def test_shared_user_empty_when_hosts_disagree(self) -> None:
        from kin_privhelper.orchestration import choose_shared_ssh_user

        ok = {
            ("peer", "kin"): True,
            ("peer", "root"): False,
            ("monitoring", "kin"): False,
            ("monitoring", "root"): True,
        }
        self.assertEqual(choose_shared_ssh_user(ok, hosts=("peer", "monitoring")), "")

    def test_privhelper_sources_have_no_cursor_ssh_username(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "kin_privhelper"
        for name in ("orchestration.py", "commands.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn('"cursor"', text, msg=name)
            self.assertNotIn("'cursor'", text, msg=name)
            self.assertIn("ssh_password_candidates", text)

    def test_backup_and_inventory_defaults_use_kin(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        backup = (repo / "backup" / "kin-mail-backup.sh").read_text(encoding="utf-8")
        self.assertIn('SSH_USER="kin"', backup)
        self.assertNotIn('SSH_USER="cursor"', backup)
        example = (repo / "backup" / "kin-mail-backup.conf.example").read_text(
            encoding="utf-8"
        )
        self.assertIn('SSH_USER="kin"', example)
        self.assertNotIn('SSH_USER="cursor"', example)
        inv_dir = repo / "ansible" / "inventory"
        for path in sorted(inv_dir.glob("*.example.yml")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("ansible_user: cursor", text, msg=str(path))
            if "ansible_user:" in text:
                self.assertIn("ansible_user: kin", text, msg=str(path))


class PeerInstallArchiveTests(unittest.TestCase):
    def test_archive_extracts_install_kin_mail_sh(self) -> None:
        import tarfile
        import tempfile
        from pathlib import Path

        from kin_privhelper.orchestration import build_peer_install_archive

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "install"
            src.mkdir()
            (src / "kin-mail.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (src / "nested").mkdir()
            (src / "nested" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            dest = Path(td) / "peer-install.tgz"
            build_peer_install_archive(src, dest)
            self.assertTrue(dest.is_file())
            with tarfile.open(dest, "r:gz") as tar:
                names = {info.name.replace("\\", "/") for info in tar.getmembers()}
            self.assertIn("install/kin-mail.sh", names)
            self.assertIn("install/nested/helper.sh", names)
            self.assertNotIn(
                "ansible/roles/drbd_disk_prep/files/select_drbd_disk.py", names
            )

    def test_archive_includes_disk_selector_when_provided(self) -> None:
        import tarfile
        import tempfile
        from pathlib import Path

        from kin_privhelper.orchestration import build_peer_install_archive

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "install"
            src.mkdir()
            (src / "kin-mail.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            selector = Path(td) / "select_drbd_disk.py"
            selector.write_text("print('ok')\n", encoding="utf-8")
            dest = Path(td) / "peer-install.tgz"
            build_peer_install_archive(src, dest, selector=selector)
            with tarfile.open(dest, "r:gz") as tar:
                names = {info.name.replace("\\", "/") for info in tar.getmembers()}
            self.assertIn("install/kin-mail.sh", names)
            self.assertIn(
                "ansible/roles/drbd_disk_prep/files/select_drbd_disk.py", names
            )

    def test_archive_refuses_missing_driver(self) -> None:
        import tempfile
        from pathlib import Path

        from kin_privhelper.orchestration import build_peer_install_archive

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "install"
            src.mkdir()
            dest = Path(td) / "peer-install.tgz"
            with self.assertRaises(FileNotFoundError):
                build_peer_install_archive(src, dest)


class SyncPeerHaConsoleStateTests(unittest.IsolatedAsyncioTestCase):
    """sync_peer_ha_console_state must not leave the peer at TOPOLOGY=2vm +
    ha-setup-complete when the users.json push fails - that split state used
    to strand the local wizard on the anonymous pre-deploy Super Admin
    identity even though the peer already looked fully done (platform bug
    audit, 25 Aug 2026).
    """

    _PROBE_BLOB = (
        "KIN_PEER_STATE_BEGIN\nHA=0\nSETUP=0\nCFG=1\nTOPO=0\nKIN_PEER_STATE_END\n"
        "KIN_PEER_HA_BEGIN\nKIN_PEER_HA_END\n"
        "KIN_PEER_TOPO_BEGIN\nKIN_PEER_TOPO_END\n"
        'KIN_PEER_CFG_BEGIN\nTOPOLOGY="1vm"\nKIN_PEER_CFG_END\n'
    )

    async def test_users_push_failure_writes_no_peer_marker(self) -> None:
        from unittest.mock import AsyncMock, patch

        from kin_privhelper.orchestration import sync_peer_ha_console_state

        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        with (
            patch(
                "kin_privhelper.orchestration._ssh_run",
                new=AsyncMock(return_value=(0, self._PROBE_BLOB)),
            ),
                patch(
                    "kin_privhelper.orchestration.ensure_peer_console_runtime",
                    new=AsyncMock(return_value=(True, ["peer console ok"])),
                ),
            patch(
                "kin_privhelper.console_users_sync.push_local_users_to_peer",
                new=AsyncMock(return_value=(False, "users push boom")),
            ),
            patch(
                "kin_privhelper.orchestration._push_peer_text_file",
                new=AsyncMock(return_value=(0, "")),
            ) as push_file,
        ):
            ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        self.assertFalse(ok)
        self.assertTrue(any("users push boom" in n for n in notes))
        push_file.assert_not_awaited()

    async def test_users_push_success_then_writes_peer_markers(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from kin_console import settings as settings_mod
        from kin_privhelper.orchestration import sync_peer_ha_console_state

        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        with tempfile.TemporaryDirectory() as td:
            secret_path = Path(td) / "session.secret"
            secret_path.write_text("fixed-session-secret\n", encoding="utf-8")
            with (
                patch(
                    "kin_privhelper.orchestration._ssh_run",
                    new=AsyncMock(return_value=(0, self._PROBE_BLOB)),
                ),
                patch(
                    "kin_privhelper.orchestration.ensure_peer_console_runtime",
                    new=AsyncMock(return_value=(True, ["peer console ok"])),
                ),
                patch(
                    "kin_privhelper.console_users_sync.push_local_users_to_peer",
                    new=AsyncMock(return_value=(True, "users pushed")),
                ),
                patch(
                    "kin_privhelper.orchestration._push_peer_text_file",
                    new=AsyncMock(return_value=(0, "")),
                ) as push_file,
                patch("kin_privhelper.deploy_state.ensure_server_id", return_value="fixed-server-id"),
                patch("kin_privhelper.deploy_state.read_license_token", return_value=""),
                patch("kin_console.auth.ensure_session_secret"),
                patch.object(settings_mod.settings, "session_secret_file", secret_path),
            ):
                ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        self.assertTrue(ok)
        self.assertTrue(any("users pushed" in n for n in notes))
        dests = [c.kwargs.get("dest") for c in push_file.await_args_list]
        self.assertIn("/etc/kin-mail/server-id", dests)
        self.assertIn("/etc/kin-mail/ha-setup-complete", dests)
        self.assertLess(
            dests.index("/etc/kin-mail/server-id"),
            dests.index("/etc/kin-mail/ha-setup-complete"),
        )

    async def test_server_id_and_license_token_synced_to_peer(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from kin_console import settings as settings_mod
        from kin_privhelper.orchestration import sync_peer_ha_console_state

        # Peer already fully caught up on config/markers (noop) - isolates
        # the new server-id/license.token/session.secret push from the
        # marker-write block.
        noop_blob = (
            "KIN_PEER_STATE_BEGIN\nHA=1\nSETUP=1\nCFG=1\nTOPO=1\nKIN_PEER_STATE_END\n"
            "KIN_PEER_HA_BEGIN\ncomplete 2026-08-21T00:00:00Z\nKIN_PEER_HA_END\n"
            "KIN_PEER_TOPO_BEGIN\n2vm\nKIN_PEER_TOPO_END\n"
            'KIN_PEER_CFG_BEGIN\nTOPOLOGY="2vm"\nKIN_PEER_CFG_END\n'
        )
        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        with tempfile.TemporaryDirectory() as td:
            secret_path = Path(td) / "session.secret"
            secret_path.write_text("fixed-session-secret\n", encoding="utf-8")
            with (
                patch(
                    "kin_privhelper.orchestration._ssh_run",
                    new=AsyncMock(return_value=(0, noop_blob)),
                ),
                patch(
                    "kin_privhelper.orchestration.ensure_peer_console_runtime",
                    new=AsyncMock(return_value=(True, ["peer console ok"])),
                ),
                patch(
                    "kin_privhelper.console_users_sync.push_local_users_to_peer",
                    new=AsyncMock(return_value=(True, "users pushed")),
                ),
                patch(
                    "kin_privhelper.orchestration._push_peer_text_file",
                    new=AsyncMock(return_value=(0, "")),
                ) as push_file,
                patch(
                    "kin_privhelper.deploy_state.ensure_server_id",
                    return_value="fixed-server-id",
                ),
                patch(
                    "kin_privhelper.deploy_state.read_license_token",
                    return_value="signed-license-token-blob",
                ),
                patch("kin_console.auth.ensure_session_secret"),
                patch.object(settings_mod.settings, "session_secret_file", secret_path),
            ):
                ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        self.assertTrue(ok)
        dests = [c.kwargs.get("dest") for c in push_file.await_args_list]
        self.assertIn("/etc/kin-mail/server-id", dests)
        self.assertIn("/var/lib/kin-mail-console/license.token", dests)
        self.assertIn("/var/lib/kin-mail-console/session.secret", dests)
        bodies = {c.kwargs.get("dest"): c.kwargs.get("body") for c in push_file.await_args_list}
        self.assertEqual(bodies["/etc/kin-mail/server-id"], "fixed-server-id\n")
        self.assertEqual(
            bodies["/var/lib/kin-mail-console/license.token"], "signed-license-token-blob\n"
        )
        self.assertEqual(
            bodies["/var/lib/kin-mail-console/session.secret"], "fixed-session-secret\n"
        )
        self.assertTrue(any("Synced server-id" in n for n in notes))
        self.assertTrue(any("Synced license.token" in n for n in notes))
        self.assertTrue(any("Synced session.secret" in n for n in notes))
        for dest in (
            "/var/lib/kin-mail-console/license.token",
            "/var/lib/kin-mail-console/session.secret",
        ):
            calls = [c for c in push_file.await_args_list if c.kwargs.get("dest") == dest]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].kwargs.get("mode"), "600")
            self.assertEqual(calls[0].kwargs.get("owner"), "kin-console")
            self.assertEqual(calls[0].kwargs.get("group"), "kin-console")

    async def test_session_secret_push_failure_is_not_fatal(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from kin_console import settings as settings_mod
        from kin_privhelper.orchestration import sync_peer_ha_console_state

        noop_blob = (
            "KIN_PEER_STATE_BEGIN\nHA=1\nSETUP=1\nCFG=1\nTOPO=1\nKIN_PEER_STATE_END\n"
            "KIN_PEER_HA_BEGIN\ncomplete 2026-08-21T00:00:00Z\nKIN_PEER_HA_END\n"
            "KIN_PEER_TOPO_BEGIN\n2vm\nKIN_PEER_TOPO_END\n"
            'KIN_PEER_CFG_BEGIN\nTOPOLOGY="2vm"\nKIN_PEER_CFG_END\n'
        )
        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")

        async def push(_host, _user, _password, _secrets, **kw):
            if kw.get("dest") == "/var/lib/kin-mail-console/session.secret":
                return 1, "scp fail"
            return 0, ""

        with tempfile.TemporaryDirectory() as td:
            secret_path = Path(td) / "session.secret"
            secret_path.write_text("fixed-session-secret\n", encoding="utf-8")
            with (
                patch(
                    "kin_privhelper.orchestration._ssh_run",
                    new=AsyncMock(return_value=(0, noop_blob)),
                ),
                patch(
                    "kin_privhelper.orchestration.ensure_peer_console_runtime",
                    new=AsyncMock(return_value=(True, ["peer console ok"])),
                ),
                patch(
                    "kin_privhelper.console_users_sync.push_local_users_to_peer",
                    new=AsyncMock(return_value=(True, "users pushed")),
                ),
                patch(
                    "kin_privhelper.orchestration._push_peer_text_file",
                    new=AsyncMock(side_effect=push),
                ),
                patch(
                    "kin_privhelper.deploy_state.ensure_server_id",
                    return_value="fixed-server-id",
                ),
                patch(
                    "kin_privhelper.deploy_state.read_license_token",
                    return_value="",
                ),
                patch("kin_console.auth.ensure_session_secret"),
                patch.object(settings_mod.settings, "session_secret_file", secret_path),
            ):
                ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        # A session.secret push failure must not fail the whole HA build -
        # unlike server-id/license.token, it only affects login-cookie
        # continuity across a future VIP failover.
        self.assertTrue(ok)
        self.assertTrue(any("Could not write session.secret" in n for n in notes))

    async def test_no_local_license_token_skips_that_push(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from kin_console import settings as settings_mod
        from kin_privhelper.orchestration import sync_peer_ha_console_state

        noop_blob = (
            "KIN_PEER_STATE_BEGIN\nHA=1\nSETUP=1\nCFG=1\nTOPO=1\nKIN_PEER_STATE_END\n"
            "KIN_PEER_HA_BEGIN\ncomplete 2026-08-21T00:00:00Z\nKIN_PEER_HA_END\n"
            "KIN_PEER_TOPO_BEGIN\n2vm\nKIN_PEER_TOPO_END\n"
            'KIN_PEER_CFG_BEGIN\nTOPOLOGY="2vm"\nKIN_PEER_CFG_END\n'
        )
        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        with tempfile.TemporaryDirectory() as td:
            secret_path = Path(td) / "session.secret"
            secret_path.write_text("fixed-session-secret\n", encoding="utf-8")
            with (
                patch(
                    "kin_privhelper.orchestration._ssh_run",
                    new=AsyncMock(return_value=(0, noop_blob)),
                ),
                patch(
                    "kin_privhelper.orchestration.ensure_peer_console_runtime",
                    new=AsyncMock(return_value=(True, ["peer console ok"])),
                ),
                patch(
                    "kin_privhelper.console_users_sync.push_local_users_to_peer",
                    new=AsyncMock(return_value=(True, "users pushed")),
                ),
                patch(
                    "kin_privhelper.orchestration._push_peer_text_file",
                    new=AsyncMock(return_value=(0, "")),
                ) as push_file,
                patch(
                    "kin_privhelper.deploy_state.ensure_server_id",
                    return_value="fixed-server-id",
                ),
                patch("kin_privhelper.deploy_state.read_license_token", return_value=""),
                patch("kin_console.auth.ensure_session_secret"),
                patch.object(settings_mod.settings, "session_secret_file", secret_path),
            ):
                ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        self.assertTrue(ok)
        dests = [c.kwargs.get("dest") for c in push_file.await_args_list]
        self.assertIn("/etc/kin-mail/server-id", dests)
        self.assertNotIn("/var/lib/kin-mail-console/license.token", dests)
        self.assertIn("/var/lib/kin-mail-console/session.secret", dests)

    async def test_identity_push_failure_writes_no_peer_marker(self) -> None:
        from unittest.mock import AsyncMock, patch

        from kin_privhelper.orchestration import sync_peer_ha_console_state

        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")

        async def push(_host, _user, _password, _secrets, **kw):
            if kw.get("dest") == "/etc/kin-mail/server-id":
                return 1, "scp fail"
            return 0, ""

        with (
            patch(
                "kin_privhelper.orchestration._ssh_run",
                new=AsyncMock(return_value=(0, self._PROBE_BLOB)),
            ),
                patch(
                    "kin_privhelper.orchestration.ensure_peer_console_runtime",
                    new=AsyncMock(return_value=(True, ["peer console ok"])),
                ),
            patch(
                "kin_privhelper.console_users_sync.push_local_users_to_peer",
                new=AsyncMock(return_value=(True, "users pushed")),
            ),
            patch(
                "kin_privhelper.orchestration._push_peer_text_file",
                new=AsyncMock(side_effect=push),
            ) as push_file,
            patch("kin_privhelper.deploy_state.ensure_server_id", return_value="fixed-server-id"),
            patch("kin_privhelper.deploy_state.read_license_token", return_value=""),
        ):
            ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        self.assertFalse(ok)
        self.assertTrue(any("Failed to write server-id" in n for n in notes))
        dests = [c.kwargs.get("dest") for c in push_file.await_args_list]
        self.assertEqual(dests, ["/etc/kin-mail/server-id"])
        self.assertNotIn("/etc/kin-mail/ha-setup-complete", dests)

    async def test_peer_console_runtime_failure_skips_users_and_markers(self) -> None:
        from unittest.mock import AsyncMock, patch

        from kin_privhelper.orchestration import sync_peer_ha_console_state

        host = OrchHost("mail2.example.test", "192.0.2.14", "mail2")
        with (
            patch(
                "kin_privhelper.orchestration._ssh_run",
                new=AsyncMock(return_value=(0, self._PROBE_BLOB)),
            ),
            patch(
                "kin_privhelper.orchestration.ensure_peer_console_runtime",
                new=AsyncMock(return_value=(False, ["activate boom"])),
            ),
            patch(
                "kin_privhelper.console_users_sync.push_local_users_to_peer",
                new=AsyncMock(return_value=(True, "users pushed")),
            ) as users_push,
            patch(
                "kin_privhelper.orchestration._push_peer_text_file",
                new=AsyncMock(return_value=(0, "")),
            ) as push_file,
        ):
            ok, notes = await sync_peer_ha_console_state(host, "kin", "pw", [])

        self.assertFalse(ok)
        self.assertTrue(any("activate boom" in n for n in notes))
        users_push.assert_not_awaited()
        push_file.assert_not_awaited()


class EnsureSshPasswordDoneLeakTests(unittest.IsolatedAsyncioTestCase):
    """--ensure-ssh-password is one small step inside cmd_run_ha_orchestration,
    not the whole job. Its `done` event used to be re-yielded verbatim -
    DeploySession.tsx closes its EventSource on the first `done` it sees, so
    the operator's UI looked finished after well under a second while Ansible
    kept running unattended (re-audit, 25 Aug 2026).
    """

    async def _collect(self, args: dict | None = None) -> list[dict]:
        from kin_privhelper.orchestration import cmd_run_ha_orchestration

        return [ev async for ev in cmd_run_ha_orchestration(args)]

    async def test_failure_aborts_with_exactly_one_done_and_no_ansible(self) -> None:
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from kin_privhelper import protocol as proto

        async def fake_stream(*_a, **_kw):
            yield proto.event_stdout("ensure-ssh-password: could not reload sshd\n")
            yield proto.event_done(1)

        with (
            patch("kin_privhelper.commands._stream_subprocess", new=fake_stream),
            patch(
                "kin_privhelper.commands.resolve_prepare_os",
                return_value=Path("/opt/kin-mail-deploy/install/02-prepare-os.sh"),
            ),
            patch(
                "kin_privhelper.orchestration.load_secrets",
                new=lambda: (_ for _ in ()).throw(
                    AssertionError("must not reach load_secrets after ensure-ssh-password fails")
                ),
            ),
        ):
            events = await self._collect()

        done_events = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done_events), 1, "exactly one done event, not one per subprocess")
        self.assertEqual(done_events[0].get("exit_code"), 1)
        self.assertTrue(
            any("ORCH_FAILED step=ensure_ssh_password" in str(ev.get("data")) for ev in events)
        )
        self.assertTrue(
            any("could not reload sshd" in str(ev.get("data")) for ev in events),
            "the subprocess's own stdout must still reach the operator",
        )

    async def test_success_does_not_yield_its_done_and_continues(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper import protocol as proto

        async def fake_stream(*_a, **_kw):
            yield proto.event_stdout("ok\n")
            yield proto.event_done(0)

        # A successful ensure-ssh-password must not itself produce a `done`
        # or return early - prove control flow reaches load_secrets() by
        # making that raise and asserting the exception (not a swallowed
        # early exit) is what stops the generator.
        sentinel = RuntimeError("reached load_secrets - ensure-ssh-password did not early-return")
        with (
            patch("kin_privhelper.commands._stream_subprocess", new=fake_stream),
            patch(
                "kin_privhelper.commands.resolve_prepare_os",
                return_value=Path("/opt/kin-mail-deploy/install/02-prepare-os.sh"),
            ),
            patch(
                "kin_privhelper.orchestration.load_secrets",
                side_effect=sentinel,
            ),
        ):
            events: list[dict] = []
            with self.assertRaises(RuntimeError) as ctx:
                async for ev in self._collect_iter():
                    events.append(ev)
            self.assertIs(ctx.exception, sentinel)

        self.assertFalse(
            any(ev.get("type") == "done" for ev in events),
            "ensure-ssh-password success must not yield its own done event",
        )
        self.assertTrue(any("ok" in str(ev.get("data")) for ev in events))

    async def _collect_iter(self):
        from kin_privhelper.orchestration import cmd_run_ha_orchestration

        async for ev in cmd_run_ha_orchestration(None):
            yield ev


if __name__ == "__main__":
    unittest.main()
