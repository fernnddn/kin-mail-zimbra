"""Fixed whitelist command implementations — no client-supplied argv/shell."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from . import protocol as proto

# Deploy tree on appliance (kin-mail.sh default). Override via env for lab clones.
DEPLOY_DIR = Path(os.environ.get("KIN_MAIL_DEPLOY_DIR", "/opt/kin-mail-deploy"))

# Basename only — resolved under DEPLOY_DIR; never take a path from the client.
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
    if op != "create":
        raise ValueError("create_mailbox op must be 'create' or 'status'")

    local_part = str(args.get("local_part") or "").strip().lower()
    password = str(args.get("password") or "")
    display_name = str(args.get("display_name") or "").strip()

    if not local_part or "@" in local_part:
        raise ValueError("local_part is required (mailbox name only — domain is fixed)")
    if not _LOCAL_PART_RE.match(local_part):
        raise ValueError(
            "local_part must start with alphanumeric and use only letters, digits, . _ + -"
        )
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 256:
        raise ValueError("password too long")
    if len(display_name) > 128:
        raise ValueError("display_name too long")
    if any(ord(c) < 32 for c in display_name):
        raise ValueError("display_name contains invalid characters")

    domain = _mail_domain_from_config()
    email = f"{local_part}@{domain}"
    argv = [email, password]
    if display_name:
        argv.append(display_name)
    return "create", argv



async def _stream_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "PYTHONUNBUFFERED": "1"}
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None

    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

    async def _pump(stream: asyncio.StreamReader, kind: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            await queue.put((kind, line.decode("utf-8", errors="replace")))

    tasks = [
        asyncio.create_task(_pump(proc.stdout, "stdout")),
        asyncio.create_task(_pump(proc.stderr, "stderr")),
    ]

    async def _waiter() -> None:
        await asyncio.gather(*tasks)
        await queue.put(None)

    waiter = asyncio.create_task(_waiter())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            kind, text = item
            if kind == "stdout":
                yield proto.event_stdout(text)
            else:
                yield proto.event_stderr(text)
    finally:
        await waiter

    code = await proc.wait()
    yield proto.event_done(int(code))


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

    # Local HTTPS probes only — no lab IPs hardcoded; VIP may or may not be on this host.
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
    """Stream `09-hardening.sh --status` (STATUS_ONLY=1 — read-only show_status + exit)."""
    script = resolve_hardening()
    yield proto.event_stdout(f"Running fixed script: {script} --status\n")
    # Fixed argv only — --status is not client-supplied. stdbuf -oL for true line streaming
    # through the pipe (bash would otherwise block-buffer when stdout is not a TTY).
    argv = [str(script), "--status"]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(argv, cwd=script.parent):
        yield ev


async def cmd_run_hardening() -> AsyncIterator[dict[str, Any]]:
    """Stream full `09-hardening.sh` (idempotent Part A — no client argv)."""
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
    script = resolve_kin_mail()
    yield proto.event_stdout(
        f"Running fixed script: {script} --full-install "
        f"(KIN_CONSOLE_CONFIRMED=1; dead-man NOT auto-cancelled)\n"
    )
    argv = [str(script), "--full-install"]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(
        argv,
        cwd=script.parent,
        extra_env={"KIN_CONSOLE_CONFIRMED": "1"},
    ):
        yield ev


async def cmd_cancel_firewall_deadman() -> AsyncIterator[dict[str, Any]]:
    """Explicit operator step: cancel ufw dead-man after verification (keeps ufw on)."""
    script = resolve_firewall()
    yield proto.event_stdout(f"Running fixed script: {script} cancel-deadman\n")
    argv = [str(script), "cancel-deadman"]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(argv, cwd=script.parent):
        yield ev


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
    else:
        # Never print password. Email is argv_tail[0].
        email = argv_tail[0]
        dname = argv_tail[2] if len(argv_tail) > 2 else ""
        yield proto.event_stdout(
            f"Running fixed script: {script} {email} <password-redacted>"
            + (f" {dname!r}" if dname else "")
            + "\n"
        )
        yield proto.event_stdout(
            "# Uses kin_quota_gate_allow_new_mailbox before zmprov ca (same as CLI).\n"
        )

    argv = [str(script), *argv_tail]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(argv, cwd=script.parent):
        yield ev


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


HANDLERS: dict[str, CommandHandler] = {
    proto.CMD_GET_STATUS: _adapt(cmd_get_status),
    proto.CMD_RUN_HARDENING_STATUS: _adapt(cmd_run_hardening_status),
    proto.CMD_APPLY_WIZARD_DRAFT: _adapt(cmd_apply_wizard_draft),
    proto.CMD_RUN_HARDENING: _adapt(cmd_run_hardening),
    proto.CMD_RUN_FULL_INSTALL: _adapt(cmd_run_full_install),
    proto.CMD_CANCEL_FIREWALL_DEADMAN: _adapt(cmd_cancel_firewall_deadman),
    proto.CMD_GET_AUDIT_LOG: _adapt(cmd_get_audit_log),
    proto.CMD_CREATE_MAILBOX: _adapt(cmd_create_mailbox),
}


def get_handler(cmd: str) -> CommandHandler | None:
    return HANDLERS.get(cmd)
