"""Maintenance-mode enter/exit: gated pcs node standby/unstandby.

Target names are validated against the live Pacemaker nodelist and passed as
argv (never interpolated into a shell). Dual-primary assertion stays the
cluster script, not UI state.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO
from xml.etree import ElementTree

from . import protocol as proto
from .pcs_properties import parse_stonith_enabled

NODE_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$")
DRBD_RESOURCE = os.environ.get("KIN_DRBD_RESOURCE", "kin-zimbra")
ASSERT_SCRIPT = Path(
    os.environ.get(
        "KIN_DUAL_PRIMARY_ASSERT",
        "/usr/local/sbin/kin-assert-no-dual-primary.sh",
    )
)
FAILCOUNT_RESOURCES = (
    "kin-drbd-clone",
    "kin-drbd",
    "kin-mail-svc",
    "kin-zimbra",
    "kin-fs",
    "kin-vip",
)
COROSYNC_CONF = Path("/etc/corosync/corosync.conf")
RESYNC_TIMEOUT_SEC = int(os.environ.get("KIN_MAINT_RESYNC_TIMEOUT", "180"))
RESYNC_POLL_SEC = 5
# Controlled Master move (ban -> clear). Zimbra OCF start timeout is 600s.
# Must outlive the resource-level timeouts underneath it, or the console gives
# up and rolls back a move that Pacemaker was still completing. kin-zimbra
# alone has op start timeout=600s, and promote plus the filesystem plus the VIP
# come on top of that.
FAILBACK_TIMEOUT_SEC = int(os.environ.get("KIN_FAILBACK_TIMEOUT", "1200"))
FAILBACK_POLL_SEC = 5
DRBD_CLONE = os.environ.get("KIN_DRBD_CLONE", "kin-drbd-clone")
MAINT_LOCK_PATH = Path(
    os.environ.get("KIN_MAINT_LOCK", "/run/kin-mail/maintenance.lock")
)


async def _capture(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}\n"
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, "", f"timeout after {timeout}s: {' '.join(argv)}\n"
    return (
        int(proc.returncode or 0),
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


def validate_node_name(raw: str) -> str:
    name = (raw or "").strip()
    if not NODE_RE.match(name):
        raise ValueError("target must be a Pacemaker node hostname")
    if ".." in name or name.startswith("-") or "/" in name:
        raise ValueError("target must be a Pacemaker node hostname")
    return name


def _node_tokens(rest: str) -> list[str]:
    found: list[str] = []
    for tok in rest.replace("[", " ").replace("]", " ").split():
        tok = tok.strip(",").strip("'\"")
        if NODE_RE.match(tok) and tok not in found:
            found.append(tok)
    return found


def _pcs_nodes_line_rest(line: str, prefixes: tuple[str, ...]) -> str | None:
    stripped = line.strip().lstrip("*").strip()
    low = stripped.lower()
    for prefix in prefixes:
        if low.startswith(prefix):
            if ":" not in stripped:
                return ""
            return stripped.split(":", 1)[1]
    return None


# pcs 0.10 prints a node that is still draining as
# "Standby with resource(s) running:" not "Standby:". A parser that only
# matches "Standby:" treats mid-flight enter as not-in-maintenance.
_STANDBY_PREFIXES = ("standby with resource", "standby:")
_MEMBER_PREFIXES = (
    "online:",
    "standby with resource",
    "standby:",
    "maintenance:",
)
_OFFLINE_PREFIXES = ("offline:",)


def _has_pcs_nodes_sections(pcs_nodes: str) -> bool:
    """Does this look like `pcs status nodes` output, however empty?"""
    for line in (pcs_nodes or "").splitlines():
        low = line.strip().lower()
        for prefix in (*_MEMBER_PREFIXES, *_OFFLINE_PREFIXES):
            if low.startswith(prefix):
                return True
    return False


def parse_online_nodes(pcs_nodes: str) -> list[str]:
    """Parse `pcs status nodes` member lines (Online, Standby, draining)."""
    found: list[str] = []
    for line in pcs_nodes.splitlines():
        rest = _pcs_nodes_line_rest(line, _MEMBER_PREFIXES)
        if rest is None:
            continue
        found.extend(_node_tokens(rest))
    if not found and not _has_pcs_nodes_sections(pcs_nodes):
        # Last resort for output that is not `pcs status nodes` at all. It must
        # NOT run once real section headers are present: an empty Online list
        # next to a populated Offline one is a legitimate answer, and scanning
        # every token would then report the offline node as online - the exact
        # thing the crm_node fallback was fixed to stop doing.
        for line in pcs_nodes.splitlines():
            parts = line.split()
            for tok in parts:
                tok = tok.strip("'\"")
                if "." in tok and NODE_RE.match(tok):
                    found.append(tok)
    out: list[str] = []
    for name in found:
        if name not in out:
            out.append(name)
    return out


def parse_standby_nodes(pcs_nodes: str) -> list[str]:
    standby: list[str] = []
    for line in pcs_nodes.splitlines():
        rest = _pcs_nodes_line_rest(line, _STANDBY_PREFIXES)
        if rest is None:
            continue
        for tok in _node_tokens(rest):
            if tok not in standby:
                standby.append(tok)
    return standby


def parse_offline_nodes(pcs_nodes: str) -> list[str]:
    """Parse `pcs status nodes` Offline: names (forced remove-host targets)."""
    found: list[str] = []
    for line in pcs_nodes.splitlines():
        rest = _pcs_nodes_line_rest(line, _OFFLINE_PREFIXES)
        if rest is None:
            continue
        for tok in _node_tokens(rest):
            if tok not in found:
                found.append(tok)
    return found


def parse_maintenance_mode(props_text: str) -> bool:
    """Is the whole cluster in Pacemaker maintenance-mode?

    Nothing read this before, and its absence is why the Phase 7 run was
    unreadable: in maintenance-mode Pacemaker stops acting on resources but
    reports no failures, so the console showed Zimbra stopped everywhere, the
    VIP routed nowhere, and fail-count "Clear" - three true statements that
    together look like an unexplained outage rather than a cluster that was
    told to keep its hands off.
    """
    for line in props_text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("maintenance-mode"):
            continue
        _, _, value = stripped.partition(":")
        if not value:
            _, _, value = stripped.partition("=")
        return value.strip().strip('"').lower() in ("true", "on", "yes", "1")
    return False


# `pcs resource ban` writes a location constraint whose id starts with
# "cli-ban-"; `pcs resource clear` removes exactly those. Anything else in the
# constraints section is a deliberate part of the design and must be left be.
_BAN_ID_PREFIX = "cli-ban-"


def parse_ban_constraints(cib_constraints_xml: str) -> list[dict[str, str]]:
    """Temporary bans left in the CIB by an interrupted Move Master.

    A ban is a -INFINITY location rule. If a move is cut short between the ban
    and the clear - the helper is restarted, the node is fenced, the browser
    is closed - it survives in the CIB and quietly forbids promotion for good.
    The cluster then has no Primary and no error to show for it.
    """
    out: list[dict[str, str]] = []
    if not cib_constraints_xml.strip():
        return out
    try:
        root = ElementTree.fromstring(cib_constraints_xml)
    except ElementTree.ParseError:
        return out
    for el in root.iter("rsc_location"):
        ident = el.get("id") or ""
        if not ident.startswith(_BAN_ID_PREFIX):
            continue
        out.append(
            {
                "id": ident,
                "resource": el.get("rsc") or "",
                "node": el.get("node") or "",
                "role": el.get("role") or "",
                "score": el.get("score") or "",
            }
        )
    return out


def parse_promoted_names(crm: str) -> list[str]:
    """All Promoted/Masters hostnames from crm_mon (may be more than one)."""
    for line in crm.splitlines():
        match = re.search(r"(?:Promoted|Masters):\s*\[([^\]]+)\]", line)
        if match:
            return _node_tokens(match.group(1))
    return []


def parse_promoted(crm: str) -> str | None:
    """Stable single Promoted node, or None when missing / dual-Promoted conflict."""
    names = parse_promoted_names(crm)
    if len(names) == 1:
        return names[0]
    return None


def parse_unpromoted(crm: str) -> list[str]:
    for line in crm.splitlines():
        match = re.search(r"(?:Unpromoted|Slaves):\s*\[([^\]]+)\]", line)
        if match:
            return _node_tokens(match.group(1))
    return []


def parse_rejoining_nodes(
    *,
    nodes: list[str],
    promoted_names: list[str],
    unpromoted: list[str],
    standby: list[str],
    offline: list[str],
) -> list[str]:
    """Members the promotable clone has not given a role to yet.

    This is the window after a node is powered back on: Pacemaker counts it as
    a member, but DRBD has not made it Promoted or Unpromoted. Drawn as its own
    state so a reboot reads as neither healthy nor failed, which is the
    distinction vSphere and Nutanix both keep.

    It only applies when some OTHER node already holds a role. Without that
    condition every node with no role is "rejoining", which on a single-node
    appliance means its only node is permanently rejoining, and on a pair being
    built for the first time means both are - neither of which is a node coming
    back from anywhere.
    """
    with_role = {n for n in list(promoted_names) + list(unpromoted) if n}
    if not with_role:
        return []
    busy = with_role | set(standby) | set(offline)
    return [n for n in nodes if n not in busy]


def parse_resource_node(crm: str, resource: str) -> str | None:
    """Which node a Pacemaker primitive (e.g. kin-vip) is Started on.

    Same "Started <node>" convention `crm_mon -1 -r` uses for every
    primitive, as already relied on by zimbra_started_on above.
    """
    for line in crm.splitlines():
        if resource not in line or "Started" not in line:
            continue
        match = re.search(r"Started\s+(\S+)", line)
        if not match:
            continue
        node = match.group(1).strip().strip("'\"")
        if NODE_RE.match(node):
            return node
    return None


def drbd_both_uptodate(status: str) -> bool:
    """True when every disk/peer-disk line in `drbdadm status` is UpToDate."""
    disks = re.findall(r"(?:disk|peer-disk):\s*(\S+)", status, flags=re.I)
    if not disks:
        # Newer drbdadm: "disk UpToDate" without colon
        disks = re.findall(r"\bdisk[:\s]+(\S+)", status, flags=re.I)
    if len(disks) < 2:
        return False
    return all(d.lower() == "uptodate" for d in disks)


# Replication states that mean bytes are actually moving between the nodes.
# SyncSource/SyncTarget are healthy: that is a resync in progress.
_DRBD_LIVE_REPLICATION = {
    "established",
    "syncsource",
    "synctarget",
    "pausedsyncs",
    "pausedsynct",
    "verifys",
    "verifyt",
    "wfbitmaps",
    "wfbitmapt",
    "wfsyncuuid",
    "ahead",
    "behind",
}
_DRBD_LIVE_CONNECTION = {"connected", "established"}


def drbd_link_state(status: str) -> str:
    """Is the replication link up? Returns "", "connected", "syncing" or "down".

    THE reason this exists: after a partition heals badly, DRBD can sit
    StandAlone with BOTH sides reporting UpToDate. Every other signal the
    console had went green - `drbd_both_uptodate` is True, there is no resync
    percentage, no resource has failed - while nothing written on the Promoted
    node was reaching the other one. Lose that node and you lose every message
    since the split, and the page said the cluster was healthy the whole time.

    "" means there is no DRBD resource to talk about (a single-node appliance),
    which is not the same as a link that is down and must not be alerted on.
    """
    text = status or ""
    if not re.search(r"\brole:\S+", text, flags=re.I):
        return ""

    conns = [m.lower() for m in re.findall(r"\bconnection:\s*(\S+)", text, flags=re.I)]
    if conns and any(c not in _DRBD_LIVE_CONNECTION for c in conns):
        return "down"

    repls = [m.lower() for m in re.findall(r"\breplication:\s*(\S+)", text, flags=re.I)]
    if repls and any(r not in _DRBD_LIVE_REPLICATION for r in repls):
        return "down"

    syncing = bool(re.search(r"\bdone:\d", text)) or any(
        r.startswith(("sync", "pausedsync", "wfbitmap", "wfsync")) for r in repls
    )
    if syncing:
        return "syncing"
    if repls or re.search(r"\bpeer-disk:\s*\S+", text, flags=re.I):
        return "connected"
    # A resource that names no peer at all is not replicating to one.
    return "down"


def drbd_sync_percent(status: str) -> float | None:
    """Initial/resync progress (0-100) from `drbdadm status`'s `done:NN.NN`
    field, or None when nothing is actively syncing right now.

    A freshly built HA pair's new node starts Inconsistent and climbs to
    UpToDate over the first full resync - console showed a flat "DRBD not
    UpToDate" with no indication this was expected and in progress, so the
    operator had to SSH in and run drbdadm status by hand to find out it was
    just 32% through a normal sync (live 2vm practice run, 25 Aug 2026).
    """
    m = re.search(r"\bdone:(\d+(?:\.\d+)?)", status)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_promoted_ban_flag(ban_help: str) -> str:
    """Which flag `pcs resource ban` uses to mean "the Promoted role".

    pcs renamed this between the two releases this product runs on:
    Ubuntu 22.04 ships pcs 0.10, which only knows --master; pcs 0.11
    (Ubuntu 24.04) renamed it to --promoted and dropped --master. Hardcoding
    --promoted made Move Master fail outright on 22.04 with "option --promoted
    not recognized", leaving the cluster untouched (live Phase 5 QA).

    Prefer --promoted when the local pcs advertises it, fall back to --master,
    and default to --master when the help text cannot be read at all, since
    that is what the currently deployed release ships.
    """
    text = ban_help or ""
    if "--promoted" in text:
        return "--promoted"
    if "--master" in text:
        return "--master"
    return "--master"


async def promoted_ban_flag() -> str:
    """Ask the local pcs which promoted-role flag it accepts."""
    _c, out, err = await _capture(["pcs", "resource", "ban", "--help"], timeout=20)
    return parse_promoted_ban_flag(f"{out}\n{err}")


def flag_not_recognized(text: str) -> bool:
    """True when pcs rejected the flag itself rather than the operation.

    pcs answers an unknown option with "option --promoted not recognized" and
    then dumps its top-level usage, which is what the operator saw.
    """
    low = (text or "").lower()
    return "not recognized" in low or "unrecognized" in low or "no such option" in low


def other_ban_flag(flag: str) -> str:
    return "--master" if flag == "--promoted" else "--promoted"


def parse_quorate(quorum_text: str) -> bool | None:
    """True/False from `pcs quorum status`, None when it could not be read.

    None matters: `pcs quorum status` fails outright when corosync is down, and
    "could not tell" must never be rendered as "quorum is fine".
    """
    if not (quorum_text or "").strip():
        return None
    if re.search(r"Quorate:\s*Yes", quorum_text, flags=re.I):
        return True
    if re.search(r"Quorate:\s*No", quorum_text, flags=re.I):
        return False
    return None


def parse_vote_totals(quorum_text: str) -> tuple[int | None, int | None]:
    """(total_votes, quorum_needed) from the Votequorum information block."""
    total = re.search(r"Total votes:\s*(\d+)", quorum_text or "", flags=re.I)
    needed = re.search(r"Quorum:\s*(\d+)", quorum_text or "", flags=re.I)
    return (
        int(total.group(1)) if total else None,
        int(needed.group(1)) if needed else None,
    )


def parse_no_quorum_policy(text: str) -> str:
    """`pcs property` value of no-quorum-policy, lowercased ('' when absent)."""
    m = re.search(r"no-quorum-policy[=:]\s*(\S+)", text or "", flags=re.I)
    return m.group(1).strip().lower() if m else ""


def quorum_recovery_hint(
    *,
    quorate: bool | None,
    peer_offline: bool,
    observability_reachable: bool,
    no_quorum_policy: str = "",
) -> str:
    """Operator-facing explanation when the cluster has lost quorum.

    The 2-node + qdevice layout carries 3 votes and needs 2. Losing the peer
    AND the Observability witness at the same time leaves the survivor on 1
    vote, so `no-quorum-policy=stop` correctly stops mail even though this node
    is perfectly healthy. That is the right default (it is what prevents
    split-brain), but the console used to show nothing about it at all - no
    cause, no way back - so the operator had no idea why a healthy node had
    stopped serving mail.

    Deliberately returns instructions rather than performing the recovery. With
    the Observability VM down the SBD fencing LUN is down with it, so forcing
    quorum here cannot be fenced: if the peer is actually alive behind a
    network partition, both nodes would go Primary and DRBD would diverge.
    That call needs a human who can confirm the peer is genuinely dead.
    """
    if quorate is not False:
        return ""
    # With no-quorum-policy=ignore the appliance deliberately keeps serving on
    # the last node standing, so this is informational, not an outage.
    if (no_quorum_policy or "").lower() == "ignore":
        if peer_offline and not observability_reachable:
            return (
                "Running alone: the peer mail node and the Observability witness "
                "are both unreachable, so this node holds 1 of 3 votes. Mail "
                "keeps running here on purpose (no-quorum-policy=ignore). "
                "Bring back either the peer or the Observability VM to restore "
                "redundancy - until then there is no failover target and no "
                "SBD fencing."
            )
        return (
            "Running without quorum on purpose (no-quorum-policy=ignore). Mail "
            "keeps serving here, but redundancy is degraded until the missing "
            "cluster members come back."
        )
    if peer_offline and not observability_reachable:
        return (
            "Quorum lost: the peer mail node and the Observability witness are "
            "both unreachable, so this node holds 1 of 3 votes and Pacemaker "
            "has stopped mail here on purpose. Bring back EITHER the peer or "
            "the Observability VM and mail restarts on its own. Only if you "
            "have confirmed the peer is genuinely powered off (not just "
            "unreachable) run `pcs quorum unblock --force` on this node - with "
            "Observability down there is no SBD fencing, so doing that while "
            "the peer is actually alive will split-brain DRBD."
        )
    if peer_offline:
        return (
            "Quorum lost with the peer mail node unreachable. Check the "
            "Observability VM is voting, then bring the peer back."
        )
    if not observability_reachable:
        return (
            "Quorum lost with the Observability witness unreachable. Bring the "
            "Observability VM back, or re-add it from the Cluster page."
        )
    return (
        "Quorum lost while both peers look reachable - check corosync ring "
        "connectivity between the nodes."
    )


def qdevice_voting(quorum_text: str) -> bool:
    """True if pcs quorum output shows a live Qdevice with at least one vote."""
    if not re.search(r"Quorate:\s*Yes", quorum_text, flags=re.I):
        return False
    if not re.search(r"\bQdevice\b", quorum_text, flags=re.I):
        return False
    for line in quorum_text.splitlines():
        stripped = line.strip()
        if stripped.endswith("Qdevice") and not stripped.lower().startswith("nodeid"):
            nums = [int(x) for x in re.findall(r"\d+", stripped)]
            # Typical: nodeid=0 votes=1  →  [0, 1]
            if len(nums) >= 2 and nums[1] >= 1:
                return True
    return bool(re.search(r"Flags:.*\bQdevice\b", quorum_text, flags=re.I))


def parse_corosync_ring_addrs(conf: str) -> dict[str, str]:
    """Map node name to ring0_addr from corosync.conf."""
    mapping: dict[str, str] = {}
    current_addr = ""
    current_name = ""
    for raw in conf.splitlines():
        line = raw.strip()
        if line.startswith("ring0_addr:"):
            current_addr = line.split(":", 1)[1].strip()
        elif line.startswith("name:"):
            current_name = line.split(":", 1)[1].strip()
        if current_addr and current_name:
            mapping[current_name] = current_addr
            current_addr = ""
            current_name = ""
    return mapping


def node_ips_for_status(
    addrs: dict[str, str],
    *,
    local_host: str = "",
    local_ip: str = "",
) -> dict[str, str]:
    """Public hostname to IPv4 map for CLUSTER_STATUS_JSON.

    Corosync ring0_addr is the source of truth when Pacemaker is configured.
    A 1vm host often has no corosync.conf yet, so SERVER_IP fills that hole
    for the local hostname only (never overwrites a corosync mapping).
    """
    out: dict[str, str] = {}
    for name, ip in (addrs or {}).items():
        n = str(name or "").strip()
        a = str(ip or "").strip()
        if n and a:
            out[n] = a
    host = str(local_host or "").strip()
    addr = str(local_ip or "").strip()
    if host and addr and host not in out:
        out[host] = addr
    return out


def parse_failcount_value(text: str) -> int | None:
    """Parsed fail-count, or None when the query text is not recognizable.

    Returning 0 for unknown text previously hid probe failures as healthy.
    """
    m = re.search(r"value=(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"fail-count[^=]*=\s*(\d+)", text, flags=re.I)
    if m:
        return int(m.group(1))
    if "No failcounts" in text or "value=0" in text:
        return 0
    # pcs prints "INFINITY" sometimes
    if "INFINITY" in text.upper():
        return 999
    return None


def this_hostname() -> str:
    for cmd in (["hostname", "-f"], ["hostname"]):
        try:
            out = subprocess.check_output(cmd, text=True, timeout=3).strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if out and out != "localhost":
            return out
    fq = socket.getfqdn()
    if fq and fq != "localhost":
        return fq
    return socket.gethostname()


def hosts_match(left: str, right: str) -> bool:
    """True when hostnames are the same FQDN or share the same short name."""
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return a.split(".")[0] == b.split(".")[0]


def crm_node_list_as_pcs_nodes(crm_node_l: str) -> str:
    """Render `crm_node -l` in the shape `pcs status nodes` produces.

    `pcs status nodes` needs a live CIB connection, so it fails on a node whose
    Pacemaker has not come up yet - which is exactly the window an operator
    watches after powering a node back on. The fallback used to hand the raw
    `crm_node -l` text to the same parsers, and none of their prefixes matched
    it, so every name fell through to a loose token scan that reported BOTH
    nodes online. A node that Pacemaker had already marked `lost` was drawn as
    a healthy cluster member for the whole reboot.

    Lines are `<id> <name> <state>`, where only `member` means joined.
    """
    online: list[str] = []
    offline: list[str] = []
    for raw in (crm_node_l or "").splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        name = parts[1].strip("'\"")
        if "." not in name or not NODE_RE.match(name):
            continue
        state = parts[2].strip().lower() if len(parts) > 2 else ""
        (online if state == "member" else offline).append(name)
    if not online and not offline:
        return ""
    return (
        "Pacemaker Nodes:\n"
        f" Online: {' '.join(online)}\n"
        " Standby:\n"
        " Standby with resource(s) running:\n"
        " Maintenance:\n"
        f" Offline: {' '.join(offline)}\n"
    )


async def _pcs_nodes_text() -> str:
    code, out, err = await _capture(["pcs", "status", "nodes"])
    if code != 0 or not out.strip():
        _c2, out2, _e2 = await _capture(["crm_node", "-l"])
        shaped = crm_node_list_as_pcs_nodes(out2)
        if shaped:
            return shaped
        return out + "\n" + err + "\n" + out2
    return out


async def cluster_node_names() -> list[str]:
    return parse_online_nodes(await _pcs_nodes_text())


async def resolve_target(raw: str) -> str:
    name = validate_node_name(raw)
    nodes = await cluster_node_names()
    if name not in nodes:
        raise ValueError(f"target {name!r} is not in the Pacemaker nodelist ({', '.join(nodes) or 'empty'})")
    return name


async def _emit(text: str, *, err: bool = False) -> dict[str, Any]:
    if err:
        return proto.event_stderr(text if text.endswith("\n") else text + "\n")
    return proto.event_stdout(text if text.endswith("\n") else text + "\n")


async def _https_code(url: str) -> tuple[str, int]:
    code, out, err = await _capture(
        ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8", url]
    )
    body = (out or "").strip() or "000"
    try:
        http = int(body)
    except ValueError:
        http = 0
    return body, code


def https_probe_ok(http_code: str | int) -> bool:
    """VIP / webmail is healthy enough for a Master move settle check.

    Exact 200 is too strict: Zimbra/nginx often answers 301/302 before the
    mailbox UI, and some probes return other 2xx. Treat any 2xx/3xx as OK.
    """
    try:
        code = int(str(http_code).strip() or "0")
    except ValueError:
        return False
    return 200 <= code < 400


async def wait_drbd_uptodate(*, timeout_sec: int | None = None) -> tuple[bool, list[str]]:
    """Poll until both DRBD replicas are UpToDate (or timeout)."""
    limit = FAILBACK_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    logs: list[str] = []
    started = asyncio.get_event_loop().time()
    deadline = started + limit
    while True:
        st = await gather_status()
        pct = st.get("drbd_sync_percent")
        uptodate = bool(st["drbd_uptodate"])
        # Elapsed, not just a repeated False. A line that never changes reads
        # as a hang; the same line with a clock on it reads as progress.
        waited = int(asyncio.get_event_loop().time() - started)
        if pct is None:
            logs.append(f"drbd_uptodate={uptodate} waited={waited}s of {limit}s")
        else:
            logs.append(
                f"drbd_uptodate={uptodate} sync={pct:.1f}% "
                f"waited={waited}s of {limit}s"
            )
        if uptodate:
            logs.append("DRBD both replicas UpToDate")
            return True, logs
        if asyncio.get_event_loop().time() >= deadline:
            logs.append(f"TIMEOUT waiting for DRBD UpToDate after {limit}s")
            return False, logs
        await asyncio.sleep(FAILBACK_POLL_SEC)


def _stack_on_target(st: dict[str, Any], target: str) -> bool:
    """True when Promoted + VIP + Zimbra are all on target (no dual Promoted)."""
    if bool(st.get("promoted_conflict")):
        return False
    promoted = st.get("promoted")
    vip_node = st.get("vip_node")
    zimbra_node = st.get("zimbra_node")
    return (
        bool(promoted)
        and hosts_match(str(promoted), target)
        and bool(vip_node)
        and hosts_match(str(vip_node), target)
        and bool(zimbra_node)
        and hosts_match(str(zimbra_node), target)
    )


async def wait_failback_settled(
    target: str,
    *,
    timeout_sec: int | None = None,
) -> tuple[bool, list[str]]:
    """Wait until Promoted + VIP (+ Zimbra) are on target after a ban.

    HTTPS on the VIP is preferred but not a hard fail once the Pacemaker stack
    has been on the target: OCF already ran zmcontrol, and a console-host curl
    to the VIP can fail for lab firewall/routing reasons even when mail works.
    """
    limit = FAILBACK_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    logs: list[str] = []
    deadline = asyncio.get_event_loop().time() + limit
    vip_ip = ""
    stack_ok_streak = 0
    while True:
        st = await gather_status()
        vip_ip = str(st.get("vip_ip") or vip_ip or "")
        promoted = st.get("promoted")
        vip_node = st.get("vip_node")
        zimbra_node = st.get("zimbra_node")
        dual = bool(st.get("promoted_conflict"))
        waited = int(asyncio.get_event_loop().time() - (deadline - limit))
        last = (
            f"promoted={promoted} vip_node={vip_node} zimbra_node={zimbra_node} "
            f"dual_promoted={dual} waited={waited}s of {limit}s"
        )
        logs.append(last)
        stack_ok = _stack_on_target(st, target)
        https_ok = True
        https_body = ""
        if vip_ip and stack_ok:
            https_body, _c = await _https_code(f"https://{vip_ip}/")
            https_ok = https_probe_ok(https_body)
            logs.append(f"https://{vip_ip}/ -> HTTP {https_body}")
        if stack_ok:
            stack_ok_streak += 1
        else:
            stack_ok_streak = 0

        if stack_ok and https_ok and not dual:
            dual_ok = True
            if ASSERT_SCRIPT.is_file():
                c, out, err = await _capture([str(ASSERT_SCRIPT)])
                dual_txt = (out + err).strip()
                dual_ok = c == 0 and "NO_DUAL_PRIMARY_OK" in dual_txt
                logs.append(dual_txt or f"dual-primary assert exit {c}")
            if dual_ok:
                logs.append(f"Master settled on {target}")
                return True, logs

        # Stack has been stable on target across polls; HTTPS still unhappy.
        # Accept settle so we do not leave a temporary ban forever.
        if stack_ok and stack_ok_streak >= 3 and not dual:
            dual_ok = True
            if ASSERT_SCRIPT.is_file():
                c, out, err = await _capture([str(ASSERT_SCRIPT)])
                dual_txt = (out + err).strip()
                dual_ok = c == 0 and "NO_DUAL_PRIMARY_OK" in dual_txt
                logs.append(dual_txt or f"dual-primary assert exit {c}")
            if dual_ok:
                logs.append(
                    f"Master settled on {target} (stack OK; VIP HTTPS probe "
                    f"was HTTP {https_body or 'n/a'} - accepting without hard HTTPS fail)"
                )
                return True, logs

        if asyncio.get_event_loop().time() >= deadline:
            # Last chance: if the stack is already on target, do not report a
            # hard timeout that leaves operators with a stuck ban.
            if stack_ok and not dual:
                logs.append(
                    f"TIMEOUT soft-accept: stack already on {target} after {limit}s "
                    f"({last}; HTTPS {https_body or 'n/a'})"
                )
                return True, logs
            logs.append(f"TIMEOUT waiting for Master on {target} after {limit}s: {last}")
            return False, logs
        await asyncio.sleep(FAILBACK_POLL_SEC)


def zimbra_started_on(crm: str, node: str) -> bool:
    for line in crm.splitlines():
        if "kin-zimbra" in line and "Started" in line and node in line:
            return True
    return False


async def _zmcontrol_on(node: str, addr: str, crm: str) -> tuple[bool, str]:
    """Local zmcontrol, or crm_mon for the peer - no inter-node SSH.

    Pacemaker's kin-zimbra OCF monitor already runs ``zmcontrol status``.
    Preflight also has an independent ``peer_https`` check. A dedicated SSH
    key between mail nodes is intentionally not added: even a forced-command
    key is permanent lateral-movement surface for a dump the monitor already
    produces.
    """
    me = this_hostname()
    short = me.split(".")[0]
    node_short = node.split(".")[0]
    if node == me or node_short == short or node_short == socket.gethostname():
        c, out, err = await _capture(["su", "-", "zimbra", "-c", "zmcontrol status"], timeout=45)
        text = (out + err).strip()
        ok = c == 0 and "Stopped" not in text
        return ok, text or f"exit {c}"
    started = zimbra_started_on(crm, node)
    if started:
        where = addr or node
        return True, (
            f"kin-zimbra Started on {node} ({where}; crm_mon; OCF monitor is zmcontrol status). "
            "Remote SSH zmcontrol is intentionally not configured"
        )
    return False, f"cannot confirm zmcontrol on {node}: kin-zimbra is not Started there"


def _failcount_query_absent(text: str) -> bool:
    low = text.lower()
    return any(
        needle in low
        for needle in (
            "not found",
            "no such",
            "unknown resource",
            "does not exist",
            "no failcount",
            "no fail-count",
        )
    )


async def _failcounts(
    nodes: list[str],
) -> tuple[bool, list[str], list[str], list[str]]:
    """(all_zero, human lines, unreadable entries).

    A query we could not READ is not evidence of a failure COUNT. Treating the
    two the same is what made a brand-new cluster report "fail-count nonzero"
    with nothing wrong, and since preflight gates on this, it blocked Enter
    Maintenance and Remove Host on a healthy pair (live Phase 5 QA).

    FAILCOUNT_RESOURCES lists the clone and the group as well as the
    primitives, and crm_failcount does not answer for every one of those on
    every Pacemaker build - so unreadable is the normal case, not an anomaly.
    Unreadable entries are reported separately and do not fail the check; only
    a value that was genuinely read and is non-zero does.
    """
    lines: list[str] = []
    unreadable: list[str] = []
    nonzero: list[str] = []
    ok = True
    for node in nodes:
        for res in FAILCOUNT_RESOURCES:
            c, out, err = await _capture(
                ["crm_failcount", "--query", "-r", res, "-N", node]
            )
            text = (out + err).strip()
            if c != 0 and _failcount_query_absent(text):
                continue
            if c != 0:
                unreadable.append(f"{res}@{node}: exit {c}")
                lines.append(f"  {res}@{node} fail-count=unreadable (exit {c})")
                continue
            val = parse_failcount_value(text)
            if val is None:
                unreadable.append(f"{res}@{node}: {text[:60]}")
                lines.append(f"  {res}@{node} fail-count=unreadable")
                continue
            lines.append(f"  {res}@{node} fail-count={val}")
            if val != 0:
                ok = False
                nonzero.append(f"{res} on {node} (fail-count={val})")
    return ok, lines, unreadable, nonzero


def try_lock_maintenance(path: Path | None = None) -> IO[str] | None:
    """Non-blocking flock so a second enter/exit cannot overlap the first."""
    lock_path = path or MAINT_LOCK_PATH
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return fh


def release_maintenance_lock(fh: IO[str] | None) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def _config_value(key: str) -> str:
    from .apply_config import parse_config

    path = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
    if not path.is_file():
        return ""
    try:
        values = parse_config(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return str(values.get(key) or "").strip()


def _config_observability_ip() -> str:
    return _config_value("OBSERVABILITY_VM_IP")


def _config_server_ip() -> str:
    return _config_value("SERVER_IP")


def _config_vip_ip() -> str:
    return _config_value("CLUSTER_VIP_IP")


def _running_job_snapshot() -> dict[str, Any]:
    """Ask the daemon what holds the single-flight slot.

    Imported here rather than at module scope: daemon imports commands imports
    this module, so a top-level import would be a cycle.
    """
    try:
        from .daemon import running_job

        return dict(running_job())
    except Exception:  # noqa: BLE001 - status must never fail on this
        return {"running": False, "command": "", "seconds": 0}


def _config_peer_name() -> str:
    """The peer this node is configured to pair with, from /etc/kin-mail/config.

    Needed for the half-removed state. Remove Host records last-removed-peer
    while DEMOTING the topology, so a run that stops before that leaves no
    record at all, and the survivor is a lone node still calling itself a pair
    with nothing on the page naming the node that should be there. PEER_HOST_NAME
    survives precisely because the demote is what clears it.
    """
    return _config_value("PEER_HOST_NAME")


async def _observability_snapshot(corosync_txt: str) -> dict[str, Any]:
    from .observability_status import (
        configured_observability_identity,
        probe_observability_reachable,
        snapshot_from_texts,
    )
    from .qdevice_status import parse_qnetd_host

    identity_cfg = _config_observability_ip()
    identity = configured_observability_identity(
        config_ip=identity_cfg,
        qnetd_host=parse_qnetd_host(corosync_txt),
    )
    reachable = False
    if identity:
        reachable = await probe_observability_reachable(identity)
    return snapshot_from_texts(
        config_ip=identity_cfg,
        corosync_conf=corosync_txt,
        reachable=reachable,
    )


_OPS_HEADER_RE = re.compile(
    r"^=== (?P<op>enter|exit) (?P<target>\S+) - (?P<stamp>.+?) ===\s*$"
)


def parse_maintenance_provenance(
    ops_log: str, standby: list[str]
) -> tuple[str, str]:
    """When was the standby node put there, and was it this console?

    Returns (since, source). `source` is "console" when the transcript records
    an enter that was never followed by an exit, and "unknown" when a node is
    in standby with nothing to account for it.

    The distinction is the whole point. Pacemaker standby survives a reboot, so
    a node can come back up still in it, and an operator watching that boot has
    no way to tell a deliberate maintenance from a leftover one - which is
    exactly the question asked after the 29 Aug 2026 power test. "Entered from
    this console at 14:02" and "in standby with no record of how" call for
    completely different responses.
    """
    if not standby:
        return "", ""
    latest: dict[str, tuple[str, str]] = {}
    for line in (ops_log or "").splitlines():
        m = _OPS_HEADER_RE.match(line.strip())
        if not m:
            continue
        latest[m.group("target")] = (m.group("op"), m.group("stamp").strip())
    for node in standby:
        op, stamp = latest.get(node, ("", ""))
        if op == "enter":
            return stamp, "console"
    return "", "unknown"


def _maintenance_provenance(standby: list[str]) -> tuple[str, str]:
    if not standby:
        return "", ""
    try:
        text = CLUSTER_OPS_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return parse_maintenance_provenance(text, standby)


async def gather_status() -> dict[str, Any]:
    # A multi deployment is not a cluster, so none of what follows applies to
    # it. pcs, crm_mon and drbdadm are not installed on those machines; every
    # call below would fail and the page would then describe three machines as
    # an unhealthy single one - which is exactly what it did.
    #
    # Branching here rather than filtering afterwards also means a split does
    # not pay for ten subprocess round trips on every refresh.
    from .deploy_state import saved_wizard_topology as _saved_topology

    if _saved_topology() == "split":
        from . import multi_deployment

        deployment = await multi_deployment.gather()
        return {
            "local_host": this_hostname(),
            "topology": "split",
            "deployment": deployment,
            # Named the same as the replicated pair's field so the page has one
            # shape to render, but derived from the links rather than from
            # Pacemaker. Empty lists, not absent keys: a missing key reads as
            # "unknown" in the UI and draws placeholders.
            "nodes": [
                n["name"] for n in deployment["nodes"] if n.get("present") and n.get("ok")
            ],
            "offline": [
                n["name"]
                for n in deployment["nodes"]
                if n.get("present") and not n.get("ok")
            ],
            "standby": [],
            "stale_peers": [],
            "rejoining": [],
        }

    nodes_text = await _pcs_nodes_text()
    nodes = parse_online_nodes(nodes_text)
    standby = parse_standby_nodes(nodes_text)
    offline = parse_offline_nodes(nodes_text)
    _c, crm, _e = await _capture(["crm_mon", "-1", "-r"])
    promoted_names = parse_promoted_names(crm)
    promoted_conflict = len(promoted_names) > 1
    promoted = promoted_names[0] if len(promoted_names) == 1 else None
    unpromoted = parse_unpromoted(crm)
    zimbra_node = parse_resource_node(crm, "kin-zimbra")
    vip_node = parse_resource_node(crm, "kin-vip")
    _c2, drbd, _e2 = await _capture(["drbdadm", "status", DRBD_RESOURCE])
    _c3, quorum, _e3 = await _capture(["pcs", "quorum", "status"])
    corosync_txt = ""
    if COROSYNC_CONF.is_file():
        try:
            corosync_txt = COROSYNC_CONF.read_text(encoding="utf-8", errors="replace")
        except OSError:
            corosync_txt = ""
    addrs = parse_corosync_ring_addrs(corosync_txt) if corosync_txt else {}
    live = set(nodes) | set(offline)
    stale_peers = [n for n in addrs if n not in live]
    fc_ok, fc_lines, fc_unreadable, fc_nonzero = await _failcounts(nodes)
    observability = await _observability_snapshot(corosync_txt)
    quorate = parse_quorate(quorum)
    votes_total, votes_needed = parse_vote_totals(quorum)
    _cp, props_text, _ep = await _capture(["pcs", "property"])
    no_quorum_policy = parse_no_quorum_policy(props_text)
    maintenance_mode = parse_maintenance_mode(props_text)
    # Nothing read this before. Remove Observability disables fencing as its
    # first action, so a disarm that failed part-way - or a troubleshooting
    # `pcs property set stonith-enabled=false` nobody undid - left a two-node
    # DRBD cluster with no fencing and no indication of it anywhere.
    fencing_enabled = parse_stonith_enabled(props_text)
    # cibadmin rather than `pcs constraint`, whose subcommand spelling moved
    # between pcs 0.10 and 0.11. The CIB scope is the same on both.
    _cc, cib_constraints, _ec = await _capture(
        ["cibadmin", "--query", "--scope", "constraints"]
    )
    bans = parse_ban_constraints(cib_constraints if _cc == 0 else "")
    from .corosync_stub import is_harmless_package_stub_cluster
    from .deploy_state import saved_wizard_topology

    package_stub = bool(
        corosync_txt
        and is_harmless_package_stub_cluster(
            corosync_txt,
            status_text=crm,
            live_nodes=nodes,
        )
    )

    # Cached: an expiry date does not change between 30-second polls.
    try:
        from .tls_status import cached_certificate

        _cfg = {}
        try:
            from .apply_config import CONF_FILE, parse_config

            if CONF_FILE.is_file():
                _cfg = parse_config(CONF_FILE.read_text(encoding="utf-8"))
        except OSError:
            _cfg = {}
        _tls = cached_certificate(
            str(_cfg.get("MAIL_HOST") or ""), str(_cfg.get("TLS_METHOD") or "")
        )
    except Exception:  # noqa: BLE001 - never let a certificate read break status
        _tls = {}

    rejoining = parse_rejoining_nodes(
        nodes=nodes,
        promoted_names=promoted_names,
        unpromoted=unpromoted,
        standby=standby,
        offline=offline,
    )
    maint_since, maint_source = _maintenance_provenance(standby)

    return {
        "local_host": this_hostname(),
        "topology": saved_wizard_topology(),
        "nodes": nodes,
        "standby": standby,
        "rejoining": rejoining,
        "maintenance_since": maint_since,
        "maintenance_source": maint_source,
        "offline": offline,
        "stale_peers": stale_peers,
        "promoted": promoted,
        "promoted_names": promoted_names,
        "promoted_conflict": promoted_conflict,
        "unpromoted": unpromoted,
        "zimbra_node": zimbra_node,
        "drbd_uptodate": drbd_both_uptodate(drbd),
        "drbd_sync_percent": drbd_sync_percent(drbd),
        # Both disks can read UpToDate while the link between them is dead.
        # Without this the console called that healthy.
        #
        # Only meaningful when a second node is supposed to exist. After Remove
        # Host there is nothing to replicate to, and reporting "replication is
        # down" on a healthy single-node appliance is a red banner that can
        # never be cleared (seen live, 31 Aug 2026). A peer that is merely
        # switched off still counts: it appears in offline or stale_peers.
        "tls_days_left": _tls.get("days_left"),
        "tls_method": _tls.get("method") or "",
        "tls_source": _tls.get("source") or "",
        "drbd_link": (
            drbd_link_state(drbd)
            if len(set(nodes) | set(offline) | set(stale_peers)) > 1
            else ""
        ),
        "qdevice_ok": qdevice_voting(quorum),
        "quorate": quorate,
        "votes_total": votes_total,
        "votes_needed": votes_needed,
        "no_quorum_policy": no_quorum_policy,
        "fencing_enabled": fencing_enabled,
        "maintenance_mode": maintenance_mode,
        "bans": bans,
        "quorum_hint": quorum_recovery_hint(
            quorate=quorate,
            peer_offline=bool(offline) or bool(stale_peers),
            observability_reachable=bool(observability.get("reachable")),
            no_quorum_policy=no_quorum_policy,
        ),
        "observability": observability,
        "failcount_ok": fc_ok,
        "failcount_lines": fc_lines,
        "failcount_unreadable": fc_unreadable,
        "failcount_nonzero": fc_nonzero,
        "vip_ip": _config_vip_ip(),
        "vip_node": vip_node,
        "package_stub": package_stub,
        "addrs": addrs,
        "raw": {
            "pcs_nodes": nodes_text,
            "crm": crm,
            "drbd": drbd,
            "quorum": quorum,
        },
    }


async def run_preflight(target: str) -> tuple[bool, list[str], dict[str, Any]]:
    """Return (all_ok, log_lines, checks dict)."""
    st = await gather_status()
    logs: list[str] = []
    checks: dict[str, Any] = {}

    def rec(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": ok, "detail": detail}
        mark = "OK" if ok else "FAIL"
        logs.append(f"[{mark}] {name}: {detail}")

    rec(
        "pacemaker_nodes",
        bool(st["nodes"]),
        f"nodes={st['nodes'] or 'none'} standby={st.get('standby') or []}",
    )
    rec("drbd_uptodate", bool(st["drbd_uptodate"]), "both replicas UpToDate" if st["drbd_uptodate"] else "DRBD is not UpToDate/UpToDate")
    # Moving the Master while the link is down hands service to a copy that has
    # been diverging, and silently discards everything written since the split.
    # UpToDate on both sides does not mean they agree.
    _link = str(st.get("drbd_link") or "")
    rec(
        "drbd_replicating",
        _link in ("connected", "syncing", ""),
        {
            "connected": "replication link is up",
            "syncing": "replication link is up (resync in progress)",
            "": "no DRBD resource on this host",
        }.get(
            _link,
            "the replication link is DOWN. Both nodes may report UpToDate and "
            "still hold different data. Reconnect DRBD before moving anything: "
            "drbdadm status " + DRBD_RESOURCE,
        ),
    )
    rec("qdevice_voting", bool(st["qdevice_ok"]), "qdevice reachable and voting" if st["qdevice_ok"] else "qdevice missing, offline, or not voting")
    # Detail names any entry we could not read, so an operator seeing this
    # pass on a cluster with unreadable entries knows why, instead of the check
    # silently failing on them.
    _fc_unreadable = st.get("failcount_unreadable") or []
    # Detail names ONLY the offending resources. Joining all twelve lines
    # produced a wall of "fail-count=0" that the UI then truncated, so the
    # operator could see the check had failed but not which resource caused it
    # (live Phase 5 QA).
    _fc_nonzero = st.get("failcount_nonzero") or []
    rec(
        "failcount_zero",
        bool(st["failcount_ok"]),
        (
            "; ".join(_fc_nonzero)
            + ". Clear it from the Cluster page (Cluster Healthy menu -> Clear"
            " stale fail-counts) once you have checked why it failed."
        )
        if _fc_nonzero
        else ("all resources report fail-count=0" if st["failcount_lines"] else "no fail-count records"),
    )
    if _fc_unreadable:
        logs.append(
            "[INFO] fail-count could not be read for "
            f"{len(_fc_unreadable)} entr{'y' if len(_fc_unreadable) == 1 else 'ies'} "
            f"({'; '.join(_fc_unreadable[:4])}). Not treated as a failure: "
            "crm_failcount does not answer for clones and groups on every "
            "Pacemaker build."
        )

    # Peer that must keep serving (currently Promoted), plus the other node Online.
    promoted = st["promoted"]
    peer = None
    for n in st["nodes"]:
        if n != target:
            peer = n
            break
    rec("peer_online", peer is not None, f"peer={peer}" if peer else "no peer in nodelist")

    if target in (st.get("standby") or []):
        rec("not_already_standby", False, f"{target} is already in standby")
    else:
        rec("not_already_standby", True, f"{target} is not in standby")

    serving = promoted or peer
    https_ok = False
    zm_ok = False
    https_detail = "skipped (no serving node)"
    zm_detail = "skipped"
    if serving:
        addr = (st.get("addrs") or {}).get(serving) or serving
        body, _c = await _https_code(f"https://{addr}/")
        https_ok = body == "200"
        https_detail = f"https://{addr}/ → HTTP {body}"
        zm_ok, zm_detail = await _zmcontrol_on(
            serving, addr, (st.get("raw") or {}).get("crm", "")
        )
        zm_detail = zm_detail[:1500]
    rec("peer_https", https_ok, https_detail)
    rec("peer_zmcontrol", zm_ok, zm_detail.splitlines()[0] if zm_detail else "")

    # Two states that stop the cluster acting without reporting any failure.
    # Both were invisible here before, and both produce the same picture as a
    # healthy-but-idle cluster: no errors, nothing running.
    bans = st.get("bans") or []
    if bans:
        rec(
            "no_stale_ban",
            False,
            "a temporary ban from an earlier Move Master is still in the CIB: "
            + "; ".join(
                f"{b.get('resource')} not allowed on {b.get('node')}"
                + (f" as {b.get('role')}" if b.get("role") else "")
                for b in bans
            )
            + ". Clear it from the Cluster page before moving the Master.",
        )
    else:
        rec("no_stale_ban", True, "no leftover Move Master bans in the CIB")

    if st.get("maintenance_mode"):
        rec(
            "not_maintenance_mode",
            False,
            "the cluster is in Pacemaker maintenance-mode, so it will not start "
            "or move anything. Leave maintenance-mode first.",
        )
    else:
        rec("not_maintenance_mode", True, "cluster is not in maintenance-mode")

    if ASSERT_SCRIPT.is_file():
        c, out, err = await _capture([str(ASSERT_SCRIPT)])
        dual_ok = c == 0 and "NO_DUAL_PRIMARY_OK" in (out + err)
        rec("no_dual_primary", dual_ok, (out + err).strip() or f"exit {c}")
    else:
        rec("no_dual_primary", False, f"assert script missing: {ASSERT_SCRIPT}")

    all_ok = all(v["ok"] for v in checks.values())
    checks["_all_ok"] = all_ok
    checks["_target"] = target
    checks["_promoted"] = promoted
    checks["_standby"] = st.get("standby") or []
    return all_ok, logs, checks


async def wait_exit_healthy(target: str) -> tuple[bool, list[str]]:
    logs: list[str] = []
    deadline = asyncio.get_event_loop().time() + RESYNC_TIMEOUT_SEC
    last = ""
    while True:
        st = await gather_status()
        promoted = st["promoted"]
        secondary_ok = target != promoted
        uptodate = bool(st["drbd_uptodate"])
        fc_ok = bool(st["failcount_ok"])
        in_standby = target in st["standby"]
        dual_ok = False
        dual_txt = ""
        if ASSERT_SCRIPT.is_file():
            c, out, err = await _capture([str(ASSERT_SCRIPT)])
            dual_txt = (out + err).strip()
            dual_ok = c == 0 and "NO_DUAL_PRIMARY_OK" in dual_txt
        last = (
            f"standby={in_standby} promoted={promoted} target_secondary={secondary_ok} "
            f"drbd_uptodate={uptodate} failcount_ok={fc_ok} dual_ok={dual_ok}"
        )
        logs.append(last)
        if not in_standby and secondary_ok and uptodate and fc_ok and dual_ok:
            logs.append("NO_DUAL_PRIMARY_OK")
            logs.append("exit verification complete")
            return True, logs
        if asyncio.get_event_loop().time() >= deadline:
            logs.append(f"TIMEOUT after {RESYNC_TIMEOUT_SEC}s: {last}")
            if dual_txt:
                logs.append(dual_txt)
            return False, logs
        await asyncio.sleep(RESYNC_POLL_SEC)


async def _maintenance_events(args: dict[str, Any] | None = None) -> Any:
    args = args or {}
    op = str(args.get("op") or "status").strip().lower()
    if op not in (
        "status",
        "preflight",
        "enter",
        "exit",
        "cleanup",
        "failback",
        "clearban",
        "opslog",
    ):
        yield proto.event_stderr(
            "op must be status, preflight, enter, exit, cleanup, clearban, "
            "opslog, or failback\n"
        )
        yield proto.event_done(2)
        return

    # Maintenance mode, failback, fail-counts and constraint clearing are
    # Pacemaker. A multi deployment has no Pacemaker: there is nothing to put
    # in standby, nothing to fail back to, and no constraints to clear.
    #
    # Refusing here rather than per-op because every one of them reaches
    # run_preflight or the pair printer, both of which index st["promoted"],
    # st["drbd_uptodate"] and st["qdevice_ok"] directly - so the alternative to
    # one guard is a KeyError traceback in the operator's face for each button
    # that should not have been offered in the first place.
    #
    # status and opslog stay: one reports the deployment, the other is a log.
    from .deploy_state import saved_wizard_topology as _topo_for_op

    if op not in ("status", "opslog") and _topo_for_op() == "split":
        yield proto.event_stderr(
            f"'{op}' is a cluster operation and this is a multi deployment.\n"
            "The edge and the mailbox are separate machines with separate jobs; "
            "neither fails over to the other, so there is no maintenance mode "
            "to enter and nothing to fail back.\n"
        )
        yield proto.event_done(2)
        return

    if op == "status":
        st = await gather_status()

        # A multi deployment prints its own transcript and returns here.
        #
        # Everything below indexes Pacemaker keys directly - st["promoted"],
        # st["qdevice_ok"], st["drbd_uptodate"] - and a split snapshot has none
        # of them, so falling through would raise KeyError and the Cluster page
        # would show an error on every refresh for every multi deployment.
        # Padding the snapshot with neutral values would avoid the crash and
        # print "promoted=None qdevice_ok=False drbd_uptodate=False" into the
        # ops log, which is the exact noise this release is removing.
        if st.get("topology") == "split":
            dep = st.get("deployment") or {}
            yield await _emit("=== multi deployment status ===")
            yield await _emit(f"local={st.get('local_host')}")
            for node in dep.get("nodes") or []:
                if not node.get("present"):
                    yield await _emit(f"{node.get('role')}: not linked")
                    continue
                checks = ",".join(
                    f"{c.get('label')}={'ok' if c.get('ok') else 'down'}"
                    for c in node.get("checks") or []
                )
                yield await _emit(
                    f"{node.get('role')}={node.get('name')} "
                    f"{'ok' if node.get('ok') else 'DOWN'} [{checks}]"
                )
            public = {
                "local_host": st.get("local_host") or "",
                "topology": "split",
                "deployment": dep,
                "nodes": st.get("nodes") or [],
                "offline": st.get("offline") or [],
                "standby": [],
                "stale_peers": [],
                "rejoining": [],
                "privileged_run": _running_job_snapshot(),
            }
            yield await _emit(
                "CLUSTER_STATUS_JSON:" + json.dumps(public, separators=(",", ":"))
            )
            yield proto.event_done(0)
            return

        yield await _emit("=== cluster maintenance status ===")
        yield await _emit(f"local={st['local_host']}")
        yield await _emit(f"nodes={st['nodes']}")
        yield await _emit(f"standby={st['standby']}")
        yield await _emit(f"offline={st['offline']}")
        yield await _emit(f"stale_peers={st['stale_peers']}")
        yield await _emit(f"promoted={st['promoted']}")
        if st.get("promoted_conflict"):
            yield await _emit(
                f"promoted_conflict=True names={st.get('promoted_names')}",
                err=True,
            )
        yield await _emit(f"unpromoted={st['unpromoted']}")
        yield await _emit(f"zimbra_node={st.get('zimbra_node') or '-'}")
        yield await _emit(f"drbd_uptodate={st['drbd_uptodate']}")
        yield await _emit(f"drbd_sync_percent={st.get('drbd_sync_percent')}")
        yield await _emit(f"qdevice_ok={st['qdevice_ok']}")
        yield await _emit(f"vip={st.get('vip_ip') or '-'} on={st.get('vip_node') or '-'}")
        yield await _emit(
            f"quorate={st.get('quorate')} "
            f"votes={st.get('votes_total')}/{st.get('votes_needed')}"
        )
        if st.get("quorum_hint"):
            yield await _emit(str(st["quorum_hint"]), err=True)
        obs = st.get("observability") or {}
        yield await _emit(
            "observability="
            f"status={obs.get('status')} ip={obs.get('ip') or '-'} "
            f"reachable={obs.get('reachable')}"
        )
        yield await _emit(f"failcount_ok={st['failcount_ok']}")
        for line in st["failcount_lines"]:
            yield await _emit(line)
        yield await _emit(f"topology={st.get('topology') or '-'}")
        node_ips = node_ips_for_status(
            st.get("addrs") or {},
            local_host=str(st.get("local_host") or ""),
            local_ip=_config_server_ip(),
        )
        from .add_host import attach_peer_eligible, read_last_removed_peer

        last_removed = read_last_removed_peer()
        attach_ok = attach_peer_eligible(
            topology=str(st.get("topology") or ""),
            live_nodes=list(st.get("nodes") or []),
            vip_ip=str(st.get("vip_ip") or ""),
            vip_node=st.get("vip_node") if isinstance(st.get("vip_node"), str) else None,
            promoted=st.get("promoted") if isinstance(st.get("promoted"), str) else None,
            package_stub=bool(st.get("package_stub")),
        )
        public = {
            "local_host": st["local_host"],
            "topology": st.get("topology") or "",
            "nodes": st["nodes"],
            "standby": st["standby"],
            "offline": st["offline"],
            "stale_peers": st["stale_peers"],
            "promoted": st["promoted"],
            "promoted_conflict": bool(st.get("promoted_conflict")),
            "promoted_names": st.get("promoted_names") or [],
            "unpromoted": st["unpromoted"],
            "zimbra_node": st.get("zimbra_node"),
            "drbd_uptodate": st["drbd_uptodate"],
            "drbd_sync_percent": st.get("drbd_sync_percent"),
            "drbd_link": st.get("drbd_link") or "",
        # Certificate expiry rides on this poll so it can raise an alert. It
        # is the one fault that is invisible until the day it takes mail down,
        # and on two of the three TLS methods nothing renews it automatically.
        "tls_days_left": st.get("tls_days_left"),
        "tls_method": st.get("tls_method") or "",
        "tls_source": st.get("tls_source") or "",
            "qdevice_ok": st["qdevice_ok"],
            "quorate": st.get("quorate"),
            "votes_total": st.get("votes_total"),
            "votes_needed": st.get("votes_needed"),
            "quorum_hint": st.get("quorum_hint") or "",
            "no_quorum_policy": st.get("no_quorum_policy") or "",
            "fencing_enabled": st.get("fencing_enabled"),
            "observability": st.get("observability") or {},
            "failcount_ok": st["failcount_ok"],
            "maintenance_active": bool(st["standby"]),
            # A node that is up but has not been given a DRBD role yet. Drawn
            # as its own state so a reboot does not read as either healthy or
            # failed, neither of which it is.
            "rejoining": st.get("rejoining") or [],
            # Pacemaker standby survives a reboot. Saying when it was entered,
            # and whether this console did it, is the difference between "we
            # did that" and "why is this node in maintenance?".
            "maintenance_since": st.get("maintenance_since") or "",
            "maintenance_source": st.get("maintenance_source") or "",
            # Distinct from maintenance_active, which means "a node is in
            # standby". This one means Pacemaker has been told to stop acting
            # on resources cluster-wide, which looks identical to a dead
            # cluster from the outside unless it is said out loud.
            "maintenance_mode": bool(st.get("maintenance_mode")),
            "bans": st.get("bans") or [],
            "node_ips": node_ips,
            "vip_ip": st.get("vip_ip") or "",
            "vip_node": st.get("vip_node"),
            "last_removed_peer": last_removed or None,
            "attach_peer_eligible": attach_ok,
            "configured_peer": _config_peer_name(),
            # What the helper is doing right now. The console's own idea of
            # "busy" is React state that a page reload throws away, so after a
            # refresh it re-offered Enter Maintenance and Remove Host while a
            # job was still running and the operator got a bare "busy" with
            # nothing saying what (live QA, 8 Sep 2026).
            "privileged_run": _running_job_snapshot(),
            "package_stub": bool(st.get("package_stub")),
        }
        yield await _emit("CLUSTER_STATUS_JSON:" + json.dumps(public, separators=(",", ":")))
        yield proto.event_done(0)
        return

    if op == "clearban":
        # Removes only the cli-ban-* location constraints that `pcs resource
        # ban` writes, which is exactly what `pcs resource clear` targets. A
        # ban that outlived the Move Master that placed it forbids promotion
        # for good: no Primary, no mail, and no failure reported anywhere.
        # Recovering from that used to require SSH.
        st = await gather_status()
        bans = st.get("bans") or []
        if not bans:
            yield await _emit("No leftover Move Master bans to clear.")
            yield proto.event_done(0)
            return
        resources: list[str] = []
        for ban in bans:
            name = str(ban.get("resource") or "").strip()
            if name and name not in resources:
                resources.append(name)
        rc = 0
        for name in resources:
            yield await _emit(f"pcs resource clear {name}")
            c, out, err = await _capture(
                ["pcs", "resource", "clear", name], timeout=120
            )
            if out:
                yield await _emit(out)
            if err:
                yield await _emit(err, err=True)
            if c != 0:
                rc = c
        st_after = await gather_status()
        left = st_after.get("bans") or []
        yield await _emit(
            f"promoted={st_after.get('promoted')} "
            f"vip_node={st_after.get('vip_node')} "
            f"bans_remaining={len(left)}"
        )
        if left:
            yield await _emit(
                "Some bans are still present: "
                + "; ".join(str(b.get("id") or "?") for b in left),
                err=True,
            )
            rc = rc or 1
        else:
            yield await _emit(
                "Constraints cleared. The cluster can promote a node again; "
                "give it a minute to settle before checking the Master."
            )
        yield proto.event_done(rc)
        return

    if op == "cleanup":
        # Clears Pacemaker fail-count/last-failure history and re-probes every
        # resource. A probe that finds a failed resource may stop/restart it.
        # Refuse while dual-Promoted or dual-Primary is detected so cleanup
        # cannot paper over a split-brain. Before this button existed, the
        # only path was `pcs resource cleanup` over SSH (live 2vm practice,
        # 25 Aug 2026).
        lock_fh = try_lock_maintenance()
        if lock_fh is None:
            yield await _emit(
                "Refusing: another maintenance enter or exit is already running.",
                err=True,
            )
            yield proto.event_done(1)
            return
        try:
            yield await _emit("=== maintenance cleanup ===")
            st = await gather_status()
            if st.get("promoted_conflict"):
                yield await _emit(
                    "Refusing cleanup: crm_mon shows more than one Promoted node "
                    f"({', '.join(st.get('promoted_names') or [])}). Resolve dual "
                    "Promoted before re-probing resources.",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if ASSERT_SCRIPT.is_file():
                c_assert, out_a, err_a = await _capture([str(ASSERT_SCRIPT)])
                assert_txt = (out_a + err_a).strip()
                if c_assert != 0 or "NO_DUAL_PRIMARY_OK" not in assert_txt:
                    yield await _emit(
                        "Refusing cleanup: dual-primary assert failed. "
                        f"{assert_txt or f'exit {c_assert}'}",
                        err=True,
                    )
                    yield proto.event_done(1)
                    return
            elif (st.get("topology") or "") != "1vm":
                yield await _emit(
                    f"Refusing cleanup: assert script missing: {ASSERT_SCRIPT}",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if (st.get("topology") or "") != "1vm" and not st.get("promoted"):
                yield await _emit(
                    "Refusing cleanup: no stable Promoted node; wait for "
                    "Pacemaker to settle before re-probing.",
                    err=True,
                )
                yield proto.event_done(1)
                return
            yield await _emit(
                "pcs resource cleanup (clears fail-counts and re-probes; "
                "failed resources may restart)"
            )
            c, out, err = await _capture(["pcs", "resource", "cleanup"], timeout=120)
            if out:
                yield await _emit(out)
            if err:
                yield await _emit(err, err=True)
            if c != 0:
                yield await _emit(f"pcs resource cleanup failed (exit {c})", err=True)
                yield proto.event_done(c)
                return
            st = await gather_status()
            yield await _emit(f"failcount_ok={st['failcount_ok']}")
            for line in st["failcount_lines"]:
                yield await _emit(line)
            yield proto.event_done(0)
        finally:
            release_maintenance_lock(lock_fh)
        return

    try:
        target = await resolve_target(str(args.get("target") or ""))
    except ValueError as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return

    yield await _emit(f"=== maintenance {op} target={target} ===")

    if op == "opslog":
        # Read-only. Seeds the Activity log so a refresh does not erase what
        # the last Move Master, maintenance, or cleanup actually did.
        if not CLUSTER_OPS_LOG.is_file():
            yield await _emit(
                "(no cluster operations recorded yet - Move Master, maintenance, "
                "and constraint clears are written here as they run)"
            )
            yield proto.event_done(0)
            return
        try:
            text = CLUSTER_OPS_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            yield await _emit(f"cannot read {CLUSTER_OPS_LOG}: {exc}", err=True)
            yield proto.event_done(1)
            return
        yield await _emit(text.rstrip("\n") if text.strip() else "(transcript is empty)")
        yield proto.event_done(0)
        return

    if op == "preflight":
        ok, logs, checks = await run_preflight(target)
        for line in logs:
            yield await _emit(line)
        yield await _emit("PREFLIGHT_JSON:" + json.dumps(checks, separators=(",", ":")))
        yield proto.event_done(0 if ok else 1)
        return

    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield await _emit(
            "Refusing: another maintenance enter or exit is already running.",
            err=True,
        )
        yield proto.event_done(1)
        return

    try:
        if op == "enter":
            local = this_hostname()
            if hosts_match(target, local):
                yield await _emit(
                    "Refusing Enter Maintenance on the host serving this console. "
                    "Open the peer console and put this node in maintenance from there.",
                    err=True,
                )
                yield proto.event_done(1)
                return
            ok, logs, checks = await run_preflight(target)
            for line in logs:
                yield await _emit(line)
            yield await _emit("PREFLIGHT_JSON:" + json.dumps(checks, separators=(",", ":")))
            if not ok:
                yield await _emit(
                    "Refusing enter: pre-flight failed (no override in this slice).",
                    err=True,
                )
                yield proto.event_done(1)
                return
            yield await _emit(f"pcs node standby {target}")
            c, out, err = await _capture(["pcs", "node", "standby", target], timeout=120)
            if out:
                yield await _emit(out)
            if err:
                yield await _emit(err, err=True)
            if c != 0:
                yield await _emit(f"pcs node standby failed (exit {c})", err=True)
                yield proto.event_done(c)
                return
            _c, crm, _e = await _capture(["crm_mon", "-1", "-r"])
            yield await _emit(crm or "(no crm_mon output)")
            _c2, nodes, _e2 = await _capture(["pcs", "status", "nodes"])
            yield await _emit(nodes)
            st = await gather_status()
            yield await _emit(f"promoted_now={st['promoted']} standby={st['standby']}")
            if target not in st["standby"]:
                yield await _emit(
                    f"pcs node standby returned success but {target} is not listed as Standby "
                    "(including Standby with resource(s) running).",
                    err=True,
                )
                yield proto.event_done(1)
                return
            yield proto.event_done(0)
            return

        if op == "failback":
            # Controlled Master move (HA-RUNBOOK §3): ban current Promoted,
            # wait for target + VIP, then clear. Does not enable prefer pin.
            yield await _emit(
                f"Controlled Master move to {target} "
                f"(expect several minutes of mail downtime while Zimbra restarts)"
            )
            st = await gather_status()
            if st.get("promoted_conflict"):
                yield await _emit(
                    "Refusing failback: crm_mon shows more than one Promoted node "
                    f"({', '.join(st.get('promoted_names') or [])}).",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if ASSERT_SCRIPT.is_file():
                c_assert, out_a, err_a = await _capture([str(ASSERT_SCRIPT)])
                assert_txt = (out_a + err_a).strip()
                if c_assert != 0 or "NO_DUAL_PRIMARY_OK" not in assert_txt:
                    yield await _emit(
                        "Refusing failback: dual-primary assert failed. "
                        f"{assert_txt or f'exit {c_assert}'}",
                        err=True,
                    )
                    yield proto.event_done(1)
                    return
            current = st.get("promoted")
            if not current:
                yield await _emit(
                    "Refusing failback: no stable Promoted node yet.",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if hosts_match(str(current), target):
                yield await _emit(
                    f"Refusing failback: {target} is already Promoted.",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if target in (st.get("standby") or []) or target in (st.get("offline") or []):
                yield await _emit(
                    f"Refusing failback: {target} must be Online (not Standby/Offline).",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if current in (st.get("standby") or []) or current in (st.get("offline") or []):
                yield await _emit(
                    f"Refusing failback: current Promoted {current} is not Online.",
                    err=True,
                )
                yield proto.event_done(1)
                return
            if not bool(st.get("qdevice_ok")):
                yield await _emit(
                    "Refusing failback: quorum device is not voting.",
                    err=True,
                )
                yield proto.event_done(1)
                return

            yield await _emit("Waiting for DRBD both replicas UpToDate before move")
            sync_ok, sync_logs = await wait_drbd_uptodate()
            for line in sync_logs:
                yield await _emit(line)
            if not sync_ok:
                yield await _emit(
                    "Refusing failback: DRBD did not reach UpToDate/UpToDate in time.",
                    err=True,
                )
                yield proto.event_done(1)
                return

            # Health gate. Everything above rules out an impossible move; this
            # rules out a move that is possible but unwise. The Phase 7 2-host
            # run started here: the target's pre-flight had already failed, the
            # console showed that failure, and Move Master stayed clickable
            # anyway. Banning the only working Promoted node so the stack can
            # land on a node that cannot take it leaves mail down on both.
            #
            # This runs after the DRBD wait above, not before, so a move that
            # merely started while a resync was finishing is not refused for a
            # condition that had already cleared by the time we would act.
            yield await _emit(f"Pre-flight check on {target} before moving the Master")
            pf_ok, pf_logs, pf_checks = await run_preflight(target)
            for line in pf_logs:
                yield await _emit(line)
            if not pf_ok:
                failed = [
                    f"{name} ({info.get('detail') or 'no detail'})"
                    for name, info in pf_checks.items()
                    if not name.startswith("_") and not info.get("ok")
                ]
                yield await _emit(
                    "Refusing failback: the cluster is not healthy enough to move "
                    "the Master. Nothing has been changed. Failed: "
                    + "; ".join(failed),
                    err=True,
                )
                yield await _emit(
                    "Fix the checks above first. Moving the Master away from a "
                    "healthy node onto an unhealthy one takes mail down on both.",
                    err=True,
                )
                yield proto.event_done(1)
                return

            ban_flag = await promoted_ban_flag()
            yield await _emit(
                f"pcs resource ban {DRBD_CLONE} {current} {ban_flag}"
            )
            c, out, err = await _capture(
                ["pcs", "resource", "ban", DRBD_CLONE, current, ban_flag],
                timeout=120,
            )
            # Belt and braces: if pcs rejected the FLAG (not the operation),
            # the help probe guessed wrong for this build - try the other
            # spelling once rather than failing a move that would have worked.
            # Nothing has been changed at this point, so the retry is safe.
            if c != 0 and flag_not_recognized(f"{out}\n{err}"):
                ban_flag = other_ban_flag(ban_flag)
                yield await _emit(
                    f"pcs rejected that option; retrying with {ban_flag}"
                )
                c, out, err = await _capture(
                    ["pcs", "resource", "ban", DRBD_CLONE, current, ban_flag],
                    timeout=120,
                )
            if out:
                yield await _emit(out)
            if err:
                yield await _emit(err, err=True)
            if c != 0:
                yield await _emit(
                    f"pcs resource ban failed (exit {c}); left cluster unchanged",
                    err=True,
                )
                yield proto.event_done(c)
                return

            # Everything from here until the ban is cleared runs with a
            # -INFINITY constraint pinned on the node that is currently serving
            # mail. Every handled failure below removes it. Nothing removed it
            # if something RAISED - an asyncio timeout inside the settle poll, a
            # subprocess error in gather_status, or the operator simply closing
            # the browser tab, which closes this generator. The cluster would
            # then be left unable to promote ANY node: DRBD with no Primary, the
            # group unable to start, mail down on both, and nothing on screen
            # naming the constraint that did it.
            ban_cleared = False
            try:
                yield await _emit(
                    f"Waiting for Promoted + VIP + Zimbra on {target}. "
                    f"This finishes as soon as they land, usually in well under a "
                    f"minute; {FAILBACK_TIMEOUT_SEC}s is the point at which it "
                    f"gives up, not how long it takes."
                )
                settled, settle_logs = await wait_failback_settled(target)
                for line in settle_logs:
                    yield await _emit(line)
                if not settled:
                    # If the stack already landed on the target despite settle
                    # reporting false, clear the temporary ban so the cluster is
                    # not left with a sticky constraint.
                    st_now = await gather_status()
                    if _stack_on_target(st_now, target):
                        yield await _emit(
                            f"Settle reported incomplete, but stack is already on {target}; "
                            f"clearing temporary ban on {DRBD_CLONE}"
                        )
                        c_clr, out_clr, err_clr = await _capture(
                            ["pcs", "resource", "clear", DRBD_CLONE],
                            timeout=120,
                        )
                        ban_cleared = c_clr == 0
                        if out_clr:
                            yield await _emit(out_clr)
                        if err_clr:
                            yield await _emit(err_clr, err=True)
                        # Re-read the whole stack, not just `promoted`. Clearing a
                        # ban makes Pacemaker recompute placement, and for a moment
                        # `promoted` can read empty or stale while the transition
                        # runs. Judging the move on that one field reported a
                        # completed Move Master as FAILED, then fell through to the
                        # failure branch and told the operator the Master had not
                        # moved - while it was sitting on the target (live, 31 Aug
                        # 2026). Give the transition a few polls to land.
                        st_after = await gather_status()
                        for _ in range(6):
                            if c_clr != 0 or _stack_on_target(st_after, target):
                                break
                            await asyncio.sleep(FAILBACK_POLL_SEC)
                            st_after = await gather_status()
                        if c_clr == 0 and _stack_on_target(st_after, target):
                            yield await _emit(f"Master move to {target} complete (recovered)")
                            yield proto.event_done(0)
                            return
                        if c_clr == 0:
                            # The stack was on the target a moment ago and is not
                            # now. Say that, rather than "the Master was not moved",
                            # which is a different and wrong story.
                            yield await _emit(
                                f"The stack reached {target} but did not stay there "
                                f"(promoted={st_after.get('promoted')} "
                                f"vip_node={st_after.get('vip_node')} "
                                f"zimbra_node={st_after.get('zimbra_node')}). "
                                "The temporary ban has been cleared.",
                                err=True,
                            )
                            yield proto.event_done(1)
                            return
                    # The move failed and the stack is not on the target, so the
                    # ban we placed is now pointing -INFINITY at the node that was
                    # serving mail. Leaving it there means no node is allowed to be
                    # Promoted: DRBD has no Primary, the group cannot start, and
                    # mail is down on both nodes with nothing on screen naming the
                    # constraint that did it. This used to ask the operator to run
                    # `pcs resource clear` by hand, which is exactly the SSH-only
                    # recovery this console exists to remove.
                    #
                    # Clearing is safe here: it only removes the temporary ban this
                    # operation added, putting placement back where it was before we
                    # started. The cluster then settles the Master wherever it can
                    # actually run, which is the state the operator wants to be in
                    # while they work out why the target would not take it.
                    yield await _emit(
                        f"Move did not settle on {target}. Removing the temporary ban "
                        f"so the cluster is not left unable to promote any node."
                    )
                    c_undo, out_undo, err_undo = await _capture(
                        ["pcs", "resource", "clear", DRBD_CLONE],
                        timeout=120,
                    )
                    ban_cleared = c_undo == 0
                    if out_undo:
                        yield await _emit(out_undo)
                    if err_undo:
                        yield await _emit(err_undo, err=True)
                    if c_undo == 0:
                        st_undo = await gather_status()
                        yield await _emit(
                            f"Ban removed. promoted={st_undo.get('promoted')} "
                            f"vip_node={st_undo.get('vip_node')} "
                            f"drbd_uptodate={st_undo.get('drbd_uptodate')}"
                        )
                        yield await _emit(
                            f"The Master was not moved to {target}. The cluster is "
                            "back to the placement it had before this attempt.",
                            err=True,
                        )
                    else:
                        yield await _emit(
                            f"pcs resource clear failed (exit {c_undo}). A ban on "
                            f"{current} may still be in place, which stops ANY node "
                            f"being promoted. Clear it as soon as you can: "
                            f"pcs resource clear {DRBD_CLONE}",
                            err=True,
                        )
                    yield proto.event_done(1)
                    return

                yield await _emit(f"pcs resource clear {DRBD_CLONE}")
                c2, out2, err2 = await _capture(
                    ["pcs", "resource", "clear", DRBD_CLONE],
                    timeout=120,
                )
                ban_cleared = c2 == 0
                if out2:
                    yield await _emit(out2)
                if err2:
                    yield await _emit(err2, err=True)
                if c2 != 0:
                    yield await _emit(
                        f"pcs resource clear failed (exit {c2}); Master is on {target} "
                        "but the temporary ban may still be present",
                        err=True,
                    )
                    yield proto.event_done(c2)
                    return

                st = await gather_status()
                yield await _emit(
                    f"promoted_now={st.get('promoted')} vip_node={st.get('vip_node')} "
                    f"drbd_uptodate={st.get('drbd_uptodate')}"
                )
                if not hosts_match(str(st.get("promoted") or ""), target):
                    yield await _emit(
                        f"After clear, Promoted is {st.get('promoted')}, not {target}",
                        err=True,
                    )
                    yield proto.event_done(1)
                    return
                yield await _emit(f"Master move to {target} complete")
                yield proto.event_done(0)
                return
            except BaseException:
                # Includes GeneratorExit and CancelledError: a closed tab must
                # not leave the cluster pinned. No yields in here - an async
                # generator may not yield while it is being closed - so this
                # clears the ban silently and lets the exception carry on.
                if not ban_cleared:
                    try:
                        await _capture(
                            ["pcs", "resource", "clear", DRBD_CLONE], timeout=60
                        )
                    except BaseException:  # noqa: BLE001 - best effort teardown
                        pass
                raise

        yield await _emit(f"pcs node unstandby {target}")
        c, out, err = await _capture(["pcs", "node", "unstandby", target], timeout=120)
        if out:
            yield await _emit(out)
        if err:
            yield await _emit(err, err=True)
        if c != 0:
            yield await _emit(f"pcs node unstandby failed (exit {c})", err=True)
            yield proto.event_done(c)
            return
        yield await _emit(
            "Waiting for Secondary-only + DRBD UpToDate + fail-count 0 + NO_DUAL_PRIMARY_OK"
        )
        ok, logs = await wait_exit_healthy(target)
        for line in logs:
            yield await _emit(line)
        st = await gather_status()
        yield await _emit(
            f"promoted_now={st['promoted']} standby={st['standby']} "
            f"drbd_uptodate={st['drbd_uptodate']}"
        )
        if ok:
            yield await _emit("maintenance exit done")
        else:
            yield await _emit(
                "maintenance exit incomplete: node unstandby'd but verification did not pass",
                err=True,
            )
        yield proto.event_done(0 if ok else 1)
    finally:
        release_maintenance_lock(lock_fh)


# Everything the Cluster page runs used to exist only in the browser tab that
# ran it. A hard refresh, a navigation, or the node itself rebooting mid-move
# and the transcript was gone - so "what did Move Master actually do?" had no
# answer anywhere, which is exactly the question the Phase 7 run needed to ask.
# The status output is a fresh snapshot, not a record, and it is what the
# Activity log was seeded from.
CLUSTER_OPS_LOG = Path(
    os.environ.get("KIN_CLUSTER_OPS_LOG", "/var/log/kin-mail/cluster-ops.log")
)
CLUSTER_OPS_LOG_LIMIT = 2_000_000
_OPS_LOGGED_OPS = frozenset({"enter", "exit", "cleanup", "failback", "clearban"})


def _trim_ops_log(path: Path, limit: int = CLUSTER_OPS_LOG_LIMIT) -> None:
    """Keep the transcript bounded without losing the most recent runs."""
    try:
        if path.stat().st_size <= limit:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    keep = text[-(limit // 2):]
    # Start at a line boundary so the file never opens mid-word.
    _, sep, rest = keep.partition("\n")
    try:
        path.write_text(
            "=== earlier entries trimmed ===\n" + (rest if sep else keep),
            encoding="utf-8",
        )
    except OSError:
        return


def append_cluster_ops_log(text: str, *, path: Path | None = None) -> None:
    target = CLUSTER_OPS_LOG if path is None else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        # A transcript that cannot be written must never take down the
        # operation it was recording.
        return
    _trim_ops_log(target)


async def cmd_maintenance(args: dict[str, Any] | None = None) -> Any:
    """Run a maintenance op, recording mutating ones to a durable transcript."""
    op = str((args or {}).get("op") or "status").strip()
    record = op in _OPS_LOGGED_OPS
    if record:
        target = str((args or {}).get("target") or "").strip()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        append_cluster_ops_log(
            f"\n=== {op}{(' ' + target) if target else ''} - {stamp} ===\n"
        )
    async for event in _maintenance_events(args):
        if record:
            kind = event.get("type")
            if kind in ("stdout", "stderr"):
                append_cluster_ops_log(str(event.get("data") or ""))
            elif kind == "done":
                append_cluster_ops_log(
                    f"=== {op} finished, exit {event.get('exit_code')} ===\n"
                )
        yield event
