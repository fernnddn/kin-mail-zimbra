"""Debian/Ubuntu corosync package-stub detection (no live corosync)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.corosync_stub import (
    is_debian_corosync_stub,
    is_harmless_package_stub_cluster,
    parse_pcs_cluster_name,
    parse_resource_instance_count,
)
from kin_privhelper.orchestration import live_cluster_blocks_apply
from kin_privhelper.maintenance import parse_online_nodes


# Live mail.nisaroti.my.id tonight: package default, name: commented out,
# pcs omitted the unnamed node, Online/Offline empty.
LIVE_STUB_NAME_COMMENTED = """\
# Please read the corosync.conf.5 manual page
totem {
	version: 2

	# Corosync itself works without a cluster name, but DLM needs one.
	cluster_name: debian

	crypto_cipher: none
	crypto_hash: none
}

nodelist {
	node {
		# Hostname of the node
		#name: node1
		nodeid: 1
		ring0_addr: 127.0.0.1
	}
}
"""

# Some package versions leave name: node1 uncommented.
STUB_NAME_NODE1 = """\
totem {
	version: 2
	cluster_name: debian
	crypto_cipher: none
	crypto_hash: none
}

nodelist {
	node {
		name: node1
		nodeid: 1
		ring0_addr: 127.0.0.1
	}
}
"""

# Upstream-style example: rings still commented, Debian cluster_name only.
STUB_RINGS_COMMENTED = """\
totem {
	version: 2
	cluster_name: debian
}

nodelist {
	node {
		name: node1
		nodeid: 1
		#ring0_addr: 192.168.0.1
	}
}
"""

PCS_STUB_NODES = """\
Warning: Some nodes are missing names in corosync.conf, those nodes were omitted
Corosync Nodes:
 Online:
 Offline:
"""

REAL_TWO_NODE = """\
totem {
	version: 2
	cluster_name: kin-mail
	transport: knet
	crypto_cipher: aes256
	crypto_hash: sha256
}

nodelist {
	node {
		ring0_addr: 10.10.40.15
		name: mail.nisaroti.my.id
		nodeid: 1
	}
	node {
		ring0_addr: 10.10.40.14
		name: mail2.nisaroti.my.id
		nodeid: 2
	}
}
"""

# cluster_name still debian, but a real LAN member: not the package stub.
DEBIAN_NAME_WITH_LAN = """\
totem {
	version: 2
	cluster_name: debian
}

nodelist {
	node {
		name: mail.nisaroti.my.id
		nodeid: 1
		ring0_addr: 10.10.40.15
	}
}
"""

TWO_LOOPBACK_NODES = """\
totem {
	version: 2
	cluster_name: debian
}

nodelist {
	node {
		nodeid: 1
		ring0_addr: 127.0.0.1
	}
	node {
		nodeid: 2
		ring0_addr: 127.0.0.2
	}
}
"""

PCS_REAL_NODES = """\
Corosync Nodes:
 Online: mail.nisaroti.my.id mail2.nisaroti.my.id
 Offline:
"""

# Operator-captured pcs status on mail.nisaroti.my.id after the Ansible stub
# fix was deployed but before this Python pre-flight knew about the stub.
LIVE_PCS_STATUS = """\
Cluster name: debian
1 node configured
0 resource instances configured

Node List: Online: [ mail.nisaroti.my.id ]

Daemon Status:
  corosync: active/enabled
  pacemaker: active/enabled
  pcsd: active/enabled
"""

LIVE_CRM_MON = """\
Cluster Summary:
  * Stack: corosync
  * Current DC: mail.nisaroti.my.id
  * 1 node configured
  * 0 resource instances configured

Node List:
  * Online: [ mail.nisaroti.my.id ]

No active resources
"""

PCS_KIN_MAIL_WITH_RESOURCES = """\
Cluster name: kin-mail
2 nodes configured
6 resource instances configured

Node List: Online: [ mail.nisaroti.my.id ]
"""


class DebianCorosyncStubTests(unittest.TestCase):
    def test_live_stub_name_commented_is_stub(self) -> None:
        self.assertTrue(is_debian_corosync_stub(LIVE_STUB_NAME_COMMENTED))

    def test_stub_with_example_node1_name_is_stub(self) -> None:
        self.assertTrue(is_debian_corosync_stub(STUB_NAME_NODE1))

    def test_stub_with_commented_rings_is_stub(self) -> None:
        self.assertTrue(is_debian_corosync_stub(STUB_RINGS_COMMENTED))

    def test_quoted_debian_cluster_name_is_stub(self) -> None:
        text = (
            "totem {\n"
            '  cluster_name: "debian"\n'
            "}\n"
            "nodelist {\n"
            "  node {\n"
            "    nodeid: 1\n"
            "    ring0_addr: 127.0.0.1\n"
            "  }\n"
            "}\n"
        )
        self.assertTrue(is_debian_corosync_stub(text))
        self.assertFalse(is_debian_corosync_stub(""))
        self.assertFalse(is_debian_corosync_stub(None))
        self.assertFalse(is_debian_corosync_stub("   # only comments\n"))

    def test_real_two_node_kin_mail_is_not_stub(self) -> None:
        self.assertFalse(is_debian_corosync_stub(REAL_TWO_NODE))

    def test_debian_name_with_lan_ip_is_not_stub(self) -> None:
        self.assertFalse(is_debian_corosync_stub(DEBIAN_NAME_WITH_LAN))

    def test_two_loopback_nodes_is_not_stub(self) -> None:
        self.assertFalse(is_debian_corosync_stub(TWO_LOOPBACK_NODES))

    def test_pcs_warning_text_is_not_a_config(self) -> None:
        self.assertFalse(is_debian_corosync_stub(PCS_STUB_NODES))
        self.assertFalse(is_debian_corosync_stub(PCS_REAL_NODES))

    def test_bytes_stub_is_accepted(self) -> None:
        self.assertTrue(is_debian_corosync_stub(LIVE_STUB_NAME_COMMENTED.encode()))

    def test_real_membership_would_still_match_expected_names(self) -> None:
        # The role refuses when running and a mail node name is absent from
        # `pcs status nodes corosync`. Stub output has no names; real output does.
        expected = ["mail.nisaroti.my.id", "mail2.nisaroti.my.id"]
        for name in expected:
            self.assertNotIn(name, PCS_STUB_NODES)
            self.assertIn(name, PCS_REAL_NODES)

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "cluster_setup"
            / "filter_plugins"
            / "corosync_stub.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_corosync_stub_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.is_debian_corosync_stub, is_debian_corosync_stub)
        self.assertTrue(mod.FilterModule().filters()["kin_is_debian_corosync_stub"](LIVE_STUB_NAME_COMMENTED))

    def test_resource_instance_count_from_live_pcs_and_crm(self) -> None:
        self.assertEqual(parse_resource_instance_count(LIVE_PCS_STATUS), 0)
        self.assertEqual(parse_resource_instance_count(LIVE_CRM_MON), 0)
        self.assertEqual(parse_resource_instance_count(PCS_KIN_MAIL_WITH_RESOURCES), 6)
        self.assertIsNone(parse_resource_instance_count(""))
        self.assertEqual(parse_pcs_cluster_name(LIVE_PCS_STATUS), "debian")
        self.assertEqual(parse_pcs_cluster_name(PCS_KIN_MAIL_WITH_RESOURCES), "kin-mail")

    def test_pcs_status_nodes_line_parses_uname_fallback(self) -> None:
        # Pacemaker reports the uname even when corosync.conf has no name:.
        self.assertEqual(
            parse_online_nodes(LIVE_PCS_STATUS),
            ["mail.nisaroti.my.id"],
        )

    def test_harmless_stub_matches_tonight_live_host(self) -> None:
        self.assertTrue(
            is_harmless_package_stub_cluster(
                LIVE_STUB_NAME_COMMENTED,
                status_text=LIVE_PCS_STATUS,
                live_nodes=["mail.nisaroti.my.id"],
            )
        )
        self.assertTrue(
            is_harmless_package_stub_cluster(
                LIVE_STUB_NAME_COMMENTED,
                status_text=LIVE_CRM_MON,
                live_nodes=["mail.nisaroti.my.id"],
            )
        )

    def test_harmless_stub_rejects_resources_or_extra_nodes(self) -> None:
        self.assertFalse(
            is_harmless_package_stub_cluster(
                LIVE_STUB_NAME_COMMENTED,
                status_text=PCS_KIN_MAIL_WITH_RESOURCES,
                live_nodes=["mail.nisaroti.my.id"],
            )
        )
        self.assertFalse(
            is_harmless_package_stub_cluster(
                LIVE_STUB_NAME_COMMENTED,
                status_text=LIVE_PCS_STATUS,
                live_nodes=["mail.nisaroti.my.id", "mail2.nisaroti.my.id"],
            )
        )
        self.assertFalse(
            is_harmless_package_stub_cluster(
                REAL_TWO_NODE,
                status_text=LIVE_CRM_MON,
                live_nodes=["mail.nisaroti.my.id"],
            )
        )

    def test_harmless_stub_accepts_empty_cib_and_rejects_real_cib(self) -> None:
        from tests.test_pacemaker_cib import EMPTY_SKELETON_CIB, REAL_CIB_WITH_RESOURCES

        self.assertTrue(
            is_harmless_package_stub_cluster(
                LIVE_STUB_NAME_COMMENTED,
                status_text=LIVE_PCS_STATUS,
                live_nodes=["mail.nisaroti.my.id"],
                cib_text=EMPTY_SKELETON_CIB,
            )
        )
        self.assertFalse(
            is_harmless_package_stub_cluster(
                LIVE_STUB_NAME_COMMENTED,
                status_text=LIVE_PCS_STATUS,
                live_nodes=["mail.nisaroti.my.id"],
                cib_text=REAL_CIB_WITH_RESOURCES,
            )
        )

    def test_apply_gate_lets_tonight_stub_through(self) -> None:
        kwargs = dict(
            join_mode="apply",
            live_nodes=["mail.nisaroti.my.id"],
            peer_name="mail2.nisaroti.my.id",
            peer_ip="10.10.40.52",
            corosync_conf=LIVE_STUB_NAME_COMMENTED,
        )
        self.assertFalse(
            live_cluster_blocks_apply(status_text=LIVE_PCS_STATUS, **kwargs)
        )
        # orchestration.py passes gather_status()['raw']['crm'], not pcs status.
        self.assertFalse(
            live_cluster_blocks_apply(status_text=LIVE_CRM_MON, **kwargs)
        )

    def test_apply_gate_refuses_real_cluster_wrong_peer(self) -> None:
        self.assertTrue(
            live_cluster_blocks_apply(
                join_mode="apply",
                live_nodes=["mail.nisaroti.my.id"],
                peer_name="mail-wrong.example.test",
                peer_ip="192.0.2.99",
                corosync_conf=REAL_TWO_NODE,
                status_text=PCS_KIN_MAIL_WITH_RESOURCES,
            )
        )

    def test_apply_gate_refuses_stub_conf_when_resources_exist(self) -> None:
        self.assertTrue(
            live_cluster_blocks_apply(
                join_mode="apply",
                live_nodes=["mail.nisaroti.my.id"],
                peer_name="mail2.nisaroti.my.id",
                peer_ip="10.10.40.52",
                corosync_conf=LIVE_STUB_NAME_COMMENTED,
                status_text=PCS_KIN_MAIL_WITH_RESOURCES,
            )
        )

    def test_apply_gate_allows_when_peer_already_in_ring(self) -> None:
        self.assertFalse(
            live_cluster_blocks_apply(
                join_mode="apply",
                live_nodes=["mail.nisaroti.my.id"],
                peer_name="mail2.nisaroti.my.id",
                peer_ip="10.10.40.14",
                corosync_conf=REAL_TWO_NODE,
                status_text=PCS_KIN_MAIL_WITH_RESOURCES,
            )
        )

    def test_apply_gate_skips_when_no_live_nodes_or_check_mode(self) -> None:
        self.assertFalse(
            live_cluster_blocks_apply(
                join_mode="apply",
                live_nodes=[],
                peer_name="mail2.nisaroti.my.id",
                peer_ip="10.10.40.52",
                corosync_conf=LIVE_STUB_NAME_COMMENTED,
                status_text=LIVE_PCS_STATUS,
            )
        )
        self.assertFalse(
            live_cluster_blocks_apply(
                join_mode="check",
                live_nodes=["mail.nisaroti.my.id"],
                peer_name="mail2.nisaroti.my.id",
                peer_ip="10.10.40.52",
                corosync_conf=REAL_TWO_NODE,
                status_text=PCS_KIN_MAIL_WITH_RESOURCES,
            )
        )


if __name__ == "__main__":
    unittest.main()
