from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.documents import render_markdown
from tin_lite.domain import Project, RunStatus, WorkflowRun


def test_markdown_renderer_is_complete_deterministic_and_safe() -> None:
    markdown = """# Reader proof

## Repeated heading

Text with **bold**, *italic*, ~~strike~~, and `inline_code`.
[unsafe](javascript:alert(1)).<br><sub>detail</sub>

## Repeated heading

| Label | Count |
| --- | ---: |
| Found | 2 of 5 |

## Final section

- [x] Complete
- [ ] Pending

```python
print("safe")
```

![Missing preview](https://example.com/missing.png)

<script>alert("never")</script>
"""

    document = render_markdown(markdown)

    assert [heading.id for heading in document.headings] == [
        "repeated-heading",
        "repeated-heading-2",
        "final-section",
    ]
    assert '<a href="#harmful-link" rel="noreferrer">unsafe</a>' in document.html
    assert "<br /><sub>detail</sub>" in document.html
    assert '<div class="md-code-block" data-language="python">' in document.html
    assert '<input class="task-list-item-checkbox" type="checkbox" disabled checked/>' in (
        document.html
    )
    assert "<table>" in document.html
    assert 'data-fallback-name="missing.png"' in document.html
    assert "<script>" not in document.html
    assert "&lt;script&gt;alert" in document.html
    assert document.word_count > 20
    assert document.reading_minutes == 1


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_scalar_frontmatter_is_collapsible_safe_and_not_article_copy(newline):
    body = "# Article\n\n## Helpful section\n\nSpecific useful copy.\n"
    metadata = "---\nrevision: abc123\nnote: <script>bad()</script>\n---\n"
    source = (metadata + body).replace("\n", newline)
    document = render_markdown(source)
    assert document.markdown == source
    assert document.html.startswith(render_markdown(body).html)
    assert '<details class="md-document-metadata">' in document.html
    assert document.html.endswith("</details>\n")
    assert "<summary>Document metadata</summary>" in document.html
    assert "&lt;script&gt;" in document.html and "<script>" not in document.html
    assert "<details open" not in document.html
    assert document.word_count == render_markdown(body).word_count
    assert [heading.title for heading in document.headings] == ["Helpful section"]


@pytest.mark.parametrize(
    "source",
    [
        "---\nAn ordinary section\n---\n# Article",
        "---\nmissing: end delimiter\n# Article",
        "---\nitems:\n  - nested yaml\n---\n# Article",
        "```yaml\n---\nrevision: abc\n---\n```",
    ],
)
def test_non_scalar_or_unclosed_metadata_is_not_hidden(source):
    document = render_markdown(source)
    assert "md-document-metadata" not in document.html
    assert document.markdown == source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_status", [RunStatus.NEEDS_INPUT, RunStatus.SUCCEEDED, RunStatus.FAILED]
)
async def test_run_markdown_adapter_keeps_raw_source_and_serves_generic_document(
    run_status: RunStatus,
) -> None:
    project = Project(uuid4(), "Tin POC", "projects/tin-poc", "main")
    finished_at = datetime.now(UTC)
    run = WorkflowRun(
        id=uuid4(),
        project_id=project.id,
        workflow_id=uuid4(),
        executor="scan.report",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"scan.report:{uuid4()}",
        thread_id="thread",
        generation=1,
        fencing_token=1,
        status=run_status,
        review_required=run_status == RunStatus.NEEDS_INPUT,
        canonical_commit_sha="c" * 40,
        artifact_path="reports/SCAN.md",
        finished_at=finished_at,
    )
    markdown = b"# Scan\n\n## First\n\nOne.\n\n## Second\n\nTwo.\n\n## Third\n\nThree.\n"

    class Database:
        async def get_run(self, run_id):
            return run if run_id == run.id else None

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

    class Storage:
        async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
            assert repo_id == project.state_repo_id
            assert commit_sha == run.canonical_commit_sha
            assert path == run.artifact_path
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
        switchboard_public_url="http://test",
    )
    app.state.runtime = SimpleNamespace(database=Database(), storage=Storage())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(f"/documents/runs/{run.id}")
        document = await client.get(f"/api/workflows/runs/{run.id}/artifact/document")
        raw = await client.get(f"/api/workflows/runs/{run.id}/artifact")
        card = await client.get(f"/runs/{run.id}")

    assert page.status_code == 200
    assert '<main id="viewer"' in page.text
    assert "/assets/markdown-viewer.js" in page.text
    assert document.status_code == 200
    assert document.headers["X-Tin-Artifact-Source"] == "code.storage"
    payload = document.json()
    assert payload["markdown"] == markdown.decode()
    assert payload["filename"] == "SCAN.md"
    # The reader links each folder of the saved output's project path, as Files does.
    assert (payload["path"], payload["revision"]) == ("reports/SCAN.md", "c" * 40)
    assert payload["source_url"] == f"/api/workflows/runs/{run.id}/artifact"
    assert payload["timestamp"] == finished_at.isoformat().replace("+00:00", "Z")
    assert [heading["title"] for heading in payload["headings"]] == [
        "First",
        "Second",
        "Third",
    ]
    assert raw.content == markdown
    assert raw.headers["content-type"].startswith("text/markdown")
    assert f'href="/documents/runs/{run.id}"' in card.text


@pytest.mark.asyncio
async def test_failed_task_keeps_its_exact_isolated_markdown_readable() -> None:
    project = Project(uuid4(), "Tin POC", "projects/tin-poc", "main")
    requested_at = datetime.now(UTC)
    path = "reports/AUTOMATED_BRAND_WORKFLOW.md"
    task_diff = {
        "stats": {"files": 1, "additions": 4, "deletions": 0},
        "files": [
            {
                "path": path,
                "old_path": None,
                "state": "added",
                "bytes": 80,
                "patch": "diff --git a/report b/report\n",
            }
        ],
        "sha256": "f" * 64,
    }
    run = WorkflowRun(
        id=uuid4(),
        project_id=project.id,
        workflow_id=uuid4(),
        executor="project.task",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"project.task:{uuid4()}",
        thread_id="thread",
        generation=1,
        fencing_token=1,
        status=RunStatus.FAILED,
        review_requested_at=requested_at,
        ephemeral_branch="tasks/run/1",
        expected_head_sha="a" * 40,
        task_phase="failed",
        task_diff=task_diff,
        task_has_changes=True,
    )
    markdown = b"# Brand workflow\n\n## Evidence\n\nGrounded.\n"

    class Database:
        async def get_run(self, run_id):
            return run if run_id == run.id else None

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

    class Storage:
        async def get_task_branch_diff(self, **values):
            assert values == {
                "repo_id": project.state_repo_id,
                "branch": run.ephemeral_branch,
                "base_branch": project.canonical_branch,
                "expected_base_sha": run.expected_head_sha,
            }
            return task_diff, "exact diff"

        async def read_ephemeral_artifact(self, **values):
            assert values == {
                "repo_id": project.state_repo_id,
                "branch": run.ephemeral_branch,
                "path": path,
            }
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
    query = {"path": path}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        document = await client.get(f"/api/tasks/{run.id}/review/document", params=query)
        raw = await client.get(f"/api/tasks/{run.id}/review/file", params=query)
        unknown = await client.get(
            f"/api/tasks/{run.id}/review/document",
            params={"path": "README.md"},
        )

    assert document.status_code == 200
    assert document.headers["X-Tin-Task-Review"] == str(run.id)
    payload = document.json()
    assert payload["markdown"] == markdown.decode()
    assert payload["filename"] == "AUTOMATED_BRAND_WORKFLOW.md"
    assert payload["source_url"].endswith("?path=reports%2FAUTOMATED_BRAND_WORKFLOW.md")
    assert raw.content == markdown
    assert raw.headers["content-type"].startswith("text/markdown")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_task_approval_returns_an_explicit_applying_projection() -> None:
    project_id = uuid4()
    project = Project(project_id, "Tin POC", "projects/tin-poc", "main")
    run = WorkflowRun(
        id=uuid4(),
        project_id=project_id,
        workflow_id=uuid4(),
        executor="project.task",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"project.task:{uuid4()}",
        thread_id="thread",
        generation=1,
        fencing_token=1,
        status=RunStatus.NEEDS_INPUT,
        task_phase="review",
        task_diff={"files": [{"path": "report.md", "state": "added"}]},
        task_has_changes=True,
    )
    applying = replace(run, status=RunStatus.RUNNING, task_phase="applying")

    class Database:
        async def get_run(self, run_id):
            return run if run_id == run.id else None

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == run.project_id and clerk_user_id == "user_test"

        async def get_project(self, value):
            return project if value == project.id else None

        async def begin_task_approval(self, *, run_id, clerk_user_id):
            assert run_id == run.id
            assert clerk_user_id == "user_test"
            return applying

    class Handle:
        def __init__(self):
            self.signals = []

        async def signal(self, value):
            self.signals.append(value)

    handle = Handle()

    class Temporal:
        def get_workflow_handle(self, workflow_id):
            assert workflow_id == run.temporal_workflow_id
            return handle

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.runtime = SimpleNamespace(database=Database(), temporal=Temporal())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/tasks/{run.id}/approve")

    assert response.status_code == 202
    assert response.json()["status"] == "running"
    assert response.json()["task_phase"] == "applying"
    assert handle.signals == ["approve"]
