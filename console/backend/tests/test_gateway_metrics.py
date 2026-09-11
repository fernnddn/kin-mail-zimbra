"""The gateway reachability collector.

The gateway is the one component whose failure stops every message while the
appliance still looks healthy: Zimbra running, CPU idle, the queue quietly
climbing. Nothing else in the Monitoring tab would say why, so this collector
exists to say it.

What matters here is that it is honest in the two directions that are easy to
get wrong. It must publish zeros rather than nothing when there is no gateway,
because a console cannot tell "not linked" from "the collector never ran" if
the file is simply absent. And it must never report a gateway as up because a
config file said so - the numbers have to come from a socket.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "monitoring/gateway/kin-mail-gateway-metrics.py"


def _load():
    spec = importlib.util.spec_from_file_location("kin_gateway_metrics", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.conf = self.root / "mail-gateway.conf"
        self.state = self.root / "state.json"
        self.mod.GATEWAY_CONF = self.conf
        self.mod.GATEWAY_STATE = self.state

    def _link(self, host: str = "192.0.2.9", enabled: str = "1", phase: str = "applied") -> None:
        self.conf.write_text(
            f'GATEWAY_ENABLED={enabled}\nGATEWAY_HOST="{host}"\n', encoding="utf-8"
        )
        self.state.write_text(json.dumps({"phase": phase}), encoding="utf-8")

    def _render(self, **kw) -> str:
        with mock.patch.object(self.mod, "probe", return_value=kw.pop("probe", (True, 0.01))), \
             mock.patch.object(self.mod, "zimbra_relay_host", return_value=kw.pop("relay", "")):
            out = self.root / "out"
            self.mod.main(["--textfile-dir", str(out)])
            return (out / self.mod.PROM_NAME).read_text(encoding="utf-8")

    # -- honesty when nothing is configured ---------------------------------
    def test_with_no_gateway_it_publishes_zeros_rather_than_nothing(self) -> None:
        body = self._render()
        self.assertIn("kin_mail_gateway_linked 0", body)
        self.assertIn('kin_mail_gateway_up{host="none",port="26"} 0', body)
        self.assertIn('kin_mail_gateway_up{host="none",port="25"} 0', body)

    def test_with_no_gateway_it_does_not_probe_anything(self) -> None:
        with mock.patch.object(self.mod, "probe") as probe:
            self.mod.main(["--textfile-dir", str(self.root / "o")])
        probe.assert_not_called()

    # -- honesty when one is ------------------------------------------------
    def test_a_reachable_gateway_reports_up(self) -> None:
        self._link()
        body = self._render(probe=(True, 0.02))
        self.assertIn('kin_mail_gateway_up{host="192.0.2.9",port="26"} 1', body)
        self.assertIn("kin_mail_gateway_linked 1", body)

    def test_an_unreachable_gateway_reports_down_even_though_it_is_configured(self) -> None:
        """The whole point. Configured and reachable are different facts."""
        self._link()
        body = self._render(probe=(False, 5.0))
        self.assertIn("kin_mail_gateway_linked 1", body)
        self.assertIn('kin_mail_gateway_up{host="192.0.2.9",port="26"} 0', body)

    def test_a_recorded_but_never_applied_link_is_not_linked(self) -> None:
        self._link(phase="never-applied")
        body = self._render()
        self.assertIn("kin_mail_gateway_linked 0", body)

    def test_a_disabled_link_is_not_linked(self) -> None:
        self._link(enabled="0")
        body = self._render()
        self.assertIn("kin_mail_gateway_linked 0", body)

    def test_relay_drift_is_visible_because_it_is_read_from_zimbra(self) -> None:
        # Sourcing this from our own state file would make it impossible to
        # see the interesting failure: the two disagreeing.
        self._link()
        self.assertIn("kin_mail_gateway_relay_configured 1", self._render(relay="192.0.2.9:26"))
        self.assertIn("kin_mail_gateway_relay_configured 0", self._render(relay="192.0.2.8:26"))
        self.assertIn("kin_mail_gateway_relay_configured 0", self._render(relay=""))

    def test_a_deliberate_port_change_is_not_reported_as_drift(self) -> None:
        self._link()
        self.assertIn("kin_mail_gateway_relay_configured 1", self._render(relay="192.0.2.9:2525"))

    # -- the file itself ----------------------------------------------------
    def test_the_config_file_is_parsed_and_not_executed(self) -> None:
        marker = self.root / "pwned"
        self.conf.write_text(
            f'GATEWAY_HOST="192.0.2.9"\n$(touch {marker})\nGATEWAY_ENABLED=1\n', encoding="utf-8"
        )
        self.assertEqual(self.mod.read_config()["GATEWAY_HOST"], "192.0.2.9")
        self.assertFalse(marker.exists())

    def test_the_prom_file_is_written_atomically_and_world_readable(self) -> None:
        self._link()
        out = self.root / "textfile"
        with mock.patch.object(self.mod, "probe", return_value=(True, 0.01)), \
             mock.patch.object(self.mod, "zimbra_relay_host", return_value="192.0.2.9:26"):
            self.mod.main(["--textfile-dir", str(out)])
        prom = out / self.mod.PROM_NAME
        self.assertEqual(prom.stat().st_mode & 0o777, 0o644)
        # node_exporter rejects a directory littered with half-written files.
        self.assertEqual(list(out.glob("*.tmp")), [])

    def test_a_hostname_with_quotes_cannot_break_the_metric_format(self) -> None:
        """Defence in depth: the console already refuses such a host.

        A quote that reached the label value unescaped would end the label
        early and make the whole .prom file unparseable, which takes out every
        other metric on the appliance, not just this one.
        """
        self._link(host='gw"evil')
        body = self._render()
        seen = False
        for line in body.splitlines():
            if not line.startswith("kin_mail_gateway_up{"):
                continue
            seen = True
            self.assertIn(chr(92) + chr(34), line, "the quote should be escaped, not dropped")
            # Four structural quotes: two around each label value. Any further
            # quote must be an escaped one.
            unescaped = len(re.findall('(?<!' + chr(92) * 2 + ')' + chr(34), line))
            self.assertEqual(unescaped, 4, line)
        self.assertTrue(seen)

    def test_a_corrupt_state_file_reads_as_not_applied(self) -> None:
        self.conf.write_text('GATEWAY_ENABLED=1\nGATEWAY_HOST="192.0.2.9"\n', encoding="utf-8")
        self.state.write_text("{ truncated", encoding="utf-8")
        self.assertIn("kin_mail_gateway_linked 0", self._render())


class ItIsWiredIntoTheMonitoringRole(unittest.TestCase):
    """A collector nobody installs is not monitoring."""

    def setUp(self) -> None:
        raw = (REPO / "ansible/roles/monitoring_stack/tasks/main.yml").read_text(
            encoding="utf-8"
        )
        # Comments stripped before anything is asserted. The comment above the
        # systemd task in that file explains at length why systemd_service must
        # never be used, so a naive search for the string matches the warning
        # rather than the mistake. Three guards in this repository have already
        # passed by matching their own prose.
        self.tasks = "\n".join(
            ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")
        )

    def test_the_role_installs_and_starts_it(self) -> None:
        self.assertIn("kin-mail-gateway-metrics.py", self.tasks)
        self.assertIn("kin-mail-gateway.timer", self.tasks)

    def test_it_uses_the_module_name_that_exists_on_the_appliance(self) -> None:
        # ansible-core 2.12 ships on Ubuntu 22.04 and has no systemd_service.
        # This exact mistake broke monitoring on every single-server appliance.
        self.assertNotIn("systemd_service", self.tasks)

    def test_the_role_ships_the_file_it_copies(self) -> None:
        src = REPO / "ansible/roles/monitoring_stack/files/kin-mail-gateway-metrics.py"
        self.assertTrue(src.exists(), "the copy source is missing")
        self.assertTrue(src.resolve().is_file(), "the symlink does not resolve to a real file")


if __name__ == "__main__":
    unittest.main()


class TheMonitoringTabMustBeAbleToSayTheGatewayIsDown(unittest.TestCase):
    """Four states, and collapsing any two of them misleads an operator.

    The gateway failing looks exactly like perfect health from every other
    card on that page. If this reader cannot tell "no collector" from "not
    linked" from "down", the page is worse than silent: it is confident.
    """

    def setUp(self) -> None:
        from kin_console import monitoring as M

        self.M = M
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patch = mock.patch.object(M, "TEXTFILE_DIR", str(self.dir))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, body: str, *, age: float = 0.0) -> None:
        path = self.dir / self.M.GATEWAY_PROM_FILE
        path.write_text(body, encoding="utf-8")
        if age:
            stamp = path.stat().st_mtime - age
            os.utime(path, (stamp, stamp))

    def test_no_file_at_all_is_unknown_not_healthy_and_not_down(self) -> None:
        got = self.M.gateway_health()
        self.assertFalse(got["known"])
        self.assertFalse(got["linked"])
        self.assertEqual(got["ports"], {})

    def test_an_empty_file_is_unknown(self) -> None:
        self._write("")
        self.assertFalse(self.M.gateway_health()["known"])

    def test_a_healthy_gateway_reads_as_up_on_both_ports(self) -> None:
        self._write(
            'kin_mail_gateway_linked 1\n'
            'kin_mail_gateway_up{host="192.0.2.9",port="25"} 1\n'
            'kin_mail_gateway_up{host="192.0.2.9",port="26"} 1\n'
            'kin_mail_gateway_relay_configured 1\n'
        )
        got = self.M.gateway_health()
        self.assertTrue(got["linked"])
        self.assertEqual(got["ports"], {"25": True, "26": True})
        self.assertEqual(got["host"], "192.0.2.9")
        self.assertTrue(got["relay_configured"])

    def test_a_down_relay_port_is_visible(self) -> None:
        self._write(
            'kin_mail_gateway_linked 1\n'
            'kin_mail_gateway_up{host="192.0.2.9",port="25"} 1\n'
            'kin_mail_gateway_up{host="192.0.2.9",port="26"} 0\n'
        )
        self.assertIs(self.M.gateway_health()["ports"]["26"], False)

    def test_a_stale_file_does_not_report_a_live_up(self) -> None:
        """The collector stopping is itself news, not something to paper over."""
        self._write(
            'kin_mail_gateway_linked 1\n'
            'kin_mail_gateway_up{host="192.0.2.9",port="26"} 1\n',
            age=self.M.GATEWAY_STALE_AFTER_SEC + 60,
        )
        got = self.M.gateway_health()
        self.assertTrue(got["stale"])
        self.assertEqual(got["ports"], {}, "a stale reading must not be shown as up")

    def test_a_fresh_file_is_not_stale(self) -> None:
        self._write('kin_mail_gateway_linked 0\n')
        self.assertFalse(self.M.gateway_health()["stale"])

    def test_garbage_in_the_file_does_not_raise(self) -> None:
        self._write("this is not a prometheus exposition format\n\x00\n")
        self.M.gateway_health()

    def test_it_is_part_of_the_host_facts_the_tab_reads(self) -> None:
        self.assertIn("mail_gateway", self.M.host_facts())

    def test_every_collector_directory_is_created_by_this_role(self) -> None:
        """A role that relies on another role having run works on one topology.

        /usr/local/libexec does not exist on a stock Ubuntu and copy does not
        create parents. It used to be created by corosync_qdevice, which runs
        only on the two-server path, so monitoring worked on 2vm by accident
        and failed on every single server this company actually sells.
        """
        import yaml

        tasks = yaml.safe_load(
            (REPO / "ansible/roles/monitoring_stack/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        made = {
            str(t.get("ansible.builtin.file", {}).get("path", ""))
            for t in tasks
            if t.get("ansible.builtin.file", {}).get("state") == "directory"
        }
        self.assertIn("{{ monitoring_stack_gateway_script | dirname }}", made)
        self.assertIn("{{ monitoring_stack_mailflow_script | dirname }}", made)

    def test_the_directory_is_created_before_anything_is_copied_into_it(self) -> None:
        import yaml

        tasks = yaml.safe_load(
            (REPO / "ansible/roles/monitoring_stack/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        names = [str(t.get("name", "")) for t in tasks]
        mkdir_at = next(
            i for i, t in enumerate(tasks)
            if t.get("ansible.builtin.file", {}).get("path", "")
            == "{{ monitoring_stack_gateway_script | dirname }}"
        )
        copy_at = [
            i for i, t in enumerate(tasks)
            if "kin-mail-gateway-metrics.py" in str(t.get("ansible.builtin.copy", {}).get("src", ""))
        ]
        self.assertTrue(copy_at, f"the gateway collector is never copied: {names}")
        self.assertLess(mkdir_at, copy_at[0])
