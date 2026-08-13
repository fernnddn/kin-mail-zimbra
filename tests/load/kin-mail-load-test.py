#!/usr/bin/env python3
"""Conservative concurrent IMAP / SMTP AUTH / HTTPS+SOAP load against KIN Mail.

Not a stress-to-failure tool. Default concurrency is small. Passwords come from
the environment so they are never argv. Host/SNI are CLI flags (no lab IPs in
this file).

Exit 0 if the stability bar is met; 1 if the bar is missed; 2 if the abort file
is seen or a watcher HTTPS sample is non-200 (when --https-watch is used).
"""
from __future__ import annotations

import argparse
import imaplib
import json
import os
import smtplib
import socket
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BAR_FAIL_PCT = 1.0
BAR_P95_HTTPS_MS = 2000
BAR_P95_AUTH_MS = 3000


def ctx(insecure: bool) -> ssl.SSLContext:
    if insecure:
        c = ssl._create_unverified_context()
    else:
        c = ssl.create_default_context()
    return c


def https_get(host: str, sni: str, insecure: bool, timeout: float) -> tuple[str, int, int]:
    t0 = time.perf_counter()
    url = f"https://{host}/"
    context = ctx(insecure)
    req = urllib.request.Request(url, method="GET", headers={"Host": sni})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    ms = int((time.perf_counter() - t0) * 1000)
    return "https_get", code, ms


def soap_auth(host: str, sni: str, user: str, password: str, insecure: bool, timeout: float) -> tuple[str, int, int]:
    t0 = time.perf_counter()
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">'
        "<soap:Body>"
        '<AuthRequest xmlns="urn:zimbraAccount">'
        f'<account by="name">{user}</account>'
        f"<password>{password}</password>"
        "</AuthRequest></soap:Body></soap:Envelope>"
    ).encode()
    url = f"https://{host}/service/soap"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Host": sni,
            "Content-Type": "application/soap+xml; charset=utf-8",
        },
    )
    code = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx(insecure)) as resp:
            payload = resp.read().decode(errors="replace")
            code = resp.getcode()
            if "AuthResponse" not in payload:
                code = 0
    except urllib.error.HTTPError as e:
        code = e.code
        payload = e.read().decode(errors="replace") if e.fp else ""
        if "AuthResponse" in payload:
            code = 200
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError):
        code = 0
    ms = int((time.perf_counter() - t0) * 1000)
    return "soap_auth", code, ms


def imap_login(host: str, user: str, password: str, insecure: bool, timeout: float) -> tuple[str, int, int]:
    t0 = time.perf_counter()
    code = 0
    try:
        client = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx(insecure), timeout=timeout)
        typ, _ = client.login(user, password)
        if typ == "OK":
            client.select("INBOX", readonly=True)
            code = 200
        client.logout()
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, socket.timeout, TimeoutError):
        code = 0
    ms = int((time.perf_counter() - t0) * 1000)
    return "imap_login", code, ms


def smtp_auth(host: str, user: str, password: str, insecure: bool, timeout: float) -> tuple[str, int, int]:
    """AUTH then QUIT. Does not send a message (load, not mailbox fill)."""
    t0 = time.perf_counter()
    code = 0
    try:
        smtp = smtplib.SMTP(host, 587, timeout=timeout)
        smtp.ehlo()
        smtp.starttls(context=ctx(insecure))
        smtp.ehlo()
        smtp.login(user, password)
        smtp.quit()
        code = 200
    except (smtplib.SMTPException, OSError, ssl.SSLError, socket.timeout, TimeoutError):
        code = 0
    ms = int((time.perf_counter() - t0) * 1000)
    return "smtp_auth", code, ms


def p95(values: list[int]) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(statistics.quantiles(values, n=20)[18])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="Connect address (VIP or Promoted IP)")
    ap.add_argument("--sni", default="mail.example.invalid", help="TLS / HTTP Host header")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-env", default="KIN_LOADTEST_PASSWORD")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--duration", type=int, default=45, help="seconds")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--insecure", action="store_true", help="Skip TLS verify (lab certs)")
    ap.add_argument("--abort-file", default="", help="Stop if this path exists")
    args = ap.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        print(f"missing env {args.password_env}", file=sys.stderr)
        return 2
    if args.concurrency < 1 or args.concurrency > 20:
        print("concurrency must be 1–20 (this is not a soak/stress tool)", file=sys.stderr)
        return 2

    stop = threading.Event()
    results: list[tuple[str, int, int]] = []
    lock = threading.Lock()

    ops = (https_get, soap_auth, imap_login, smtp_auth)

    def worker(_wid: int) -> None:
        while not stop.is_set():
            if args.abort_file and os.path.exists(args.abort_file):
                stop.set()
                return
            for fn in ops:
                if stop.is_set():
                    return
                try:
                    if fn is https_get:
                        row = fn(args.host, args.sni, args.insecure, args.timeout)
                    elif fn is soap_auth:
                        row = fn(args.host, args.sni, args.user, password, args.insecure, args.timeout)
                    else:
                        row = fn(args.host, args.user, password, args.insecure, args.timeout)
                except Exception:
                    row = (getattr(fn, "__name__", "op"), 0, 0)
                with lock:
                    results.append(row)
                    if row[0] == "https_get" and row[1] != 200:
                        stop.set()
                        return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(worker, i) for i in range(args.concurrency)]
        while time.time() - t0 < args.duration and not stop.is_set():
            if args.abort_file and os.path.exists(args.abort_file):
                stop.set()
                break
            time.sleep(0.25)
        stop.set()
        for fut in as_completed(futs, timeout=args.timeout + 5):
            fut.result()

    elapsed = time.time() - t0
    aborted = bool(args.abort_file and os.path.exists(args.abort_file))
    by_op: dict[str, list[tuple[int, int]]] = {}
    for name, code, ms in results:
        by_op.setdefault(name, []).append((code, ms))

    summary_ops = {}
    total = 0
    failed = 0
    for name, rows in sorted(by_op.items()):
        ok = [ms for code, ms in rows if code == 200]
        bad = [ms for code, ms in rows if code != 200]
        total += len(rows)
        failed += len(bad)
        summary_ops[name] = {
            "n": len(rows),
            "ok": len(ok),
            "fail": len(bad),
            "p50_ms": int(statistics.median(ok)) if ok else None,
            "p95_ms": p95(ok) if ok else None,
            "max_ms": max(ok) if ok else None,
        }

    fail_pct = (100.0 * failed / total) if total else 100.0
    https = summary_ops.get("https_get", {})
    bar = {
        "fail_pct_le": BAR_FAIL_PCT,
        "https_p95_ms_le": BAR_P95_HTTPS_MS,
        "auth_p95_ms_le": BAR_P95_AUTH_MS,
        "https_zero_hard_fail": True,
    }
    https_hard = any(code != 200 for name, code, _ms in results if name == "https_get")
    auth_p95s = [
        summary_ops[k]["p95_ms"]
        for k in ("soap_auth", "imap_login", "smtp_auth")
        if summary_ops.get(k, {}).get("p95_ms") is not None
    ]
    met = (
        not aborted
        and not https_hard
        and total > 0
        and fail_pct <= BAR_FAIL_PCT
        and (https.get("p95_ms") is None or https["p95_ms"] <= BAR_P95_HTTPS_MS)
        and (not auth_p95s or max(auth_p95s) <= BAR_P95_AUTH_MS)
    )
    out = {
        "host": args.host,
        "sni": args.sni,
        "concurrency": args.concurrency,
        "duration_s_requested": args.duration,
        "elapsed_s": round(elapsed, 2),
        "total": total,
        "failed": failed,
        "fail_pct": round(fail_pct, 3),
        "aborted": aborted,
        "https_non_200": https_hard,
        "bar": bar,
        "bar_met": met,
        "ops": summary_ops,
    }
    print(json.dumps(out, indent=2))
    if aborted or https_hard:
        return 2
    return 0 if met else 1


if __name__ == "__main__":
    sys.exit(main())
