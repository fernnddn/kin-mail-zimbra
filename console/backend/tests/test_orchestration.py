"""Unit tests for HA orchestration inventory + RBAC (no live Ansible)."""

from __future__ import annotations

import unittest

from kin_privhelper.orchestration import (
    STEPS,
    OrchHost,
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
        self.assertNotIn("mail.gits-it.site", inv)
        self.assertNotIn("scsi-36001405", inv)
        self.assertNotIn("10.10.40.", inv)

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
            ids[:6],
            [
                "peer_os_prep",
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

    def test_ansible_cfg_uses_absolute_roles_path(self) -> None:
        from pathlib import Path

        from kin_privhelper.orchestration import render_ansible_cfg

        cfg = render_ansible_cfg(
            local_tmp=Path("/var/lib/example/.ansible/tmp"),
            roles_path=Path("/opt/kin-mail-console/ansible/roles"),
        )
        self.assertIn("roles_path = /opt/kin-mail-console/ansible/roles", cfg)
        self.assertIn("local_tmp = /var/lib/example/.ansible/tmp", cfg)

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
        self.assertFalse(command_allowed(ROLE_CUSTOMER_ADMIN, "store_observability_secrets"))
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
                    ["--skip-tags", "agents,verify"],
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
        self.assertEqual(plays[2][1], ["--skip-tags", "agents,verify"])

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


if __name__ == "__main__":
    unittest.main()
