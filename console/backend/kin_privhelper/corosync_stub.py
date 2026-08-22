"""Detect the Debian/Ubuntu corosync package-default stub config.

Installing corosync.deb enables and starts corosync with a generic example
file (cluster_name debian, loopback-only node, often no node name). That is
not a real cluster. A genuine membership (LAN IPs, extra nodes, a name other
than debian) must not match.

Canonical copy: imported by orchestration pre-flight and by the Ansible
filter plugin at ansible/roles/cluster_setup/filter_plugins/corosync_stub.py.
"""

from __future__ import annotations

import re
from typing import Any

_CLUSTER_NAME_RE = re.compile(r"^cluster_name:\s*(\S+)", re.IGNORECASE)
_RING_ADDR_RE = re.compile(r"^ring\d+_addr:\s*(\S+)", re.IGNORECASE)
_RESOURCE_INSTANCES_RE = re.compile(
    r"(?im)^\s*\*?\s*(\d+)\s+resource instances configured\s*$"
)
_PCS_CLUSTER_NAME_RE = re.compile(r"(?im)^\s*Cluster name:\s*(\S+)\s*$")


def _active_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if stripped:
            lines.append(stripped)
    return lines


def _is_loopback_addr(addr: str) -> bool:
    value = addr.strip().lower().strip("[]").rstrip(";")
    if value in {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}:
        return True
    parts = value.split(".")
    if len(parts) == 4 and parts[0] == "127":
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False
    return False


def is_debian_corosync_stub(text: Any) -> bool:
    """True only for the untouched Debian/Ubuntu package example config."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str) or not text.strip():
        return False

    cluster_names: list[str] = []
    ring_addrs: list[str] = []
    for line in _active_lines(text):
        name_match = _CLUSTER_NAME_RE.match(line)
        if name_match:
            cluster_names.append(name_match.group(1).strip().strip("\"'"))
            continue
        ring_match = _RING_ADDR_RE.match(line)
        if ring_match:
            ring_addrs.append(ring_match.group(1).strip().strip("\"'"))

    if cluster_names != ["debian"]:
        return False
    if any(not _is_loopback_addr(addr) for addr in ring_addrs):
        return False
    if len(ring_addrs) > 1:
        return False
    return True


def parse_corosync_cluster_name(text: Any) -> str | None:
    """Return the sole active cluster_name from corosync.conf, or None."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str) or not text.strip():
        return None
    names: list[str] = []
    for line in _active_lines(text):
        name_match = _CLUSTER_NAME_RE.match(line)
        if name_match:
            names.append(name_match.group(1).strip().strip("\"'"))
    if len(names) != 1:
        return None
    return names[0]


def parse_resource_instance_count(status_text: str) -> int | None:
    """Return N from 'N resource instances configured', or None if absent."""
    if not status_text:
        return None
    match = _RESOURCE_INSTANCES_RE.search(status_text)
    if not match:
        return None
    return int(match.group(1))


def parse_pcs_cluster_name(status_text: str) -> str | None:
    if not status_text:
        return None
    match = _PCS_CLUSTER_NAME_RE.search(status_text)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def is_harmless_package_stub_cluster(
    conf: str,
    *,
    status_text: str = "",
    live_nodes: list[str] | None = None,
    cib_text: str | None = None,
) -> bool:
    """True when a live pcs/crm view is still the package-default stub.

    Conf must match is_debian_corosync_stub. Any parsed resource instance
    count above zero, a pcs cluster name other than debian, or more than
    one live node means this is not safe to treat as throwaway. When
    cib_text is provided and non-empty, it must also parse as a
    throwaway package-first-start CIB (see
    pacemaker_cib.is_throwaway_package_cib): no resources or constraints,
    and cluster-name is not kin-mail. First-start nodes/status history
    from the debian loopback cluster are allowed.
    """
    if not is_debian_corosync_stub(conf):
        return False
    nres = parse_resource_instance_count(status_text)
    if nres is not None and nres > 0:
        return False
    cname = parse_pcs_cluster_name(status_text)
    if cname is not None and cname.lower() != "debian":
        return False
    if live_nodes is not None and len(live_nodes) > 1:
        return False
    if cib_text is not None and str(cib_text).strip():
        from .pacemaker_cib import is_throwaway_package_cib

        if not is_throwaway_package_cib(cib_text):
            return False
    return True
