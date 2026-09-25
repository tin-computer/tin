import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_integrations import FakeIntegrationDatabase
from test_technical_title_repair import AFTER, BEFORE

from tin_lite.integrations import (
    GitHubFileChange,
    IntegrationAuthorizationError,
    IntegrationService,
)


@pytest.fixture
async def delivery():
    db = FakeIntegrationDatabase()
    project_id, run_id = uuid4(), uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        provider_key="infra.github",
        status="connected",
        external_account_id="42",
        updated_at=datetime.now(UTC),
        configuration={
            "selected_repository": "owner/site",
            "write_opted_in": True,
            "permissions": {"contents": "write", "pull_requests": "write"},
        },
    )
    db.connections[(project_id, "infra.github")] = connection
    state = {
        "path": "index.html",
        "original": BEFORE,
        "advanced_files": [{"filename": "other.md", "status": "added"}],
        "advance_status": "ahead",
        "closed": False,
        "tree": [],
        "tree_truncated": False,
        "change_status": "modified",
        "base": "b" * 40,
        "repo_id": 12,
        "exists": False,
        "html": BEFORE,
        "pr": False,
        "fault": None,
        "extra": False,
    }
    requests = []
    pr = {"number": 1, "html_url": "https://github.com/owner/site/pull/1"}

    def handler(request):
        path, method = request.url.path, request.method
        requests.append((method, path))
        if method == "GET" and path == "/repos/owner/site":
            return httpx.Response(
                200,
                json={"id": state["repo_id"], "full_name": "owner/site", "default_branch": "main"},
            )
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"type": "commit", "sha": state["base"]}})
        if method == "GET" and "/git/trees/" in path:
            assert path.endswith("b" * 40)
            return httpx.Response(
                200, json={"tree": state["tree"], "truncated": state["tree_truncated"]}
            )
        if method == "GET" and "/compare/" in path:
            if path.endswith("..." + state["base"]) and state["base"] != "b" * 40:
                return httpx.Response(
                    200,
                    json={
                        "status": state["advance_status"],
                        "merge_base_commit": {"sha": "b" * 40},
                        "files": state["advanced_files"],
                    },
                )
            files = (
                [{"filename": state["path"], "status": state["change_status"]}]
                if state["html"] != state["original"]
                else []
            )
            if state["extra"]:
                files.append({"filename": "other.py", "status": "modified"})
            return httpx.Response(
                200,
                json={
                    "status": "ahead" if files else "identical",
                    "merge_base_commit": {"sha": "b" * 40},
                    "files": files,
                },
            )
        if method == "GET" and path.endswith("/contents/" + state["path"]):
            if state["html"] is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": state["path"],
                    "size": len(state["html"].encode()),
                    "sha": "file-sha",
                    "encoding": "base64",
                    "content": base64.b64encode(state["html"].encode()).decode(),
                },
            )
        if method == "GET" and path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[pr]
                if state["pr"]
                and "head" in request.url.params
                and (not state["closed"] or request.url.params.get("state") == "all")
                else [],
            )
        if method == "POST" and path.endswith("/git/refs"):
            assert json.loads(request.content)["sha"] == "b" * 40
            status = 422 if state["exists"] else 201
            state["exists"] = True
            return httpx.Response(status, json={"ref": "created"})
        if method == "PUT" and path.endswith("/contents/" + state["path"]):
            state["html"] = base64.b64decode(json.loads(request.content)["content"]).decode()
            if state["fault"] == "after_file_write":
                state["fault"] = None
                raise httpx.ReadTimeout("Fixture lost file response")
            commit = "e" * 40
            return httpx.Response(
                200,
                json={
                    "content": {"sha": "changed-sha"},
                    "commit": {
                        "sha": commit,
                        "html_url": f"https://github.com/owner/site/commit/{commit}",
                    },
                },
            )
        if method == "POST" and path.endswith("/pulls"):
            state["pr"] = True
            if state["fault"] == "after_pr_create":
                state["fault"] = None
                raise httpx.ReadTimeout("Fixture lost PR response")
            return httpx.Response(201, json=pr)
        raise AssertionError(f"Unexpected fixture request {method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = IntegrationService(
            database=db, settings=SimpleNamespace(integration_credential_key=None), client=client
        )
        service._github_installation_token = AsyncMock(return_value="fixture-token")
        binding = await service.github_repository_binding(
            project_id=project_id, expected_repository="owner/site"
        )
        kwargs = dict(
            project_id=project_id,
            run_id=run_id,
            execution_key=f"{run_id}:repair",
            title="Add missing page title",
            body="Review before merging.",
            files=(GitHubFileChange("index.html", AFTER),),
            base_branch="main",
            expected_base_sha="b" * 40,
            expected_binding=binding,
        )
        yield SimpleNamespace(
            db=db,
            service=service,
            kwargs=kwargs,
            state=state,
            requests=requests,
            connection=connection,
        )


@pytest.mark.parametrize("fault", [None, "after_file_write", "after_pr_create"])
@pytest.mark.parametrize("change_status", ["modified", "added"])
async def test_bound_delivery_and_lost_responses_create_one_exact_unmerged_pr(
    delivery, fault, change_status
):
    f = delivery
    f.state["change_status"] = change_status
    f.state["fault"] = fault
    if fault:
        with pytest.raises(httpx.ReadTimeout):
            await f.service.github_create_pull_request(**f.kwargs)
    first = await f.service.github_create_pull_request(**f.kwargs)
    # Completed external receipt remains readable after the default branch advances.
    f.state["base"] = "c" * 40
    f.connection.status = "disconnected"
    requests_before_replay = len(f.requests)
    replay = await f.service.github_create_pull_request(**f.kwargs)
    assert replay == first
    assert len(f.requests) == requests_before_replay
    assert first.url == "https://github.com/owner/site/pull/1"
    assert f.state["html"] == AFTER
    assert sum(method == "POST" and path.endswith("/pulls") for method, path in f.requests) == 1
    assert sum(method == "PUT" for method, _ in f.requests) == 1
    assert not any("/merge" in path for _, path in f.requests)


@pytest.mark.parametrize("change", ["base", "repository", "installation", "id", "revoked"])
async def test_target_changes_fail_before_any_github_write(delivery, change):
    f = delivery
    if change == "base":
        f.state["base"] = "d" * 40
    elif change == "repository":
        f.connection.configuration["selected_repository"] = "other/site"
    elif change == "installation":
        f.connection.external_account_id = "99"
    elif change == "id":
        f.state["repo_id"] = 13
    else:
        f.connection.status = "disconnected"
    with pytest.raises(IntegrationAuthorizationError):
        await f.service.github_create_pull_request(**f.kwargs)
    assert not any(method in {"POST", "PUT", "PATCH"} for method, _ in f.requests)


@pytest.mark.parametrize("foreign", ["unrelated_path", "unexpected_copy"])
async def test_retry_never_overwrites_an_unexpected_branch(delivery, foreign):
    f = delivery
    f.state["exists"] = True
    if foreign == "unrelated_path":
        f.state["extra"] = True
    else:
        f.state["html"] = "Someone else's changes"
    with pytest.raises(IntegrationAuthorizationError):
        await f.service.github_create_pull_request(**f.kwargs)
    assert not any(
        method == "PUT" or path.endswith("/pulls") and method == "POST"
        for method, path in f.requests
    )


async def test_incomplete_open_pr_evidence_fails_closed(delivery):
    f = delivery
    f.service._github_read_open_pull_requests = AsyncMock(
        return_value=(SimpleNamespace(truncated=True, changed_paths=()), None)
    )
    with pytest.raises(IntegrationAuthorizationError, match="incomplete"):
        await f.service.github_create_pull_request(**f.kwargs)
    assert not any(method in {"POST", "PUT", "PATCH"} for method, _ in f.requests)


@pytest.mark.parametrize("fault", [None, "after_file_write", "after_pr_create"])
async def test_new_markdown_delivery_survives_unrelated_commits_and_closed_pr(delivery, fault):
    f = delivery
    f.state.update(
        path="content/blog/new.md", original=None, html=None, change_status="added", fault=fault
    )
    f.kwargs.update(
        files=(GitHubFileChange("content/blog/new.md", "# Reviewed article\n"),),
        allow_unrelated_base_advance=True,
    )
    if fault:
        with pytest.raises(httpx.ReadTimeout):
            await f.service.github_create_pull_request(**f.kwargs)
    f.state.update(base="c" * 40, closed=fault == "after_pr_create")
    result = await f.service.github_create_pull_request(**f.kwargs)
    assert result.number == 1
    assert f.state["html"] == "# Reviewed article\n"
    assert sum(method == "POST" and path.endswith("/pulls") for method, path in f.requests) == 1
    assert sum(method == "PUT" for method, _ in f.requests) == 1


@pytest.mark.parametrize(
    "change", ["same_path", "renamed_path", "parent", "truncated", "rewritten"]
)
async def test_content_delivery_rejects_material_base_changes(delivery, change):
    f = delivery
    f.kwargs.update(allow_unrelated_base_advance=True)
    f.state["base"] = "c" * 40
    if change == "same_path":
        f.state["advanced_files"] = [{"filename": "index.html"}]
    elif change == "renamed_path":
        f.state["advanced_files"] = [
            {"filename": "renamed.html", "previous_filename": "index.html"}
        ]
    elif change == "parent":
        f.kwargs["files"] = (GitHubFileChange("content/new.md", "# New"),)
        f.state["advanced_files"] = [{"filename": "content"}]
    elif change == "truncated":
        f.state["advanced_files"] = [{"filename": f"other/{i}.txt"} for i in range(300)]
    else:
        f.state["advance_status"] = "diverged"
    with pytest.raises(IntegrationAuthorizationError):
        await f.service.github_create_pull_request(**f.kwargs)
    assert not any(method in {"POST", "PUT", "PATCH"} for method, _ in f.requests)


@pytest.mark.parametrize(
    "shape", ["regular", "missing", "symlink", "submodule", "parent_file", "truncated", "oversized"]
)
async def test_markdown_destination_read_uses_exact_commit_and_regular_files(delivery, shape):
    f = delivery
    path = "content/article.md"
    f.state.update(
        path=path,
        html="# Existing article\n",
        tree=[
            {"path": "content", "type": "tree", "mode": "040000"},
            {"path": path, "type": "blob", "mode": "100644", "sha": "file-sha"},
        ],
    )
    if shape == "missing":
        f.state["tree"].pop()
    elif shape == "symlink":
        f.state["tree"][1]["mode"] = "120000"
    elif shape == "submodule":
        f.state["tree"][1].update(type="commit", mode="160000")
    elif shape == "parent_file":
        f.state["tree"][0].update(type="blob", mode="100644")
    elif shape == "truncated":
        f.state["tree_truncated"] = True
    elif shape == "oversized":
        f.state["html"] = "a" * 100_001
    call = f.service.github_markdown_file(
        project_id=f.kwargs["project_id"], binding=f.kwargs["expected_binding"], path=path
    )
    if shape in {"regular", "missing"}:
        assert await call == (f.state["html"] if shape == "regular" else None)
    else:
        from tin_lite.integrations import IntegrationError

        with pytest.raises(IntegrationError):
            await call
    assert not any(method in {"POST", "PUT", "PATCH"} for method, _ in f.requests)


@pytest.mark.parametrize("change", ["unrelated", "same_path", "rewritten"])
async def test_direct_commit_never_overwrites_a_destination_changed_after_preparation(
    delivery, change
):
    f = delivery
    path = "content/blog/new.md"
    f.state.update(path=path, original=None, html=None)
    # The default branch advanced after the article was prepared at the bound head.
    f.state["base"] = "c" * 40
    if change == "same_path":
        f.state.update(advanced_files=[{"filename": path}], html="# Someone else's page\n")
    elif change == "rewritten":
        f.state["advance_status"] = "diverged"
    call = f.service.github_commit_files(
        project_id=f.kwargs["project_id"],
        run_id=f.kwargs["run_id"],
        execution_key=f.kwargs["execution_key"],
        message="Content: Reviewed article",
        files=(GitHubFileChange(path, "# Reviewed article\n"),),
        base_branch="main",
        expected_binding=f.kwargs["expected_binding"],
    )
    if change == "unrelated":
        assert (await call).commit == "e" * 40
        assert f.state["html"] == "# Reviewed article\n"
        return
    with pytest.raises(IntegrationAuthorizationError):
        await call
    assert not any(method in {"POST", "PUT", "PATCH"} for method, _ in f.requests)
    assert f.db.call_receipts[f.kwargs["execution_key"]].status == "failed"
