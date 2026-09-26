from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.catalog import BUILTIN_WORKFLOWS, CREATIVE_STUDIO_SYSTEM
from tin_lite.character_design import (
    KEY,
    MODEL,
    ROUTE_KEY,
    CharacterDesign,
    CharacterDesigner,
    CharacterDesignError,
    DesignBrief,
    ProductPage,
    _PageParser,
    _palette,
    _validated_public_url,
    geometry_problems,
    model_route_definition,
)
from tin_lite.character_design_activities import CharacterDesignActivities, character_path
from tin_lite.domain import CREATIVE_CHARACTER_WORKFLOW_NAME, EffectReceipt, RunStatus
from tin_lite.model_providers import ModelResult, ModelUsage, ProviderName
from tin_lite.workflows import CharacterDesignWorkflow, registered_workflow_implementations

ROOT = Path(__file__).parents[1]
LARRY = (ROOT / "src" / "tin_lite" / "example_character.svg").read_bytes()


def test_page_parser_extracts_facts_and_weights_brand_colors() -> None:
    parser = _PageParser()
    parser.feed(
        "<html><head><title> Claw  Messenger </title>"
        '<meta name="description" content="Text your agent.">'
        '<meta name="theme-color" content="#0a84ff">'
        '<link rel="stylesheet" href="/app.css">'
        "<style>.hljs-keyword{color:#177245}.btn-primary{background:#007aff}</style>"
        '<script type="application/ld+json">{"name":"Claw","description":"iMessage API"}'
        "</script></head><body><h1>The iMessage <b>API</b></h1>"
        '<a href="/start">Start free trial</a><button>Docs</button>'
        "<p>Connect your agent.</p><script>var x = 1;</script></body></html>"
    )
    assert parser.title.strip() == "Claw  Messenger"
    assert parser.description == "Text your agent."
    assert parser.headings == ["h1: The iMessage API"]
    assert parser.actions == ["Start free trial", "Docs"]
    assert parser.stylesheets == ["/app.css"]
    assert "var x" not in "".join(parser.text)
    assert any("iMessage API" in item for item in parser.structured)
    colors = _palette("\n".join(parser.style_text), ".token{color:#145a39}", "#0a84ff")
    assert colors[0] == "#0a84ff" and colors[1] == "#007aff"
    assert colors.index("#177245") > colors.index("#007aff")


def test_public_url_guard_rejects_non_https_and_credentials() -> None:
    assert _validated_public_url("https://example.com/page") == "https://example.com/page"
    for bad in (
        "http://example.com/",
        "https://user:pw@example.com/",
        "https://example.com:8443/",
        "",
    ):
        with pytest.raises(CharacterDesignError):
            _validated_public_url(bad)


def test_geometry_problems_pass_the_example_and_catch_a_misplaced_face() -> None:
    assert geometry_problems(LARRY) == []
    moved = LARRY.replace(
        b'<g id="mouth-open" display="none">', b'<g id="mouth-open" display="none" transform="x">'
    )
    moved = moved.replace(
        b'<ellipse cx="256" cy="340" rx="14" ry="8" fill="#FF8FA0"/>',
        b'<ellipse cx="450" cy="480" rx="14" ry="8" fill="#FF8FA0"/>',
    )
    problems = geometry_problems(moved)
    assert any("mouth-open spans" in item for item in problems)
    shown = LARRY.replace(b'<g id="eyes-closed" display="none">', b'<g id="eyes-closed">')
    assert any('eyes-closed must carry display="none"' in item for item in geometry_problems(shown))


class FakeRouter:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.requests: list = []

    async def generate(self, route_key, request):
        assert route_key == ROUTE_KEY
        self.requests.append(request)
        parsed = self.outputs.pop(0)
        return ModelResult(
            provider=ProviderName.OPENAI,
            model=MODEL,
            text=json.dumps(parsed),
            parsed=parsed,
            request_id=f"resp_{len(self.requests)}",
            usage=ModelUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        )


def _draft(svg: bytes) -> dict:
    return {
        "audience": "developers",
        "product_noun": "message",
        "concept": {
            "name": "Claw",
            "species_or_object": "lobster",
            "personality": "eager",
            "prop": "phone",
            "why_it_fits": "claws",
        },
        "palette": {
            "accent": "#007aff",
            "secondary": "#cfe7ff",
            "skin": "#fff0d9",
            "ink": "#1b1b1f",
        },
        "svg": svg.decode("utf-8"),
    }


@pytest.mark.asyncio
async def test_designer_repairs_a_rejected_draft_then_refines_once() -> None:
    broken = LARRY.replace(
        b'<g id="mouth-mid" display="none">', b'<g id="mouth-medium" display="none">'
    )
    router = FakeRouter(
        [
            _draft(broken),
            {"problems_found": ["renamed"], "svg": LARRY.decode("utf-8")},
            {"problems_found": [], "svg": LARRY.decode("utf-8")},
        ]
    )
    designer = CharacterDesigner(router=router, example_svg=LARRY)
    page = ProductPage(
        url="https://clawmessenger.com/",
        final_url="https://clawmessenger.com/",
        title="Claw Messenger",
        description="Text your agent",
        headings=("h1: The iMessage API",),
        actions=("Start free trial",),
        text="Connect your agent.",
        colors=("#007aff",),
        theme_color="",
    )
    design = await designer.design(
        DesignBrief(project_name="Claw", slug="claw", brief="", notes="", memory="", page=page)
    )
    assert design.svg == LARRY.strip()
    assert design.repairs == 1 and design.refined
    assert sorted(design.timings_ms) == ["draft", "refine", "repair1"]
    assert design.request_ids == ("resp_1", "resp_2", "resp_3")
    assert design.usage["total_tokens"] == 450
    first = router.requests[0]
    assert first.output_schema_name == "character_design"
    assert first.reasoning_effort.value == "medium"
    assert (
        "Claw Messenger" in first.messages[0].content
        and "example_character_svg" in first.messages[0].content
    )
    repair = router.requests[1]
    assert "mouth-mid" in repair.messages[-1].content
    restored = CharacterDesign.from_receipt(design.receipt())
    assert restored.svg == LARRY.strip() and restored.concept["concept"]["name"] == "Claw"


@pytest.mark.asyncio
async def test_designer_keeps_the_draft_when_refinement_breaks_the_contract() -> None:
    router = FakeRouter([_draft(LARRY), {"problems_found": ["x"], "svg": "<svg>not valid</svg>"}])
    design = await CharacterDesigner(router=router, example_svg=LARRY).design(
        DesignBrief(project_name="p", slug="s", brief="", notes="", memory="", page=None)
    )
    assert design.svg == LARRY.strip() and not design.refined
    assert any("discarded" in item for item in design.warnings)


@pytest.mark.asyncio
async def test_designer_gives_up_after_bounded_repairs() -> None:
    bad = LARRY.replace(b'<g id="mouth-mid" display="none">', b'<g id="mm" display="none">')
    router = FakeRouter(
        [
            _draft(bad),
            {"problems_found": [], "svg": bad.decode()},
            {"problems_found": [], "svg": bad.decode()},
        ]
    )
    with pytest.raises(CharacterDesignError, match="still violates"):
        await CharacterDesigner(router=router, example_svg=LARRY).design(
            DesignBrief(project_name="p", slug="s", brief="", notes="", memory="", page=None)
        )
    assert len(router.requests) == 3


class FakeDatabase:
    def __init__(self, run, project) -> None:
        self.run, self.project = run, project
        self.receipts: dict[str, EffectReceipt] = {}
        self.events: list[str] = []
        self.progress: list[str] = []

    @asynccontextmanager
    async def effect_lock(self, execution_key: str, operation: str) -> AsyncIterator:
        yield None, self.receipts.get(execution_key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.receipts.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", None)
        )

    async def complete_effect(self, conn, *, execution_key, result):
        op = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, op, "completed", result)

    async def fail_effect(self, conn, *, execution_key, error_message):
        op = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, op, "failed", None)

    async def get_effect(self, execution_key):
        return self.receipts.get(execution_key)

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id):
        yield

    async def get_run(self, run_id):
        return self.run if run_id == self.run.id else None

    async def get_project(self, project_id):
        return self.project

    async def mark_run_running(self, run_id):
        self.run.status = RunStatus.RUNNING

    async def project_run_progress(self, **values):
        self.progress.append(values["step"])
        return True

    async def add_activity(self, *, event_type, **values):
        self.events.append(event_type)

    async def request_human_review(self, **values):
        self.run.status = RunStatus.NEEDS_INPUT
        for key in ("canonical_commit_sha", "artifact_ref", "artifact_path"):
            setattr(self.run, key, values[key])
        return True

    async def record_human_review(self, *, run_id, decision):
        self.run.review_decision = decision
        self.run.status = RunStatus.RUNNING

    async def project_success(self, **values):
        self.run.status = RunStatus.SUCCEEDED
        self.run.artifact_path = values["artifact_path"]

    async def project_failure(self, **values):
        self.run.status = RunStatus.FAILED
        self.run.error_message = values["error_message"]


class FakeStorage:
    def __init__(self, definition: dict) -> None:
        self.documents: dict[str, bytes] = {"wiki/INDEX.md": b"# Memory\n\nClaw texts agents.\n"}
        self.registry = json.dumps(definition).encode()
        self.publishes = 0

    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        if repo_id == "registry/workflows":
            return self.registry
        return self.documents[path]

    async def publish_state_document(self, *, path, content, **values):
        self.publishes += 1
        self.documents[path] = content
        return "b" * 40, True


class FakeDesigner:
    def __init__(self) -> None:
        self.calls = 0

    async def design(self, brief, *, route_key):
        self.calls += 1
        assert route_key == ROUTE_KEY
        assert brief.memory.startswith("# Memory") and brief.slug == "claw"
        return CharacterDesign(
            svg=LARRY,
            concept={"audience": "devs"},
            model=MODEL,
            request_ids=("r1",),
            usage={"total_tokens": 10},
            timings_ms={"draft": 5},
            repairs=0,
            refined=True,
        )


def _run_and_project():
    project = SimpleNamespace(
        id=uuid4(),
        name="Claw QA",
        state_repo_id="projects/claw",
        canonical_branch="main",
        memory_commit_sha="c" * 40,
        memory_index_path="wiki/INDEX.md",
    )
    run = SimpleNamespace(
        id=uuid4(),
        project_id=project.id,
        executor=KEY,
        definition_commit_sha="d" * 40,
        input={"slug": "claw", "brief": "", "product_url": "", "notes": ""},
        status=RunStatus.PENDING,
        review_required=True,
        review_decision=None,
        canonical_commit_sha=None,
        artifact_ref=None,
        artifact_path=None,
        error_message=None,
    )
    return run, project


@pytest.mark.asyncio
async def test_activities_design_publish_review_and_project_with_replayable_receipts() -> None:
    run, project = _run_and_project()
    database = FakeDatabase(run, project)
    storage = FakeStorage({"key": KEY, "model_route": model_route_definition()})
    designer = FakeDesigner()
    activities = CharacterDesignActivities(
        database=database, storage=storage, settings=SimpleNamespace(), designer=designer
    )
    await activities.character_design(str(run.id))
    assert storage.documents[character_path("claw")] == LARRY
    assert database.progress == ["prepare", "design", "publish"]
    assert database.events == ["character_designed"]
    commit = database.receipts[f"{run.id}:character:commit"]
    assert commit.status == "completed" and commit.result["artifact_path"] == "characters/claw.svg"
    assert database.receipts[f"{run.id}:character:model"].result["svg"] == LARRY.decode()
    # A retried activity replays the recorded receipts: no second model call or publish.
    await activities.character_design(str(run.id))
    assert designer.calls == 1 and storage.publishes == 1
    assert await activities.character_review(str(run.id)) is True
    assert database.run.status == RunStatus.NEEDS_INPUT
    assert database.run.artifact_ref.endswith("/characters/claw.svg")
    await activities.character_approval(str(run.id))
    await activities.character_project(str(run.id))
    assert database.run.status == RunStatus.SUCCEEDED and database.events[-1] == "character_ready"


@pytest.mark.asyncio
async def test_activities_refuse_a_worker_without_the_pinned_route() -> None:
    run, project = _run_and_project()
    database = FakeDatabase(run, project)
    storage = FakeStorage({"key": KEY, "model_route": {"model": "other"}})
    activities = CharacterDesignActivities(
        database=database, storage=storage, settings=SimpleNamespace(), designer=FakeDesigner()
    )
    with pytest.raises(ValueError, match="pinned character model route"):
        await activities.character_design(str(run.id))
    unconfigured = CharacterDesignActivities(
        database=database, storage=storage, settings=SimpleNamespace(), designer=None
    )
    with pytest.raises(ApplicationError, match="not configured"):
        await unconfigured.character_design(str(run.id))


@pytest.mark.asyncio
async def test_an_unreachable_product_page_is_recorded_and_the_design_goes_on(monkeypatch) -> None:
    from tin_lite import character_design

    def unreachable(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    client = httpx.AsyncClient

    async def public(url):
        return None

    monkeypatch.setattr(character_design, "_require_public_hostname", public)
    monkeypatch.setattr(
        character_design.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(unreachable), **kwargs),
    )
    run, project = _run_and_project()
    activities = CharacterDesignActivities(
        database=FakeDatabase(run, project),
        storage=FakeStorage({}),
        settings=SimpleNamespace(),
        designer=FakeDesigner(),
    )
    context = await activities._context(run, project, {"product_url": "https://example.com/"})
    assert context["page"] is None
    assert context["page_error"] == "product page could not be read"


def test_direct_workflow_is_registered_with_a_pinned_route_and_no_style_picker() -> None:
    assert (
        registered_workflow_implementations()[CREATIVE_CHARACTER_WORKFLOW_NAME]
        is CharacterDesignWorkflow
    )
    workflows = {item.key: item for item in BUILTIN_WORKFLOWS}
    direct = workflows[CREATIVE_CHARACTER_WORKFLOW_NAME].definition
    assert direct["executor"] == KEY == "creative.character"
    assert direct["system"] == CREATIVE_STUDIO_SYSTEM
    assert direct["model_route"] == {
        "key": "creative.character.v1",
        "provider": "openai",
        "model": "gpt-6-sol",
        "capabilities": ["json_schema", "reasoning_effort", "text"],
    }
    assert direct["human_review"]["review_label"] == "Review character"
    assert list(direct["input_schema"]["properties"]) == [
        "project_id",
        "slug",
        "brief",
        "product_url",
        "notes",
    ]
    assert "style" not in direct["input_schema"]["properties"]
    assert "procedure" not in direct
    assert not any(item.key == "creative.character_direct" for item in BUILTIN_WORKFLOWS)


@pytest.mark.asyncio
async def test_direct_workflow_identifier_only_history_and_retry() -> None:
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI required")
    run_id, calls = str(uuid4()), []

    @activity.defn(name="character_design")
    async def design(value: str):
        calls.append(("design", value))
        if len(calls) == 1:
            raise ApplicationError("Synthetic transient error")

    @activity.defn(name="character_review")
    async def review(value: str) -> bool:
        calls.append(("review", value))
        return False

    @activity.defn(name="character_approval")
    async def approval(value: str):
        raise AssertionError("no approval without review")

    @activity.defn(name="character_project")
    async def project(value: str):
        calls.append(("project", value))

    @activity.defn(name="character_failure")
    async def fail(value: dict):
        raise AssertionError("This workflow should recover")

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="character-design-proof",
            workflows=[CharacterDesignWorkflow],
            activities=[design, review, approval, project, fail],
        ):
            handle = await env.client.start_workflow(
                CharacterDesignWorkflow.run,
                run_id,
                id=f"character-design-proof:{run_id}",
                task_queue="character-design-proof",
            )
            await handle.result()
            history = await handle.fetch_history()
        await Replayer(workflows=[CharacterDesignWorkflow]).replay_workflow(history)
    assert calls == [
        ("design", run_id),
        ("design", run_id),
        ("review", run_id),
        ("project", run_id),
    ]
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            payloads = event.activity_task_scheduled_event_attributes.input.payloads
            assert len(payloads) == 1 and payloads[0].data == f'"{run_id}"'.encode()


def test_receipt_round_trip_rejects_invalid_stored_designs() -> None:
    with pytest.raises(CharacterDesignError):
        CharacterDesign.from_receipt({"svg": "", "concept": {}})
    asyncio.run(asyncio.sleep(0))
