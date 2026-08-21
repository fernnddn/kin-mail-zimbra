"""Parse pcs constraint location --full ids (no live Pacemaker)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.pcs_location import location_ids_for_node

NODE = "mail2.example.test"

# pcs 0.10 --full
PCS_0_10_FULL = """
Location Constraints:
  Resource: kin-drbd-clone
    Enabled on:
      Node: mail2.example.test (score:50) (id:location-kin-drbd-clone-mail2.example.test)
  Resource: kin-mail-svc
    Enabled on:
      Node: mail.example.test (score:INFINITY) (id:location-kin-mail-svc-mail.example.test)
"""

# Quoted listing with --full ids, same shape as 2.14 prefers-node lines.
PCS_QUOTED_FULL = """
Location Constraints:
  resource 'kin-drbd-clone' prefers node 'mail2.example.test' with score 50 (id:location-kin-drbd-clone-mail2.example.test)
  resource 'kin-zimbra' prefers node 'mail.example.test' with score 100 (id:location-kin-zimbra-mail.example.test)
"""

PCS_BARE_ID = """
  resource 'kin-drbd-clone' prefers node 'mail2.example.test' with score 50 id: location-kin-drbd-clone-mail2.example.test
"""

REPO_ROOT = Path(__file__).resolve().parents[3]
REMOVE_HOST = REPO_ROOT / "ansible" / "roles" / "cluster_remove_host"


class PcsLocationIdTests(unittest.TestCase):
    def test_pcs_0_10_full_only_retired_node(self) -> None:
        self.assertEqual(
            location_ids_for_node(PCS_0_10_FULL, NODE),
            ["location-kin-drbd-clone-mail2.example.test"],
        )
        self.assertEqual(location_ids_for_node(PCS_0_10_FULL, "mail.example.test"), [
            "location-kin-mail-svc-mail.example.test",
        ])

    def test_quoted_full(self) -> None:
        self.assertEqual(
            location_ids_for_node(PCS_QUOTED_FULL, NODE),
            ["location-kin-drbd-clone-mail2.example.test"],
        )

    def test_bare_id_without_parens(self) -> None:
        self.assertEqual(
            location_ids_for_node(PCS_BARE_ID, NODE),
            ["location-kin-drbd-clone-mail2.example.test"],
        )

    def test_empty_and_unknown(self) -> None:
        self.assertEqual(location_ids_for_node("", NODE), [])
        self.assertEqual(location_ids_for_node(PCS_QUOTED_FULL, "other.example.test"), [])

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = REMOVE_HOST / "filter_plugins" / "pcs_location.py"
        spec = importlib.util.spec_from_file_location("ansible_pcs_location_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.location_ids_for_node, location_ids_for_node)
        filters = mod.FilterModule().filters()
        self.assertEqual(
            filters["kin_location_ids_for_node"](PCS_QUOTED_FULL, NODE),
            ["location-kin-drbd-clone-mail2.example.test"],
        )

    def test_role_uses_the_filter(self) -> None:
        tasks = (REMOVE_HOST / "tasks" / "cleanup_constraints.yml").read_text(encoding="utf-8")
        self.assertIn("kin_location_ids_for_node", tasks)
        self.assertNotIn("KIN_LOC_OUT", tasks)


if __name__ == "__main__":
    unittest.main()
