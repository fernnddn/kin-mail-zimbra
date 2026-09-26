"""A directory turns the seat count from a commercial note into a hard requirement.

QA, 26 September 2026: Active Directory Users and Computers showing six people,
beside the Zimbra admin console showing four accounts, none of them from the
directory. The same report arrived on 24 and 25 September. Every time, the
cause was one field left blank.

The AD sync creates a mailbox per person and each one spends a contracted seat,
so CONTRACTED_SEATS=PLACEHOLDER_UNSET refuses all of them. The deploy finishes,
authentication is configured correctly, the sweep timer is installed - and the
admin console lists nobody, so not one person can sign in.

The wizard invited exactly this: the field was labelled optional and its
placeholder read "Leave blank to set later". Nothing connected that choice to
an empty mailbox list forty minutes later. This is checked here as well as in
the wizard, because the wizard is not the only thing that writes a draft.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kin_privhelper.apply_config import validate_draft

EXISTING: dict[str, str] = {"SERVER_IP": "192.0.2.6", "NET_IFACE": "ens33"}


def draft(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "topology": "1vm",
        "mail_domain": "example.test",
        "mail_host": "mail.example.test",
        "admin_pass": "AdminPw12345",
        "tls_method": "manual",
        "host_root_pass": "RootPw12345",
        "kin_user_pass": "KinPw12345",
        "timezone": "Asia/Jakarta",
        "le_email": "admin@example.test",
        "ad_auth_enabled": True,
        "ad_ldap_url": "ldaps://dc.example.test:636",
        "ad_search_base": "DC=example,DC=test",
        "ad_search_bind_dn": "svc@example.test",
        "ad_search_bind_password": "BindPw12345",
        "ad_test_user": "someone",
        "ad_test_pass": "TestPw12345",
        "contracted_seats": "150",
    }
    base.update(over)
    return base


def seat_errors(**over: object) -> list[str]:
    return [e for e in validate_draft(draft(**over), dict(EXISTING)) if "seat" in e]


class SeatsAreRequiredWhenADIsOn(unittest.TestCase):
    def test_a_complete_draft_with_seats_is_accepted(self) -> None:
        self.assertEqual(validate_draft(draft(), dict(EXISTING)), [])

    def test_the_placeholder_is_refused(self) -> None:
        errs = seat_errors(contracted_seats="PLACEHOLDER_UNSET")
        self.assertTrue(errs, "an unset seat count was accepted with AD enabled")
        self.assertIn("sign in", errs[0])

    def test_blank_is_refused(self) -> None:
        self.assertTrue(seat_errors(contracted_seats=""))

    def test_a_missing_field_is_refused(self) -> None:
        d = draft()
        del d["contracted_seats"]
        errs = [e for e in validate_draft(d, dict(EXISTING)) if "seat" in e]
        self.assertTrue(errs, "a draft with no seat field at all was accepted")

    def test_zero_is_refused_because_it_creates_nobody(self) -> None:
        # Zero passes "is this a whole number" and then refuses every create,
        # which is the same empty admin console by a different route.
        self.assertTrue(seat_errors(contracted_seats="0"))

    def test_the_refusal_explains_the_consequence(self) -> None:
        # "contracted_seats is required" sends the operator to look for a
        # licence. Naming the consequence sends them to the field.
        msg = seat_errors(contracted_seats="PLACEHOLDER_UNSET")[0]
        self.assertIn("Active Directory", msg)
        self.assertIn("creates nobody", msg)


class SeatsStayOptionalWithoutADirectory(unittest.TestCase):
    """Mailboxes made by hand are a different trade.

    The commercial figure often is not known on the day the appliance is built,
    and the gate refusing new mailboxes until somebody sets it is a reasonable
    thing to defer. This must not start demanding a number from every deploy.
    """

    def test_the_placeholder_is_still_accepted(self) -> None:
        self.assertEqual(
            seat_errors(ad_auth_enabled=False, contracted_seats="PLACEHOLDER_UNSET"),
            [],
        )

    def test_blank_is_still_accepted(self) -> None:
        self.assertEqual(seat_errors(ad_auth_enabled=False, contracted_seats=""), [])

    def test_zero_is_still_accepted(self) -> None:
        self.assertEqual(seat_errors(ad_auth_enabled=False, contracted_seats="0"), [])

    def test_a_non_number_is_still_refused(self) -> None:
        self.assertTrue(seat_errors(ad_auth_enabled=False, contracted_seats="ten"))


if __name__ == "__main__":
    unittest.main()
