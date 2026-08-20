"""Debian/Ubuntu corosync package-stub detection (no live corosync)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_FILTER_DIR = (
    Path(__file__).resolve().parents[3]
    / "ansible"
    / "roles"
    / "cluster_setup"
    / "filter_plugins"
)
sys.path.insert(0, str(_FILTER_DIR))

from corosync_stub import is_debian_corosync_stub  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
