"""Empty Pacemaker CIB skeleton detection (no live Pacemaker)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.pacemaker_cib import (
    is_empty_skeleton_cib,
    is_throwaway_package_cib,
)


# Shape matches the ~17-line package-first-start cib.xml observed live on
# mail.nisaroti.my.id / mail2.nisaroti.my.id before pcs cluster destroy
# (2026-08-21): empty nodes/resources/constraints, empty status.
EMPTY_SKELETON_CIB = """\
<?xml version="1.0"?>
<cib crm_feature_set="3.0.14" validate-with="pacemaker-3.5" epoch="0" \
num_updates="0" admin_epoch="0" \
cib-last-written="Fri Aug 21 01:00:00 2026" update-origin="localhost" \
update-client="cibadmin" update-user="hacluster" have-quorum="0" dc-uuid="0">
  <configuration>
    <crm_config>
      <cluster_property_set id="cib-bootstrap-options">
        <nvpair id="cib-bootstrap-options-have-watchdog" name="have-watchdog" value="false"/>
        <nvpair id="cib-bootstrap-options-dc-version" name="dc-version" value="2.1.2-4"/>
        <nvpair id="cib-bootstrap-options-cluster-infrastructure" name="cluster-infrastructure" value="corosync"/>
      </cluster_property_set>
    </crm_config>
    <nodes/>
    <resources/>
    <constraints/>
  </configuration>
  <status/>
</cib>
"""

# Same skeleton with self-closing sections expanded to empty pairs.
EMPTY_SKELETON_EXPANDED = """\
<cib epoch="0" num_updates="0" admin_epoch="0">
  <configuration>
    <crm_config/>
    <nodes></nodes>
    <resources></resources>
    <constraints></constraints>
  </configuration>
  <status></status>
</cib>
"""

REAL_CIB_WITH_RESOURCES = """\
<?xml version="1.0"?>
<cib crm_feature_set="3.0.14" validate-with="pacemaker-3.5" epoch="42" \
num_updates="8" admin_epoch="0" have-quorum="1" dc-uuid="1">
  <configuration>
    <crm_config>
      <cluster_property_set id="cib-bootstrap-options">
        <nvpair id="cib-bootstrap-options-cluster-name" name="cluster-name" value="kin-mail"/>
        <nvpair id="cib-bootstrap-options-stonith-enabled" name="stonith-enabled" value="true"/>
      </cluster_property_set>
    </crm_config>
    <nodes>
      <node id="1" uname="mail.example.test"/>
      <node id="2" uname="mail2.example.test"/>
    </nodes>
    <resources>
      <primitive class="ocf" id="ClusterIP" provider="heartbeat" type="IPaddr2">
        <instance_attributes id="ClusterIP-instance_attributes">
          <nvpair id="ClusterIP-instance_attributes-ip" name="ip" value="192.0.2.50"/>
        </instance_attributes>
      </primitive>
    </resources>
    <constraints/>
  </configuration>
  <status>
    <node_state id="1" uname="mail.example.test" in_ccm="true" crmd="online" join="member" expected="member">
      <lrm id="1">
        <lrm_resources/>
      </lrm>
    </node_state>
  </status>
</cib>
"""

# Nodes registered but no resources yet: still not a throwaway skeleton.
NODES_ONLY_CIB = """\
<cib epoch="1" num_updates="1" admin_epoch="0">
  <configuration>
    <crm_config/>
    <nodes>
      <node id="1" uname="mail.example.test"/>
    </nodes>
    <resources/>
    <constraints/>
  </configuration>
  <status/>
</cib>
"""

# Empty configuration sections but status records a prior join.
STATUS_HISTORY_ONLY = """\
<cib epoch="0" num_updates="0" admin_epoch="0">
  <configuration>
    <crm_config/>
    <nodes/>
    <resources/>
    <constraints/>
  </configuration>
  <status>
    <node_state id="1" uname="mail.example.test" in_ccm="false" crmd="offline"/>
  </status>
</cib>
"""

CONSTRAINTS_ONLY = """\
<cib epoch="1" num_updates="1" admin_epoch="0">
  <configuration>
    <crm_config/>
    <nodes/>
    <resources/>
    <constraints>
      <rsc_location id="cli-prefer-ClusterIP" rsc="ClusterIP" node="mail.example.test" score="INFINITY"/>
    </constraints>
  </configuration>
  <status/>
</cib>
"""

# Shape matches Ubuntu 22.04 after corosync.deb auto-starts Pacemaker on the
# package stub (cluster_name debian): local hostname joins, status records
# that member, no resources. log-23.txt classified this as "not an empty
# skeleton" and then still ran pcs cluster setup, which refused leftover CIB.
DEBIAN_STUB_FIRST_START_CIB = """\
<?xml version="1.0"?>
<cib crm_feature_set="3.0.14" validate-with="pacemaker-3.5" epoch="5" \
num_updates="4" admin_epoch="0" have-quorum="1" dc-uuid="1">
  <configuration>
    <crm_config>
      <cluster_property_set id="cib-bootstrap-options">
        <nvpair id="cib-bootstrap-options-have-watchdog" name="have-watchdog" value="false"/>
        <nvpair id="cib-bootstrap-options-dc-version" name="dc-version" value="2.1.2-4"/>
        <nvpair id="cib-bootstrap-options-cluster-infrastructure" name="cluster-infrastructure" value="corosync"/>
        <nvpair id="cib-bootstrap-options-cluster-name" name="cluster-name" value="debian"/>
        <nvpair id="cib-bootstrap-options-stonith-enabled" name="stonith-enabled" value="true"/>
      </cluster_property_set>
    </crm_config>
    <nodes>
      <node id="1" uname="mail.example.test"/>
    </nodes>
    <resources/>
    <constraints/>
  </configuration>
  <status>
    <node_state id="1" uname="mail.example.test" in_ccm="true" crmd="online" join="member" expected="member">
      <lrm id="1">
        <lrm_resources/>
      </lrm>
    </node_state>
  </status>
</cib>
"""

# pcs cluster setup wrote kin-mail + both nodes, then failed before resources.
# Must not be treated as throwaway leftover.
KIN_MAIL_NODES_ONLY_CIB = """\
<cib epoch="1" num_updates="1" admin_epoch="0">
  <configuration>
    <crm_config>
      <cluster_property_set id="cib-bootstrap-options">
        <nvpair id="cib-bootstrap-options-cluster-name" name="cluster-name" value="kin-mail"/>
      </cluster_property_set>
    </crm_config>
    <nodes>
      <node id="1" uname="mail.example.test"/>
      <node id="2" uname="mail2.example.test"/>
    </nodes>
    <resources/>
    <constraints/>
  </configuration>
  <status/>
</cib>
"""


class EmptySkeletonCibTests(unittest.TestCase):
    def test_live_style_empty_skeleton_is_empty(self) -> None:
        self.assertTrue(is_empty_skeleton_cib(EMPTY_SKELETON_CIB))

    def test_expanded_empty_sections_are_empty(self) -> None:
        self.assertTrue(is_empty_skeleton_cib(EMPTY_SKELETON_EXPANDED))

    def test_bytes_skeleton_is_accepted(self) -> None:
        self.assertTrue(is_empty_skeleton_cib(EMPTY_SKELETON_CIB.encode()))

    def test_real_cib_with_resources_is_not_empty(self) -> None:
        self.assertFalse(is_empty_skeleton_cib(REAL_CIB_WITH_RESOURCES))

    def test_nodes_only_is_not_empty(self) -> None:
        self.assertFalse(is_empty_skeleton_cib(NODES_ONLY_CIB))

    def test_status_history_is_not_empty(self) -> None:
        self.assertFalse(is_empty_skeleton_cib(STATUS_HISTORY_ONLY))

    def test_constraints_only_is_not_empty(self) -> None:
        self.assertFalse(is_empty_skeleton_cib(CONSTRAINTS_ONLY))

    def test_debian_stub_first_start_is_not_an_empty_skeleton(self) -> None:
        self.assertFalse(is_empty_skeleton_cib(DEBIAN_STUB_FIRST_START_CIB))
        self.assertFalse(is_empty_skeleton_cib(NODES_ONLY_CIB))
        self.assertFalse(is_empty_skeleton_cib(STATUS_HISTORY_ONLY))

    def test_throwaway_accepts_package_first_start_with_local_member(self) -> None:
        self.assertTrue(is_throwaway_package_cib(EMPTY_SKELETON_CIB))
        self.assertTrue(is_throwaway_package_cib(EMPTY_SKELETON_EXPANDED))
        self.assertTrue(is_throwaway_package_cib(DEBIAN_STUB_FIRST_START_CIB))
        self.assertTrue(is_throwaway_package_cib(NODES_ONLY_CIB))
        self.assertTrue(is_throwaway_package_cib(STATUS_HISTORY_ONLY))
        self.assertTrue(is_throwaway_package_cib(DEBIAN_STUB_FIRST_START_CIB.encode()))

    def test_throwaway_rejects_real_or_reserved_cluster(self) -> None:
        self.assertFalse(is_throwaway_package_cib(REAL_CIB_WITH_RESOURCES))
        self.assertFalse(is_throwaway_package_cib(CONSTRAINTS_ONLY))
        self.assertFalse(is_throwaway_package_cib(KIN_MAIL_NODES_ONLY_CIB))
        self.assertFalse(is_throwaway_package_cib(KIN_MAIL_NODES_ONLY_CIB, "kin-mail"))

    def test_throwaway_respects_reserved_cluster_name(self) -> None:
        self.assertFalse(is_throwaway_package_cib(DEBIAN_STUB_FIRST_START_CIB, "debian"))
        self.assertTrue(is_throwaway_package_cib(DEBIAN_STUB_FIRST_START_CIB, "kin-mail"))

    def test_throwaway_fail_closed_on_missing_or_invalid(self) -> None:
        self.assertFalse(is_throwaway_package_cib(""))
        self.assertFalse(is_throwaway_package_cib(None))
        self.assertFalse(is_throwaway_package_cib("   "))
        self.assertFalse(is_throwaway_package_cib("<not-cib/>"))
        self.assertFalse(is_throwaway_package_cib("<cib><configuration/></cib>"))
        self.assertFalse(is_throwaway_package_cib("not xml at all"))

    def test_missing_or_invalid_is_fail_closed(self) -> None:
        self.assertFalse(is_empty_skeleton_cib(""))
        self.assertFalse(is_empty_skeleton_cib(None))
        self.assertFalse(is_empty_skeleton_cib("   "))
        self.assertFalse(is_empty_skeleton_cib("<not-cib/>"))
        self.assertFalse(is_empty_skeleton_cib("<cib><configuration/></cib>"))
        self.assertFalse(is_empty_skeleton_cib("not xml at all"))

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "cluster_setup"
            / "filter_plugins"
            / "pacemaker_cib.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_pacemaker_cib_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.is_empty_skeleton_cib, is_empty_skeleton_cib)
        self.assertIs(mod.is_throwaway_package_cib, is_throwaway_package_cib)
        filters = mod.FilterModule().filters()
        self.assertTrue(filters["kin_is_empty_skeleton_cib"](EMPTY_SKELETON_CIB))
        self.assertTrue(
            filters["kin_is_throwaway_package_cib"](
                DEBIAN_STUB_FIRST_START_CIB, "kin-mail"
            )
        )
        self.assertFalse(
            filters["kin_is_throwaway_package_cib"](REAL_CIB_WITH_RESOURCES)
        )


if __name__ == "__main__":
    unittest.main()
