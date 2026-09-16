"""Setup wizard router — OAuth login flow and resource discovery for local dev."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/setup")

# In-memory session store for pending OAuth logins (cleaned up after completion
# or timeout).  Not persisted — restart clears all sessions, which is fine
# because the browser will re-poll with a session_id it sent itself.
_sessions: dict[str, dict] = {}

_SESSION_MAX_AGE = 300  # 5 minutes
_SESSION_MAX_COUNT = 50
_STALE_SESSION_AGE = 600  # 10 minutes — aggressive cleanup threshold


def _find_cli() -> str:
    """Return the first Databricks CLI executable found on known paths."""
    for path in [
        "/opt/homebrew/bin/databricks",
        "/usr/local/bin/databricks",
    ]:
        if os.path.exists(path):
            return path
    return "databricks"


def _maybe_cleanup_sessions() -> None:
    """Remove stale sessions when the store grows too large."""
    if len(_sessions) <= _SESSION_MAX_COUNT:
        return
    now = time.time()
    stale = [
        sid
        for sid, s in list(_sessions.items())
        if now - s.get("started", 0) > _STALE_SESSION_AGE
    ]
    for sid in stale:
        try:
            _sessions[sid]["proc"].terminate()
        except Exception:
            pass
        del _sessions[sid]


class StartLoginRequest(BaseModel):
    host: str


@router.post("/start-login")
async def start_login(body: StartLoginRequest):
    """Start a `databricks auth login` subprocess and return a session id.

    The CLI will open the user's browser for OAuth.  The frontend polls
    ``/api/setup/login-status/{session_id}`` until the flow completes and
    the token is available.
    """
    _maybe_cleanup_sessions()

    host = body.host.rstrip("/")
    if not re.match(r"^https?://", host):
        raise HTTPException(400, "host must start with https://")

    session_id = str(uuid.uuid4())
    cli = _find_cli()

    env = {**os.environ, "DATABRICKS_AUTH_STORAGE": "plaintext"}

    try:
        proc = subprocess.Popen(
            [cli, "auth", "login", "--host", host],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise HTTPException(
            500,
            "Databricks CLI not found. Install it with: brew install databricks/tap/databricks",
        )

    _sessions[session_id] = {
        "proc": proc,
        "host": host,
        "env": env,
        "started": time.time(),
        "stdout": "",
        "stderr": "",
    }

    # Give the CLI 2 seconds to emit the auth URL before returning.
    time.sleep(2)
    try:
        stdout = proc.stdout.read(4096) if proc.stdout else ""
        stderr = proc.stderr.read(4096) if proc.stderr else ""
        _sessions[session_id]["stdout"] = stdout
        _sessions[session_id]["stderr"] = stderr
    except Exception:
        pass

    # Try to surface the browser URL so the frontend can show a clickable link.
    combined = _sessions[session_id]["stdout"] + _sessions[session_id]["stderr"]
    auth_url: str | None = None
    url_match = re.search(r"https?://[^\s]+", combined)
    if url_match:
        auth_url = url_match.group(0)

    return {
        "session_id": session_id,
        "auth_url": auth_url,
        "message": "Login started — check your browser",
    }


@router.get("/login-status/{session_id}")
async def login_status(session_id: str):
    """Poll this endpoint until status is 'complete' or 'error'."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    proc = session["proc"]
    retcode = proc.poll()

    if retcode is None:
        # Still running — check for timeout.
        if time.time() - session["started"] > _SESSION_MAX_AGE:
            proc.terminate()
            del _sessions[session_id]
            return {"status": "error", "error": "Login timed out"}
        return {"status": "pending"}

    if retcode != 0:
        stderr = session.get("stderr", "")
        try:
            remaining = proc.stderr.read() if proc.stderr else ""
            stderr = stderr + remaining
        except Exception:
            pass
        del _sessions[session_id]
        return {"status": "error", "error": f"Login failed: {stderr[:200]}"}

    # OAuth completed — retrieve the token immediately.
    host = session["host"]
    env = session.get("env") or {**os.environ, "DATABRICKS_AUTH_STORAGE": "plaintext"}
    del _sessions[session_id]

    try:
        result = subprocess.run(
            [_find_cli(), "auth", "token", "--host", host],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            token = (
                data.get("access_token")
                or data.get("token")
                or data.get("Token", "")
            )
            if token:
                return {"status": "complete", "token": token}
        return {"status": "error", "error": f"Could not retrieve token: {result.stderr.strip()[:200]}"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@router.get("/resources")
async def get_resources(request: Request):
    """Return warehouses, catalogs, and available LLM models for the wizard."""
    token = request.headers.get("x-forwarded-access-token") or request.headers.get(
        "authorization", ""
    ).removeprefix("Bearer ")
    host = request.headers.get("x-databricks-host") or os.environ.get(
        "DATABRICKS_HOST", ""
    )

    if not token or not host:
        raise HTTPException(401, "Missing auth token or host")

    try:
        from databricks.sdk import WorkspaceClient

        ws = WorkspaceClient(host=host, token=token)

        warehouses: list[dict] = []
        try:
            for wh in ws.warehouses.list():
                warehouses.append({"id": wh.id, "name": wh.name})
        except Exception:
            pass

        catalogs: list[str] = []
        try:
            for cat in ws.catalogs.list():
                if cat.name:
                    catalogs.append(cat.name)
        except Exception:
            pass

        models: list[dict] = []
        try:
            from backend.services.model_catalog import CHAT_MODELS

            models = [{"id": m, "name": m} for m in CHAT_MODELS]
        except Exception:
            models = [
                {
                    "id": "databricks-claude-sonnet-4-6",
                    "name": "databricks-claude-sonnet-4-6",
                },
                {
                    "id": "databricks-meta-llama-3-3-70b-instruct",
                    "name": "databricks-meta-llama-3-3-70b-instruct",
                },
            ]

        return {"warehouses": warehouses, "catalogs": catalogs, "models": models}
    except Exception as exc:
        raise HTTPException(500, str(exc))
