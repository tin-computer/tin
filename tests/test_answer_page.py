from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.activities import TinActivities
from tin_lite.answer_page import (
    AnswerPageDrafter,
    AnswerPageSource,
    validate_answer_page_artifacts,
)
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import ANSWER_PAGE_WORKFLOW_ID, VISIBILITY_AUDIT_WORKFLOW_ID
from tin_lite.domain import EffectReceipt, Project, RunStatus, WorkflowRun


def authenticate(app: FastAPI) -> None:
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )


def markdown_page() -> str:
    return (
        "# How to keep recurring AI work reliable\n\n"
        "Use a durable workflow runner that separates orchestration from product state.\n\n"
        "## What reliability requires\n\nPersist run state outside the model.\n\n"
        "## What to evaluate\n\nCheck retries, provenance, and readable outputs.\n\n"
        "## Sources\n\n- [Workflow guidance](https://example.com/workflows)\n"
    )


class FakeResponses:
    model = "gpt-6-luna"

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def create(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {
            "id": "resp_answer_page",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "query": "durable AI workflows",
                        "sources": [{"url": "https://example.com/workflows", "title": "Workflows"}],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": markdown_page(),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/workflows",
                                    "title": "Workflows",
                                }
                            ],
                        }
                    ],
                },
            ],
            "usage": {"total_tokens": 20},
        }


@pytest.mark.parametrize("has_memory", [False, True])
@pytest.mark.parametrize("has_visibility", [False, True])
async def test_integration_availability_preserves_answer_page_evidence_fallback(
    has_memory, has_visibility
):
    project = SimpleNamespace(
        id=uuid4(),
        state_repo_id="projects/synthetic",
        memory_commit_sha="m" * 40 if has_memory else None,
        memory_index_path="wiki/INDEX.md" if has_memory else None,
    )
    research = [
        SimpleNamespace(
            executor="codex.procedure",
            canonical_commit_sha=str(index) * 40,
            artifact_path=f"reports/research-{index}.md",
            artifact_ref=(
                f"code.storage://projects/synthetic@{str(index) * 40}/reports/research-{index}.md"
            ),
        )
        for index in range(6)
    ]
    visibility = (
        [
            SimpleNamespace(
                executor="visibility.audit",
                canonical_commit_sha=str(index) * 40,
                artifact_path="reports/AI_VISIBILITY.md",
                artifact_ref=(
                    f"code.storage://projects/synthetic@{str(index) * 40}/reports/AI_VISIBILITY.md"
                ),
            )
            for index in (6, 7)
        ]
        if has_visibility
        else []
    )
    activities = object.__new__(TinActivities)
    activities._db = SimpleNamespace(
        list_memory_source_runs=AsyncMock(return_value=research + visibility)
    )
    activities._storage = SimpleNamespace(
        read_canonical_artifact=AsyncMock(return_value=b"# Verified product facts")
    )
    sources = await activities._answer_page_sources(run_id=uuid4(), project=project)
    expected = [f"tin.project://{project.id}/integrations"]
    if has_memory:
        expected.append(f"code.storage://projects/synthetic@{'m' * 40}/wiki/INDEX.md")
    selected = visibility[-1:] if has_visibility else ([] if has_memory else research[-5:])
    expected.extend(source.artifact_ref for source in selected)
    assert [source.artifact_ref for source in sources] == expected
    assert all(source.content == "# Verified product facts" for source in sources[1:])
    assert activities._storage.read_canonical_artifact.await_count == len(expected) - 1


@pytest.mark.asyncio
async def test_answer_page_uses_public_research_without_github() -> None:
    responses = FakeResponses()
    drafter = AnswerPageDrafter(responses=responses, skill_suite="# Answer-page rules")
    sources = [
        AnswerPageSource(
            label="project memory",
            artifact_ref="code.storage://projects/test@abc/wiki/INDEX.md",
            content="# Project memory\n\nA durable workflow product.\n",
        )
    ]

    draft = await drafter.draft(project_name="Tin", sources=sources)
    page, evidence = drafter.build_artifacts(
        run_id="run-1",
        source_refs=[sources[0].artifact_ref],
        draft=draft,
        artifact_path="reports/ANSWER_PAGE.md",
        evidence_path="reports/answer-page/run-1/evidence.json",
    )

    payload = responses.payloads[0]
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == {"type": "web_search"}
    assert "github" not in json.dumps(payload).casefold()
    validate_answer_page_artifacts(
        page,
        evidence,
        artifact_path="reports/ANSWER_PAGE.md",
        evidence_path="reports/answer-page/run-1/evidence.json",
    )
    assert json.loads(evidence)["source_refs"] == [sources[0].artifact_ref]


class FakeDatabase:
    def __init__(self, *, project: Project, run: WorkflowRun, visibility_run: WorkflowRun) -> None:
        self.project = project
        self.run = run
        self.visibility_run = visibility_run
        self.receipts: dict[str, EffectReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.events: list[str] = []
        self.projection_writes = 0

    @asynccontextmanager
    async def effect_lock(
        self, execution_key: str, operation: str
    ) -> AsyncIterator[tuple[None, EffectReceipt | None]]:
        async with self._locks.setdefault(execution_key, asyncio.Lock()):
            yield None, self.receipts.get(execution_key)

    async def start_effect(self, conn, *, execution_key: str, operation: str) -> None:
        self.receipts.setdefault(
            execution_key,
            EffectReceipt(execution_key, operation, "started", None),
        )

    async def complete_effect(self, conn, *, execution_key: str, result: dict) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "completed", result)

    async def fail_effect(self, conn, *, execution_key: str, error_message: str) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "failed", None)

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id):
        assert project_id == self.project.id
        yield

    async def get_run(self, run_id):
        return self.run if run_id == self.run.id else None

    async def get_project(self, project_id):
        return self.project if project_id == self.project.id else None

    async def mark_run_running(self, run_id):
        self.run = replace(self.run, status=RunStatus.RUNNING)

    async def list_memory_source_runs(self, **values):
        assert values["exclude_run_id"] == self.run.id
        return [self.visibility_run]

    async def get_effect(self, execution_key, conn=None):
        return self.receipts.get(execution_key)

    async def project_success(self, **values):
        assert self.run.review_decision == "approved"
        self.projection_writes += 1
        self.run = replace(
            self.run,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha=values["canonical_commit_sha"],
            artifact_ref=values["artifact_ref"],
            artifact_path=values["artifact_path"],
        )

    async def add_activity(self, *, event_type: str, **values):
        self.events.append(event_type)

    async def request_human_review(self, **values):
        if self.run.status != RunStatus.NEEDS_INPUT:
            self.events.append("human_review_requested")
        self.run = replace(
            self.run,
            status=RunStatus.NEEDS_INPUT if self.run.review_required else RunStatus.RUNNING,
            canonical_commit_sha=values["canonical_commit_sha"],
            artifact_ref=values["artifact_ref"],
            artifact_path=values["artifact_path"],
            artifact_title=values.get("artifact_title"),
        )
        return self.run.review_required

    async def record_human_review(self, *, decision: str, **values):
        assert decision == "approved"
        if self.run.review_decision is None:
            self.events.append("human_review_approved")
        self.run = replace(
            self.run,
            status=RunStatus.RUNNING,
            review_decision=decision,
        )


class FakeStorage:
    def __init__(self) -> None:
        self.documents: dict[str, bytes] = {
            "wiki/INDEX.md": b"# Project memory\n\nTin runs durable workflows.\n",
            "reports/AI_VISIBILITY.md": (
                b"# AI visibility audit\n\n## Buyer questions\n\nHow should teams run AI work?\n"
            ),
        }
        self.publishes = 0

    async def read_canonical_artifact(self, *, path: str, **values) -> bytes:
        return self.documents[path]

    async def publish_state_documents(self, *, documents: dict[str, bytes], **values):
        self.publishes += 1
        self.documents.update(documents)
        await asyncio.sleep(0)
        return "a" * 40, True


class FakeDrafter:
    def __init__(self) -> None:
        self.calls = 0

    async def draft(self, **values) -> dict:
        self.calls += 1
        assert [source.label for source in values["sources"]] == [
            "current integration availability",
            "project memory",
            "latest AI visibility audit",
        ]
        await asyncio.sleep(0)
        return {
            "response_id": "resp_answer_page",
            "model": "gpt-6-luna",
            "markdown": markdown_page(),
            "search_calls": 1,
            "queries": ["durable AI workflows"],
            "sources": [{"url": "https://example.com/workflows", "title": "Workflows"}],
            "citations": [],
            "usage": {},
        }

    build_artifacts = staticmethod(AnswerPageDrafter.build_artifacts)


def activity_fixture() -> tuple[Project, WorkflowRun, WorkflowRun]:
    project = Project(
        uuid4(),
        "Tin",
        "projects/test",
        "main",
        memory_commit_sha="m" * 40,
        memory_index_path="wiki/INDEX.md",
        memory_index="# Project memory\n",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        project_id=project.id,
        workflow_id=ANSWER_PAGE_WORKFLOW_ID,
        executor="content.answer_page",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"content.answer_page:{run_id}",
        thread_id=str(ANSWER_PAGE_WORKFLOW_ID),
        generation=1,
        fencing_token=1,
        status=RunStatus.PENDING,
        review_required=True,
    )
    visibility_id = uuid4()
    visibility_run = WorkflowRun(
        id=visibility_id,
        project_id=project.id,
        workflow_id=VISIBILITY_AUDIT_WORKFLOW_ID,
        executor="visibility.audit",
        definition_commit_sha="v" * 40,
        temporal_workflow_id=f"visibility.audit:{visibility_id}",
        thread_id=str(VISIBILITY_AUDIT_WORKFLOW_ID),
        generation=1,
        fencing_token=1,
        status=RunStatus.SUCCEEDED,
        canonical_commit_sha="c" * 40,
        artifact_path="reports/AI_VISIBILITY.md",
        artifact_ref="code.storage://projects/test@commit/reports/AI_VISIBILITY.md",
    )
    return project, run, visibility_run


@pytest.mark.asyncio
async def test_answer_page_duplicate_execution_reuses_model_commit_and_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run, visibility_run = activity_fixture()
    run = replace(run, created_at=datetime(2026, 9, 28, 17, 0, tzinfo=UTC))
    database = FakeDatabase(project=project, run=run, visibility_run=visibility_run)
    storage = FakeStorage()
    drafter = FakeDrafter()
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda details: None)
    activities = TinActivities(
        database=database,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
        answer_page_drafter=drafter,  # type: ignore[arg-type]
    )

    await asyncio.gather(
        activities.draft_answer_page(str(run.id)),
        activities.draft_answer_page(str(run.id)),
    )
    required = await asyncio.gather(
        activities.request_answer_page_review(str(run.id)),
        activities.request_answer_page_review(str(run.id)),
    )
    assert required == [True, True]
    assert database.run.status == RunStatus.NEEDS_INPUT
    await asyncio.gather(
        activities.record_answer_page_approval(str(run.id)),
        activities.record_answer_page_approval(str(run.id)),
    )
    await asyncio.gather(
        activities.project_answer_page_result(str(run.id)),
        activities.project_answer_page_result(str(run.id)),
    )

    assert drafter.calls == 1
    assert storage.publishes == 1
    assert database.projection_writes == 1
    assert database.run.status == RunStatus.SUCCEEDED
    path = "content/answers/2026-09-28-how-to-keep-recurring-ai-work-reliable.md"
    assert database.run.artifact_path == path
    assert database.run.artifact_title == "How to keep recurring AI work reliable"
    assert path in storage.documents and "reports/ANSWER_PAGE.md" not in storage.documents
    assert database.events == [
        "answer_page_drafted",
        "human_review_requested",
        "human_review_approved",
        "answer_page_ready",
    ]


@pytest.mark.asyncio
async def test_answer_page_approval_signals_temporal() -> None:
    project, run, _ = activity_fixture()
    run = replace(run, status=RunStatus.NEEDS_INPUT)

    class Database:
        async def get_run(self, run_id):
            return run if run_id == run.id else None

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

        async def set_review_actor(self, *, run_id, clerk_user_id):
            assert run_id == run.id
            assert clerk_user_id == "user_test"

        async def output_revision(self, *, run_id):
            return None

    class Handle:
        def __init__(self) -> None:
            self.signals: list[str] = []

        async def signal(self, name: str) -> None:
            self.signals.append(name)

    handle = Handle()

    class Temporal:
        def get_workflow_handle(self, workflow_id: str):
            assert workflow_id == run.temporal_workflow_id
            return handle

    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(database=Database(), temporal=Temporal())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/workflows/runs/{run.id}/approve")

    assert response.status_code == 202
    assert response.json()["status"] == "needs_input"
    assert response.json()["review_required"] is True
    assert handle.signals == ["approve"]


@pytest.mark.asyncio
async def test_report_run_cannot_receive_content_approval() -> None:
    project, _, visibility_run = activity_fixture()

    class Database:
        async def get_run(self, run_id):
            return visibility_run if run_id == visibility_run.id else None

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

    class TemporalMustNotBeSignaled:
        def __getattr__(self, name):
            raise AssertionError(f"report approval touched Temporal: {name}")

    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(
        database=Database(),
        temporal=TemporalMustNotBeSignaled(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/workflows/runs/{visibility_run.id}/approve")

    assert project.id == visibility_run.project_id
    assert response.status_code == 409
    assert response.json()["detail"] == "run does not require human review"


def test_argument_plan_is_saved_separately_from_reader_copy():
    from tin_lite.answer_page import AnswerPageProtocolError, extract_argument_plan

    plan = dict(
        buyer_decision="Choose a provider",
        positioning="Managed setup",
        answer="Use the API",
        proof="Official setup docs, observed 2026-09-22",
        objection="Usage costs",
        next_step="Read setup",
    )
    import json

    metadata, article = extract_argument_plan(
        "<!-- tin-answer-plan-v1 " + json.dumps(plan) + " -->\n# A useful answer\n"
    )
    assert metadata == plan and article.startswith("# A useful answer")
    with pytest.raises(AnswerPageProtocolError):
        extract_argument_plan("# Unsupported comparison")


def structured_page(
    *,
    frontmatter: str = (
        "---\nmeta_title: Which tools keep recurring AI work reliable?\n"
        "meta_description: Durable workflow runners keep recurring AI work reliable by saving "
        "run state outside the model and retrying safely.\n---\n\n"
    ),
    updated: str = "Last updated: 2026-09-28\n\n",
    faq: str = (
        "## FAQ\n\n### Do I need a workflow runner?\n\nYes, once work repeats weekly.\n\n"
        "### Can a cron job do this?\n\nOnly for work without retries or review.\n\n"
    ),
) -> str:
    answer = (
        "Durable workflow runners keep recurring AI work reliable because they save each run's "
        "state outside the model, retry failed steps without repeating paid calls, and keep "
        "every output readable after the run ends. Compare runners by how they recover, how "
        "they record provenance and what a reviewer sees before anything ships."
    )
    return (
        f"{frontmatter}# Which tools keep recurring AI work reliable?\n\n{updated}{answer}\n\n"
        "## What does reliability require?\n\nRun state lives outside the model "
        "([Temporal docs](https://docs.temporal.io/workflows)).\n\n"
        "## How do the options compare?\n\n| Option | Recovery |\n| --- | --- |\n"
        "| Runner | Durable ([guide](https://example.com/runner)) |\n\n"
        f"{faq}"
        "## Sources\n\n- [Temporal docs](https://docs.temporal.io/workflows)\n"
        "- [Runner guide](https://example.com/runner)\n- [Review notes](https://example.org/review)\n"
    )


def structured_responses(text: str) -> FakeResponses:
    responses = FakeResponses()
    original = responses.create

    async def create(payload: dict) -> dict:
        response = await original(payload)
        response["output"][1]["content"][0]["text"] = text
        return response

    responses.create = create  # type: ignore[method-assign]
    return responses


@pytest.mark.asyncio
async def test_search_structured_suite_researches_more_and_saves_a_checked_page() -> None:
    responses = structured_responses(structured_page())
    drafter = AnswerPageDrafter(responses=responses, skill_suite="# Rules\nANSWER_SEO_V1")

    draft = await drafter.draft(project_name="Tin", sources=[], today="2026-09-28")
    page, evidence = drafter.build_artifacts(
        run_id="run-1",
        source_refs=[],
        draft=draft,
        artifact_path="content/answers/2026-09-28-reliable-ai-work.md",
        evidence_path="reports/answer-page/run-1/evidence.json",
    )

    payload = responses.payloads[0]
    assert payload["max_tool_calls"] == 12
    assert json.loads(payload["input"])["today"] == "2026-09-28"
    assert page.decode().startswith(
        '---\nmeta_title: "Which tools keep recurring AI work reliable?"\nmeta_description: "'
    )
    assert json.loads(evidence)["structure"] == "answer-seo-v1"
    validate_answer_page_artifacts(
        page,
        evidence,
        artifact_path="content/answers/2026-09-28-reliable-ai-work.md",
        evidence_path="reports/answer-page/run-1/evidence.json",
    )


@pytest.mark.asyncio
async def test_earlier_pinned_suite_keeps_four_searches_and_the_original_page() -> None:
    responses = FakeResponses()
    drafter = AnswerPageDrafter(responses=responses, skill_suite="# Answer-page rules")

    draft = await drafter.draft(project_name="Tin", sources=[])

    assert responses.payloads[0]["max_tool_calls"] == 4
    assert "today" not in json.loads(responses.payloads[0]["input"])
    assert draft["markdown"] == markdown_page() and "structure" not in draft


def test_missing_search_listing_and_date_are_rebuilt_from_the_page():
    from tin_lite.answer_page import normalize_search_metadata, validate_answer_page

    page = normalize_search_metadata(
        structured_page(frontmatter="", updated=""), today="2026-09-28"
    )

    assert page.startswith('---\nmeta_title: "Which tools keep recurring AI work reliable?"\n')
    description = json.loads(page.split("\n")[2].removeprefix("meta_description: "))
    assert 70 <= len(description) <= 160 and description.startswith("Durable workflow runners")
    assert "\nLast updated: 2026-09-28\n" in page
    validate_answer_page(page.encode(), structured=True)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda page: page.replace("## FAQ", "## Questions"), "FAQ section"),
        (lambda page: page.replace("## How do the options compare?", "## Options"), "questions"),
        (
            lambda page: page.replace("- [Review notes](https://example.org/review)\n", ""),
            "3 sources",
        ),
        (
            lambda page: page.replace(
                "([Temporal docs](https://docs.temporal.io/workflows))", ""
            ).replace("Durable ([guide](https://example.com/runner))", "Durable"),
            "inline",
        ),
        (
            lambda page: (
                page.replace("Durable workflow runners keep", "Runners keep", 1).split(" because")[
                    0
                ]
                + ".\n\n## What does"
                + page.split("## What does", 1)[1]
            ),
            "direct answer",
        ),
        (
            lambda page: page.replace(
                "Run state lives outside the model", " ".join(["word"] * 160)
            ),
            "150 words",
        ),
        (lambda page: page.replace("## Sources", "## Sources and notes"), "Sources"),
    ],
)
def test_search_structure_checks_catch_missing_pieces(change, message):
    from tin_lite.answer_page import normalize_search_metadata, validate_answer_page

    page = change(normalize_search_metadata(structured_page(), today="2026-09-28"))
    with pytest.raises(ValueError, match=message):
        validate_answer_page(page.encode(), structured=True)


def test_pages_saved_before_search_structure_keep_their_original_checks():
    evidence = json.dumps(
        {
            "artifact_path": "reports/ANSWER_PAGE.md",
            "source_refs": [],
            "search_calls": 1,
        }
    ).encode()
    validate_answer_page_artifacts(
        markdown_page().encode(),
        evidence,
        artifact_path="reports/ANSWER_PAGE.md",
        evidence_path="reports/answer-page/run-1/evidence.json",
    )


def test_pinned_answer_page_suite_carries_the_search_structure_rules():
    from tin_lite.catalog import BUILTIN_WORKFLOWS
    from tin_lite.native_skill_pins import pinned_suite

    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.answer_page")
    definition, _ = workflow.definition_and_resource_files()
    suite = pinned_suite(definition, "content.answer_page")
    assert "ANSWER_PLAN_V1" in suite and "ANSWER_SEO_V1" in suite
    assert workflow.version_label == "1.3.0"


def test_delivery_writes_one_header_with_the_search_listing():
    from tin_lite.answer_page import normalize_search_metadata
    from tin_lite.content_delivery import DeliverySettings, new_page_header, page_frontmatter

    page = normalize_search_metadata(structured_page(), today="2026-09-28")
    metadata, article = page_frontmatter(page)
    assert metadata["meta_title"] == "Which tools keep recurring AI work reliable?"
    assert article.startswith("# Which tools keep recurring AI work reliable?")

    plain = DeliverySettings.model_construct(frontmatter={})
    configured = DeliverySettings.model_construct(frontmatter={"title": "{title}"})
    only_page = new_page_header(plain, "Title", "2026-09-28", "slug", metadata)
    merged = new_page_header(configured, "Title", "2026-09-28", "slug", metadata)
    assert only_page.count("---\n") == 2 and "meta_description:" in only_page
    assert merged.count("---\n") == 2 and "title: Title" in merged and "meta_title:" in merged
    assert new_page_header(plain, "Title", "2026-09-28", "slug") == ""
    assert page_frontmatter("# No frontmatter\n") == ({}, "# No frontmatter\n")


def test_answer_page_names_come_from_the_date_and_question():
    from tin_lite.answer_page import page_title
    from tin_lite.domain import answer_page_path

    title = page_title(
        "---\nmeta_title: x\n---\n\n# Which *tools* work with [coding agents](https://x.y)?\n"
    )
    assert title == "Which tools work with coding agents?"
    assert answer_page_path(title, "2026-09-28") == (
        "content/answers/2026-09-28-which-tools-work-with-coding-agents.md"
    )
    assert answer_page_path("", "2026-09-28") == "content/answers/2026-09-28-answer-page.md"
    assert (
        len(answer_page_path("word " * 60, "2026-09-28")) <= len("content/answers/") + 11 + 80 + 3
    )
