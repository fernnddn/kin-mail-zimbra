"""pcs constraint listing parsers (no live Pacemaker)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.pcs_constraints import (
    listing_has_promote_then_start,
    listing_has_promoted_colocation,
)

CLONE = "kin-drbd-clone"
GROUP = "kin-mail-svc"

# pcs 0.10 / ClusterLabs Clusters from Scratch style.
PCS_0_10 = """\
Location Constraints:
Ordering Constraints:
  promote kin-drbd-clone then start kin-mail-svc
Colocation Constraints:
  kin-mail-svc with kin-drbd-clone (with-rsc-role:Promoted)
"""

# Same shape as this lab's location lines in docs/progress/2.14.
PCS_QUOTED = """\
Location Constraints:
  resource 'kin-drbd-clone' prefers node 'mail.gits-it.site' with score 50
Ordering Constraints:
  promote resource 'kin-drbd-clone' then start resource 'kin-mail-svc'
Colocation Constraints:
  resource 'kin-mail-svc' with resource 'kin-drbd-clone' (rsc-role:Started) (with-rsc-role:Promoted)
"""

EMPTY = """\
Location Constraints:
Ordering Constraints:
Colocation Constraints:
"""


class PcsConstraintListingTests(unittest.TestCase):
    def test_pcs_0_10_order_and_colocation(self) -> None:
        self.assertTrue(listing_has_promote_then_start(PCS_0_10, CLONE, GROUP))
        self.assertTrue(listing_has_promoted_colocation(PCS_0_10, GROUP, CLONE))

    def test_quoted_pcs_order_and_colocation(self) -> None:
        self.assertTrue(listing_has_promote_then_start(PCS_QUOTED, CLONE, GROUP))
        self.assertTrue(listing_has_promoted_colocation(PCS_QUOTED, GROUP, CLONE))

    def test_empty_listing_needs_adds(self) -> None:
        self.assertFalse(listing_has_promote_then_start(EMPTY, CLONE, GROUP))
        self.assertFalse(listing_has_promoted_colocation(EMPTY, GROUP, CLONE))

    def test_order_line_is_not_colocation(self) -> None:
        only_order = "  promote kin-drbd-clone then start kin-mail-svc\n"
        self.assertTrue(listing_has_promote_then_start(only_order, CLONE, GROUP))
        self.assertFalse(listing_has_promoted_colocation(only_order, GROUP, CLONE))

    def test_prefer_line_is_not_colocation(self) -> None:
        prefer = "  resource 'kin-drbd-clone' prefers node 'mail.example.test' with score 50\n"
        self.assertFalse(listing_has_promoted_colocation(prefer, GROUP, CLONE))

    def test_stale_promote_resource_substring_is_not_required(self) -> None:
        # The old when: looked for the literal 'promote resource', which pcs 0.10
        # listings do not contain, so every apply re-ran constraint add.
        self.assertNotIn("promote resource", PCS_0_10)
        self.assertTrue(listing_has_promote_then_start(PCS_0_10, CLONE, GROUP))

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "pacemaker_mail_stack"
            / "filter_plugins"
            / "pcs_constraints.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_pcs_constraints_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.listing_has_promote_then_start, listing_has_promote_then_start)
        filters = mod.FilterModule().filters()
        self.assertTrue(filters["kin_pcs_has_promote_then_start"](PCS_0_10, CLONE, GROUP))
        self.assertTrue(filters["kin_pcs_has_promoted_colocation"](PCS_QUOTED, GROUP, CLONE))

    def test_role_uses_the_filters(self) -> None:
        tasks = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "pacemaker_mail_stack"
            / "tasks"
            / "constraints.yml"
        )
        text = tasks.read_text(encoding="utf-8")
        self.assertIn("kin_pcs_has_promoted_colocation", text)
        self.assertIn("kin_pcs_has_promote_then_start", text)
        self.assertNotIn("'promote resource'", text)

    def test_skip_tags_stay_join_check_only(self) -> None:
        # Applying skip_tags on join_mode=apply would skip qdevice
        # configure/service/verify on a real apply. cluster_join steps never
        # run in check, so their skip_tags are unused by design.
        orch = (
            Path(__file__).resolve().parents[3]
            / "console"
            / "backend"
            / "kin_privhelper"
            / "orchestration.py"
        )
        text = orch.read_text(encoding="utf-8")
        self.assertIn(
            'skip = list(step.skip_tags) if join_mode == "check" else []',
            text,
        )


if __name__ == "__main__":
    unittest.main()
