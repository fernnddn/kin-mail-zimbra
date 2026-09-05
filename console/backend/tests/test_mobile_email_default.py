"""Mobile email (ActiveSync) ships enabled.

It used to default to off. The stated reason was that a first-night Deploy
should not be able to fail on the Ondrej PHP PPA being unreachable - but
`run_full_install` already treats 07-zpush.sh as a soft failure ("mail install
continues. Re-run later"), so that risk was covered elsewhere and the only
thing the default bought was an appliance handed over without phone mail
unless somebody had opted in during the wizard.

The one place it must STILL be forced off is the peer install on mail B: Z-Push
lives on the primary's DRBD copy, and standing up a second one is part of how
an HA join failed after a good node-A deploy.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from kin_privhelper.apply_config import build_base_config, merge_draft

REPO = Path(__file__).resolve().parents[3]


def _base_draft(**over: object) -> dict[str, object]:
    draft: dict[str, object] = {
        "mail_host": "mail.example.test",
        "mail_domain": "example.test",
        "server_ip": "198.51.100.10",
        "net_iface": "eth0",
        "admin_email": "admin@example.test",
        "tls_method": "customer",
        "contracted_seats": "25",
    }
    draft.update(over)
    return draft


class DefaultIsOn(unittest.TestCase):
    def test_a_draft_that_never_mentions_mobile_gets_it_enabled(self) -> None:
        out = merge_draft(_base_draft(), {})
        self.assertEqual(out["ZPUSH_ENABLED"], "yes")

    def test_explicitly_enabling_is_honoured(self) -> None:
        out = merge_draft(_base_draft(zpush_enabled=True), {})
        self.assertEqual(out["ZPUSH_ENABLED"], "yes")

    def test_an_operator_who_opts_out_is_still_honoured(self) -> None:
        # A default is not a policy. Someone who chose Skip must get Skip.
        out = merge_draft(_base_draft(zpush_enabled=False), {})
        self.assertEqual(out["ZPUSH_ENABLED"], "no")

    def test_a_brand_new_appliance_config_has_it_on(self) -> None:
        try:
            base = build_base_config()
        except RuntimeError as exc:  # no global IPv4 in this sandbox
            self.skipTest(f"host network not detectable here: {exc}")
        self.assertEqual(base["ZPUSH_ENABLED"], "yes")


class PeerInstallStillForcesItOff(unittest.TestCase):
    def test_mail_b_never_stands_up_a_second_zpush(self) -> None:
        src = (
            REPO / "console/backend/kin_privhelper/apply_config.py"
        ).read_text(encoding="utf-8")
        # The override and the reason must travel together.
        self.assertIn('out["ZPUSH_ENABLED"] = "no"', src)
        self.assertIn("add a\n    # second Z-Push", src)


class DeployStillSurvivesAFailedZpush(unittest.TestCase):
    """The safety net that makes defaulting ON reasonable."""

    def test_a_zpush_failure_does_not_stop_the_install(self) -> None:
        driver = (REPO / "install/kin-mail.sh").read_text(encoding="utf-8")
        self.assertIn("07-zpush.sh failed - mail install continues", driver)
        self.assertIn("soft_failed_stages+=(\"07-zpush.sh\")", driver)

    def test_the_installer_also_defaults_it_on(self) -> None:
        # Console and shell installer must agree, or a config written by one
        # and read by the other flips behaviour.
        driver = (REPO / "install/kin-mail.sh").read_text(encoding="utf-8")
        self.assertIn('"${ZPUSH_ENABLED:-yes}"', driver)
        cfg = (REPO / "install/00-config.sh").read_text(encoding="utf-8")
        self.assertIn(': "${ZPUSH_ENABLED:=yes}"', cfg)


class WizardMatchesTheBackend(unittest.TestCase):
    def test_the_draft_default_is_true(self) -> None:
        types_ts = (
            REPO / "console/frontend/src/wizard/types.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("zpush_enabled: true,", types_ts)

    def test_the_step_no_longer_advertises_skip_as_the_default(self) -> None:
        step = (
            REPO / "console/frontend/src/wizard/steps/ZpushStep.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Skip is the\n        default", step)
        self.assertIn("on by default", step)

    def test_an_older_draft_with_no_value_shows_as_enabled(self) -> None:
        # A draft saved before this field existed has zpush_enabled undefined.
        # `draft.zpush_enabled ? ...` would render that as Skipped and then
        # SAVE Skipped when Continue was pressed, quietly opting the operator
        # out of the new default.
        step = (
            REPO / "console/frontend/src/wizard/steps/ZpushStep.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("draft.zpush_enabled !== false", step)
        review = (
            REPO / "console/frontend/src/wizard/steps/ReviewStep.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('draft.zpush_enabled !== false ? "Enabled"', review)


if __name__ == "__main__":
    unittest.main()
