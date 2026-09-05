"""The bind DN built from a username must escape it as a DN component.

The search path escapes its LDAP filter with escape_filter_chars. The template
path built a DN by raw string substitution, so a username carrying DN
metacharacters ( , + = " \\ < > ; ) changed the structure of the DN rather than
the value inside it.

It is not an authentication bypass on its own - whatever DN comes out, the
password still has to be right for it - but a filter is escaped and a DN is
not, in the same function, and that asymmetry is the kind that gets worse.

The other common template is a UPN (`%u@corp.example.test`), which is not a DN
component at all and where RFC 4514 escaping would corrupt a legitimate
username containing '+'. Escaping is applied only when the template is a DN.
"""

from __future__ import annotations

import unittest
from unittest import mock

import ldap3

from kin_console import ad_auth
from kin_console.ad_settings import AdSettings


class _Captured(Exception):
    """Raised by the stub to stop before any network I/O."""


def _settings(template: str) -> AdSettings:
    return AdSettings(
        enabled=True,
        ldap_url="ldaps://dc.example.test",
        search_base="dc=example,dc=test",
        search_filter="(sAMAccountName=%u)",
        search_bind_dn="",
        search_bind_password="",
        bind_dn_template=template,
    )


class BindDnConstruction(unittest.TestCase):
    def _bind_dn_for(self, template: str, username: str) -> str:
        seen: dict[str, str] = {}

        def fake_connection(server, user=None, password=None, **kw):  # noqa: ANN001
            seen["user"] = user
            raise _Captured()

        with mock.patch.object(ldap3, "Connection", fake_connection), \
             mock.patch.object(ldap3, "Server", lambda *a, **k: object()):
            try:
                ad_auth.verify_ad_password(username, "irrelevant", _settings(template))
            except _Captured:
                pass
        self.assertIn("user", seen, "the stub was never reached")
        return seen["user"]

    def test_an_ordinary_username_is_unchanged(self) -> None:
        dn = self._bind_dn_for("cn=%u,ou=Users,dc=example,dc=test", "alice")
        self.assertEqual(dn, "cn=alice,ou=Users,dc=example,dc=test")

    def test_a_dotted_username_is_unchanged(self) -> None:
        dn = self._bind_dn_for("cn=%u,ou=Users,dc=example,dc=test", "first.last")
        self.assertEqual(dn, "cn=first.last,ou=Users,dc=example,dc=test")

    def test_dn_metacharacters_cannot_restructure_the_dn(self) -> None:
        dn = self._bind_dn_for(
            "cn=%u,ou=Users,dc=example,dc=test", "evil,ou=Admins,dc=example,dc=test"
        )
        # The injected commas and equals must survive as escaped VALUE text,
        # so the DN still has exactly one cn= component.
        self.assertTrue(dn.startswith("cn=evil\\,"), dn)
        self.assertEqual(dn.count("ou=Users"), 1, dn)
        # Everything the attacker supplied is inside the first RDN.
        first_rdn = dn.split(",ou=Users", 1)[0]
        self.assertIn("\\,ou\\=Admins", first_rdn)

    def test_each_escapable_character_is_escaped(self) -> None:
        for ch in (",", "+", '"', "\\", "<", ">", ";", "="):
            with self.subTest(ch=ch):
                dn = self._bind_dn_for("cn=%u,dc=example,dc=test", f"a{ch}b")
                self.assertIn(f"a\\{ch}b", dn, dn)

    def test_a_upn_template_is_left_alone(self) -> None:
        # Not a DN. Escaping here would corrupt a legitimate username.
        dn = self._bind_dn_for("%u@corp.example.test", "first.last")
        self.assertEqual(dn, "first.last@corp.example.test")

    def test_a_upn_template_keeps_a_plus_in_the_username(self) -> None:
        dn = self._bind_dn_for("%u@corp.example.test", "a+b")
        self.assertEqual(dn, "a+b@corp.example.test")

    def test_all_three_placeholders_are_substituted(self) -> None:
        dn = self._bind_dn_for("cn=%u+%n+%j,dc=example,dc=test", "bob")
        self.assertEqual(dn, "cn=bob+bob+bob,dc=example,dc=test")


class RefusalsBeforeAnyBind(unittest.TestCase):
    """Things that must never reach the directory at all."""

    def test_an_empty_password_is_refused_without_binding(self) -> None:
        # An empty password is an anonymous bind on many directories, and an
        # anonymous bind succeeds. This must never get that far.
        def explode(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("connected with an empty password")

        with mock.patch.object(ldap3, "Connection", explode):
            for pwd in ("", None):
                res = ad_auth.verify_ad_password(
                    "alice", pwd, _settings("cn=%u,dc=example,dc=test")  # type: ignore[arg-type]
                )
                self.assertFalse(res.ok)
                self.assertEqual(res.reason, "invalid_credentials")

    def test_an_empty_username_is_refused(self) -> None:
        res = ad_auth.verify_ad_password("  ", "pw", _settings("cn=%u,dc=example,dc=test"))
        self.assertFalse(res.ok)

    def test_cleartext_ldap_without_starttls_is_refused(self) -> None:
        cfg = _settings("cn=%u,dc=example,dc=test")
        cfg = AdSettings(**{**cfg.__dict__, "ldap_url": "ldap2://dc.example.test"})
        res = ad_auth.verify_ad_password("alice", "pw", cfg)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "misconfigured")
        self.assertIn("ldaps://", res.detail)

    def test_a_disabled_appliance_never_contacts_the_directory(self) -> None:
        def explode(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("contacted AD while disabled")

        cfg = _settings("cn=%u,dc=example,dc=test")
        cfg = AdSettings(**{**cfg.__dict__, "enabled": False})
        with mock.patch.object(ldap3, "Connection", explode):
            res = ad_auth.verify_ad_password("alice", "pw", cfg)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "not_enabled")


class ErrorTextNeverCarriesASecret(unittest.TestCase):
    def test_a_leaked_password_is_redacted(self) -> None:
        for text in (
            "bind failed password=hunter2",
            "PASSWORD: hunter2 rejected",
            "pwd=hunter2",
            "passwd = hunter2",
        ):
            with self.subTest(text=text):
                out = ad_auth._safe_detail(RuntimeError(text))
                self.assertNotIn("hunter2", out)
                self.assertIn("***", out)

    def test_detail_is_bounded(self) -> None:
        out = ad_auth._safe_detail(RuntimeError("x" * 5000))
        self.assertLessEqual(len(out), 240)


if __name__ == "__main__":
    unittest.main()
