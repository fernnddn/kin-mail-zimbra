"""What a multi deployment looks like, seen from the edge.

This is not a cluster and the distinction is the point. Three machines with
three different jobs and no shared state: the Proxmox Mail Gateway filters and
is the MX, the edge runs the MTA and the proxy and holds no mail, the mailbox
holds the directory and every message. Nothing is replicated, nothing fails
over, there is no quorum and there is no fencing.

So there is nothing here about DRBD, qdevice or SBD. Those belong to the
replicated pair this product used to build, and asking pcs about them on a
multi deployment returns either noise or nothing at all - which is how the
Cluster page came to describe a three-machine deployment as "Single server".

Health is the LINKS, because the links are what break. Each probe opens a TCP
connection to the port the next machine in the chain actually has to answer on.
A successful connect proves two of the three things that go wrong at once: the
service is listening, and the firewall in front of it lets this host through.
ICMP proves neither - a pingable mailbox with ufw misconfigured looks perfect
and delivers nothing.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

# Short on purpose. These are machines on the same LAN as this one; a probe
# that takes longer than this is already a fault, and the Cluster page refreshes
# on a timer, so a slow probe would stack up behind the next one.
PROBE_TIMEOUT_SEC = 3.0


def _gateway_conf() -> Path:
    return Path(
        os.environ.get("KIN_MAIL_GATEWAY_CONF", "/etc/kin-mail/mail-gateway.conf")
    )


def _config_file() -> Path:
    """Resolved per call, not at import.

    KIN_MAIL_CONFIG is the override the rest of the privhelper already uses,
    and reading it at call time is what lets this be tested without a real
    /etc/kin-mail on the machine running the tests.
    """
    return Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))


def _config() -> dict[str, str]:
    """/etc/kin-mail/config as a dict, or empty when it cannot be read."""
    try:
        from .apply_config import parse_config

        path = _config_file()
        if not path.is_file():
            return {}
        return parse_config(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gateway_host() -> str:
    """The linked Proxmox Mail Gateway, or "" when none is linked.

    Read from the gateway's own config file rather than the appliance config:
    linking a gateway is a separate operation with its own state, and a
    hostname left in the appliance config by an earlier attempt would draw a
    machine on the page that is not actually in the path.
    """
    try:
        path = _gateway_conf()
        if not path.is_file():
            return ""
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("GATEWAY_HOST="):
                continue
            return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        return ""
    return ""


def _gateway_enabled() -> bool:
    try:
        path = _gateway_conf()
        if not path.is_file():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GATEWAY_ENABLED="):
                return line.split("=", 1)[1].strip().strip("\"'") == "1"
    except OSError:
        return False
    return False


async def probe_tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """True when something accepts a TCP connection on host:port."""
    if not host:
        return False
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass


# The ports each machine has to answer on for mail to move, and what to call
# them on the page. Named rather than numbered in the UI: an operator reading
# "DIRECTORY" knows what is broken; reading "389" they have to look it up.
EDGE_PORTS: tuple[tuple[int, str], ...] = (
    (25, "SMTP"),
    (443, "HTTPS"),
    (587, "Submission"),
    (993, "IMAPS"),
)
MAILBOX_LINKS: tuple[tuple[int, str], ...] = (
    (389, "Directory"),
    (7025, "Mail delivery"),
)
# Outbound relay. The gateway listens on 25 for the internet and on 26 for this
# appliance, so 26 is the one that proves OUR path to it.
GATEWAY_PORT = 26


def _local_firewall_active() -> bool | None:
    """Is ufw running on this machine? None when it cannot be determined.

    Asked because it can switch itself off. Applying the host firewall arms a
    dead man that disables ufw again after five minutes unless somebody
    cancels it - which is the right design, because rules that lock the
    operator out otherwise lock them out permanently.
    ONLY the mailbox has that cancelled for it: the orchestrator holds an SSH
    connection through the change and cancels on the proof that it survived.
    Nobody does that for the edge, so an operator who misses the prompt is left
    with an internet-facing mail server and no host firewall - and until now
    nothing anywhere said so. Seen on the live edge, disabled for a day
    (20 Sep 2026).
    """
    import shutil
    import subprocess

    if shutil.which("ufw") is None:
        return None
    try:
        out = subprocess.run(
            ["ufw", "status"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return "Status: active" in (out.stdout or "")


async def gather() -> dict[str, Any]:
    """Nodes and links of a multi deployment, probed concurrently."""
    conf = _config()
    edge_host = (conf.get("EDGE_HOST") or conf.get("MAIL_HOST") or "").strip()
    edge_ip = (conf.get("EDGE_IP") or conf.get("SERVER_IP") or "").strip()
    mailbox_host = (conf.get("MAILBOX_HOST") or "").strip()
    mailbox_ip = (conf.get("MAILBOX_IP") or "").strip()
    gateway_host = _gateway_host()
    gateway_linked = bool(gateway_host) and _gateway_enabled()

    edge_tasks = [probe_tcp("127.0.0.1", port) for port, _ in EDGE_PORTS]
    mailbox_tasks = [probe_tcp(mailbox_ip, port) for port, _ in MAILBOX_LINKS]
    gateway_task = probe_tcp(gateway_host, GATEWAY_PORT) if gateway_linked else None

    tasks: list[Any] = [*edge_tasks, *mailbox_tasks]
    if gateway_task is not None:
        tasks.append(gateway_task)
    results = await asyncio.gather(*tasks)

    edge_results = list(results[: len(EDGE_PORTS)])
    mailbox_results = list(
        results[len(EDGE_PORTS) : len(EDGE_PORTS) + len(MAILBOX_LINKS)]
    )
    gateway_ok = bool(results[-1]) if gateway_task is not None else False

    edge_services = [
        {"label": label, "port": port, "ok": bool(ok)}
        for (port, label), ok in zip(EDGE_PORTS, edge_results)
    ]
    mailbox_links = [
        {"label": label, "port": port, "ok": bool(ok)}
        for (port, label), ok in zip(MAILBOX_LINKS, mailbox_results)
    ]

    edge_ok = all(s["ok"] for s in edge_services)
    # Not folded into edge_ok. A host with no firewall still carries mail, and
    # colouring the whole node red would say "mail is broken" about something
    # that is a real and separate problem.
    firewall = _local_firewall_active()
    edge_warnings = []
    if firewall is False:
        edge_warnings.append(
            "The host firewall is not running on this machine. Applying it arms "
            "a dead man that switches ufw off again after five minutes unless "
            "it is cancelled, and it was not. Re-apply it from Settings, then "
            "cancel the dead man."
        )
    mailbox_ok = bool(mailbox_ip) and all(link["ok"] for link in mailbox_links)

    nodes = [
        {
            "role": "gateway",
            "title": "Mail gateway",
            "name": gateway_host or "No mail gateway",
            "ip": gateway_host,
            # A gateway that has never been linked is not a fault to draw in
            # red: it is a machine that is not part of the deployment yet. The
            # Cluster page draws it as a placeholder and the healthcheck is
            # where it is chased up.
            "present": gateway_linked,
            "ok": gateway_ok,
            "checks": (
                [{"label": "Relay in", "port": GATEWAY_PORT, "ok": gateway_ok}]
                if gateway_linked
                else []
            ),
            "detail": (
                "Filters inbound mail and is the MX"
                if gateway_linked
                else "Not linked yet - this appliance is still its own MX"
            ),
        },
        {
            "role": "edge",
            "title": "Edge",
            "name": edge_host or "This server",
            "ip": edge_ip,
            "present": True,
            "ok": edge_ok,
            "checks": edge_services,
            "detail": "MTA and proxy. Holds no mail.",
            "local": True,
            "warnings": edge_warnings,
        },
        {
            "role": "mailbox",
            "title": "Mailbox",
            "name": mailbox_host or "Mailbox",
            "ip": mailbox_ip,
            "present": bool(mailbox_ip),
            "ok": mailbox_ok,
            "checks": mailbox_links,
            "detail": "Directory and mail store. Not reachable from the internet.",
        },
    ]

    links = [
        {
            "from": "gateway",
            "to": "edge",
            "label": "FILTERED MAIL",
            "present": gateway_linked,
            "ok": gateway_linked and gateway_ok and edge_ok,
        },
        {
            "from": "edge",
            "to": "mailbox",
            "label": "DIRECTORY + DELIVERY",
            "present": bool(mailbox_ip),
            "ok": edge_ok and mailbox_ok,
        },
    ]

    healthy = edge_ok and mailbox_ok and (gateway_ok if gateway_linked else True)
    # A warning is not an outage, but it must not be swallowed either: the page
    # reads this to decide whether to say "needs attention".
    return {
        "kind": "split",
        "nodes": nodes,
        "links": links,
        "healthy": healthy,
        "warned": bool(edge_warnings),
        "gateway_linked": gateway_linked,
    }
