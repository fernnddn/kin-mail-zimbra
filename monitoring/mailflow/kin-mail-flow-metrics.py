#!/usr/bin/env python3
"""Mail flow metrics for the console's Monitoring tab.

The Monitoring tab could tell an operator that the machine was busy. It could
not tell them whether mail was actually moving - the one question a mail
appliance exists to answer. CPU at 4% means nothing if the deferred queue has
been climbing for six hours.

Two sources, both cheap and both local:

  the Postfix log   what happened - accepted, delivered, deferred, bounced
  the Postfix spool what is waiting right now - queue depth and oldest age

Written as a node_exporter textfile so it travels the same path as every other
metric: node_exporter -> Prometheus on loopback -> the console proxy. Nothing
new listens on a port, and the browser still asks for named metrics rather
than sending PromQL.

Counters are cumulative and survive restarts, because Prometheus wants a
counter it can rate() rather than a per-interval delta. State (log position
plus running totals) lives beside the .prom file. A rotated or truncated log
is detected and read from the beginning rather than skipped or double-counted.

  kin-mail-flow-metrics.py                 write the .prom file
  kin-mail-flow-metrics.py --print         write nothing, print to stdout
  kin-mail-flow-metrics.py --reset         forget counters and start over
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_LOG = "/var/log/zimbra.log"
DEFAULT_SPOOL = "/opt/zimbra/data/postfix/spool"
DEFAULT_TEXTFILE_DIR = "/var/lib/node_exporter/textfile"
PROM_NAME = "kin_mail_flow.prom"
STATE_NAME = "kin_mail_flow.state.json"

# Never read more than this in one pass. A log that grew by gigabytes while
# this was not running must not be turned into a multi-minute blocking read on
# a live mail node.
MAX_READ_BYTES = 64 * 1024 * 1024

QUEUES = ("incoming", "active", "deferred", "hold", "corrupt")

# Postfix delivery outcomes. Zimbra hands final delivery to lmtp (into a local
# mailbox) and uses smtp to relay outward, so the transport is what separates
# inbound from outbound without having to parse addresses.
_DELIVERY = re.compile(
    r"postfix(?:-\w+)?/(?P<transport>lmtp|smtp|local|virtual|pipe)\[\d+\]:\s"
    r"(?P<qid>[0-9A-Fa-f]+):.*?\bstatus=(?P<status>sent|bounced|deferred|expired)\b"
)
# A message accepted into the queue. NOQUEUE lines are rejections and never
# carry a queue id, which is what separates the two.
_ACCEPTED = re.compile(r"postfix(?:-\w+)?/smtpd\[\d+\]:\s(?P<qid>[0-9A-Fa-f]+):\sclient=")
_REJECTED = re.compile(r"postfix(?:-\w+)?/smtpd\[\d+\]:\sNOQUEUE:\sreject:")
# Connections refused before a transaction - rate limits, blocklists, TLS.
_LOST = re.compile(r"postfix(?:-\w+)?/smtpd\[\d+\]:\s(?:lost connection|timeout) after")

_COUNTER_KEYS = (
    "accepted",
    "rejected",
    "lost",
    "delivered_lmtp",
    "delivered_smtp",
    "deferred",
    "bounced",
    "expired",
)


def new_counters() -> dict[str, int]:
    return {k: 0 for k in _COUNTER_KEYS}


def parse_lines(lines: Any, counters: dict[str, int]) -> dict[str, int]:
    """Fold log lines into cumulative counters. Pure: no I/O, no clock."""
    for line in lines:
        if "postfix" not in line:
            continue
        m = _DELIVERY.search(line)
        if m:
            status = m.group("status")
            transport = m.group("transport")
            if status == "sent":
                if transport == "lmtp":
                    counters["delivered_lmtp"] += 1
                else:
                    counters["delivered_smtp"] += 1
            elif status == "deferred":
                counters["deferred"] += 1
            elif status == "bounced":
                counters["bounced"] += 1
            elif status == "expired":
                counters["expired"] += 1
            continue
        if _ACCEPTED.search(line):
            counters["accepted"] += 1
            continue
        if _REJECTED.search(line):
            counters["rejected"] += 1
            continue
        if _LOST.search(line):
            counters["lost"] += 1
    return counters


def queue_depths(spool: Path) -> dict[str, int]:
    """Messages sitting in each Postfix queue right now.

    Counted by walking the spool rather than shelling out to postqueue: this
    runs every 30s as an unprivileged collector on a live mail node, and it
    must not depend on Postfix being responsive to tell you Postfix is stuck.
    """
    out: dict[str, int] = {}
    for q in QUEUES:
        d = spool / q
        n = 0
        if d.is_dir():
            for _root, _dirs, files in os.walk(d):
                n += len(files)
        out[q] = n
    return out


def oldest_queued_seconds(spool: Path, *, now: float | None = None) -> float:
    """Age of the oldest message in the deferred or active queue, in seconds.

    This is the number that says "mail has been stuck since breakfast". Queue
    depth alone does not: ten messages that arrived a second ago and ten that
    have been retrying since 03:00 are the same count.
    """
    clock = time.time() if now is None else now
    oldest: float | None = None
    for q in ("active", "deferred"):
        d = spool / q
        if not d.is_dir():
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    mtime = os.stat(os.path.join(root, f)).st_mtime
                except OSError:
                    continue
                if oldest is None or mtime < oldest:
                    oldest = mtime
    if oldest is None:
        return 0.0
    return max(0.0, clock - oldest)


HEAD_BYTES = 256


def _head_fingerprint(log: Path, length: int) -> tuple[str, int]:
    """Hash of the first `length` bytes, and how many bytes were actually read.

    Inode catches a rename-and-recreate rotation and a shrinking size catches
    an obvious truncation, but neither catches logrotate's copytruncate when
    the replacement happens to reach the same length as what was already read.
    That looks exactly like "no new data" and the collector goes quiet.

    The comparison length is stored with the hash and reused on the next pass.
    Hashing "the first 256 bytes, or the whole file if it is shorter" is what
    a first attempt at this did, and on a young log every append changed the
    hash and was read as a truncation - re-counting the whole file every 30
    seconds. A fixed prefix cannot be changed by an append at any file size.
    """
    want = max(1, min(HEAD_BYTES, length or HEAD_BYTES))
    try:
        with log.open("rb") as fh:
            head = fh.read(want)
    except OSError:
        return "", 0
    if not head:
        return "", 0
    return hashlib.sha256(head).hexdigest()[:16], len(head)


def read_new_lines(log: Path, state: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Return (new lines, updated position state).

    A rotated, truncated or replaced log is read from the beginning rather
    than skipped: counting a few events twice once is better than silently
    losing a day of mail flow.
    """
    try:
        st = log.stat()
    except OSError:
        return [], state
    inode = getattr(st, "st_ino", 0)
    size = st.st_size
    offset = int(state.get("offset") or 0)
    prev_inode = int(state.get("inode") or 0)
    prev_head = str(state.get("head") or "")
    prev_head_len = int(state.get("head_len") or 0)

    # Re-read exactly the prefix the stored hash was taken over, so an append
    # can never look like a change.
    same_prefix, _n = _head_fingerprint(log, prev_head_len) if prev_head_len else ("", 0)

    if prev_inode and inode != prev_inode:
        offset = 0          # rotated away and recreated
    elif size < offset:
        offset = 0          # truncated to something shorter
    elif prev_head and same_prefix and same_prefix != prev_head:
        offset = 0          # same inode, same-or-greater size, new content
    if size - offset > MAX_READ_BYTES:
        # Way behind. Skip to the tail rather than block the node.
        offset = size - MAX_READ_BYTES

    lines: list[str] = []
    try:
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            data = fh.read(MAX_READ_BYTES)
            lines = data.splitlines()
            # A final line with no newline is still being written; leave it for
            # the next pass instead of counting half an event.
            if data and not data.endswith("\n") and lines:
                lines.pop()
                consumed = len(data) - len(data.rsplit("\n", 1)[-1])
            else:
                consumed = len(data)
    except OSError:
        return [], state
    head, head_len = _head_fingerprint(log, HEAD_BYTES)
    return lines, {
        "inode": inode,
        "offset": offset + consumed,
        "head": head,
        "head_len": head_len,
    }


def render(
    counters: dict[str, int],
    depths: dict[str, int],
    oldest: float,
    *,
    ok: bool,
    now: float,
    mta: bool = True,
) -> str:
    L: list[str] = []
    a = L.append
    a("# HELP kin_mail_events_total Postfix SMTP events since this collector started.")
    a("# TYPE kin_mail_events_total counter")
    for event, key in (
        ("accepted", "accepted"),
        ("rejected", "rejected"),
        ("lost", "lost"),
    ):
        a(f'kin_mail_events_total{{event="{event}"}} {counters[key]}')
    a("# HELP kin_mail_delivered_total Messages delivered, by transport.")
    a("# TYPE kin_mail_delivered_total counter")
    a(f'kin_mail_delivered_total{{transport="lmtp"}} {counters["delivered_lmtp"]}')
    a(f'kin_mail_delivered_total{{transport="smtp"}} {counters["delivered_smtp"]}')
    a("# HELP kin_mail_undelivered_total Messages not delivered, by outcome.")
    a("# TYPE kin_mail_undelivered_total counter")
    for outcome in ("deferred", "bounced", "expired"):
        a(f'kin_mail_undelivered_total{{outcome="{outcome}"}} {counters[outcome]}')
    a("# HELP kin_mail_queue_messages Messages currently in each Postfix queue.")
    a("# TYPE kin_mail_queue_messages gauge")
    for q in QUEUES:
        a(f'kin_mail_queue_messages{{queue="{q}"}} {depths.get(q, 0)}')
    a("# HELP kin_mail_queue_oldest_seconds Age of the oldest queued message.")
    a("# TYPE kin_mail_queue_oldest_seconds gauge")
    a(f"kin_mail_queue_oldest_seconds {oldest:.0f}")
    a("# HELP kin_mail_flow_up 1 when this collector read the mail log successfully.")
    a("# TYPE kin_mail_flow_up gauge")
    a(f"kin_mail_flow_up {1 if ok else 0}")
    a("# HELP kin_mail_flow_mta 1 when this node runs Postfix.")
    a("# TYPE kin_mail_flow_mta gauge")
    a(f"kin_mail_flow_mta {1 if mta else 0}")
    a("# HELP kin_mail_flow_last_run_timestamp_seconds When this collector last ran.")
    a("# TYPE kin_mail_flow_last_run_timestamp_seconds gauge")
    a(f"kin_mail_flow_last_run_timestamp_seconds {now:.0f}")
    return "\n".join(L) + "\n"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"inode": 0, "offset": 0, "counters": new_counters()}
    counters = new_counters()
    for k, v in (data.get("counters") or {}).items():
        if k in counters:
            try:
                counters[k] = max(0, int(v))
            except (TypeError, ValueError):
                pass
    return {
        "inode": int(data.get("inode") or 0),
        "offset": int(data.get("offset") or 0),
        "head": str(data.get("head") or ""),
        "head_len": int(data.get("head_len") or 0),
        "counters": counters,
    }


def _write_atomic(path: Path, body: str) -> None:
    # node_exporter reads this directory continuously. A partial file would be
    # parsed as a broken metric set, so write beside it and rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KIN Mail flow metrics collector")
    ap.add_argument("--log", default=os.environ.get("KIN_MAIL_LOG", DEFAULT_LOG))
    ap.add_argument("--spool", default=os.environ.get("KIN_MAIL_SPOOL", DEFAULT_SPOOL))
    ap.add_argument(
        "--textfile-dir",
        default=os.environ.get("KIN_TEXTFILE_DIR", DEFAULT_TEXTFILE_DIR),
    )
    ap.add_argument("--print", dest="to_stdout", action="store_true")
    ap.add_argument("--reset", action="store_true", help="forget counters and reread")
    args = ap.parse_args(argv)

    log = Path(args.log)
    spool = Path(args.spool)
    out_dir = Path(args.textfile_dir)
    state_path = out_dir / STATE_NAME

    state = {"inode": 0, "offset": 0, "head": "", "head_len": 0, "counters": new_counters()}
    if not args.reset:
        state = _load_state(state_path)

    lines, pos = read_new_lines(log, state)
    counters = parse_lines(lines, state["counters"])
    ok = log.exists()
    mta = spool.is_dir()

    depths = queue_depths(spool)
    oldest = oldest_queued_seconds(spool)
    body = render(counters, depths, oldest, ok=ok, now=time.time(), mta=mta)

    if args.to_stdout:
        sys.stdout.write(body)
        return 0

    # Everything above is pure computation. Only the writes can fail here, and
    # when they do (disk full, the textfile directory removed or its mode
    # changed) the useful thing is one legible line in the journal every
    # interval, not a Python traceback every interval. Exit non-zero so
    # `systemctl status kin-mail-flow` shows the fault, and leave the previous
    # .prom in place: stale numbers plus a stale mtime is a state the console
    # can detect and report, whereas a truncated file is not.
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"kin-mail-flow: cannot create textfile directory {out_dir}: {exc}",
            file=sys.stderr,
        )
        return 3
    try:
        _write_atomic(out_dir / PROM_NAME, body)
        _write_atomic(
            state_path,
            json.dumps({**pos, "counters": counters}, separators=(",", ":")) + "\n",
        )
    except OSError as exc:
        print(
            f"kin-mail-flow: cannot write metrics into {out_dir}: {exc}. "
            "Mail is unaffected; the console reports these figures as stale "
            "from the age of the last file written.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
