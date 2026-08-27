"""Directory sign-in has to work for BOTH the console and Zimbra.

Operator, Phase 6 QA: "itu juga berfungsi untuk yang zimbra mail nya juga
berfungsi untuk yang autentikasi si console nya jadi ada 2 fungsi itu harus
berfungsi semua."

Writing AD_* into /etc/kin-mail/config only configures the console. Zimbra
authenticates through its own zimbraAuthLdap* domain attributes, which
06-hybrid-auth.sh sets with zmprov - so saving the form used to leave mail
sign-in purely local with nothing saying so.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import kin_privhelper.commands  # noqa: F401
from kin_privhelper import appliance_settings as aps


class AdAppliesToBothTests(unittest.IsolatedAsyncioTestCase):
    BASE = {
        "section": "ad",
        "enabled": True,
        "ldap_url": "ldaps://dc.example.test:636",
        "search_base": "DC=example,DC=test",
        "search_bind_dn": "CN=svc,DC=example,DC=test",
        "search_bind_password": "secret",
    }

    async def _run(self, args, config_values):
        written: dict[str, str] = {}

        def fake_update(updates):
            written.update(updates)
            return 0, ["set " + " ".join(sorted(updates))]

        ran: list[list[str]] = []

        async def fake_stream(argv, extra_env=None):
            ran.append(list(argv))
            yield {"type": "stdout", "data": "hybrid ok\n"}
            yield {"type": "done", "exit_code": 0}

        with (
            patch.object(aps, "update_config_keys", fake_update),
            patch.object(aps, "_config_value", lambda k: config_values.get(k, "")),
            patch.object(aps, "resolve_under_deploy", lambda c, n: "/opt/kin/06-hybrid-auth.sh"),
            patch("kin_privhelper.commands._stream_subprocess", fake_stream),
        ):
            events = [ev async for ev in aps._set_ad(args)]
        text = "".join(str(e.get("data") or "") for e in events)
        code = next(e.get("exit_code") for e in events if e.get("type") == "done")
        return written, ran, text, code

    async def test_enabling_ad_reconfigures_zimbra_too(self) -> None:
        written, ran, text, code = await self._run(
            {**self.BASE, "test_user": "alice", "test_pass": "pw"},
            {"AD_TEST_USER": "alice", "AD_TEST_PASS": "pw"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(written["AD_AUTH_ENABLED"], "yes")
        self.assertEqual(written["AD_TEST_USER"], "alice")
        self.assertEqual(len(ran), 1, "06-hybrid-auth.sh must actually run")
        self.assertIn("06-hybrid-auth.sh", ran[0][0])
        self.assertIn("Zimbra now authenticates", text)

    async def test_without_a_test_account_it_says_zimbra_was_not_touched(self) -> None:
        # 06-hybrid-auth.sh refuses without one, so silently skipping it would
        # leave the operator believing mail sign-in had been switched over.
        written, ran, text, code = await self._run(self.BASE, {})
        self.assertEqual(written["AD_AUTH_ENABLED"], "yes")
        self.assertEqual(ran, [], "must not run the script it knows will refuse")
        self.assertIn("Zimbra was NOT reconfigured", text)
        self.assertIn("AD_TEST_USER", text)
        self.assertEqual(code, 0)

    async def test_disabling_ad_leaves_zimbra_alone(self) -> None:
        written, ran, text, code = await self._run(
            {**self.BASE, "enabled": False}, {"AD_TEST_USER": "a", "AD_TEST_PASS": "b"}
        )
        self.assertEqual(written["AD_AUTH_ENABLED"], "no")
        self.assertEqual(ran, [])
        self.assertIn("left untouched", text)
        self.assertEqual(code, 0)

    async def test_blank_secrets_do_not_wipe_stored_ones(self) -> None:
        # The form never echoes secrets back, so an untouched field arrives
        # empty and must mean "keep what is stored".
        written, _ran, _text, _code = await self._run(
            {**self.BASE, "search_bind_password": "", "test_user": "", "test_pass": ""},
            {"AD_TEST_USER": "alice", "AD_TEST_PASS": "pw"},
        )
        self.assertNotIn("AD_SEARCH_BIND_PASSWORD", written)
        self.assertNotIn("AD_TEST_USER", written)
        self.assertNotIn("AD_TEST_PASS", written)

    async def test_a_failing_hybrid_script_is_reported_not_swallowed(self) -> None:
        async def failing(argv, extra_env=None):
            yield {"type": "stderr", "data": "zmprov blew up\n"}
            yield {"type": "done", "exit_code": 3}

        with (
            patch.object(aps, "update_config_keys", lambda u: (0, [])),
            patch.object(aps, "_config_value", lambda k: "set"),
            patch.object(aps, "resolve_under_deploy", lambda c, n: "/opt/kin/06-hybrid-auth.sh"),
            patch("kin_privhelper.commands._stream_subprocess", failing),
        ):
            events = [ev async for ev in aps._set_ad({**self.BASE, "test_user": "a"})]
        text = "".join(str(e.get("data") or "") for e in events)
        code = next(e.get("exit_code") for e in events if e.get("type") == "done")
        self.assertEqual(code, 3)
        self.assertIn("mail sign-in is unchanged", text)

    def test_console_and_zimbra_read_the_same_keys(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        hybrid = (root / "install/06-hybrid-auth.sh").read_text(encoding="utf-8")
        console = (root / "console/backend/kin_console/ad_auth.py").read_text(encoding="utf-8")
        for key in ("AD_LDAP_URL", "AD_SEARCH_BASE", "AD_SEARCH_BIND_DN"):
            self.assertIn(key, hybrid, f"{key} missing from the Zimbra side")
            self.assertIn(key, console, f"{key} missing from the console side")


if __name__ == "__main__":
    unittest.main()


class CloudflareTokenTests(unittest.IsolatedAsyncioTestCase):
    """The console must be able to supply the Cloudflare API token itself.

    certbot's plugin reads it from a file and 04-tls-dkim.sh refuses to prompt
    when it has no terminal - which is every console-driven run. Without this
    the operator had to SSH in and hand-write /etc/letsencrypt/cloudflare.ini,
    the exact admin access this console exists to replace.
    """

    def test_validation_rejects_what_is_not_a_token(self) -> None:
        from kin_privhelper.appliance_settings import cloudflare_token_error as err

        self.assertIsNotNone(err(""))
        self.assertIsNotNone(err("   "))
        self.assertIsNotNone(err("PASTE_YOUR_TOKEN_HERE"))
        self.assertIsNotNone(err("too-short"))
        self.assertIsNotNone(err("token with spaces in it aaaaaaaaaaaa"))
        self.assertIsNotNone(err("has\nnewline" + "a" * 30))
        self.assertIsNotNone(err("a" * 250))
        self.assertIsNotNone(err("bad$chars!" + "a" * 30))

    def test_a_realistic_token_is_accepted(self) -> None:
        from kin_privhelper.appliance_settings import cloudflare_token_error as err

        self.assertIsNone(err("Xy9_-.ABCdef0123456789ABCdef0123456789xy"))

    async def test_token_is_written_root_only_and_never_echoed(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper import appliance_settings as aps

        token = "Xy9_-.ABCdef0123456789ABCdef0123456789xy"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "letsencrypt" / "cloudflare.ini"
            with (
                patch.object(aps, "CF_CREDS_PATH", dest),
                patch.object(os, "chown", lambda *a, **k: None),
            ):
                events = [
                    ev async for ev in aps._set_cloudflare_token({"token": token})
                ]
            body = dest.read_text(encoding="utf-8")
            self.assertEqual(body, f"dns_cloudflare_api_token = {token}\n")
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

        text = "".join(str(e.get("data") or "") for e in events)
        code = next(e.get("exit_code") for e in events if e.get("type") == "done")
        self.assertEqual(code, 0)
        # The token must not appear anywhere in the output, not even truncated.
        self.assertNotIn(token, text)
        self.assertNotIn(token[:12], text)

    async def test_a_bad_token_is_refused_without_writing(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from kin_privhelper import appliance_settings as aps

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cloudflare.ini"
            with patch.object(aps, "CF_CREDS_PATH", dest):
                events = [ev async for ev in aps._set_cloudflare_token({"token": "nope"})]
            self.assertFalse(dest.exists(), "must not create the file for a bad token")
        code = next(e.get("exit_code") for e in events if e.get("type") == "done")
        self.assertEqual(code, 2)

    def test_the_installer_still_fails_closed_without_a_token(self) -> None:
        # Storing it from the console is an addition, not a replacement for the
        # non-interactive guard in the installer.
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        tls = (root / "install/04-tls-dkim.sh").read_text(encoding="utf-8")
        self.assertIn("if [ ! -t 0 ]", tls)
        self.assertIn("$CF_CREDS", tls)

    def test_every_component_agrees_on_the_credentials_path(self) -> None:
        """The console writes the token; certbot reads it. If these three
        drift, the console would store it somewhere nothing ever reads."""
        from pathlib import Path

        from kin_privhelper.appliance_settings import CF_CREDS_PATH

        root = Path(__file__).resolve().parents[3]
        expected = "/etc/letsencrypt/cloudflare.ini"
        self.assertEqual(str(CF_CREDS_PATH), expected)
        # The installer's own definition.
        self.assertIn(
            f'CF_CREDS="{expected}"',
            (root / "install/00-config.sh").read_text(encoding="utf-8"),
        )
        # And the value the wizard writes into /etc/kin-mail/config.
        self.assertIn(
            f'"CF_CREDS": "{expected}"',
            (root / "console/backend/kin_privhelper/apply_config.py").read_text(
                encoding="utf-8"
            ),
        )
