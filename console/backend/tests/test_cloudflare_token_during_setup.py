"""The one TLS choice that renews unattended could not be completed on a new box.

27 September 2026, step 4 of the wizard on a freshly built appliance. The
operator picked "Automatic (Cloudflare DNS)", created a token scoped to the one
zone, pasted it, pressed Store token, and got:

    Not authenticated

Nothing was wrong with the token. /api/settings/cloudflare-token required a
Super Admin session, and the wizard before deploy runs anonymously - that is
what "first boot, nobody has an account yet" means, and every other step on
that page uses it. So the only method that issues and renews with nobody
present was the only one that could not be configured, and the fallback is
Manual: a deploy that stops for 25 minutes and, if the TXT record is missed,
finishes on a self-signed certificate. The same operator had already been
through that once.

The fix is the door every other wizard step uses, and it shuts by itself: once
mail is deployed, the anonymous identity is refused everywhere.

What is deliberately NOT opened: the rest of appliance settings. Seats, AD,
firewall and the licence are post-deploy administration. Letting the whole
command through to pass one field would be the kind of convenience that is
only noticed when somebody abuses it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kin_privhelper import deploy_state as ds
from kin_privhelper import protocol as proto
from kin_privhelper.daemon import _authorize

SETUP = ds.SETUP_USERNAME


class BeforeDeployTheWizardCanStoreTheToken(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(ds, "is_mail_deployed", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_token_section_is_allowed(self) -> None:
        role, deny = _authorize(
            SETUP, proto.CMD_APPLY_APPLIANCE_SETTINGS, {"section": "cloudflare_token"}
        )
        self.assertIsNone(deny)
        self.assertIsNotNone(role)

    def test_every_other_section_is_refused(self) -> None:
        for section in ("seats", "ad", "firewall", "license", "branding", ""):
            with self.subTest(section=section):
                role, deny = _authorize(
                    SETUP, proto.CMD_APPLY_APPLIANCE_SETTINGS, {"section": section}
                )
                self.assertIsNone(role, f"anonymous setup could write {section!r}")
                self.assertEqual(deny, "denied_setup_settings_section")

    def test_a_missing_section_is_refused(self) -> None:
        # Absent is not "the harmless one" - it must not fall through to a
        # handler that picks its own default.
        role, deny = _authorize(SETUP, proto.CMD_APPLY_APPLIANCE_SETTINGS, {})
        self.assertIsNone(role)
        self.assertEqual(deny, "denied_setup_settings_section")
        role, deny = _authorize(SETUP, proto.CMD_APPLY_APPLIANCE_SETTINGS, None)
        self.assertIsNone(role)

    def test_the_rest_of_the_wizard_still_works(self) -> None:
        for cmd in ("run_full_install", "apply_wizard_draft", "get_status"):
            with self.subTest(cmd=cmd):
                role, deny = _authorize(SETUP, cmd, {})
                self.assertIsNone(deny)
                self.assertIsNotNone(role)

    def test_a_command_outside_the_whitelist_is_still_refused(self) -> None:
        role, deny = _authorize(SETUP, "create_mailbox", {})
        self.assertIsNone(role)
        self.assertEqual(deny, "denied_setup_cmd")


class AfterDeployTheAnonymousIdentityIsClosed(unittest.TestCase):
    """The window is the point. It must not stay open once there are accounts."""

    def test_even_the_token_section_is_refused(self) -> None:
        with patch.object(ds, "is_mail_deployed", return_value=True):
            role, deny = _authorize(
                SETUP,
                proto.CMD_APPLY_APPLIANCE_SETTINGS,
                {"section": "cloudflare_token"},
            )
            self.assertIsNone(role)
            self.assertEqual(deny, "denied_setup_after_deploy")


class TheCommandIsOnTheWizardWhitelist(unittest.TestCase):
    def test_listed(self) -> None:
        # Without this the section check above is never reached: the command
        # name is rejected first, and the symptom is the same "Not
        # authenticated" the operator saw.
        self.assertIn(proto.CMD_APPLY_APPLIANCE_SETTINGS, ds.SETUP_ALLOWED_COMMANDS)


if __name__ == "__main__":
    unittest.main()
