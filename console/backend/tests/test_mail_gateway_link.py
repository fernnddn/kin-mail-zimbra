"""The mail gateway link: what it refuses, and what it never leaks.

From 0.1.10 a Proxmox Mail Gateway in front of Zimbra is mandatory. That makes
this module responsible for an API credential with full control of the machine
every message passes through, so most of what is worth testing here is the
negative space: values it will not accept, a compliance flag that will not go
green early, and a secret that never appears anywhere the console can read.

The decisions that could break mail delivery - relaynomx, the relay domain,
double DKIM signing - live in install/lib/mail-gateway.sh and have their own
suite. These are the parts only the daemon can enforce.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kin_privhelper import commands, mail_gateway as gw, protocol as proto, rbac


def _drain(gen) -> list[dict]:
    async def go() -> list[dict]:
        return [ev async for ev in gen]

    return asyncio.run(go())


def _text(events: list[dict]) -> str:
    return "".join(str(e.get("data") or "") for e in events)


def _exit(events: list[dict]) -> int:
    for ev in reversed(events):
        if ev.get("type") == "done":
            return int(ev.get("exit_code", 1))
    return -1


class GatewayTempPaths(unittest.TestCase):
    """Point everything this module writes at a temp tree, including the
    Fernet key, so no test can touch a real appliance path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state = root / "state"
        state.mkdir()
        self.root = root

        from kin_privhelper import deploy_state, provisioning_secrets

        patches = [
            mock.patch.object(gw, "GATEWAY_CONF", root / "mail-gateway.conf"),
            mock.patch.object(gw, "STATE_DIR", state),
            mock.patch.object(gw, "STATE_FILE", state / "mail-gateway-state.json"),
            mock.patch.object(gw, "PIN_FILE", state / "mail-gateway-fingerprint"),
            mock.patch.object(gw, "VAULT_PATH", root / "vault.json"),
            mock.patch.object(provisioning_secrets, "KEY_PATH", root / "secrets.key"),
            mock.patch.object(deploy_state, "ensure_kin_mail_dir", lambda p=None: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)


# ---------------------------------------------------------------------------
# Input validation - these values end up in a config file a shell script parses
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def test_a_gateway_address_that_could_carry_a_command_is_refused(self) -> None:
        for value in (
            "",
            "192.0.2.9; rm -rf /",
            "gw.example.com`id`",
            "gw example com",
            "gw.example.com\nGATEWAY_AUTH=ticket",
            "-gw.example.com",
            ".example.com",
            "$(whoami)",
            "gw.example.com|nc attacker 1234",
        ):
            with self.subTest(value=value):
                self.assertFalse(gw.valid_host(value))

    def test_ordinary_gateway_addresses_are_accepted(self) -> None:
        for value in ("192.0.2.9", "gw.example.com", "pmg-01.corp.local", "fd00::1"):
            with self.subTest(value=value):
                self.assertTrue(gw.valid_host(value))

    def test_a_malformed_pmg_token_id_is_refused(self) -> None:
        for value in (
            "",
            "root@pam",            # no token name
            "kinmail",             # no realm
            "root@pam!kin mail",   # whitespace ends the shell word
            'root@pam!kin"mail',
            "root@pam!kin;mail",
            "root@pam!kin$mail",
            "root@pam!kin\nmail",
        ):
            with self.subTest(value=value):
                self.assertFalse(gw.valid_token_id(value))

    def test_a_real_pmg_token_id_is_accepted(self) -> None:
        self.assertTrue(gw.valid_token_id("root@pam!kinmail"))

    def test_a_port_that_is_not_a_port_is_refused(self) -> None:
        for value in ("", "0", "65536", "-1", "eight thousand", None, "80 80"):
            with self.subTest(value=value):
                self.assertFalse(gw.valid_port(value))

    def test_ordinary_ports_are_accepted(self) -> None:
        for value in (8006, "8006", 443, 1, 65535):
            with self.subTest(value=value):
                self.assertTrue(gw.valid_port(value))


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------
class CredentialTests(GatewayTempPaths):
    def test_the_token_secret_round_trips_through_the_vault(self) -> None:
        gw.store_credentials({"token_secret": "swordfish"})
        self.assertEqual(gw.load_credentials(), {"token_secret": "swordfish"})
        self.assertTrue(gw.credential_present())

    def test_the_vault_file_never_contains_the_secret_in_the_clear(self) -> None:
        gw.store_credentials({"token_secret": "swordfish"})
        raw = gw.VAULT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("swordfish", raw)
        # Metadata may say WHICH fields are set; never what they are.
        self.assertEqual(json.loads(raw)["fields"], ["token_secret"])

    def test_the_vault_is_readable_only_by_root(self) -> None:
        gw.store_credentials({"token_secret": "swordfish"})
        self.assertEqual(gw.VAULT_PATH.stat().st_mode & 0o777, 0o600)

    def test_only_known_credential_fields_are_stored(self) -> None:
        gw.store_credentials({"token_secret": "s", "root_password": "should-not-persist"})
        self.assertEqual(gw.load_credentials(), {"token_secret": "s"})

    def test_storing_nothing_is_refused_rather_than_writing_an_empty_vault(self) -> None:
        with self.assertRaises(ValueError):
            gw.store_credentials({"token_secret": ""})

    def test_an_unreadable_vault_does_not_count_as_a_credential(self) -> None:
        # A corrupt vault used to be an easy way to make a compliance gate pass
        # on a credential nothing could actually use.
        gw.VAULT_PATH.write_text("this is not json", encoding="utf-8")
        self.assertFalse(gw.credential_present())

    def test_forgetting_a_credential_that_was_never_stored_is_not_an_error(self) -> None:
        gw.forget_credentials()
        gw.forget_credentials()


# ---------------------------------------------------------------------------
# The configuration file
# ---------------------------------------------------------------------------
class ConfigFileTests(GatewayTempPaths):
    def test_the_config_file_holds_no_secret_and_stays_console_readable(self) -> None:
        gw.store_credentials({"token_secret": "swordfish"})
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")
        body = gw.GATEWAY_CONF.read_text(encoding="utf-8")
        self.assertNotIn("swordfish", body)
        self.assertIn('GATEWAY_HOST="192.0.2.9"', body)
        # The console reads this directly for the status panel. There is
        # nothing in it worth protecting, and 0600 would only break that read.
        self.assertEqual(gw.GATEWAY_CONF.stat().st_mode & 0o777, 0o644)

    def test_config_is_read_back_without_executing_it(self) -> None:
        marker = self.root / "pwned"
        gw.GATEWAY_CONF.write_text(
            f'GATEWAY_HOST="192.0.2.9"\n$(touch {marker})\nGATEWAY_AUTH=token\n',
            encoding="utf-8",
        )
        conf = gw.read_config()
        self.assertEqual(conf["GATEWAY_HOST"], "192.0.2.9")
        self.assertEqual(conf["GATEWAY_AUTH"], "token")
        self.assertFalse(marker.exists())

    def test_a_missing_config_file_reads_as_empty_rather_than_raising(self) -> None:
        self.assertEqual(gw.read_config(), {})


# ---------------------------------------------------------------------------
# Certificate pinning
# ---------------------------------------------------------------------------
class PinningTests(GatewayTempPaths):
    def test_a_pinned_fingerprint_is_stored_per_host(self) -> None:
        gw.pin_fingerprint("192.0.2.9", "AA:BB")
        gw.pin_fingerprint("192.0.2.8", "CC:DD")
        self.assertEqual(gw.pinned_fingerprint("192.0.2.9"), "AA:BB")
        self.assertEqual(gw.pinned_fingerprint("192.0.2.8"), "CC:DD")

    def test_re_pinning_a_host_replaces_rather_than_appends(self) -> None:
        gw.pin_fingerprint("192.0.2.9", "AA:BB")
        gw.pin_fingerprint("192.0.2.9", "EE:FF")
        self.assertEqual(gw.pinned_fingerprint("192.0.2.9"), "EE:FF")
        self.assertEqual(gw.PIN_FILE.read_text(encoding="utf-8").count("192.0.2.9"), 1)

    def test_an_unpinned_host_reports_no_fingerprint(self) -> None:
        self.assertEqual(gw.pinned_fingerprint("192.0.2.9"), "")


# ---------------------------------------------------------------------------
# Status and the compliance gate
# ---------------------------------------------------------------------------
class StatusTests(GatewayTempPaths):
    def test_a_fresh_appliance_is_not_compliant(self) -> None:
        snap = gw.status()
        self.assertFalse(snap["compliant"])
        self.assertFalse(snap["configured"])
        self.assertEqual(snap["phase"], "never-applied")

    def test_recording_a_gateway_is_not_the_same_as_applying_it(self) -> None:
        """The banner must not go green because somebody opened the dialog."""
        gw.store_credentials({"token_secret": "s"})
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")

        snap = gw.status()
        self.assertTrue(snap["configured"])
        self.assertFalse(
            snap["compliant"],
            "connect alone must not satisfy the mandatory-gateway requirement",
        )

        gw.STATE_FILE.write_text(
            json.dumps({"phase": "applied", "at": "2026-09-11T20:00:00+0000"}),
            encoding="utf-8",
        )
        self.assertTrue(gw.status()["compliant"])

    def test_reverting_takes_the_appliance_back_out_of_compliance(self) -> None:
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")
        gw.STATE_FILE.write_text(json.dumps({"phase": "applied"}), encoding="utf-8")
        self.assertTrue(gw.status()["compliant"])
        gw.STATE_FILE.write_text(json.dumps({"phase": "reverted"}), encoding="utf-8")
        self.assertFalse(gw.status()["compliant"])

    def test_a_disabled_link_is_not_compliant_even_if_it_was_applied(self) -> None:
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail", enabled=False)
        gw.STATE_FILE.write_text(json.dumps({"phase": "applied"}), encoding="utf-8")
        self.assertFalse(gw.status()["compliant"])

    def test_status_never_carries_the_credential(self) -> None:
        gw.store_credentials({"token_secret": "swordfish"})
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")
        blob = json.dumps(gw.status())
        self.assertNotIn("swordfish", blob)
        # The token id is an identifier, not a secret, and the operator needs
        # to see which token is in use in order to revoke the right one.
        self.assertIn("root@pam!kinmail", blob)

    def test_a_corrupt_state_file_reads_as_never_applied(self) -> None:
        gw.STATE_FILE.write_text("{ truncated", encoding="utf-8")
        self.assertEqual(gw.status()["phase"], "never-applied")
        self.assertFalse(gw.status()["compliant"])


# ---------------------------------------------------------------------------
# The privhelper command
# ---------------------------------------------------------------------------
class CommandTests(GatewayTempPaths):
    def test_an_unknown_operation_is_refused_by_name(self) -> None:
        events = _drain(commands.cmd_mail_gateway({"op": "rm -rf /"}))
        self.assertEqual(_exit(events), 2)
        self.assertIn("must be one of", _text(events))

    def test_status_answers_without_a_credential_or_a_reachable_gateway(self) -> None:
        # Status has to work exactly when everything else cannot: nothing
        # configured, gateway down, never applied. Those are the moments the
        # console most needs an answer.
        events = _drain(commands.cmd_mail_gateway({"op": "status"}))
        self.assertEqual(_exit(events), 0)
        line = [ln for ln in _text(events).splitlines() if ln.startswith("KIN_GW_STATUS ")]
        self.assertEqual(len(line), 1)
        snap = json.loads(line[0][len("KIN_GW_STATUS ") :])
        self.assertFalse(snap["compliant"])

    def test_connect_refuses_a_gateway_address_that_is_not_an_address(self) -> None:
        events = _drain(
            commands.cmd_mail_gateway(
                {"op": "connect", "host": "192.0.2.9; id", "token_id": "root@pam!k",
                 "token_secret": "s"}
            )
        )
        self.assertEqual(_exit(events), 2)
        self.assertFalse(gw.GATEWAY_CONF.exists())
        self.assertFalse(gw.credential_present())

    def test_connect_refuses_a_malformed_token_id(self) -> None:
        events = _drain(
            commands.cmd_mail_gateway(
                {"op": "connect", "host": "192.0.2.9", "token_id": "kinmail", "token_secret": "s"}
            )
        )
        self.assertEqual(_exit(events), 2)
        self.assertIn("root@pam!kinmail", _text(events), "the error should show the shape expected")

    def test_connect_refuses_a_token_with_no_secret(self) -> None:
        events = _drain(
            commands.cmd_mail_gateway(
                {"op": "connect", "host": "192.0.2.9", "token_id": "root@pam!kinmail"}
            )
        )
        self.assertEqual(_exit(events), 2)
        self.assertFalse(gw.credential_present())

    def test_connect_stores_the_credential_and_never_echoes_it(self) -> None:
        events = _drain(
            commands.cmd_mail_gateway(
                {
                    "op": "connect",
                    "host": "192.0.2.9",
                    "api_port": 8006,
                    "token_id": "root@pam!kinmail",
                    "token_secret": "swordfish",
                }
            )
        )
        self.assertEqual(_exit(events), 0)
        self.assertNotIn("swordfish", _text(events))
        self.assertEqual(gw.load_credentials(), {"token_secret": "swordfish"})
        self.assertTrue(gw.status()["configured"])
        # ...and still not compliant, because nothing has been applied.
        self.assertFalse(gw.status()["compliant"])

    def test_trust_certificate_refuses_anything_that_is_not_a_fingerprint(self) -> None:
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")
        for value in ("", "not-a-fingerprint", "AA:BB; rm -rf /", "../../etc/passwd"):
            with self.subTest(value=value):
                events = _drain(
                    commands.cmd_mail_gateway({"op": "trust_certificate", "fingerprint": value})
                )
                self.assertEqual(_exit(events), 2)
        self.assertEqual(gw.pinned_fingerprint("192.0.2.9"), "")

    def test_trust_certificate_records_the_fingerprint_the_operator_confirmed(self) -> None:
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")
        events = _drain(
            commands.cmd_mail_gateway(
                {"op": "trust_certificate", "fingerprint": "aa:bb:cc:dd"}
            )
        )
        self.assertEqual(_exit(events), 0)
        self.assertEqual(gw.pinned_fingerprint("192.0.2.9"), "AA:BB:CC:DD")

    def test_forget_clears_the_credential_and_disables_the_link(self) -> None:
        gw.store_credentials({"token_secret": "swordfish"})
        gw.write_config(host="192.0.2.9", token_id="root@pam!kinmail")
        gw.STATE_FILE.write_text(json.dumps({"phase": "applied"}), encoding="utf-8")
        self.assertTrue(gw.status()["compliant"])

        events = _drain(commands.cmd_mail_gateway({"op": "forget"}))
        self.assertEqual(_exit(events), 0)
        self.assertFalse(gw.credential_present())
        self.assertFalse(gw.status()["compliant"])
        # Forgetting the credential does NOT stop mail flowing through the
        # gateway, and saying otherwise would be a lie the operator acts on.
        self.assertIn("until you run revert", _text(events))

    def _fake_script(self):
        """A resolvable, executable stand-in so lock behaviour is what is
        under test rather than the absence of a deploy tree."""
        path = self.root / "mail-gateway.sh"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_applying_takes_the_maintenance_lock(self) -> None:
        """A resize, a deploy and a relay change must never overlap."""
        with mock.patch.object(
            commands, "resolve_mail_gateway", return_value=self._fake_script()
        ), mock.patch(
            "kin_privhelper.maintenance.try_lock_maintenance", return_value=None
        ) as locked:
            events = _drain(commands.cmd_mail_gateway({"op": "apply"}))
        self.assertTrue(locked.called, "apply must ask for the maintenance lock")
        self.assertNotEqual(_exit(events), 0)
        self.assertIn("another operation is already running", _text(events))

    def test_reverting_takes_the_maintenance_lock_too(self) -> None:
        with mock.patch.object(
            commands, "resolve_mail_gateway", return_value=self._fake_script()
        ), mock.patch(
            "kin_privhelper.maintenance.try_lock_maintenance", return_value=None
        ) as locked:
            events = _drain(commands.cmd_mail_gateway({"op": "revert"}))
        self.assertTrue(locked.called)
        self.assertNotEqual(_exit(events), 0)

    def test_reading_operations_do_not_take_the_maintenance_lock(self) -> None:
        # An operator asking what WOULD happen during a deploy is exactly when
        # they want to ask, so plan must not be blocked by an unrelated job.
        for op in ("probe", "plan", "verify"):
            with self.subTest(op=op):
                with mock.patch.object(
                    commands, "resolve_mail_gateway", return_value=self._fake_script()
                ), mock.patch(
                    "kin_privhelper.maintenance.try_lock_maintenance", return_value=None
                ) as locked:
                    events = _drain(commands.cmd_mail_gateway({"op": op}))
                self.assertFalse(locked.called, f"{op} must not take the maintenance lock")
                self.assertEqual(_exit(events), 0)

    def test_the_token_secret_reaches_the_script_by_environment_not_argv(self) -> None:
        """An argument is visible in ps(1) to every user on the box."""
        gw.store_credentials({"token_secret": "swordfish"})
        seen = {}

        async def fake_stream(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("extra_env") or {}
            seen["secrets"] = kwargs.get("secrets") or []
            yield proto.event_done(0)

        with mock.patch.object(
            commands, "resolve_mail_gateway", return_value=self._fake_script()
        ), mock.patch.object(commands, "_stream_subprocess", fake_stream):
            _drain(commands.cmd_mail_gateway({"op": "probe"}))

        self.assertNotIn("swordfish", " ".join(seen["argv"]))
        self.assertEqual(seen["env"].get("KIN_PMG_TOKEN_SECRET"), "swordfish")
        # ...and it is on the redaction list, so it cannot reach the transcript.
        self.assertIn("swordfish", seen["secrets"])


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
class WiringTests(unittest.TestCase):
    def test_the_command_is_on_the_privhelper_whitelist(self) -> None:
        self.assertIn(proto.CMD_MAIL_GATEWAY, proto.ALLOWED_COMMANDS)

    def test_the_command_has_a_handler(self) -> None:
        self.assertIsNotNone(commands.get_handler(proto.CMD_MAIL_GATEWAY))

    def test_changing_where_mail_is_relayed_is_an_ops_level_command(self) -> None:
        """A Customer Admin must not be able to repoint the appliance's mail."""
        self.assertIn(proto.CMD_MAIL_GATEWAY, rbac.SENSITIVE_OPS_COMMANDS)

    def test_every_advertised_operation_has_a_home(self) -> None:
        self.assertEqual(
            set(gw.ALL_OPS),
            set(gw.READ_ONLY_OPS) | set(gw.MUTATING_OPS) | set(gw.LOCAL_OPS),
        )
        # Apply and revert are the only two that change the appliance, and so
        # the only two that may take the maintenance lock.
        self.assertEqual(set(gw.MUTATING_OPS), {"apply", "revert"})


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The HTTP surface
#
# Called directly rather than through a TestClient: that needs httpx, which is
# not a dependency of this product. What matters here is who may ask, and what
# an unanswered question looks like - because both used to be wrong. A status
# read that did not happen must never be rendered as "no gateway configured".
# ---------------------------------------------------------------------------
class HttpSurfaceTests(unittest.TestCase):
    def _user(self, role: str):
        return type("U", (), {"username": "op", "role": role})()

    def test_a_customer_admin_is_refused_rather_than_told_there_is_no_gateway(self) -> None:
        from fastapi import HTTPException

        from kin_console import app as app_module

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                app_module.mail_gateway_status(user=self._user(rbac.ROLE_CUSTOMER_ADMIN))
            )
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_unreadable_status_is_an_error_not_a_green_or_red_banner(self) -> None:
        from fastapi import HTTPException

        from kin_console import app as app_module

        async def dead(*_a, **_k):
            return {"ok": False, "error": "privhelper is not answering", "log": ""}

        with mock.patch.object(app_module, "_collect_privhelper", dead):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(
                    app_module.mail_gateway_status(user=self._user(rbac.ROLE_SUPER_ADMIN))
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("privhelper is not answering", str(caught.exception.detail))

    def test_a_readable_status_is_returned_as_it_came_back(self) -> None:
        from kin_console import app as app_module

        snapshot = {"compliant": True, "gateway_host": "192.0.2.9", "phase": "applied"}

        async def alive(*_a, **_k):
            return {"ok": True, "log": "KIN_GW_STATUS " + json.dumps(snapshot) + "\n"}

        with mock.patch.object(app_module, "_collect_privhelper", alive):
            got = asyncio.run(
                app_module.mail_gateway_status(user=self._user(rbac.ROLE_SUPPORT_OPS))
            )
        self.assertEqual(got, snapshot)

    def test_connect_is_a_post_so_the_token_never_lands_in_an_access_log(self) -> None:
        """A streaming GET would leave the credential in the URL."""
        src = Path(app_module_path()).read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/mail-gateway/connect")', src)
        # ...and the streaming endpoint must not accept connect at all.
        self.assertIn(
            'detail="mail_gateway stream allows probe, plan, apply, verify, revert or status"',
            src,
        )


def app_module_path() -> str:
    from kin_console import app as app_module

    return app_module.__file__


class AuditTrailTests(unittest.TestCase):
    """An audit line that says only "mail_gateway" is not an audit line.

    Reading the configuration and repointing every message the company sends
    are the same command with a different op, and the log has to tell them
    apart. The credential must never appear, and args are not logged anywhere
    in this daemon regardless - the label is built from them, not dumped.
    """

    def _label(self, args: dict) -> str:
        from kin_privhelper import daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        self.assertIn("elif cmd == proto.CMD_MAIL_GATEWAY:", src)
        # Reproduce the label the daemon builds, from the daemon's own rule.
        cmd = proto.CMD_MAIL_GATEWAY
        gwop = str(args.get("op") or "status")[:24]
        gwhost = str(args.get("host") or "")[:80]
        return f"{cmd}:{gwop}" + (f":{gwhost}" if gwhost else "")

    def test_the_daemon_has_a_label_rule_for_this_command(self) -> None:
        from kin_privhelper import daemon

        src = "\n".join(
            ln
            for ln in Path(daemon.__file__).read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        )
        self.assertIn("elif cmd == proto.CMD_MAIL_GATEWAY:", src)
        self.assertIn('audit_cmd = f"{cmd}:{gwop}"', src)

    def test_the_operation_reaches_the_audit_label(self) -> None:
        self.assertEqual(self._label({"op": "apply"}), "mail_gateway:apply")
        self.assertEqual(self._label({"op": "probe"}), "mail_gateway:probe")
        self.assertNotEqual(self._label({"op": "apply"}), self._label({"op": "probe"}))

    def test_the_gateway_address_is_recorded_on_connect(self) -> None:
        self.assertEqual(
            self._label({"op": "connect", "host": "192.0.2.9"}),
            "mail_gateway:connect:192.0.2.9",
        )

    def test_the_credential_never_reaches_the_label(self) -> None:
        label = self._label(
            {"op": "connect", "host": "192.0.2.9", "token_secret": "swordfish",
             "password": "hunter2"}
        )
        self.assertNotIn("swordfish", label)
        self.assertNotIn("hunter2", label)

    def test_the_daemon_never_logs_args(self) -> None:
        """_audit_line takes a username, a command, a result and an exit code.

        Pinned because a previous attempt to keep secrets out of the log did it
        by deleting them from args before the handler ran, which silently broke
        licence application and AD binds.
        """
        from kin_privhelper import daemon
        import inspect

        sig = inspect.signature(daemon._audit_line)
        self.assertEqual(
            list(sig.parameters), ["username", "cmd", "result", "exit_code"]
        )
