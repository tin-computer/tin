"""Typed plan items: articles, answer pages for AI-visibility gaps and page refreshes.

content.plan 0.8.0 (content-editorial-v7) schedules all three; plans written before kinds
existed keep their exact bytes and brief digests, and their items are articles.
"""

import json
from copy import deepcopy
from types import SimpleNamespace

import jsonschema
import pytest
from test_content_plan import fixture_plan, item
from test_content_plan_editorial import context, pages, portfolio
from test_content_programs import setup
from test_content_refresh import EVIDENCE, audit, finding
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_plan as legacy
from tin_lite import content_plan_editorial as editorial
from tin_lite import content_refresh as refresh
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_plan_activities import plan_kinds
from tin_lite.content_plan_sources import refresh_rows
from tin_lite.model_providers import ModelUsage
from tin_lite.organic_audit import canonical_json, digest

GAP = {
    "source_id": "audit:oa_gap",
    "data": {
        "id": "oa_gap",
        "check_id": editorial.ANSWER_CHECK,
        "verification": {
            "kind": "inspect_existing_buyer_answers",
            "questions": [{"question": "Which API tool sends iMessages?", "source_url": ""}],
        },
    },
}
FINDINGS = audit(finding("search.low_ctr", "/pricing"), finding("search.decay", "/blog/old"))


def typed_context():
    data = context()
    data["research"]["rows"] += [GAP, *refresh_rows(FINDINGS, EVIDENCE)]
    return data


def aliases(data):
    return {f"s{index:03d}": row["source_id"] for index, row in enumerate(data["research"]["rows"])}


def typed(count=1, kind="article"):
    proposal = portfolio(count)
    for opportunity in proposal["opportunities"]:
        opportunity["kind"] = kind
    return proposal


def test_old_plans_keep_their_bytes_and_their_items_are_articles():
    plan = fixture_plan()
    plan["batches"][0]["items"] = [item("legacy")]
    raw = canonical_json(plan)
    parsed = legacy.parse_plan(raw)
    assert canonical_json(parsed) == raw
    assert "kind" not in parsed["batches"][0]["items"][0]
    assert digest(parsed["batches"][0]["items"][0]) == digest(item("legacy"))
    assert legacy.item_kind(parsed["batches"][0]["items"][0]) == "article"
    # The pinned v1 model contract never offered the field.
    assert "kind" not in legacy.MODEL_SCHEMA["$defs"]["ContentItem"]["properties"]
    assert "Kind:" not in legacy.render_plan(parsed, label="Old plan")


def test_typed_items_round_trip_and_keep_their_shape_rules():
    plan = fixture_plan()
    answer = {**item("answer"), "kind": "answer"}
    page = {
        **item("refresh"),
        "kind": "refresh",
        "action": "update_page",
        "destination": "https://example.com/pricing",
    }
    plan["batches"][0]["items"] = [answer, page]
    parsed = legacy.parse_plan(canonical_json(plan))
    assert [legacy.item_kind(i) for i in parsed["batches"][0]["items"]] == ["answer", "refresh"]
    report = legacy.render_plan(parsed, label="Typed plan")
    assert "Kind: answer page" in report and "Kind: page refresh" in report
    for bad in (
        {**answer, "action": "update_page", "destination": "https://example.com/x"},
        {**page, "action": "new_page", "destination": ""},
        {**answer, "kind": "video"},
    ):
        plan["batches"][0]["items"] = [bad]
        with pytest.raises(ValueError):
            legacy.ContentPlan.model_validate(plan)


def test_v7_contract_is_typed_and_older_contracts_keep_their_schema():
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY)
    current = editorial.contract(definition)
    assert current.TYPED and current.POLICY["version"] == "content-editorial-v7"
    assert definition["content_schema"] == editorial.MODEL_SCHEMA
    opportunity = editorial.MODEL_SCHEMA["$defs"]["TypedOpportunity"]
    assert "kind" in opportunity["required"]
    v6 = deepcopy(definition)
    v6.update(
        content_policy=editorial.V6_POLICY,
        content_instructions=editorial.V6_INSTRUCTIONS,
        content_schema=editorial.PORTFOLIO_SCHEMA,
    )
    assert not editorial.contract(v6).TYPED
    assert "kind" not in editorial.PORTFOLIO_SCHEMA["$defs"]["Opportunity"]["properties"]
    # An untyped run binds the untyped schema, even when kinds are offered.
    untyped = editorial.bound_schema(pages(), {"s001": "keyword:k1"}, kinds={"article"})
    assert "kind" not in untyped["$defs"]["Opportunity"]["properties"]


def test_the_plan_offers_only_the_kinds_its_evidence_supports():
    assert plan_kinds(context()["research"]) == {"article"}
    assert plan_kinds(typed_context()["research"]) == {"article", "answer", "refresh"}
    schema = editorial.bound_schema(
        pages(), {"s001": "keyword:k1"}, schema=editorial.MODEL_SCHEMA, kinds={"article"}
    )
    assert schema["$defs"]["TypedOpportunity"]["properties"]["kind"]["enum"] == ["article"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(typed(1, "answer"), schema)


def test_refresh_sources_rank_by_impressions_and_lead_the_page_inventory():
    rows = refresh_rows(FINDINGS, EVIDENCE, planned={"/guides/setup": {"search.near_page_one"}})
    assert [row["data"]["path"] for row in rows] == ["/guides/setup", "/pricing", "/blog/old"]
    assert rows[0]["data"]["planned_by"] == "organic.content_efficacy"
    assert rows[2]["data"]["body_allowed"] is True
    assert all(row["source_id"].startswith("refresh:") for row in rows)
    data = typed_context()
    urls, _ = editorial.page_candidates(data)
    assert urls[:3] == [
        "https://example.com/",
        "https://example.com/pricing",
        "https://example.com/blog/old",
    ]
    assert refresh.plan_candidates(FINDINGS, EVIDENCE, limit=1)[0]["path"] == "/pricing"


def test_answers_cite_an_ai_visibility_gap_and_land_without_a_destination():
    data = typed_context()
    proposal = typed(1, "answer")
    gap_alias = next(a for a, s in aliases(data).items() if s == GAP["source_id"])
    proposal["opportunities"][0]["source_ids"] = [gap_alias]
    plan, _ = editorial.allocate(data, proposal, pages(), aliases(data), typed=True)
    [answer] = [i for b in plan["batches"] for i in b["items"]]
    assert answer["kind"] == "answer" and answer["destination"] == ""
    proposal["opportunities"][0]["source_ids"] = ["s000"]  # a keyword, not a gap
    with pytest.raises(ValueError, match="buyer questions"):
        editorial.allocate(data, proposal, pages(), aliases(data), typed=True)


def test_refreshes_target_a_marked_page_and_cite_its_source():
    data = typed_context()
    inventory = pages()  # p001 is /pricing, which the audit flagged
    proposal = typed(1, "refresh")
    proposal["opportunities"][0].update(action="update_page", page_id="p001")
    plan, _ = editorial.allocate(data, proposal, inventory, aliases(data), typed=True)
    [page] = [i for b in plan["batches"] for i in b["items"]]
    assert page["kind"] == "refresh" and page["destination"] == "https://example.com/pricing"
    source = next(r for r in data["research"]["rows"] if r["data"].get("path") == "/pricing")
    assert source["source_id"] in page["source_ids"]
    inventory["pages"][0]["url"] = "https://example.com/unmarked"
    with pytest.raises(ValueError, match="marked for a refresh"):
        editorial.allocate(data, proposal, inventory, aliases(data), typed=True)


def test_typed_articles_match_untyped_ones_and_a_page_gets_one_brief():
    data = typed_context()
    untyped, _ = editorial.allocate(data, portfolio(2), pages(), aliases(data))
    typed_plan, _ = editorial.allocate(data, typed(2), pages(), aliases(data), typed=True)
    assert typed_plan == untyped
    with pytest.raises(ValueError):
        editorial.allocate(data, typed(1), pages(), aliases(data))  # v6 has no kind field
    proposal = typed(2)
    proposal["opportunities"][0].update(action="update_page", page_id="p001", kind="refresh")
    proposal["opportunities"][1].update(action="update_page", page_id="p001")
    plan, quality = editorial.allocate(data, proposal, pages(), aliases(data), typed=True)
    [merged] = [i for b in plan["batches"] for i in b["items"]]
    # An article update rewrites the whole page, so it covers the refresh.
    assert "kind" not in merged and len(quality["consolidations"]) == 1


async def test_the_planner_schedules_all_three_kinds_from_its_research(publication_db, monkeypatch):
    db, storage, project, configured, activities, _model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    seen = {}

    async def research(**kwargs):
        seen.update(kwargs)
        return {
            "scope": {"host": "example.com", "market": "US"},
            "sources": {"audit": {"revision": "c" * 40}},
            "rows": [
                {"source_id": "keyword:k1", "data": {"keyword": "useful query"}},
                GAP,
                *refresh_rows(FINDINGS, EVIDENCE),
            ],
        }

    class Model:
        schemas = []

        async def generate(self, key, request, *, timeout_seconds=None):
            data = json.loads(request.messages[0].content)
            self.schemas.append(request.output_schema)
            sources = {
                row.get("data", {}).get("check_id") or row.get("data", {}).get("keyword"): row[
                    "source_id"
                ]
                for row in data["sources"]
            }
            pricing = next(
                page["page_id"]
                for page in data["pages"]["pages"]
                if page["url"] == "https://example.com/pricing"
            )
            result = typed(3)
            article, answer, page = result["opportunities"]
            article["source_ids"] = [sources["useful query"]]
            answer.update(kind="answer", source_ids=[sources[editorial.ANSWER_CHECK]])
            page.update(
                kind="refresh",
                action="update_page",
                page_id=pricing,
                source_ids=[sources["useful query"]],
            )
            jsonschema.validate(result, request.output_schema)
            return SimpleNamespace(parsed=result, usage=ModelUsage(), request_id="model-test")

    monkeypatch.setattr("tin_lite.content_plan_activities.research_sources", research)
    activities.router = Model()
    run = await create()
    await activities.content_plan_execute(str(run.id))
    saved = await db.get_run(run.id)
    assert saved.status.value == "succeeded"
    assert seen["typed"] is True and seen["planned"] == {}
    enum = Model.schemas[0]["$defs"]["TypedOpportunity"]["properties"]["kind"]["enum"]
    assert enum == ["article", "answer", "refresh"]
    plan = legacy.parse_plan(
        await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=legacy.plan_path(configured.id),
        )
    )
    items = [i for b in plan["batches"] for i in b["items"]]
    assert [legacy.item_kind(i) for i in items] == ["article", "answer", "refresh"]
    assert items[2]["destination"] == "https://example.com/pricing"
    assert any(source.startswith("refresh:") for source in items[2]["source_ids"])
    report = (
        await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=f"reports/content-plan/{run.id}/PLAN.md",
        )
    ).decode()
    assert "Kind: answer page" in report and "Kind: page refresh" in report
