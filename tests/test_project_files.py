from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.domain import Project


@pytest.mark.asyncio
async def test_project_files_are_membership_gated_and_pinned_to_canonical_state() -> None:
    project = Project(uuid4(), "Tin POC", "projects/tin-poc", "main")
    revision = "c" * 40
    markdown = b"# Canonical report\n\nShared project state.\n"

    class Database:
        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

        async def get_project(self, project_id):
            return project if project_id == project.id else None

    class Storage:
        async def list_canonical_files(self, *, repo_id, branch):
            assert repo_id == project.state_repo_id
            assert branch == project.canonical_branch
            return ["README.md", "reports/SCAN.md", "reports/evidence.json"], revision

        async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
            assert repo_id == project.state_repo_id
            assert commit_sha == revision
            assert path == "reports/SCAN.md"
            return markdown

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.settings = SimpleNamespace(
        clerk_publishable_key="pk_test_test",
        clerk_frontend_api_url="https://clerk.test",
    )
    app.state.runtime = SimpleNamespace(database=Database(), storage=Storage())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get(f"/api/projects/{project.id}/files")
        raw = await client.get(
            f"/api/projects/{project.id}/files/raw",
            params={"path": "reports/SCAN.md", "revision": revision},
        )
        document = await client.get(
            f"/api/projects/{project.id}/files/document",
            params={"path": "reports/SCAN.md", "revision": revision},
        )
        unsafe = await client.get(
            f"/api/projects/{project.id}/files/raw",
            params={"path": "../secret", "revision": revision},
        )
        noncanonical = await client.get(
            f"/api/projects/{project.id}/files/raw",
            params={"path": "reports/SCAN.md", "revision": "e" * 40},
        )
        inaccessible = await client.get(f"/api/projects/{uuid4()}/files")

    assert listing.status_code == 200
    assert listing.json() == {
        "project_id": str(project.id),
        "revision": revision,
        "files": [
            {"path": "README.md"},
            {"path": "reports/SCAN.md"},
            {"path": "reports/evidence.json"},
        ],
    }
    assert raw.status_code == 200
    assert raw.content == markdown
    assert raw.headers["X-Tin-File-Source"] == "code.storage"
    assert raw.headers["X-Tin-File-Revision"] == revision
    assert raw.headers["Content-Disposition"].startswith("inline;")
    assert document.status_code == 200
    assert document.headers["X-Tin-File-Revision"] == revision
    assert document.json()["path"] == "reports/SCAN.md"
    assert document.json()["revision"] == revision
    assert document.json()["size_bytes"] == len(markdown)
    assert unsafe.status_code == 422
    assert noncanonical.status_code == 404
    assert inaccessible.status_code == 404


def test_a_project_file_that_holds_a_credential_is_refused_before_commit() -> None:
    """Context the founder hands over is read by every run; a token in it is a leak."""
    from tin_lite.project_files import credential_findings, normalize_project_file_mutations

    clean = (
        "# Positioning note\n\nSource: pasted by the founder, 2026-09-18.\n\n"
        "We sell to agencies; the token of trust is a working reply within a minute. "
        "Password resets are the top support question.\n"
    )
    assert credential_findings(clean) == []
    assert normalize_project_file_mutations(
        [{"operation": "upsert", "path": "context/positioning.md", "content": clean}]
    )

    leaks = {
        "a private key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n",
        "an AWS access key": "aws key AKIAIOSFODNN7EXAMPLE in the deploy notes",
        "a GitHub token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + " works",
        "a Slack token": "bot xoxb-1234567890-abcdefghij",
        "a Stripe or Clerk secret key": "STRIPE=sk_live_" + "A" * 24,
        "an OpenAI key": "OPENAI_API_KEY=sk-proj-" + "z" * 40,
        "a Google API key": "AIza" + "S" * 35,
        "a secret assignment": "client_secret: " + "q" * 32,
    }
    for kind, text in leaks.items():
        assert credential_findings(text) == [kind] or kind in credential_findings(text), kind
        with pytest.raises(ValueError, match="appears to contain"):
            normalize_project_file_mutations(
                [{"operation": "upsert", "path": "context/notes.md", "content": text}]
            )


@pytest.mark.parametrize("key", ["password", "access_token", "client_secret", "api-key"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_secret_assignments_are_refused_without_echoing_the_value(key, quote):
    from tin_lite.project_files import credential_findings, normalize_project_file_mutations

    value = "synthetic.credential.value-123456789"
    text = f"{{{quote}{key}{quote}: {quote}{value}{quote}}}"
    assert "a secret assignment" in credential_findings(text)
    with pytest.raises(ValueError, match="appears to contain") as error:
        normalize_project_file_mutations(
            [{"operation": "upsert", "path": "context/settings.md", "content": text}]
        )
    assert value not in str(error.value)
