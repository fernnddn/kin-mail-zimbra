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
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PROM_BASE = "http://127.0.0.1:9090"
PROM_TIMEOUT_SEC = 8

# Where the collectors write their .prom files. Same default as the Ansible
# role; overridable so tests never read a real appliance path.
TEXTFILE_DIR = os.environ.get(
    "KIN_TEXTFILE_DIR", "/var/lib/node_exporter/textfile"
)

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
        "disk_bytes_per_sec",
        "sum by (instance) (rate(node_disk_read_bytes_total[5m]))",
    ),
    "disk_write": (
        "Disk write",
        "disk_bytes_per_sec",
        "sum by (instance) (rate(node_disk_written_bytes_total[5m]))",
    ),
    # The metrics below exist to answer "why does this feel slow?" and "is the
    # replication link healthy?" - the two questions the Phase 7 run could not
    # answer from this tab. iowait and disk busy separate a loaded CPU from a
    # disk that cannot keep up; network errors and drops are what a saturated
    # NIC looks like before DRBD starts logging PingAck timeouts.
    "cpu_iowait": (
        "CPU waiting on disk",
        "percent",
        'avg by (instance) (rate(node_cpu_seconds_total{mode="iowait"}[5m])) * 100',
    ),
    "disk_busy": (
        "Busiest disk utilisation",
        "percent",
        "max by (instance) "
        '(rate(node_disk_io_time_seconds_total{device!~"loop.*"}[5m])) * 100',
    ),
    "swap_used": (
        "Swap used",
        "percent",
        "(1 - (node_memory_SwapFree_bytes / clamp_min(node_memory_SwapTotal_bytes, 1)))"
        " * 100",
    ),
    "net_errors": (
        "Network errors and drops",
        "per_sec",
        "sum by (instance) ("
        'rate(node_network_receive_errs_total{device!~"lo|veth.*|docker.*"}[5m])'
        ' + rate(node_network_transmit_errs_total{device!~"lo|veth.*|docker.*"}[5m])'
        ' + rate(node_network_receive_drop_total{device!~"lo|veth.*|docker.*"}[5m])'
        ' + rate(node_network_transmit_drop_total{device!~"lo|veth.*|docker.*"}[5m])'
        ")",
    ),
    "tcp_established": (
        "TCP connections",
        "count",
        "node_netstat_Tcp_CurrEstab",
    ),
    "load5": ("Load average (5m)", "number", "node_load5"),
    # --- mail flow -----------------------------------------------------------
    # Everything above describes the machine. These describe the mail, which
    # is the reason the machine exists. They come from the textfile collector
    # (monitoring/mailflow/kin-mail-flow-metrics.py) reading the Postfix log
    # and spool, so they travel the same loopback path as the node metrics.
    #
    # Rates use 5m to match the rest of the catalogue and to stay meaningful
    # on an appliance where mail arrives in bursts rather than continuously.
    "mail_received": (
        "Mail accepted",
        "per_min",
        "sum by (instance) (rate(kin_mail_events_total{event=\"accepted\"}[5m])) * 60",
    ),
    "mail_delivered_in": (
        "Delivered to mailboxes",
        "per_min",
        "sum by (instance) (rate(kin_mail_delivered_total{transport=\"lmtp\"}[5m])) * 60",
    ),
    "mail_delivered_out": (
        "Sent to other servers",
        "per_min",
        "sum by (instance) (rate(kin_mail_delivered_total{transport=\"smtp\"}[5m])) * 60",
    ),
    "mail_rejected": (
        "Rejected at the door",
        "per_min",
        "sum by (instance) (rate(kin_mail_events_total{event=\"rejected\"}[5m])) * 60",
    ),
    "mail_deferred": (
        "Deferred (will retry)",
        "per_min",
        "sum by (instance) "
        "(rate(kin_mail_undelivered_total{outcome=\"deferred\"}[5m])) * 60",
    ),
    "mail_bounced": (
        "Bounced (gave up)",
        "per_min",
        "sum by (instance) "
        "(rate(kin_mail_undelivered_total{outcome=\"bounced\"}[5m])) * 60",
    ),
    "mail_queue": (
        "Messages waiting",
        "count",
        "sum by (instance) (kin_mail_queue_messages)",
    ),
    "mail_queue_deferred": (
        "Deferred queue",
        "count",
        "sum by (instance) (kin_mail_queue_messages{queue=\"deferred\"})",
    ),
    # Depth alone cannot tell ten messages that arrived a second ago from ten
    # that have been retrying since 03:00. This is the one that says "stuck".
    "mail_queue_oldest": (
        "Oldest message waiting",
        "seconds",
        "max by (instance) (kin_mail_queue_oldest_seconds)",
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
    # A live window: the last half hour at the resolution the data actually
    # has. Prometheus scrapes every 15s (monitoring_stack_scrape_interval), so
    # a finer step invents nothing and just repeats the same sample - and 5
    # minutes of it was too few points to draw a line worth looking at. Thirty
    # minutes at 15s is 120 points, refreshed often enough that the right-hand
    # edge moves while you watch it.
    "now": (1800, 15),
    "1h": (3600, 15),
    "6h": (6 * 3600, 60),
    "24h": (24 * 3600, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 7200),
    "1y": (365 * 86400, 86400),
}

# The live window, not an hour of it.
#
# The first thing an operator looks at is what the box is doing NOW, and an
# hour-wide window averages away the spike that made them open the tab. "now"
# is the last half hour at the scrape resolution, so the right-hand edge moves
# while they watch. This is also the default the console opens on; the two are
# deliberately the same value, because a server default that disagrees with the
# client's opening view means the tab shows one window and the API answers for
# another.
DEFAULT_RANGE = "now"


class MonitoringError(RuntimeError):
    """Prometheus was unreachable or returned something unusable."""


def known_metric(name: str) -> bool:
    return name in CATALOGUE


def metric_meta(name: str) -> dict[str, str]:
    label, unit, _q = CATALOGUE[name]
    return {"metric": name, "label": label, "unit": unit}


def catalogue_public() -> list[dict[str, str]]:
    return [metric_meta(n) for n in CATALOGUE]


def parse_metric_list(raw: str) -> list[str]:
    """Names from a comma-separated request, validated against the catalogue.

    The browser sends names, never PromQL, and this is the only place a batch
    request turns text into metrics - so it is the place that has to refuse
    anything not in the catalogue. Order is preserved and duplicates dropped,
    so asking for the same chart twice cannot multiply the work.
    """
    wanted: list[str] = []
    for chunk in raw.split(","):
        name = chunk.strip()
        if not name or name in wanted:
            continue
        if not known_metric(name):
            raise MonitoringError(f"Unknown metric: {name}")
        wanted.append(name)
    if not wanted:
        raise MonitoringError("No metrics requested")
    return wanted


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
        # Do not say "re-run the deploy": a live appliance refuses that
        # outright ("mail is already installed"), so it sent the operator down
        # a dead end. Install monitoring is its own action on this tab.
        raise MonitoringError(
            "Prometheus is not answering on 127.0.0.1:9090, so this appliance "
            "is not collecting metrics yet. Use Install monitoring on this tab "
            "to add it; mail is unaffected either way."
        ) from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MonitoringError("Prometheus returned a non-JSON body") from exc


# ---------------------------------------------------------------------------
# Host facts, read straight from the kernel.
#
# The operator wants the concrete numbers next to the trends: how many cores
# and which CPU, how many GB of RAM are in use out of how many, how much of the
# mail volume and the system disk is left. node_exporter only exposes the CPU
# model behind a non-default flag and only from 1.4, and jammy ships 1.3 - so
# reading /proc and statvfs here is both simpler and always right. Prometheus
# stays responsible for history.
# ---------------------------------------------------------------------------

MAIL_MOUNT = "/opt/zimbra"
SYSTEM_MOUNT = "/"


def parse_cpu_model(cpuinfo: str) -> str:
    for line in (cpuinfo or "").splitlines():
        if line.lower().startswith("model name"):
            _k, _sep, val = line.partition(":")
            return val.strip()
    # ARM and some VMs have no model name; fall back to whatever identifies it.
    for key in ("hardware", "processor"):
        for line in (cpuinfo or "").splitlines():
            if line.lower().startswith(key):
                _k, _sep, val = line.partition(":")
                if val.strip() and not val.strip().isdigit():
                    return val.strip()
    return ""


def parse_cpu_counts(cpuinfo: str) -> tuple[int, int]:
    """(logical threads, physical cores). Cores falls back to threads."""
    threads = 0
    physical: set[tuple[str, str]] = set()
    phys_id = ""
    for line in (cpuinfo or "").splitlines():
        low = line.lower()
        if low.startswith("processor"):
            threads += 1
        elif low.startswith("physical id"):
            phys_id = line.partition(":")[2].strip()
        elif low.startswith("core id"):
            physical.add((phys_id, line.partition(":")[2].strip()))
    cores = len(physical) or threads
    return threads, cores


def parse_meminfo(meminfo: str) -> dict[str, int]:
    """kB values from /proc/meminfo, as bytes."""
    out: dict[str, int] = {}
    for line in (meminfo or "").splitlines():
        key, _sep, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            out[key.strip()] = int(parts[0]) * 1024
        except ValueError:
            continue
    return out


def memory_usage(meminfo: str) -> dict[str, Any]:
    m = parse_meminfo(meminfo)
    total = m.get("MemTotal", 0)
    # MemAvailable is the kernel's own estimate and is what "used" should be
    # derived from - MemFree alone counts cache as used and reads alarmingly.
    available = m.get("MemAvailable", m.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": round(used / total * 100, 1) if total else None,
    }


def same_filesystem(a: str, b: str) -> bool:
    """Are these two paths on one filesystem? False if either cannot be read."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def disk_usage(path: str) -> dict[str, Any] | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    # f_bavail, not f_bfree: the reserved blocks are not usable space.
    free = st.f_bavail * st.f_frsize
    used = max(0, total - free)
    if not total:
        return None
    return {
        "mount": path,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "percent": round(used / total * 100, 1),
    }


def parse_uptime(uptime_text: str) -> float | None:
    try:
        return float((uptime_text or "").split()[0])
    except (ValueError, IndexError):
        return None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _instant(query: str) -> list[dict[str, Any]]:
    """One PromQL instant query. Built here, never sent by the browser."""
    return parse_vector(_get("/api/v1/query", {"query": query}))


def _one_label(query: str, label: str) -> str:
    """One label value from an instant query, or "" when there is none.

    parse_vector keeps only the instance and the sample value, which is right
    for a number. Some facts are carried AS a label - a CPU model cannot be a
    float - so this reads the raw payload instead of widening that parser for
    one caller.
    """
    try:
        payload = _get("/api/v1/query", {"query": query})
    except MonitoringError:
        return ""
    for item in payload.get("data", {}).get("result", []) or []:
        if not isinstance(item, dict):
            continue
        labels = item.get("metric") or {}
        if isinstance(labels, dict):
            value = str(labels.get(label) or "").strip()
            if value:
                return value
    return ""


def _one(query: str) -> float | None:
    try:
        rows = _instant(query)
    except MonitoringError:
        return None
    for row in rows:
        val = row.get("value")
        if val is not None:
            return float(val)
    return None


def _label_selector(instance: str) -> str:
    """instance="name", with the name made safe for a PromQL string literal.

    The name comes from the scrape config this product writes, not from the
    browser - the API only ever accepts a name that is already present in the
    catalogue of scraped instances - but building a query by concatenation is
    worth closing off at the point of concatenation rather than trusting every
    future caller to have checked.
    """
    safe = re.sub(r'[^A-Za-z0-9._:-]', "", instance)
    return f'instance="{safe}"'


# Machines the Monitoring tab can offer, and what to call them. The role label
# is written by the scrape config (monitoring_stack/templates/prometheus.yml.j2).
ROLE_LABELS = {"edge": "This server", "mailbox": "Mailbox"}


def scraped_nodes() -> list[dict[str, str]]:
    """Every node Prometheus currently has machine metrics for.

    Read from the DATA, not from the scrape config: a target that is listed but
    has never been scraped successfully has nothing to draw, and offering a tab
    that opens on empty charts is worse than not offering the tab.
    """
    # role!="" is the filter, and it is doing real work.
    #
    # The prometheus package ships its own /etc/prometheus/prometheus.yml that
    # scrapes localhost:9090 and localhost:9100. Installing monitoring replaces
    # that file, but the series it already wrote stay in the TSDB for the
    # retention period, and for the first few minutes afterwards they are still
    # inside Prometheus' staleness window - so an instant query for
    # node_time_seconds answers with localhost:9100 as well as this appliance,
    # and the Monitoring tab grew a third machine that does not exist. Seen on
    # the live pair, 19 Sep 2026.
    #
    # Only the scrape config this product writes sets `role`, so requiring it
    # is the difference between "a node we deployed" and "a series that once
    # existed". An appliance whose prometheus.yml predates the label answers
    # with nothing, the switcher does not appear, and the tab behaves exactly
    # as it did before there was one - which is the right way to degrade.
    try:
        payload = _get("/api/v1/query", {"query": 'node_time_seconds{role!=""}'})
    except MonitoringError:
        return []
    seen: dict[str, str] = {}
    for item in payload.get("data", {}).get("result", []) or []:
        if not isinstance(item, dict):
            continue
        labels = item.get("metric") or {}
        if not isinstance(labels, dict):
            continue
        name = str(labels.get("instance") or "").strip()
        role = str(labels.get("role") or "").strip()
        if not name or not role:
            continue
        seen.setdefault(name, role)
    out = [
        {
            "instance": name,
            "role": role,
            # A role this build does not know about still names a real machine,
            # so it is offered under its own name rather than dropped.
            "label": ROLE_LABELS.get(role) or name,
        }
        for name, role in seen.items()
    ]
    # Edge first: it is the machine the console runs on and the one an operator
    # opens the tab expecting to see.
    order = {"edge": 0, "mailbox": 1}
    out.sort(key=lambda n: (order.get(n["role"], 2), n["instance"]))
    return out


def host_facts_for_instance(instance: str) -> dict[str, Any]:
    """The same shape as host_facts(), for a node this one only scrapes.

    /proc is not available for another machine, so every figure here comes from
    node_exporter through Prometheus. Two of them cannot: the CPU model needs a
    collector jammy's node_exporter 1.3 does not enable, and mail-flow and
    gateway health are the edge's own business. They come back empty rather
    than guessed - a blank field reads as "not known from here", an invented
    one reads as fact.
    """
    sel = _label_selector(instance)
    # Published by install/12-node-metrics.sh through node_exporter's textfile
    # collector, because /proc belongs to the machine it is on and node_exporter
    # 1.3 - what jammy ships - has no collector for the CPU model. Empty when
    # that node has not been given the collector yet, which is honest: better a
    # blank field than a model invented from this machine's own hardware.
    model = _one_label(f"kin_node_cpu_info{{{sel}}}", "model")
    threads = _one(f"kin_node_cpu_threads{{{sel}}}")
    if threads is None:
        threads = _one(f"count(count by (cpu) (node_cpu_seconds_total{{{sel}}}))")
    cores = _one(f"kin_node_cpu_cores{{{sel}}}")
    total = _one(f"node_memory_MemTotal_bytes{{{sel}}}")
    avail = _one(f"node_memory_MemAvailable_bytes{{{sel}}}")
    uptime = _one(f"node_time_seconds{{{sel}}} - node_boot_time_seconds{{{sel}}}")
    loads = [
        _one(f"node_load1{{{sel}}}"),
        _one(f"node_load5{{{sel}}}"),
        _one(f"node_load15{{{sel}}}"),
    ]

    used = None
    percent = None
    if total is not None and avail is not None and total > 0:
        used = total - avail
        percent = round((used / total) * 100, 1)

    disks: list[dict[str, Any]] = []
    for mount in (MAIL_MOUNT, SYSTEM_MOUNT):
        msel = f'{sel},mountpoint="{mount}"'
        size = _one(f"node_filesystem_size_bytes{{{msel}}}")
        free = _one(f"node_filesystem_avail_bytes{{{msel}}}")
        if size is None or free is None or size <= 0:
            continue
        disks.append(
            {
                "mount": mount,
                "total_bytes": int(size),
                "used_bytes": int(size - free),
                "free_bytes": int(free),
                "percent": round(((size - free) / size) * 100, 1),
            }
        )

    return {
        "cpu": {
            "model": model,
            "threads": int(threads) if threads else 0,
            # Cores, when the node publishes them. Falling back to the thread
            # count is what the console did before either was available, and it
            # is wrong on anything with hyper-threading - but it is the number
            # node_cpu_seconds_total can actually answer for.
            "cores": int(cores) if cores else (int(threads) if threads else 0),
            "load": [round(v, 2) for v in loads if v is not None],
        },
        "memory": {
            "total_bytes": int(total) if total else 0,
            "used_bytes": int(used) if used else 0,
            "available_bytes": int(avail) if avail else 0,
            "percent": percent,
        },
        "disks": disks,
        # Two mountpoints reported by node_exporter are two filesystems; it
        # does not publish a bind-mounted directory as its own mount, so the
        # ambiguity the local reader has to warn about cannot arise here.
        "disks_share_a_filesystem": False,
        "uptime_seconds": int(uptime) if uptime else None,
        "remote": True,
        "instance": instance,
    }


def host_facts() -> dict[str, Any]:
    cpuinfo = _read("/proc/cpuinfo")
    threads, cores = parse_cpu_counts(cpuinfo)
    loads: list[float] = []
    try:
        loads = [round(v, 2) for v in os.getloadavg()]
    except OSError:
        loads = []
    return {
        "cpu": {
            "model": parse_cpu_model(cpuinfo),
            "threads": threads,
            "cores": cores,
            "load": loads,
        },
        "memory": memory_usage(_read("/proc/meminfo")),
        "disks": [
            d
            for d in (disk_usage(MAIL_MOUNT), disk_usage(SYSTEM_MOUNT))
            if d is not None
        ],
        # On a single-disk appliance /opt/zimbra is a directory on the root
        # filesystem, not a volume of its own, so the two cards show identical
        # figures. That is correct and it looks like a bug, and an operator who
        # reads it as two disks will size the next one wrongly. Say it instead.
        "disks_share_a_filesystem": same_filesystem(MAIL_MOUNT, SYSTEM_MOUNT),
        "uptime_seconds": parse_uptime(_read("/proc/uptime")),
        # The gateway carries every message in and out. Its failure looks like
        # perfect health from here - Zimbra up, CPU idle, queue climbing - so
        # it belongs on the page an operator opens when something feels wrong.
        "mail_gateway": gateway_health(),
    }


# The file the gateway collector writes. Read directly rather than queried
# through Prometheus: the answer is needed on a page that must still work when
# Prometheus is not installed, and "is the gateway up" is a single current
# value, not a range.
GATEWAY_PROM_FILE = "kin_mail_gateway.prom"
# Six collection intervals at 60s. Past this the collector has stopped, and a
# stale "up" is worse than no answer.
GATEWAY_STALE_AFTER_SEC = 360

_GATEWAY_UP = re.compile(
    r'^kin_mail_gateway_up\{host="(?P<host>[^"]*)",port="(?P<port>\d+)"\}\s+(?P<value>[01])'
)
_GATEWAY_LINKED = re.compile(r"^kin_mail_gateway_linked\s+([01])")
_GATEWAY_RELAY = re.compile(r"^kin_mail_gateway_relay_configured\s+([01])")


def gateway_health(*, now: float | None = None) -> dict[str, Any]:
    """Whether the mail gateway is answering, from the collector's own file.

    The gateway is the one component whose failure stops every message while
    this appliance still looks healthy, so the Monitoring tab has to be able to
    say so. Three states that must stay distinct:

      collector never ran   -> unknown. Say so; do not imply anything.
      collector ran, no link -> not linked. Normal right after a deploy.
      collector ran, link down -> the alarm this whole thing exists for.
    """
    now = time.time() if now is None else now
    path = os.path.join(TEXTFILE_DIR, GATEWAY_PROM_FILE)
    out: dict[str, Any] = {
        "known": False,
        "linked": False,
        "relay_configured": False,
        "ports": {},
        "host": "",
        "stale": False,
        "age_seconds": None,
    }
    try:
        age = now - os.stat(path).st_mtime
        body = _read(path)
    except OSError:
        return out
    if not body:
        return out

    out["known"] = True
    out["age_seconds"] = int(max(0.0, age))
    out["stale"] = age > GATEWAY_STALE_AFTER_SEC
    for line in body.splitlines():
        m = _GATEWAY_UP.match(line)
        if m:
            out["ports"][m.group("port")] = m.group("value") == "1"
            if m.group("host") and m.group("host") != "none":
                out["host"] = m.group("host")
            continue
        m = _GATEWAY_LINKED.match(line)
        if m:
            out["linked"] = m.group(1) == "1"
            continue
        m = _GATEWAY_RELAY.match(line)
        if m:
            out["relay_configured"] = m.group(1) == "1"

    # A stale file must not be read as a live "up". The collector stopping is
    # itself something to show, not something to paper over.
    if out["stale"]:
        out["ports"] = {}
    return out


def fetch_range(metric: str, range_name: str, *, now: float) -> list[dict[str, Any]]:
    params = build_range_params(metric, range_name, now=now)
    return parse_matrix(_get("/api/v1/query_range", params))


# The file the mail flow collector writes into node_exporter's textfile dir.
MAILFLOW_PROM_FILE = "kin_mail_flow.prom"
# Six collection intervals. The timer runs every 30s, so anything past this is
# not a slow tick, it is a collector that has stopped.
MAILFLOW_STALE_AFTER_SEC = 180


def mail_flow_freshness() -> dict[str, Any]:
    """How long ago the mail flow collector last wrote its metrics.

    This exists because of how node_exporter's textfile collector works: it
    re-publishes whatever is in the file on every scrape, forever, whether or
    not anything is still writing it. So a collector that died, or a disk that
    filled, or a permissions change, leaves Prometheus recording the same
    values every 15 seconds as though they were current. On the charts that is
    a flat line, which is exactly what a quiet mail server looks like, and on a
    monthly report it is a period that silently reports no traffic.

    The file's own mtime is the one thing that still tells the truth, and
    node_exporter publishes it for free.
    """
    # Matched with a regex, not equality.
    #
    # node_exporter labels this metric with the file it read, and whether that
    # is the bare name or the full path under the textfile directory has
    # differed between versions. An exact match on the bare name therefore
    # finds nothing on an appliance that is collecting perfectly well, and the
    # console then says "no mail flow metrics file is being collected" beside a
    # Reports tab full of real figures. Two contradictory statements on one
    # screen is worse than either alone: the operator cannot tell which to
    # believe, and the honest warning stops being believed at all.
    #
    # The pattern accepts both shapes and cannot match a different collector's
    # file, because the name is anchored at the end.
    # The backslash has to survive TWO parsers, and getting that wrong took the
    # Reports tab down with
    #   invalid parameter "query": parse error: unknown escape sequence U+002E
    # on a live appliance (QA Phase 16, 12 Sep 2026).
    #
    # PromQL string literals use Go escaping. Writing `\.` inside a
    # double-quoted PromQL string is not "an escaped dot", it is an *invalid
    # escape sequence*, and Prometheus rejects the whole query - so the panel
    # that exists to warn about stale figures instead printed a parser error
    # where the warning should have been.
    #
    # A backtick string in PromQL is raw: no escape processing at all, so what
    # is written here is exactly what the regex engine receives. That removes
    # the double-escaping question rather than solving it once.
    escaped = MAILFLOW_PROM_FILE.replace(".", "\\.")
    query = (
        "time() - node_textfile_mtime_seconds"
        "{file=~`(.*/)?" + escaped + "`}"
    )
    try:
        rows = parse_vector(_get("/api/v1/query", {"query": query}))
    except MonitoringError as exc:
        return {"known": False, "stale": False, "age_seconds": None, "reason": str(exc)}
    ages = [float(r["value"]) for r in rows if r.get("value") is not None]
    if not ages:
        return {
            "known": False,
            "stale": False,
            "age_seconds": None,
            "reason": (
                "No mail flow metrics file is being collected on this node. "
                "Mail is unaffected, but the Reports tab has nothing to count."
            ),
        }
    # Youngest wins: on a pair, one node writing is enough for the figures to
    # be current, and the replica legitimately has no Postfix spool to read.
    age = min(ages)
    stale = age > MAILFLOW_STALE_AFTER_SEC
    return {
        "known": True,
        "stale": stale,
        "age_seconds": age,
        "reason": (
            (
                "The mail flow collector last wrote "
                f"{int(age // 60)} minutes ago. Anything on the Monitoring and "
                "Reports tabs that comes from mail flow is that old, even "
                "though the charts will keep drawing a line."
            )
            if stale
            else ""
        ),
    }


def fetch_instant(metric: str) -> list[dict[str, Any]]:
    if not known_metric(metric):
        raise MonitoringError(f"unknown metric {metric!r}")
    _label, _unit, query = CATALOGUE[metric]
    return parse_vector(_get("/api/v1/query", {"query": query}))
