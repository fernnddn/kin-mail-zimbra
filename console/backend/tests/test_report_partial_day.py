"""A report must not print figures above the words "no figures".

The daily table comes from a Prometheus range query, which can only produce a
point at a day BOUNDARY. The totals come from one instant query over the whole
period, which includes the day in progress.

On a freshly deployed appliance those two facts collide: the collector started
today, no completed day has any data, so the table is empty - and the totals are
not. The console printed real numbers above the sentence "No mail figures for
this period" (live QA Phase 17, 12 September 2026). Two contradictory statements
on one screen is worse than either alone, because the operator cannot tell which
to believe and stops believing both.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from kin_console import reporting as R


class ThePartialDayIsQueried(unittest.TestCase):
    def test_a_period_ending_mid_day_asks_for_the_elapsed_part(self) -> None:
        end = R.now_local().replace(hour=16, minute=30, second=0, microsecond=0)
        params = R.build_partial_day_params("accepted", end)
        self.assertIsNotNone(params)
        assert params is not None
        # 16h30m since local midnight.
        self.assertIn(f"{16 * 3600 + 30 * 60}s", params["query"])
        self.assertEqual(params["time"], f"{end.timestamp():.3f}")

    def test_a_period_ending_exactly_at_midnight_has_no_partial_day(self) -> None:
        """The range query has already covered everything; a second row would
        duplicate the last one."""
        end = R.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertIsNone(R.build_partial_day_params("accepted", end))

    def test_an_unknown_series_is_refused(self) -> None:
        with self.assertRaises(R.MonitoringError):
            R.build_partial_day_params("not-a-metric", R.now_local())

    def test_the_query_is_an_instant_one_not_a_range(self) -> None:
        params = R.build_partial_day_params("accepted", R.now_local())
        assert params is not None
        self.assertNotIn("step", params)
        self.assertNotIn("start", params)


class TheTableAgreesWithTheTotals(unittest.TestCase):
    """The whole point: no more numbers-above-"no numbers"."""

    def _report(self, *, daily, total, partial):
        with mock.patch.object(R, "fetch_daily", return_value=daily), mock.patch.object(
            R, "fetch_total", return_value=total
        ), mock.patch.object(R, "fetch_partial_day", return_value=partial):
            return R.build_report("this_month")

    def test_a_brand_new_appliance_gets_a_row_for_today(self) -> None:
        # No completed day has data; today does. This is the reported case.
        rep = self._report(daily=[], total=41.0, partial=41.0)
        self.assertTrue(rep["daily"], "totals exist but the table is empty")
        self.assertEqual(rep["daily"][-1]["date"], R.now_local().strftime("%Y-%m-%d"))
        self.assertEqual(rep["daily"][-1]["accepted"], 41)

    def test_the_partial_row_is_marked_as_partial(self) -> None:
        """It covers hours, not a day. A reader comparing it with yesterday
        should be told why it is smaller."""
        rep = self._report(daily=[], total=41.0, partial=41.0)
        self.assertTrue(rep["daily"][-1].get("partial"))

    def test_completed_days_are_not_marked_partial(self) -> None:
        yesterday = (R.now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
        rep = self._report(
            daily=[((R.now_local()).timestamp(), 7.0)], total=9.0, partial=2.0
        )
        for row in rep["daily"]:
            if row["date"] != R.now_local().strftime("%Y-%m-%d"):
                self.assertFalse(row.get("partial"), row)
        self.assertIsInstance(yesterday, str)

    def test_no_partial_data_means_no_extra_row(self) -> None:
        rep = self._report(daily=[], total=None, partial=None)
        self.assertEqual(rep["daily"], [])

    def test_a_zero_partial_day_is_still_a_row(self) -> None:
        """Zero traffic today is a fact. A missing row reads as missing data."""
        rep = self._report(daily=[], total=0.0, partial=0.0)
        self.assertTrue(rep["daily"])
        self.assertEqual(rep["daily"][-1]["accepted"], 0)

    def test_a_partial_day_never_duplicates_an_existing_date(self) -> None:
        today_ts = (
            R.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        ).timestamp()
        rep = self._report(daily=[(today_ts, 5.0)], total=5.0, partial=5.0)
        dates = [r["date"] for r in rep["daily"]]
        self.assertEqual(len(dates), len(set(dates)), f"duplicate dates: {dates}")

    def test_every_column_is_present_on_the_partial_row(self) -> None:
        # A row missing a key renders as a hole in the table.
        rep = self._report(daily=[], total=1.0, partial=1.0)
        for key in R.REPORT_COLUMNS:
            self.assertIn(key, rep["daily"][-1])

    def test_a_prometheus_failure_on_the_partial_day_is_not_fatal(self) -> None:
        with mock.patch.object(R, "fetch_daily", return_value=[]), mock.patch.object(
            R, "fetch_total", return_value=3.0
        ), mock.patch.object(
            R, "fetch_partial_day", side_effect=R.MonitoringError("down")
        ):
            rep = R.build_report("this_month")
        self.assertEqual(rep["daily"], [])


if __name__ == "__main__":
    unittest.main()
