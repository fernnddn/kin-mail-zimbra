"""A license applies to the appliance, not to whichever node you typed it on."""

from __future__ import annotations

import unittest


class PlanLicenseSyncTests(unittest.TestCase):
    def _plan(self, **kw):
        from kin_privhelper.license_sync import plan_license_sync

        base = {"topology": "2vm", "peer_name": "mail2.example.test", "peer_ip": "192.0.2.9"}
        base.update(kw)
        return plan_license_sync(**base)

    def test_a_single_node_has_nothing_to_sync(self) -> None:
        self.assertEqual(self._plan(topology="1vm")["action"], "single")

    def test_a_pair_pushes_to_the_peer(self) -> None:
        plan = self._plan()
        self.assertEqual(plan["action"], "push")
        self.assertEqual(plan["peer_ip"], "192.0.2.9")

    def test_a_pair_with_no_peer_address_refuses_rather_than_pretending(self) -> None:
        # Reporting success here is how one node ends up licensed and the other
        # not, with the answer depending on where the VIP is pointing.
        plan = self._plan(peer_ip="")
        self.assertEqual(plan["action"], "refuse")
        self.assertIn("applied here only", plan["error"])


class PeerConfigWithSeatsTests(unittest.TestCase):
    SAMPLE = (
        'MAIL_HOST="mail2.example.test"\n'
        'SERVER_IP="192.0.2.9"\n'
        'MAIL_DOMAIN="example.test"\n'
        'CONTRACTED_SEATS="10"\n'
    )

    def _run(self, text, seats="50"):
        from kin_privhelper.license_sync import peer_config_with_seats

        return peer_config_with_seats(text, seats)

    def test_only_the_seat_count_changes(self) -> None:
        from kin_privhelper.apply_config import parse_config

        body, err = self._run(self.SAMPLE)
        self.assertEqual(err, "")
        got = parse_config(body)
        self.assertEqual(got["CONTRACTED_SEATS"], "50")
        # The peer keeps its own identity: rewriting it from this node's config
        # would point the peer at this node's hostname and address.
        self.assertEqual(got["MAIL_HOST"], "mail2.example.test")
        self.assertEqual(got["SERVER_IP"], "192.0.2.9")
        self.assertEqual(got["MAIL_DOMAIN"], "example.test")

    def test_an_empty_read_is_refused(self) -> None:
        # Formatting an empty parse would produce a valid-looking config with
        # every one of the peer's settings gone.
        _body, err = self._run("")
        self.assertIn("empty", err)

    def test_a_truncated_read_is_refused(self) -> None:
        _body, err = self._run('MAIL_DOMAIN="example.test"\n')
        self.assertIn("MAIL_HOST", err)
        self.assertIn("SERVER_IP", err)

    def test_a_nonsense_seat_count_never_reaches_the_peer(self) -> None:
        for bad in ("", "0", "-5", "abc", "10; rm -rf /"):
            with self.subTest(bad=bad):
                _body, err = self._run(self.SAMPLE, seats=bad)
                self.assertTrue(err, f"{bad!r} was accepted")


class LicenseApplyWiringTests(unittest.TestCase):
    """The handler has to actually call the sync, and report a failed one."""

    def _source(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper/appliance_settings.py"
        ).read_text(encoding="utf-8")

    def test_applying_a_license_pushes_it_to_the_peer(self) -> None:
        src = self._source()
        head = src.index("async def _set_license")
        body = src[head : head + 4000]
        self.assertIn("push_license_to_peer", body)

    def test_setting_seats_by_hand_pushes_too(self) -> None:
        src = self._source()
        head = src.index("async def _set_seats")
        body = src[head : src.index("async def", head + 10)]
        self.assertIn("push_license_to_peer", body)

    def test_a_failed_push_is_not_reported_as_success(self) -> None:
        src = self._source()
        head = src.index("async def _set_license")
        body = src[head : head + 4000]
        # Exit 0 here would tell the operator the appliance is licensed when
        # half of it is not.
        self.assertIn("proto.event_done(3)", body)

    def test_seats_are_written_before_the_token(self) -> None:
        from kin_privhelper import license_sync

        from pathlib import Path

        src = Path(license_sync.__file__).read_text(encoding="utf-8")
        head = src.index("async def push_license_to_peer")
        body = src[head:]
        # A token with a stale seat count reads as licensed while enforcing the
        # old limit, which is worse than not syncing at all.
        self.assertLess(body.index("MAIL_CONFIG_DEST"), body.index("LICENSE_TOKEN_DEST"))


if __name__ == "__main__":
    unittest.main()
