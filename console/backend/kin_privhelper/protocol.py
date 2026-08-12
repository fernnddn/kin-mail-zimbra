"""Wire protocol: one JSON request line, then NDJSON event stream."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1

# Exact whitelist — nothing else is accepted (no free-form shell).
CMD_GET_STATUS = "get_status"
CMD_RUN_HARDENING_STATUS = "run_script:09-hardening.sh --status"
CMD_APPLY_WIZARD_DRAFT = "apply_wizard_draft"
CMD_RUN_HARDENING = "run_hardening"
CMD_RUN_FULL_INSTALL = "run_full_install"
CMD_CANCEL_FIREWALL_DEADMAN = "cancel_firewall_deadman"
CMD_GET_AUDIT_LOG = "get_audit_log"
CMD_GET_DEPLOY_LOG = "get_deploy_log"
CMD_CREATE_MAILBOX = "create_mailbox"
CMD_CLEAR_INITIAL_CONSOLE_PASSWORD = "clear_initial_console_password"
ALLOWED_COMMANDS = frozenset(
    {
        CMD_GET_STATUS,
        CMD_RUN_HARDENING_STATUS,
        CMD_APPLY_WIZARD_DRAFT,
        CMD_RUN_HARDENING,
        CMD_RUN_FULL_INSTALL,
        CMD_CANCEL_FIREWALL_DEADMAN,
        CMD_GET_AUDIT_LOG,
        CMD_GET_DEPLOY_LOG,
        CMD_CREATE_MAILBOX,
        CMD_CLEAR_INITIAL_CONSOLE_PASSWORD,
    }
)


def encode_line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_line(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8").strip()
    if not text:
        raise ValueError("empty request")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    return data


def make_request(cmd: str, username: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"v": PROTOCOL_VERSION, "cmd": cmd, "username": username}
    if args:
        req["args"] = args
    return req


def event_accepted(cmd: str) -> dict[str, Any]:
    return {"type": "accepted", "cmd": cmd}


def event_stdout(data: str) -> dict[str, Any]:
    return {"type": "stdout", "data": data}


def event_stderr(data: str) -> dict[str, Any]:
    return {"type": "stderr", "data": data}


def event_done(exit_code: int) -> dict[str, Any]:
    return {"type": "done", "exit_code": exit_code}


def event_error(code: str, message: str) -> dict[str, Any]:
    return {"type": "error", "code": code, "message": message}
