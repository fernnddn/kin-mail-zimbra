"""KIN Mail admin console FastAPI application."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kin_privhelper import protocol as proto

from . import auth, draft
from .privhelper_client import run_command
from .settings import settings

app = FastAPI(title="KIN Mail Console", docs_url=None, redoc_url=None, openapi_url=None)

EULA_COOKIE = "kin_console_eula"
EULA_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.on_event("startup")
def _startup() -> None:
    auth.ensure_session_secret()
    if not auth.password_hash_exists():
        # Bootstrap must create the hash before start; refuse anonymous first-boot in-process.
        raise RuntimeError(
            f"Missing admin password hash at {settings.password_hash_file}. "
            "Run console/bootstrap.sh first."
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "kin-mail-console"}


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
def login(body: LoginBody, response: Response) -> dict[str, str]:
    if body.username != settings.console_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    try:
        stored = auth.load_password_hash()
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth store unavailable") from exc
    if not auth.verify_password(body.password, stored):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    auth.set_session_cookie(response, body.username)
    return {"status": "ok", "username": body.username}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, str]:
    auth.clear_session_cookie(response)
    return {"status": "ok"}


@app.get("/api/me")
def me(username: str = Depends(auth.require_user)) -> dict[str, str]:
    return {"username": username}


# --- Wizard draft (auth required; never applies to the live installer) ------


@app.get("/api/wizard/draft")
def get_wizard_draft(_user: str = Depends(auth.require_user)) -> dict:
    return draft.public_draft(draft.load_draft())


@app.put("/api/wizard/draft")
def put_wizard_draft(
    body: draft.DraftPatch,
    _user: str = Depends(auth.require_user),
) -> dict:
    current = draft.load_draft()
    updated = draft.apply_patch(current, body)
    # Light validation for known enums — store anyway if empty (in-progress draft).
    if updated.topology and updated.topology not in ("1vm", "2vm"):
        raise HTTPException(status_code=400, detail="topology must be 1vm or 2vm")
    if updated.tls_method and updated.tls_method not in ("cloudflare", "manual", "customer"):
        raise HTTPException(status_code=400, detail="tls_method invalid")
    saved = draft.save_draft(updated)
    return draft.public_draft(saved)


# --- Privileged execution via local privhelper (SSE) -------------------------

# Browser may only pick these action names; each maps to a fixed whitelist cmd.
_STREAM_ACTIONS: dict[str, str] = {
    "hardening_status": proto.CMD_RUN_HARDENING_STATUS,
    "apply_draft": proto.CMD_APPLY_WIZARD_DRAFT,
    "run_hardening": proto.CMD_RUN_HARDENING,
    "get_status": proto.CMD_GET_STATUS,
}


@app.get("/api/wizard/deploy/stream")
async def wizard_deploy_stream(
    request: Request,
    username: str = Depends(auth.require_user),
) -> StreamingResponse:
    """Stream a whitelisted privhelper command into the log viewer.

    Query `action` is an enum of safe aliases — never a free-form shell/command string.
    """
    action = (request.query_params.get("action") or "hardening_status").strip()
    cmd = _STREAM_ACTIONS.get(action)
    if not cmd:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {action!r}; allowed: {', '.join(sorted(_STREAM_ACTIONS))}",
        )

    async def event_gen():
        yield f"data: {json.dumps({'type': 'meta', 'cmd': cmd, 'action': action})}\n\n"
        async for ev in run_command(
            cmd,
            username,
            socket_path=settings.privhelper_socket,
        ):
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
async def wizard_deploy_hint(_user: str = Depends(auth.require_user)) -> dict[str, object]:
    """Compat: prefer SSE /api/wizard/deploy/stream?action=… for live output."""
    return {
        "status": "use_stream",
        "message": "Use GET /api/wizard/deploy/stream?action=…",
        "actions": sorted(_STREAM_ACTIONS.keys()),
        "commands": {k: v for k, v in _STREAM_ACTIONS.items()},
    }


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
