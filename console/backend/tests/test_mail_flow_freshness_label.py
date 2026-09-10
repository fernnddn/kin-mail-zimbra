"""The freshness probe must find the collector's file however it is labelled.

Phase 14 QA produced two statements on one screen that cannot both be true: a
Reports tab showing real figures, and a warning saying no mail flow metrics
file is being collected on the node. The deploy log for the same appliance
shows the collector installed and its first sample taken.

The probe asked Prometheus for node_textfile_mtime_seconds with an exact match
on the bare filename. node_exporter labels that metric with the file it read,
and whether that is the bare name or the full path under the textfile directory
has differed between versions. An exact match therefore finds nothing on an
appliance that is collecting perfectly well.

Two contradictory statements on one screen are worse than either alone. The
operator cannot tell which to believe, and an honest warning that cries wolf on
a healthy box is one nobody reads on the day it is right.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from kin_console import monitoring as m


def _query_sent() -> str:
    seen: dict[str, str] = {}

    def fake_get(_path: str, params: dict) -> dict:
        seen["q"] = params["query"]
        return {"status": "success", "data": {"result": []}}

    with mock.patch.object(m, "_get", fake_get):
        m.mail_flow_freshness()
    return seen["q"]


class TheProbeMatchesEitherLabelForm(unittest.TestCase):
    def setUp(self) -> None:
        self.query = _query_sent()

    def test_it_uses_a_regex_match_not_equality(self) -> None:
        self.assertIn("file=~", self.query)
        self.assertNotIn(f'file="{m.MAILFLOW_PROM_FILE}"', self.query)

    def test_the_dot_is_escaped(self) -> None:
        # An unescaped dot would match kin_mail_flowXprom as well, which is a
        # different file and would report the wrong thing as fresh.
        self.assertIn(r"kin_mail_flow\.prom", self.query)

    def test_it_matches_both_shapes_and_nothing_else(self) -> None:
        # Prometheus anchors its label regexes, so the pattern is tested here
        # the same way: fully anchored.
        pattern = re.search(r'file=~"([^"]+)"', self.query)
        self.assertIsNotNone(pattern, "no regex matcher in the query")
        rx = re.compile("^" + pattern.group(1) + "$")
        for label in (
            "kin_mail_flow.prom",
            "/var/lib/node_exporter/textfile/kin_mail_flow.prom",
        ):
            with self.subTest(label=label, expect="match"):
                self.assertTrue(rx.match(label), f"{label} should be found")
        for label in (
            "other.prom",
            "kin_mail_flowXprom",
            "notkin_mail_flow.prom",
            "kin_mail_flow.prom.bak",
        ):
            with self.subTest(label=label, expect="no match"):
                self.assertFalse(rx.match(label), f"{label} must not be mistaken for it")

    def test_it_still_asks_for_the_age_not_the_timestamp(self) -> None:
        # The caller turns this into "last wrote N minutes ago", so the query
        # has to return an age.
        self.assertTrue(self.query.startswith("time() - node_textfile_mtime_seconds"))


class AnAbsentCollectorIsStillReported(unittest.TestCase):
    """Making the match tolerant must not make it blind."""

    def test_no_series_still_means_no_collector(self) -> None:
        with mock.patch.object(
            m, "_get", lambda *_a, **_k: {"status": "success", "data": {"result": []}}
        ):
            state = m.mail_flow_freshness()
        self.assertFalse(state["known"])
        self.assertIn("No mail flow metrics file", state["reason"])

    def test_a_fresh_file_is_reported_as_current(self) -> None:
        with mock.patch.object(
            m,
            "_get",
            lambda *_a, **_k: {
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [0, "30"]}]},
            },
        ):
            state = m.mail_flow_freshness()
        self.assertTrue(state["known"])
        self.assertFalse(state["stale"])
        self.assertEqual(state["reason"], "")

    def test_an_old_file_is_still_called_stale(self) -> None:
        old = str(m.MAILFLOW_STALE_AFTER_SEC + 600)
        with mock.patch.object(
            m,
            "_get",
            lambda *_a, **_k: {
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [0, old]}]},
            },
        ):
            state = m.mail_flow_freshness()
        self.assertTrue(state["known"])
        self.assertTrue(state["stale"])
        self.assertIn("minutes ago", state["reason"])


if __name__ == "__main__":
    unittest.main()
