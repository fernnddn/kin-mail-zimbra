"""Built-in metrics for the console Monitoring tab.

Prometheus listens on 127.0.0.1 only and is never exposed as its own port: the
console proxies it on the port the operator is already authenticated to, so
Monitoring is a tab in this app rather than a second web UI to log into.

The browser never sends PromQL. It asks for a named metric out of CATALOGUE
and this module builds the query, so a console session cannot be used to run
arbitrary expressions against the TSDB, and the frontend never has to know
what node_exporter calls things.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PROM_BASE = "http://127.0.0.1:9090"
PROM_TIMEOUT_SEC = 8

# name -> (label, unit, promql). {job} is filled with the scrape filter.
# Rates use 5m so a 15s scrape still has enough samples after a restart.
CATALOGUE: dict[str, tuple[str, str, str]] = {
    "cpu": (
        "CPU used",
        "percent",
        '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    ),
    "memory": (
        "Memory used",
        "percent",
        "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
    ),
    "load1": ("Load average (1m)", "number", "node_load1"),
    "disk_root": (
        "System disk used",
        "percent",
        '(1 - (node_filesystem_avail_bytes{mountpoint="/"} '
        '/ node_filesystem_size_bytes{mountpoint="/"})) * 100',
    ),
    "disk_mail": (
        "Mail storage used",
        "percent",
        '(1 - (node_filesystem_avail_bytes{mountpoint="/opt/zimbra"} '
        '/ node_filesystem_size_bytes{mountpoint="/opt/zimbra"})) * 100',
    ),
    "net_rx": (
        "Network in",
        "bytes_per_sec",
        'sum by (instance) (rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*"}[5m]))',
    ),
    "net_tx": (
        "Network out",
        "bytes_per_sec",
        'sum by (instance) (rate(node_network_transmit_bytes_total{device!~"lo|veth.*|docker.*"}[5m]))',
    ),
    "disk_read": (
        "Disk read",
        "bytes_per_sec",
        "sum by (instance) (rate(node_disk_read_bytes_total[5m]))",
    ),
    "disk_write": (
        "Disk write",
        "bytes_per_sec",
        "sum by (instance) (rate(node_disk_written_bytes_total[5m]))",
    ),
    "uptime": (
        "Uptime",
        "seconds",
        "time() - node_boot_time_seconds",
    ),
}

# name -> (seconds back, step seconds). Steps are chosen so every window
# returns roughly 250-400 points: enough to draw, small enough that a year of
# history is not a multi-megabyte JSON response.
RANGES: dict[str, tuple[int, int]] = {
    "1h": (3600, 15),
    "6h": (6 * 3600, 60),
    "24h": (24 * 3600, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 7200),
    "1y": (365 * 86400, 86400),
}

DEFAULT_RANGE = "6h"


class MonitoringError(RuntimeError):
    """Prometheus was unreachable or returned something unusable."""


def known_metric(name: str) -> bool:
    return name in CATALOGUE


def metric_meta(name: str) -> dict[str, str]:
    label, unit, _q = CATALOGUE[name]
    return {"metric": name, "label": label, "unit": unit}


def catalogue_public() -> list[dict[str, str]]:
    return [metric_meta(n) for n in CATALOGUE]


def range_window(name: str) -> tuple[int, int]:
    """(seconds_back, step). Unknown names fall back to the default window."""
    return RANGES.get(name, RANGES[DEFAULT_RANGE])


def expected_points(range_name: str) -> int:
    seconds, step = range_window(range_name)
    return seconds // step


def build_range_params(metric: str, range_name: str, *, now: float) -> dict[str, str]:
    if not known_metric(metric):
        raise MonitoringError(f"unknown metric {metric!r}")
    seconds, step = range_window(range_name)
    _label, _unit, query = CATALOGUE[metric]
    return {
        "query": query,
        "start": f"{now - seconds:.3f}",
        "end": f"{now:.3f}",
        "step": str(step),
    }


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # Prometheus renders gaps as NaN/Inf; JSON cannot carry them and a chart
    # must show a break, not a spike.
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def parse_matrix(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Prometheus matrix -> [{instance, points:[[ts, value|None], ...]}]."""
    if (payload or {}).get("status") != "success":
        raise MonitoringError(str((payload or {}).get("error") or "query failed"))
    result = ((payload.get("data") or {}).get("result")) or []
    series: list[dict[str, Any]] = []
    for item in result:
        metric = item.get("metric") or {}
        instance = str(metric.get("instance") or metric.get("nodename") or "").strip()
        points: list[list[Any]] = []
        for pair in item.get("values") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            ts = _num(pair[0])
            if ts is None:
                continue
            points.append([ts, _num(pair[1])])
        series.append({"instance": instance or "this server", "points": points})
    series.sort(key=lambda s: s["instance"])
    return series


def parse_vector(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Prometheus vector -> [{instance, value|None}]."""
    if (payload or {}).get("status") != "success":
        raise MonitoringError(str((payload or {}).get("error") or "query failed"))
    result = ((payload.get("data") or {}).get("result")) or []
    out: list[dict[str, Any]] = []
    for item in result:
        metric = item.get("metric") or {}
        instance = str(metric.get("instance") or "").strip()
        value = item.get("value")
        val = None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            val = _num(value[1])
        out.append({"instance": instance or "this server", "value": val})
    out.sort(key=lambda s: s["instance"])
    return out


def _get(path: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{PROM_BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=PROM_TIMEOUT_SEC) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # Prometheus reports a bad query as 400 with a JSON body; keep its text.
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            raise MonitoringError(str(body.get("error") or exc.reason)) from exc
        except (ValueError, AttributeError):
            raise MonitoringError(f"Prometheus HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MonitoringError(
            "Prometheus is not answering on 127.0.0.1:9090. Metrics are "
            "collected by the kin-mail monitoring stack; re-run the deploy if "
            "it was never installed."
        ) from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MonitoringError("Prometheus returned a non-JSON body") from exc


def fetch_range(metric: str, range_name: str, *, now: float) -> list[dict[str, Any]]:
    params = build_range_params(metric, range_name, now=now)
    return parse_matrix(_get("/api/v1/query_range", params))


def fetch_instant(metric: str) -> list[dict[str, Any]]:
    if not known_metric(metric):
        raise MonitoringError(f"unknown metric {metric!r}")
    _label, _unit, query = CATALOGUE[metric]
    return parse_vector(_get("/api/v1/query", {"query": query}))
