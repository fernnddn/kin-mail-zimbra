"""Every path that writes a DRBD resource must write the SAME tuned one.

Three roles wrote a two-node kin-zimbra.res: drbd_resource for a greenfield
pair, cluster_survivor_replace and drbd_live_join for Add Host. Only the
greenfield one carried the Phase 7 fix. The other two still had

    syncer { rate 40M; }

with no rate controller and no ping/connect timings, so DRBD's stock
ping-timeout of 5 (half a second) applied. That is exactly the configuration
whose PingAck timeouts tore replication down every three minutes and fed the
Phase 7 fence loop, and Add Host would have put it back on a rebuilt pair.

They are one file now. These tests fail if anyone splits them again.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROLES = REPO / "ansible/roles"
TEMPLATES = [
    ROLES / "drbd_resource/templates/kin-zimbra.res.j2",
    ROLES / "cluster_survivor_replace/templates/kin-zimbra.res.j2",
    ROLES / "drbd_live_join/templates/kin-zimbra.res.j2",
]


def _directives(text: str) -> list[str]:
    """Config lines only. Comments explain the settings and name them too."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TemplateParityTests(unittest.TestCase):
    def test_all_three_are_the_same_file(self) -> None:
        bodies = {t.read_text(encoding="utf-8") for t in TEMPLATES}
        self.assertEqual(len(bodies), 1, "the DRBD templates have drifted apart")

    def test_the_add_host_ones_are_links_not_copies(self) -> None:
        # A copy drifts the moment someone edits one of them, which is how the
        # Phase 7 tuning ended up in one of three.
        for t in TEMPLATES[1:]:
            with self.subTest(template=str(t.relative_to(REPO))):
                self.assertTrue(t.is_symlink(), f"{t} is a copy, not a link")

    def test_the_rate_controller_replaced_the_fixed_syncer(self) -> None:
        body = "\n".join(_directives(TEMPLATES[0].read_text(encoding="utf-8")))
        self.assertNotIn("syncer", body)
        for knob in ("c-plan-ahead", "c-min-rate", "c-max-rate"):
            with self.subTest(knob=knob):
                self.assertIn(knob, body)

    def test_the_link_timings_are_pinned(self) -> None:
        body = "\n".join(_directives(TEMPLATES[0].read_text(encoding="utf-8")))
        # DRBD's stock ping-timeout is 5 tenths of a second. On a shared
        # virtual switch that deadline is missed routinely, and every miss
        # tears down replication.
        for knob in ("ping-int", "ping-timeout", "connect-int", "timeout"):
            with self.subTest(knob=knob):
                self.assertIn(knob, body)


class NoDriftingCopiesTests(unittest.TestCase):
    """Two roles shipping the same filename must ship the same bytes.

    This is how the Phase 7 replication tuning ended up in one of three DRBD
    templates, and how a device-settle retry added after a live failure on
    25 Aug 2026 reached drbd_resource but not drbd_live_join - so a node added
    later got a meta-loop unit that could fail on reboot before the loop
    device appeared.
    """

    def test_files_with_the_same_name_across_roles_are_identical(self) -> None:
        from collections import defaultdict

        by_name: dict[str, list[Path]] = defaultdict(list)
        for path in ROLES.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in (".j2", ".sh", ".py"):
                continue
            by_name[path.name].append(path)

        drifted = []
        for name, paths in sorted(by_name.items()):
            if len(paths) < 2:
                continue
            bodies = {p.read_bytes() for p in paths}
            if len(bodies) > 1:
                drifted.append(
                    f"{name}: " + ", ".join(str(p.relative_to(ROLES)) for p in paths)
                )
        self.assertEqual(drifted, [], "; ".join(drifted))


class TuningVariablesTests(unittest.TestCase):
    """A shared template is only shared if every role defines what it reads."""

    NEEDED = (
        "drbd_resource_tuning",
        "drbd_resource_ping_int",
        "drbd_resource_ping_timeout",
        "drbd_resource_connect_int",
        "drbd_resource_timeout",
        "drbd_resource_c_plan_ahead",
        "drbd_resource_c_fill_target",
        "drbd_resource_c_min_rate",
    )

    def test_every_role_that_renders_it_defines_its_variables(self) -> None:
        for role in ("drbd_resource", "cluster_survivor_replace", "drbd_live_join"):
            defaults = (ROLES / role / "defaults/main.yml").read_text(encoding="utf-8")
            for name in self.NEEDED:
                with self.subTest(role=role, var=name):
                    self.assertIn(f"{name}:", defaults)

    def test_the_add_host_defaults_match_the_greenfield_ones(self) -> None:
        import re

        green = (ROLES / "drbd_resource/defaults/main.yml").read_text(encoding="utf-8")
        wanted = {}
        for name in self.NEEDED:
            m = re.search(rf"^{name}:\s*(\S+)\s*$", green, re.M)
            if m:
                wanted[name] = m.group(1).strip("\"'")
        self.assertTrue(wanted, "read no values from the greenfield defaults")
        for role in ("cluster_survivor_replace", "drbd_live_join"):
            text = (ROLES / role / "defaults/main.yml").read_text(encoding="utf-8")
            for name, value in wanted.items():
                with self.subTest(role=role, var=name):
                    m = re.search(rf"^{name}:\s*(.+)$", text, re.M)
                    self.assertIsNotNone(m)
                    # Mapped through cluster_add_host_* with a default; the
                    # default has to be the greenfield value or a rebuilt pair
                    # gets different replication settings from a fresh one.
                    self.assertIn(value, m.group(1), f"{role}/{name}")


class ReplicationValueTests(unittest.TestCase):
    """A rebuilt pair must replicate on the same terms as a fresh one."""

    def _value(self, path: Path, name: str) -> str:
        import re

        m = re.search(rf"^{name}:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(m, f"{name} not found in {path}")
        return m.group(1).strip("\"'")

    def test_the_resync_ceiling_matches_the_greenfield_one(self) -> None:
        green = self._value(
            ROLES / "drbd_resource/defaults/main.yml", "drbd_resource_sync_rate"
        )
        for role in ("cluster_survivor_replace", "drbd_live_join"):
            with self.subTest(role=role):
                got = self._value(
                    ROLES / role / "defaults/main.yml", "cluster_add_host_drbd_sync_rate"
                )
                # Add Host resyncs a whole disk down the NIC that also carries
                # corosync and the SBD iSCSI session. It was 100M against 40M.
                self.assertEqual(got, green)

    def test_the_port_protocol_and_device_match_too(self) -> None:
        pairs = [
            ("drbd_resource_port", "cluster_add_host_drbd_port"),
            ("drbd_resource_protocol", "cluster_add_host_drbd_protocol"),
            ("drbd_resource_device", "cluster_add_host_drbd_device"),
            ("drbd_resource_name", "cluster_add_host_drbd_resource"),
        ]
        for green_name, add_name in pairs:
            green = self._value(ROLES / "drbd_resource/defaults/main.yml", green_name)
            for role in ("cluster_survivor_replace", "drbd_live_join"):
                with self.subTest(role=role, var=green_name):
                    # A different port would move the survivor's live
                    # replication socket during an Add Host.
                    self.assertEqual(
                        self._value(ROLES / role / "defaults/main.yml", add_name), green
                    )


class AddressGuardTests(unittest.TestCase):
    """Every path that writes a DRBD address must refuse a wrong one.

    An empty address, the same address on both nodes, or the Observability IP
    all point replication somewhere it must never go. The greenfield role has
    always refused them; the two Add Host roles did not, so running those
    playbooks directly - without the console's own validation in front - could
    write any of the three.
    """

    WRITERS = [
        ROLES / "drbd_resource/tasks/resource_file.yml",
        ROLES / "drbd_live_join/tasks/resource_file.yml",
        ROLES / "cluster_survivor_replace/tasks/drbd_peer.yml",
    ]

    def test_every_writer_checks_the_addresses_first(self) -> None:
        for path in self.WRITERS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(role=str(path.relative_to(ROLES))):
                self.assertIn("drbd_resource_node_a_address | length > 0", text)
                self.assertIn(
                    "drbd_resource_node_a_address != drbd_resource_node_b_address", text
                )
                self.assertIn("corosync_qdevice_qnetd_ip", text)

    def test_the_guard_runs_before_the_template(self) -> None:
        for path in self.WRITERS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(role=str(path.relative_to(ROLES))):
                guard = text.index("drbd_resource_node_a_address | length > 0")
                tpl = text.index("ansible.builtin.template:")
                self.assertLess(guard, tpl, "the file is written before it is checked")


class SingleNodeHandoffTests(unittest.TestCase):
    """Remove Host leaves a one-host file. Add Host has to read it back."""

    def test_the_disk_paths_survive_the_round_trip(self) -> None:
        import sys

        sys.path.insert(0, str(REPO / "console/backend"))
        from kin_privhelper.drbd_res import node_device, node_disk, node_meta_disk

        # What cluster_remove_host writes, with a LUKS mapper underneath.
        single = (
            "resource kin-zimbra {\n"
            "  startup {\n    wfc-timeout 60;\n  }\n"
            "  disk {\n    on-io-error detach;\n  }\n"
            "  on mail.example.test {\n"
            "    device    /dev/drbd0;\n"
            "    disk      /dev/mapper/kin-zimbra-crypt;\n"
            "    meta-disk /dev/sdb2;\n"
            "  }\n"
            "}\n"
        )
        # If these come back empty, Add Host falls back to inventory defaults
        # and points DRBD at the raw partition under a live LUKS mapper.
        self.assertEqual(node_disk(single, "mail.example.test"), "/dev/mapper/kin-zimbra-crypt")
        self.assertEqual(node_meta_disk(single, "mail.example.test"), "/dev/sdb2")
        self.assertEqual(node_device(single, "mail.example.test"), "/dev/drbd0")

    def test_the_single_node_template_still_names_the_disks(self) -> None:
        tpl = (
            ROLES / "cluster_remove_host/templates/kin-zimbra-single.res.j2"
        ).read_text(encoding="utf-8")
        for field in ("device", "disk", "meta-disk"):
            with self.subTest(field=field):
                self.assertIn(field, tpl)


if __name__ == "__main__":
    unittest.main()
