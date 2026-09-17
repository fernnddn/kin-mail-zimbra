"""The split layout, as the web wizard hands it to the appliance.

The whole point of this topology is that the operator configures it in the
browser and never logs into the mailbox. That makes this mapping the only
channel to the second machine: if the address, the hostname or the password does
not survive the trip from the wizard to /etc/kin-mail/config, the deploy reaches
the mailbox step and stops, and the reason is a machine nobody has a shell on.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from kin_privhelper.apply_config import format_config, merge_draft, validate_draft
from kin_privhelper.commands import cmd_apply_wizard_draft
from kin_privhelper.deploy_state import _normalize_topology, write_topology_marker


def split_draft(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "topology": "split",
        "edge_ip": "192.0.2.6",
        "edge_host": "mail.example.test",
        "mailbox_ip": "192.0.2.4",
        "mailbox_host": "store.example.test",
        "mailbox_ssh_user": "kin",
        "mailbox_ssh_pass": "mboxsecret",
        "mail_domain": "example.test",
        "mail_host": "mail.example.test",
        "admin_pass": "AdminPw12345",
        "tls_method": "manual",
        # Not part of the split, but validate_draft requires them and this
        # fixture exists to isolate the split fields, not to retest the rest.
        "host_root_pass": "RootPw12345",
        "kin_user_pass": "KinPw12345",
        "timezone": "Asia/Jakarta",
        "le_email": "admin@example.test",
    }
    base.update(over)
    return base


# validate_draft refuses to merge onto a config that does not already know which
# machine it is on. Every real appliance has these before the wizard runs.
EXISTING: dict[str, str] = {"SERVER_IP": "192.0.2.6", "NET_IFACE": "ens33"}


class TheWizardAcceptsASplit(unittest.TestCase):
    def test_a_complete_split_draft_is_accepted(self) -> None:
        self.assertEqual(validate_draft(split_draft(), dict(EXISTING)), [])

    def test_the_topology_value_survives_normalisation(self) -> None:
        # Three layers read this back - the marker file, the config and the
        # draft - and every one of them goes through _normalize_topology. An
        # unrecognised value there reads as "topology unknown", which is not a
        # harmless default: it is what leaves a deployed appliance reporting
        # setup as incomplete, and the console without a login.
        self.assertEqual(_normalize_topology("split"), "split")
        self.assertEqual(_normalize_topology("SPLIT"), "split")
        self.assertEqual(_normalize_topology("3vm"), "")

    def test_the_topology_marker_writer_accepts_a_split(self) -> None:
        # apply_wizard_draft stops Deploy if this returns False.
        import inspect

        src = inspect.getsource(write_topology_marker)
        self.assertNotIn("not 1vm/2vm", src)
        self.assertIn("split", inspect.getsource(_normalize_topology))


class BothValidationLayersAgree(unittest.TestCase):
    """The draft endpoint and the config writer must accept the same layouts.

    They are separate checks with separate messages, and teaching only one about
    a new topology produces a wizard that refuses to save what the appliance can
    perfectly well install - which is exactly what happened: the step rendered,
    the fields filled in, and Continue answered "topology must be 1vm or 2vm".
    """

    def test_the_http_layer_knows_every_layout_the_config_writer_knows(self) -> None:
        src = (
            Path(__file__).resolve().parents[1] / "kin_console/app.py"
        ).read_text(encoding="utf-8")
        self.assertIn('("1vm", "2vm", "split")', src)
        self.assertNotIn('detail="topology must be 1vm or 2vm"', src)
        self.assertIn("updated.edge_host or updated.mail_host", src)

    def test_the_domain_step_writes_the_edge_hostname_from_mail_host(self) -> None:
        # Topology runs first and saves edge_host while mail_host is still
        # empty. Domain is where the operator types the hostname. If Domain
        # only fills edge_host when it is already empty, a later edit of
        # mail_host leaves EDGE_HOST stale and apply_draft can refuse a
        # collision that the operator already corrected.
        src = (
            Path(__file__).resolve().parents[2]
            / "frontend/src/wizard/steps/DomainStep.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("draft.topology === \"split\" ? { edge_host: host }", src)
        self.assertNotIn("!draft.edge_host.trim()", src)

    def test_no_layout_is_accepted_by_one_layer_and_refused_by_the_other(self) -> None:
        for topology in ("1vm", "2vm", "split"):
            with self.subTest(topology=topology):
                errs = validate_draft({"topology": topology}, dict(EXISTING))
                self.assertFalse(
                    any("topology must be" in e for e in errs),
                    f"the config writer refuses {topology}",
                )


class ItRefusesWhatCannotBeBuilt(unittest.TestCase):
    """Each of these is cheap here and expensive halfway through an install."""

    def assert_refused(self, draft: dict[str, object], needle: str) -> None:
        errs = validate_draft(draft, dict(EXISTING))
        self.assertTrue(errs, f"accepted a draft that should have been refused: {needle}")
        self.assertTrue(
            any(needle in e for e in errs),
            f"refused, but not for {needle!r}: {errs}",
        )

    def test_a_missing_mailbox_address(self) -> None:
        self.assert_refused(split_draft(mailbox_ip=""), "mailbox_ip is required")

    def test_a_mailbox_address_that_is_not_an_address(self) -> None:
        self.assert_refused(split_draft(mailbox_ip="store.example.test"), "IPv4")

    def test_both_roles_on_one_machine(self) -> None:
        self.assert_refused(split_draft(mailbox_ip="192.0.2.6"), "different machines")

    def test_both_machines_sharing_a_hostname(self) -> None:
        # Zimbra keys every server entry on the hostname, so two servers with
        # one name is not a split at all.
        self.assert_refused(split_draft(mailbox_host="mail.example.test"), "must differ")

    def test_no_way_to_reach_the_mailbox(self) -> None:
        # Without this the edge cannot build the second machine, and the
        # operator has no shell on it to finish by hand.
        self.assert_refused(split_draft(mailbox_ssh_pass=""), "mailbox_ssh_pass is required")

    def test_an_already_stored_password_counts(self) -> None:
        # The browser is never sent the stored password back, so an empty field
        # on a second pass means "unchanged" and must not be read as "missing".
        errs = validate_draft(
            split_draft(mailbox_ssh_pass=""),
            dict(EXISTING, MAILBOX_SSH_PASS="stored-earlier"),
        )
        self.assertEqual(errs, [])


    def test_an_empty_edge_host_is_this_machine_once_mail_host_is_known(self) -> None:
        # Topology is asked first, Domain later. The operator never types the
        # edge hostname: it is the mail server hostname they fill in next.
        # Requiring it as its own stored field is how Deploy stopped before
        # install over a complete split.
        errs = validate_draft(split_draft(edge_host=""), dict(EXISTING))
        self.assertEqual(errs, [])
        out = merge_draft(split_draft(edge_host=""), dict(EXISTING))
        self.assertEqual(out["EDGE_HOST"], "mail.example.test")

    def test_an_empty_edge_ip_is_this_machine(self) -> None:
        errs = validate_draft(split_draft(edge_ip=""), dict(EXISTING))
        self.assertEqual(errs, [])
        out = merge_draft(split_draft(edge_ip=""), dict(EXISTING))
        self.assertEqual(out["EDGE_IP"], "192.0.2.6")

    def test_mail_host_colliding_with_the_mailbox_is_still_refused(self) -> None:
        self.assert_refused(
            split_draft(edge_host="", mail_host="store.example.test"),
            "must differ",
        )


class ItReachesTheConfigFileIntact(unittest.TestCase):
    def test_every_field_lands_under_the_name_the_installer_reads(self) -> None:
        out = merge_draft(split_draft(), {})
        self.assertEqual(out["TOPOLOGY"], "split")
        self.assertEqual(out["EDGE_IP"], "192.0.2.6")
        self.assertEqual(out["EDGE_HOST"], "mail.example.test")
        self.assertEqual(out["MAILBOX_IP"], "192.0.2.4")
        self.assertEqual(out["MAILBOX_HOST"], "store.example.test")
        self.assertEqual(out["MAILBOX_SSH_USER"], "kin")
        self.assertEqual(out["MAILBOX_SSH_PASS"], "mboxsecret")

    def test_an_empty_password_keeps_the_stored_one(self) -> None:
        out = merge_draft(split_draft(mailbox_ssh_pass=""), {"MAILBOX_SSH_PASS": "kept"})
        self.assertEqual(out["MAILBOX_SSH_PASS"], "kept")

    def test_choosing_a_single_server_clears_the_split_addresses(self) -> None:
        # Otherwise a config that once described a split keeps naming a mailbox
        # that is no longer part of the deployment, and the resolver on that
        # machine still believes it has a role.
        out = merge_draft(
            {"topology": "1vm", "mail_domain": "example.test"},
            {"EDGE_IP": "192.0.2.6", "MAILBOX_IP": "192.0.2.4"},
        )
        self.assertEqual(out["EDGE_IP"], "")
        self.assertEqual(out["MAILBOX_IP"], "")
        self.assertEqual(out["MAILBOX_SSH_PASS"], "")

    def test_the_password_is_single_quoted_like_every_other_secret(self) -> None:
        # Double quotes would let $ and ` in a password be expanded when the
        # installer sources this file.
        text = format_config(merge_draft(split_draft(mailbox_ssh_pass="a$b`c"), {}))
        self.assertIn("MAILBOX_SSH_PASS='a$b`c'", text)

    def test_a_quote_in_the_password_cannot_break_the_file(self) -> None:
        text = format_config(merge_draft(split_draft(mailbox_ssh_pass="it's"), {}))
        self.assertIn(r"MAILBOX_SSH_PASS='it'\''s'", text)

    def test_the_addresses_sit_with_the_other_machine_identity(self) -> None:
        # This block is what an operator edits by hand to move a deployment.
        # Appended to the bottom it lands under the AD and test credentials.
        text = format_config(merge_draft(split_draft(), {}))
        self.assertLess(text.index("MAILBOX_IP="), text.index("ADMIN_PASS="))


class ApplyDraftLeavesAReasonInTheTranscript(unittest.TestCase):
    def test_the_command_writes_deploy_last_log_before_it_streams(self) -> None:
        # The banner used to hide the refusal. If the SSE drops, deploy-last.log
        # is the only record; writing it after streaming is how that stayed empty.
        import inspect

        src = inspect.getsource(cmd_apply_wizard_draft)
        self.assertIn("DEPLOY_LAST_LOG", src)
        self.assertLess(src.index("DEPLOY_LAST_LOG"), src.index("event_stdout"))
        self.assertLess(src.index('open("w"'), src.index("event_done"))


if __name__ == "__main__":
    unittest.main()
