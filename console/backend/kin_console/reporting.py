"""Mail reports for the console, and CSV export.

The Monitoring tab answers "what is happening now". A report answers "what
happened last month", which is the question somebody asks once a month and
currently has to leave the console to answer - or cannot answer at all.

Everything here is READ-ONLY. It runs Prometheus range queries over the same
loopback proxy the Monitoring tab uses and does not touch mail, the cluster, or
any configuration.

Two shapes come out of the same query, because they are the same data at
different resolutions:

  a daily breakdown  one row per day, which is what an operator wants in a
                     spreadsheet - raw enough to pivot, aggregated enough to read
  period totals      the sum of those rows, which is what goes on the summary

Counters are cumulative (the collector never resets them on purpose), so a
period total is `increase()` over the window rather than a difference of two
samples: increase() copes with the counter resetting when the collector is
reinstalled, and a subtraction does not.

Day boundaries are the APPLIANCE's local days, not UTC. A monthly report that
ends at 07:00 on the 1st because the server reasoned in UTC is wrong in a way
nobody would think to check.

One limitation, stated rather than hidden: the daily breakdown asks Prometheus
for a fixed 24-hour step, so in a timezone that observes DST the buckets for
the days after a transition are offset by an hour from local midnight. Period
TOTALS are exact everywhere, because they are a single window measured in real
elapsed seconds. This appliance ships Asia/Jakarta, which has no DST, so the
breakdown is exact there; getting it exact in a DST zone would need one query
per day and 250 queries to draw one table is not a trade worth making.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .monitoring import (  # noqa: F401
    MonitoringError,
    PROM_TIMEOUT_SEC,
    _get,
    _num,
    parse_matrix,
    parse_vector,
)

# Report column -> (heading, PromQL over a window, "counter" | "gauge_max").
#
# Counters are summed over the period; gauges are reported at their worst point
# in it, because "the queue peaked at 400" is the fact worth keeping and an
# average queue depth of 3 hides it.
REPORT_SERIES: dict[str, tuple[str, str, str]] = {
    "accepted": (
        "Accepted",
        'sum(increase(kin_mail_events_total{event="accepted"}[$w]))',
        "counter",
    ),
    "delivered_in": (
        "Delivered to mailboxes",
        'sum(increase(kin_mail_delivered_total{transport="lmtp"}[$w]))',
        "counter",
    ),
    "delivered_out": (
        "Sent to other servers",
        'sum(increase(kin_mail_delivered_total{transport="smtp"}[$w]))',
        "counter",
    ),
    "rejected": (
        "Rejected at the door",
        'sum(increase(kin_mail_events_total{event="rejected"}[$w]))',
        "counter",
    ),
    "deferred": (
        # Postfix logs status=deferred on EVERY retry, so this counts delivery
        # attempts that were postponed, not distinct messages. A message that
        # retried nine times before going through appears nine times.
        "Deferral events",
        'sum(increase(kin_mail_undelivered_total{outcome="deferred"}[$w]))',
        "counter",
    ),
    "bounced": (
        "Bounced",
        'sum(increase(kin_mail_undelivered_total{outcome="bounced"}[$w]))',
        "counter",
    ),
    "queue_peak": (
        "Queue peak",
        "max(max_over_time(kin_mail_queue_messages[$w]))",
        "gauge_max",
    ),
    "queue_oldest_peak": (
        "Longest wait (seconds)",
        "max(max_over_time(kin_mail_queue_oldest_seconds[$w]))",
        "gauge_max",
    ),
}

# Order matters: it is the column order in the CSV and on screen.
REPORT_COLUMNS: tuple[str, ...] = tuple(REPORT_SERIES)

DAY_SECONDS = 86400

PERIODS: tuple[str, ...] = (
    "this_month",
    "last_month",
    "last_7d",
    "last_30d",
    "last_90d",
)
DEFAULT_PERIOD = "last_month"


ETC_TIMEZONE = "/etc/timezone"


def appliance_zone() -> tzinfo:
    """The appliance's real timezone, not a fixed offset.

    `datetime.astimezone()` attaches the offset in force AT THAT INSTANT and
    then keeps it. `.replace(day=1)` on a date in April therefore carried
    April's offset back to March, and in any timezone that observes DST the
    month boundaries came out an hour wrong - a monthly report that started and
    ended at 01:00. Jakarta has no DST so nothing showed locally, which is
    exactly why it was worth checking somewhere that does.

    A real ZoneInfo computes the offset per date. The name comes from
    /etc/timezone, which is what Ubuntu writes and what the wizard's TIMEZONE
    setting ends up in; TZ wins when set, so tests can pin it.
    """
    name = (os.environ.get("TZ") or "").strip()
    if not name:
        try:
            name = Path(ETC_TIMEZONE).read_text(encoding="utf-8").strip()
        except OSError:
            name = ""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    # Last resort: the fixed local offset. Wrong across a DST boundary, but
    # only reachable on a host with no usable zone database at all.
    return datetime.now().astimezone().tzinfo or timezone.utc


def now_local() -> datetime:
    return datetime.now(appliance_zone())


def _local_midnight(when: datetime) -> datetime:
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(when: datetime) -> datetime:
    return _local_midnight(when).replace(day=1)


def previous_month_start(when: datetime) -> datetime:
    first = month_start(when)
    return month_start(first - timedelta(days=1))


def period_bounds(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, human label) for a named period, in the appliance's own days.

    `end` is exclusive and never in the future: a report for the current month
    covers midnight on the 1st up to now, not up to a month boundary that has
    not happened. A report that padded the rest of the month with zeros would
    read as an outage.
    """
    name = (period or "").strip().lower() or DEFAULT_PERIOD
    # A caller's `now` may carry a fixed offset (that is what astimezone gives).
    # Re-anchor it so every .replace() below lands on the right offset for the
    # date it produces, not for the date it started from.
    now = now.astimezone(appliance_zone())
    if name not in PERIODS:
        raise MonitoringError(
            f"unknown report period {period!r} (expected one of: {', '.join(PERIODS)})"
        )
    if name == "this_month":
        start = month_start(now)
        return start, now, start.strftime("%B %Y")
    if name == "last_month":
        start = previous_month_start(now)
        return start, month_start(now), start.strftime("%B %Y")
    days = {"last_7d": 7, "last_30d": 30, "last_90d": 90}[name]
    start = _local_midnight(now) - timedelta(days=days - 1)
    return start, now, f"Last {days} days"


def build_daily_params(metric: str, start: datetime, end: datetime) -> dict[str, str]:
    """A Prometheus range query returning ONE POINT PER LOCAL DAY.

    Evaluated at the END of each day with a 1d lookback, so each point is that
    day's total. The first evaluation is start+1d rather than start, because
    `increase(x[1d])` at exactly `start` would report the day BEFORE the period.
    """
    if metric not in REPORT_SERIES:
        raise MonitoringError(f"unknown report series {metric!r}")
    if end.timestamp() <= start.timestamp():
        raise MonitoringError("report period ends before it starts")
    _heading, template, _kind = REPORT_SERIES[metric]
    first_eval = start + timedelta(days=1)
    if first_eval > end:
        first_eval = end
    return {
        "query": template.replace("$w", "1d"),
        "start": f"{first_eval.timestamp():.3f}",
        "end": f"{end.timestamp():.3f}",
        "step": str(DAY_SECONDS),
    }


def build_total_params(metric: str, start: datetime, end: datetime) -> dict[str, str]:
    """A single instant query for the whole period, evaluated at its end.

    Not a sum of the daily points: the last partial day of `this_month` is
    shorter than a day, and summing daily buckets would either miss it or
    count a whole day of it.
    """
    if metric not in REPORT_SERIES:
        raise MonitoringError(f"unknown report series {metric!r}")
    if end.timestamp() <= start.timestamp():
        raise MonitoringError("report period ends before it starts")
    _heading, template, _kind = REPORT_SERIES[metric]
    # Elapsed seconds from the epochs, NOT (end - start).total_seconds().
    # Subtracting two aware datetimes in the same zone is wall-clock
    # arithmetic: across a DST boundary it reports 31 days for a month that
    # actually lasted 743 hours, and the query window would then reach an hour
    # further back than the period and count that hour twice.
    window = max(1, int(round(end.timestamp() - start.timestamp())))
    return {
        "query": template.replace("$w", f"{window}s"),
        "time": f"{end.timestamp():.3f}",
    }


def _round_count(value: float | None, kind: str) -> float | int | None:
    """increase() interpolates, so a whole-message count comes back fractional."""
    if value is None:
        return None
    if kind == "counter":
        return int(round(value))
    if kind == "gauge_max":
        return int(round(value))
    return round(value, 2)


def daily_rows(
    per_metric: dict[str, list[tuple[float, float | None]]],
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Fold per-metric point lists into one row per day, oldest first.

    A day with no sample for a metric is 0 rather than blank: the collector
    reports zero traffic as zero, and a hole in a spreadsheet reads as missing
    data when it usually means a quiet Sunday.
    """
    by_day: dict[str, dict[str, Any]] = {}
    for metric, points in per_metric.items():
        kind = REPORT_SERIES.get(metric, ("", "", "counter"))[2]
        for ts, value in points:
            # The point is the END of the day it summarises.
            day = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
                appliance_zone()
            )
            label = (day - timedelta(seconds=1)).strftime("%Y-%m-%d")
            row = by_day.setdefault(label, {"date": label})
            row[metric] = _round_count(value, kind)
    out: list[dict[str, Any]] = []
    for label in sorted(by_day):
        row = by_day[label]
        for metric in REPORT_COLUMNS:
            row.setdefault(metric, 0)
        out.append(row)
    return out


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Facts a person would put in an email, derived from the daily rows."""
    if not rows:
        return {"busiest_day": None, "busiest_day_accepted": 0, "days": 0}
    busiest = max(rows, key=lambda r: (r.get("accepted") or 0))
    return {
        "busiest_day": busiest["date"],
        "busiest_day_accepted": busiest.get("accepted") or 0,
        "days": len(rows),
    }


def outcome_rates(totals: dict[str, Any]) -> dict[str, float | None]:
    """Share of FINAL delivery outcomes that succeeded, and that bounced.

    Deliberately not "delivered / accepted". Those two do not divide:
    `accepted` counts messages entering the queue, while the delivered counters
    count per RECIPIENT, so a message to three people makes the ratio exceed
    100%. The first version of this clamped that to 100 and hid it.

    Denominator is delivered + bounced - everything that reached a final
    answer. Deferrals are excluded because they are retries, not outcomes: a
    message that retried nine times and then arrived is one delivery, not nine
    failures.

    None when nothing reached a final outcome. That is a quiet month, not a
    0% delivery rate, and printing "0%" beside it would read as a total outage.
    """
    delivered = (totals.get("delivered_in") or 0) + (totals.get("delivered_out") or 0)
    bounced = totals.get("bounced") or 0
    final = delivered + bounced
    if not final:
        return {"delivered_pct": None, "bounced_pct": None}
    return {
        "delivered_pct": round((delivered / final) * 100, 1),
        "bounced_pct": round((bounced / final) * 100, 1),
    }


# --- CSV --------------------------------------------------------------------

def to_csv(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """Rows to CSV text. `columns` is [(key, heading)], in order.

    Written with the csv module rather than joining commas, because a heading
    or a hostname containing a comma or a quote has to be escaped and a
    hand-rolled join is exactly where that goes wrong. CRLF is what
    spreadsheets expect and what RFC 4180 specifies.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([heading for _key, heading in columns])
    for row in rows:
        writer.writerow([_csv_cell(row.get(key)) for key, _heading in columns])
    return buf.getvalue()


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def data_quality_notes(report: dict[str, Any]) -> list[str]:
    """Every reason this report might not mean what it appears to mean.

    Built from the report payload rather than re-queried, so JSON and CSV can
    never disagree about whether the numbers are trustworthy.

    The order is the order an operator needs it: whether the collector is
    installed at all, then whether it is still writing, then which individual
    figures could not be fetched, then whether the period is simply empty.

    A phase of "unknown" is deliberately not a note on its own. Appliances
    deployed before the state file existed read back unknown while collecting
    perfectly well, and mail_flow already reports a collector that is genuinely
    absent. Warning on unknown would put a permanent scare on healthy boxes,
    which is how a real warning stops being read.
    """
    notes: list[str] = []

    install = report.get("metrics_install") or {}
    phase = str(install.get("phase") or "")
    if phase not in ("", "ok", "unknown"):
        reason = str(install.get("reason") or "")
        if phase == "running":
            notes.append(
                "The built-in monitoring install is still running on this "
                "appliance. Figures below start from the moment it finishes."
            )
        else:
            notes.append(
                reason
                or "The built-in monitoring install did not succeed on this "
                "appliance, so these figures may be counting nothing."
            )

    flow = report.get("mail_flow") or {}
    if not flow.get("known") or flow.get("stale"):
        reason = str(flow.get("reason") or "")
        if reason:
            notes.append(reason)

    unavailable = list(report.get("unavailable") or [])
    if unavailable:
        headings = [
            REPORT_SERIES[key][0] if key in REPORT_SERIES else key
            for key in unavailable
        ]
        notes.append(
            "These figures could not be read and are blank rather than zero: "
            + ", ".join(headings)
            + "."
        )

    if report.get("no_data"):
        # What an empty period MEANS depends on whether anything is collecting.
        #
        # Accusing every empty period of a missing pipeline is a false alarm on
        # the most ordinary case there is: an appliance deployed three weeks
        # ago, asked for last month. Collection was working, the install
        # succeeded, and the report still said this looks like a box with no
        # metrics. A warning that fires on healthy appliances is one operators
        # learn to scroll past, which costs exactly the times it is right.
        collecting = bool(flow.get("known")) and not flow.get("stale")
        installed_ok = phase == "ok"
        if collecting and installed_ok:
            notes.append(
                "No samples were recorded in this period, although metrics "
                "collection is working on this appliance now. That usually "
                "means the period is older than the collector. Figures start "
                "when collection starts and are never back-filled."
            )
        else:
            notes.append(
                "No samples at all were returned for this period. That is not "
                "the same as no mail: it is what an appliance with no metrics "
                "collection looks like."
            )

    return notes


def report_csv(report: dict[str, Any]) -> str:
    """The report as CSV, carrying the same warnings the JSON carries.

    The CSV used to be built straight from the daily rows, which made it the
    most dangerous surface in the console: a spreadsheet of clean zeroes, with
    a confident filename and no hint that nothing had ever been collected, is
    exactly the artefact that gets forwarded to somebody who was not there.
    Whatever the Reports tab says on screen has to travel with the file.

    Notes are `#` comment lines above the header. Spreadsheets import them as
    text rather than silently dropping them, and a reader who opens the file in
    anything at all sees them first.
    """
    notes = data_quality_notes(report)
    prefix = ""
    if notes:
        lines = ["# WARNING: read these before using the figures below."]
        lines += [f"# {note}" for note in notes]
        lines.append("#")
        prefix = "".join(f"{line}\r\n" for line in lines)
    body = to_csv(list(report.get("daily") or []), report_csv_columns())
    return prefix + body


def report_csv_columns() -> list[tuple[str, str]]:
    return [("date", "Date")] + [
        (key, REPORT_SERIES[key][0]) for key in REPORT_COLUMNS
    ]


def series_csv(
    charts: list[dict[str, Any]],
    *,
    tz_name: str = "",
) -> str:
    """Monitoring samples as CSV: one row per sample, long format.

    Long rather than a column per metric: the metrics do not share a timestamp
    grid once a node has been down, and a wide table would silently align
    samples that were minutes apart. Long format is also what a pivot table
    wants.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(
        ["timestamp_utc", "timestamp_local", "metric", "label", "unit", "instance", "value"]
    )
    for chart in charts:
        metric = str(chart.get("metric") or "")
        label = str(chart.get("label") or "")
        unit = str(chart.get("unit") or "")
        for one in chart.get("series") or []:
            instance = str(one.get("instance") or "")
            for point in one.get("points") or []:
                try:
                    ts, value = point[0], point[1]
                except (TypeError, IndexError):
                    continue
                when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                writer.writerow(
                    [
                        when.strftime("%Y-%m-%d %H:%M:%S"),
                        when.astimezone(appliance_zone()).strftime("%Y-%m-%d %H:%M:%S"),
                        metric,
                        label,
                        unit,
                        instance,
                        "" if value is None else value,
                    ]
                )
    return buf.getvalue()


def csv_filename(prefix: str, start: datetime | None = None, end: datetime | None = None) -> str:
    """A filename that sorts and says what it holds, with no spaces."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in prefix).strip("-")
    if start and end:
        return f"{safe}-{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}.csv"
    return f"{safe}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"


# --- fetching ---------------------------------------------------------------
#
# Blocking urllib, like the rest of the monitoring proxy. Callers run it in a
# thread. A series that fails comes back empty rather than failing the report:
# one dead query must not blank a monthly summary the operator is about to
# hand to somebody.


def _first_series_points(series: list[dict[str, Any]]) -> list[tuple[float, float | None]]:
    if not series:
        return []
    return [(p[0], p[1]) for p in series[0].get("points") or []]


def fetch_daily(metric: str, start: datetime, end: datetime) -> list[tuple[float, float | None]]:
    params = build_daily_params(metric, start, end)
    return _first_series_points(parse_matrix(_get("/api/v1/query_range", params)))


def fetch_total(metric: str, start: datetime, end: datetime) -> float | None:
    params = build_total_params(metric, start, end)
    rows = parse_vector(_get("/api/v1/query", params))
    if not rows:
        return None
    return rows[0].get("value")


def build_report(period: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The whole report. Read-only; raises MonitoringError only on a bad period."""
    clock = now or now_local()
    start, end, label = period_bounds(period, clock)

    per_metric: dict[str, list[tuple[float, float | None]]] = {}
    totals: dict[str, Any] = {}
    failed: list[str] = []
    for metric in REPORT_COLUMNS:
        kind = REPORT_SERIES[metric][2]
        try:
            per_metric[metric] = fetch_daily(metric, start, end)
        except MonitoringError:
            per_metric[metric] = []
            failed.append(metric)
        try:
            totals[metric] = _round_count(fetch_total(metric, start, end), kind)
        except MonitoringError:
            totals[metric] = None
            if metric not in failed:
                failed.append(metric)

    rows = daily_rows(per_metric, start=start, end=end)
    return {
        "period": (period or DEFAULT_PERIOD).strip().lower() or DEFAULT_PERIOD,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "generated_at": clock.isoformat(),
        "columns": [
            {"key": key, "heading": REPORT_SERIES[key][0], "kind": REPORT_SERIES[key][2]}
            for key in REPORT_COLUMNS
        ],
        "totals": totals,
        **outcome_rates(totals),
        "daily": rows,
        **summarise(rows),
        # Named rather than hidden: a summary with a silently missing column is
        # worse than one that says which number it could not get.
        "unavailable": failed,
        # Absence, not emptiness. Prometheus answers a query about a metric it
        # has never seen with a successful, empty result, so a node that never
        # had the collector installed produces no error anywhere above: every
        # total is None and every daily row is missing. Without this flag that
        # is indistinguishable from a genuinely quiet month, and the console
        # would render "no problems" over a pipeline that does not exist.
        "no_data": all(totals.get(key) is None for key in REPORT_COLUMNS),
    }
