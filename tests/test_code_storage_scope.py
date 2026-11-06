from __future__ import annotations

import base64

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tin_lite.code_storage import CodeStorage, _read_file_with_retry


class EmptyRepo:
    def __init__(self) -> None:
        self.head: str | None = None
        self.initializations = 0

    async def list_commits(self, **values) -> dict:
        commits = [{"sha": self.head}] if self.head else []
        return {"commits": commits}

    def create_commit(self, **values):
        repo = self

        class Builder:
            def add_file_from_string(self, path: str, content: str):
                assert path == "README.md"
                assert content.startswith("# System wiki")
                return self

            async def send(self) -> dict:
                repo.initializations += 1
                repo.head = "a" * 40
                return {"commit_sha": repo.head}

        return Builder()


class ExistingRepoClient:
    def __init__(self, repo: EmptyRepo) -> None:
        self.repo = repo

    async def find_one(self, **values):
        return self.repo

    async def create_repo(self, **values):
        raise AssertionError("existing repository must not be recreated")


class MultiDocumentRepo:
    def __init__(self) -> None:
        self.commit_values = None
        self.files: list[tuple[str, bytes]] = []

    def create_commit(self, **values):
        self.commit_values = values
        repo = self

        class Builder:
            def add_file(self, path: str, content: bytes):
                repo.files.append((path, content))
                return self

            async def send(self) -> dict:
                return {"commit_sha": "c" * 40}

        return Builder()


class CloseTrackingResponse:
    def __init__(self) -> None:
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *values) -> None:
        self.closed = True

    async def aread(self) -> bytes:
        return b"artifact"


class ReadRepo:
    def __init__(self, response: CloseTrackingResponse) -> None:
        self.response = response

    async def get_file_stream(self, **values):
        return self.response


class ListRepo:
    def __init__(self) -> None:
        self.refs: list[str] = []

    async def list_commits(self, **values):
        assert values["branch"] == "main"
        return {"commits": [{"sha": "c" * 40}]}

    async def list_files(self, **values):
        self.refs.append(values["ref"])
        return {"ref": values["ref"], "paths": ["wiki/INDEX.md", "README.md"]}


class SequenceReadRepo:
    def __init__(self, responses: list[CloseTrackingResponse]) -> None:
        self.responses = responses
        self.reads = 0

    async def get_file_stream(self, **values):
        response = self.responses[self.reads]
        self.reads += 1
        return response


class FailedReadResponse(CloseTrackingResponse):
    async def aread(self) -> bytes:
        raise httpx.ReadError("storage stream ended early")


class EagerReadRepo:
    id = "registry/workflows"
    api_base_url = "https://api.tin.code.storage"
    api_version = 1

    def generate_jwt(self, repo_id: str, options: dict) -> str:
        assert repo_id == self.id
        assert options == {"permissions": ["git:read"], "ttl": 300}
        return "run-scoped-token"

    async def get_file_stream(self, **values):
        raise AssertionError("the SDK stream wrapper must not be used")


@pytest.mark.asyncio
async def test_existing_empty_repository_is_initialized() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    repo = EmptyRepo()
    storage._client = ExistingRepoClient(repo)  # type: ignore[assignment]

    first = await storage.ensure_repo(
        "wiki/system",
        initial_readme="# System wiki\n\nRead-only knowledge managed by Tin.\n",
    )
    second = await storage.ensure_repo(
        "wiki/system",
        initial_readme="# System wiki\n\nRead-only knowledge managed by Tin.\n",
    )

    assert first is repo
    assert second is repo
    assert repo.initializations == 1


@pytest.mark.asyncio
async def test_state_documents_are_written_in_one_guarded_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    repo = MultiDocumentRepo()

    async def ensure_repo(*values, **kwargs):
        return repo

    async def head_sha(*values, **kwargs):
        return "h" * 40

    async def documents_equal(*values, **kwargs):
        return False

    monkeypatch.setattr(storage, "ensure_repo", ensure_repo)
    monkeypatch.setattr(storage, "head_sha", head_sha)
    monkeypatch.setattr(storage, "_documents_equal", documents_equal)

    sha, changed = await storage.publish_state_documents(
        repo_id="projects/test",
        branch="main",
        documents={"report.md": b"report", "evidence.json": b"evidence"},
        workflow_key="visibility.audit",
        execution_key="run:visibility_commit",
        run_id="run",
    )

    assert sha == "c" * 40
    assert changed is True
    assert repo.commit_values["expected_head_sha"] == "h" * 40
    assert repo.files == [("evidence.json", b"evidence"), ("report.md", b"report")]


@pytest.mark.asyncio
async def test_canonical_artifact_read_closes_the_storage_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    response = CloseTrackingResponse()

    async def get_repo(repo_id: str):
        return ReadRepo(response)

    monkeypatch.setattr(storage, "get_repo", get_repo)

    content = await storage.read_canonical_artifact(
        repo_id="projects/test",
        commit_sha="c" * 40,
        path="report.md",
    )

    assert content == b"artifact"
    assert response.entered is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_canonical_file_list_is_pinned_to_the_resolved_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    repo = ListRepo()

    async def get_repo(repo_id: str):
        assert repo_id == "projects/test"
        return repo

    monkeypatch.setattr(storage, "get_repo", get_repo)

    paths, revision = await storage.list_canonical_files(
        repo_id="projects/test",
        branch="main",
    )

    assert revision == "c" * 40
    assert repo.refs == [revision]
    assert paths == ["README.md", "wiki/INDEX.md"]


@pytest.mark.asyncio
async def test_canonical_artifact_read_retries_a_dropped_storage_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    failed_response = FailedReadResponse()
    successful_response = CloseTrackingResponse()
    repo = SequenceReadRepo([failed_response, successful_response])

    async def get_repo(repo_id: str):
        return repo

    async def no_wait(delay: float) -> None:
        assert delay == 0.1

    monkeypatch.setattr(storage, "get_repo", get_repo)
    monkeypatch.setattr("tin_lite.code_storage.asyncio.sleep", no_wait)

    content = await storage.read_canonical_artifact(
        repo_id="projects/test",
        commit_sha="c" * 40,
        path="report.md",
    )

    assert content == b"artifact"
    assert repo.reads == 2
    assert failed_response.closed is True
    assert successful_response.closed is True


@pytest.mark.asyncio
async def test_large_file_read_uses_eager_http_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    expected = b"x" * 43_172

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["path"] == "procedures/strategy/weights.json"
        assert request.url.params["ref"] == "a" * 40
        assert request.headers["authorization"] == "Bearer run-scoped-token"
        return httpx.Response(200, content=expected)

    transport = httpx.MockTransport(handle)

    def client_with_transport(**values):
        return real_client(transport=transport, **values)

    monkeypatch.setattr("tin_lite.code_storage.httpx.AsyncClient", client_with_transport)

    content = await _read_file_with_retry(
        EagerReadRepo(),  # type: ignore[arg-type]
        path="procedures/strategy/weights.json",
        ref="a" * 40,
    )

    assert content == expected


@pytest.mark.asyncio
async def test_canonical_artifact_read_stops_after_five_dropped_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    responses = [FailedReadResponse() for _ in range(5)]
    repo = SequenceReadRepo(responses)
    delays: list[float] = []

    async def get_repo(repo_id: str):
        return repo

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(storage, "get_repo", get_repo)
    monkeypatch.setattr("tin_lite.code_storage.asyncio.sleep", no_wait)

    with pytest.raises(httpx.ReadError):
        await storage.read_canonical_artifact(
            repo_id="projects/test",
            commit_sha="c" * 40,
            path="report.md",
        )

    assert repo.reads == 5
    assert delays == [0.1, 0.2, 0.4, 0.8]
    assert all(response.closed for response in responses)


def test_sandbox_write_token_is_scoped_to_exact_ephemeral_branch() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    branch = "generations/run-1/7"

    remotes = storage.sandbox_remotes(
        repo_id="projects/demo",
        branch=branch,
        subject="run:run-1",
    )

    basic = remotes.ephemeral_auth_header.removeprefix("Authorization: Basic ")
    _, token = base64.b64decode(basic).decode().split(":", 1)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["scopes"] == ["git:read", "git:write"]
    assert claims["refs"] == [
        [f"refs/namespaces/ephemeral/refs/heads/{branch}", ["no-force-push"]],
        ["*", ["no-push"]],
    ]
    assert "@" not in remotes.ephemeral_url


def test_canonical_token_is_read_only() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    storage = CodeStorage(organization="tin", private_key=pem)
    remotes = storage.sandbox_remotes(
        repo_id="projects/demo",
        branch="generations/run-1/7",
        subject="run:run-1",
    )
    basic = remotes.canonical_auth_header.removeprefix("Authorization: Basic ")
    _, token = base64.b64decode(basic).decode().split(":", 1)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["scopes"] == ["git:read"]
