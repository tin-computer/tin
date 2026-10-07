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
from tin_lite.content_plan_sources import competitor_rows, refresh_rows, watch_changes
from tin_lite.model_providers import ModelUsage
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.public_workflows import PUBLIC_WORKFLOWS

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
    assert current.TYPED and current.POLICY["version"] == "content-editorial-v8"
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
    # v8 is v7 with a larger output cap; a definition pinned to v7 keeps its 16,000 tokens.
    assert {
        k: v for k, v in editorial.POLICY.items() if k not in {"version", "max_output_tokens"}
    } == {k: v for k, v in editorial.V7_POLICY.items() if k not in {"version", "max_output_tokens"}}
    assert editorial.POLICY["max_output_tokens"] == 32_000
    assert editorial.V7_POLICY["version"] == "content-editorial-v7"
    assert editorial.V7_POLICY["max_output_tokens"] == 16_000
    assert editorial.V7_INSTRUCTIONS == editorial.INSTRUCTIONS
    v7 = deepcopy(definition)
    v7.update(content_policy=editorial.V7_POLICY)
    pinned = editorial.contract(v7)
    assert pinned.TYPED and pinned.POLICY is editorial.V7_POLICY
    assert pinned.MODEL_SCHEMA == editorial.MODEL_SCHEMA
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


def test_refresh_sources_rank_by_upside_and_lead_the_page_inventory():
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
    # The newest succeeded competitor.watch report, read server-side from its output file.
    watcher = next(w for w in PUBLIC_WORKFLOWS if w.key == "competitor.watch")
    await db.upsert_registry_workflow(
        workflow_id=watcher.id,
        key=watcher.key,
        title=watcher.title,
        description=watcher.description,
        executor=watcher.executor,
        definition_repo_id="registry/workflows",
        definition_path=watcher.definition_path,
        current_commit_sha="f" * 40,
        version_label=watcher.definition["version"],
        definition=watcher.definition,
    )
    watched, _ = await db.create_run(
        project_id=project.id, workflow_id=watcher.id, input_payload={}
    )
    report = f"reports/competitor-watch/{watched.id}.md"
    revision = storage.repo.edit({report: watch_report().encode()})
    await db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, artifact_path=$3, "
        "finished_at=now(), lease_active=false WHERE id=$1",
        watched.id,
        revision,
        report,
    )
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
    items = [i for b in plan["batches"] for i in b["items"] if not i.get("source")]
    assert [legacy.item_kind(i) for i in items] == ["article", "answer", "refresh"]
    assert items[2]["destination"] == "https://example.com/pricing"
    assert any(source.startswith("refresh:") for source in items[2]["source_ids"])
    watched_items = [i for b in plan["batches"] for i in b["items"] if i.get("source")]
    assert [i["title"] for i in watched_items] == ["Linear alternative", "Height alternative"]
    context = await activities.saved(run.id, "context")
    assert context["competitor_watch"]["path"] == report
    report = (
        await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=f"reports/content-plan/{run.id}/PLAN.md",
        )
    ).decode()
    assert "Kind: answer page" in report and "Kind: page refresh" in report


# competitor.watch: material competitor changes become comparison items.

WATCH_EVIDENCE = {
    "version": 1,
    "checked_at": "2026-09-28T10:00:00Z",
    "competitors": [
        {
            "id": "linear.app",
            "name": "Linear",
            "source": "input",
            "pages": [{"url": "https://linear.app/pricing", "kind": "pricing", "status": "read"}],
        },
        {
            "id": "height.app",
            "name": "Height",
            "source": "keyword_plan",
            "pages": [{"url": "https://height.app/pricing", "kind": "pricing", "status": "read"}],
        },
    ],
}


def watch_report(status="changes", evidence=WATCH_EVIDENCE):
    block = json.dumps(evidence, separators=(",", ":"))
    return f"""# Competitor watch, 2026-09-28

Status: {status}
Watched: Linear (linear.app, from input); Height (height.app, from keyword_plan)
Previous check: 2026-09-21T10:00:00Z in reports/competitor-watch/earlier.md
Coverage: 2 of 2 pages read

Linear raised its Standard plan; a comparison page fits while the gap is fresh.

## What changed
- **Linear**: Standard price_monthly 8 -> 10 (https://linear.app/pricing). Pro costs less.
- **Height**: free tier removed (https://height.app/pricing). The free plan still holds.
- **Height**: Pro seat_minimum 1 -> 3 (https://elsewhere.example/height). Seats matter.
- **Unknown Co**: price cut (https://unknown.example/pricing). Not watched.

## Suggested responses
Nothing needs a response this week.

```tin-competitor-watch
{block}
```
"""


def watch(report=None):
    return {
        "run_id": "r1",
        "revision": "c" * 40,
        "path": "reports/competitor-watch/r1.md",
        "changes": watch_changes(report or watch_report()),
    }


def watched_context(*extra_pages):
    data = typed_context()
    data["research"]["rows"] += competitor_rows(watch())
    data["research"]["page_candidates"] += [{"url": url} for url in extra_pages]
    return data


def test_a_watch_report_yields_only_backed_material_changes():
    changes = watch_changes(watch_report())
    assert [(c["competitor"], c["url"]) for c in changes] == [
        ("linear.app", "https://linear.app/pricing"),
        ("height.app", "https://height.app/pricing"),
        # A line citing another site is backed by the competitor's own page instead.
        ("height.app", "https://height.app/pricing"),
    ]
    rows = competitor_rows(watch())
    assert [row["source_id"] for row in rows] == ["competitor:linear.app", "competitor:height.app"]
    assert len(rows[1]["data"]["changes"]) == 2
    for quiet in ("no-change", "baseline", "diagnostic"):
        assert watch_changes(watch_report(status=quiet)) == []
    assert watch_changes(watch_report().replace("```tin-competitor-watch", "```json")) == []


def test_competitor_changes_become_comparison_items_that_cite_the_report():
    data = watched_context("https://example.com/vs/linear")
    plan, added = editorial.competitor_items(data, data["plan"], pages())
    items = {i["id"]: i for b in plan["batches"] for i in b["items"]}
    assert len(added) == 2 and all(items[i]["source"] == "competitor.watch" for i in added)
    linear, height = (items[i] for i in added)
    # The site already compares against Linear: refresh that page; Height gets a new page.
    assert linear["kind"] == "refresh" and linear["destination"] == "https://example.com/vs/linear"
    assert height["title"] == "Height alternative" and "kind" not in height
    assert height["source_ids"] == ["competitor:height.app"]
    assert height["evidence"] == "https://height.app/pricing"
    assert "against https://height.app/pricing" in height["verification"][0]
    assert legacy.parse_plan(canonical_json(plan)) == plan
    assert "From: competitor.watch" in legacy.render_plan(plan, label="Plan")


def test_competitor_items_are_deduplicated_and_capped():
    data = watched_context()
    data["plan"]["batches"][3]["items"] = [
        {**item("existing"), "title": "Linear vs Example", "intent": "Compare with Linear"}
    ]
    data["editable"] = [b["id"] for b in data["plan"]["batches"] if b["id"] != "week_04"]
    plan, added = editorial.competitor_items(data, data["plan"], pages())
    assert [i["title"] for b in plan["batches"] for i in b["items"] if i["id"] in added] == [
        "Height alternative"
    ]
    # A second pass over the same report adds nothing.
    again, more = editorial.competitor_items({**data, "plan": plan}, plan, pages())
    assert more == [] and again == plan
    many = deepcopy(WATCH_EVIDENCE)
    many["competitors"] = [
        {
            **many["competitors"][0],
            "id": f"rival{i}.com",
            "name": f"Rival {i}",
            "pages": [
                {"url": f"https://rival{i}.com/pricing", "kind": "pricing", "status": "read"}
            ],
        }
        for i in range(5)
    ]
    report = watch_report(evidence=many).replace(
        "- **Linear**",
        "\n".join(f"- **Rival {i}**: price up (https://rival{i}.com/pricing)." for i in range(5))
        + "\n- **Linear**",
    )
    capped = typed_context()
    capped["research"]["rows"] += competitor_rows(watch(report))
    _, added = editorial.competitor_items(capped, capped["plan"], pages())
    assert len(added) == editorial.MAX_COMPETITOR_ITEMS == 3


def test_no_competitor_report_changes_nothing():
    data = typed_context()
    plan, added = editorial.competitor_items(data, data["plan"], pages())
    assert added == [] and plan == data["plan"]
