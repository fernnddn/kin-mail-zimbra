"""Built-in Monitoring tab: catalogue, windows and Prometheus response parsing.

The browser never sends PromQL - it names a metric out of CATALOGUE - so these
tests pin the query construction and the parsing, which is everything the UI
depends on and all of it is testable without a running Prometheus.
"""

from __future__ import annotations

import math
import unittest

from kin_console import monitoring as m


# Every unit the chart formatter knows how to render. A metric carrying a unit
# that is not in this set draws, but with the wrong words next to the number.
KNOWN_UNITS = {
    "percent",
    "bytes_per_sec",
    "disk_bytes_per_sec",
    "per_sec",
    "number",
    "seconds",
}


class CatalogueTests(unittest.TestCase):
    def test_every_metric_has_label_unit_and_query(self) -> None:
        self.assertTrue(m.CATALOGUE)
        for name, (label, unit, query) in m.CATALOGUE.items():
            self.assertTrue(label.strip(), name)
            self.assertIn(unit, KNOWN_UNITS, name)
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


class HostFactTests(unittest.TestCase):
    """Phase 5 QA: the operator wants the concrete numbers, not just trends -
    how many cores and which CPU, GB of RAM used out of total, GB left on the
    mail volume and the system disk, and uptime."""

    CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz
physical id\t: 0
core id\t\t: 0
processor\t: 1
model name\t: Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz
physical id\t: 0
core id\t\t: 0
processor\t: 2
model name\t: Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz
physical id\t: 0
core id\t\t: 1
processor\t: 3
model name\t: Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz
physical id\t: 0
core id\t\t: 1
"""

    MEMINFO = """MemTotal:       16316412 kB
MemFree:          210484 kB
MemAvailable:    8123456 kB
Buffers:          123456 kB
"""

    def test_cpu_model_and_counts(self) -> None:
        self.assertEqual(
            m.parse_cpu_model(self.CPUINFO),
            "Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz",
        )
        threads, cores = m.parse_cpu_counts(self.CPUINFO)
        self.assertEqual(threads, 4)
        # Hyper-threaded: 4 threads sharing 2 physical cores.
        self.assertEqual(cores, 2)

    def test_cpu_without_model_name_does_not_return_a_number(self) -> None:
        # ARM /proc/cpuinfo has no "model name"; "processor : 0" must not be
        # mistaken for one.
        arm = "processor\t: 0\nBogoMIPS\t: 50.00\nHardware\t: BCM2835\n"
        self.assertEqual(m.parse_cpu_model(arm), "BCM2835")
        self.assertEqual(m.parse_cpu_model(""), "")

    def test_cores_fall_back_to_threads_without_topology(self) -> None:
        plain = "processor\t: 0\nprocessor\t: 1\n"
        self.assertEqual(m.parse_cpu_counts(plain), (2, 2))

    def test_memory_used_is_derived_from_available(self) -> None:
        mem = m.memory_usage(self.MEMINFO)
        self.assertEqual(mem["total_bytes"], 16316412 * 1024)
        self.assertEqual(mem["available_bytes"], 8123456 * 1024)
        # Used must come off MemAvailable, not MemFree - counting cache as used
        # reads alarmingly high and is wrong.
        self.assertEqual(mem["used_bytes"], (16316412 - 8123456) * 1024)
        self.assertAlmostEqual(mem["percent"], 50.2, places=0)

    def test_memory_without_available_falls_back_to_free(self) -> None:
        mem = m.memory_usage("MemTotal: 1000 kB\nMemFree: 400 kB\n")
        self.assertEqual(mem["available_bytes"], 400 * 1024)
        self.assertEqual(mem["used_bytes"], 600 * 1024)

    def test_empty_meminfo_is_safe(self) -> None:
        mem = m.memory_usage("")
        self.assertEqual(mem["total_bytes"], 0)
        self.assertIsNone(mem["percent"])

    def test_disk_usage_reports_used_and_free(self) -> None:
        d = m.disk_usage("/")
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d["used_bytes"] + d["free_bytes"], d["total_bytes"])
        self.assertGreaterEqual(d["percent"], 0)
        self.assertLessEqual(d["percent"], 100)

    def test_disk_usage_of_a_missing_mount_is_none(self) -> None:
        self.assertIsNone(m.disk_usage("/definitely/not/a/mount/point"))

    def test_uptime(self) -> None:
        self.assertEqual(m.parse_uptime("123456.78 987654.32"), 123456.78)
        self.assertIsNone(m.parse_uptime(""))
        self.assertIsNone(m.parse_uptime("garbage"))

    def test_host_facts_shape_is_stable(self) -> None:
        # Must not raise on a host with no /proc (the dev machine).
        facts = m.host_facts()
        self.assertEqual(set(facts), {"cpu", "memory", "disks", "uptime_seconds"})
        self.assertEqual(set(facts["cpu"]), {"model", "threads", "cores", "load"})
        self.assertIsInstance(facts["disks"], list)


class DiagnosticMetricTests(unittest.TestCase):
    """Metrics added to answer "why is this slow?" and "is the link healthy?"."""

    def test_the_lag_and_link_metrics_exist(self) -> None:
        # Phase 7 had an operator reporting the console felt laggy while DRBD
        # logged PingAck timeouts, and this tab could not distinguish a busy
        # CPU from a saturated disk or a NIC dropping frames.
        for wanted in (
            "cpu_iowait",
            "disk_busy",
            "swap_used",
            "net_errors",
            "tcp_established",
            "load5",
        ):
            self.assertIn(wanted, m.CATALOGUE)

    def test_disk_throughput_is_not_quoted_in_bits(self) -> None:
        # Network is bits per second, disks are bytes per second. Both used the
        # same unit, so a disk reading 50 MB/s was labelled "400 Mbps".
        self.assertEqual(m.CATALOGUE["disk_read"][1], "disk_bytes_per_sec")
        self.assertEqual(m.CATALOGUE["disk_write"][1], "disk_bytes_per_sec")
        self.assertEqual(m.CATALOGUE["net_rx"][1], "bytes_per_sec")

    def test_swap_query_survives_a_host_with_no_swap(self) -> None:
        # Dividing by SwapTotal on a swapless host is a division by zero, which
        # Prometheus returns as NaN and the chart would draw as a gap forever.
        self.assertIn("clamp_min", m.CATALOGUE["swap_used"][2])

    def test_every_new_metric_still_builds_a_query(self) -> None:
        for name in m.CATALOGUE:
            params = m.build_range_params(name, "1h", now=1000.0)
            self.assertTrue(params["query"])
            self.assertNotIn("{job}", params["query"])


class UnitsMatchTheFrontendTests(unittest.TestCase):
    def test_the_chart_formatter_handles_every_unit_we_emit(self) -> None:
        """A unit the backend invents and the frontend does not know is silent.

        formatValue falls through to a bare number, so a percentage renders as
        "93.00" and throughput as "52428800" - wrong, but never an error, so
        nothing catches it except reading the tab.
        """
        from pathlib import Path

        chart = (
            Path(__file__).resolve().parents[2]
            / "frontend/src/monitoring/chart.ts"
        ).read_text(encoding="utf-8")
        for unit in sorted({u for _l, u, _q in m.CATALOGUE.values()}):
            if unit == "number":
                continue  # the formatter's default branch
            self.assertIn(f'case "{unit}":', chart, unit)


class BatchRequestTests(unittest.TestCase):
    """The batch endpoint is the only place a list of names becomes queries."""

    def test_names_are_kept_in_order_and_deduplicated(self) -> None:
        self.assertEqual(
            m.parse_metric_list("memory, cpu ,memory"),
            ["memory", "cpu"],
        )

    def test_anything_outside_the_catalogue_is_refused(self) -> None:
        # The browser must never be able to widen this into PromQL.
        for bad in (
            "cpu,node_cpu_seconds_total",
            'cpu,up{job="x"}',
            "cpu,rate(node_cpu_seconds_total[5m])",
            "../etc/passwd",
        ):
            with self.assertRaises(m.MonitoringError, msg=bad):
                m.parse_metric_list(bad)

    def test_an_empty_request_is_refused_rather_than_returning_everything(self) -> None:
        for empty in ("", "   ", ",,,"):
            with self.assertRaises(m.MonitoringError):
                m.parse_metric_list(empty)
