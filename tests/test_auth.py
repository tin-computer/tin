from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, status
from test_procedure_publication import publication_db as publication_db

import tin_lite.auth as auth_module
from tin_lite.api import router
from tin_lite.auth import AuthContext, ClerkAuth
from tin_lite.domain import Project, ProjectInvitation, ProjectMembership, Workspace

USER_A = "user_Alpha123"
USER_B = "user_Beta456"
USER_WRONG = "user_Wrong789"


def session_token(payload: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.c3ludGhldGlj"


@pytest.mark.asyncio
async def test_backend_session_continuation_skips_only_the_absent_azp_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = session_token({"sub": USER_A, "sid": "sess_a"})
    calls: list[list[str] | None] = []

    def authenticate(request, options):
        del request
        calls.append(options.authorized_parties)
        if options.authorized_parties:
            return SimpleNamespace(is_signed_in=False, payload={}, token=None)
        return SimpleNamespace(
            is_signed_in=True,
            payload={"sub": USER_A, "sid": "sess_a"},
            token=token,
        )

    monkeypatch.setattr(auth_module, "authenticate_request", authenticate)
    identity = object.__new__(ClerkAuth)
    identity._secret_key = "test-secret"  # noqa: S105, SLF001
    identity._jwt_key = None  # noqa: SLF001
    identity._authorized_parties = ["https://lite.tin.computer"]  # noqa: SLF001

    context = await identity.authenticate_session(
        SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    )

    assert context.clerk_user_id == USER_A
    assert calls == [["https://lite.tin.computer"], None]


@pytest.mark.asyncio
async def test_foreign_browser_azp_never_uses_the_continuation_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = session_token({"sub": USER_A, "sid": "sess_a", "azp": "https://foreign.test"})
    calls: list[list[str] | None] = []

    def authenticate(request, options):
        del request
        calls.append(options.authorized_parties)
        return SimpleNamespace(is_signed_in=False, payload={}, token=None)

    monkeypatch.setattr(auth_module, "authenticate_request", authenticate)
    identity = object.__new__(ClerkAuth)
    identity._secret_key = "test-secret"  # noqa: S105, SLF001
    identity._jwt_key = None  # noqa: SLF001
    identity._authorized_parties = ["https://lite.tin.computer"]  # noqa: SLF001

    with pytest.raises(HTTPException) as error:
        await identity.authenticate_session(
            SimpleNamespace(headers={"authorization": f"Bearer {token}"})
        )

    assert error.value.status_code == 401
    assert calls == [["https://lite.tin.computer"]]


@pytest.mark.asyncio
async def test_oauth_token_uses_clerks_format_agnostic_verification_endpoint() -> None:
    async def verify(request: httpx.Request) -> httpx.Response:
        assert request.url == ("https://api.clerk.com/oauth_applications/access_tokens/verify")
        assert request.headers["authorization"] == "Bearer test-secret"
        assert json.loads(request.content) == {"access_token": "jwt-or-opaque-token"}
        return httpx.Response(
            200,
            json={
                "id": "oat_test",
                "client_id": "client_codex",
                "subject": USER_A,
                "scopes": ["openid"],
                "revoked": False,
                "expired": False,
                "expires_at": 1_800_000_000,
            },
        )

    identity = object.__new__(ClerkAuth)
    identity._secret_key = "test-secret"  # noqa: S105, SLF001
    identity._oauth_client_ids = frozenset({"client_codex"})  # noqa: SLF001
    identity._oauth_resource = "https://tin.test/mcp"  # noqa: SLF001
    identity._oauth_issuer = "https://clerk.tin.test"  # noqa: SLF001
    identity._client = httpx.AsyncClient(  # noqa: SLF001
        headers={"Authorization": "Bearer test-secret"},
        transport=httpx.MockTransport(verify),
    )
    try:
        context = await identity.authenticate_oauth_token("jwt-or-opaque-token")
    finally:
        await identity.close()

    assert context is not None
    assert context.clerk_user_id == USER_A
    assert context.client_id == "client_codex"
    assert context.scopes == frozenset({"openid"})
    assert context.expires_at == 1_800_000_000
    assert context.resource == "https://tin.test/mcp"


@pytest.mark.asyncio
async def test_oauth_token_rejects_clerk_verification_failure_or_revocation() -> None:
    async def verify(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["access_token"]  # noqa: S105
        if token == "revoked-token":  # noqa: S105
            return httpx.Response(
                200,
                json={"subject": USER_A, "scopes": ["openid"], "revoked": True},
            )
        return httpx.Response(401, json={"error": "invalid token"})

    identity = object.__new__(ClerkAuth)
    identity._secret_key = "test-secret"  # noqa: S105, SLF001
    identity._oauth_client_ids = frozenset({"client_codex"})  # noqa: SLF001
    identity._client = httpx.AsyncClient(  # noqa: SLF001
        headers={"Authorization": "Bearer test-secret"},
        transport=httpx.MockTransport(verify),
    )
    try:
        invalid = await identity.authenticate_oauth_token("invalid-token")
        revoked = await identity.authenticate_oauth_token("revoked-token")
    finally:
        await identity.close()

    assert invalid is None
    assert revoked is None


class FakeIdentity:
    def __init__(self) -> None:
        self.emails = {
            USER_A: frozenset({"a@example.com"}),
            USER_B: frozenset({"b@example.com"}),
            USER_WRONG: frozenset({"wrong@example.com"}),
        }

    async def authenticate_session(self, request) -> AuthContext:
        authorization = request.headers.get("authorization", "")
        user_id = {
            "Bearer session-a": USER_A,
            "Bearer session-b": USER_B,
            "Bearer session-wrong": USER_WRONG,
        }.get(authorization)
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            )
        return AuthContext(
            clerk_user_id=user_id,
            token_type="session_token",  # noqa: S106
            session_id=f"sess_{user_id}",
            raw_token=authorization.removeprefix("Bearer "),  # noqa: S106
        )

    async def verified_email_addresses(self, clerk_user_id: str) -> frozenset[str]:
        return self.emails[clerk_user_id]


class FakeStorage:
    def __init__(self) -> None:
        self.repos: dict[str, str] = {}

    async def ensure_repo(self, repo_id: str, *, initial_readme: str):
        self.repos.setdefault(repo_id, initial_readme)
        return SimpleNamespace(id=repo_id)


class FailingStorage:
    async def ensure_repo(self, repo_id: str, *, initial_readme: str):
        del repo_id, initial_readme
        raise RuntimeError("storage unavailable")


class FakeAuthDatabase:
    def __init__(self) -> None:
        self.workspace = Workspace(uuid4(), "Checkpoint workspace", USER_A)
        self.other_workspace = Workspace(uuid4(), "Other workspace", None)
        self.project = Project(
            uuid4(),
            "Checkpoint A",
            "projects/checkpoint-a",
            "main",
            workspace_id=self.workspace.id,
            workspace_name=self.workspace.name,
        )
        self.other_project = Project(
            uuid4(),
            "Other",
            "projects/other",
            "main",
            workspace_id=self.other_workspace.id,
            workspace_name=self.other_workspace.name,
        )
        self.members: dict[UUID, set[str]] = {
            self.project.id: {USER_A},
            self.other_project.id: set(),
        }
        self.workspaces: dict[UUID, Workspace] = {
            self.workspace.id: self.workspace,
            self.other_workspace.id: self.other_workspace,
        }
        self.workspace_members: dict[UUID, set[str]] = {
            self.workspace.id: {USER_A},
            self.other_workspace.id: set(),
        }
        self.recorded_users: list[str] = []
        self.invitations: dict[str, ProjectInvitation] = {}
        self.personal_projects: dict[UUID, Project] = {}
        self.created_projects: dict[tuple[UUID, UUID], Project] = {}

    def all_projects(self) -> tuple[Project, ...]:
        return (self.project, self.other_project, *self.personal_projects.values())

    async def record_tin_user(self, clerk_user_id: str) -> None:
        self.recorded_users.append(clerk_user_id)

    async def list_projects_for_user(self, clerk_user_id: str) -> list[Project]:
        return [
            replace(
                project,
                can_create_project_in_workspace=(
                    clerk_user_id in self.workspace_members.get(project.workspace_id, set())
                ),
                member_count=len(self.members.get(project.id, set())),
            )
            for project in self.all_projects()
            if clerk_user_id in self.members[project.id]
        ]

    async def list_workspaces_for_user(self, clerk_user_id: str) -> list[Workspace]:
        return [
            workspace
            for workspace in self.workspaces.values()
            if clerk_user_id in self.workspace_members[workspace.id]
        ]

    async def has_workspace_access(self, *, workspace_id: UUID, clerk_user_id: str) -> bool:
        return clerk_user_id in self.workspace_members.get(workspace_id, set())

    async def bootstrap_personal_project(
        self,
        *,
        workspace_id: UUID,
        workspace_name: str,
        project_id: UUID,
        name: str,
        state_repo_id: str,
        clerk_user_id: str,
        reuse_existing: bool,
    ) -> Project:
        existing = await self.list_projects_for_user(clerk_user_id)
        if not reuse_existing:
            existing = [project for project in existing if project.id == project_id]
        if existing:
            return existing[0]
        workspace = self.workspaces.setdefault(
            workspace_id,
            Workspace(workspace_id, workspace_name, clerk_user_id),
        )
        self.workspace_members.setdefault(workspace_id, set()).add(clerk_user_id)
        project = self.personal_projects.setdefault(
            project_id,
            Project(
                project_id,
                name,
                state_repo_id,
                "main",
                workspace_id=workspace_id,
                workspace_name=workspace.name,
                can_create_project_in_workspace=True,
                created_by_clerk_user_id=clerk_user_id,
            ),
        )
        self.members.setdefault(project_id, set()).add(clerk_user_id)
        return project

    async def create_workspace_project(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID,
        name: str,
        state_repo_id: str,
        clerk_user_id: str,
        request_id: UUID,
    ) -> Project:
        if not await self.has_workspace_access(
            workspace_id=workspace_id,
            clerk_user_id=clerk_user_id,
        ):
            raise LookupError("workspace not found")
        key = (workspace_id, request_id)
        project = self.created_projects.setdefault(
            key,
            Project(
                project_id,
                name,
                state_repo_id,
                "main",
                workspace_id=workspace_id,
                workspace_name=self.workspaces[workspace_id].name,
                can_create_project_in_workspace=True,
                created_by_clerk_user_id=clerk_user_id,
            ),
        )
        self.personal_projects[project.id] = project
        self.members.setdefault(project.id, set()).add(clerk_user_id)
        return project

    async def has_project_access(self, *, project_id: UUID, clerk_user_id: str) -> bool:
        return clerk_user_id in self.members.get(project_id, set())

    async def get_project(self, project_id: UUID) -> Project | None:
        return next(
            (project for project in self.all_projects() if project.id == project_id),
            None,
        )

    async def list_runs(self, *, project_id: UUID, limit: int):
        raise AssertionError(f"unauthorized run list executed for {project_id=} {limit=}")

    async def list_project_members(self, project_id: UUID) -> list[ProjectMembership]:
        now = datetime.now(UTC)
        return [
            ProjectMembership(project_id, clerk_user_id, now)
            for clerk_user_id in sorted(self.members[project_id])
        ]

    async def create_project_invitation(
        self,
        *,
        project_id: UUID,
        email: str,
        token_hash: str,
        created_by_clerk_user_id: str,
        expires_at: datetime,
    ) -> ProjectInvitation:
        invitation = ProjectInvitation(
            id=uuid4(),
            project_id=project_id,
            project_name=self.project.name,
            email=email,
            created_by_clerk_user_id=created_by_clerk_user_id,
            accepted_by_clerk_user_id=None,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
            accepted_at=None,
            revoked_at=None,
        )
        self.invitations[token_hash] = invitation
        return invitation

    async def get_project_invitation(self, token_hash: str) -> ProjectInvitation | None:
        return self.invitations.get(token_hash)

    async def accept_project_invitation(
        self,
        *,
        token_hash: str,
        clerk_user_id: str,
        expected_email: str,
    ) -> ProjectInvitation:
        invitation = self.invitations[token_hash]
        assert invitation.email == expected_email
        self.members[invitation.project_id].add(clerk_user_id)
        accepted = replace(
            invitation,
            accepted_by_clerk_user_id=clerk_user_id,
            accepted_at=datetime.now(UTC),
        )
        self.invitations[token_hash] = accepted
        return accepted


def auth_app(database: FakeAuthDatabase) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=database, storage=FakeStorage())
    app.state.auth = FakeIdentity()
    app.state.settings = SimpleNamespace(switchboard_public_url="https://tin.test")
    return app


@pytest.mark.asyncio
async def test_product_api_rejects_an_unauthenticated_request() -> None:
    database = FakeAuthDatabase()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app(database)),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/projects")

    assert response.status_code == 401
    assert database.recorded_users == []


@pytest.mark.asyncio
async def test_project_access_is_scoped_and_does_not_disclose_other_projects() -> None:
    database = FakeAuthDatabase()
    headers = {"Authorization": "Bearer session-a"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app(database)),
        base_url="http://test",
        headers=headers,
    ) as client:
        projects = await client.get("/api/projects")
        forbidden = await client.get(f"/api/projects/{database.other_project.id}/runs")

    assert [item["id"] for item in projects.json()] == [str(database.project.id)]
    assert forbidden.status_code == 404
    assert database.recorded_users == [USER_A, USER_A]


@pytest.mark.asyncio
async def test_projectless_user_bootstraps_one_durable_personal_project() -> None:
    database = FakeAuthDatabase()
    app = auth_app(database)
    headers = {"Authorization": "Bearer session-b"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        first = await client.post(
            "/api/projects/bootstrap",
            json={"name": "  Bea's   project  "},
        )
        second = await client.post(
            "/api/projects/bootstrap",
            json={"name": "A duplicate project"},
        )
        projects = await client.get("/api/projects")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["name"] == "Bea's project"
    assert first.json()["state_repo_id"] == f"projects/{first.json()['id']}"
    assert projects.json() == [first.json()]
    assert len(database.personal_projects) == 1
    assert len(app.state.runtime.storage.repos) == 1


@pytest.mark.asyncio
async def test_workspace_bootstrap_returns_the_new_admin_container_and_project() -> None:
    database = FakeAuthDatabase()
    app = auth_app(database)
    headers = {"Authorization": "Bearer session-b"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        response = await client.post(
            "/api/workspaces/bootstrap",
            json={"name": "Bea's project"},
        )
        workspaces = await client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json()["workspace"]["id"] == response.json()["project"]["workspace_id"]
    assert response.json()["workspace"]["name"] == "Bea's workspace"
    assert response.json()["project"]["can_create_project_in_workspace"] is True
    assert workspaces.json() == [response.json()["workspace"]]


@pytest.mark.asyncio
async def test_workspace_admin_can_create_an_idempotent_second_project() -> None:
    database = FakeAuthDatabase()
    app = auth_app(database)
    request_id = uuid4()
    headers = {"Authorization": "Bearer session-a"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        first = await client.post(
            f"/api/workspaces/{database.workspace.id}/projects",
            json={"name": "New market", "request_id": str(request_id)},
        )
        second = await client.post(
            f"/api/workspaces/{database.workspace.id}/projects",
            json={"name": "New market", "request_id": str(request_id)},
        )
        projects = await client.get("/api/projects")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert [item["name"] for item in projects.json()] == ["Checkpoint A", "New market"]
    assert first.json()["state_repo_id"] == f"projects/{first.json()['id']}"
    assert len(app.state.runtime.storage.repos) == 1


@pytest.mark.asyncio
async def test_existing_member_bootstrap_reuses_project_without_creating_storage() -> None:
    database = FakeAuthDatabase()
    app = auth_app(database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/projects/bootstrap",
            json={"name": "Unused"},
            headers={"Authorization": "Bearer session-a"},
        )

    assert response.status_code == 200
    assert response.json()["id"] == str(database.project.id)
    assert app.state.runtime.storage.repos == {}


@pytest.mark.asyncio
async def test_personal_project_is_not_projected_when_storage_initialization_fails() -> None:
    database = FakeAuthDatabase()
    app = auth_app(database)
    app.state.runtime.storage = FailingStorage()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/projects/bootstrap",
            json={"name": "Bea's project"},
            headers={"Authorization": "Bearer session-b"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "project storage could not be prepared"
    assert database.personal_projects == {}
    assert database.members[database.other_project.id] == set()


@pytest.mark.asyncio
async def test_flat_member_invitation_is_email_bound_and_grants_the_same_access() -> None:
    database = FakeAuthDatabase()
    app = auth_app(database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            f"/api/projects/{database.project.id}/invitations",
            json={"email": " B@Example.com "},
            headers={"Authorization": "Bearer session-a"},
        )
        assert created.status_code == 201
        invitation_url = created.json()["invitation_url"]
        token = parse_qs(urlsplit(invitation_url).query)["invite"][0]

        wrong_identity = await client.post(
            f"/api/invitations/{token}/accept",
            headers={"Authorization": "Bearer session-wrong"},
        )
        accepted = await client.post(
            f"/api/invitations/{token}/accept",
            headers={"Authorization": "Bearer session-b"},
        )
        second_invite = await client.post(
            f"/api/projects/{database.project.id}/invitations",
            json={"email": "next@example.com"},
            headers={"Authorization": "Bearer session-b"},
        )
        collaborator_workspaces = await client.get(
            "/api/workspaces",
            headers={"Authorization": "Bearer session-b"},
        )
        collaborator_projects = await client.get(
            "/api/projects",
            headers={"Authorization": "Bearer session-b"},
        )
        forbidden_project = await client.post(
            f"/api/workspaces/{database.workspace.id}/projects",
            json={"name": "Not mine", "request_id": str(uuid4())},
            headers={"Authorization": "Bearer session-b"},
        )
        personal_workspace = await client.post(
            "/api/workspaces/bootstrap",
            json={"name": "Bea's project"},
            headers={"Authorization": "Bearer session-b"},
        )

    assert created.json()["email"] == "b@example.com"
    assert wrong_identity.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["id"] == str(database.project.id)
    assert second_invite.status_code == 201
    assert collaborator_workspaces.json() == []
    assert collaborator_projects.json()[0]["workspace_name"] == database.workspace.name
    assert collaborator_projects.json()[0]["can_create_project_in_workspace"] is False
    assert collaborator_projects.json()[0]["member_count"] == 2
    assert forbidden_project.status_code == 404
    assert personal_workspace.status_code == 200
    assert personal_workspace.json()["project"]["id"] != str(database.project.id)
    assert database.members[database.project.id] == {USER_A, USER_B}


@pytest.mark.asyncio
async def test_an_accepted_invitation_replays_for_its_member_after_it_expires(
    publication_db,
) -> None:
    db = publication_db
    project = await db.create_project(name="Acme", state_repo_id=f"projects/{uuid4()}")
    token_hash = "a" * 64
    await db.create_project_invitation(
        project_id=project.id,
        email="b@example.com",
        token_hash=token_hash,
        created_by_clerk_user_id=USER_A,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    accept = {"token_hash": token_hash, "expected_email": "b@example.com"}
    await db.accept_project_invitation(clerk_user_id=USER_B, **accept)
    await db.pool.execute(
        "UPDATE project_invitations SET created_at=now()-interval '8 days', "
        "expires_at=now()-interval '1 day' WHERE token_hash=$1",
        token_hash,
    )

    again = await db.accept_project_invitation(clerk_user_id=USER_B, **accept)
    assert again.project_id == project.id and again.accepted_by_clerk_user_id == USER_B
    with pytest.raises(RuntimeError, match="already been accepted"):
        await db.accept_project_invitation(clerk_user_id=USER_WRONG, **accept)
    # An expired invitation never grants access again once the membership is gone.
    await db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", project.id)
    with pytest.raises(RuntimeError, match="expired"):
        await db.accept_project_invitation(clerk_user_id=USER_B, **accept)
    assert not await db.has_project_access(project_id=project.id, clerk_user_id=USER_B)
