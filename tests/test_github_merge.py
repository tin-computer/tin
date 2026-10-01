"""Merging a pull request Tin opened: only its exact files, only at the head Tin checked."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from test_integrations import PROJECT_ID, USER_ID, FakeIntegrationDatabase, _github_settings

from tin_lite.domain import IntegrationConnection, SideEffectConflictError
from tin_lite.integrations import (
    GITHUB_PROVIDER,
    GitHubFileChange,
    GitHubRepositoryBinding,
    IntegrationAuthorizationError,
    IntegrationService,
)

HEAD = "a" * 40
BASE = "9" * 40
MERGED = "c" * 40
PAGE = "---\ntitle: A page\n---\n\n# A page\n\nThe approved copy.\n"
FILES = (GitHubFileChange(path="content/answers/a-page.md", content=PAGE),)


def connected(database):
    now = datetime.now(UTC)
    connection = IntegrationConnection(
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
        },
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=USER_ID,
        last_checked_at=now,
        last_error_code=None,
        created_at=now,
        updated_at=now,
    )
    database.connections[(PROJECT_ID, GITHUB_PROVIDER)] = connection
    return GitHubRepositoryBinding(connection.id, 42, 7, "example-org/site", "main", BASE)


class GitHub:
    """A synthetic GitHub for one pull request; records every request it answers."""

    def __init__(self, *, files=None, merge=None, pull=None):
        self.requests = []
        self.files = (
            files if files is not None else [{"filename": FILES[0].path, "status": "added"}]
        )
        self.merge = merge or httpx.Response(200, json={"sha": MERGED, "merged": True})
        self.pull = pull or {}

    async def __call__(self, request):
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        root = "/repos/example-org/site"
        if method == "POST" and path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "installation-token"})
        if method == "GET" and path == root:
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "full_name": "example-org/site",
                    "default_branch": "main",
                    "allow_merge_commit": True,
                    "allow_squash_merge": True,
                },
            )
        if method == "GET" and path == f"{root}/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"type": "commit", "sha": BASE}})
        if method == "GET" and path == f"{root}/pulls/12":
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "merged": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "body": "Never leaves the adapter.",
                    "head": {
                        "sha": HEAD,
                        "ref": "tin/0123456789abcdef",
                        "repo": {"full_name": "example-org/site"},
                    },
                    "base": {"ref": "main"},
                    **self.pull,
                },
            )
        if method == "GET" and path == f"{root}/pulls/12/files":
            return httpx.Response(200, json=self.files)
        if method == "GET" and path == f"{root}/contents/{FILES[0].path}":
            assert request.url.params["ref"] == HEAD
            return httpx.Response(200, json={"content": base64.b64encode(PAGE.encode()).decode()})
        if method == "PUT" and path == f"{root}/pulls/12/merge":
            payload = json.loads(request.content)
            assert payload == {
                "sha": HEAD,
                "merge_method": "squash",
                "commit_title": "Add a page (#12)",
            }
            return self.merge
        raise AssertionError(f"unexpected GitHub request {method} {request.url}")


async def merge(service, binding, **overrides):
    values = {
        "project_id": PROJECT_ID,
        "execution_key": "run-3:procedure_pull_request_merge",
        "number": 12,
        "expected_head_sha": HEAD,
        "branch": "tin/0123456789abcdef",
        "files": FILES,
        "expected_binding": binding,
        "commit_title": "Add a page (#12)",
        **overrides,
    }
    return await service.github_merge_pull_request(**values)


def service_for(tmp_path, database, github, client):
    configured, _ = _github_settings(tmp_path)
    return IntegrationService(database=database, settings=configured, client=client)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_merge_state_reports_only_the_verdict_and_head(tmp_path) -> None:
    database = FakeIntegrationDatabase()
    connected(database)
    github = GitHub(pull={"mergeable": None, "mergeable_state": "unknown"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        state = await service_for(
            tmp_path, database, github, client
        ).github_pull_request_merge_state(
            project_id=PROJECT_ID, repository="example-org/site", number=12
        )
    assert state == {
        "state": "open",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "url": None,
        "mergeable": None,
        "mergeable_state": "unknown",
        "head_sha": HEAD,
        "head_ref": "tin/0123456789abcdef",
        "base_ref": "main",
        "same_repository": True,
    }


@pytest.mark.asyncio
async def test_merge_checks_the_exact_files_merges_once_and_replays(tmp_path) -> None:
    database = FakeIntegrationDatabase()
    binding = connected(database)
    github = GitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        service = service_for(tmp_path, database, github, client)
        first = await merge(service, binding)
        count = len(github.requests)
        replay = await merge(service, binding)
        with pytest.raises(SideEffectConflictError):
            await merge(
                service,
                binding,
                files=(replace(FILES[0], content="# Another page\n"),),
            )
    assert (
        first
        == replay
        == {
            "merged": True,
            "repository": "example-org/site",
            "number": 12,
            "commit": MERGED,
            "url": f"https://github.com/example-org/site/commit/{MERGED}",
        }
    )
    assert len(github.requests) == count  # A recorded merge makes no GitHub call.
    assert github.requests.count(("PUT", "/repos/example-org/site/pulls/12/merge")) == 1
    receipt = database.call_receipts["run-3:procedure_pull_request_merge"]
    assert receipt.status == "completed" and receipt.capability == "contents.write"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "files",
    [
        [
            {"filename": FILES[0].path, "status": "added"},
            {"filename": "src/app.tsx", "status": "added"},
        ],
        [{"filename": FILES[0].path, "status": "removed"}],
    ],
)
async def test_merge_refuses_a_pull_request_that_changed(tmp_path, files) -> None:
    database = FakeIntegrationDatabase()
    binding = connected(database)
    github = GitHub(files=files)
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        with pytest.raises(IntegrationAuthorizationError, match="changed after Tin opened it"):
            await merge(service_for(tmp_path, database, github, client), binding)
    assert not any(method == "PUT" for method, _ in github.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["added", "modified"])
async def test_merge_requires_the_page_to_be_a_new_file(tmp_path, status) -> None:
    # Rewriting an existing file with the approved copy (a README, another page) is a PR.
    database = FakeIntegrationDatabase()
    binding = connected(database)
    github = GitHub(files=[{"filename": FILES[0].path, "status": status}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        service = service_for(tmp_path, database, github, client)
        if status == "added":
            assert (await merge(service, binding, new_paths=(FILES[0].path,)))["merged"]
        else:
            with pytest.raises(IntegrationAuthorizationError, match="replace an existing file"):
                await merge(service, binding, new_paths=(FILES[0].path,))
    assert any(method == "PUT" for method, _ in github.requests) is (status == "added")


@pytest.mark.asyncio
async def test_merge_refuses_a_retargeted_pull_request(tmp_path) -> None:
    database = FakeIntegrationDatabase()
    binding = connected(database)
    github = GitHub(pull={"base": {"ref": "release/elsewhere"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        with pytest.raises(IntegrationAuthorizationError, match="destination branch"):
            await merge(service_for(tmp_path, database, github, client), binding)
    assert not any(method == "PUT" for method, _ in github.requests)


@pytest.mark.asyncio
async def test_merge_refused_by_github_is_a_reason_not_an_error(tmp_path) -> None:
    database = FakeIntegrationDatabase()
    binding = connected(database)
    github = GitHub(merge=httpx.Response(405, json={"message": "Base branch was modified"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        result = await merge(service_for(tmp_path, database, github, client), binding)
    assert result == {"merged": False, "reason": "GitHub refused the merge, so Tin left it open."}
    assert database.call_receipts["run-3:procedure_pull_request_merge"].status == "failed"


@pytest.mark.asyncio
async def test_a_lost_merge_response_is_recovered_without_merging_again(tmp_path) -> None:
    database = FakeIntegrationDatabase()
    binding = connected(database)
    github = GitHub(merge=httpx.Response(502, json={"message": "Bad gateway"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        service = service_for(tmp_path, database, github, client)
        with pytest.raises(Exception, match="could not complete"):
            await merge(service, binding)
        # GitHub had merged it after all; the retry reads that instead of merging twice.
        github.pull = {"merged": True, "merge_commit_sha": MERGED, "state": "closed"}
        recovered = await merge(service, binding)
    assert recovered["merged"] is True and recovered["commit"] == MERGED
    assert github.requests.count(("PUT", "/repos/example-org/site/pulls/12/merge")) == 1
