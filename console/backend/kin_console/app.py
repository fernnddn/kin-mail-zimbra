"""KIN Mail admin console FastAPI application."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .settings import settings

app = FastAPI(title="KIN Mail Console", docs_url=None, redoc_url=None, openapi_url=None)


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


def _spa_index() -> Path:
    return settings.static_dir / "index.html"


@app.get("/")
def root(request: Request) -> Response:
    # Always serve SPA shell; React router enforces login client-side, API enforces server-side.
    index = _spa_index()
    if not index.is_file():
        return JSONResponse({"detail": "Frontend not installed"}, status_code=503)
    return FileResponse(index)


# Authenticated API only beyond health/login/logout/me — SPA assets are public HTML/JS
# but every privileged action goes through /api/* with require_user.

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
