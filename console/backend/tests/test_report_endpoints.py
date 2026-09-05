"""The report and CSV export endpoints.

Called directly rather than through a TestClient: that needs httpx, which is
not a dependency of this product and is not worth becoming one for a test.

The things worth pinning are the ones a browser depends on and a unit test of
the reporting module cannot see - that a download is actually a download, that
Excel can read it, that a bad parameter is a 400 rather than a stack trace, and
above all that none of it is readable without a session. Mail volumes are
customer data.
"""

from __future__ import annotations

import ast
import asyncio
import csv
import io
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from kin_console import app as app_module
from kin_console import reporting as R
from kin_console.monitoring import MonitoringError

APP_SRC = Path(__file__).resolve().parents[1] / "kin_console" / "app.py"


def _run(coro):
    return asyncio.run(coro)


def _fake_report(period: str = "last_month") -> dict:
    return {
        "period": period,
        "label": "August 2026",
        "start": "2026-08-01T00:00:00+07:00",
        "end": "2026-09-01T00:00:00+07:00",
        "generated_at": "2026-09-05T13:30:00+07:00",
        "columns": [
            {"key": k, "heading": R.REPORT_SERIES[k][0], "kind": R.REPORT_SERIES[k][2]}
            for k in R.REPORT_COLUMNS
        ],
        "totals": {k: 10 for k in R.REPORT_COLUMNS},
        "delivered_pct": 99.0,
        "bounced_pct": 1.0,
        "daily": [
            {"date": "2026-08-01", **{k: 1 for k in R.REPORT_COLUMNS}},
            {"date": "2026-08-02", **{k: 2 for k in R.REPORT_COLUMNS}},
        ],
        "busiest_day": "2026-08-02",
        "busiest_day_accepted": 2,
        "days": 2,
        "unavailable": [],
    }


class EveryEndpointRequiresASession(unittest.TestCase):
    """Mail volumes are customer data. None of this may be anonymous."""

    def _route(self, path: str):
        for route in app_module.app.routes:
            if getattr(route, "path", None) == path:
                return route
        raise AssertionError(f"no route for {path}")

    def test_the_new_routes_exist(self) -> None:
        for path in (
            "/api/reports/mail",
            "/api/reports/mail.csv",
            "/api/monitoring/series.csv",
        ):
            self.assertIsNotNone(self._route(path))

    def test_each_one_depends_on_a_console_user(self) -> None:
        source = APP_SRC.read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = {"reports_mail", "reports_mail_csv", "monitoring_series_csv"}
        seen: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name not in wanted:
                continue
            seen.add(node.name)
            defaults = ast.get_source_segment(source, node) or ""
            self.assertIn(
                "auth.require_console_user",
                defaults,
                f"{node.name} does not require a console session",
            )
        self.assertEqual(seen, wanted, "an endpoint was renamed without updating this")


class GatedTheSameAsTheChartsBeside(unittest.TestCase):
    """Reports must not drift away from the Monitoring tab's posture.

    All three roles already see the charts, and a Customer Admin looking at
    their own mail volumes is appropriate. What must not happen is the two
    surfaces disagreeing after somebody tightens one of them: the reports read
    the same Prometheus data, so a rule that applies to the charts applies here.
    """

    def _dependency_text(self, name: str) -> str:
        source = APP_SRC.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
                return ast.get_source_segment(source, node) or ""
        raise AssertionError(f"no endpoint named {name}")

    def test_reports_are_gated_like_the_series_endpoint(self) -> None:
        charts = self._dependency_text("monitoring_series_batch")
        for name in ("reports_mail", "reports_mail_csv", "monitoring_series_csv"):
            with self.subTest(endpoint=name):
                mine = self._dependency_text(name)
                # Whatever the charts require, these require too.
                self.assertEqual(
                    "auth.require_console_user" in charts,
                    "auth.require_console_user" in mine,
                )
                self.assertEqual(
                    "command_allowed(" in charts, "command_allowed(" in mine,
                    f"{name} does not match the charts' role check",
                )

    def test_none_of_them_mutate_anything(self) -> None:
        # A report is a read. If one of these ever grows a privhelper call, it
        # has stopped being one.
        for name in ("reports_mail", "reports_mail_csv", "monitoring_series_csv"):
            with self.subTest(endpoint=name):
                body = self._dependency_text(name)
                for forbidden in ("_collect_privhelper", "write_config", "subprocess"):
                    self.assertNotIn(forbidden, body)


class ReportingUsesMonitoringsInternals(unittest.TestCase):
    """reporting.py imports private names out of monitoring.py.

    That is deliberate - it is the same Prometheus proxy and duplicating the
    HTTP handling would be worse - but a private name carries no promise. This
    fails loudly if one is renamed, instead of the reports quietly 500ing.
    """

    def test_the_borrowed_names_still_exist(self) -> None:
        from kin_console import monitoring

        for name in ("_get", "_num", "parse_matrix", "parse_vector",
                     "MonitoringError", "PROM_TIMEOUT_SEC"):
            self.assertTrue(hasattr(monitoring, name), f"monitoring.{name} is gone")

    def test_reporting_uses_the_same_objects(self) -> None:
        from kin_console import monitoring

        self.assertIs(R.parse_matrix, monitoring.parse_matrix)
        self.assertIs(R.MonitoringError, monitoring.MonitoringError)

    def test_the_console_boots_even_if_reporting_is_broken(self) -> None:
        # reporting is imported inside the endpoints, not at module scope, so
        # a fault in it cannot stop the console starting.
        source = APP_SRC.read_text(encoding="utf-8")
        head = source[: source.index("app = FastAPI")]
        self.assertNotIn("from . import reporting", head)
        self.assertNotIn("from .reporting import", head)


class ReportEndpoint(unittest.TestCase):
    def test_it_returns_the_report(self) -> None:
        with mock.patch.object(R, "build_report", lambda p: _fake_report(p)):
            out = _run(app_module.reports_mail(period="last_month"))
        self.assertEqual(out["label"], "August 2026")
        self.assertEqual(len(out["daily"]), 2)

    def test_no_period_uses_the_default(self) -> None:
        seen: list[str] = []

        def spy(p: str) -> dict:
            seen.append(p)
            return _fake_report(p)

        with mock.patch.object(R, "build_report", spy):
            _run(app_module.reports_mail(period=""))
        self.assertEqual(seen, [R.DEFAULT_PERIOD])

    def test_an_unknown_period_is_a_400_not_a_stack_trace(self) -> None:
        for bad in ("last_decade", "'; DROP TABLE", "../../etc/passwd", "2026-08"):
            with self.subTest(period=bad):
                with self.assertRaises(HTTPException) as ctx:
                    _run(app_module.reports_mail(period=bad))
                self.assertEqual(ctx.exception.status_code, 400)

    def test_prometheus_being_down_is_a_503(self) -> None:
        def boom(_p: str) -> dict:
            raise MonitoringError("Prometheus is not answering")

        with mock.patch.object(R, "build_report", boom):
            with self.assertRaises(HTTPException) as ctx:
                _run(app_module.reports_mail(period="last_month"))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("Prometheus", str(ctx.exception.detail))


class ReportCsvEndpoint(unittest.TestCase):
    def _csv(self, period: str = "last_month"):
        with mock.patch.object(R, "build_report", lambda p: _fake_report(p)):
            return _run(app_module.reports_mail_csv(period=period))

    def test_it_is_served_as_a_download(self) -> None:
        resp = self._csv()
        self.assertIn("text/csv", resp.media_type)
        disposition = resp.headers["content-disposition"]
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn(".csv", disposition)

    def test_the_filename_says_what_it_holds_and_sorts(self) -> None:
        disposition = self._csv().headers["content-disposition"]
        self.assertIn("20260801", disposition)
        self.assertIn("20260901", disposition)
        self.assertNotIn(" ", disposition.split("filename=")[1])

    def test_excel_reads_it_as_utf8(self) -> None:
        # Without a BOM Excel decodes a UTF-8 CSV as the local codepage and
        # mangles anything non-ASCII in it.
        body = self._csv().body.decode("utf-8")
        self.assertTrue(body.startswith("﻿"), "no UTF-8 BOM")

    def test_it_is_never_cached(self) -> None:
        # A report is a point-in-time answer; a cached one is a wrong one.
        self.assertEqual(self._csv().headers["cache-control"], "no-store")

    def test_the_body_parses_as_csv_with_a_row_per_day(self) -> None:
        body = self._csv().body.decode("utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0][0], "Date")
        self.assertEqual(len(rows), 3, "header plus two days")
        self.assertEqual(len(rows[1]), len(R.REPORT_COLUMNS) + 1)

    def test_a_bad_period_never_reaches_prometheus(self) -> None:
        def explode(_p: str) -> dict:
            raise AssertionError("queried Prometheus for an invalid period")

        with mock.patch.object(R, "build_report", explode):
            with self.assertRaises(HTTPException) as ctx:
                _run(app_module.reports_mail_csv(period="nonsense"))
        self.assertEqual(ctx.exception.status_code, 400)


class MetricsCsvEndpoint(unittest.TestCase):
    def _series(self, name: str, window: str, *, now: float):
        return [
            {
                "instance": "127.0.0.1:9100",
                "points": [[1757000000.0, 41.5], [1757000015.0, None]],
            }
        ]

    def _csv(self, metrics: str = "cpu,memory", window: str = "1h"):
        from kin_console import monitoring

        with mock.patch.object(monitoring, "fetch_range", self._series):
            return _run(app_module.monitoring_series_csv(metrics=metrics, range=window))

    def test_it_exports_one_row_per_sample(self) -> None:
        body = self._csv().body.decode("utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0][0], "timestamp_utc")
        # two metrics x two samples
        self.assertEqual(len(rows), 5)

    def test_a_gap_exports_as_an_empty_cell(self) -> None:
        body = self._csv().body.decode("utf-8")
        self.assertNotIn("None", body)

    def test_an_unknown_range_is_a_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._csv(window="last_century")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_an_unknown_metric_is_a_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._csv(metrics="cpu,rm -rf /")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_the_browser_cannot_smuggle_promql(self) -> None:
        # The catalogue is the whole allow-list; this endpoint must be no
        # weaker than the JSON one it sits beside.
        with self.assertRaises(HTTPException):
            self._csv(metrics='up{job="x"} or vector(1)')

    def test_a_dead_query_yields_an_empty_export_not_a_500(self) -> None:
        from kin_console import monitoring

        def boom(*_a, **_k):
            raise MonitoringError("down")

        with mock.patch.object(monitoring, "fetch_range", boom):
            resp = _run(app_module.monitoring_series_csv(metrics="cpu", range="1h"))
        body = resp.body.decode("utf-8").lstrip("﻿")
        rows = [r for r in csv.reader(io.StringIO(body)) if r]
        self.assertEqual(len(rows), 1, "header only")

    def test_it_is_a_download_and_uncached(self) -> None:
        resp = self._csv()
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual(resp.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
