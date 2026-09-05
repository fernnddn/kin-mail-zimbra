"""Attaching a replacement built on the retired peer's address.

Ansible connects with StrictHostKeyChecking=accept-new. That accepts a host it
has never seen and REFUSES one whose key has changed - which is exactly what a
new VM on the old VM's address looks like. Attaching one failed on host key
verification, with the only clue an ssh error buried inside an ansible task.

plan_add_host used to sidestep this by refusing the address outright, with the
message "new server IP must differ from the retired peer address" and no
reason. But rebuilding on the same address is the normal thing to do: the DNS
record, the firewall rules and the addressing plan all still point at it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kin_privhelper.add_host import forget_host_keys, plan_add_host

BASE = dict(
    local_host="mail-a.example.test",
    local_ip="198.51.100.10",
    new_name="mail-b.example.test",
    new_ip="198.51.100.11",
    live_nodes=["mail-a.example.test"],
    promoted="mail-a.example.test",
    standby=[],
    data_disk="/dev/sdb1",
    meta_disk="/dev/sdb2",
)
CONTEXT = dict(
    retired_name="mail-b-old.example.test",
    obs_name="obs",
    obs_ip="198.51.100.53",
    vip_ip="198.51.100.20",
)


class ReusingTheRetiredAddress(unittest.TestCase):
    def test_it_is_allowed_and_explained(self) -> None:
        plan = plan_add_host(**BASE, **CONTEXT, retired_ip="198.51.100.11")
        self.assertEqual(plan.errors, ())
        joined = " ".join(plan.notes)
        self.assertIn("address the retired peer used", joined)
        self.assertIn("host key is forgotten", joined)
        self.assertIn("old VM is really gone", joined)

    def test_a_different_address_needs_no_such_note(self) -> None:
        plan = plan_add_host(**BASE, **CONTEXT, retired_ip="198.51.100.99")
        self.assertEqual(plan.errors, ())
        self.assertNotIn("address the retired peer used", " ".join(plan.notes))

    def test_the_survivor_address_is_still_refused(self) -> None:
        # Reusing the DEAD peer's address is fine. Reusing the LIVE survivor's
        # is not, and that guard must not have been relaxed with it.
        args = dict(BASE)
        args["new_ip"] = args["local_ip"]
        plan = plan_add_host(**args, **CONTEXT, retired_ip="198.51.100.99")
        self.assertTrue(any("differ from the survivor" in e for e in plan.errors))

    def test_the_vip_and_witness_are_still_refused(self) -> None:
        for field, value in (("vip", "198.51.100.20"), ("obs", "198.51.100.53")):
            with self.subTest(field=field):
                args = dict(BASE)
                args["new_ip"] = value
                plan = plan_add_host(**args, **CONTEXT, retired_ip="198.51.100.99")
                self.assertTrue(plan.errors, f"accepted the {field} address")


class ForgettingHostKeys(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen not available")
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".ssh").mkdir()
        self.known = self.home / ".ssh" / "known_hosts"

    def _seed(self, *hosts: str) -> None:
        key = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1yPRrKGGRs7Xf9V2n4Hn7pXbA1qWv"
            "3Qb2mKcJhZ9Xt"
        )
        self.known.write_text("".join(f"{h} {key}\n" for h in hosts), encoding="utf-8")

    def test_a_stale_key_is_removed(self) -> None:
        self._seed("198.51.100.11", "198.51.100.12")
        lines = forget_host_keys(["198.51.100.11"], homes=[str(self.home)])
        body = self.known.read_text(encoding="utf-8")
        self.assertNotIn("198.51.100.11", body)
        self.assertIn("198.51.100.12", body, "an unrelated host must be left alone")
        self.assertTrue(any("198.51.100.11" in line for line in lines))

    def test_both_the_name_and_the_address_are_forgotten(self) -> None:
        self._seed("198.51.100.11", "mail-b.example.test", "mail-c.example.test")
        forget_host_keys(
            ["198.51.100.11", "mail-b.example.test"], homes=[str(self.home)]
        )
        body = self.known.read_text(encoding="utf-8")
        self.assertNotIn("198.51.100.11", body)
        self.assertNotIn("mail-b.example.test", body)
        self.assertIn("mail-c.example.test", body)

    def test_forgetting_something_absent_is_silent(self) -> None:
        self._seed("198.51.100.12")
        self.assertEqual(forget_host_keys(["203.0.113.250"], homes=[str(self.home)]), [])
        self.assertIn("198.51.100.12", self.known.read_text(encoding="utf-8"))

    def test_no_known_hosts_file_is_not_an_error(self) -> None:
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(forget_host_keys(["198.51.100.11"], homes=[str(empty)]), [])

    def test_blank_and_duplicate_targets_are_ignored(self) -> None:
        self._seed("198.51.100.11")
        lines = forget_host_keys(
            ["", "  ", "198.51.100.11", "198.51.100.11"], homes=[str(self.home)]
        )
        self.assertEqual(len(lines), 1, "the same target must not be reported twice")

    def test_it_never_raises_on_an_unwritable_home(self) -> None:
        # A backup VM or a locked-down console must not crash the attach.
        self.assertEqual(
            forget_host_keys(["198.51.100.11"], homes=["/nonexistent/path/xyz"]), []
        )

    def test_the_attach_path_forgets_before_ansible_runs(self) -> None:
        src = Path(__file__).resolve().parents[1] / "kin_privhelper" / "add_host.py"
        text = src.read_text(encoding="utf-8")
        forget_at = text.index("forget_host_keys([plan.new_ip")
        write_at = text.index("_write_work_files(inv)")
        self.assertLess(
            forget_at, write_at, "keys must be forgotten before the inventory runs"
        )


if __name__ == "__main__":
    unittest.main()
