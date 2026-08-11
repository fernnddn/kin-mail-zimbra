"""Fixed whitelist command implementations — no client-supplied argv/shell."""

from __future__ import annotations

import asyncio
import os
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


def resolve_under_deploy(candidates: tuple[str, ...], label: str) -> Path:
    """Resolve a script under DEPLOY_DIR; reject path escape (same pattern as former healthcheck)."""
    for rel in candidates:
        candidate = (DEPLOY_DIR / rel).resolve()
        try:
            candidate.relative_to(DEPLOY_DIR.resolve())
        except ValueError as exc:
            raise RuntimeError("script path escapes deploy dir") from exc
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        f"{label} not found under {DEPLOY_DIR} (tried {', '.join(candidates)})"
    )


def resolve_hardening() -> Path:
    return resolve_under_deploy(HARDENING_CANDIDATES, "09-hardening.sh")


async def _stream_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
) -> AsyncIterator[dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "PYTHONUNBUFFERED": "1"},
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


CommandHandler = Callable[[], AsyncIterator[dict[str, Any]]]

HANDLERS: dict[str, CommandHandler] = {
    proto.CMD_GET_STATUS: cmd_get_status,
    proto.CMD_RUN_HARDENING_STATUS: cmd_run_hardening_status,
    proto.CMD_APPLY_WIZARD_DRAFT: cmd_apply_wizard_draft,
    proto.CMD_RUN_HARDENING: cmd_run_hardening,
}


def get_handler(cmd: str) -> CommandHandler | None:
    return HANDLERS.get(cmd)
