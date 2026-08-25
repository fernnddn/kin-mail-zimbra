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
from pathlib import Path
from typing import Any, IO

from . import protocol as proto

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
FAILBACK_TIMEOUT_SEC = int(os.environ.get("KIN_FAILBACK_TIMEOUT", "900"))
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


def parse_online_nodes(pcs_nodes: str) -> list[str]:
    """Parse `pcs status nodes` member lines (Online, Standby, draining)."""
    found: list[str] = []
    for line in pcs_nodes.splitlines():
        rest = _pcs_nodes_line_rest(line, _MEMBER_PREFIXES)
        if rest is None:
            continue
        found.extend(_node_tokens(rest))
    if not found:
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


async def _pcs_nodes_text() -> str:
    code, out, err = await _capture(["pcs", "status", "nodes"])
    if code != 0 or not out.strip():
        _c2, out2, _e2 = await _capture(["crm_node", "-l"])
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


async def _failcounts(nodes: list[str]) -> tuple[bool, list[str]]:
    lines: list[str] = []
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
                ok = False
                lines.append(f"  {res}@{node} fail-count=query-failed:{c}")
                continue
            val = parse_failcount_value(text)
            if val is None:
                ok = False
                lines.append(f"  {res}@{node} fail-count=unparsed:{text[:80]}")
                continue
            lines.append(f"  {res}@{node} fail-count={val}")
            if val != 0:
                ok = False
    return ok, lines


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


async def gather_status() -> dict[str, Any]:
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
    fc_ok, fc_lines = await _failcounts(nodes)
    observability = await _observability_snapshot(corosync_txt)
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

    return {
        "local_host": this_hostname(),
        "topology": saved_wizard_topology(),
        "nodes": nodes,
        "standby": standby,
        "offline": offline,
        "stale_peers": stale_peers,
        "promoted": promoted,
        "promoted_names": promoted_names,
        "promoted_conflict": promoted_conflict,
        "unpromoted": unpromoted,
        "zimbra_node": zimbra_node,
        "drbd_uptodate": drbd_both_uptodate(drbd),
        "drbd_sync_percent": drbd_sync_percent(drbd),
        "qdevice_ok": qdevice_voting(quorum),
        "observability": observability,
        "failcount_ok": fc_ok,
        "failcount_lines": fc_lines,
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
        f"nodes={st['nodes'] or 'none'} standby={st['standby']}",
    )
    rec("drbd_uptodate", bool(st["drbd_uptodate"]), "both replicas UpToDate" if st["drbd_uptodate"] else "DRBD is not UpToDate/UpToDate")
    rec("qdevice_voting", bool(st["qdevice_ok"]), "qdevice reachable and voting" if st["qdevice_ok"] else "qdevice missing, offline, or not voting")
    rec("failcount_zero", bool(st["failcount_ok"]), "; ".join(st["failcount_lines"]) or "no fail-count records")

    # Peer that must keep serving (currently Promoted), plus the other node Online.
    promoted = st["promoted"]
    peer = None
    for n in st["nodes"]:
        if n != target:
            peer = n
            break
    rec("peer_online", peer is not None, f"peer={peer}" if peer else "no peer in nodelist")

    if target in st["standby"]:
        rec("not_already_standby", False, f"{target} is already in standby")
    else:
        rec("not_already_standby", True, f"{target} is not in standby")

    serving = promoted or peer
    https_ok = False
    zm_ok = False
    https_detail = "skipped (no serving node)"
    zm_detail = "skipped"
    if serving:
        addr = st["addrs"].get(serving) or serving
        body, _c = await _https_code(f"https://{addr}/")
        https_ok = body == "200"
        https_detail = f"https://{addr}/ → HTTP {body}"
        zm_ok, zm_detail = await _zmcontrol_on(serving, addr, st["raw"]["crm"])
        zm_detail = zm_detail[:1500]
    rec("peer_https", https_ok, https_detail)
    rec("peer_zmcontrol", zm_ok, zm_detail.splitlines()[0] if zm_detail else "")

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
    checks["_standby"] = st["standby"]
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


async def wait_drbd_uptodate(*, timeout_sec: int | None = None) -> tuple[bool, list[str]]:
    """Poll until both DRBD replicas are UpToDate (or timeout)."""
    limit = FAILBACK_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    logs: list[str] = []
    deadline = asyncio.get_event_loop().time() + limit
    while True:
        st = await gather_status()
        pct = st.get("drbd_sync_percent")
        uptodate = bool(st["drbd_uptodate"])
        if pct is None:
            logs.append(f"drbd_uptodate={uptodate}")
        else:
            logs.append(f"drbd_uptodate={uptodate} sync={pct:.1f}%")
        if uptodate:
            logs.append("DRBD both replicas UpToDate")
            return True, logs
        if asyncio.get_event_loop().time() >= deadline:
            logs.append(f"TIMEOUT waiting for DRBD UpToDate after {limit}s")
            return False, logs
        await asyncio.sleep(FAILBACK_POLL_SEC)


async def wait_failback_settled(
    target: str,
    *,
    timeout_sec: int | None = None,
) -> tuple[bool, list[str]]:
    """Wait until Promoted + VIP (+ Zimbra) are on target after a ban."""
    limit = FAILBACK_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    logs: list[str] = []
    deadline = asyncio.get_event_loop().time() + limit
    vip_ip = ""
    while True:
        st = await gather_status()
        vip_ip = str(st.get("vip_ip") or vip_ip or "")
        promoted = st.get("promoted")
        vip_node = st.get("vip_node")
        zimbra_node = st.get("zimbra_node")
        dual = bool(st.get("promoted_conflict"))
        last = (
            f"promoted={promoted} vip_node={vip_node} zimbra_node={zimbra_node} "
            f"dual_promoted={dual}"
        )
        logs.append(last)
        promoted_ok = bool(promoted) and hosts_match(str(promoted), target)
        vip_ok = bool(vip_node) and hosts_match(str(vip_node), target)
        zimbra_ok = bool(zimbra_node) and hosts_match(str(zimbra_node), target)
        https_ok = True
        if vip_ip and promoted_ok and vip_ok:
            body, _c = await _https_code(f"https://{vip_ip}/")
            https_ok = body == "200"
            logs.append(f"https://{vip_ip}/ → HTTP {body}")
        if promoted_ok and vip_ok and zimbra_ok and https_ok and not dual:
            dual_ok = True
            if ASSERT_SCRIPT.is_file():
                c, out, err = await _capture([str(ASSERT_SCRIPT)])
                dual_txt = (out + err).strip()
                dual_ok = c == 0 and "NO_DUAL_PRIMARY_OK" in dual_txt
                logs.append(dual_txt or f"dual-primary assert exit {c}")
            if dual_ok:
                logs.append(f"Master settled on {target}")
                return True, logs
        if asyncio.get_event_loop().time() >= deadline:
            logs.append(f"TIMEOUT waiting for Master on {target} after {limit}s: {last}")
            return False, logs
        await asyncio.sleep(FAILBACK_POLL_SEC)


async def cmd_maintenance(args: dict[str, Any] | None = None) -> Any:
    args = args or {}
    op = str(args.get("op") or "status").strip().lower()
    if op not in ("status", "preflight", "enter", "exit", "cleanup", "failback"):
        yield proto.event_stderr(
            "op must be status, preflight, enter, exit, cleanup, or failback\n"
        )
        yield proto.event_done(2)
        return

    if op == "status":
        st = await gather_status()
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
            "qdevice_ok": st["qdevice_ok"],
            "observability": st.get("observability") or {},
            "failcount_ok": st["failcount_ok"],
            "maintenance_active": bool(st["standby"]),
            "node_ips": node_ips,
            "vip_ip": st.get("vip_ip") or "",
            "vip_node": st.get("vip_node"),
            "last_removed_peer": last_removed or None,
            "attach_peer_eligible": attach_ok,
            "package_stub": bool(st.get("package_stub")),
        }
        yield await _emit("CLUSTER_STATUS_JSON:" + json.dumps(public, separators=(",", ":")))
        yield proto.event_done(0)
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

            yield await _emit(
                f"pcs resource ban {DRBD_CLONE} {current} --promoted"
            )
            c, out, err = await _capture(
                ["pcs", "resource", "ban", DRBD_CLONE, current, "--promoted"],
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

            yield await _emit(
                f"Waiting for Promoted + VIP + Zimbra on {target} "
                f"(timeout {FAILBACK_TIMEOUT_SEC}s)"
            )
            settled, settle_logs = await wait_failback_settled(target)
            for line in settle_logs:
                yield await _emit(line)
            if not settled:
                yield await _emit(
                    "Failback did not settle on the target. Ban may still be in "
                    f"place on {current}. Do not issue a second ban; capture pcs "
                    f"status and clear manually when safe: pcs resource clear {DRBD_CLONE}",
                    err=True,
                )
                yield proto.event_done(1)
                return

            yield await _emit(f"pcs resource clear {DRBD_CLONE}")
            c2, out2, err2 = await _capture(
                ["pcs", "resource", "clear", DRBD_CLONE],
                timeout=120,
            )
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
