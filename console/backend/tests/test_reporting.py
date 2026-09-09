"""Mail reports and CSV export.

The Monitoring tab answers "what is happening now". A report answers "what
happened last month" - asked once a month, and until now it meant leaving the
console or not answering at all.

The parts worth testing hardest are the ones that are silently wrong rather
than broken: a month that runs on UTC days when the appliance is in Jakarta, a
ratio that divides two counters which do not divide, and a CSV that a
spreadsheet opens with the columns shifted.
"""

from __future__ import annotations

import csv
import io
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from kin_console import reporting as R
from kin_console.monitoring import MonitoringError


class _TZ:
    """Run a block with the appliance in a specific timezone."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.old = os.environ.get("TZ")

    def __enter__(self) -> None:
        os.environ["TZ"] = self.name
        time.tzset()

    def __exit__(self, *exc: object) -> None:
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()


class PeriodBounds(unittest.TestCase):
    def test_last_month_is_the_whole_previous_month(self) -> None:
        with _TZ("Asia/Jakarta"):
            now = datetime(2026, 9, 5, 13, 30).astimezone()
            start, end, label = R.period_bounds("last_month", now)
            self.assertEqual(label, "August 2026")
            self.assertEqual((start.year, start.month, start.day), (2026, 8, 1))
            self.assertEqual((end.year, end.month, end.day), (2026, 9, 1))
            self.assertEqual((start.hour, start.minute), (0, 0))

    def test_days_are_the_appliance_local_days_not_utc(self) -> None:
        # A monthly report that ends at 07:00 on the 1st because the server
        # reasoned in UTC is wrong in a way nobody would think to check.
        with _TZ("Asia/Jakarta"):
            now = datetime(2026, 9, 5, 13, 30).astimezone()
            start, _end, _label = R.period_bounds("last_month", now)
            self.assertEqual(start.utcoffset(), timedelta(hours=7))
            self.assertEqual(start.strftime("%H:%M"), "00:00")

    def test_this_month_ends_now_not_at_the_month_boundary(self) -> None:
        # Padding the rest of the month with zeros would read as an outage.
        with _TZ("Asia/Jakarta"):
            now = datetime(2026, 9, 5, 13, 30).astimezone()
            start, end, label = R.period_bounds("this_month", now)
            self.assertEqual(label, "September 2026")
            self.assertEqual(end, now)
            self.assertEqual(start.day, 1)

    def test_january_rolls_back_to_december(self) -> None:
        with _TZ("Asia/Jakarta"):
            now = datetime(2026, 1, 9, 8, 0).astimezone()
            start, end, label = R.period_bounds("last_month", now)
            self.assertEqual(label, "December 2025")
            self.assertEqual((start.year, start.month), (2025, 12))
            self.assertEqual((end.year, end.month, end.day), (2026, 1, 1))

    def test_a_leap_february_is_29_days(self) -> None:
        with _TZ("UTC"):
            now = datetime(2028, 3, 4, 12, 0).astimezone()
            start, end, _ = R.period_bounds("last_month", now)
            self.assertEqual((end - start).days, 29)

    def test_rolling_windows(self) -> None:
        with _TZ("Asia/Jakarta"):
            now = datetime(2026, 9, 5, 13, 30).astimezone()
            for period, days in (("last_7d", 7), ("last_30d", 30), ("last_90d", 90)):
                with self.subTest(period=period):
                    start, end, label = R.period_bounds(period, now)
                    self.assertIn(str(days), label)
                    self.assertEqual(start.strftime("%H:%M"), "00:00")
                    self.assertEqual(end, now)
                    # Inclusive of today, so N-1 whole days plus today.
                    self.assertEqual((_midnight(now) - start).days, days - 1)

    def test_an_unknown_period_is_refused_by_name(self) -> None:
        with self.assertRaises(MonitoringError) as ctx:
            R.period_bounds("last_year", datetime.now().astimezone())
        self.assertIn("last_year", str(ctx.exception))
        self.assertIn("last_month", str(ctx.exception))

    def test_an_empty_period_is_the_default(self) -> None:
        now = datetime.now().astimezone()
        self.assertEqual(
            R.period_bounds("", now)[2], R.period_bounds(R.DEFAULT_PERIOD, now)[2]
        )


def _midnight(when: datetime) -> datetime:
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


class DaylightSaving(unittest.TestCase):
    """Nothing here shows up in Jakarta, which is why it needed looking for.

    `datetime.astimezone()` attaches the offset in force at that instant and
    keeps it, so `.replace(day=1)` on a date in April carried April's offset
    back to March and a monthly report began and ended at 01:00. And
    subtracting two aware datetimes in the same zone is WALL-CLOCK arithmetic:
    it calls a 743-hour March "31 days", so the totals window reached an hour
    further back than the period and counted that hour twice.
    """

    def _report_month(self, zone: str) -> tuple[datetime, datetime, str]:
        with _TZ(zone):
            import importlib

            importlib.reload(R)
            now = datetime(2026, 4, 10, 12, 0, tzinfo=R.appliance_zone())
            return R.period_bounds("last_month", now)

    def tearDown(self) -> None:
        import importlib

        importlib.reload(R)

    def test_month_boundaries_use_the_offset_of_their_own_date(self) -> None:
        start, end, _ = self._report_month("Europe/London")
        self.assertEqual(start.strftime("%H:%M"), "00:00")
        self.assertEqual(end.strftime("%H:%M"), "00:00")
        self.assertEqual(start.utcoffset(), timedelta(0), "March in London is GMT")
        self.assertEqual(end.utcoffset(), timedelta(hours=1), "April is BST")

    def test_the_totals_window_is_real_elapsed_time(self) -> None:
        for zone, hours in (
            ("Europe/London", 743),
            ("America/New_York", 743),
            ("Asia/Jakarta", 744),
        ):
            with self.subTest(zone=zone):
                start, end, _ = self._report_month(zone)
                with _TZ(zone):
                    params = R.build_total_params("accepted", start, end)
                window = int(params["query"].split("[")[1].split("s]")[0])
                self.assertEqual(window, hours * 3600)

    def test_a_no_dst_appliance_is_unaffected(self) -> None:
        start, end, label = self._report_month("Asia/Jakarta")
        self.assertEqual(label, "March 2026")
        self.assertEqual(start.utcoffset(), timedelta(hours=7))
        self.assertEqual(end.utcoffset(), timedelta(hours=7))

    def test_the_zone_comes_from_the_appliance_not_a_fixed_offset(self) -> None:
        with _TZ("Europe/London"):
            import importlib

            importlib.reload(R)
            zone = R.appliance_zone()
            # A real zone answers differently on either side of a transition.
            self.assertNotEqual(
                datetime(2026, 1, 15, tzinfo=zone).utcoffset(),
                datetime(2026, 7, 15, tzinfo=zone).utcoffset(),
            )

    def test_an_unknown_zone_name_falls_back_rather_than_raising(self) -> None:
        with _TZ("Not/AZone"):
            import importlib

            importlib.reload(R)
            self.assertIsNotNone(R.appliance_zone())
            # And the report still builds.
            start, end, _ = R.period_bounds("last_7d", R.now_local())
            self.assertLess(start.timestamp(), end.timestamp())


class QueryBuilders(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = _TZ("Asia/Jakarta")
        self.tz.__enter__()
        self.now = datetime(2026, 9, 5, 13, 30).astimezone()
        self.start, self.end, _ = R.period_bounds("last_month", self.now)

    def tearDown(self) -> None:
        self.tz.__exit__()

    def test_the_daily_query_steps_one_day(self) -> None:
        params = R.build_daily_params("accepted", self.start, self.end)
        self.assertEqual(params["step"], str(R.DAY_SECONDS))
        self.assertIn("[1d]", params["query"])

    def test_the_first_evaluation_is_the_end_of_the_first_day(self) -> None:
        # increase(x[1d]) evaluated at `start` would report the day BEFORE the
        # period, so the report would open with a day that is not in it.
        params = R.build_daily_params("accepted", self.start, self.end)
        first = datetime.fromtimestamp(float(params["start"]), tz=timezone.utc)
        self.assertEqual(first, (self.start + timedelta(days=1)).astimezone(timezone.utc))

    def test_the_total_is_one_window_not_a_sum_of_days(self) -> None:
        # The last day of `this_month` is partial; summing daily buckets would
        # either miss it or count a whole day of it.
        params = R.build_total_params("accepted", self.start, self.end)
        self.assertIn("time", params)
        self.assertNotIn("step", params)
        window = int((self.end - self.start).total_seconds())
        self.assertIn(f"[{window}s]", params["query"])

    def test_every_report_series_builds(self) -> None:
        for metric in R.REPORT_COLUMNS:
            with self.subTest(metric=metric):
                daily = R.build_daily_params(metric, self.start, self.end)
                total = R.build_total_params(metric, self.start, self.end)
                self.assertNotIn("$w", daily["query"])
                self.assertNotIn("$w", total["query"])

    def test_counters_use_increase_and_gauges_use_max_over_time(self) -> None:
        # A counter summed with max_over_time reports its lifetime value; a
        # gauge summed with increase reports nonsense.
        for metric, (_h, query, kind) in R.REPORT_SERIES.items():
            with self.subTest(metric=metric):
                if kind == "counter":
                    self.assertIn("increase(", query)
                else:
                    self.assertIn("max_over_time(", query)

    def test_an_unknown_series_is_refused(self) -> None:
        for build in (R.build_daily_params, R.build_total_params):
            with self.subTest(build=build.__name__):
                with self.assertRaises(MonitoringError):
                    build("not_a_metric", self.start, self.end)

    def test_a_backwards_period_is_refused(self) -> None:
        with self.assertRaises(MonitoringError):
            R.build_daily_params("accepted", self.end, self.start)


class DailyRows(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = _TZ("Asia/Jakarta")
        self.tz.__enter__()
        self.now = datetime(2026, 9, 5, 13, 30).astimezone()
        self.start, self.end, _ = R.period_bounds("last_month", self.now)

    def tearDown(self) -> None:
        self.tz.__exit__()

    def _points(self, values: list[float]) -> list[tuple[float, float | None]]:
        first = self.start + timedelta(days=1)
        return [((first + timedelta(days=i)).timestamp(), v) for i, v in enumerate(values)]

    def test_a_full_month_produces_one_row_per_day(self) -> None:
        rows = R.daily_rows({"accepted": self._points([1] * 31)}, start=self.start, end=self.end)
        self.assertEqual(len(rows), 31)
        self.assertEqual(rows[0]["date"], "2026-08-01")
        self.assertEqual(rows[-1]["date"], "2026-08-31")

    def test_a_point_is_labelled_with_the_day_it_summarises(self) -> None:
        # The sample sits at the END of its day; labelling it with that
        # timestamp's date would shift the whole report forward by one day.
        rows = R.daily_rows({"accepted": self._points([5])}, start=self.start, end=self.end)
        self.assertEqual(rows[0]["date"], "2026-08-01")

    def test_rows_are_oldest_first(self) -> None:
        rows = R.daily_rows({"accepted": self._points([1] * 5)}, start=self.start, end=self.end)
        self.assertEqual([r["date"] for r in rows], sorted(r["date"] for r in rows))

    def test_counts_are_whole_numbers(self) -> None:
        # increase() interpolates, so a count of messages arrives fractional.
        rows = R.daily_rows({"accepted": self._points([1200.4, 998.6])}, start=self.start, end=self.end)
        self.assertEqual(rows[0]["accepted"], 1200)
        self.assertEqual(rows[1]["accepted"], 999)

    def test_a_metric_with_no_sample_is_zero_not_blank(self) -> None:
        # A hole in a spreadsheet reads as missing data; it usually means a
        # quiet Sunday.
        rows = R.daily_rows({"accepted": self._points([7])}, start=self.start, end=self.end)
        for key in R.REPORT_COLUMNS:
            self.assertIn(key, rows[0])
        self.assertEqual(rows[0]["bounced"], 0)

    def test_a_null_sample_survives_as_null(self) -> None:
        rows = R.daily_rows({"accepted": self._points([None])}, start=self.start, end=self.end)
        self.assertIsNone(rows[0]["accepted"])

    def test_no_data_at_all_is_an_empty_report_not_a_crash(self) -> None:
        rows = R.daily_rows({}, start=self.start, end=self.end)
        self.assertEqual(rows, [])
        self.assertEqual(R.summarise(rows)["days"], 0)
        self.assertIsNone(R.summarise(rows)["busiest_day"])

    def test_the_busiest_day_is_the_one_with_most_accepted(self) -> None:
        rows = R.daily_rows(
            {"accepted": self._points([10, 90, 30])}, start=self.start, end=self.end
        )
        summary = R.summarise(rows)
        self.assertEqual(summary["busiest_day"], "2026-08-02")
        self.assertEqual(summary["busiest_day_accepted"], 90)


class OutcomeRates(unittest.TestCase):
    def test_a_normal_month(self) -> None:
        rates = R.outcome_rates({"delivered_in": 9000, "delivered_out": 900, "bounced": 100})
        self.assertEqual(rates["delivered_pct"], 99.0)
        self.assertEqual(rates["bounced_pct"], 1.0)

    def test_multi_recipient_mail_cannot_exceed_100_percent(self) -> None:
        # The first version divided delivered by accepted. Delivery counters
        # are per RECIPIENT and accepted is per MESSAGE, so a message to three
        # people took the ratio over 100 and it was clamped, hiding the fact
        # that the two numbers do not divide.
        rates = R.outcome_rates(
            {"accepted": 1000, "delivered_in": 2400, "delivered_out": 100, "bounced": 0}
        )
        self.assertEqual(rates["delivered_pct"], 100.0)

    def test_deferrals_are_not_failures(self) -> None:
        # Postfix logs status=deferred on every retry. A message that retried
        # nine times and then arrived is one delivery, not nine failures.
        rates = R.outcome_rates(
            {"delivered_in": 100, "delivered_out": 0, "bounced": 0, "deferred": 9999}
        )
        self.assertEqual(rates["delivered_pct"], 100.0)

    def test_a_quiet_month_is_not_a_zero_percent_month(self) -> None:
        rates = R.outcome_rates({"delivered_in": 0, "delivered_out": 0, "bounced": 0})
        self.assertIsNone(rates["delivered_pct"])
        self.assertIsNone(rates["bounced_pct"])

    def test_the_two_shares_add_up(self) -> None:
        rates = R.outcome_rates({"delivered_in": 700, "delivered_out": 0, "bounced": 300})
        self.assertAlmostEqual(
            (rates["delivered_pct"] or 0) + (rates["bounced_pct"] or 0), 100.0, places=1
        )

    def test_the_deferred_heading_says_it_counts_events(self) -> None:
        self.assertIn("event", R.REPORT_SERIES["deferred"][0].lower())


def _table(body: str) -> list[list[str]]:
    """The data rows of an export, with the provenance header skipped.

    The export now opens with "#" comment lines naming the appliance, the
    window and every metric in the file. A spreadsheet imports them as text and
    a reader gets the context that a wall of numbers cannot carry on its own;
    these tests care about the table underneath.
    """
    table = "\r\n".join(ln for ln in body.splitlines() if not ln.startswith("#"))
    return [r for r in csv.reader(io.StringIO(table)) if r]


class CsvOutput(unittest.TestCase):
    def test_the_header_matches_the_columns(self) -> None:
        body = R.to_csv([], R.report_csv_columns())
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0][0], "Date")
        self.assertEqual(len(rows[0]), len(R.REPORT_COLUMNS) + 1)

    def test_a_comma_in_a_value_does_not_shift_the_columns(self) -> None:
        # The reason this uses the csv module rather than joining strings.
        body = R.to_csv(
            [{"date": "2026-08-01", "accepted": 'a,b"c'}], R.report_csv_columns()
        )
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(len(rows[1]), len(rows[0]))
        self.assertEqual(rows[1][1], 'a,b"c')

    def test_missing_values_are_empty_cells(self) -> None:
        body = R.to_csv([{"date": "2026-08-01"}], R.report_csv_columns())
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[1][1], "")

    def test_lines_end_crlf_for_spreadsheets(self) -> None:
        body = R.to_csv([{"date": "2026-08-01", "accepted": 1}], R.report_csv_columns())
        self.assertIn("\r\n", body)

    def test_metric_samples_export_long_format(self) -> None:
        charts = [
            {
                "metric": "cpu",
                "label": "CPU used",
                "unit": "percent",
                "series": [
                    {"instance": "127.0.0.1:9100", "points": [[1757000000, 41.5], [1757000015, None]]}
                ],
            }
        ]
        body = R.series_csv(charts)
        rows = _table(body)
        self.assertEqual(
            rows[0],
            [
                "timestamp_utc",
                "timestamp_local",
                "metric",
                "label",
                "unit",
                "unit_description",
                "instance",
                "value",
            ],
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][2], "cpu")
        self.assertEqual(rows[1][7], "41.5")
        # A gap must export as empty, not as the string "None".
        self.assertEqual(rows[2][7], "")
        # The raw token stays for machines; the words are for people.
        self.assertEqual(rows[1][4], "percent")
        self.assertEqual(rows[1][5], "percent")

    def test_a_gap_never_exports_as_the_word_none(self) -> None:
        body = R.series_csv(
            [{"metric": "m", "label": "M", "unit": "number",
              "series": [{"instance": "i", "points": [[1757000000, None]]}]}]
        )
        self.assertNotIn("None", body)

    def test_malformed_points_are_skipped_not_fatal(self) -> None:
        body = R.series_csv(
            [{"metric": "m", "label": "M", "unit": "number",
              "series": [{"instance": "i", "points": [[1757000000, 1], [], None, [1]]}]}]
        )
        self.assertEqual(len(_table(body)), 2)

    def test_an_empty_export_is_still_a_valid_csv(self) -> None:
        body = R.series_csv([])
        self.assertEqual(len(_table(body)), 1, "header row only")
        # Even with nothing to show, the file says what it is.
        self.assertIn("# KIN Mail monitoring samples", body)

    def test_filenames_sort_and_carry_no_spaces(self) -> None:
        with _TZ("Asia/Jakarta"):
            start = datetime(2026, 8, 1).astimezone()
            end = datetime(2026, 9, 1).astimezone()
            name = R.csv_filename("kin-mail-report-last_month", start, end)
        self.assertNotIn(" ", name)
        self.assertTrue(name.endswith(".csv"))
        self.assertIn("20260801", name)
        self.assertIn("20260901", name)

    def test_a_filename_cannot_carry_path_separators(self) -> None:
        name = R.csv_filename("../../etc/passwd")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)


class BuildReport(unittest.TestCase):
    """The whole thing, against a stub Prometheus."""

    def _fake_get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("query_range"):
            start = float(params["start"])
            end = float(params["end"])
            step = float(params["step"])
            values, t, i = [], start, 0
            while t <= end + 0.5:
                values.append([t, str(100 + i)])
                t += step
                i += 1
            return {
                "status": "success",
                "data": {"resultType": "matrix",
                         "result": [{"metric": {"instance": "n"}, "values": values}]},
            }
        return {
            "status": "success",
            "data": {"resultType": "vector",
                     "result": [{"metric": {}, "value": [float(params["time"]), "4321.7"]}]},
        }

    def test_a_full_month_report(self) -> None:
        with _TZ("Asia/Jakarta"), mock.patch.object(R, "_get", self._fake_get):
            report = R.build_report("last_month", now=datetime(2026, 9, 5, 13, 30).astimezone())
        self.assertEqual(report["label"], "August 2026")
        self.assertEqual(len(report["daily"]), 31)
        self.assertEqual(report["totals"]["accepted"], 4322)
        self.assertEqual(report["unavailable"], [])
        self.assertEqual(report["days"], 31)

    def test_a_dead_query_names_itself_instead_of_blanking_the_report(self) -> None:
        def half_dead(path: str, params: dict[str, str]) -> dict[str, object]:
            if "bounced" in params.get("query", ""):
                raise MonitoringError("boom")
            return self._fake_get(path, params)

        with _TZ("Asia/Jakarta"), mock.patch.object(R, "_get", half_dead):
            report = R.build_report("last_month", now=datetime(2026, 9, 5, 13, 30).astimezone())
        self.assertIn("bounced", report["unavailable"])
        self.assertEqual(report["totals"]["accepted"], 4322, "the rest still came back")

    def test_the_report_carries_its_own_provenance(self) -> None:
        with _TZ("Asia/Jakarta"), mock.patch.object(R, "_get", self._fake_get):
            report = R.build_report("last_month", now=datetime(2026, 9, 5, 13, 30).astimezone())
        for key in ("period", "label", "start", "end", "generated_at", "columns"):
            self.assertIn(key, report)
        self.assertTrue(str(report["start"]).startswith("2026-08-01"))

    def test_an_unknown_period_raises_rather_than_defaulting(self) -> None:
        with mock.patch.object(R, "_get", self._fake_get):
            with self.assertRaises(MonitoringError):
                R.build_report("last_decade")


if __name__ == "__main__":
    unittest.main()
