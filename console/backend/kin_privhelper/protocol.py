"""Wire protocol: one JSON request line, then NDJSON event stream."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1

# Exact whitelist — nothing else is accepted (no free-form shell).
CMD_GET_STATUS = "get_status"
# Fixed script + fixed --status flag (read-only path in 09-hardening.sh).
CMD_RUN_HARDENING_STATUS = "run_script:09-hardening.sh --status"
ALLOWED_COMMANDS = frozenset({CMD_GET_STATUS, CMD_RUN_HARDENING_STATUS})


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


def make_request(cmd: str, username: str) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "cmd": cmd, "username": username}


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
