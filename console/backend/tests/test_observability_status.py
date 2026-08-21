"""Observability identity, qnetd-host parse, and Add/Remove gating (no live VM)."""

from __future__ import annotations

import unittest

from kin_privhelper.observability_status import (
    classify_observability,
    configured_observability_identity,
    observability_actions,
    observability_public,
    snapshot_from_texts,
)
from kin_privhelper.qdevice_status import parse_qnetd_host, strip_quorum_device

COROSYNC_WITH_QNETD = """\
totem {
    version: 2
    cluster_name: kin-mail
}
nodelist {
    node {
        ring0_addr: 192.0.2.51
        name: mail.example.test
        nodeid: 1
    }
}
quorum {
    provider: corosync_votequorum
    device {
        votes: 1
        model: net
        net {
            tls: on
            host: 192.0.2.53
            port: 5403
            algorithm: ffsplit
            tie_breaker: lowest
        }
    }
}
"""

COROSYNC_NO_DEVICE = """\
quorum {
    provider: corosync_votequorum
}
"""


class ObservabilityStatusTests(unittest.TestCase):
    def test_parse_qnetd_host(self) -> None:
        self.assertEqual(parse_qnetd_host(COROSYNC_WITH_QNETD), "192.0.2.53")
        self.assertEqual(parse_qnetd_host(COROSYNC_NO_DEVICE), "")
        self.assertEqual(parse_qnetd_host(""), "")

    def test_config_ip_wins_over_corosync(self) -> None:
        self.assertEqual(
            configured_observability_identity(
                config_ip="192.0.2.99",
                qnetd_host="192.0.2.53",
            ),
            "192.0.2.99",
        )
        self.assertEqual(
            configured_observability_identity(config_ip="", qnetd_host="192.0.2.53"),
            "192.0.2.53",
        )
        self.assertEqual(
            configured_observability_identity(config_ip="", qnetd_host=""),
            "",
        )

    def test_classify_and_actions(self) -> None:
        self.assertEqual(classify_observability(identity="", reachable=False), "absent")
        self.assertEqual(
            classify_observability(identity="192.0.2.53", reachable=False),
            "unreachable",
        )
        self.assertEqual(
            classify_observability(identity="192.0.2.53", reachable=True),
            "healthy",
        )
        self.assertEqual(
            observability_actions("absent"),
            {"add": True, "remove": False},
        )
        self.assertEqual(
            observability_actions("unreachable"),
            {"add": False, "remove": True},
        )
        self.assertEqual(
            observability_actions("healthy"),
            {"add": False, "remove": False},
        )

    def test_snapshot_does_not_use_qdevice_ok(self) -> None:
        # Reachable TCP with no qdevice vote still counts as healthy for the
        # diagram. qdevice_ok stays a separate CLUSTER_STATUS_JSON field.
        snap = snapshot_from_texts(
            config_ip="192.0.2.53",
            corosync_conf=COROSYNC_WITH_QNETD,
            reachable=True,
            hostname="obs.example.test",
        )
        self.assertEqual(snap["status"], "healthy")
        self.assertTrue(snap["present"])
        self.assertTrue(snap["reachable"])
        self.assertFalse(snap["can_add"])
        self.assertFalse(snap["can_remove"])
        self.assertNotIn("qdevice_ok", snap)

        down = snapshot_from_texts(
            config_ip="192.0.2.53",
            corosync_conf=COROSYNC_WITH_QNETD,
            reachable=False,
        )
        self.assertEqual(down["status"], "unreachable")
        self.assertTrue(down["can_remove"])
        self.assertFalse(down["can_add"])

        gone = snapshot_from_texts(
            config_ip="",
            corosync_conf=COROSYNC_NO_DEVICE,
            reachable=False,
        )
        self.assertEqual(gone["status"], "absent")
        self.assertFalse(gone["present"])
        self.assertTrue(gone["can_add"])
        self.assertFalse(gone["can_remove"])
        self.assertEqual(gone["ip"], "")

    def test_corosync_alone_counts_as_present(self) -> None:
        snap = snapshot_from_texts(
            config_ip="",
            corosync_conf=COROSYNC_WITH_QNETD,
            reachable=False,
        )
        self.assertEqual(snap["status"], "unreachable")
        self.assertEqual(snap["ip"], "192.0.2.53")

    def test_strip_quorum_device(self) -> None:
        new, changed = strip_quorum_device(COROSYNC_WITH_QNETD)
        self.assertTrue(changed)
        self.assertEqual(parse_qnetd_host(new), "")
        self.assertIn("provider: corosync_votequorum", new)
        self.assertNotIn("two_node:", new)
        self.assertNotIn("model: net", new)
        again, changed_again = strip_quorum_device(new)
        self.assertFalse(changed_again)
        self.assertEqual(again, new)

    def test_public_shape(self) -> None:
        pub = observability_public(
            identity="192.0.2.53",
            hostname="obs.example.test",
            status="healthy",
        )
        self.assertEqual(
            set(pub),
            {"present", "status", "ip", "hostname", "reachable", "can_add", "can_remove"},
        )


if __name__ == "__main__":
    unittest.main()
