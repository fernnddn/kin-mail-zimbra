#!/usr/bin/env python3
"""Is the mail gateway still there?

From 0.1.10 a Proxmox Mail Gateway sits in front of this appliance and carries
every message in and out. That makes it the single component whose failure
stops all mail while the appliance itself looks perfectly healthy: CPU is fine,
Zimbra is running, the queue just quietly climbs. Nothing else in the Monitoring
tab would say why.

This probes the two ports that matter and publishes what it found, on the same
path as every other metric: node_exporter textfile -> Prometheus on loopback ->
the console proxy. Nothing new listens on a port.

  kin_mail_gateway_up{port="26"}          1 if the relay port answered
  kin_mail_gateway_up{port="25"}          1 if the inbound port answered
  kin_mail_gateway_probe_seconds{port=}   how long the connect took
  kin_mail_gateway_linked                 1 if a link is configured AND applied
  kin_mail_gateway_relay_configured       1 if Zimbra really points at it

A TCP connect, deliberately, and not an SMTP conversation. Opening a session
and disconnecting mid-transaction every minute is the kind of thing that gets
an address rate-limited by its own gateway, and "the port answers" is the
question being asked.

  kin-mail-gateway-metrics.py            write the .prom file
  kin-mail-gateway-metrics.py --print    write nothing, print to stdout
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TEXTFILE_DIR = "/var/lib/node_exporter/textfile"
PROM_NAME = "kin_mail_gateway.prom"

GATEWAY_CONF = Path(os.environ.get("KIN_MAIL_GATEWAY_CONF", "/etc/kin-mail/mail-gateway.conf"))
GATEWAY_STATE = Path(
    os.environ.get(
        "KIN_PMG_STATE_FILE", "/var/lib/kin-mail-console/mail-gateway-state.json"
    )
)

# Long enough that a busy gateway is not called dead, short enough that a
# minute-interval timer never overlaps itself.
CONNECT_TIMEOUT = 5.0

RELAY_PORT = 26
INBOUND_PORT = 25


def read_config() -> dict[str, str]:
    """Parse the link configuration without executing it.

    Sourcing a config file lets a corrupted one run commands, and this runs as
    root on a timer.
    """
    out: dict[str, str] = {}
    try:
        text = GATEWAY_CONF.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def read_phase() -> str:
    try:
        data = json.loads(GATEWAY_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    return str(data.get("phase") or "") if isinstance(data, dict) else ""


def probe(host: str, port: int) -> tuple[bool, float]:
    """One TCP connect. Returns (reachable, seconds)."""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            pass
    except OSError:
        return False, time.monotonic() - started
    return True, time.monotonic() - started


def zimbra_relay_host() -> str:
    """What Zimbra is actually relaying through, as opposed to what we asked for.

    Read from Zimbra rather than from our own state file on purpose: the
    interesting failure is the two disagreeing, and a metric sourced from the
    same file as the claim cannot show that.
    """
    try:
        res = subprocess.run(
            ["su", "-", "zimbra", "-c", "zmprov gs $(zmhostname) zimbraMtaRelayHost"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in res.stdout.splitlines():
        if line.startswith("zimbraMtaRelayHost:"):
            return line.split(":", 1)[1].strip()
    return ""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(host: str, linked: bool, results: dict[int, tuple[bool, float]], relay_ok: bool) -> str:
    lines = [
        "# HELP kin_mail_gateway_linked Whether a mail gateway link is configured and applied.",
        "# TYPE kin_mail_gateway_linked gauge",
        f"kin_mail_gateway_linked {1 if linked else 0}",
        "# HELP kin_mail_gateway_up Whether the mail gateway answered on this port.",
        "# TYPE kin_mail_gateway_up gauge",
    ]
    label_host = _escape(host or "none")
    for port, (up, _secs) in sorted(results.items()):
        lines.append(
            f'kin_mail_gateway_up{{host="{label_host}",port="{port}"}} {1 if up else 0}'
        )
    lines += [
        "# HELP kin_mail_gateway_probe_seconds How long the TCP connect took.",
        "# TYPE kin_mail_gateway_probe_seconds gauge",
    ]
    for port, (_up, secs) in sorted(results.items()):
        lines.append(
            f'kin_mail_gateway_probe_seconds{{host="{label_host}",port="{port}"}} {secs:.4f}'
        )
    lines += [
        "# HELP kin_mail_gateway_relay_configured Whether Zimbra relays through the configured gateway.",
        "# TYPE kin_mail_gateway_relay_configured gauge",
        f"kin_mail_gateway_relay_configured {1 if relay_ok else 0}",
    ]
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--textfile-dir", default=DEFAULT_TEXTFILE_DIR)
    ap.add_argument("--print", dest="to_stdout", action="store_true")
    ap.add_argument("--skip-zimbra", action="store_true", help="do not shell out to zmprov")
    args = ap.parse_args(argv)

    conf = read_config()
    host = conf.get("GATEWAY_HOST", "").strip()
    enabled = conf.get("GATEWAY_ENABLED") == "1"
    linked = bool(host and enabled and read_phase() == "applied")

    results: dict[int, tuple[bool, float]] = {}
    relay_ok = False
    if host:
        for port in (RELAY_PORT, INBOUND_PORT):
            results[port] = probe(host, port)
        if not args.skip_zimbra:
            relay = zimbra_relay_host()
            # Compare the host half only: the port is ours to choose and a
            # deliberate change of it is not a fault.
            relay_ok = relay.split(":", 1)[0] == host if relay else False
    else:
        # No gateway configured. Publish zeros rather than nothing, so the
        # console can tell "not linked" from "the collector is not running".
        results = {RELAY_PORT: (False, 0.0), INBOUND_PORT: (False, 0.0)}

    body = render(host, linked, results, relay_ok)
    if args.to_stdout:
        sys.stdout.write(body)
        return 0
    _write_atomic(Path(args.textfile_dir) / PROM_NAME, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
