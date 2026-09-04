"""pcs property config listing parsers (no live Pacemaker)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.pcs_properties import listing_has_property, parse_stonith_enabled

# Ubuntu 22.04 pcs 0.10 default text listing.
PCS_0_10 = """\
Cluster Properties:
 cluster-infrastructure: corosync
 cluster-name: kin-mail
 have-watchdog: true
 stonith-enabled: true
 stonith-timeout: 150s
"""

PCS_EQUALS = """\
Cluster Properties:
stonith-enabled=true
stonith-timeout=150
"""

PCS_DISABLED = """\
Cluster Properties:
 stonith-enabled: false
 stonith-timeout: 60s
"""


class PcsPropertyListingTests(unittest.TestCase):
    def test_pcs_0_10_colon_listing(self) -> None:
        self.assertTrue(listing_has_property(PCS_0_10, "stonith-enabled", "true"))
        self.assertTrue(listing_has_property(PCS_0_10, "stonith-timeout", "150s", "150"))
        self.assertFalse(listing_has_property(PCS_0_10, "stonith-enabled", "false"))

    def test_equals_listing(self) -> None:
        self.assertTrue(listing_has_property(PCS_EQUALS, "stonith-enabled", "true"))
        self.assertTrue(listing_has_property(PCS_EQUALS, "stonith-timeout", "150s", "150"))

    def test_equals_only_gate_would_refuse_jammy(self) -> None:
        self.assertNotIn("stonith-enabled=true", PCS_0_10)
        self.assertTrue(listing_has_property(PCS_0_10, "stonith-enabled", "true"))

    def test_disabled_is_not_enabled(self) -> None:
        self.assertFalse(listing_has_property(PCS_DISABLED, "stonith-enabled", "true"))

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "sbd_stonith"
            / "filter_plugins"
            / "pcs_properties.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_pcs_properties_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.listing_has_property, listing_has_property)
        self.assertTrue(
            mod.FilterModule().filters()["kin_pcs_has_property"](
                PCS_0_10, "stonith-enabled", "true"
            )
        )

    def test_role_uses_the_filter(self) -> None:
        tasks = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "sbd_stonith"
            / "tasks"
            / "verify.yml"
        )
        text = tasks.read_text(encoding="utf-8")
        self.assertIn("kin_pcs_has_property", text)
        self.assertNotIn("stonith-enabled=true", text)


if __name__ == "__main__":
    unittest.main()


class StonithEnabledTests(unittest.TestCase):
    """Nothing read stonith-enabled until now.

    Remove Observability disables fencing as its first action. A disarm that
    failed part-way, or a troubleshooting override nobody undid, left a
    two-node DRBD cluster with no fencing - the thing that stops two replicas
    diverging - and no indication of it anywhere in the console.
    """

    def test_reads_both_pcs_spellings(self) -> None:
        self.assertIs(
            parse_stonith_enabled("Cluster Properties:\n stonith-enabled: true\n"), True
        )
        self.assertIs(parse_stonith_enabled("stonith-enabled=true"), True)
        self.assertIs(
            parse_stonith_enabled("Cluster Properties:\n stonith-enabled: false\n"), False
        )
        self.assertIs(parse_stonith_enabled("stonith-enabled=false"), False)

    def test_case_and_spacing_do_not_matter(self) -> None:
        self.assertIs(parse_stonith_enabled("  stonith-enabled : TRUE  "), True)
        self.assertIs(parse_stonith_enabled("STONITH-ENABLED: False"), False)

    def test_unreadable_is_none_not_false(self) -> None:
        # A failed `pcs property` capture returns "". Reporting that as
        # "fencing is off" would raise the one alarm that means the operator's
        # data can diverge, on a cluster that is fine.
        self.assertIsNone(parse_stonith_enabled(""))
        self.assertIsNone(parse_stonith_enabled("no-quorum-policy: stop\n"))
        self.assertIsNone(parse_stonith_enabled("stonith-enabled: maybe"))

    def test_a_similarly_named_property_is_not_confused_for_it(self) -> None:
        self.assertIsNone(
            parse_stonith_enabled("stonith-enabled-extra: false\nstonith-action: reboot\n")
        )

    def test_the_snapshot_exposes_it(self) -> None:
        # The parser is useless if gather_status never puts it in the payload
        # and the API never forwards it; both were the actual gap.
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper"
            / "maintenance.py"
        ).read_text(encoding="utf-8")
        self.assertIn("parse_stonith_enabled(props_text)", src)
        # In the status dict gather_status builds...
        self.assertIn('"fencing_enabled": fencing_enabled,', src)
        # ...and forwarded by the API layer, or the console never sees it.
        self.assertIn('"fencing_enabled": st.get("fencing_enabled"),', src)
