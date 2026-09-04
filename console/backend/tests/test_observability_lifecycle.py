"""Add/Remove Observability planners (no live cluster)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.observability_lifecycle import (
    boot_id_unchanged,
    observability_inventory_name,
    plan_add_observability,
    plan_remove_observability,
    render_observability_inventory,
    sbd_stop_order,
)
from kin_privhelper.qdevice_status import parse_quorum_votes, quorum_votes_match_rebuild, strip_quorum_device
from kin_privhelper.observability_status import snapshot_from_texts

REPO = Path(__file__).resolve().parents[3]
QUORUM_OK = """\
Quorum information
------------------
Expected votes:   3
Highest expected: 3
Total votes:      3
Quorum:           2
Flags:            Quorate Qdevice
"""


class ObservabilityLifecycleTests(unittest.TestCase):
    def test_remove_refuses_healthy_or_ssh(self) -> None:
        plan = plan_remove_observability(
            identity="192.0.2.53",
            reachable=True,
            ssh_ok=False,
        )
        self.assertTrue(plan.errors)
        alive = plan_remove_observability(
            identity="192.0.2.53",
            reachable=False,
            ssh_ok=True,
        )
        self.assertTrue(alive.errors)

    def test_remove_forced_when_unreachable(self) -> None:
        plan = plan_remove_observability(
            identity="192.0.2.53",
            reachable=False,
            ssh_ok=False,
        )
        self.assertEqual(plan.errors, ())
        self.assertEqual(plan.status, "unreachable")
        self.assertTrue(any("forced" in n for n in plan.notes))

    def test_remove_refuses_absent(self) -> None:
        plan = plan_remove_observability(identity="", reachable=False, ssh_ok=False)
        self.assertTrue(plan.errors)

    def test_add_only_when_absent(self) -> None:
        blocked = plan_add_observability(
            current_identity="192.0.2.53",
            current_reachable=False,
            new_ip="192.0.2.99",
            has_credentials=True,
        )
        self.assertTrue(blocked.errors)
        ok = plan_add_observability(
            current_identity="",
            current_reachable=False,
            new_ip="192.0.2.99",
            hostname="obs.example.test",
            has_credentials=True,
        )
        self.assertEqual(ok.errors, ())
        self.assertEqual(ok.new_ip, "192.0.2.99")
        self.assertNotEqual(ok.new_ip, "192.0.2.53")

    def test_add_never_defaults_old_ip(self) -> None:
        plan = plan_add_observability(
            current_identity="",
            current_reachable=False,
            new_ip="",
            has_credentials=True,
        )
        self.assertTrue(any("IPv4" in e for e in plan.errors))

    def test_sbd_stop_order_unpromoted_first(self) -> None:
        order = sbd_stop_order(
            promoted="mail.example.test",
            hosts=["mail.example.test", "mail2.example.test"],
        )
        self.assertEqual(order[0], "mail2.example.test")
        self.assertEqual(order[-1], "mail.example.test")

    def test_inventory_never_embeds_passwords(self) -> None:
        inv = render_observability_inventory(
            mail_hosts=[
                ("mail.example.test", "192.0.2.51", "mail"),
                ("mail2.example.test", "192.0.2.52", "mail2"),
            ],
            obs_name="obs.example.test",
            obs_ip="192.0.2.99",
            retired_ip="192.0.2.53",
            local_mail_name="mail.example.test",
            force_tls_reinit=True,
            expect_fresh_sbd=True,
        )
        self.assertIn("lookup", inv)
        self.assertIn("KIN_OBS_ANSIBLE_PASSWORD", inv)
        self.assertIn("corosync_qdevice_force_tls_reinit: true", inv)
        self.assertIn("observability_expect_fresh_sbd: true", inv)
        self.assertIn("ansible_connection: local", inv)
        self.assertNotIn("cluster_remove_host", inv)
        self.assertNotIn("cluster_survivor_replace", inv)

    def test_remove_inventory_omits_dead_observability_host(self) -> None:
        inv = render_observability_inventory(
            mail_hosts=[
                ("mail.example.test", "192.0.2.51", "mail"),
                ("mail2.example.test", "192.0.2.52", "mail2"),
            ],
            obs_name="observability-retired",
            obs_ip="192.0.2.53",
            retired_ip="192.0.2.53",
            local_mail_name="mail",  # short name must still map to local
            include_observability_host=False,
        )
        self.assertNotIn("monitoring:", inv)
        self.assertNotIn("KIN_OBS_ANSIBLE_PASSWORD", inv)
        self.assertIn("mail_nodes:", inv)
        self.assertIn("ansible_connection: local", inv)
        self.assertIn("observability_retired_ip: \"192.0.2.53\"", inv)

    def test_remove_error_tells_operator_to_power_off_first(self) -> None:
        plan = plan_remove_observability(
            identity="192.0.2.53",
            reachable=True,
            ssh_ok=False,
        )
        self.assertTrue(any("Power off or delete" in e for e in plan.errors))

    def test_inventory_name(self) -> None:
        self.assertEqual(
            observability_inventory_name(ip="192.0.2.99", hostname="obs.example.test"),
            "obs.example.test",
        )
        self.assertEqual(
            observability_inventory_name(ip="192.0.2.99", hostname=""),
            "obs-192-0-2-99",
        )

    def test_quorum_rebuild_match(self) -> None:
        self.assertEqual(parse_quorum_votes(QUORUM_OK), (3, 3, 2))
        self.assertTrue(quorum_votes_match_rebuild(QUORUM_OK))
        self.assertFalse(quorum_votes_match_rebuild("Expected votes: 2\nTotal votes: 2\nQuorum: 2\n"))

    def test_strip_helper_matches_privhelper(self) -> None:
        path = (
            REPO
            / "ansible"
            / "roles"
            / "observability_disarm"
            / "files"
            / "strip_quorum_device.py"
        )
        spec = importlib.util.spec_from_file_location("strip_helper", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        conf = """quorum {
    provider: corosync_votequorum
    device {
        votes: 1
        model: net
        net {
            host: 192.0.2.53
            port: 5403
        }
    }
}
"""
        a, ac = strip_quorum_device(conf)
        b, bc = mod.strip_quorum_device(conf)
        self.assertEqual(a, b)
        self.assertEqual(ac, bc)

    def test_roles_are_not_mail_node_remove(self) -> None:
        remove = (
            REPO / "ansible" / "playbooks" / "mail-remove-observability.yml"
        ).read_text(encoding="utf-8")
        add = (REPO / "ansible" / "playbooks" / "mail-add-observability.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("observability_disarm", remove)
        self.assertNotIn("cluster_remove_host", remove)
        self.assertNotIn("cluster_survivor_replace", remove)
        self.assertIn("corosync_qnetd", add)
        self.assertIn("iscsi_target", add)
        self.assertIn("observability_rearm", add)
        self.assertNotIn("cluster_survivor_replace", add)

    def test_tls_role_can_force_reinit(self) -> None:
        tls = (
            REPO / "ansible" / "roles" / "corosync_qdevice" / "tasks" / "tls.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("corosync_qdevice_force_tls_reinit", tls)
        self.assertIn("inventory_hostname != ansible_play_hosts[0]", tls)
        self.assertIn("corosync-qdevice-net-certutil -m", tls)
        minus_m = tls.find("corosync-qdevice-net-certutil -m")
        self.assertNotIn(
            'creates: "{{ corosync_qdevice_tls_ready_marker }}"',
            tls[minus_m : minus_m + 500],
        )
        self.assertIn("corosync_qdevice_m.rc", tls)
        self.assertIn("corosync_qdevice_tls_ready_marker", tls)
        self.assertIn("corosync_qdevice_tls_marker", tls)

    def test_gating_falls_out_of_status(self) -> None:
        gone = snapshot_from_texts(config_ip="", corosync_conf="quorum {\n}\n", reachable=False)
        self.assertTrue(gone["can_add"])
        self.assertFalse(gone["can_remove"])

    def test_boot_id_unchanged_fails_closed(self) -> None:
        same = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        self.assertTrue(boot_id_unchanged(same, same + "\n"))
        self.assertFalse(boot_id_unchanged(same, "ffffffff-ffff-ffff-ffff-ffffffffffff"))
        self.assertFalse(boot_id_unchanged("", same))
        self.assertFalse(boot_id_unchanged(same, ""))
        self.assertFalse(boot_id_unchanged("  ", "  "))

    def test_disarm_stop_checks_boot_id_and_fails_closed(self) -> None:
        stop_one = (
            REPO
            / "ansible"
            / "roles"
            / "observability_disarm"
            / "tasks"
            / "stop_one.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("/proc/sys/kernel/random/boot_id", stop_one)
        self.assertIn("kin_boot_id_unchanged", stop_one)
        self.assertIn("systemctl stop sbd", stop_one)
        self.assertNotIn("failed_when: false", stop_one)
        self.assertIn("observability_boot_id_before", stop_one)
        self.assertIn("observability_boot_id_after", stop_one)

    def test_ansible_boot_id_filter_is_the_same_function(self) -> None:
        path = (
            REPO
            / "ansible"
            / "roles"
            / "observability_disarm"
            / "filter_plugins"
            / "boot_id.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_boot_id_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.boot_id_unchanged, boot_id_unchanged)
        self.assertTrue(
            mod.FilterModule().filters()["kin_boot_id_unchanged"](
                "aaa\n", "aaa"
            )
        )
        # This used to assert the OPPOSITE - that start_one.yml contained no
        # boot_id check at all - which encoded the assumption that arming SBD
        # is safe and only stopping it can fence a node. Arming is when sbd
        # opens the watchdog and reads its slot, so it is exactly when a node
        # can reset; if it did, the loop went straight on to arm the peer and
        # took mail down on both. Both loops carry the guard now, and
        # test_observability_rearm_guard.py covers the arming side in full.
        rearm_start = (
            REPO
            / "ansible"
            / "roles"
            / "observability_rearm"
            / "tasks"
            / "start_one.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("kin_boot_id_unchanged", rearm_start)
        self.assertIn("/proc/sys/kernel/random/boot_id", rearm_start)


if __name__ == "__main__":
    unittest.main()


class ReplacementIsNamedTests(unittest.TestCase):
    """Remove then Add must leave the same estate you started with.

    The first Observability VM keeps its image hostname unless something
    renames it; that was fixed for the initial deploy on 29 Aug 2026 but the
    rebuild path did not include the role at all, so a replacement would have
    come back called `ubuntu` while the console still showed `observability`.
    """

    def test_the_inventory_names_the_replacement_host(self) -> None:
        inv = render_observability_inventory(
            mail_hosts=[("mail.example.test", "192.0.2.1", "a")],
            obs_name="obs-192-0-2-5",
            obs_ip="192.0.2.5",
            mail_domain="example.test",
        )
        self.assertIn('observability_hostname: "observability.example.test"', inv)

    def test_no_domain_means_no_invented_name(self) -> None:
        inv = render_observability_inventory(
            mail_hosts=[("mail.example.test", "192.0.2.1", "a")],
            obs_name="obs-192-0-2-5",
            obs_ip="192.0.2.5",
        )
        self.assertNotIn("observability_hostname", inv)

    def test_the_rebuild_playbook_runs_the_identity_role(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        play = (
            repo / "ansible/playbooks/mail-add-observability.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("observability_identity", play)
        # It has to run before qnetd: the TLS identity and the corosync config
        # should be written by a host that already knows its own name.
        self.assertLess(
            play.index("observability_identity"), play.index("corosync_qnetd")
        )

    def test_add_passes_the_domain_through(self) -> None:
        from pathlib import Path

        ops = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper/observability_ops.py"
        ).read_text(encoding="utf-8")
        self.assertIn("mail_domain=str(config.get(\"MAIL_DOMAIN\")", ops)

