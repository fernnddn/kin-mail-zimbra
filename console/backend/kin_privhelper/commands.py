"""Fixed whitelist command implementations — no client-supplied argv/shell."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from . import protocol as proto

# Deploy tree on appliance (kin-mail.sh default). Override via env for lab clones.
DEPLOY_DIR = Path(os.environ.get("KIN_MAIL_DEPLOY_DIR", "/opt/kin-mail-deploy"))

# Basename only — resolved under DEPLOY_DIR; never take a path from the client.
HEALTHCHECK_CANDIDATES = (
    "install/05-healthcheck.sh",
    "05-healthcheck.sh",
)


def resolve_healthcheck() -> Path:
    for rel in HEALTHCHECK_CANDIDATES:
        candidate = (DEPLOY_DIR / rel).resolve()
        try:
            candidate.relative_to(DEPLOY_DIR.resolve())
        except ValueError as exc:
            raise RuntimeError("script path escapes deploy dir") from exc
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        f"05-healthcheck.sh not found under {DEPLOY_DIR} "
        f"(tried {', '.join(HEALTHCHECK_CANDIDATES)})"
    )


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
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
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


async def cmd_run_healthcheck() -> AsyncIterator[dict[str, Any]]:
    script = resolve_healthcheck()
    yield proto.event_stdout(f"Running fixed script: {script}\n")
    # Fixed argv only — no client-supplied arguments.
    async for ev in _stream_subprocess([str(script)], cwd=script.parent):
        yield ev


CommandHandler = Callable[[], AsyncIterator[dict[str, Any]]]

HANDLERS: dict[str, CommandHandler] = {
    proto.CMD_GET_STATUS: cmd_get_status,
    proto.CMD_RUN_HEALTHCHECK: cmd_run_healthcheck,
}


def get_handler(cmd: str) -> CommandHandler | None:
    return HANDLERS.get(cmd)
