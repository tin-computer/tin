from __future__ import annotations

from collections import Counter

import httpx
import pytest

from tin_lite import code_storage
from tin_lite.code_storage import CodeStorage, _PinnedReadCache

SHA = "a" * 40
OTHER_SHA = "b" * 40
PACKAGE = "workflow_packages/custom.digest"
FILES = {
    f"{PACKAGE}/workflow.json": b'{"definition": {}}\n',
    f"{PACKAGE}/PROMPT.md": b"# Prompt\n",
    f"{PACKAGE}/skills/draft/SKILL.md": b"---\nname: draft\n---\n",
}


class FakeRepo:
    api_base_url = "https://api.tin.code.storage"
    api_version = 1

    def __init__(self, repo_id: str) -> None:
        self.id = repo_id

    def generate_jwt(self, repo_id: str, options: dict) -> str:
        assert repo_id == self.id
        return "read-token"

    async def get_file_stream(self, **values):
        raise AssertionError("the SDK stream wrapper must not be used")


class FakeGitStorage:
    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.missing: set[str] = set()

    async def find_one(self, *, id: str):
        self.lookups.append(id)
        return None if id in self.missing else FakeRepo(id)

    async def delete_repo(self, **values) -> dict:
        return {}


class FakeCodeStorageAPI:
    """Serves one tree at any commit and counts every request by kind."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.requests: Counter[str] = Counter()
        self.seen: list[tuple[str, str, str]] = []
        self.fail_next_file_reads = 0
        self.clients_opened = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        ref, path = params["ref"], params["path"]
        if request.url.path.endswith("/files/metadata"):
            self.requests["metadata"] += 1
            self.seen.append(("metadata", ref, path))
            if path in self.files:
                entry = {
                    "path": path,
                    "type": "blob",
                    "mode": "100644",
                    "size": len(self.files[path]),
                }
                return httpx.Response(200, json={"ref": ref, "files": [entry], "has_more": False})
            if any(name.startswith(f"{path}/") for name in self.files):
                entry = {"path": path, "type": "tree", "mode": "040000"}
                return httpx.Response(200, json={"ref": ref, "files": [entry], "has_more": False})
            return httpx.Response(200, json={"ref": ref, "files": [], "has_more": False})
        assert request.url.path == "/api/v1/repos/file"
        self.requests["file"] += 1
        self.seen.append(("file", ref, path))
        if self.fail_next_file_reads:
            self.fail_next_file_reads -= 1
            return httpx.Response(503)
        if path not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.files[path])


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeCodeStorageAPI:
    fake = FakeCodeStorageAPI(FILES)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(fake.handle)

    def client_with_transport(**values):
        fake.clients_opened += 1
        return real_client(transport=transport, **values)

    monkeypatch.setattr("tin_lite.code_storage.httpx.AsyncClient", client_with_transport)
    return fake


@pytest.fixture
def storage() -> CodeStorage:
    storage = CodeStorage(organization="tin", private_key="unused")
    storage._client = FakeGitStorage()  # type: ignore[assignment]
    return storage


async def _load_package(storage: CodeStorage, *, repo_id: str, sha: str) -> dict[str, bytes]:
    return {
        path: await storage.read_workflow_resource(repo_id=repo_id, commit_sha=sha, path=path)
        for path in FILES
    }


@pytest.mark.asyncio
async def test_repeated_pinned_package_load_reads_storage_once(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    first = await _load_package(storage, repo_id="projects/one", sha=SHA)
    cold = dict(api.requests)

    for _ in range(8):
        assert await _load_package(storage, repo_id="projects/one", sha=SHA) == first

    assert first == FILES
    # Shared parent prefixes are resolved once: workflow_packages, the package, its three
    # files, skills and skills/draft. Before this cache the same load made 11.
    assert cold == {"metadata": 7, "file": 3}
    assert api.requests == cold
    assert storage._client.lookups == ["projects/one"]  # type: ignore[attr-defined]
    assert api.clients_opened == 1
    await storage.close()


@pytest.mark.asyncio
async def test_pinned_cache_is_keyed_by_repository_and_commit(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    path = f"{PACKAGE}/workflow.json"
    await storage.read_workflow_resource(repo_id="projects/one", commit_sha=SHA, path=path)
    reads = api.requests["file"]

    await storage.read_workflow_resource(repo_id="projects/one", commit_sha=OTHER_SHA, path=path)
    assert api.requests["file"] == reads + 1
    await storage.read_workflow_resource(repo_id="projects/two", commit_sha=SHA, path=path)
    assert api.requests["file"] == reads + 2
    assert ("file", OTHER_SHA, path) in api.seen


@pytest.mark.asyncio
async def test_canonical_reads_are_cached_only_when_pinned_to_a_commit(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    path = f"{PACKAGE}/PROMPT.md"
    for _ in range(3):
        assert (
            await storage.read_canonical_artifact(repo_id="registry/x", commit_sha=SHA, path=path)
            == FILES[path]
        )
    assert api.requests["file"] == 1

    for _ in range(3):
        await storage.read_canonical_artifact(repo_id="registry/x", commit_sha="main", path=path)
        await storage.read_canonical_artifact(repo_id="registry/x", commit_sha=SHA[:12], path=path)
    assert api.requests["file"] == 7

    for _ in range(2):
        await storage.read_ephemeral_artifact(repo_id="registry/x", branch="run/1", path=path)
    assert api.requests["file"] == 9


@pytest.mark.asyncio
async def test_failed_and_missing_reads_are_not_cached(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    missing = f"{PACKAGE}/skills/other/SKILL.md"
    for _ in range(2):
        with pytest.raises(ValueError, match="missing"):
            await storage.read_workflow_resource(
                repo_id="projects/one", commit_sha=SHA, path=missing
            )
        assert (
            await storage.read_canonical_artifact_if_exists(
                repo_id="projects/one", commit_sha=SHA, path=missing
            )
            is None
        )
    assert api.requests["file"] == 2

    path = f"{PACKAGE}/PROMPT.md"
    api.fail_next_file_reads = 1
    with pytest.raises(httpx.HTTPStatusError):
        await storage.read_canonical_artifact(repo_id="projects/one", commit_sha=SHA, path=path)
    assert (
        await storage.read_canonical_artifact(repo_id="projects/one", commit_sha=SHA, path=path)
        == FILES[path]
    )
    assert api.requests["file"] == 4


@pytest.mark.asyncio
async def test_pinned_resource_validation_runs_before_the_cache(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    path = f"{PACKAGE}/workflow.json"
    await storage.read_workflow_resource(repo_id="projects/one", commit_sha=SHA, path=path)

    with pytest.raises(ValueError, match="pin a commit"):
        await storage.read_workflow_resource(repo_id="projects/one", commit_sha="main", path=path)
    with pytest.raises(ValueError):
        await storage.read_workflow_resource(
            repo_id="projects/one", commit_sha=SHA, path=f"{PACKAGE}/../workflow.json"
        )


@pytest.mark.asyncio
async def test_checkpoint_and_listing_reads_at_a_commit_are_cached(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    path = f"{PACKAGE}/PROMPT.md"
    for _ in range(3):
        assert (
            await storage.read_procedure_checkpoint(repo_id="projects/one", revision=SHA, path=path)
            == FILES[path]
        )
    assert api.requests["file"] == 1

    listings: list[str] = []

    class ListingRepo(FakeRepo):
        async def list_files(self, **values) -> dict:
            listings.append(values["ref"])
            return {"paths": ["b.md", "a.md"]}

    async def get_repo(repo_id: str):
        return ListingRepo(repo_id)

    storage.get_repo = get_repo  # type: ignore[method-assign]
    for _ in range(3):
        paths = await storage.list_canonical_files_at(repo_id="projects/one", revision=SHA)
        assert paths == ["a.md", "b.md"]
        paths.append("mutated by caller")
        await storage.list_canonical_files_at(repo_id="projects/one", revision="main")
    assert listings == [SHA, "main", "main", "main"]


@pytest.mark.asyncio
async def test_deleting_a_repository_forgets_its_handle_and_reads(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    path = f"{PACKAGE}/workflow.json"
    await storage.read_canonical_artifact(repo_id="projects/one", commit_sha=SHA, path=path)

    await storage.delete_repo("projects/one")
    storage._client.missing.add("projects/one")  # type: ignore[attr-defined]

    with pytest.raises(LookupError):
        await storage.read_canonical_artifact(repo_id="projects/one", commit_sha=SHA, path=path)


@pytest.mark.asyncio
async def test_repository_handles_expire_and_missing_repositories_are_not_remembered(
    monkeypatch: pytest.MonkeyPatch, storage: CodeStorage
) -> None:
    now = [1000.0]
    monkeypatch.setattr(code_storage.time, "monotonic", lambda: now[0])
    lookups = storage._client.lookups  # type: ignore[attr-defined]

    first = await storage.get_repo("projects/one")
    assert await storage.get_repo("projects/one") is first
    assert lookups == ["projects/one"]

    now[0] += code_storage._REPO_HANDLE_TTL_SECONDS + 1
    assert await storage.get_repo("projects/one") is not first
    assert lookups == ["projects/one", "projects/one"]

    storage._client.missing.add("projects/gone")  # type: ignore[attr-defined]
    for _ in range(2):
        with pytest.raises(LookupError):
            await storage.get_repo("projects/gone")
    assert lookups.count("projects/gone") == 2


def test_pinned_cache_evicts_least_recently_used_bytes() -> None:
    overhead = code_storage._PINNED_CACHE_KEY_OVERHEAD
    cache = _PinnedReadCache(max_bytes=3 * (100 + overhead), max_entry_bytes=200 + overhead)
    for name in ("a", "b", "c"):
        cache.put(("file", "repo", SHA, name), name.encode() * 100, 100)
    assert cache.get(("file", "repo", SHA, "a")) == b"a" * 100

    cache.put(("file", "repo", SHA, "d"), b"d" * 100, 100)
    assert cache.get(("file", "repo", SHA, "b")) is None
    assert cache.get(("file", "repo", SHA, "a")) is not None
    assert len(cache) == 3
    assert cache.size_bytes == 3 * (100 + overhead)

    cache.put(("file", "repo", SHA, "large"), b"x" * 201, 201)
    assert cache.get(("file", "repo", SHA, "large")) is None
    assert len(cache) == 3

    cache.discard_repo("repo")
    assert len(cache) == 0
    assert cache.size_bytes == 0


@pytest.mark.asyncio
async def test_closed_storage_reopens_its_shared_client(
    api: FakeCodeStorageAPI, storage: CodeStorage
) -> None:
    path = f"{PACKAGE}/PROMPT.md"
    await storage.read_canonical_artifact(repo_id="projects/one", commit_sha="main", path=path)
    await storage.read_canonical_artifact(repo_id="projects/one", commit_sha="main", path=path)
    assert api.clients_opened == 1

    await storage.close()
    await storage.close()
    await storage.read_canonical_artifact(repo_id="projects/one", commit_sha="main", path=path)
    assert api.clients_opened == 2
    await storage.close()
