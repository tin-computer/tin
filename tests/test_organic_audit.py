from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_procedure_publication import HistoryStorage, run_fixture

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.dataforseo import DataForSEO, DataForSEOError
from tin_lite.domain import EffectReceipt
from tin_lite.organic_audit import (
    ARTIFACT_LIMITS,
    AUDIT_POLICY,
    audit_paths,
    build_documents,
    canonical_json,
    normalize_pages,
    public_site,
    technical_findings,
)
from tin_lite.organic_audit_activities import OrganicAuditActivities
from tin_lite.organic_audit_ai import (
    AI_CONTRACT,
    classify,
    payload,
    read_response,
    summarize,
    validate_panel,
)
from tin_lite.organic_audit_publication import publish_audit
from tin_lite.publication import OutputConflictError, PublicationPendingError


def test_catalog_description_names_bounded_seo_and_geo_scope():
    spec = next(workflow for workflow in BUILTIN_WORKFLOWS if workflow.key == "organic.audit")
    assert "technical SEO and AI visibility (GEO)" in spec.description
    assert "up to 100 public pages" in spec.description
    assert "mention, cite, or recommend your business" in spec.description
    assert "No GitHub required." in spec.description
    assert "frozen buyer-question" not in spec.description
    assert spec.definition["description"] == spec.description


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://[::1]",
        "https://example.com:8443",
        "https://user:password@example.com",
        "https://example.com/?token=x",
        "https://example.com/path",
        "https://example.com/#fragment",
        "https://a.internal",
        "https://example.com./",
        "https://bad_host.com",
        "https://example.com\n",
    ],
)
def test_scope_rejects_unsafe_or_ambiguous_targets(url):
    with pytest.raises(ValueError):
        public_site(url)


def test_explicit_www_host_is_preserved():
    assert public_site("https://WWW.example.com") == ("https://www.example.com/", "www.example.com")


def page_fixture(**changes):
    return {
        "url": "https://example.com/",
        "resource_type": "html",
        "status_code": 200,
        "meta": {"title": "Example", "description": "A product"},
        "checks": {"canonical": True, "no_title": True, "canonical_to_broken": False},
        "duplicate_title": True,
        **changes,
    }


def test_provider_top_level_fields_and_unknown_are_not_passes():
    pages = normalize_pages([page_fixture(), page_fixture(url="https://other.com/")], "example.com")
    findings, coverage = technical_findings(pages, "example.com")
    assert {item["check_id"] for item in findings} == {
        "metadata.title_missing",
        "metadata.title_duplicate",
    }
    assert any(item["status"] == "unknown" for item in coverage)
    assert len(pages) == 1
    assert all(item["next_action"] == "technical_fix" for item in findings)
    assert technical_findings(pages, "example.com")[0] == findings


def document_fixture(run_id):
    return build_documents(
        run_id=run_id,
        project_id=str(uuid4()),
        definition_sha="d" * 40,
        scope={
            "url": "https://example.com/",
            "host": "example.com",
            "market": "US",
            "started_at": "2026-09-08",
        },
        crawl={"status": "completed", "pages": normalize_pages([page_fixture()], "example.com")},
        ai={"status": "partial", "summary": "Not measured."},
        spending={},
    )


def test_three_bounded_artifacts_and_stable_downstream_inventory():
    run_id = str(uuid4())
    docs = document_fixture(run_id)
    paths = audit_paths(run_id)
    assert set(docs) == set(paths.values())
    assert all(
        len(docs[paths[name]]) <= limit <= 3_000_000 for name, limit in ARTIFACT_LIMITS.items()
    )
    findings = json.loads(docs[paths["findings.json"]])
    assert findings["downstream_authority"] == "recommendations_only"
    assert len(findings["evidence_sha256"]) == 64
    assert b"Result: partial: site files were not collected" in docs[paths["AUDIT.md"]]


def response(text="Useful answer", *, search=True, citations=None):
    return {
        "id": "resp_test",
        "status": "completed",
        "model": "gpt-6-luna",
        "usage": {"input_tokens": 500, "output_tokens": 200},
        "output": [
            *(
                [
                    {
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {"sources": [{"url": "https://example.com/"}]},
                    }
                ]
                if search
                else []
            ),
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": citations or []}],
            },
        ],
    }


def panel_fixture():
    return {
        "site_type": "saas",
        "host": "example.com",
        "name": "Acme",
        "aliases": ["Acme Tools"],
        "competitor_names": ["Rival Brand"],
        "public_description": "A public tool for planning team work.",
        "questions": [
            {
                "job": "Planning team work",
                "family": family,
                "question": question,
                "fit_reason": "This buyer needs an appropriate team planning product.",
                "source_url": "https://example.com/",
            }
            for family, question in zip(
                ["discovery", "problem", "comparison", "constraint"],
                [
                    "Which tools help small teams plan weekly work?",
                    "How can a small team coordinate work without spreadsheets?",
                    "What should I compare when choosing a team planning tool?",
                    "Which team planning tools work well for a team of three?",
                ],
                strict=True,
            )
        ],
    }


def test_panel_is_frozen_unbranded_and_anchored_to_observed_public_sources():
    observation = read_response(response(json.dumps(panel_fixture())), search=True)
    panel = validate_panel(observation, "example.com")
    assert panel["planned_observations"] == 8
    assert len(panel["sha256"]) == 64
    data = panel_fixture()
    data["questions"][0]["question"] = "Why should I use Acme for planning team work?"
    with pytest.raises(ValueError, match="leak"):
        validate_panel({**observation, "text": json.dumps(data)}, "example.com")
    with pytest.raises(ValueError, match="different"):
        validate_panel(observation, "other.com")
    with pytest.raises(ValueError, match="evidence"):
        validate_panel({**observation, "sources": []}, "example.com")


def test_answer_request_has_no_target_or_project_context():
    request = payload(
        stage="answer",
        data="Which planning tools work well for a small team?",
        market="US",
        search=True,
    )
    assert request["tool_choice"] == "required" and request["max_tool_calls"] == 3
    assert request["model"] == AUDIT_POLICY["model"]
    assert "Acme" not in str(request) and "example.com" not in str(request)
    assert request["store"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(status="incomplete"),
        lambda r: r.update(output=r["output"][1:]),
        lambda r: r["output"][0].update(status="failed"),
        lambda r: r["output"][1]["content"][0].update(text=""),
    ],
)
def test_incomplete_answer_is_not_scored_as_absence(mutate):
    raw = response()
    mutate(raw)
    with pytest.raises(ValueError):
        read_response(raw, search=True)


def test_mentions_citations_and_recommendations_are_independent():
    panel = panel_fixture()
    observation = {"text": "Acme is not a good fit for this buyer.", "citations": []}
    judgment = {
        "text": json.dumps(
            {
                "mentioned": True,
                "mention_quote": observation["text"],
                "shortlisted": False,
                "shortlist_quote": "",
                "selected_first": False,
                "first_choice_quote": "",
            }
        )
    }
    result = classify(observation, judgment, panel)
    assert result["mentioned"] and not result["shortlisted"] and not result["owned_domain_cited"]
    neutral = json.loads(judgment["text"])
    neutral.update(mentioned=False, mention_quote="")
    judgment = {"text": json.dumps(neutral)}
    result = classify(
        {
            "text": "This source provides useful context.",
            "citations": ["https://example.com/facts"],
        },
        judgment,
        panel,
    )
    assert not result["mentioned"] and result["owned_domain_cited"]
    result = classify(
        {"text": "A neutral answer.", "citations": ["https://example.com.evil.org/"]},
        judgment,
        panel,
    )
    assert not result["owned_domain_cited"]


def test_recommendation_requires_exact_target_bearing_quote():
    observation = {"text": "Acme may be worth considering.", "citations": []}
    bad = {
        "mentioned": True,
        "mention_quote": observation["text"],
        "shortlisted": True,
        "shortlist_quote": "Acme is the best choice.",
        "selected_first": True,
        "first_choice_quote": "Acme is the best choice.",
    }
    with pytest.raises(ValueError):
        classify(observation, {"text": json.dumps(bad)}, panel_fixture())


def test_missing_observation_withholds_comparable_metrics():
    assert summarize({"planned_observations": 8}, [])["metrics"] is None


@pytest.mark.asyncio
async def test_provider_auth_fixed_endpoint_and_bounded_crawl():
    seen = []
    task_id = str(uuid4())

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [{"id": task_id, "status_code": 20100, "cost": 0}],
            },
        )

    provider = DataForSEO(
        "private-login", "private-password", transport=httpx.MockTransport(handle)
    )
    request = provider.crawl_request(host="example.com", tag="tin-test")
    result = await provider.submit(request)
    assert result["task_id"] == task_id
    assert seen[0].url == "https://api.dataforseo.com/v3/on_page/task_post"
    assert request["max_crawl_pages"] == 100 and request["allow_subdomains"] is False
    assert not request["enable_javascript"] and "robots_txt_merge_mode" not in request
    assert "private" not in str(result)


@pytest.mark.asyncio
async def test_provider_error_does_not_expose_body_or_retry():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500, text="secret provider details")

    provider = DataForSEO("login", "password", transport=httpx.MockTransport(handle))
    with pytest.raises(DataForSEOError) as exc:
        await provider.submit(provider.crawl_request(host="example.com", tag="x"))
    assert "secret" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_task_recovery_requires_exact_metadata_not_just_tag():
    wanted = DataForSEO.crawl_request(host="example.com", tag="unique")
    task_id = str(uuid4())

    def handle(request):
        assert request.url.path == "/v3/on_page/id_list"
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "status_code": 20000,
                        "result": [
                            {"id": str(uuid4()), "metadata": {**wanted, "target": "other.com"}},
                            {"id": task_id, "metadata": wanted, "cost": 0.015},
                        ],
                    }
                ],
            },
        )

    provider = DataForSEO("login", "password", transport=httpx.MockTransport(handle))
    result = await provider.recover(request=wanted, submitted_at="2026-09-08T00:00:00+00:00")
    assert result["task_id"] == task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("later_edit", [b"Founder corrected the report", None])
async def test_lost_publication_recovers_original_revision_without_overwriting_later_edit(
    later_edit,
):
    storage, run_id = HistoryStorage(), str(uuid4())
    docs = document_fixture(run_id)
    intent = {}

    async def save(value):
        intent.update(value)

    args = dict(
        storage=storage,
        repo_id=storage.repo.id,
        branch="main",
        run_id=run_id,
        documents=docs,
        save_intent=save,
        validate_active=AsyncMock(),
    )
    storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await publish_audit(**args, intent=None)
    original = storage.repo.head
    path = audit_paths(run_id)["AUDIT.md"]
    storage.repo.edit({path: later_edit})
    later = storage.repo.head
    assert await publish_audit(**args, intent=intent) == original
    assert storage.repo.head == later and storage.repo.writes == 1


@pytest.mark.asyncio
async def test_existing_output_even_identical_is_never_overwritten_without_intent():
    storage, run_id = HistoryStorage(), str(uuid4())
    docs = document_fixture(run_id)
    storage.repo.edit(docs)
    with pytest.raises(OutputConflictError):
        await publish_audit(
            storage=storage,
            repo_id=storage.repo.id,
            branch="main",
            run_id=run_id,
            documents=docs,
            intent=None,
            save_intent=AsyncMock(),
            validate_active=AsyncMock(),
        )
    assert storage.repo.writes == 0


class MemoryDB:
    """Exercise receipt decisions; separate Postgres tests prove SQL atomicity."""

    def __init__(self):
        self.run = replace(
            run_fixture(),
            executor="organic.audit",
            input={"site_url": "https://example.com/", "market": "US"},
        )
        self.project = SimpleNamespace(
            id=self.run.project_id,
            state_repo_id="projects/publication-test",
            canonical_branch="main",
        )
        self.effects = {}
        self.locks = {}
        self.mark_run_running = AsyncMock()
        self.project_run_progress = AsyncMock()
        self.project_failure = AsyncMock()
        self.complete_organic_audit_projection = AsyncMock()

    async def get_run(self, run_id, **kwargs):
        assert run_id == self.run.id
        return self.run

    async def get_project(self, project_id, **kwargs):
        assert project_id == self.project.id
        return self.project

    async def get_effect(self, key, **kwargs):
        return self.effects.get(key)

    @asynccontextmanager
    async def effect_lock(self, key, operation):
        async with self.locks.setdefault(key, asyncio.Lock()):
            yield self, self.effects.get(key)

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id):
        yield

    async def start_effect(self, conn, *, execution_key, operation):
        self.effects.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", {})
        )

    async def save_effect_progress(self, conn, *, execution_key, result):
        self.effects[execution_key] = replace(self.effects[execution_key], result=result)

    async def save_publication_intent(self, conn, *, execution_key, intent):
        await self.save_effect_progress(
            conn, execution_key=execution_key, result={"publication": intent}
        )

    async def complete_effect(self, conn, *, execution_key, result):
        self.effects[execution_key] = replace(
            self.effects[execution_key], status="completed", result=result
        )


async def activities_fixture(*, budget="8"):
    db, storage = MemoryDB(), HistoryStorage()
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    storage.read_canonical_artifact = AsyncMock(return_value=canonical_json(definition))
    provider = SimpleNamespace(
        validate_target=AsyncMock(return_value=("https://example.com/", "example.com")),
        crawl_request=DataForSEO.crawl_request,
        submit=AsyncMock(return_value={"task_id": str(uuid4()), "reported_cost_usd": "0.015"}),
        recover=AsyncMock(),
        summary=AsyncMock(return_value={"crawl_progress": "finished", "domain": "example.com"}),
        pages=AsyncMock(return_value=[page_fixture()]),
        stop=AsyncMock(),
    )
    activities = OrganicAuditActivities(
        database=db,
        storage=storage,
        settings=SimpleNamespace(organic_audit_max_cost_usd=budget),
        provider=provider,
        site_resolver=AsyncMock(return_value={"status": "observed", "redirects": []}),
    )
    await activities.organic_prepare(str(db.run.id))
    return activities, db, storage, provider


@pytest.mark.asyncio
async def test_full_native_technical_path_survives_duplicate_delivery():
    activities, db, storage, provider = await activities_fixture()
    run_id = str(db.run.id)
    for _ in range(2):
        await activities.organic_prepare(run_id)
        await activities.organic_start_crawl(run_id)
        assert await activities.organic_poll_crawl(run_id)
        await activities.organic_end_crawl(run_id)
        assert await activities.organic_prepare_panel(run_id) == 0  # No model configured.
        await activities.organic_publish(run_id)
        await activities.organic_project(run_id)
    assert provider.submit.await_count == 1
    assert provider.pages.await_count == 1
    assert storage.repo.writes == 1
    report = storage.repo.trees[storage.repo.head][audit_paths(run_id)["AUDIT.md"]][1]
    assert b"Result: partial" in report and b"HTML title is missing" in report


@pytest.mark.asyncio
async def test_model_unknown_is_not_resent_and_retains_reservation():
    activities, db, _, _ = await activities_fixture(budget="0.25")
    run_id = str(db.run.id)
    call = AsyncMock(side_effect=TimeoutError)
    first = await activities._paid(run_id, "answer:0", {"question": "x"}, "0.20", call)
    second = await activities._paid(run_id, "answer:0", {"question": "x"}, "0.20", call)
    assert first == second and first["status"] == "unknown" and call.await_count == 1
    denied = await activities._paid(run_id, "answer:1", {"question": "y"}, "0.20", call)
    assert denied["reason"] == "spending_limit" and call.await_count == 1


@pytest.mark.asyncio
async def test_crash_after_crawl_acceptance_recovers_without_second_submit():
    activities, db, _, provider = await activities_fixture()
    run_id = str(db.run.id)
    provider.submit.side_effect = TimeoutError
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError):
        await activities.organic_start_crawl(run_id)
    provider.recover.return_value = {"task_id": str(uuid4()), "reported_cost_usd": "0.015"}
    await activities.organic_start_crawl(run_id)
    assert provider.submit.await_count == 1 and provider.recover.await_count == 1


def test_registry_pins_policy_and_instructions_without_github_or_review():
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    assert definition["audit_policy"] == AUDIT_POLICY
    assert definition["audit_instructions"] == AI_CONTRACT
    assert definition["schedule_modes"] == ["on_demand"]
    assert not definition.get("human_review") and not definition.get("integration_requirements")
