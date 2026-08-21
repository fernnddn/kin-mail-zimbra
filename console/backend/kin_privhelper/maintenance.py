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


def parse_promoted(crm: str) -> str | None:
    for line in crm.splitlines():
        match = re.search(r"(?:Promoted|Masters):\s*\[([^\]]+)\]", line)
        if match:
            names = _node_tokens(match.group(1))
            return names[0] if names else None
    return None


def parse_unpromoted(crm: str) -> list[str]:
    for line in crm.splitlines():
        match = re.search(r"(?:Unpromoted|Slaves):\s*\[([^\]]+)\]", line)
        if match:
            return _node_tokens(match.group(1))
    return []


def drbd_both_uptodate(status: str) -> bool:
    """True when every disk/peer-disk line in `drbdadm status` is UpToDate."""
    disks = re.findall(r"(?:disk|peer-disk):\s*(\S+)", status, flags=re.I)
    if not disks:
        # Newer drbdadm: "disk UpToDate" without colon
        disks = re.findall(r"\bdisk[:\s]+(\S+)", status, flags=re.I)
    if len(disks) < 2:
        return False
    return all(d.lower() == "uptodate" for d in disks)


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
    """Map node name → ring0_addr from corosync.conf."""
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


def parse_failcount_value(text: str) -> int:
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
    return 0


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
    """Local zmcontrol, or crm_mon for the peer — no inter-node SSH.

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


async def gather_status() -> dict[str, Any]:
    nodes_text = await _pcs_nodes_text()
    nodes = parse_online_nodes(nodes_text)
    standby = parse_standby_nodes(nodes_text)
    offline = parse_offline_nodes(nodes_text)
    _c, crm, _e = await _capture(["crm_mon", "-1", "-r"])
    promoted = parse_promoted(crm)
    unpromoted = parse_unpromoted(crm)
    _c2, drbd, _e2 = await _capture(["drbdadm", "status", DRBD_RESOURCE])
    _c3, quorum, _e3 = await _capture(["pcs", "quorum", "status"])
    addrs = {}
    if COROSYNC_CONF.is_file():
        addrs = parse_corosync_ring_addrs(COROSYNC_CONF.read_text(encoding="utf-8", errors="replace"))
    live = set(nodes) | set(offline)
    stale_peers = [n for n in addrs if n not in live]
    fc_ok, fc_lines = await _failcounts(nodes)
    return {
        "local_host": this_hostname(),
        "nodes": nodes,
        "standby": standby,
        "offline": offline,
        "stale_peers": stale_peers,
        "promoted": promoted,
        "unpromoted": unpromoted,
        "drbd_uptodate": drbd_both_uptodate(drbd),
        "qdevice_ok": qdevice_voting(quorum),
        "failcount_ok": fc_ok,
        "failcount_lines": fc_lines,
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


async def cmd_maintenance(args: dict[str, Any] | None = None) -> Any:
    args = args or {}
    op = str(args.get("op") or "status").strip().lower()
    if op not in ("status", "preflight", "enter", "exit"):
        yield proto.event_stderr("op must be status, preflight, enter, or exit\n")
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
        yield await _emit(f"unpromoted={st['unpromoted']}")
        yield await _emit(f"drbd_uptodate={st['drbd_uptodate']}")
        yield await _emit(f"qdevice_ok={st['qdevice_ok']}")
        yield await _emit(f"failcount_ok={st['failcount_ok']}")
        for line in st["failcount_lines"]:
            yield await _emit(line)
        public = {
            "local_host": st["local_host"],
            "nodes": st["nodes"],
            "standby": st["standby"],
            "offline": st["offline"],
            "stale_peers": st["stale_peers"],
            "promoted": st["promoted"],
            "unpromoted": st["unpromoted"],
            "drbd_uptodate": st["drbd_uptodate"],
            "qdevice_ok": st["qdevice_ok"],
            "failcount_ok": st["failcount_ok"],
            "maintenance_active": bool(st["standby"]),
        }
        yield await _emit("CLUSTER_STATUS_JSON:" + json.dumps(public, separators=(",", ":")))
        yield proto.event_done(0)
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
