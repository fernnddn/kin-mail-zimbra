"""Fixed whitelist command implementations - no client-supplied argv/shell."""

from __future__ import annotations

import asyncio
import json
import grp
import os
import re
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
        except TimeoutError:
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
    for line in lines:
        yield proto.event_stdout(line)
    yield proto.event_done(int(code))


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
    # 7 Sep 2026). Additive and last, on the same trade as the HA step: a
    # package hiccup here reports loudly and must never turn a working deploy
    # into a failed one.
    if install_exit == 0:
        try:
            from .monitoring_install import cmd_install_monitoring as _install_metrics

            async for ev in _install_metrics({}):
                if ev.get("type") == "done":
                    continue
                yield ev
        except Exception as exc:  # noqa: BLE001 - never fail a good deploy
            yield proto.event_stderr(
                f"Monitoring install could not run: {exc}. Mail is unaffected; "
                "install it later from the Monitoring tab.\n"
            )
    yield proto.event_done(install_exit)


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
