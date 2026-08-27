"""Built-in Monitoring tab: catalogue, windows and Prometheus response parsing.

The browser never sends PromQL - it names a metric out of CATALOGUE - so these
tests pin the query construction and the parsing, which is everything the UI
depends on and all of it is testable without a running Prometheus.
"""

from __future__ import annotations

import math
import unittest

from kin_console import monitoring as m


class CatalogueTests(unittest.TestCase):
    def test_every_metric_has_label_unit_and_query(self) -> None:
        self.assertTrue(m.CATALOGUE)
        for name, (label, unit, query) in m.CATALOGUE.items():
            self.assertTrue(label.strip(), name)
            self.assertIn(unit, {"percent", "bytes_per_sec", "number", "seconds"}, name)
            self.assertTrue(query.strip(), name)

    def test_the_metrics_the_operator_asked_for_are_present(self) -> None:
        for wanted in ("cpu", "memory", "net_rx", "net_tx", "disk_root", "disk_mail"):
            self.assertIn(wanted, m.CATALOGUE)

    def test_public_catalogue_never_leaks_promql(self) -> None:
        # The query is an implementation detail; shipping it to the browser
        # would invite someone to try sending their own back.
        for entry in m.catalogue_public():
            self.assertEqual(set(entry), {"metric", "label", "unit"})

    def test_unknown_metric_is_rejected(self) -> None:
        self.assertFalse(m.known_metric("rm -rf"))
        with self.assertRaises(m.MonitoringError):
            m.build_range_params("nope", "1h", now=0.0)
        with self.assertRaises(m.MonitoringError):
            m.fetch_instant("nope")


class RangeTests(unittest.TestCase):
    def test_operator_can_pick_from_realtime_to_a_year(self) -> None:
        self.assertIn("1h", m.RANGES)
        self.assertIn("1y", m.RANGES)

    def test_every_window_stays_a_drawable_number_of_points(self) -> None:
        # Unbounded point counts are how a "1 year" button becomes a
        # multi-megabyte response that hangs the tab.
        for name in m.RANGES:
            points = m.expected_points(name)
            self.assertGreater(points, 100, name)
            self.assertLessEqual(points, 500, name)

    def test_unknown_range_falls_back_instead_of_raising(self) -> None:
        self.assertEqual(m.range_window("nonsense"), m.RANGES[m.DEFAULT_RANGE])

    def test_range_params_span_exactly_the_window(self) -> None:
        params = m.build_range_params("cpu", "24h", now=1_000_000.0)
        self.assertAlmostEqual(
            float(params["end"]) - float(params["start"]), 24 * 3600, places=3
        )
        self.assertEqual(params["step"], "300")
        self.assertIn("node_cpu_seconds_total", params["query"])


class ParseTests(unittest.TestCase):
    def test_matrix_parses_points_per_instance_sorted(self) -> None:
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"instance": "b:9100"}, "values": [[2.0, "7"]]},
                    {"metric": {"instance": "a:9100"}, "values": [[1.0, "3"], [2.0, "4"]]},
                ]
            },
        }
        series = m.parse_matrix(payload)
        self.assertEqual([s["instance"] for s in series], ["a:9100", "b:9100"])
        self.assertEqual(series[0]["points"], [[1.0, 3.0], [2.0, 4.0]])

    def test_gaps_become_null_not_a_spike(self) -> None:
        # NaN/Inf cannot survive JSON; a chart must draw a break instead of
        # inventing a value.
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {}, "values": [[1.0, "NaN"], [2.0, "+Inf"], [3.0, "5"]]}
                ]
            },
        }
        points = m.parse_matrix(payload)[0]["points"]
        self.assertEqual(points, [[1.0, None], [2.0, None], [3.0, 5.0]])
        self.assertFalse(any(isinstance(p[1], float) and math.isnan(p[1]) for p in points))

    def test_series_with_no_instance_label_is_still_named(self) -> None:
        payload = {"status": "success", "data": {"result": [{"metric": {}, "values": []}]}}
        self.assertEqual(m.parse_matrix(payload)[0]["instance"], "this server")

    def test_malformed_points_are_skipped_not_fatal(self) -> None:
        payload = {
            "status": "success",
            "data": {"result": [{"metric": {}, "values": [["x"], [1.0, "2"], "junk"]}]},
        }
        self.assertEqual(m.parse_matrix(payload)[0]["points"], [[1.0, 2.0]])

    def test_vector_parses_current_values(self) -> None:
        payload = {
            "status": "success",
            "data": {"result": [{"metric": {"instance": "a"}, "value": [1.0, "42.5"]}]},
        }
        self.assertEqual(m.parse_vector(payload), [{"instance": "a", "value": 42.5}])

    def test_error_status_raises_with_the_prometheus_message(self) -> None:
        for parse in (m.parse_matrix, m.parse_vector):
            with self.assertRaises(m.MonitoringError) as ctx:
                parse({"status": "error", "error": "bad expression"})
            self.assertIn("bad expression", str(ctx.exception))

    def test_empty_and_missing_payloads_do_not_crash(self) -> None:
        empty = {"status": "success", "data": {"result": []}}
        self.assertEqual(m.parse_matrix(empty), [])
        self.assertEqual(m.parse_vector(empty), [])
        with self.assertRaises(m.MonitoringError):
            m.parse_matrix({})


class BindingTests(unittest.TestCase):
    def test_prometheus_is_localhost_only(self) -> None:
        # Monitoring must be a tab in this console, not a second port the
        # operator has to expose and log into separately.
        self.assertTrue(m.PROM_BASE.startswith("http://127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
