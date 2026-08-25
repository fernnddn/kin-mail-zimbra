"""_set_license write order: CONTRACTED_SEATS before license.token.

Writing the token first used to mean a failed config write could leave a
verified license token on disk paired with a stale seat count - the quota
gate only reads CONTRACTED_SEATS, so that combination reads as "licensed at
the old limit" rather than surfacing the write failure (re-audit,
25 Aug 2026)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import kin_privhelper.commands  # noqa: F401 - appliance_settings <-> commands is circular;
# importing commands first (as production always does, via daemon.py) lets
# appliance_settings finish initializing so patch() can resolve into it.


class SetLicenseWriteOrderTests(unittest.IsolatedAsyncioTestCase):
    _VERIFIED = {
        "server_id": "test-server-id",
        "seats": 25,
        "type": "standard",
        "status": "active",
        "grace_until": None,
        "provisioning_blocked": False,
    }

    async def _run(self, args: dict) -> list[dict]:
        from kin_privhelper.appliance_settings import _set_license

        return [ev async for ev in _set_license(args)]

    async def test_config_write_failure_never_writes_token(self) -> None:
        with (
            patch("kin_console.license.verify_license", return_value=dict(self._VERIFIED)),
            patch(
                "kin_privhelper.appliance_settings.update_config_keys",
                return_value=(1, ["ERROR: disk full"]),
            ),
            patch("kin_privhelper.appliance_settings.write_license_token") as write_token,
            patch("kin_privhelper.appliance_settings.read_server_id", return_value="test-server-id"),
        ):
            events = await self._run({"token": "irrelevant-mocked"})

        write_token.assert_not_called()
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 1)
        self.assertFalse(
            any("LICENSE_JSON" in str(ev.get("data")) for ev in events),
            "must not report success when the seat count was not persisted",
        )

    async def test_config_write_success_writes_seats_before_token(self) -> None:
        calls: list[str] = []
        with (
            patch("kin_console.license.verify_license", return_value=dict(self._VERIFIED)),
            patch(
                "kin_privhelper.appliance_settings.update_config_keys",
                side_effect=lambda *a, **kw: (calls.append("config"), (0, ["ok"]))[1],
            ),
            patch(
                "kin_privhelper.appliance_settings.write_license_token",
                side_effect=lambda *a, **kw: calls.append("token"),
            ),
            patch("kin_privhelper.appliance_settings.read_server_id", return_value="test-server-id"),
        ):
            events = await self._run({"token": "irrelevant-mocked"})

        self.assertEqual(calls, ["config", "token"])
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 0)
        self.assertTrue(any("LICENSE_JSON" in str(ev.get("data")) for ev in events))


class SetSeatsInvalidLicenseTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_token_refuses_seat_mutation(self) -> None:
        from kin_privhelper.appliance_settings import _set_seats

        with (
            patch(
                "kin_privhelper.appliance_settings.read_license_token",
                return_value="garbage-token",
            ),
            patch(
                "kin_privhelper.appliance_settings.read_server_id",
                return_value="test-server-id",
            ),
            patch(
                "kin_privhelper.appliance_settings.ensure_server_id",
                return_value="test-server-id",
            ),
            patch("kin_privhelper.appliance_settings.update_config_keys") as update,
        ):
            events = [ev async for ev in _set_seats({"seats": "99"})]

        update.assert_not_called()
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 2)
        self.assertTrue(
            any("not valid" in str(ev.get("data")) for ev in events),
        )

    async def test_missing_token_still_allows_manual_seats(self) -> None:
        from kin_privhelper.appliance_settings import _set_seats

        with (
            patch(
                "kin_privhelper.appliance_settings.read_license_token",
                return_value="",
            ),
            patch(
                "kin_privhelper.appliance_settings.read_server_id",
                return_value="test-server-id",
            ),
            patch(
                "kin_privhelper.appliance_settings.ensure_server_id",
                return_value="test-server-id",
            ),
            patch(
                "kin_privhelper.appliance_settings.update_config_keys",
                return_value=(0, ["ok"]),
            ) as update,
        ):
            events = [ev async for ev in _set_seats({"seats": "99"})]

        update.assert_called_once()
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(done[0].get("exit_code"), 0)


if __name__ == "__main__":
    unittest.main()
