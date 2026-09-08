"""A report must never be a cleaner answer than the data behind it.

Reporting is the surface the business cares about most, and it is the one that
fails most quietly. Prometheus answers a query about a metric it has never seen
with a successful, EMPTY result, not an error. So an appliance where the
mail-flow collector was never installed produces a report with no exception
anywhere in it: every total is None, the daily table has no rows, and the old
payload said `unavailable: []`, which reads as "nothing went wrong".

A quiet month and an absent pipeline looked identical. These tests keep them
apart, and keep the CSV honest: the download used to be built straight from the
daily rows with no freshness attached at all, so exporting was a way to get a
tidier version of the same lie, in a file that outlives the screen and gets
forwarded to somebody who was not there.
"""

from __future__ import annotations

import csv
import io
import unittest

from kin_console import reporting as R


def _report(**over: object) -> dict:
    base: dict = {
        "period": "last_month",
        "totals": {key: 0 for key in R.REPORT_COLUMNS},
        "daily": [{"date": "2026-08-01", **{k: 0 for k in R.REPORT_COLUMNS}}],
        "unavailable": [],
        "no_data": False,
        "mail_flow": {"known": True, "stale": False, "age_seconds": 30, "reason": ""},
        "metrics_install": {"phase": "ok", "installed": True, "reason": ""},
    }
    base.update(over)
    return base


class AbsenceIsNotZero(unittest.TestCase):
    def test_a_healthy_quiet_month_carries_no_warnings(self) -> None:
        # The control. If this ever starts warning, the real warnings stop
        # being read.
        self.assertEqual(R.data_quality_notes(_report()), [])

    def test_no_samples_at_all_says_so(self) -> None:
        notes = R.data_quality_notes(_report(no_data=True))
        self.assertTrue(notes)
        joined = " ".join(notes)
        self.assertIn("not the same as no mail", joined)

    def test_an_absent_collector_is_reported(self) -> None:
        notes = R.data_quality_notes(
            _report(
                mail_flow={
                    "known": False,
                    "stale": False,
                    "reason": "No mail flow metrics file is being collected.",
                }
            )
        )
        self.assertIn("No mail flow metrics file is being collected.", notes)

    def test_a_stale_collector_is_reported(self) -> None:
        notes = R.data_quality_notes(
            _report(
                mail_flow={
                    "known": True,
                    "stale": True,
                    "reason": "The mail flow collector last wrote 90 minutes ago.",
                }
            )
        )
        self.assertIn("The mail flow collector last wrote 90 minutes ago.", notes)

    def test_a_failed_metrics_install_is_reported(self) -> None:
        notes = R.data_quality_notes(
            _report(
                metrics_install={
                    "phase": "failed",
                    "installed": False,
                    "reason": "The metrics install failed (exit 2).",
                }
            )
        )
        self.assertIn("The metrics install failed (exit 2).", notes)

    def test_an_install_still_running_is_reported(self) -> None:
        notes = R.data_quality_notes(
            _report(metrics_install={"phase": "running", "installed": False, "reason": ""})
        )
        self.assertTrue(any("still running" in n for n in notes))

    def test_an_unknown_install_alone_is_not_a_warning(self) -> None:
        # Appliances deployed before the state file existed read back unknown
        # while collecting perfectly well. Warning on that would put a
        # permanent scare on every healthy box.
        notes = R.data_quality_notes(
            _report(metrics_install={"phase": "unknown", "installed": False, "reason": ""})
        )
        self.assertEqual(notes, [])

    def test_unfetchable_columns_are_named_by_their_heading(self) -> None:
        notes = R.data_quality_notes(_report(unavailable=["bounced"]))
        self.assertTrue(any("Bounced" in n for n in notes))
        self.assertTrue(any("blank rather than zero" in n for n in notes))


class TheCsvCarriesTheSameWarnings(unittest.TestCase):
    def _lines(self, report: dict) -> list[str]:
        return R.report_csv(report).splitlines()

    def test_a_healthy_report_exports_a_bare_table(self) -> None:
        lines = self._lines(_report())
        self.assertFalse(
            [ln for ln in lines if ln.startswith("#")],
            "a healthy report must not cry wolf",
        )
        self.assertTrue(lines[0].startswith("Date"))

    def test_every_json_warning_reaches_the_csv(self) -> None:
        # The contract that matters: the file cannot be more reassuring than
        # the screen. Derived from data_quality_notes so a caveat added later
        # is covered without editing this test.
        report = _report(
            no_data=True,
            unavailable=["bounced"],
            mail_flow={"known": False, "stale": False, "reason": "No collector here."},
            metrics_install={
                "phase": "failed",
                "installed": False,
                "reason": "The metrics install failed (exit 2).",
            },
        )
        body = R.report_csv(report)
        for note in R.data_quality_notes(report):
            self.assertIn(note, body)

    def test_the_warnings_come_before_the_numbers(self) -> None:
        body = R.report_csv(_report(no_data=True))
        self.assertTrue(body.startswith("#"), "a warning under the table is a footnote")
        self.assertIn("WARNING", body.splitlines()[0])

    def test_the_table_still_parses_once_comments_are_skipped(self) -> None:
        # The notes must not cost the operator a spreadsheet.
        body = R.report_csv(_report(no_data=True))
        table = "\r\n".join(ln for ln in body.splitlines() if not ln.startswith("#"))
        rows = list(csv.reader(io.StringIO(table)))
        self.assertEqual(rows[0][0], "Date")
        self.assertEqual(len(rows), 2, "header plus the one day")

    def test_a_csv_with_no_rows_at_all_still_explains_itself(self) -> None:
        # The worst case in the field: nothing was ever collected, so there is
        # not even a row to look wrong. An empty table with a confident
        # filename is exactly what gets forwarded.
        body = R.report_csv(_report(daily=[], no_data=True))
        self.assertIn("#", body)
        self.assertIn("no metrics collection", body)


class BuildReportFlagsAbsence(unittest.TestCase):
    def test_all_totals_missing_sets_no_data(self) -> None:
        from unittest import mock

        with mock.patch.object(R, "fetch_daily", lambda *_a, **_k: []), mock.patch.object(
            R, "fetch_total", lambda *_a, **_k: None
        ):
            report = R.build_report("last_month")
        self.assertTrue(report["no_data"])
        self.assertEqual(report["unavailable"], [], "empty is not an error")

    def test_real_traffic_does_not_set_no_data(self) -> None:
        from unittest import mock

        with mock.patch.object(R, "fetch_daily", lambda *_a, **_k: []), mock.patch.object(
            R, "fetch_total", lambda *_a, **_k: 5
        ):
            report = R.build_report("last_month")
        self.assertFalse(report["no_data"])


if __name__ == "__main__":
    unittest.main()
