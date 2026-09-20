"""Fixed whitelist command implementations - no client-supplied argv/shell."""

from __future__ import annotations

import asyncio
import json
import grp
import os
import re
import shlex
import shutil
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import protocol as proto
from .deploy_state import (
    DEPLOY_LAST_LOG,
    ZIMBRA_INSTALL_LOG,
    append_unexpected_stop_if_needed,
    full_install_in_progress,
    is_full_install_complete,
    mark_full_install_finished,
    mark_full_install_started,
    mark_ha_orchestration_finished,
    mark_ha_orchestration_started,
    reclaim_stale_full_install_marker,
)

# Deploy tree on appliance (kin-mail.sh default). Override via env for lab clones.
DEPLOY_DIR = Path(os.environ.get("KIN_MAIL_DEPLOY_DIR", "/opt/kin-mail-deploy"))

# Basename only - resolved under DEPLOY_DIR; never take a path from the client.
HARDENING_CANDIDATES = (
    "install/09-hardening.sh",
    "09-hardening.sh",
)
KIN_MAIL_CANDIDATES = (
    "install/kin-mail.sh",
    "kin-mail.sh",
)
FIREWALL_CANDIDATES = (
    "install/10-host-firewall.sh",
    "10-host-firewall.sh",
)
GROW_DISK_CANDIDATES = (
    "install/lib/grow-disk.sh",
    "lib/grow-disk.sh",
)
MAIL_GATEWAY_CANDIDATES = (
    "install/lib/mail-gateway.sh",
    "lib/mail-gateway.sh",
)
CREATE_MAILBOX_CANDIDATES = (
    "install/08-create-mailbox.sh",
    "08-create-mailbox.sh",
)
PREPARE_OS_CANDIDATES = (
    "install/02-prepare-os.sh",
    "02-prepare-os.sh",
)


def resolve_under_deploy(candidates: tuple[str, ...], label: str) -> Path:
    """Resolve a script under DEPLOY_DIR; reject path escape (same pattern as former healthcheck)."""
    found: list[Path] = []
    for rel in candidates:
        candidate = (DEPLOY_DIR / rel).resolve()
        try:
            candidate.relative_to(DEPLOY_DIR.resolve())
        except ValueError as exc:
            raise RuntimeError("script path escapes deploy dir") from exc
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(candidate)
    # Prefer a directory that also has sibling pipeline stages (complete tree).
    for candidate in found:
        parent = candidate.parent
        if (parent / "01-preflight.sh").is_file() and (parent / "10-host-firewall.sh").is_file():
            return candidate
    if found:
        return found[0]

    hint = ""
    try:
        if DEPLOY_DIR.is_symlink():
            target = os.readlink(DEPLOY_DIR)
            if target.startswith("/home/") or "/home/" in f"/{target}":
                hint = (
                    f" {DEPLOY_DIR} is a symlink to {target}; "
                    "kin-mail-privhelperd runs with ProtectHome=true so /home is invisible. "
                    "Run console/bootstrap.sh to install a real tree under /opt/kin-mail-deploy "
                    "(do not symlink into /home)."
                )
    except OSError:
        pass
    raise FileNotFoundError(
        f"{label} not found under {DEPLOY_DIR} (tried {', '.join(candidates)}).{hint}"
    )


def resolve_hardening() -> Path:
    return resolve_under_deploy(HARDENING_CANDIDATES, "09-hardening.sh")


def resolve_kin_mail() -> Path:
    return resolve_under_deploy(KIN_MAIL_CANDIDATES, "kin-mail.sh")


def resolve_firewall() -> Path:
    return resolve_under_deploy(FIREWALL_CANDIDATES, "10-host-firewall.sh")


def resolve_prepare_os() -> Path:
    return resolve_under_deploy(PREPARE_OS_CANDIDATES, "02-prepare-os.sh")


def resolve_grow_disk() -> Path:
    return resolve_under_deploy(GROW_DISK_CANDIDATES, "grow-disk.sh")


def resolve_mail_gateway() -> Path:
    return resolve_under_deploy(MAIL_GATEWAY_CANDIDATES, "mail-gateway.sh")


def resolve_create_mailbox() -> Path:
    return resolve_under_deploy(CREATE_MAILBOX_CANDIDATES, "08-create-mailbox.sh")


def _mail_domain_from_config() -> str:
    """Read MAIL_DOMAIN from appliance config (root-only; same source as 08)."""
    conf = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
    if not conf.is_file():
        raise RuntimeError(f"missing {conf}")
    from kin_privhelper.apply_config import parse_config

    values = parse_config(conf.read_text(encoding="utf-8"))
    domain = str(values.get("MAIL_DOMAIN") or "").strip()
    if not domain:
        raise RuntimeError("MAIL_DOMAIN is empty in /etc/kin-mail/config")
    return domain


_LOCAL_PART_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._+-]{0,62})$")


def validate_create_mailbox_args(args: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (op, argv_tail for 08-create-mailbox.sh). Password never logged here."""
    op = str(args.get("op") or "create").strip().lower()
    if op == "status":
        return "status", ["--status"]
    if op == "list":
        return "list", ["--list"]
    if op == "delete":
        email = str(args.get("email") or "").strip().lower()
        if "@" not in email:
            raise ValueError("email is required to delete a mailbox")
        return "delete", ["--delete", email]
    if op == "rename":
        email = str(args.get("email") or "").strip().lower()
        new_local = str(args.get("new_local_part") or "").strip().lower()
        if "@" not in email:
            raise ValueError("email is required to rename a mailbox")
        if not new_local or "@" in new_local or not _LOCAL_PART_RE.match(new_local):
            raise ValueError("new_local_part must be a valid local part")
        return "rename", ["--rename", email, new_local]
    if op != "create":
        raise ValueError("create_mailbox op must be create, status, list, delete, or rename")

    local_part = str(args.get("local_part") or "").strip().lower()
    password = str(args.get("password") or "")
    display_name = str(args.get("display_name") or "").strip()
    given_name = str(args.get("given_name") or "").strip()
    surname = str(args.get("surname") or "").strip()
    account_status = str(args.get("account_status") or "active").strip().lower()

    if not local_part or "@" in local_part:
        raise ValueError("local_part is required (mailbox name only; domain is fixed)")
    if not _LOCAL_PART_RE.match(local_part):
        raise ValueError(
            "local_part must start with alphanumeric and use only letters, digits, . _ + -"
        )
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 256:
        raise ValueError("password too long")
    for label, value in (
        ("display_name", display_name),
        ("given_name", given_name),
        ("surname", surname),
    ):
        if len(value) > 128:
            raise ValueError(f"{label} too long")
        if any(ord(c) < 32 for c in value):
            raise ValueError(f"{label} contains invalid characters")
    if account_status not in ("active", "locked"):
        raise ValueError("account_status must be active or locked")

    domain = _mail_domain_from_config()
    email = f"{local_part}@{domain}"
    argv = [email, password]
    if display_name:
        argv.append(display_name)
    if given_name:
        argv.extend(["--given", given_name])
    if surname:
        argv.extend(["--sn", surname])
    if account_status != "active":
        argv.extend(["--account-status", account_status])
    return "create", argv


def _redact_secrets(text: str, secrets: list[str] | None) -> str:
    """Replace known secrets before a line is logged or yielded. Empty values ignored."""
    if not secrets:
        return text
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


async def _watch_log_file(
    path: Path,
    queue: asyncio.Queue[tuple[str, str] | None],
    stop: asyncio.Event,
    *,
    poll_s: float = 0.2,
    announce: bool = True,
    event_kind: str = "stdout",
    start_at: int | None = None,
) -> None:
    """Follow new bytes on path (like tail -F) onto the SSE queue.

    Starts at EOF so a leftover log from a prior run is not dumped. In-place
    truncate (03's `: > $LOG`) resets to offset 0. If truncate and the first
    new writes happen between polls, the file head no longer matches - restart
    from offset 0 rather than seeking into the middle of zmsetup output. A
    replaced inode (perl -i scrub) is treated as already-seen (new EOF) so the
    wizard does not replay the whole apply phase.
    """
    pos = 0
    inode: int | None = None
    started = False
    buf = ""
    announced = False
    existed_at_start = path.is_file()
    head = b""

    def _head() -> bytes:
        try:
            with path.open("rb") as fh:
                return fh.read(128)
        except OSError:
            return b""

    async def _read_available() -> None:
        nonlocal pos, inode, started, buf, announced, head
        try:
            st = path.stat()
        except OSError:
            return
        cur_head = _head()
        if not started:
            inode = st.st_ino
            started = True
            if start_at is not None:
                pos = max(0, start_at)
            else:
                pos = st.st_size if existed_at_start else 0
            head = cur_head
        elif st.st_ino != inode:
            # perl -i scrub replaces the inode with the already-streamed text.
            inode = st.st_ino
            pos = st.st_size
            buf = ""
            head = cur_head
            return
        elif st.st_size < pos:
            pos = 0
            buf = ""
            head = cur_head
        elif (
            head
            and cur_head
            and not cur_head.startswith(head[: len(cur_head)])
            and not head.startswith(cur_head)
        ):
            # Truncate+rewrite in one poll (size never sampled at 0). GNU tail
            # misses this too; we cannot seek(old_pos) into a new log.
            pos = 0
            buf = ""
            head = cur_head
        elif len(cur_head) >= len(head):
            head = cur_head[:128]
        if st.st_size <= pos:
            return
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if not announced:
                announced = True
                if announce:
                    await queue.put((event_kind, f"=== {path} (redacted installer output) ===\n"))
            await queue.put((event_kind, line + "\n"))

    while True:
        await _read_available()
        if stop.is_set():
            await _read_available()
            if buf:
                if not announced:
                    announced = True
                    if announce:
                        await queue.put((event_kind, f"=== {path} (redacted installer output) ===\n"))
                await queue.put((event_kind, buf if buf.endswith("\n") else buf + "\n"))
                buf = ""
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.05, poll_s))
        # Must stay asyncio.TimeoutError, not the builtin. The two are aliases
        # only on Python 3.11+, which is what CI runs; the appliance is Ubuntu
        # 22.04 on 3.10, where they are distinct and a bare "except
        # TimeoutError" catches nothing. That let this poll loop die on the
        # first idle tick, so a sidecar log the parent never echoes (zmsetup)
        # stopped tailing and the operator watched a silent deploy.
        except asyncio.TimeoutError:
            continue


async def _stream_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    transcript: Path | None = None,
    transcript_reset: bool = False,
    secrets: list[str] | None = None,
    follow_logs: list[Path] | None = None,
    follow_poll_s: float = 0.2,
    file_backed: bool = False,
    stdin_text: str | None = None,
    line_filter: Callable[[str], str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "PYTHONUNBUFFERED": "1"}
    if extra_env:
        env.update(extra_env)

    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
    log_fh = None
    if transcript is not None:
        try:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            log_fh = transcript.open("w" if transcript_reset else "a", encoding="utf-8")
            from .log_format import format_command_banner

            header_cmd = _redact_secrets(
                format_command_banner(argv, datetime.now(timezone.utc).isoformat()),
                secrets,
            )
            log_fh.write(f"\n{header_cmd}\n")
            log_fh.flush()
            try:
                os.chmod(transcript, 0o640)
                os.chown(transcript, -1, grp.getgrnam("kin-console").gr_gid)
            except (OSError, KeyError):
                pass
        except OSError:
            log_fh = None

    def _tee(text: str) -> None:
        if log_fh is None:
            return
        try:
            log_fh.write(_redact_secrets(text, secrets))
            log_fh.flush()
        except OSError:
            pass

    use_file = bool(file_backed and transcript is not None)
    tail_from: int | None = None
    if use_file and transcript is not None:
        try:
            tail_from = transcript.stat().st_size
        except OSError:
            tail_from = 0
    stdin = asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
    if use_file:
        raw_out = os.open(str(transcript), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=stdin,
                stdout=raw_out,
                stderr=raw_out,
                cwd=str(cwd) if cwd else None,
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(raw_out)
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
        assert proc.stdout is not None and proc.stderr is not None

    if stdin_text is not None and proc.stdin is not None:
        blob = stdin_text if stdin_text.endswith("\n") else stdin_text + "\n"
        proc.stdin.write(blob.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

    async def _pump(stream: asyncio.StreamReader, kind: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            await queue.put((kind, line.decode("utf-8", errors="replace")))

    pump_tasks: list[asyncio.Task[None]] = []
    if not use_file:
        assert proc.stdout is not None and proc.stderr is not None
        pump_tasks = [
            asyncio.create_task(_pump(proc.stdout, "stdout")),
            asyncio.create_task(_pump(proc.stderr, "stderr")),
        ]
    stop_follow = asyncio.Event()
    follow_tasks = [
        asyncio.create_task(
            _watch_log_file(
                path,
                queue,
                stop_follow,
                poll_s=follow_poll_s,
                announce=not use_file,
                event_kind="tee_only" if use_file else "stdout",
            )
        )
        for path in (follow_logs or [])
        if path
    ]
    if use_file and transcript is not None:
        # Child stdout is the transcript. Tail it for SSE; do not tee (already on disk).
        follow_tasks.append(
            asyncio.create_task(
                _watch_log_file(
                    transcript,
                    queue,
                    stop_follow,
                    poll_s=follow_poll_s,
                    announce=False,
                    start_at=tail_from,
                )
            )
        )

    async def _waiter() -> None:
        if pump_tasks:
            await asyncio.gather(*pump_tasks)
        else:
            await proc.wait()
        stop_follow.set()
        if follow_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*follow_tasks), timeout=3)
            except asyncio.TimeoutError:
                for task in follow_tasks:
                    task.cancel()
                await asyncio.gather(*follow_tasks, return_exceptions=True)
        await queue.put(None)

    waiter = asyncio.create_task(_waiter())
    try:
        idle_after_exit = 0.0
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                if waiter.done():
                    break
                if proc.returncode is not None:
                    stop_follow.set()
                    idle_after_exit += 2.0
                    if idle_after_exit >= 6.0:
                        break
                continue
            idle_after_exit = 0.0
            if item is None:
                break
            kind, text = item
            text = _redact_secrets(text, secrets)
            if line_filter is not None:
                text = line_filter(text)
            if kind == "tee_only":
                _tee(text)
                continue
            if not use_file:
                _tee(text if kind == "stdout" else f"[stderr] {text}")
            if kind == "stdout":
                yield proto.event_stdout(text)
            else:
                yield proto.event_stderr(text)
    finally:
        stop_follow.set()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
        except asyncio.TimeoutError:
            waiter.cancel()
            try:
                await waiter
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            try:
                await queue.put(None)
            except Exception:  # noqa: BLE001
                pass
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass

    code = await proc.wait()
    yield proto.event_done(int(code or 0))


async def _run_capture(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        int(proc.returncode or 0),
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


async def cmd_get_status() -> AsyncIterator[dict[str, Any]]:
    """Read-only cluster/service status report (fixed command, no client input)."""
    yield proto.event_stdout("=== KIN Mail status (privhelper get_status) ===\n")

    checks: list[tuple[str, list[str]]] = [
        ("date", ["date", "-Is"]),
        ("hostname", ["hostname", "-f"]),
        ("kin-mail-console", ["systemctl", "is-active", "kin-mail-console.service"]),
        ("kin-mail-privhelperd", ["systemctl", "is-active", "kin-mail-privhelperd.service"]),
        ("pcs resources", ["pcs", "status", "resources"]),
        ("drbd", ["drbdadm", "status"]),
    ]

    overall_ok = True
    for title, argv in checks:
        yield proto.event_stdout(f"\n--- {title} ---\n")
        try:
            code, out, err = await _run_capture(argv)
        except FileNotFoundError:
            overall_ok = False
            yield proto.event_stderr(f"command not found: {argv[0]}\n")
            continue
        if out:
            yield proto.event_stdout(out if out.endswith("\n") else out + "\n")
        if err:
            yield proto.event_stderr(err if err.endswith("\n") else err + "\n")
        if code != 0:
            overall_ok = False
            yield proto.event_stdout(f"(exit {code})\n")

    # Local HTTPS probes only - no lab IPs hardcoded; VIP may or may not be on this host.
    yield proto.event_stdout("\n--- https probe ---\n")
    console_port = os.environ.get("CONSOLE_PORT", "9443")
    for label, argv in (
        (
            "https://127.0.0.1/",
            ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "https://127.0.0.1/"],
        ),
        (
            "console /api/health",
            [
                "curl",
                "-sk",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--max-time",
                "5",
                f"https://127.0.0.1:{console_port}/api/health",
            ],
        ),
    ):
        try:
            code, out, err = await _run_capture(argv)
            body = (out or "").strip() or "????"
            yield proto.event_stdout(f"{label} → HTTP {body} (exit {code})\n")
            if label.startswith("console") and body != "200":
                overall_ok = False
        except FileNotFoundError:
            yield proto.event_stderr("curl not found\n")
            overall_ok = False

    yield proto.event_stdout("\n=== end get_status ===\n")
    yield proto.event_done(0 if overall_ok else 1)


async def cmd_run_hardening_status() -> AsyncIterator[dict[str, Any]]:
    """Stream `09-hardening.sh --status` (STATUS_ONLY=1 - read-only show_status + exit)."""
    script = resolve_hardening()
    yield proto.event_stdout(f"Running fixed script: {script} --status\n")
    # Fixed argv only - --status is not client-supplied. stdbuf -oL for true line streaming
    # through the pipe (bash would otherwise block-buffer when stdout is not a TTY).
    argv = [str(script), "--status"]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(argv, cwd=script.parent):
        yield ev


async def cmd_run_hardening() -> AsyncIterator[dict[str, Any]]:
    """Stream full `09-hardening.sh` (idempotent Part A - no client argv)."""
    script = resolve_hardening()
    yield proto.event_stdout(f"Running fixed script: {script} (full Part A)\n")
    argv = [str(script)]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(argv, cwd=script.parent):
        yield ev


async def cmd_apply_wizard_draft() -> AsyncIterator[dict[str, Any]]:
    """Merge draft into /etc/kin-mail/config after backup + validation (file write only)."""
    from . import apply_config

    # Run sync work in a thread so the event loop can still stream/busy-gate.
    code, lines = await asyncio.to_thread(apply_config.apply_wizard_draft)
    # Persist before streaming. The Deploy button treats a non-zero exit as
    # "Could not save settings, Deploy stopped before install". If the SSE
    # drops, the banner is all the operator sees unless the reason is also
    # in deploy-last.log for the next hydrate.
    try:
        DEPLOY_LAST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEPLOY_LAST_LOG.open("w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.chmod(DEPLOY_LAST_LOG, 0o640)
    except OSError:
        pass
    for line in lines:
        yield proto.event_stdout(line)
    yield proto.event_done(int(code))


def _deploy_line(text: str) -> dict[str, Any]:
    """Yield a line to the stream and persist it in the deploy transcript.

    Anything emitted after _stream_subprocess has finished reaches only
    whoever is still listening. The console parses the transcript later, and
    on a reload or in a second tab that is the only record there is, so a
    marker that is not written there does not exist as far as the UI is
    concerned.
    """
    line = text if text.endswith("\n") else text + "\n"
    try:
        DEPLOY_LAST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEPLOY_LAST_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    return proto.event_stdout(line)


async def cmd_run_full_install() -> AsyncIterator[dict[str, Any]]:
    """Stream kin-mail.sh --full-install with KIN_CONSOLE_CONFIRMED=1 (dead-man still armed)."""
    from .maintenance import gather_status

    st = await gather_status()
    if st.get("standby"):
        yield proto.event_stderr(
            "Refusing full install while a node is in maintenance "
            f"(standby={st['standby']}). Exit maintenance first.\n"
        )
        yield proto.event_done(1)
        return
    if is_full_install_complete():
        yield proto.event_stderr(
            "Refusing: mail is already installed on this host "
            "(/etc/kin-mail/setup-complete). Do not run Deploy again. "
            "On a 2-server pair use Build HA pair instead.\n"
        )
        yield proto.event_done(1)
        return
    script = resolve_kin_mail()
    header = (
        f"Running fixed script: {script} --full-install "
        f"(KIN_CONSOLE_CONFIRMED=1; dead-man NOT auto-cancelled)\n"
        f"Following redacted installer log: {ZIMBRA_INSTALL_LOG}\n"
    )
    yield proto.event_stdout(header)
    # Persist for View logs in a separate browser tab / after the run ends.
    try:
        DEPLOY_LAST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEPLOY_LAST_LOG.open("w", encoding="utf-8") as fh:
            fh.write(header)
        os.chmod(DEPLOY_LAST_LOG, 0o640)
    except OSError:
        pass
    argv = [str(script), "--full-install"]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    mark_full_install_started()
    # Held back until the additive metrics tail below has run, so the console
    # does not call the deploy finished and then keep streaming.
    install_exit = 1
    try:
        async for ev in _stream_subprocess(
            argv,
            cwd=script.parent,
            extra_env={"KIN_CONSOLE_CONFIRMED": "1"},
            transcript=DEPLOY_LAST_LOG,
            transcript_reset=False,
            follow_logs=[ZIMBRA_INSTALL_LOG],
            file_backed=True,
        ):
            if ev.get("type") == "done":
                raw = ev.get("exit_code")
                install_exit = 1 if raw is None else int(raw)
                continue
            yield ev
    finally:
        if full_install_in_progress():
            # Child survived this privhelperd process (KillMode=process). Leave
            # the marker so a later start/log fetch can stamp FAIL if it dies.
            pass
        else:
            append_unexpected_stop_if_needed()
            mark_full_install_finished()

    # Metrics are what fill the Monitoring and Reports tabs. Build HA pair
    # installs them as its own step; a single-server appliance had nothing that
    # ever did, so both tabs stayed empty for the life of the box (live QA,
    # 7 Sep 2026).
    #
    # DETACHED on purpose. The first version ran it inline here, and inline is
    # inside the generator that feeds the browser's SSE stream: when that
    # connection dropped during the long package upgrade, the generator was
    # closed, GeneratorExit was thrown in at the streaming loop, and every line
    # after it never ran. A deploy on 8 Sep 2026 finished perfectly and still
    # had no metrics for exactly that reason, and the operator had watched the
    # log drop and reconnect without knowing what it had cost. A task belongs
    # to the event loop rather than to this generator, so it survives the
    # client going away.
    if install_exit == 0:
        try:
            from .monitoring_install import run_after_install

            started = run_after_install()
            yield proto.event_stdout(
                "Installing built-in monitoring in the background "
                f"({started}). It keeps running if this page goes away; the "
                "Monitoring tab shows it when it lands, and has an Install "
                "monitoring button if it does not.\n"
            )
            if started != "started":
                # It never got off the ground. Say so in the transcript's own
                # vocabulary: the console reads KIN_METRICS_END to decide
                # whether this deploy has monitoring, and a run that emits no
                # marker at all is indistinguishable from an old transcript,
                # which is how "no monitoring" reads as "nothing to report".
                yield proto.event_stderr(
                    f"Built-in monitoring did not start ({started}). Mail is "
                    "unaffected. Use Install monitoring on the Monitoring "
                    "tab.\n"
                )
                yield _deploy_line("KIN_METRICS_END exit=1 reason=not-started")
        except Exception as exc:  # noqa: BLE001 - never fail a good deploy
            yield proto.event_stderr(
                f"Monitoring install could not be started: {exc}. Mail is "
                "unaffected; install it from the Monitoring tab.\n"
            )
            # Same reason as above: a deploy that tried and could not must not
            # look like a deploy that was never expected to.
            yield _deploy_line("KIN_METRICS_END exit=1 reason=not-started")
    yield proto.event_done(install_exit)


# The two filesystems an appliance runs out of, and the only two this command
# will look at. The console sends a name, never a path: a request that can name
# an arbitrary mountpoint is a request that can be aimed at the wrong disk.
GROW_TARGETS = {
    "system": "/",
    "mail": os.environ.get("KIN_ZIMBRA_DIR", "/opt/zimbra"),
}


def _appliance_config() -> dict[str, str]:
    path = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _local_ipv4s() -> set[str]:
    override = os.environ.get("KIN_LOCAL_IPV4S", "")
    if override.strip():
        return set(override.split())
    addrs: set[str] = set()
    try:
        import subprocess

        completed = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                addrs.add(parts[3].split("/", 1)[0])
    except OSError:
        pass
    return addrs


def mail_store_is_elsewhere() -> str | None:
    """On a split, the mail disk lives on the mailbox node, not on this one.

    The console runs on the edge. Growing /opt/zimbra here would enlarge the
    MTA tree, which holds no mail, while the store on the other machine stays
    full. Refuse rather than look successful.
    """
    cfg = _appliance_config()
    if cfg.get("TOPOLOGY") != "split":
        return None
    mailbox_ip = cfg.get("MAILBOX_IP") or ""
    mailbox_host = cfg.get("MAILBOX_HOST") or "the mailbox"
    if mailbox_ip and mailbox_ip in _local_ipv4s():
        return None
    where = f"{mailbox_host} ({mailbox_ip})" if mailbox_ip else mailbox_host
    return (
        f"The mail store is on {where}. Growing /opt/zimbra on this machine "
        "would enlarge the edge, which holds no mail. Run Extend a disk on "
        "the mailbox node."
    )


# The known-hosts file privhelperd is allowed to write. ProtectHome=true puts
# /root out of reach, so ssh cannot use its default location; the orchestrator
# already uses this one, so both paths trust the same host key.
MAILBOX_KNOWN_HOSTS = "/etc/kin-mail/split-known-hosts"
# Where the pushed copy lands before it is installed root-owned.
STAGED_GROW_DISK = "/tmp/kin-grow-disk.staged.sh"


def _mailbox_ssh_options() -> list[str]:
    """The option set the orchestrator proved during the build.

    One spelling, used by both ssh and scp. Two spellings drift, and the one
    that drifts is the one nobody runs until the day it matters - the
    known-hosts file especially: privhelperd runs with ProtectHome=true, so
    /root is not writable and ssh has nowhere to put a host key unless it is
    told otherwise.
    """
    return [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"UserKnownHostsFile={MAILBOX_KNOWN_HOSTS}",
    ]


def mailbox_ssh_argv(user: str, ip: str, remote: str) -> list[str]:
    """Run a fixed command as root on the mailbox."""
    return [
        "sshpass",
        "-e",
        "ssh",
        *_mailbox_ssh_options(),
        f"{user}@{ip}",
        # One root shell for the whole command. `sudo -S -p '' a && b` elevates
        # only a, because the shell splits on && before sudo ever sees it.
        f"sudo -S -p '' bash -c {shlex.quote(remote)}",
    ]


def mailbox_scp_argv(user: str, ip: str, local: str, remote_path: str) -> list[str]:
    """Copy one file to the mailbox, with the same options."""
    return [
        "sshpass",
        "-e",
        "scp",
        *_mailbox_ssh_options(),
        local,
        f"{user}@{ip}:{remote_path}",
    ]


def _mailbox_ssh_password(cfg: dict[str, str]) -> str:
    """The password for the mailbox account.

    MAILBOX_SSH_PASS is what the operator typed on the Topology step, and
    02-prepare-os then sets that account's password to KIN_USER_PASS - so on a
    built deployment the second one is the live value and the first is the one
    that got it in the door. Trying the stored pair in that order is what the
    orchestrator does; doing anything else here would work on a fresh build and
    fail on every deployment that has finished.
    """
    return (cfg.get("KIN_USER_PASS") or cfg.get("MAILBOX_SSH_PASS") or "").strip()


async def _run_quiet(argv: list[str], env: dict[str, str]) -> int:
    """Run a command, discard its output, return its exit code.

    For the copy step only. Its stdout is scp noise and its stderr can echo
    the target path; what the operator needs is the result, and the caller
    says what failed.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, **env},
    )
    return await proc.wait()


async def _grow_disk_on_mailbox(
    cfg: dict[str, str], op: str, reclaim: bool, script: Path, mountpoint: str
) -> AsyncIterator[dict[str, Any]]:
    """Run grow-disk.sh on the mailbox, over the channel the build already uses.

    The console lives on the edge and the operator never logs into the mailbox -
    that is the whole shape of a multi deployment. But the mail store is on the
    mailbox, so "Extend a disk" for mail could not reach the only disk that
    matters, and the refusal told the operator to go and run it on a machine
    they are not supposed to touch.

    Nothing about the decision moves here. grow-disk.sh runs on the mailbox,
    with the mailbox's own view of its disks, and every refusal it makes is made
    there. This carries the request and brings the output back.
    """
    from .orchestration import redact_text

    # Belt and braces. cmd_grow_disk already maps a NAME to one of two
    # mountpoints; this refuses anything else outright, so a future caller
    # cannot turn this into "run a path as root on the other machine".
    if mountpoint not in GROW_TARGETS.values():
        yield proto.event_stderr(f"refusing an unknown mountpoint: {mountpoint}\n")
        yield proto.event_done(2)
        return

    ip = (cfg.get("MAILBOX_IP") or "").strip()
    user = (cfg.get("MAILBOX_SSH_USER") or cfg.get("KIN_OS_USER") or "kin").strip()
    password = _mailbox_ssh_password(cfg)
    if not ip or not password:
        yield proto.event_stderr(
            "The mailbox address or its stored password is missing from "
            "/etc/kin-mail/config, so this node cannot reach it.\n"
        )
        yield proto.event_done(2)
        return
    if shutil.which("sshpass") is None:
        yield proto.event_stderr(
            "sshpass is not installed on this node, so the mailbox cannot be "
            "reached without a password prompt.\n"
        )
        yield proto.event_done(2)
        return

    # A fixed command, built here. Nothing from the browser reaches it: the
    # console sends the NAME of a target, this file maps that to a mountpoint,
    # and the only variable part left is a flag that is either present or not.
    #
    # The script is PUSHED first rather than the mailbox's own copy being run.
    #
    # That copy was left there by the last deploy, so it is whatever version
    # that was - and this is the code path that deletes a partition on the
    # machine holding every message. Version skew is not a risk worth carrying
    # here: the mailbox runs exactly the logic this console shipped with, and a
    # fix to the resize rules takes effect without a full rebuild. grow-disk.sh
    # sources nothing, so one file is the whole of it.
    remote_script = "/usr/local/lib/kin-grow-disk.sh"
    remote = f"{remote_script} {op} {mountpoint}"
    if reclaim:
        remote += " --reclaim-reserved"
    # Installed root-owned and root-only before it is run, so the account we
    # logged in as cannot swap the file between the copy and the execution.
    remote = (
        f"install -m 0700 -o root -g root {STAGED_GROW_DISK} {remote_script}"
        f" && {remote}"
    )

    argv = mailbox_ssh_argv(user, ip, remote)
    where = cfg.get("MAILBOX_HOST") or ip
    what = "mail store" if mountpoint == GROW_TARGETS["mail"] else "system disk"
    yield proto.event_stdout(f"Reading the {what} on {where}.\n")
    rc = await _run_quiet(
        mailbox_scp_argv(user, ip, str(script), STAGED_GROW_DISK),
        {"SSHPASS": password},
    )
    if rc != 0:
        yield proto.event_stderr(
            f"Could not copy the resize helper to {ip} (scp exit {rc}). "
            "Nothing on either machine has been changed.\n"
        )
        yield proto.event_done(2)
        return
    async for ev in _stream_subprocess(
        argv,
        # SSHPASS, never argv: a password on a command line is readable by
        # every process on the box for as long as the command runs.
        extra_env={"SSHPASS": password},
        secrets=[password],
        stdin_text=password + "\n",
        line_filter=lambda line: redact_text(line, [password]),
    ):
        yield ev


async def cmd_grow_disk(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """Grow a filesystem into space added to its disk by the hypervisor.

    Two operations. "plan" reads the disk and says what it would do, changing
    nothing; "apply" does it. The console always plans first and shows the
    operator that plan, because the honest way to offer a destructive button is
    to say what it will do before it does it.

    Everything dangerous is decided in grow-disk.sh, which refuses rather than
    guesses: it will not touch a partition that has another partition after it,
    will not act on replicated storage, will not shrink anything, and has no
    force flag. This wrapper adds the two things a shell script cannot: the
    maintenance lock, so a resize cannot overlap a deploy or a cluster
    operation, and the audit trail.
    """
    from .maintenance import release_maintenance_lock, try_lock_maintenance

    args = args or {}
    op = str(args.get("op") or "plan").strip().lower()
    target = str(args.get("target") or "").strip().lower()
    # Deleting the partition the installer reserved for a second server is the
    # only way the mail disk can ever grow, and it is still a deletion. It is
    # never implied: the console has to ask for it by name, and the operator
    # has to have been shown what it removes.
    reclaim = str(args.get("reclaim") or "").strip().lower() in ("1", "true", "yes")

    if op not in ("plan", "apply"):
        yield proto.event_stderr("op must be plan or apply\n")
        yield proto.event_done(2)
        return
    if target not in GROW_TARGETS:
        yield proto.event_stderr(
            "target must be system or mail (the console never sends a path)\n"
        )
        yield proto.event_done(2)
        return

    # On a multi deployment the mail store is on the mailbox, so the request
    # goes there. Refusing used to be the whole answer, and it told the operator
    # to run it on a machine this product exists to keep them out of - which
    # meant the one disk that actually fills up could not be grown from the
    # console at all.
    #
    # Two ways to end up there. An explicit node=mailbox, which is how the
    # console offers that machine's disks; and target=mail on a split, which
    # goes there whether asked or not, because /opt/zimbra on the edge is the
    # MTA tree and growing it would report success while the store stays full.
    node = str(args.get("node") or "").strip().lower()
    mailbox_cfg: dict[str, str] | None = None
    # node="this" means THIS machine, even for the mail target.
    #
    # The edge has its own /opt/zimbra - Zimbra's MTA and proxy, the queue and
    # the logs - on its own disk in this product's layout, and it can fill up
    # like any other. Routing every "mail" request to the mailbox left that
    # disk untouchable: the console's own Check never rescanned it, so 50 GB
    # added in the hypervisor was invisible to the guest for a day (live edge,
    # 20 Sep 2026). An explicit node is the operator saying which machine.
    #
    # With no node at all the old protective default stands: on a split, "mail"
    # means the mail store, which is on the mailbox. That keeps any caller that
    # predates the node parameter from quietly growing the wrong tree.
    if node == "this":
        pass
    elif node == "mailbox" or (target == "mail" and mail_store_is_elsewhere()):
        cfg = _appliance_config()
        if cfg.get("TOPOLOGY") != "split":
            yield proto.event_stderr(
                "This appliance is a single server; there is no mailbox node "
                "to grow a disk on.\n"
            )
            yield proto.event_done(2)
            return
        if (cfg.get("MAILBOX_IP") or "").strip():
            mailbox_cfg = cfg
        else:
            yield proto.event_stderr(
                (mail_store_is_elsewhere() or "The mailbox address is not "
                 "recorded in /etc/kin-mail/config, so it cannot be reached.")
                + "\n"
            )
            yield proto.event_done(2)
            return

    mountpoint = GROW_TARGETS[target]

    try:
        script = resolve_grow_disk()
    except (FileNotFoundError, RuntimeError) as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return

    # Planning is read-only, so it does not need the lock and must not be
    # blocked by an unrelated operation: an operator looking at what WOULD
    # happen while a deploy runs is exactly when they want to look.
    if op == "plan":
        if mailbox_cfg is not None:
            async for ev in _grow_disk_on_mailbox(
                mailbox_cfg, "plan", False, script, mountpoint
            ):
                yield ev
            return
        async for ev in _stream_subprocess([str(script), "plan", mountpoint]):
            yield ev
        return

    if reclaim:
        yield proto.event_stdout(
            "This will delete the small partition reserved for a future second "
            "server before growing the disk. It is unused on a single-server "
            "appliance, and the helper refuses if it turns out to contain "
            "anything at all.\n"
        )

    # Applying moves a partition boundary. It must never overlap a deploy, an
    # Add or Remove Host, or a metrics install that is mid-apt.
    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield proto.event_stderr(
            "Refusing: another operation is already running on this appliance. "
            "A resize must not overlap it. Try again when it finishes.\n"
        )
        yield proto.event_done(1)
        return
    try:
        yield proto.event_stdout(
            f"Growing {mountpoint} into unused space on its disk. "
            "Mail keeps running; nothing is unmounted.\n"
        )
        # The lock above is this node's. It is the right one to hold either way:
        # it is what stops a resize overlapping a deploy, and a deploy is driven
        # from here even when it acts on the mailbox.
        if mailbox_cfg is not None:
            async for ev in _grow_disk_on_mailbox(
                mailbox_cfg, "apply", reclaim, script, mountpoint
            ):
                yield ev
            return
        argv = [str(script), "apply", mountpoint]
        if reclaim:
            argv.append("--reclaim-reserved")
        async for ev in _stream_subprocess(
            argv,
            transcript=DEPLOY_LAST_LOG,
            transcript_reset=False,
        ):
            yield ev
    finally:
        # BaseException too: a closed browser tab throws GeneratorExit in here
        # and a lock left held blocks every later operation.
        release_maintenance_lock(lock_fh)


async def cmd_cancel_firewall_deadman() -> AsyncIterator[dict[str, Any]]:
    """Explicit operator step: cancel ufw dead-man after verification (keeps ufw on)."""
    script = resolve_firewall()
    yield proto.event_stdout(f"Running fixed script: {script} cancel-deadman\n")
    argv = [str(script), "cancel-deadman"]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(
        argv,
        cwd=script.parent,
        transcript=DEPLOY_LAST_LOG,
        transcript_reset=False,
    ):
        yield ev


async def cmd_get_deploy_log(_args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """Read-only last deploy/install transcript for the console log viewer."""
    reclaim_stale_full_install_marker()
    yield proto.event_stdout(f"=== last deploy log: {DEPLOY_LAST_LOG} ===\n")
    if not DEPLOY_LAST_LOG.is_file():
        yield proto.event_stdout(
            "(no deploy log yet - start Deploy from the wizard, then reopen View logs)\n"
        )
        yield proto.event_done(0)
        return

    def _read() -> str:
        return DEPLOY_LAST_LOG.read_text(encoding="utf-8", errors="replace")

    try:
        text = await asyncio.to_thread(_read)
    except OSError as exc:
        yield proto.event_stderr(f"cannot read deploy log: {exc}\n")
        yield proto.event_done(1)
        return
    if not text.strip():
        yield proto.event_stdout("(deploy log is empty)\n")
    else:
        # Stream in chunks so huge installs do not become one giant SSE frame.
        chunk = 16_384
        for i in range(0, len(text), chunk):
            yield proto.event_stdout(text[i : i + chunk])
    yield proto.event_done(0)


async def cmd_get_audit_log(_args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """Read-only tail of privhelper audit log (Super Admin only via RBAC)."""
    log_path = Path(os.environ.get("PRIVHELPER_LOG", "/var/log/kin-mail/privhelper.log"))
    lines_n = 200
    try:
        lines_n = max(1, min(1000, int(os.environ.get("PRIVHELPER_AUDIT_TAIL", "200"))))
    except ValueError:
        lines_n = 200

    yield proto.event_stdout(f"=== privhelper audit (last {lines_n} lines): {log_path} ===\n")
    if not log_path.is_file():
        yield proto.event_stdout("(log file missing)\n")
        yield proto.event_done(0)
        return

    def _tail() -> list[str]:
        # Efficient-enough tail for typical audit logs.
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines_n:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
            text = data.decode("utf-8", errors="replace")
            parts = text.splitlines()
            return parts[-lines_n:]

    try:
        for line in await asyncio.to_thread(_tail):
            yield proto.event_stdout(line + "\n")
    except OSError as exc:
        yield proto.event_stderr(f"cannot read audit log: {exc}\n")
        yield proto.event_done(1)
        return
    yield proto.event_done(0)


async def cmd_create_mailbox(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """Quota-gated mailbox create via install/08-create-mailbox.sh (same gate as CLI)."""
    args = args or {}
    op_preview = str(args.get("op") or "create").strip().lower()
    if op_preview in ("create",):
        from .deploy_state import read_license_token, read_server_id, ensure_server_id
        from kin_console.license import license_view

        token = read_license_token()
        if token:
            state = license_view(token, read_server_id() or ensure_server_id())
            if state.get("provisioning_blocked"):
                yield proto.event_stderr(
                    "New mailboxes are blocked while the license is invalid, in grace, or expired. "
                    "Existing mail still flows.\n"
                )
                yield proto.event_done(1)
                return
    try:
        op, argv_tail = validate_create_mailbox_args(args)
    except ValueError as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return
    except RuntimeError as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(1)
        return

    script = resolve_create_mailbox()
    if op == "status":
        yield proto.event_stdout(f"Running fixed script: {script} --status\n")
    elif op == "list":
        yield proto.event_stdout(f"Running fixed script: {script} --list\n")
    elif op in ("delete", "rename"):
        yield proto.event_stdout(f"Running fixed script: {script} {op} {argv_tail[1]}\n")
    else:
        # Never print password. Email is always the first positional.
        email = argv_tail[0] if argv_tail else ""
        yield proto.event_stdout(
            f"Running fixed script: {script} {email} <password-redacted>\n"
        )
        yield proto.event_stdout(
            "# Uses kin_quota_gate_allow_new_mailbox before zmprov ca (same as CLI).\n"
        )

    argv = [str(script), *argv_tail]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(argv, cwd=script.parent):
        yield ev


async def cmd_clear_initial_console_password(
    _args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Remove root-only first-boot password file after successful local login/rotate.

    Never reads or echoes the password. Idempotent when the file is already gone.
    """
    from .initial_password import INITIAL_PASSWORD_FILE, clear_initial_password_file

    try:
        removed = clear_initial_password_file()
    except OSError as exc:
        yield proto.event_stderr(f"cannot remove {INITIAL_PASSWORD_FILE}: {exc}\n")
        yield proto.event_done(1)
        return
    if removed:
        yield proto.event_stdout(f"removed {INITIAL_PASSWORD_FILE}\n")
    else:
        yield proto.event_stdout(f"{INITIAL_PASSWORD_FILE} already absent\n")
    yield proto.event_done(0)


async def cmd_run_ha_orchestration(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .orchestration import cmd_run_ha_orchestration as _run

    args = args or {}
    join_mode = str(args.get("join_mode") or "apply").strip().lower() or "apply"
    mark_ha_orchestration_started()
    saw_orch_failed = False
    try:
        async for ev in _run(args):
            data = str(ev.get("data") or "")
            if "ORCH_FAILED" in data:
                saw_orch_failed = True
            if ev.get("type") == "done":
                try:
                    code = int(ev.get("exit_code") or 0)
                except (TypeError, ValueError):
                    code = 1
                if code != 0 and not saw_orch_failed:
                    line = f"ORCH_FAILED step=preflight join_mode={join_mode}\n"
                    try:
                        with DEPLOY_LAST_LOG.open("a", encoding="utf-8") as fh:
                            fh.write(line)
                    except OSError:
                        pass
                    yield proto.event_stderr(line)
            yield ev
    finally:
        mark_ha_orchestration_finished()


async def cmd_remove_host(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    from .remove_host import cmd_remove_host as _run

    async for ev in _run(args):
        yield ev


async def cmd_add_host(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    from .add_host import cmd_add_host as _run

    async for ev in _run(args):
        yield ev


async def cmd_remove_observability(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .observability_ops import cmd_remove_observability as _run

    async for ev in _run(args):
        yield ev


async def cmd_add_observability(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .observability_ops import cmd_add_observability as _run

    async for ev in _run(args):
        yield ev


async def cmd_install_monitoring(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .monitoring_install import cmd_install_monitoring as _run

    async for ev in _run(args):
        yield ev


async def cmd_store_observability_secrets(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .observability_secrets import store_observability_secrets

    args = args or {}
    try:
        meta = await asyncio.to_thread(
            store_observability_secrets,
            {
                "ip": str(args.get("ip") or ""),
                "hostname": str(args.get("hostname") or ""),
                "root_pass": str(args.get("root_pass") or ""),
                "kin_user_pass": str(args.get("kin_user_pass") or ""),
            },
        )
    except ValueError as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return
    except Exception as exc:  # noqa: BLE001
        yield proto.event_stderr(f"cannot store observability secrets: {exc}\n")
        yield proto.event_done(1)
        return
    yield proto.event_stdout(
        f"observability secrets stored (encrypted); ip={meta.get('ip')}\n"
    )
    yield proto.event_done(0)


async def cmd_maintenance(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    from .maintenance import cmd_maintenance as _run

    async for ev in _run(args):
        yield ev


async def cmd_store_provisioning_secrets(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Encrypt host root + kin passwords at rest. Never echo values."""
    from . import provisioning_secrets as vault

    args = args or {}
    updates = {
        "host_root_pass": str(args.get("host_root_pass") or ""),
        "kin_user_pass": str(args.get("kin_user_pass") or ""),
    }
    try:
        meta = await asyncio.to_thread(vault.store_secrets, updates)
    except ValueError as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return
    except Exception as exc:  # noqa: BLE001
        yield proto.event_stderr(f"cannot store provisioning secrets: {exc}\n")
        yield proto.event_done(1)
        return
    yield proto.event_stdout(
        f"provisioning secrets stored (encrypted); fields set; updated_at={meta.get('updated_at')}\n"
    )
    yield proto.event_done(0)


async def cmd_ha_disk_preflight(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Read-only DRBD disk layout check (local + wizard peer). Never partitions."""
    from .ha_disk import (
        REMOTE_PROBE,
        collect_local_facts,
        combine_results,
        evaluate_facts,
        parse_remote_probe,
    )
    from .orchestration import (
        _load_config,
        _load_draft,
        _ssh_run,
        redact_text,
        resolve_topology,
        ssh_password_candidates,
    )
    from .provisioning_secrets import load_secrets

    del args
    nodes: list[dict[str, Any]] = []
    local_res = evaluate_facts(
        collect_local_facts(),
        label="this server",
        require_zimbra_on_data=True,
    )
    nodes.append(local_res)
    for line in local_res.get("errors") or []:
        yield proto.event_stderr(f"{line}\n")
    auto = local_res.get("will_auto_partition")
    if isinstance(auto, dict) and auto.get("message"):
        yield proto.event_stdout(f"{auto['message']}\n")
    if local_res.get("ok"):
        yield proto.event_stdout(
            f"this server: {local_res.get('data_disk')} + {local_res.get('meta_disk')} ok\n"
        )

    try:
        draft = _load_draft()
        config = _load_config()
        topology = str(draft.get("topology") or config.get("TOPOLOGY") or "").strip()
    except Exception:  # noqa: BLE001
        topology = ""
        draft, config = {}, {}

    if topology in ("2vm", "2"):
        secrets_map = load_secrets()
        root_pass = secrets_map.get("host_root_pass") or ""
        kin_pass = secrets_map.get("kin_user_pass") or ""
        if not root_pass or not kin_pass:
            nodes.append(
                {
                    "ok": False,
                    "label": "second server",
                    "errors": [
                        "second server: store host credentials in the wizard to inspect its disks"
                    ],
                    "seen": [],
                    "build_allowed": False,
                }
            )
        else:
            try:
                _local, peer, _mon = resolve_topology(draft=draft, config=config)
            except Exception as exc:  # noqa: BLE001
                nodes.append(
                    {
                        "ok": False,
                        "label": "second server",
                        "errors": [f"second server: {exc}"],
                        "seen": [],
                        "build_allowed": False,
                    }
                )
            else:
                secrets = [root_pass, kin_pass]
                ssh_user = ""
                ssh_pass = ""
                for user, pwd in ssh_password_candidates(kin_pass, root_pass):
                    code, _ident = await _ssh_run(
                        peer, user, pwd, secrets, "hostname -f || hostname", timeout=10
                    )
                    if code == 0:
                        ssh_user, ssh_pass = user, pwd
                        break
                if not ssh_user:
                    nodes.append(
                        {
                            "ok": False,
                            "label": f"second server ({peer.name})",
                            "errors": [
                                f"second server ({peer.ip}): could not SSH with stored credentials"
                            ],
                            "seen": [],
                            "build_allowed": False,
                        }
                    )
                else:
                    code, blob = await _ssh_run(
                        peer, ssh_user, ssh_pass, secrets, REMOTE_PROBE, timeout=15
                    )
                    blob = redact_text(blob, secrets)
                    if code != 0:
                        nodes.append(
                            {
                                "ok": False,
                                "label": f"second server ({peer.name})",
                                "errors": [
                                    f"second server ({peer.name}): disk inspect failed "
                                    f"(ssh exit {code})"
                                ],
                                "seen": [],
                                "build_allowed": False,
                            }
                        )
                    else:
                        peer_res = evaluate_facts(
                            parse_remote_probe(blob),
                            label=f"second server ({peer.name})",
                            require_zimbra_on_data=True,
                        )
                        nodes.append(peer_res)
                        for line in peer_res.get("errors") or []:
                            yield proto.event_stderr(f"{line}\n")
                        auto_p = peer_res.get("will_auto_partition")
                        if isinstance(auto_p, dict) and auto_p.get("message"):
                            yield proto.event_stdout(f"{auto_p['message']}\n")
                        if peer_res.get("ok"):
                            yield proto.event_stdout(
                                f"second server: {peer_res.get('data_disk')} + "
                                f"{peer_res.get('meta_disk')} ok\n"
                            )

    result = combine_results(*nodes)
    yield proto.event_stdout("HA_DISK_JSON:" + json.dumps(result, separators=(",", ":")) + "\n")
    yield proto.event_done(0 if result.get("ok") or result.get("build_allowed") else 2)


async def cmd_mail_gateway(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """Manage the Proxmox Mail Gateway that sits in front of this appliance.

    Six operations reach the shell script and three never leave this process.

    The three local ones are about the credential: "connect" validates the
    operator's input and encrypts the API token into the vault, "forget" throws
    it away, and "trust_certificate" records the fingerprint of the gateway's
    self-signed certificate after a human has looked at it. None of them touch
    mail, so none of them take the maintenance lock.

    The other six run mail-gateway.sh. Reading operations - probe, plan, verify,
    status - never take the lock either: an operator asking what WOULD happen
    while a deploy is running is exactly when they want to ask. Apply and revert
    repoint a live Postfix at a different relay, so they do take it.

    The token secret is decrypted here, handed to one subprocess in its
    environment, and redacted from the transcript. It is never an argument, so
    it never appears in a process listing.
    """
    from . import mail_gateway as gw
    from .maintenance import release_maintenance_lock, try_lock_maintenance

    args = args or {}
    op = str(args.get("op") or "status").strip().lower()
    if op not in gw.ALL_OPS:
        yield proto.event_stderr(
            f"mail_gateway op must be one of {', '.join(gw.ALL_OPS)}, not {op!r}\n"
        )
        yield proto.event_done(2)
        return

    # ---- operations that never leave this process --------------------------
    if op == "status":
        # Answered here rather than by the script: status has to work when the
        # credential is missing, when the gateway is unreachable, and before
        # anything has ever been applied. Those are exactly the moments the
        # console most needs an answer, and all three would fail a script that
        # starts by authenticating.
        try:
            snapshot = await asyncio.to_thread(gw.status)
        except Exception as exc:  # noqa: BLE001
            yield proto.event_stderr(f"cannot read the gateway status: {exc}\n")
            yield proto.event_done(1)
            return
        yield proto.event_stdout(
            "KIN_GW_STATUS " + json.dumps(snapshot, separators=(",", ":")) + "\n"
        )
        yield proto.event_done(0)
        return

    if op == "forget":
        gw.forget_credentials()
        conf = gw.read_config()
        if conf.get("GATEWAY_HOST"):
            # A hand-edited config with a nonsense port must not turn "forget
            # the credential" into an unhandled exception, which would leave
            # the link enabled after the vault was already deleted.
            raw_port = conf.get("GATEWAY_API_PORT") or 8006
            gw.write_config(
                host=conf.get("GATEWAY_HOST", ""),
                api_port=int(raw_port) if gw.valid_port(raw_port) else 8006,
                auth=conf.get("GATEWAY_AUTH", "token"),
                token_id=conf.get("GATEWAY_TOKEN_ID", ""),
                user=conf.get("GATEWAY_USER", "root@pam"),
                cacert=conf.get("GATEWAY_CACERT", ""),
                enabled=False,
                public_host=conf.get("GATEWAY_PUBLIC_HOST", ""),
                public_ip=conf.get("GATEWAY_PUBLIC_IP", ""),
                greylist=conf.get("GATEWAY_GREYLIST") == "1",
            )
        yield proto.event_stdout(
            "Gateway credential deleted and the link disabled. The gateway itself is "
            "untouched, and this appliance still relays through it until you run revert.\n"
        )
        yield proto.event_done(0)
        return

    if op == "connect":
        host = str(args.get("host") or "").strip()
        auth = str(args.get("auth") or "ticket").strip().lower()
        token_id = str(args.get("token_id") or "").strip()
        user = str(args.get("user") or "root@pam").strip()
        cacert = str(args.get("cacert") or "").strip()
        api_port = args.get("api_port") or 8006
        public_host = str(args.get("public_host") or "").strip().rstrip(".")
        public_ip = str(args.get("public_ip") or "").strip()
        greylist = bool(args.get("greylist"))
        secret = str(args.get("token_secret") or "")
        password = str(args.get("password") or "")

        if not gw.valid_host(host):
            yield proto.event_stderr(
                "The gateway address has to be a hostname or an IP address.\n"
            )
            yield proto.event_done(2)
            return
        if not gw.valid_port(api_port):
            yield proto.event_stderr("The gateway API port has to be a port number.\n")
            yield proto.event_done(2)
            return
        if auth not in gw.AUTH_MODES:
            yield proto.event_stderr("Authentication has to be token or ticket.\n")
            yield proto.event_done(2)
            return
        if cacert and not Path(cacert).is_file():
            yield proto.event_stderr(f"No CA bundle at {cacert}.\n")
            yield proto.event_done(2)
            return
        # The public identity is what goes into DNS, and getting it wrong is
        # not a cosmetic error: an MX cannot hold an address, and a private
        # address in SPF authorises nobody.
        if not gw.valid_public_hostname(public_host):
            yield proto.event_stderr(
                "The gateway's public hostname has to be a name an MX record can "
                "point at, such as relay.example.com. An MX record cannot hold an "
                "IP address.\n"
            )
            yield proto.event_done(2)
            return
        if not gw.valid_public_ip(public_ip):
            yield proto.event_stderr(
                "The gateway's public address has to be the address the internet "
                "sees. A private address (10.x, 192.168.x, 172.16-31.x) published "
                "in SPF authorises nobody, and every outbound message would fail "
                "SPF.\n"
            )
            yield proto.event_done(2)
            return
        if auth == "token":
            if not gw.valid_token_id(token_id):
                yield proto.event_stderr(
                    "A PMG token id looks like root@pam!kinmail. Create one under "
                    "Configuration > User Management > API Tokens on the gateway.\n"
                )
                yield proto.event_done(2)
                return
            if not secret:
                yield proto.event_stderr("The API token secret is required.\n")
                yield proto.event_done(2)
                return
            creds = {"token_secret": secret}
        else:
            if not password:
                yield proto.event_stderr("A password is required for ticket authentication.\n")
                yield proto.event_done(2)
                return
            creds = {"password": password}

        try:
            await asyncio.to_thread(gw.store_credentials, creds)
            await asyncio.to_thread(
                gw.write_config,
                host=host,
                api_port=int(api_port),
                auth=auth,
                token_id=token_id,
                user=user,
                cacert=cacert,
                enabled=True,
                public_host=public_host,
                public_ip=public_ip,
                greylist=greylist,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            yield proto.event_stderr(f"cannot store the gateway credential: {exc}\n")
            yield proto.event_done(1)
            return
        yield proto.event_stdout(
            f"Gateway recorded: {host}:{int(api_port)} using {auth} authentication. "
            "The credential is encrypted; the console never sees it again.\n"
        )
        if public_host and public_ip:
            yield proto.event_stdout(
                f"Public identity: {public_host} at {public_ip}. DNS advice will "
                "use these, never the address above.\n"
            )
        else:
            yield proto.event_stdout(
                "No public identity recorded. The gateway will still be configured, "
                "but no DNS records can be printed: an MX cannot point at the local "
                "address this console uses to reach it.\n"
            )
        yield proto.event_stdout("Run Probe next to confirm the gateway answers.\n")
        yield proto.event_done(0)
        return

    if op == "trust_certificate":
        conf = gw.read_config()
        host = conf.get("GATEWAY_HOST", "")
        fingerprint = str(args.get("fingerprint") or "").strip().upper()
        if not host:
            yield proto.event_stderr("No gateway is configured yet.\n")
            yield proto.event_done(2)
            return
        # The fingerprint has to come back from the operator, not be taken from
        # whatever answers the socket. Confirming a value the machine just
        # invented is not confirmation.
        if not fingerprint or any(
            ch not in "0123456789ABCDEF:" for ch in fingerprint
        ):
            yield proto.event_stderr(
                "Pass the SHA-256 fingerprint exactly as Probe printed it.\n"
            )
            yield proto.event_done(2)
            return
        try:
            await asyncio.to_thread(gw.pin_fingerprint, host, fingerprint)
        except OSError as exc:
            yield proto.event_stderr(f"cannot record the fingerprint: {exc}\n")
            yield proto.event_done(1)
            return
        yield proto.event_stdout(
            f"Certificate confirmed for {host}. If it ever changes, every operation "
            "here stops until you confirm the new one.\n"
        )
        yield proto.event_done(0)
        return

    # ---- operations that run the script ------------------------------------
    try:
        script = resolve_mail_gateway()
    except (FileNotFoundError, RuntimeError) as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return

    try:
        creds = await asyncio.to_thread(gw.load_credentials)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        yield proto.event_stderr(f"cannot read the gateway credential: {exc}\n")
        yield proto.event_done(1)
        return

    extra_env: dict[str, str] = {}
    redact: list[str] = []
    if creds.get("token_secret"):
        extra_env["KIN_PMG_TOKEN_SECRET"] = creds["token_secret"]
        redact.append(creds["token_secret"])
    if creds.get("password"):
        extra_env["KIN_PMG_PASSWORD"] = creds["password"]
        redact.append(creds["password"])

    argv = [str(script), op]

    if op in gw.READ_ONLY_OPS:
        async for ev in _stream_subprocess(argv, extra_env=extra_env, secrets=redact):
            yield ev
        return

    # apply and revert repoint a live Postfix. They must not overlap a deploy,
    # an Add or Remove Host, a metrics install, or a disk resize.
    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield proto.event_stderr(
            "Refusing: another operation is already running on this appliance. "
            "Changing where mail is relayed must not overlap it. Try again when it finishes.\n"
        )
        yield proto.event_done(1)
        return
    try:
        async for ev in _stream_subprocess(
            argv,
            extra_env=extra_env,
            secrets=redact,
            transcript=DEPLOY_LAST_LOG,
            transcript_reset=False,
        ):
            yield ev
    finally:
        # BaseException too: a closed browser tab throws GeneratorExit in here
        # and a lock left held blocks every later operation.
        release_maintenance_lock(lock_fh)


CommandHandler = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]


def _adapt(fn: Callable[..., AsyncIterator[dict[str, Any]]]) -> CommandHandler:
    """Allow legacy zero-arg handlers and new args-aware handlers."""

    async def _wrapped(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            agen = fn(args)
        except TypeError:
            agen = fn()
        async for ev in agen:
            yield ev

    return _wrapped


from .appliance_settings import cmd_apply_appliance_settings
from .console_users_sync import cmd_mutate_console_users

HANDLERS: dict[str, CommandHandler] = {
    proto.CMD_GET_STATUS: _adapt(cmd_get_status),
    proto.CMD_RUN_HARDENING_STATUS: _adapt(cmd_run_hardening_status),
    proto.CMD_APPLY_WIZARD_DRAFT: _adapt(cmd_apply_wizard_draft),
    proto.CMD_RUN_HARDENING: _adapt(cmd_run_hardening),
    proto.CMD_RUN_FULL_INSTALL: _adapt(cmd_run_full_install),
    proto.CMD_INSTALL_MONITORING: _adapt(cmd_install_monitoring),
    proto.CMD_GROW_DISK: _adapt(cmd_grow_disk),
    proto.CMD_MAIL_GATEWAY: _adapt(cmd_mail_gateway),
    proto.CMD_CANCEL_FIREWALL_DEADMAN: _adapt(cmd_cancel_firewall_deadman),
    proto.CMD_GET_AUDIT_LOG: _adapt(cmd_get_audit_log),
    proto.CMD_GET_DEPLOY_LOG: _adapt(cmd_get_deploy_log),
    proto.CMD_CREATE_MAILBOX: _adapt(cmd_create_mailbox),
    proto.CMD_CLEAR_INITIAL_CONSOLE_PASSWORD: _adapt(cmd_clear_initial_console_password),
    proto.CMD_MAINTENANCE: _adapt(cmd_maintenance),
    proto.CMD_REMOVE_HOST: _adapt(cmd_remove_host),
    proto.CMD_ADD_HOST: _adapt(cmd_add_host),
    proto.CMD_REMOVE_OBSERVABILITY: _adapt(cmd_remove_observability),
    proto.CMD_ADD_OBSERVABILITY: _adapt(cmd_add_observability),
    proto.CMD_STORE_OBSERVABILITY_SECRETS: _adapt(cmd_store_observability_secrets),
    proto.CMD_STORE_PROVISIONING_SECRETS: _adapt(cmd_store_provisioning_secrets),
    proto.CMD_RUN_HA_ORCHESTRATION: _adapt(cmd_run_ha_orchestration),
    proto.CMD_HA_DISK_PREFLIGHT: _adapt(cmd_ha_disk_preflight),
    proto.CMD_MUTATE_CONSOLE_USERS: _adapt(cmd_mutate_console_users),
    proto.CMD_APPLY_APPLIANCE_SETTINGS: _adapt(cmd_apply_appliance_settings),
}


def get_handler(cmd: str) -> CommandHandler | None:
    return HANDLERS.get(cmd)
