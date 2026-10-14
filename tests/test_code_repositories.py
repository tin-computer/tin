"""Code repositories: the app, docs or SDK a project keeps beside its website repository.

Every procedure reads them read-only; only a declared product-code pull request writes to
one, and never a merge. Website writes stay on the website repository.
"""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_integrations import (
    PROJECT_ID,
    RUN_ID,
    USER_ID,
    FakeIntegrationDatabase,
    RepositoryGitHub,
    github_tarball,
    settings,
    tree_entry,
)

from tin_lite.activities import TinActivities
from tin_lite.domain import EffectReceipt, IntegrationConnection
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.integrations import (
    CODE_REPOSITORY_LIMIT,
    GITHUB_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationService,
    ProviderOption,
    code_repositories,
    connected_repositories,
)
from tin_lite.procedures import (
    reference_path,
    repositories_instruction,
    validate_codex_procedure_definition,
)
from tin_lite.workflow_packages import decode_workflow_source

ROOT = Path(__file__).resolve().parents[1]


def connection(**configuration) -> IntegrationConnection:
    now = datetime.now(UTC)
    return IntegrationConnection(
        id=uuid4(),
        project_id=PROJECT_ID,
        provider_key=GITHUB_PROVIDER,
        status="connected",
        external_account_id="42",
        external_account_label="example-org/site",
        configuration={
            "selected_repository": "example-org/site",
            "write_opted_in": True,
            "permissions": {"contents": "write", "pull_requests": "write"},
            **configuration,
        },
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=USER_ID,
        last_checked_at=now,
        last_error_code=None,
        created_at=now,
        updated_at=now,
    )


def options(*names: str) -> list[ProviderOption]:
    return [ProviderOption(id=name, label=name) for name in names]


@asynccontextmanager
async def github_service(monkeypatch, *, available, transport=None, **configuration):
    database = FakeIntegrationDatabase()
    database.connections[(PROJECT_ID, GITHUB_PROVIDER)] = connection(**configuration)

    async def unused(request):
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport or unused)) as client:
        service = IntegrationService(database=database, settings=settings(), client=client)  # type: ignore[arg-type]
        monkeypatch.setattr(
            service, "github_repositories", AsyncMock(return_value=options(*available))
        )
        monkeypatch.setattr(
            service, "_github_installation_token", AsyncMock(return_value="installation-token")
        )
        yield service, database


def test_code_repositories_never_include_the_website_and_are_bounded():
    value = connection(
        code_repositories=["example-org/site", "example-org/app", "bad", 7]
        + [f"example-org/r{index}" for index in range(10)]
    )
    names = code_repositories(value)
    assert "example-org/site" not in names and "bad" not in names
    assert names[0] == "example-org/app"
    assert len(names) <= CODE_REPOSITORY_LIMIT
    assert connected_repositories(value)[0] == {
        "repository": "example-org/site",
        "role": "website",
    }
    assert connected_repositories(replace(value, status="needs_attention")) == []
    assert connected_repositories(connection(selected_repository=None)) == []


async def test_founder_chooses_code_repositories_the_account_can_reach(monkeypatch):
    available = ("example-org/site", "example-org/app", "example-org/docs")
    async with github_service(monkeypatch, available=available) as (service, database):
        updated = await service.select_code_repositories(
            project_id=PROJECT_ID,
            repositories=["example-org/app", "example-org/site", "example-org/app"],
        )
        assert updated.configuration["code_repositories"] == ["example-org/app"]
        assert updated.configuration["selected_repository"] == "example-org/site"
        assert database.activities[-1]["summary"].endswith("example-org/app.")

        with pytest.raises(IntegrationAuthorizationError, match="not available"):
            await service.select_code_repositories(
                project_id=PROJECT_ID, repositories=["someone-else/private"]
            )

        # Choosing a code repository as the website takes it off the code list.
        moved = await service.select_option(
            project_id=PROJECT_ID, provider_key=GITHUB_PROVIDER, option_id="example-org/app"
        )
        assert moved.configuration["selected_repository"] == "example-org/app"
        assert moved.configuration["code_repositories"] == []

        cleared = await service.select_code_repositories(project_id=PROJECT_ID, repositories=[])
        assert cleared.configuration["code_repositories"] == []


async def test_code_repositories_are_limited_and_need_a_website_first(monkeypatch):
    many = [f"example-org/r{index}" for index in range(CODE_REPOSITORY_LIMIT + 1)]
    async with github_service(monkeypatch, available=many) as (service, _):
        with pytest.raises(IntegrationError, match="at most"):
            await service.select_code_repositories(project_id=PROJECT_ID, repositories=many)
    async with github_service(monkeypatch, available=many, selected_repository=None) as (
        service,
        _,
    ):
        with pytest.raises(IntegrationAuthorizationError, match="website repository first"):
            await service.select_code_repositories(project_id=PROJECT_ID, repositories=many[:1])


async def test_a_code_repository_snapshot_reads_only_a_listed_repository(monkeypatch):
    files = {"app/main.py": b"print('app')\n"}
    github = RepositoryGitHub(
        [tree_entry("app/main.py", files["app/main.py"])], github_tarball(files)
    )

    async def transport(request):
        if request.url.path == "/repos/example-org/app":
            github.requests.append(request)
            return httpx.Response(200, json={"default_branch": "main"})
        return await github(request)

    async with github_service(
        monkeypatch,
        available=(),
        transport=transport,
        code_repositories=["example-org/app"],
    ) as (service, _):
        bundle = await service.github_repository_bundle(
            project_id=PROJECT_ID,
            execution_key="run-9:procedure_repository:example-org/app",
            run_id=RUN_ID,
            repository="example-org/app",
        )
        assert bundle.repository == "example-org/app"
        assert github.paths("/repos/example-org/app/tarball/")
        assert not github.paths("/repos/example-org/site")

        with pytest.raises(IntegrationAuthorizationError, match="other repositories"):
            await service.github_repository_bundle(
                project_id=PROJECT_ID,
                execution_key="run-9:other",
                run_id=RUN_ID,
                repository="example-org/unlisted",
            )
        with pytest.raises(IntegrationAuthorizationError, match="other repositories"):
            await service.github_open_pull_requests(
                project_id=PROJECT_ID,
                execution_key="run-9:open",
                base_branch="main",
                run_id=RUN_ID,
                repository="example-org/unlisted",
            )


def procedure_definition(key: str) -> dict:
    """The package's definition as Tin registers it, procedure paths and all."""
    path = f"workflow_packages/{key}/workflow.json"
    source = decode_workflow_source((ROOT / path).read_bytes(), definition_path=path)
    return copy.deepcopy(source.definition)


def test_procedures_read_connected_repositories_unless_they_opt_out():
    definition = procedure_definition("growth.free_tool")
    assert validate_codex_procedure_definition(definition).reference_repositories is True
    definition["procedure"]["workspace"]["repositories"] = "none"
    assert validate_codex_procedure_definition(definition).reference_repositories is False
    definition["procedure"]["workspace"]["repositories"] = "all"
    with pytest.raises(ValueError, match="repositories"):
        validate_codex_procedure_definition(definition)


def test_only_product_code_pull_requests_may_target_a_code_repository():
    signup = validate_codex_procedure_definition(procedure_definition("growth.signup_source"))
    assert signup.code_repository_output is True
    read_only = procedure_definition("growth.free_tool")
    read_only["procedure"]["output"]["repository"] = "code"
    with pytest.raises(ValueError, match="website"):
        validate_codex_procedure_definition(read_only)
    site = json.loads(
        json.dumps(procedure_definition("growth.signup_source")).replace(
            "growth.signup_source", "website.change"
        )
    )
    with pytest.raises(ValueError, match="website"):
        validate_codex_procedure_definition(site)


def test_the_agent_is_told_where_each_repository_is_and_what_it_may_change():
    text = repositories_instruction(
        [
            {
                "repository": "example-org/site",
                "role": "website",
                "path": "/home/user/project",
                "editable": True,
            },
            {
                "repository": "example-org/app",
                "role": "code",
                "path": reference_path("example-org/app"),
                "editable": False,
            },
            {"repository": "example-org/docs", "role": "code", "unavailable": "too large"},
        ]
    )
    assert "example-org/site (the website) at `/home/user/project`, you may edit it" in text
    assert "example-org/app (product code) at `/home/user/repos/example-org/app`, read-only" in text
    assert "example-org/docs (product code) is not available in this run: too large" in text


class EffectDatabase:
    def __init__(self, github):
        self.receipts: dict[str, EffectReceipt] = {}
        self.github = github
        self.connection_reads = 0

    @asynccontextmanager
    async def effect_lock(self, key, operation, *, conn=None):
        yield None, self.receipts.get(key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "started", None)

    async def complete_effect(self, conn, *, execution_key, result):
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "completed", result)

    async def get_integration_connection(self, **values):
        self.connection_reads += 1
        return self.github


def activities(github, bundle):
    host = SimpleNamespace(
        _db=EffectDatabase(github),
        _integrations=SimpleNamespace(github_repository_bundle=bundle),
    )

    async def wait(awaitable, **_):
        return await awaitable

    host._await_with_heartbeats = wait
    host._procedure_repositories = TinActivities._procedure_repositories.__get__(host)
    host._procedure_target_repository = TinActivities._procedure_target_repository.__get__(host)
    return host


async def test_a_run_reads_every_connected_repository_and_goes_on_without_a_broken_one():
    github = connection(code_repositories=["example-org/app", "example-org/docs"])
    built: list[str] = []

    async def bundle(*, project_id, execution_key, run_id, repository=None):
        name = repository or "example-org/site"
        built.append(name)
        if name == "example-org/docs":
            raise IntegrationAuthorizationError("The selected repository is outside the limits")
        return SimpleNamespace(
            repository=name, archive=b"tar", file_count=3, head_sha="b" * 40, complete=True
        )

    host = activities(github, bundle)
    run = SimpleNamespace(id=RUN_ID, project_id=PROJECT_ID, input={})
    listed, archives = await host._procedure_repositories(
        run=run, sandbox_id="sbx", primary="example-org/site", editable=False
    )
    assert listed[0] == {
        "repository": "example-org/site",
        "role": "website",
        "path": "/home/user/project",
        "editable": False,
    }
    assert listed[1]["path"] == "/home/user/repos/example-org/app"
    assert listed[1]["editable"] is False
    assert listed[2]["repository"] == "example-org/docs"
    assert "outside the limits" in listed[2]["unavailable"]
    assert [name for name, _ in archives] == ["example-org/app"]
    assert built == ["example-org/app", "example-org/docs"]

    # The list is pinned: a repository added after the run started is not read on retry.
    github.configuration["code_repositories"].append("example-org/sdk")
    again, _ = await host._procedure_repositories(
        run=run, sandbox_id="sbx", primary=None, editable=False
    )
    assert [item["repository"] for item in again] == [
        "example-org/site",
        "example-org/app",
        "example-org/docs",
    ]
    # Without a workspace of its own, the website repository is a reference too.
    assert again[0]["path"] == "/home/user/repos/example-org/site"


@pytest.mark.parametrize(
    ("wanted", "codes", "target"),
    [
        ("example-org/docs", ["example-org/app", "example-org/docs"], "example-org/docs"),
        ("", ["example-org/app", "example-org/docs"], "example-org/app"),
        ("example-org/site", ["example-org/app"], None),
        ("example-org/app", [], None),
    ],
)
async def test_a_product_code_pull_request_goes_to_the_named_or_first_code_repository(
    wanted, codes, target
):
    host = activities(connection(code_repositories=codes), None)
    run = SimpleNamespace(id=RUN_ID, project_id=PROJECT_ID, input={"expected_repository": wanted})
    product = SimpleNamespace(code_repository_output=True)
    assert await host._procedure_target_repository(run, product) == target
    # Pinned: later changes to the connection don't move the pull request.
    host._db.github = connection(code_repositories=["example-org/other"])
    assert await host._procedure_target_repository(run, product) == target
    assert (
        await host._procedure_target_repository(run, SimpleNamespace(code_repository_output=False))
        is None
    )


async def test_references_unpack_read_only_beside_the_workspace():
    commands: list[str] = []
    written: list[str] = []
    sandbox = SimpleNamespace(
        files=SimpleNamespace(write=AsyncMock(side_effect=lambda path, data: written.append(path))),
        commands=SimpleNamespace(
            run=AsyncMock(side_effect=lambda command, timeout: commands.append(command))
        ),
    )
    runtime = object.__new__(E2BRuntime)
    await runtime._unpack_references(
        sandbox, (("example-org/app", b"one"), ("example-org/docs", b"two"))
    )
    assert written == [
        "/home/user/.tin-lite/reference-0.tar.gz",
        "/home/user/.tin-lite/reference-1.tar.gz",
    ]
    assert "--directory /home/user/repos/example-org/app" in commands[0]
    assert "chmod -R u=rwX,go=rX /home/user/repos" in commands[0]
    assert "rm -f /home/user/.tin-lite/reference-0.tar.gz" in commands[0]
    await runtime._unpack_references(sandbox, ())
    assert len(commands) == 2


def test_website_writers_never_declare_a_code_repository_output():
    for path in (ROOT / "workflow_packages").glob("*/workflow.json"):
        definition = json.loads(path.read_text())["definition"]
        output = (definition.get("procedure") or {}).get("output") or {}
        if output.get("repository") == "code":
            assert definition["key"] in {"growth.signup_source", "growth.framework_starter"}
