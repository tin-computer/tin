"""Staging shares the code.storage organization but never production's repositories or state."""

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tin_lite.code_storage import CodeStorage
from tin_lite.settings import Settings


def _pem() -> str:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


class RecordingRepo:
    def __init__(self, repo_id: str) -> None:
        self.id = repo_id

    async def list_commits(self, **_kwargs):
        return {"commits": [{"sha": "a" * 40}]}


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.existing: set[str] = set()

    async def find_one(self, *, id: str):
        self.calls.append(("find", id))
        return RecordingRepo(id) if id in self.existing else None

    async def create_repo(self, *, id: str, **_kwargs):
        self.calls.append(("create", id))
        self.existing.add(id)
        return RecordingRepo(id)

    async def delete_repo(self, *, id: str, **_kwargs):
        self.calls.append(("delete", id))
        self.existing.discard(id)


@pytest.mark.asyncio
async def test_prefix_reaches_every_upstream_repository_name() -> None:
    pem = _pem()
    storage = CodeStorage(organization="tin", private_key=pem, repo_prefix="staging-")
    client = RecordingClient()
    storage._client = client  # type: ignore[assignment]

    repo = await storage.ensure_repo("registry/workflows")
    assert repo.id == "staging-registry/workflows"
    assert (await storage.get_repo("registry/workflows")) is not None
    await storage.delete_repo("registry/workflows")
    assert {name for _, name in client.calls} == {"staging-registry/workflows"}

    remotes = storage.sandbox_remotes(repo_id="projects/p1", branch="run", subject="run")
    assert remotes.canonical_url == "https://tin.code.storage/staging-projects/p1.git"
    assert remotes.ephemeral_url == "https://tin.code.storage/staging-projects/p1+ephemeral.git"
    token = storage._generate_token(repo_id="projects/p1", scopes=["git:read"], subject="s", ttl=60)
    assert jwt.decode(token, options={"verify_signature": False})["repo"] == "staging-projects/p1"


@pytest.mark.asyncio
async def test_no_prefix_keeps_production_names() -> None:
    storage = CodeStorage(organization="tin", private_key=_pem())
    client = RecordingClient()
    storage._client = client  # type: ignore[assignment]

    await storage.ensure_repo("projects/p1")
    assert {name for _, name in client.calls} == {"projects/p1"}
    remotes = storage.sandbox_remotes(repo_id="projects/p1", branch="run", subject="run")
    assert remotes.canonical_url == "https://tin.code.storage/projects/p1.git"


def settings_values(monkeypatch, **overrides):
    for field in Settings.model_fields.values():
        monkeypatch.delenv(field.alias, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    values = {
        field.alias: 5432 if name.endswith("port") else "synthetic"
        for name, field in Settings.model_fields.items()
        if field.is_required()
    }
    values["CLERK_PUBLISHABLE_KEY"] = "pk_test_synthetic"
    values.update(overrides)
    return values


STAGING = {
    "TIN_LITE_ENVIRONMENT": "staging",
    "TIN_LITE_CODE_STORAGE_REPO_PREFIX": "staging-",
    "TIN_LITE_PRODUCTION_DATABASE_HOST": "prod.db.example",
    "TIN_LITE_PRODUCTION_TEMPORAL_NAMESPACE": "prod.ns",
    "DATABASE_HOST": "127.0.0.1",
    "MIGRATION_HOST": "127.0.0.1",
    "TEMPORAL_NAMESPACE": "staging.ns",
}


def test_staging_settings_load(monkeypatch):
    settings = Settings(_env_file=None, **settings_values(monkeypatch, **STAGING))
    assert settings.deployment_environment == "staging"
    assert settings.code_storage_repo_prefix == "staging-"


def test_production_is_the_default_and_needs_no_prefix(monkeypatch):
    settings = Settings(_env_file=None, **settings_values(monkeypatch))
    assert settings.deployment_environment == "production"
    assert settings.code_storage_repo_prefix == ""


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"TIN_LITE_CODE_STORAGE_REPO_PREFIX": ""}, "REPO_PREFIX"),
        ({"TIN_LITE_STRIPE_MODE": "live"}, "STRIPE_MODE=test"),
        ({"TIN_LITE_PRODUCTION_DATABASE_HOST": None}, "compare against"),
        ({"DATABASE_HOST": "prod.db.example"}, "production's database"),
        ({"MIGRATION_HOST": "prod.db.example"}, "production's database"),
        ({"TEMPORAL_NAMESPACE": "prod.ns"}, "Temporal namespace"),
    ],
)
def test_staging_refuses_production_state(monkeypatch, override, message):
    values = settings_values(monkeypatch, **{**STAGING, **override})
    values = {key: value for key, value in values.items() if value is not None}
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize("prefix", ["staging", "Staging-", "staging/", "-", "../x-"])
def test_repo_prefix_shape(monkeypatch, prefix):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            **settings_values(monkeypatch, TIN_LITE_CODE_STORAGE_REPO_PREFIX=prefix),
        )
