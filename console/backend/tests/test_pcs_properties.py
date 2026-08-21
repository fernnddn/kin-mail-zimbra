"""pcs property config listing parsers (no live Pacemaker)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.pcs_properties import listing_has_property

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
