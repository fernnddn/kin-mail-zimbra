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
from .login_throttle import LOGIN_THROTTLE
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
    # UI must match wizard_actor: show login when anonymous wizard is locked.
    payload["wizard_requires_login"] = auth.wizard_requires_login()
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
    """Read-only last deploy transcript (no privhelper - safe during busy install)."""
    from kin_privhelper.deploy_state import DEPLOY_LAST_LOG, pipeline_in_progress

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
        "install_in_progress": pipeline_in_progress(),
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
        "title": "KIN Mail: Terms of Service",
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
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, object]:
    client = _request_client_ipv4(request) or "unknown"
    throttle_key = f"{client}|{(body.username or '').strip().lower()}"
    decision = LOGIN_THROTTLE.check(throttle_key)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {decision.retry_after_sec}s.",
            headers={"Retry-After": str(decision.retry_after_sec)},
        )
    try:
        user = users.authenticate(body.username, body.password)
    except users.AuthError as exc:
        fail = LOGIN_THROTTLE.record_failure(throttle_key)
        if not fail.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed login attempts. Try again in {fail.retry_after_sec}s.",
                headers={"Retry-After": str(fail.retry_after_sec)},
            ) from exc
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth store unavailable",
        ) from exc

    LOGIN_THROTTLE.record_success(throttle_key)
    auth.set_session_cookie(response, user.username)
    # First-boot plaintext lives only under /root (root:root 0600). Console cannot
    # unlink it itself - ask privhelperd after a successful *local* login. Never
    # block login if cleanup fails (helper down / busy race).
    if user.auth_type == users.AUTH_LOCAL:
        try:
            await _collect_privhelper(
                proto.CMD_CLEAR_INITIAL_CONSOLE_PASSWORD,
                user.username,
            )
        except Exception:  # noqa: BLE001 - login must succeed regardless
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
def me(user: ConsoleUser = Depends(auth.require_console_user)) -> dict[str, object]:
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


class OwnPasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
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
    lic = _current_license_view()
    if lic.get("provisioning_blocked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "New console users cannot be created while the license is invalid, in grace, or expired. "
                "Existing mail still flows."
            ),
        )
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
    body: OwnPasswordBody,
    actor: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, str]:
    if actor.auth_type != users.AUTH_LOCAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AD-backed accounts change their password in Active Directory, not here.",
        )
    if not actor.password_hash or not auth.verify_password(
        body.current_password, actor.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
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
    "add_host": proto.CMD_ADD_HOST,
    "remove_observability": proto.CMD_REMOVE_OBSERVABILITY,
    "add_observability": proto.CMD_ADD_OBSERVABILITY,
    "appliance_settings": proto.CMD_APPLY_APPLIANCE_SETTINGS,
    "install_monitoring": proto.CMD_INSTALL_MONITORING,
}


class CreateMailboxBody(BaseModel):
    local_part: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=128)
    given_name: str = Field(default="", max_length=128)
    surname: str = Field(default="", max_length=128)
    account_status: str = Field(default="active", max_length=16)


class MailboxEmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=256)


class MailboxRenameBody(BaseModel):
    email: str = Field(min_length=3, max_length=256)
    new_local_part: str = Field(min_length=1, max_length=64)


class AdSettingsBody(BaseModel):
    enabled: bool = False
    ldap_url: str = Field(default="", max_length=512)
    search_base: str = Field(default="", max_length=512)
    search_filter: str = Field(default="", max_length=512)
    search_bind_dn: str = Field(default="", max_length=512)
    bind_dn_template: str = Field(default="", max_length=512)
    search_bind_password: str = Field(default="", max_length=256)
    # 06-hybrid-auth.sh verifies a real bind before it repoints Zimbra's
    # authentication, so both halves need this account, not just the wizard.
    test_user: str = Field(default="", max_length=256)
    test_pass: str = Field(default="", max_length=256)


class LicenseApplyBody(BaseModel):
    token: str = Field(min_length=16, max_length=8192)


class FirewallApplyBody(BaseModel):
    admin_ips: str = Field(default="", max_length=2048)


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
    return _mailbox_status_payload(result, is_super_admin=user.role == ROLE_SUPER_ADMIN)


@app.get("/api/mailbox")
async def mailbox_list(
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    if not command_allowed(user.role, proto.CMD_CREATE_MAILBOX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_CREATE_MAILBOX),
        )
    result = await _collect_privhelper(
        proto.CMD_CREATE_MAILBOX,
        user.username,
        args={"op": "list"},
    )
    payload = _mailbox_status_payload(result, is_super_admin=user.role == ROLE_SUPER_ADMIN)
    rows = _json_any_from_log(str(result.get("log") or ""), "MAILBOX_JSON:")
    payload["mailboxes"] = rows if isinstance(rows, list) else []
    return payload


@app.post("/api/mailbox")
async def mailbox_create(
    body: CreateMailboxBody,
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Self-service mailbox create. Customer Admin allowed; quota gate enforced in 08."""
    if not command_allowed(user.role, proto.CMD_CREATE_MAILBOX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_CREATE_MAILBOX),
        )
    local_part = body.local_part.strip().lower()
    if "@" in local_part:
        raise HTTPException(
            status_code=400,
            detail="Enter the local part only. Domain is fixed to the configured mail domain",
        )
    status_name = body.account_status.strip().lower() or "active"
    if status_name not in ("active", "locked"):
        raise HTTPException(status_code=400, detail="account_status must be active or locked")
    lic = _current_license_view()
    if lic.get("provisioning_blocked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "New mailboxes cannot be created while the license is invalid, in grace, or expired. "
                "Existing mail still flows."
            ),
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
            "given_name": body.given_name.strip(),
            "surname": body.surname.strip(),
            "account_status": status_name,
        },
    )
    created = _json_from_log(str(result.get("log") or ""), "CREATE_JSON:")
    payload = _mailbox_status_payload(result, is_super_admin=user.role == ROLE_SUPER_ADMIN)
    return {
        "exit_code": result["exit_code"],
        "ok": result["ok"],
        "error": result["error"],
        "local_part": local_part,
        "email": created.get("email") or "",
        "seats": payload.get("seats"),
        "message": _mailbox_create_message(payload.get("message"), created),
    }


def _mailbox_create_message(payload_message: object, created: dict[str, object]) -> object:
    """Override the generic seat/quota message when zmprov ca succeeded but
    the post-create auth probe did not (08-create-mailbox.sh).

    exit_code is non-zero in that case, so the frontend's !ok branch would
    otherwise show "Mailbox was not created." - false, since the account
    exists and a seat is already used (re-audit, 25 Aug 2026).
    """
    if not created.get("auth_probe_failed"):
        return payload_message
    return (
        f"{created.get('email') or 'The account'} was created (a seat was used) "
        "but the console could not verify it can log in yet. Do not create it "
        "again. Check back shortly, or contact support if this persists."
    )


@app.post("/api/mailbox/rename")
async def mailbox_rename(
    body: MailboxRenameBody,
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    if not command_allowed(user.role, proto.CMD_CREATE_MAILBOX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_CREATE_MAILBOX),
        )
    result = await _collect_privhelper(
        proto.CMD_CREATE_MAILBOX,
        user.username,
        args={
            "op": "rename",
            "email": body.email.strip().lower(),
            "new_local_part": body.new_local_part.strip().lower(),
        },
    )
    parsed = _json_from_log(str(result.get("log") or ""), "RENAME_JSON:")
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_human_mailbox_error(str(result.get("log") or ""), str(result.get("error") or "")),
        )
    return {"ok": True, "email": parsed.get("email") or "", "previous": parsed.get("previous") or ""}


@app.post("/api/mailbox/delete")
async def mailbox_delete(
    body: MailboxEmailBody,
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    if not command_allowed(user.role, proto.CMD_CREATE_MAILBOX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_CREATE_MAILBOX),
        )
    result = await _collect_privhelper(
        proto.CMD_CREATE_MAILBOX,
        user.username,
        args={"op": "delete", "email": body.email.strip().lower()},
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_human_mailbox_error(str(result.get("log") or ""), str(result.get("error") or "")),
        )
    return {"ok": True, "email": body.email.strip().lower()}


@app.get("/api/wizard/deploy/stream")
async def wizard_deploy_stream(
    request: Request,
    actor: auth.WizardActor = Depends(auth.wizard_actor),
) -> StreamingResponse:
    """Stream a whitelisted privhelper command into the log viewer.

    Query `action` is an enum of safe aliases - never a free-form shell/command string.
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
    elif cmd == proto.CMD_ADD_HOST:
        op = (request.query_params.get("op") or "apply").strip().lower()
        stream_args = {
            "op": op,
            "new_name": (request.query_params.get("new_name") or "").strip(),
            "new_ip": (request.query_params.get("new_ip") or "").strip(),
            "peer_host_name": (request.query_params.get("peer_host_name") or "").strip(),
            "peer_host_ip": (request.query_params.get("peer_host_ip") or "").strip(),
            "retired_name": (request.query_params.get("retired_name") or "").strip(),
            "retired_ip": (request.query_params.get("retired_ip") or "").strip(),
            "observability_vm_ip": (
                request.query_params.get("observability_vm_ip") or ""
            ).strip(),
            "cluster_vip_ip": (request.query_params.get("cluster_vip_ip") or "").strip(),
            "data_disk": (request.query_params.get("data_disk") or "").strip(),
            "meta_disk": (request.query_params.get("meta_disk") or "").strip(),
        }
    elif cmd in (proto.CMD_REMOVE_OBSERVABILITY, proto.CMD_ADD_OBSERVABILITY):
        op = (request.query_params.get("op") or "apply").strip().lower()
        stream_args = {"op": op}
    elif cmd == proto.CMD_APPLY_APPLIANCE_SETTINGS:
        section = (request.query_params.get("section") or "").strip().lower()
        if section not in ("tls_renew", "firewall", "tls_status", "status"):
            raise HTTPException(
                status_code=400,
                detail="appliance_settings stream allows tls_renew, firewall, tls_status, or status",
            )
        stream_args = {
            "section": section,
            "admin_ips": (request.query_params.get("admin_ips") or "").strip(),
            "client_ip": _request_client_ipv4(request),
        }

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
    """Compat: prefer SSE /api/wizard/deploy/stream?action=... for live output."""
    return {
        "status": "use_stream",
        "message": "Use GET /api/wizard/deploy/stream?action=...",
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
        "data_disk": parsed.get("data_disk") or "",
        "meta_disk": parsed.get("meta_disk") or "",
        "exit_code": result.get("exit_code"),
    }


def _json_from_log(log: str, prefix: str) -> dict:
    data = _json_any_from_log(log, prefix)
    return data if isinstance(data, dict) else {}


def _json_any_from_log(log: str, prefix: str):
    for line in (log or "").splitlines():
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix) :])
            except json.JSONDecodeError:
                continue
    return None


def _request_client_ipv4(request: Request) -> str:
    host = (request.client.host if request.client else "") or ""
    if host.startswith("::ffff:"):
        host = host[7:]
    if host == "127.0.0.1" and request.client is not None:
        # The public listener is a raw TLS relay (https_mux.py) - every
        # request otherwise looks like it came from 127.0.0.1. Recover the
        # real peer via the in-process table keyed by the relay's own local
        # port, which uvicorn sees as request.client.port.
        from .https_mux import real_client_ip_for_backend_port

        real = real_client_ip_for_backend_port(request.client.port)
        if real:
            host = real
    parts = host.split(".")
    if len(parts) != 4:
        return ""
    try:
        if all(0 <= int(p) <= 255 for p in parts) and host not in ("127.0.0.1", "0.0.0.0"):
            return host
    except ValueError:
        return ""
    return ""


def _current_license_view() -> dict:
    from kin_console.license import license_view
    from kin_privhelper.deploy_state import read_license_token, read_server_id

    return license_view(read_license_token(), read_server_id() or "")


# Whoever is reading this either can paste a licence or cannot. Telling a Super
# Admin that "a Super Admin can paste a signed license" tells them nothing, and
# telling anyone else to go and be one is worse - the licence comes from KIN,
# so that is who they need.
_LICENCE_FROM_KIN = "Contact KIN for a signed license key."
_LICENCE_SELF_SERVE = "Paste your signed license key on the Settings page."


def _licence_next_step(is_super_admin: bool) -> str:
    return _LICENCE_SELF_SERVE if is_super_admin else _LICENCE_FROM_KIN


def _seat_user_message(
    seats: dict, *, license_blocked: bool, is_super_admin: bool = False
) -> str:
    if license_blocked:
        return (
            "New mailboxes cannot be created while the license is invalid, in grace, or expired. "
            "Existing mail still flows."
        )
    code = str(seats.get("code") or "")
    used = seats.get("used")
    limit = seats.get("limit")
    if code == "unset":
        return f"Seat limit is not set. {_licence_next_step(is_super_admin)}"
    if code == "invalid":
        return (
            "The seat limit in the license could not be read. "
            f"{_licence_next_step(is_super_admin)}"
        )
    if code == "at_limit":
        return f"All contracted mailboxes are in use ({used}/{limit}). Contact KIN to add seats."
    if code == "count_failed":
        return "Could not count existing mailboxes. Try again in a moment."
    if code == "ok" and used is not None and limit is not None:
        return f"{used}/{limit} seats used. Ready to create a mailbox."
    if used is not None and limit is not None:
        return f"{used}/{limit} seats used."
    return "Seat status is unavailable."


def _mailbox_status_payload(
    result: dict[str, object], *, is_super_admin: bool = False
) -> dict[str, object]:
    seats = _json_from_log(str(result.get("log") or ""), "SEATS_JSON:")
    lic = _current_license_view()
    blocked = bool(lic.get("provisioning_blocked"))
    message = _seat_user_message(
        seats, license_blocked=blocked, is_super_admin=is_super_admin
    )
    can_create = bool(seats.get("ok")) and not blocked
    return {
        "ok": bool(result.get("ok")) and not blocked,
        "exit_code": result.get("exit_code"),
        "error": result.get("error"),
        "seats": seats,
        "message": message,
        "can_create": can_create,
        "license": {
            "status": lic.get("status"),
            "provisioning_blocked": blocked,
        },
    }


def _human_mailbox_error(log: str, error: str) -> str:
    text = f"{error}\n{log}"
    if "already exists" in text:
        return "That mailbox already exists."
    if "not found" in text.lower():
        return "Mailbox not found."
    if "PLACEHOLDER_UNSET" in text or "seat limit not configured" in text:
        return f"Seat limit is not set. {_LICENCE_FROM_KIN}"
    if "seat limit reached" in text:
        return "All contracted mailboxes are in use. Contact KIN to add seats."
    if "grace or expired" in text or "license is invalid" in text:
        return (
            "New mailboxes cannot be created while the license is invalid, in grace, or expired. "
            "Existing mail still flows."
        )
    return error or "The mailbox change did not succeed."


def _require_settings(user: ConsoleUser) -> None:
    if not command_allowed(user.role, proto.CMD_APPLY_APPLIANCE_SETTINGS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_APPLY_APPLIANCE_SETTINGS),
        )


def _settings_from_result(result: dict[str, object]) -> dict:
    parsed = _json_from_log(str(result.get("log") or ""), "SETTINGS_JSON:")
    if not result.get("ok") and not parsed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result.get("error") or "Could not load settings"),
        )
    return parsed


@app.get("/api/license/status")
def license_status(
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    return _current_license_view()


@app.get("/api/settings")
async def api_settings(
    user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    _require_settings(user)
    from .ad_settings import load_ad_settings

    result = await _collect_privhelper(
        proto.CMD_APPLY_APPLIANCE_SETTINGS,
        user.username,
        args={"section": "status"},
    )
    parsed = _settings_from_result(result)
    tls = await _collect_privhelper(
        proto.CMD_APPLY_APPLIANCE_SETTINGS,
        user.username,
        args={"section": "tls_status"},
    )
    tls_info = _json_from_log(str(tls.get("log") or ""), "TLS_JSON:")
    parsed["ad"] = load_ad_settings().public_summary()
    parsed["tls"] = tls_info
    parsed["license"] = parsed.get("license") or _current_license_view()
    return parsed


@app.post("/api/settings/ad")
async def api_settings_ad(
    body: AdSettingsBody,
    user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    _require_settings(user)
    result = await _collect_privhelper(
        proto.CMD_APPLY_APPLIANCE_SETTINGS,
        user.username,
        args={
            "section": "ad",
            "enabled": body.enabled,
            "ldap_url": body.ldap_url,
            "search_base": body.search_base,
            "search_filter": body.search_filter,
            "search_bind_dn": body.search_bind_dn,
            "bind_dn_template": body.bind_dn_template,
            "search_bind_password": body.search_bind_password,
            "test_user": body.test_user,
            "test_pass": body.test_pass,
        },
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result.get("error") or "Could not update directory settings"),
        )
    from .ad_settings import load_ad_settings

    return {"ok": True, "ad": load_ad_settings().public_summary()}


class CloudflareTokenBody(BaseModel):
    token: str = Field(min_length=1, max_length=256)


@app.post("/api/settings/cloudflare-token")
async def api_settings_cloudflare_token(
    body: CloudflareTokenBody,
    user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    """Store the Cloudflare API token so DNS-01 can issue and renew unattended.

    certbot reads it from a file and 04-tls-dkim.sh will not prompt when it has
    no terminal, which is every console-driven run - so without this the
    operator had to SSH in and hand-write /etc/letsencrypt/cloudflare.ini.
    The token is never written to the wizard draft and never echoed back.
    """
    _require_settings(user)
    result = await _collect_privhelper(
        proto.CMD_APPLY_APPLIANCE_SETTINGS,
        user.username,
        args={"section": "cloudflare_token", "token": body.token},
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result.get("error") or result.get("log") or "Could not store the token"),
        )
    return {"ok": True}


@app.post("/api/settings/license")
async def api_settings_license(
    body: LicenseApplyBody,
    user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    _require_settings(user)
    result = await _collect_privhelper(
        proto.CMD_APPLY_APPLIANCE_SETTINGS,
        user.username,
        args={"section": "license", "token": body.token.strip()},
    )
    parsed = _json_from_log(str(result.get("log") or ""), "LICENSE_JSON:")
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_human_license_error(str(result.get("error") or result.get("log") or "")),
        )
    return {"ok": True, "license": parsed or _current_license_view()}


def _human_license_error(text: str) -> str:
    """Map a verify_license ValueError to an actionable operator message.

    Every branch used to collapse into "That license is not valid. Ask KIN for
    a new signed license.", which is a dead end: a truncated paste, a license
    issued for the other HA node, and a genuinely bad signature all looked
    identical, with no way to tell which (live Phase 4-1, 26 Aug 2026). Order
    matters here - "license is not a signed payload.signature string" contains
    the word "signature" but means "the paste is malformed", so the structural
    cases must be tested BEFORE the signature case.
    """
    low = text.lower()
    # Structural / paste problems first - these all contain "signature" or
    # "payload" but are not cryptographic failures.
    if "not a signed payload" in low or "missing payload or signature" in low:
        return (
            "That license text is incomplete. Copy the whole license string, "
            "including the dot and everything after it, and paste it again."
        )
    if "wrong length" in low:
        return (
            "That license text looks truncated. Copy the whole license string "
            "and paste it again."
        )
    if "not json" in low or "must be an object" in low or "base64" in low or "invalid base" in low:
        return (
            "That license text is corrupted. Re-copy it from the license file "
            "without adding line breaks."
        )
    # Server identity: the single most common real cause on an HA pair, where
    # the license must match the Email Server ID of the node the VIP is
    # serving right now.
    if "different email server" in low or "server_id" in low or "server id" in low:
        return (
            "That license was issued for a different Email Server ID than the "
            "one shown on this page. On an HA pair there are two ways to land "
            "here. Either the key was issued for the other node, in which case "
            "open the console on the node whose ID matches and apply it there: "
            "applying it syncs the ID, the token and the seat count to the "
            "peer, so it only has to be done once. Or the pair has drifted "
            "onto two different IDs, which a pair should never have; compare "
            "this ID with the one on the peer console, and if they differ "
            "rebuild the identity by applying a valid key on the node the "
            "licence was issued for. If neither fits, send KIN the ID shown "
            "here and ask for a licence issued against it."
        )
    if "expired" in low:
        return "That license has expired. Ask KIN for a renewed license."
    if "canonical" in low:
        return (
            "That license failed its integrity check (payload was modified "
            "after signing). Ask KIN for a freshly signed license."
        )
    if "signature is not valid" in low or "signature" in low:
        return (
            "That license signature does not match this build's KIN signing "
            "key. Ask KIN for a license signed with the current key."
        )
    if "seats" in low:
        return "That license has an invalid seat count. Ask KIN to reissue it."
    if "incomplete" in low or "unexpected fields" in low or "license type" in low:
        return "That license payload is malformed. Ask KIN to reissue it."
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "License was not accepted.")
    if line.startswith("SETTINGS_JSON:") or line.startswith("LICENSE_JSON:"):
        return "License was not accepted."
    return line[:300]


@app.post("/api/settings/firewall")
async def api_settings_firewall(
    body: FirewallApplyBody,
    request: Request,
    user: ConsoleUser = Depends(auth.require_roles(ROLE_SUPER_ADMIN)),
) -> dict[str, object]:
    """Start firewall apply (dead-man timer still runs in 10-host-firewall.sh). Prefer SSE."""
    _require_settings(user)
    result = await _collect_privhelper(
        proto.CMD_APPLY_APPLIANCE_SETTINGS,
        user.username,
        args={
            "section": "firewall",
            "admin_ips": body.admin_ips,
            "client_ip": _request_client_ipv4(request),
        },
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result.get("error") or "Could not apply trusted admin IPs"),
        )
    return {"ok": True}


@app.get("/api/monitoring/catalogue")
def monitoring_catalogue(
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """What the Monitoring tab can chart, and over which windows."""
    from . import monitoring

    return {
        "metrics": monitoring.catalogue_public(),
        "ranges": list(monitoring.RANGES),
        "default_range": monitoring.DEFAULT_RANGE,
    }


@app.get("/api/monitoring/host")
async def monitoring_host(
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """CPU model/cores, RAM in bytes, per-mount disk usage, uptime.

    Read from /proc and statvfs rather than Prometheus: node_exporter only
    exposes the CPU model behind a non-default flag from 1.4 onwards, and this
    product runs on jammy's 1.3. These are also point-in-time facts, not
    history, so a scrape round-trip buys nothing.
    """
    import asyncio

    from . import monitoring

    facts = await asyncio.to_thread(monitoring.host_facts)
    # node_exporter republishes a textfile forever after its writer dies, so a
    # stopped collector draws the same flat line as a quiet mail server. Ride
    # the file's age along with the host facts the tab already fetches.
    facts["mail_flow"] = await asyncio.to_thread(monitoring.mail_flow_freshness)
    return facts


@app.get("/api/monitoring/series")
async def monitoring_series(
    metric: str,
    range: str = "",
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Time series for one catalogued metric.

    Proxied so Prometheus itself stays bound to 127.0.0.1 and the operator
    never has a second port or a second login. The browser passes a metric
    NAME, never PromQL.
    """
    import asyncio
    import time

    from . import monitoring

    window = range or monitoring.DEFAULT_RANGE
    if not monitoring.known_metric(metric):
        raise HTTPException(status_code=400, detail=f"Unknown metric: {metric}")
    if window not in monitoring.RANGES:
        raise HTTPException(status_code=400, detail=f"Unknown range: {window}")
    try:
        series = await asyncio.to_thread(
            monitoring.fetch_range, metric, window, now=time.time()
        )
    except monitoring.MonitoringError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    seconds, step = monitoring.range_window(window)
    end = time.time()
    return {
        "range": window,
        # The window the operator asked for. The chart's x-axis must span this,
        # not just whatever samples came back - otherwise two cards showing the
        # same 6 hours can end up with completely different time axes.
        "start": end - seconds,
        "end": end,
        "step": step,
        **monitoring.metric_meta(metric),
        "series": series,
    }


@app.get("/api/monitoring/series-batch")
async def monitoring_series_batch(
    metrics: str,
    range: str = "",
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Time series for several catalogued metrics in one round trip.

    The tab draws a dozen charts and used one request per chart, refreshing on
    a 30s timer. On a host that is already struggling - which is exactly when
    an operator opens this tab - that is a burst of requests competing with
    the thing they are trying to diagnose. One request, one x-axis, and every
    series is guaranteed to cover the same window.

    A metric that fails comes back with an empty series rather than failing
    the batch: one dead query must not blank the dashboard.
    """
    import asyncio
    import time

    from . import monitoring

    window = range or monitoring.DEFAULT_RANGE
    if window not in monitoring.RANGES:
        raise HTTPException(status_code=400, detail=f"Unknown range: {window}")
    try:
        wanted = monitoring.parse_metric_list(metrics)
    except monitoring.MonitoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # One clock for the whole batch, so every chart shares an x-axis.
    end = time.time()
    seconds, step = monitoring.range_window(window)

    async def one(name: str) -> dict[str, object]:
        try:
            series = await asyncio.to_thread(
                monitoring.fetch_range, name, window, now=end
            )
        except monitoring.MonitoringError:
            series = []
        return {**monitoring.metric_meta(name), "series": series}

    charts = await asyncio.gather(*(one(n) for n in wanted))
    return {
        "range": window,
        "start": end - seconds,
        "end": end,
        "step": step,
        "charts": list(charts),
    }


# ---------------------------------------------------------------------------
# Reports.
#
# The Monitoring tab answers "what is happening now". These answer "what
# happened last month" - the question somebody asks once a month and otherwise
# has to leave the console to answer, or cannot answer at all. Read-only: they
# run range queries through the same loopback Prometheus proxy and touch
# nothing else.
# ---------------------------------------------------------------------------


@app.get("/api/reports/mail")
async def reports_mail(
    period: str = "",
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Mail totals and a per-day breakdown for a named period."""
    import asyncio

    from . import reporting

    want = period or reporting.DEFAULT_PERIOD
    if want not in reporting.PERIODS:
        raise HTTPException(status_code=400, detail=f"Unknown period: {want}")
    try:
        report = await asyncio.to_thread(reporting.build_report, want)
    except reporting.MonitoringError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # A month of zeroes because the collector died on day three is worse than
    # no report at all, because it looks like an answer.
    from . import monitoring

    report["mail_flow"] = await asyncio.to_thread(monitoring.mail_flow_freshness)
    return report


@app.get("/api/reports/mail.csv")
async def reports_mail_csv(
    period: str = "",
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> Response:
    """The same report as a spreadsheet: one row per day."""
    import asyncio
    from datetime import datetime

    from . import reporting

    want = period or reporting.DEFAULT_PERIOD
    if want not in reporting.PERIODS:
        raise HTTPException(status_code=400, detail=f"Unknown period: {want}")
    try:
        report = await asyncio.to_thread(reporting.build_report, want)
    except reporting.MonitoringError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    body = reporting.to_csv(
        list(report.get("daily") or []), reporting.report_csv_columns()
    )
    name = reporting.csv_filename(
        f"kin-mail-report-{want}",
        datetime.fromisoformat(str(report["start"])),
        datetime.fromisoformat(str(report["end"])),
    )
    return _csv_response(body, name)


@app.get("/api/monitoring/series.csv")
async def monitoring_series_csv(
    metrics: str,
    range: str = "",
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> Response:
    """Raw samples for the charted metrics, long format.

    The operator asked to take the data "from raw to finished" elsewhere, so
    this is the samples as scraped rather than anything pre-aggregated.
    """
    import asyncio
    import time

    from . import monitoring, reporting

    window = range or monitoring.DEFAULT_RANGE
    if window not in monitoring.RANGES:
        raise HTTPException(status_code=400, detail=f"Unknown range: {window}")
    try:
        wanted = monitoring.parse_metric_list(metrics)
    except monitoring.MonitoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    end = time.time()

    async def one(name: str) -> dict[str, object]:
        try:
            series = await asyncio.to_thread(
                monitoring.fetch_range, name, window, now=end
            )
        except monitoring.MonitoringError:
            series = []
        return {**monitoring.metric_meta(name), "series": series}

    charts = await asyncio.gather(*(one(n) for n in wanted))
    body = reporting.series_csv(list(charts))
    return _csv_response(body, reporting.csv_filename(f"kin-mail-metrics-{window}"))


def _csv_response(body: str, filename: str) -> Response:
    """CSV with a BOM, so Excel opens UTF-8 correctly.

    Without it Excel reads a UTF-8 CSV as the local 8-bit codepage and mangles
    any non-ASCII in it. The BOM is invisible to every other reader.
    """
    return Response(
        content="\ufeff" + body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # A report is a point-in-time answer; a cached one is a wrong one.
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/monitoring/overview")
async def monitoring_overview(
    _user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Current value of every catalogued metric, for the gauge row."""
    import asyncio

    from . import monitoring

    async def one(name: str) -> tuple[str, object]:
        try:
            return name, await asyncio.to_thread(monitoring.fetch_instant, name)
        except monitoring.MonitoringError:
            # One dead metric must not blank the whole dashboard.
            return name, []

    pairs = await asyncio.gather(*(one(n) for n in monitoring.CATALOGUE))
    values = dict(pairs)
    available = any(values[n] for n in values)
    return {
        "available": available,
        "metrics": [
            {**monitoring.metric_meta(n), "values": values[n]} for n in monitoring.CATALOGUE
        ],
    }


@app.get("/api/cluster/ops-log")
async def cluster_ops_log(
    user: ConsoleUser = Depends(auth.require_console_user),
) -> dict[str, object]:
    """Durable transcript of cluster operations, for the Activity log tab.

    /api/cluster/status returns the output of the status probe it just ran,
    which is a snapshot and not a record: seeding the Activity log from it
    meant a refresh replaced the Move Master transcript with three lines of
    current state.
    """
    if not command_allowed(user.role, proto.CMD_MAINTENANCE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=deny_message(user.role, proto.CMD_MAINTENANCE),
        )
    result = await _collect_privhelper(
        proto.CMD_MAINTENANCE,
        user.username,
        args={"op": "opslog"},
    )
    return {
        "ok": bool(result.get("ok")),
        "exit_code": result.get("exit_code"),
        "log": result.get("log") or "",
    }


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
