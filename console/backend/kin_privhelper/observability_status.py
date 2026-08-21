"""Observability witness identity and reachability (no cluster mutation).

qdevice_ok is a Pacemaker vote signal. This module answers a different
question: is a witness VM configured, and does its qnetd port accept TCP.
The Cluster topology diagram and Add/Remove Observability gating both use
this snapshot so they cannot drift apart.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .qdevice_status import parse_qnetd_host

QNETD_PORT = 5403
PROBE_TIMEOUT_SEC = 2.0


def configured_observability_identity(*, config_ip: str, qnetd_host: str) -> str:
    """Prefer /etc/kin-mail/config, then corosync quorum.device host."""
    cfg = (config_ip or "").strip()
    if cfg:
        return cfg
    return (qnetd_host or "").strip()


def classify_observability(*, identity: str, reachable: bool) -> str:
    """absent | unreachable | healthy. Reachable is the TCP probe, not qdevice_ok."""
    if not (identity or "").strip():
        return "absent"
    return "healthy" if reachable else "unreachable"


def observability_actions(status: str) -> dict[str, bool]:
    """Exact operator gating: Add only when absent, Remove only when unreachable."""
    return {
        "add": status == "absent",
        "remove": status == "unreachable",
    }


def observability_public(
    *,
    identity: str,
    hostname: str,
    status: str,
) -> dict[str, Any]:
    present = status != "absent"
    ident = (identity or "").strip() if present else ""
    return {
        "present": present,
        "status": status,
        "ip": ident,
        "hostname": (hostname or "").strip() if present else "",
        "reachable": status == "healthy",
        "can_add": status == "absent",
        "can_remove": status == "unreachable",
    }


async def probe_tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """True when host:port accepts a TCP connect. Never used as qdevice_ok."""
    target = (host or "").strip()
    if not target:
        return False
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target, int(port)),
            timeout,
        )
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def probe_observability_reachable(identity: str) -> bool:
    """Witness live check: qnetd listen port. Independent of pcs quorum votes."""
    return await probe_tcp(identity, QNETD_PORT)


def snapshot_from_texts(
    *,
    config_ip: str,
    corosync_conf: str,
    reachable: bool,
    hostname: str = "",
) -> dict[str, Any]:
    """Pure snapshot builder for tests and gather_status."""
    identity = configured_observability_identity(
        config_ip=config_ip,
        qnetd_host=parse_qnetd_host(corosync_conf),
    )
    status = classify_observability(identity=identity, reachable=reachable)
    return observability_public(
        identity=identity,
        hostname=hostname,
        status=status,
    )
