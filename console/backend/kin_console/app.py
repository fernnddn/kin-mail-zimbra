"""KIN Mail admin console FastAPI application."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kin_privhelper import protocol as proto
from kin_privhelper.rbac import (
    ROLE_LABELS,
    ROLE_SUPER_ADMIN,
    ALL_ROLES,
    command_allowed,
    deny_message,
    role_label,
)

from . import auth, draft, users
from .privhelper_client import run_command
from .settings import settings
from .users import ConsoleUser

app = FastAPI(title="KIN Mail Console", docs_url=None, redoc_url=None, openapi_url=None)

EULA_COOKIE = "kin_console_eula"
EULA_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class CreateUserBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)
    auth_type: str = Field(default="local", min_length=1, max_length=16)
    password: str = Field(default="", max_length=256)
    ad_username: str = Field(default="", max_length=128)


@app.on_event("startup")
def _startup() -> None:
    auth.ensure_session_secret()
    users.ensure_users_store()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "kin-mail-console"}


@app.get("/api/setup/status")
def setup_status(request: Request, response: Response) -> dict[str, object]:
    """Public: setup gate + whether a full install is currently running."""
    from kin_privhelper.deploy_state import setup_status_payload

    payload = setup_status_payload()
    # 5s wizard poll: sliding-refresh a real session so setup never drops it.
    auth.maybe_refresh_session(request, response)
    return payload


@app.get("/api/wizard/public-ip")
def wizard_public_ip(
    _actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> dict[str, str]:
    """Public IPv4 this host presents on HTTP, for A/SPF paste. Empty if unknown."""
    from .public_ip import probe_public_ipv4

    return {"ipv4": probe_public_ipv4()}


@app.get("/api/wizard/deploy/last-log")
def wizard_deploy_last_log(
    _actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> dict[str, object]:
    """Read-only last deploy transcript (no privhelper — safe during busy install)."""
    from kin_privhelper.deploy_state import DEPLOY_LAST_LOG, full_install_in_progress

    text = ""
    missing = not DEPLOY_LAST_LOG.is_file()
    if not missing:
        try:
            text = DEPLOY_LAST_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"cannot read deploy log: {exc}",
            ) from exc
    return {
        "path": str(DEPLOY_LAST_LOG),
        "missing": missing,
        "text": text,
        "install_in_progress": full_install_in_progress(),
        "mtime": (
            DEPLOY_LAST_LOG.stat().st_mtime if DEPLOY_LAST_LOG.is_file() else None
        ),
    }


# --- EULA (pre-auth; static disclaimer only) ---------------------------------


@app.get("/api/eula")
def eula_status(request: Request) -> dict[str, object]:
    accepted = request.cookies.get(EULA_COOKIE) == "1"
    return {
        "accepted": accepted,
        "title": "KIN Mail — Terms of Service",
        "body": (
            "PLACEHOLDER EULA\n\n"
            "This is temporary placeholder text for the KIN Mail appliance terms of service. "
            "The operator will replace this with the final legal language.\n\n"
            "By continuing you acknowledge that this console configures mail infrastructure "
            "and that incorrect settings may affect availability or security. "
            "Privileged actions run only through the local privhelper whitelist."
        ),
    }


@app.post("/api/eula/accept")
def eula_accept(body: draft.EulaAcceptBody, response: Response) -> dict[str, object]:
    if not body.accepted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Acceptance required")
    response.set_cookie(
        key=EULA_COOKIE,
        value="1",
        max_age=EULA_MAX_AGE,
        httponly=False,  # SPA also reads acceptance client-side for routing
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"accepted": True}


# --- Auth -------------------------------------------------------------------


@app.post("/api/login")
async def login(body: LoginBody, response: Response) -> dict[str, str]:
    try:
        user = users.authenticate(body.username, body.password)
    except users.AuthError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth store unavailable",
        ) from exc
    auth.set_session_cookie(response, user.username)
    # First-boot plaintext lives only under /root (root:root 0600). Console cannot
    # unlink it itself — ask privhelperd after a successful *local* login. Never
    # block login if cleanup fails (helper down / busy race).
    if user.auth_type == users.AUTH_LOCAL:
        try:
            await _collect_privhelper(
                proto.CMD_CLEAR_INITIAL_CONSOLE_PASSWORD,
                user.username,
            )
        except Exception:  # noqa: BLE001 — login must succeed regardless
            pass
    return {
        "status": "ok",
        "username": user.username,
        "role": user.role,
        "role_label": role_label(user.role),
        "auth_type": user.auth_type,
    }


@app.post("/api/logout")
def logout(response: Response) -> dict[str, str]:
    auth.clear_session_cookie(response)
    return {"status": "ok"}


@app.get("/api/me")
def me(user: ConsoleUser = Depends(auth.require_console_user)) -> dict[str, str]:
    return {
        "username": user.username,
        "role": user.role,
        "role_label": role_label(user.role),
        "auth_type": user.auth_type,
    }


@app.get("/api/roles")
def list_roles(_user: ConsoleUser = Depends(auth.require_console_user)) -> dict[str, object]:
    return {
        "roles": [
            {"id": rid, "label": ROLE_LABELS[rid]}
            for rid in sorted(ALL_ROLES)
        ]
    }


@app.get("/api/ad/status")
def ad_status(
    _user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    from .ad_settings import load_ad_settings

    return {"ad": load_ad_settings().public_summary()}


# --- Local users (KIN Super Admin only for create/delete) -------------------


class SetPasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


async def _mutate_console_users(
    actor: str,
    args: dict,
) -> dict[str, object]:
    result = await _collect_privhelper(
        proto.CMD_MUTATE_CONSOLE_USERS,
        actor,
        args=args,
    )
    parsed = _json_from_log(str(result.get("log") or ""), "USERS_RESULT:")
    error = str(parsed.get("error") or result.get("error") or "").strip()
    code = str(parsed.get("code") or "")
    if result.get("ok") and parsed.get("ok"):
        return parsed
    if result.get("error") and "busy" in str(result.get("error")).lower():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A privileged action is already running. Retry in a moment.",
        )
    if "denied" in str(result.get("error") or "").lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error or "Not allowed to change console users",
        )
    if code == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if code == "invalid":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Invalid user change",
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=error or "Could not sync console users to the other mail node. The change was not applied.",
    )


@app.get("/api/users")
def api_list_users(
    _user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    return {"users": users.list_public_users()}


@app.post("/api/users")
async def api_create_user(
    body: CreateUserBody,
    actor: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    parsed = await _mutate_console_users(
        actor.username,
        {
            "op": "create",
            "username": body.username,
            "role": body.role,
            "auth_type": body.auth_type,
            "password": body.password,
            "ad_username": body.ad_username,
        },
    )
    return {"user": parsed.get("user")}


@app.delete("/api/users/{username}")
async def api_delete_user(
    username: str,
    actor: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, str]:
    await _mutate_console_users(
        actor.username,
        {"op": "delete", "username": username},
    )
    return {"status": "ok"}


@app.post("/api/users/{username}/password")
async def api_set_user_password(
    username: str,
    body: SetPasswordBody,
    actor: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, str]:
    await _mutate_console_users(
        actor.username,
        {"op": "set_password", "username": username, "password": body.password},
    )
    return {"status": "ok"}


@app.post("/api/me/password")
async def api_set_own_password(
    body: SetPasswordBody,
    actor: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, str]:
    if actor.auth_type != users.AUTH_LOCAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AD-backed accounts change their password in Active Directory, not here.",
        )
    await _mutate_console_users(
        actor.username,
        {"op": "set_password", "username": actor.username, "password": body.password},
    )
    return {"status": "ok"}


# --- Wizard draft (auth required after deploy; anonymous setup before) ------


@app.get("/api/wizard/draft")
def get_wizard_draft(_actor: auth.WizardActor = Depends(auth.wizard_actor)) -> dict:
    return draft.public_draft(draft.load_draft())


@app.put("/api/wizard/draft")
async def put_wizard_draft(
    body: draft.DraftPatch,
    actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> dict:
    current = draft.load_draft()
    # Host provisioning passwords go to the privhelper vault, never the draft file.
    root_pass = (body.host_root_pass or "").strip() if body.host_root_pass is not None else ""
    kin_pass = (body.kin_user_pass or "").strip() if body.kin_user_pass is not None else ""
    if root_pass or kin_pass:
        if not command_allowed(actor.role, proto.CMD_STORE_PROVISIONING_SECRETS):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=deny_message(actor.role, proto.CMD_STORE_PROVISIONING_SECRETS),
            )
        stored = await _collect_privhelper(
            proto.CMD_STORE_PROVISIONING_SECRETS,
            actor.username,
            args={"host_root_pass": root_pass, "kin_user_pass": kin_pass},
        )
        if not stored.get("ok"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(stored.get("error") or stored.get("log") or "could not store credentials"),
            )
    updated = draft.apply_patch(current, body)
    if updated.topology and updated.topology not in ("1vm", "2vm"):
        raise HTTPException(status_code=400, detail="topology must be 1vm or 2vm")
    if updated.tls_method and updated.tls_method not in ("cloudflare", "manual", "customer"):
        raise HTTPException(status_code=400, detail="tls_method invalid")
    if updated.topology == "2vm":
        if updated.peer_host_ip and not draft.valid_ipv4(updated.peer_host_ip):
            raise HTTPException(status_code=400, detail="Second server IP must be an IPv4 address")
        if updated.observability_vm_ip and not draft.valid_ipv4(updated.observability_vm_ip):
            raise HTTPException(status_code=400, detail="Observability VM IP must be an IPv4 address")
        if updated.cluster_vip_ip and not draft.valid_ipv4(updated.cluster_vip_ip):
            raise HTTPException(status_code=400, detail="Cluster VIP must be an IPv4 address")
        if updated.cluster_vip_ip:
            vip = updated.cluster_vip_ip.strip()
            if updated.peer_host_ip and vip == updated.peer_host_ip.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Cluster VIP must not be the second server IP",
                )
            if updated.observability_vm_ip and vip == updated.observability_vm_ip.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Cluster VIP must not be the Observability VM IP",
                )
            local_ip = draft.detect_local_ipv4()
            if local_ip and vip == local_ip:
                raise HTTPException(
                    status_code=400,
                    detail="Cluster VIP must not be this server's own address",
                )
    saved = draft.save_draft(updated)
    return draft.public_draft(saved)


# --- Privileged execution via local privhelper (SSE) -------------------------

# Browser may only pick these action names; each maps to a fixed whitelist cmd.
_STREAM_ACTIONS: dict[str, str] = {
    "hardening_status": proto.CMD_RUN_HARDENING_STATUS,
    "apply_draft": proto.CMD_APPLY_WIZARD_DRAFT,
    "run_hardening": proto.CMD_RUN_HARDENING,
    "get_status": proto.CMD_GET_STATUS,
    "full_install": proto.CMD_RUN_FULL_INSTALL,
    "cancel_firewall_deadman": proto.CMD_CANCEL_FIREWALL_DEADMAN,
    "audit_log": proto.CMD_GET_AUDIT_LOG,
    "deploy_log": proto.CMD_GET_DEPLOY_LOG,
    "maintenance": proto.CMD_MAINTENANCE,
    "ha_orchestration": proto.CMD_RUN_HA_ORCHESTRATION,
    "remove_host": proto.CMD_REMOVE_HOST,
    "remove_observability": proto.CMD_REMOVE_OBSERVABILITY,
    "add_observability": proto.CMD_ADD_OBSERVABILITY,
}


class CreateMailboxBody(BaseModel):
    local_part: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=128)


async def _collect_privhelper(
    cmd: str,
    username: str,
    *,
    args: dict | None = None,
) -> dict[str, object]:
    lines: list[str] = []
    exit_code = 1
    error: str | None = None
    async for ev in run_command(
        cmd,
        username,
        socket_path=settings.privhelper_socket,
        args=args,
    ):
        et = ev.get("type")
        if et == "stdout" and ev.get("data"):
            lines.append(str(ev["data"]))
        elif et == "stderr" and ev.get("data"):
            lines.append(str(ev["data"]))
        elif et == "error":
            error = str(ev.get("message") or ev.get("code") or "error")
            lines.append(f"[error] {ev.get('code')}: {error}\n")
        elif et == "done":
            exit_code = int(ev.get("exit_code", 1))
    return {
        "exit_code": exit_code,
        "ok": exit_code == 0 and error is None,
        "error": error,
        "log": "".join(lines),
    }


@app.get("/api/mailbox/status")
async def mailbox_status(
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Seat / quota status via 08-create-mailbox.sh --status (all authenticated roles)."""
    if not command_allowed(user.role, proto.CMD_CREATE_MAILBOX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_CREATE_MAILBOX),
        )
    result = await _collect_privhelper(
        proto.CMD_CREATE_MAILBOX,
        user.username,
        args={"op": "status"},
    )
    return result


@app.post("/api/mailbox")
async def mailbox_create(
    body: CreateMailboxBody,
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Self-service mailbox create — Customer Admin allowed; quota gate enforced in 08."""
    if not command_allowed(user.role, proto.CMD_CREATE_MAILBOX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_CREATE_MAILBOX),
        )
    local_part = body.local_part.strip().lower()
    if "@" in local_part:
        raise HTTPException(
            status_code=400,
            detail="Enter the local part only — domain is fixed to the configured mail domain",
        )
    maint = await _collect_privhelper(
        proto.CMD_MAINTENANCE,
        user.username,
        args={"op": "status"},
    )
    parsed = _json_from_log(str(maint.get("log") or ""), "CLUSTER_STATUS_JSON:")
    if parsed.get("maintenance_active"):
        raise HTTPException(
            status_code=409,
            detail="A mail node is in maintenance. Finish Exit Maintenance before creating mailboxes.",
        )
    result = await _collect_privhelper(
        proto.CMD_CREATE_MAILBOX,
        user.username,
        args={
            "op": "create",
            "local_part": local_part,
            "password": body.password,
            "display_name": body.display_name.strip(),
        },
    )
    # Never echo the submitted password back.
    return {
        "exit_code": result["exit_code"],
        "ok": result["ok"],
        "error": result["error"],
        "log": result["log"],
        "local_part": local_part,
    }


@app.get("/api/wizard/deploy/stream")
async def wizard_deploy_stream(
    request: Request,
    actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> StreamingResponse:
    """Stream a whitelisted privhelper command into the log viewer.

    Query `action` is an enum of safe aliases — never a free-form shell/command string.
    RBAC is enforced here and again inside privhelperd (server-side role from users.json,
    or the synthetic pre-deploy setup identity).
    """
    action = (request.query_params.get("action") or "hardening_status").strip()
    cmd = _STREAM_ACTIONS.get(action)
    if not cmd:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {action!r}; allowed: {', '.join(sorted(_STREAM_ACTIONS))}",
        )

    stream_args: dict | None = None
    if cmd == proto.CMD_MAINTENANCE:
        op = (request.query_params.get("op") or "status").strip().lower()
        target = (request.query_params.get("target") or "").strip()
        stream_args = {"op": op, "target": target}
    elif cmd == proto.CMD_RUN_HA_ORCHESTRATION:
        join_mode = (request.query_params.get("join_mode") or "apply").strip().lower()
        skip = (request.query_params.get("skip_remote_install") or "").strip().lower()
        stream_args = {"join_mode": join_mode, "skip_remote_install": skip}
    elif cmd == proto.CMD_REMOVE_HOST:
        op = (request.query_params.get("op") or "apply").strip().lower()
        target = (request.query_params.get("target") or "").strip()
        stream_args = {"op": op, "target": target}
    elif cmd in (proto.CMD_REMOVE_OBSERVABILITY, proto.CMD_ADD_OBSERVABILITY):
        op = (request.query_params.get("op") or "apply").strip().lower()
        stream_args = {"op": op}

    if not command_allowed(actor.role, cmd, args=stream_args):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(actor.role, cmd),
        )

    async def event_gen():
        yield f"data: {json.dumps({'type': 'meta', 'cmd': cmd, 'action': action})}\n\n"
        async for ev in run_command(
            cmd,
            actor.username,
            socket_path=settings.privhelper_socket,
            args=stream_args,
        ):
            # Drop the SSE feed when the browser is gone. privhelperd keeps the
            # privileged job running and last-log remains the catch-up path.
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/wizard/deploy")
async def wizard_deploy_hint(
    _actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> dict[str, object]:
    """Compat: prefer SSE /api/wizard/deploy/stream?action=… for live output."""
    return {
        "status": "use_stream",
        "message": "Use GET /api/wizard/deploy/stream?action=…",
        "actions": sorted(_STREAM_ACTIONS.keys()),
        "commands": {k: v for k, v in _STREAM_ACTIONS.items()},
    }


@app.get("/api/wizard/ha-disk-preflight")
async def wizard_ha_disk_preflight(
    actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> dict[str, object]:
    """Read-only DRBD disk check. Partitioning is Ansible drbd_disk_prep during Build HA."""
    if not command_allowed(actor.role, proto.CMD_HA_DISK_PREFLIGHT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(actor.role, proto.CMD_HA_DISK_PREFLIGHT),
        )
    result = await _collect_privhelper(
        proto.CMD_HA_DISK_PREFLIGHT,
        actor.username,
    )
    parsed = _json_from_log(str(result.get("log") or ""), "HA_DISK_JSON:")
    errors = parsed.get("errors") if isinstance(parsed.get("errors"), list) else []
    errors = [str(e) for e in errors]
    ok = bool(parsed.get("ok")) if parsed else False
    build_allowed = bool(parsed.get("build_allowed")) if parsed else ok
    will_auto = parsed.get("will_auto_partition") if isinstance(parsed.get("will_auto_partition"), list) else []
    if not ok and not build_allowed and not errors:
        err = result.get("error")
        errors = [
            str(err)
            if err
            else "Could not complete the second-disk check. Build HA pair stays blocked."
        ]
    return {
        "ok": ok,
        "build_allowed": build_allowed,
        "will_auto_partition": will_auto,
        "errors": errors,
        "instructions": parsed.get("instructions") or "",
        "nodes": parsed.get("nodes") or [],
        "exit_code": result.get("exit_code"),
    }


def _json_from_log(log: str, prefix: str) -> dict:
    for line in (log or "").splitlines():
        if line.startswith(prefix):
            try:
                data = json.loads(line[len(prefix) :])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return {}


@app.get("/api/cluster/status")
async def cluster_status(
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Live Pacemaker/DRBD snapshot for the Cluster page (no secrets)."""
    if not command_allowed(user.role, proto.CMD_MAINTENANCE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_MAINTENANCE),
        )
    result = await _collect_privhelper(
        proto.CMD_MAINTENANCE,
        user.username,
        args={"op": "status"},
    )
    parsed = _json_from_log(str(result.get("log") or ""), "CLUSTER_STATUS_JSON:")
    return {
        "ok": bool(result.get("ok")),
        "exit_code": result.get("exit_code"),
        "error": result.get("error"),
        "log": result.get("log"),
        "cluster": parsed,
    }


class ObservabilitySecretsBody(BaseModel):
    ip: str = Field(min_length=1, max_length=64)
    hostname: str = Field(default="", max_length=253)
    host_root_pass: str = Field(min_length=8, max_length=256)
    kin_user_pass: str = Field(min_length=8, max_length=256)


@app.post("/api/cluster/observability/secrets")
async def cluster_observability_secrets(
    body: ObservabilitySecretsBody,
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Store replacement Observability VM credentials. Never echoed back."""
    if not command_allowed(user.role, proto.CMD_STORE_OBSERVABILITY_SECRETS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_STORE_OBSERVABILITY_SECRETS),
        )
    if not draft.valid_ipv4(body.ip.strip()):
        raise HTTPException(status_code=400, detail="Observability IP must be an IPv4 address")
    result = await _collect_privhelper(
        proto.CMD_STORE_OBSERVABILITY_SECRETS,
        user.username,
        args={
            "ip": body.ip.strip(),
            "hostname": (body.hostname or "").strip(),
            "root_pass": body.host_root_pass,
            "kin_user_pass": body.kin_user_pass,
        },
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result.get("error") or result.get("log") or "could not store credentials"),
        )
    return {"ok": True}


def _spa_index() -> Path:
    return settings.static_dir / "index.html"


@app.get("/")
def root(request: Request) -> Response:
    # Always serve SPA shell; React router enforces EULA + login; API enforces auth.
    index = _spa_index()
    if not index.is_file():
        return JSONResponse({"detail": "Frontend not installed"}, status_code=503)
    return FileResponse(index)


static_assets = settings.static_dir / "assets"
if static_assets.is_dir():
    app.mount("/assets", StaticFiles(directory=str(static_assets)), name="assets")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str, request: Request) -> Response:
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    index = _spa_index()
    if not index.is_file():
        raise HTTPException(status_code=503, detail="Frontend not installed")
    return FileResponse(index)
