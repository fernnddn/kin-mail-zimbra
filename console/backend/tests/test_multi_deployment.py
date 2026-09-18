"""A multi deployment is three machines with three jobs, not a cluster.

These pin the shape the Cluster page renders and, more importantly, the two
decisions behind it: that nothing here consults Pacemaker or DRBD, and that a
node is called healthy because a link to it answered, not because a host
replied to a ping.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path

from kin_privhelper import multi_deployment as md

REPO = Path(__file__).resolve().parents[3]


class ProbeTests(unittest.TestCase):
    def test_a_listening_port_probes_open(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(asyncio.run(md.probe_tcp("127.0.0.1", port)))
        finally:
            srv.close()

    def test_a_closed_port_probes_shut(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        self.assertFalse(asyncio.run(md.probe_tcp("127.0.0.1", port, timeout=0.5)))

    def test_an_empty_host_is_not_probed(self) -> None:
        """No host means no machine, not a machine that failed."""
        self.assertFalse(asyncio.run(md.probe_tcp("", 25)))

    def test_a_probe_gives_up_rather_than_hanging(self) -> None:
        """The Cluster page refreshes on a timer. A probe that blocks stacks up
        behind the next one and the page stops updating at all."""
        # TEST-NET-1: routable nowhere, so the connect neither succeeds nor is
        # refused - it hangs until the timeout does its job.
        started = asyncio.get_event_loop_policy().new_event_loop()
        try:
            import time

            t0 = time.monotonic()
            self.assertFalse(
                started.run_until_complete(md.probe_tcp("192.0.2.1", 25, timeout=0.6))
            )
            self.assertLess(time.monotonic() - t0, 5.0)
        finally:
            started.close()


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory()
        self.conf = Path(self.work.name, "config")
        self.gw = Path(self.work.name, "mail-gateway.conf")
        self._env = {
            "KIN_MAIL_CONFIG": os.environ.get("KIN_MAIL_CONFIG"),
            "KIN_MAIL_GATEWAY_CONF": os.environ.get("KIN_MAIL_GATEWAY_CONF"),
        }
        os.environ["KIN_MAIL_CONFIG"] = str(self.conf)
        os.environ["KIN_MAIL_GATEWAY_CONF"] = str(self.gw)

    def tearDown(self) -> None:
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.work.cleanup()

    def write_config(self, **kv: str) -> None:
        self.conf.write_text(
            "".join(f'{k}="{v}"\n' for k, v in kv.items()), encoding="utf-8"
        )

    def test_the_three_machines_are_named_and_ordered_along_the_mail_path(self) -> None:
        self.write_config(
            TOPOLOGY="split",
            EDGE_IP="192.0.2.10",
            EDGE_HOST="mail.example.test",
            MAILBOX_IP="192.0.2.11",
            MAILBOX_HOST="store.example.test",
        )
        snap = asyncio.run(md.gather())
        self.assertEqual([n["role"] for n in snap["nodes"]], ["gateway", "edge", "mailbox"])
        by_role = {n["role"]: n for n in snap["nodes"]}
        self.assertEqual(by_role["edge"]["name"], "mail.example.test")
        self.assertEqual(by_role["mailbox"]["name"], "store.example.test")
        self.assertEqual(by_role["mailbox"]["ip"], "192.0.2.11")
        self.assertTrue(by_role["edge"]["local"])

    def test_an_unlinked_gateway_is_absent_not_broken(self) -> None:
        """A gateway nobody has linked yet is a machine that is not in the
        deployment. Drawing it red would report a fault for work not started."""
        self.write_config(EDGE_IP="192.0.2.10", MAILBOX_IP="192.0.2.11")
        snap = asyncio.run(md.gather())
        gw = next(n for n in snap["nodes"] if n["role"] == "gateway")
        self.assertFalse(gw["present"])
        self.assertEqual(gw["checks"], [])
        link = next(link for link in snap["links"] if link["from"] == "gateway")
        self.assertFalse(link["present"])

    def test_a_disabled_gateway_link_is_not_treated_as_linked(self) -> None:
        self.write_config(EDGE_IP="192.0.2.10", MAILBOX_IP="192.0.2.11")
        self.gw.write_text('GATEWAY_HOST="192.0.2.12"\nGATEWAY_ENABLED=0\n')
        snap = asyncio.run(md.gather())
        self.assertFalse(snap["gateway_linked"])

    def test_a_linked_gateway_is_probed_on_the_relay_port(self) -> None:
        self.write_config(EDGE_IP="192.0.2.10", MAILBOX_IP="192.0.2.11")
        self.gw.write_text('GATEWAY_HOST="192.0.2.12"\nGATEWAY_ENABLED=1\n')
        snap = asyncio.run(md.gather())
        self.assertTrue(snap["gateway_linked"])
        gw = next(n for n in snap["nodes"] if n["role"] == "gateway")
        self.assertEqual([c["port"] for c in gw["checks"]], [26])

    def test_a_mailbox_with_no_address_is_not_reported_healthy(self) -> None:
        """Every probe against an empty host returns False, and `all([])` on an
        empty check list would otherwise make a missing machine look perfect."""
        self.write_config(EDGE_IP="192.0.2.10")
        snap = asyncio.run(md.gather())
        mb = next(n for n in snap["nodes"] if n["role"] == "mailbox")
        self.assertFalse(mb["present"])
        self.assertFalse(mb["ok"])
        self.assertFalse(snap["healthy"])

    def test_the_links_are_the_ports_mail_actually_uses(self) -> None:
        self.write_config(EDGE_IP="192.0.2.10", MAILBOX_IP="192.0.2.11")
        snap = asyncio.run(md.gather())
        mb = next(n for n in snap["nodes"] if n["role"] == "mailbox")
        self.assertEqual([c["port"] for c in mb["checks"]], [389, 7025])
        edge = next(n for n in snap["nodes"] if n["role"] == "edge")
        self.assertEqual([c["port"] for c in edge["checks"]], [25, 443, 587, 993])


class NothingHereIsAClusterTests(unittest.TestCase):
    def test_the_module_never_mentions_replication(self) -> None:
        """Prose in the docstring explains why it is absent; code that consults
        any of it would mean the page is back to describing a pair."""
        src = (
            REPO / "console/backend/kin_privhelper/multi_deployment.py"
        ).read_text(encoding="utf-8")
        body = src.split('"""', 2)[2]
        for word in ("drbdadm", "crm_mon", "pcs ", "qdevice", "stonith", "sbd"):
            self.assertNotIn(word, body, f"{word!r} has no place in a multi deployment")

    def test_the_status_probe_short_circuits_before_any_pacemaker_call(self) -> None:
        """pcs, crm_mon and drbdadm are not installed on these machines. Ten
        failing subprocess calls per refresh produced the snapshot that made the
        Cluster page call three machines a single unhealthy server."""
        src = (
            REPO / "console/backend/kin_privhelper/maintenance.py"
        ).read_text(encoding="utf-8")
        head = src.index("async def gather_status()")
        first_pcs = src.index("_pcs_nodes_text()", head)
        branch = src.index('if _saved_topology() == "split":', head)
        self.assertLess(branch, first_pcs)
        # ...and it must RETURN there, not fall through.
        self.assertIn("return {", src[branch:first_pcs])


if __name__ == "__main__":
    unittest.main()
