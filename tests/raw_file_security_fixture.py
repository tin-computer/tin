"""Loopback-only browser fixture: real Tin routes/Clerk verification, synthetic state."""

from __future__ import annotations

import base64
import json
import socket
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from uuid import UUID

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from tin_lite.api import router
from tin_lite.auth import AuthContext, ClerkAuth, require_user
from tin_lite.domain import Project, RunStatus, WorkflowRun
from tin_lite.publication import OutputCheckpoint

REVISION = "a" * 40
PROJECT_ID = UUID("00000000-0000-4000-8000-000000000001")
ASSETS = Path(__file__).resolve().parents[1] / "src/tin_lite/static"


def fixture(origin: str):
    project = Project(PROJECT_ID, "Security fixture", "projects/synthetic", "main")
    script = (
        "window.attackExecuted = true;"
        f"fetch('{origin}/__test/private', {{credentials: 'include'}})"
        ".then(r => r.ok ? r.text() : Promise.reject()).then(secret => "
        f"fetch('{origin}/__test/stolen', {{method: 'POST', body: secret}})).catch(() => {{}});"
    )
    html = f"<!doctype html><title>Untrusted</title><script>{script}</script>"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
        '<rect width="24" height="24" fill="red"/>'
        f"<script>{script}</script></svg>"
    )
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    files = {
        "attack.html": html.encode(),
        "attack.SVG": svg.encode(),
        "attack.xhtml": html.encode(),
        "attack.xml": svg.encode(),
        "unknown.bin": html.encode(),
        "disguised.png": svg.encode(),
        "notes.md": b"# Source\n\n<script>window.attackExecuted = true</script>\n",
        "safe.png": png,
        "notes.txt": html.encode(),
    }
    runs = {}
    for number, (name, content) in enumerate(files.items(), 10):
        run = WorkflowRun(
            id=UUID(int=number),
            project_id=project.id,
            workflow_id=UUID(int=2),
            executor="codex.procedure",
            definition_commit_sha=REVISION,
            temporal_workflow_id="synthetic",
            thread_id="synthetic",
            generation=1,
            fencing_token=1,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha=REVISION,
            expected_head_sha=REVISION,
            artifact_path=name,
        )
        if name == "attack.SVG":
            checkpoint = OutputCheckpoint.create(
                run=run,
                revision=REVISION,
                path=name,
                media_type="image/svg+xml",
                content=content,
            )
            run = replace(
                run, retained_output={**checkpoint.to_dict(), "reason": "output_conflict"}
            )
        runs[run.id] = run

    class Database:
        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_Synthetic"

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def get_run(self, run_id):
            return runs.get(run_id)

        async def record_tin_user(self, user_id):
            assert user_id == "user_Synthetic"

    class Storage:
        async def canonical_file_modified_dates(self, **kwargs):
            return {}

        async def list_canonical_files(self, **kwargs):
            return list(files), REVISION

        async def read_canonical_artifact(self, *, path, commit_sha, **kwargs):
            assert commit_sha == REVISION
            return files[path]

        async def read_procedure_checkpoint(self, *, path, revision, **kwargs):
            assert revision == REVISION
            return files[path]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user_Synthetic",
            "sid": "sess_Synthetic",
            "azp": origin,
            "iss": "https://clerk.synthetic.test",
            "iat": now,
            "nbf": now - 10,
            "exp": now + 600,
        },
        key,
        algorithm="RS256",
        headers={"kid": "synthetic"},
    )
    identity = object.__new__(ClerkAuth)
    identity._secret_key = "synthetic-only"  # noqa: SLF001, S105
    identity._jwt_key = public  # noqa: SLF001
    identity._authorized_parties = [origin]  # noqa: SLF001
    app = FastAPI()
    app.state.auth = identity
    app.state.runtime = SimpleNamespace(database=Database(), storage=Storage())
    app.state.settings = SimpleNamespace()
    app.state.stolen = []

    @app.get("/")
    async def shell():
        text = ASSETS.joinpath("index.html").read_text()
        # Only the external Clerk script is stubbed. API cookies and bearer tokens
        # still pass through the actual SDK verifier and membership checks.
        import re

        text = re.sub(r'<script\b[^>]*src="\{\{CLERK[^>]+>[\s\S]*?</script>', "", text)
        return HTMLResponse(text.replace("{{ASSET_VERSION}}", "security-test"))

    @app.get("/__test/session")
    async def session():
        response = Response(token)
        response.set_cookie("__session", token, httponly=True, samesite="lax")
        return response

    @app.get("/__test/private")
    async def private(_user: Annotated[AuthContext, Depends(require_user)]):
        return Response("synthetic-other-project-secret")

    @app.post("/__test/stolen")
    async def stolen(request: Request):
        app.state.stolen.append((await request.body()).decode())
        return Response()

    @app.get("/__test/stats")
    async def stats():
        return app.state.stolen

    @app.post("/__test/reset")
    async def reset():
        app.state.stolen.clear()
        return Response()

    @app.get("/__test/unsafe")
    async def unsafe():
        # Positive control: demonstrates that cookie-backed theft succeeds if
        # the old inline response is restored. Exists only in this test server.
        return HTMLResponse(html)

    @app.get("/api/projects")
    async def projects(_user: Annotated[AuthContext, Depends(require_user)]):
        return [
            {
                "id": str(project.id),
                "name": project.name,
                "workspace_id": "synthetic",
                "workspace_name": "Synthetic",
                "member_count": 1,
                "timezone": "UTC",
            }
        ]

    app.mount("/assets", StaticFiles(directory=ASSETS))
    app.include_router(router)
    return app, token, files, runs


if __name__ == "__main__":
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app, _, files, runs = fixture(origin)
    print(
        json.dumps(
            {
                "origin": origin,
                "project": str(PROJECT_ID),
                "revision": REVISION,
                "runs": {run.artifact_path: str(run.id) for run in runs.values()},
            }
        ),
        flush=True,
    )
    uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])
