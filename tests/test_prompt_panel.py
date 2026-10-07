"""organic.prompt_panel: families and weights in code, one model call, and the audit asking it.

Audits pinned to v13 or v14 ask the newest panel; v15 drafts its own (test_organic_audit_v15.py).
"""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from loop_workflow_fakes import context, definition, gsc, load
from test_organic_audit import activities_fixture

from tin_lite import prompt_panel
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.organic_audit import V14_AUDIT_POLICY
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "organic.prompt_panel"
CASES = REPOSITORY_ROOT / "workflow_evals" / KEY / "qualification.json"
STAGES = ("discovery", "comparison", "problem", "buying_intent")
TEXTS = {
    "discovery": ["What tools help {x} for a small team?", "Need a way to handle {x} better"],
    "comparison": [
        "How does Asana compare with Trello for {x}?",
        "{x} tools compared side by side",
    ],
    "problem": [
        "Why does {x} keep slipping every single week?",
        "Is there a simpler way to fix {x}?",
    ],
    "buying_intent": [
        "Any recommendations for software that handles {x}?",
        "What are good paid options for {x} right now?",
    ],
}
TOPICS = {
    "F1": "team deadlines",
    "C1": "project status",
    "C2": "task handoffs",
    "C3": "weekly reviews",
}
BRANDED = [
    {"topic": "pricing", "text": "How much does Loopwell cost per month for a team?"},
    {"topic": "reviews", "text": "What do people say about Loopwell after a few months?"},
    {"topic": "integrations", "text": "Does Loopwell work with Slack and Google Calendar?"},
    {"topic": "versus", "text": "Loopwell or Asana for keeping a small team on track?"},
]
BRAND = """# Loopwell brand

## Brand direction

Loopwell is a team deadline tracker for small product teams.

## Visual style

Plain.

```json
{"schema": "tin-brand.v1", "name": "Loopwell",
 "light": {"ink": "#111111", "paper": "#FFFFFF", "accent": "#287A55"}}
```
"""
INDEX = """# Loopwell

### Feature map

- Deadline tracking across projects, status reports, handoff checklists, weekly reviews.

### Code map

- web/
"""
FILES = {"brand/BRAND.md": BRAND, "wiki/INDEX.md": INDEX}


def queries():
    rows = [
        ("project status report template", 9000),
        ("weekly status update", 3000),
        ("task handoff checklist", 700),
        ("weekly review meeting agenda", 300),
        ("team deadline tracker", 200),
        ("asana alternative", 100),
        ("loopwell login", 400),
    ]
    return gsc(
        [{"keys": [q, "https://loopwell.example/"], "clicks": 1, "impressions": n} for q, n in rows]
    )


def family(fid, text=None):
    prompts = [
        {"stage": stage, "text": t.format(x=TOPICS[fid])} for stage in STAGES for t in TEXTS[stage]
    ]
    if text:
        prompts[0]["text"] = text
    return {"id": fid, "name": f"Buyers tracking {TOPICS[fid]}", "prompts": prompts}


def answer(first_text=None, **overrides):
    value = {
        "core": {
            "name": "team deadline tracking",
            "same_as": "C4",
            "quote": "Loopwell is a team deadline tracker for small product teams.",
        },
        "aliases": ["Loopwell app"],
        "competitors": ["Asana", "Trello"],
        "families": [family("F1", first_text), family("C1"), family("C2"), family("C3")],
        "branded": [{**b, "fact_check": "brand/BRAND.md"} for b in BRANDED],
    }
    value.update(overrides)
    return value


def block_of(content):
    return prompt_panel.parse(content)


async def run_panel(monkeypatch, *, files=FILES, models=None, inputs=None, **services):
    module = load(KEY, monkeypatch)
    ctx = context(
        files=files,
        services={"G1_queries": queries(), **services},
        models=models if models is not None else {"prompts": answer()},
    )
    result = await module.run(ctx, {"target": "loopwell.example", **(inputs or {})})
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition(KEY)))
    return result["content"], ctx


def test_manifest_is_code_with_one_model_route_and_no_review():
    spec = validate_code_definition(definition(KEY))
    assert "human_review" not in definition(KEY)
    assert [(r.name, r.model, r.max_calls) for r in spec.model_routes] == [
        ("prompts", "gpt-6-luna", 2)
    ]
    assert spec.output_path == prompt_panel.PANEL_PATH
    assert definition(KEY)["code"]["services"]["gsc"]["max_calls"] == 1


def test_the_core_family_leads_even_when_adjacent_searches_have_the_impressions(monkeypatch):
    """The private panel inherited an SEO-tool keyword plan and gave the core category 5.2%."""
    weights = load(KEY, monkeypatch).family_weights({"F2": 50_000, "F3": 10, "F4": 10})
    assert weights["F1"] == 0.40 and max(weights[k] for k in ("F2", "F3", "F4")) < 0.40
    assert abs(sum(weights.values()) - 1) < 0.001 and min(weights.values()) >= 0.12
    even = load(KEY, monkeypatch).family_weights({"F2": None, "F3": 100, "F4": 5})
    assert even == {"F1": 0.40, "F2": 0.2, "F3": 0.2, "F4": 0.2}


async def test_code_picks_families_and_weights_and_one_call_writes_the_prompts(monkeypatch):
    content, ctx = await run_panel(monkeypatch)
    block = block_of(content)
    assert block["status"] == "ready" and block["name"] == "Loopwell"
    families = {f["id"]: f for f in block["families"]}
    assert families["F1"]["name"] == "team deadline tracking" and families["F1"]["weight"] == 0.4
    assert families["F1"]["source"] == "C4"  # the model said the tracker group is the core
    # Adjacent families by Search Console impressions; the branded query stays out.
    assert [families[f]["head_words"] for f in ("F2", "F3", "F4")] == [
        ["status"],
        ["task"],
        ["weekly"],
    ]
    assert families["F2"]["impressions"] == 12_000 and families["F2"]["weight"] < 0.40
    assert len(block["prompts"]) == 32 and len(block["branded"]) == 4
    flagged = [p for p in block["prompts"] if p["flags"]]
    assert flagged and all(
        set(p["flags"]) == {"competitor:asana", "competitor:trello"} for p in flagged
    )
    assert block["sources"]["competitors"] == ["Asana", "Trello"]
    assert [c["step"] for c in ctx.models.calls] == ["prompts"]
    request = ctx.models.calls[0]
    assert request["route"] == "prompts"
    assert "loopwell login" not in json.dumps(request["data"])
    assert "check_panel passed." in content and "Status: ready" in content
    assert "the families come from Search Console alone" in content
    assert load(KEY, monkeypatch).check_panel(block, "loopwell.example", ["Asana"]) == []


async def test_brand_and_comparison_searches_never_outweigh_the_core_category(monkeypatch):
    """Most of a real site's impressions came from its own name and its /alternatives pages."""
    skewed = [
        ("loopwell", "/", 90_000),
        ("loopwell pricing", "/pricing", 20_000),
        ("asana alternatives", "/alternatives/asana", 40_000),
        ("best status report tools", "/compare/status-tools", 30_000),
        ("asana vs trello", "/blog/asana-vs-trello", 15_000),
        ("project status report template", "/templates/status", 20_000),
        ("task handoff checklist", "/blog/handoffs", 30),
        ("weekly review meeting agenda", "/blog/reviews", 20),
        ("team deadline tracker", "/", 10),
    ]
    rows = gsc(
        [
            {"keys": [q, f"https://loopwell.example{p}"], "clicks": 1, "impressions": n}
            for q, p, n in skewed
        ]
    )
    content, ctx = await run_panel(monkeypatch, G1_queries=rows)
    block = block_of(content)
    weights = {f["id"]: f["weight"] for f in block["families"]}
    assert weights["F1"] == 0.40 and max(weights.values()) == 0.40
    assert all(0.12 <= weights[f] < 0.40 for f in ("F2", "F3", "F4"))
    impressions = {f["id"]: f["impressions"] for f in block["families"]}
    assert impressions["F2"] == 20_000  # brand, alternatives and "x vs y" searches are out
    sent = json.dumps(ctx.models.calls[0]["data"]["candidate_groups"])
    assert "loopwell" not in sent and "alternatives" not in sent and "asana vs" not in sent
    assert "85000 impressions on comparison and alternatives pages were left out" in content
    assert "Asana" in block["sources"]["competitors"]


async def test_a_forcing_prompt_gets_one_corrective_call(monkeypatch):
    """A plausible but unusable answer: one prompt forces a single pick."""
    bad = answer("Which project tool is best for early-stage startups?")
    content, ctx = await run_panel(monkeypatch, models={"prompts": bad, "prompts_fix": answer()})
    assert [c["step"] for c in ctx.models.calls] == ["prompts", "prompts_fix"]
    assert "forcing clause" in json.dumps(ctx.models.calls[1]["data"]["failures"])
    assert "best for early-stage startups" not in content


async def test_a_panel_that_still_fails_is_not_published(monkeypatch):
    bad = answer("Is Loopwell good for tracking team deadlines?")
    with pytest.raises(ValueError, match="names the product outside the branded family"):
        await run_panel(monkeypatch, models={"prompts": bad, "prompts_fix": bad})


async def test_an_answer_outside_the_schema_publishes_nothing(monkeypatch):
    with pytest.raises(ValueError, match="does not match its schema"):
        await run_panel(monkeypatch, models={"prompts": {"families": "all of them"}})


async def test_without_positioning_the_run_stops_and_names_the_files(monkeypatch):
    content, ctx = await run_panel(monkeypatch, files={})
    assert "Status: stopped" in content and "brand/BRAND.md" in content
    assert block_of(content) is None
    assert ctx.models.calls == [] and ctx.services.calls == []


async def test_without_search_console_the_weights_split_evenly(monkeypatch):
    content, _ = await run_panel(
        monkeypatch,
        G1_queries=ValueError("Search Console refused the read"),
        models={
            "prompts": answer(
                core={"name": "team deadline tracking", "same_as": "none", "quote": "x"},
                families=[
                    family("F1"),
                    {**family("C1"), "id": "X1"},
                    {**family("C2"), "id": "X2"},
                    {**family("C3"), "id": "X3"},
                ],
            )
        },
    )
    block = block_of(content)
    assert [f["weight"] for f in block["families"]] == [0.4, 0.2, 0.2, 0.2]
    assert "weights are split evenly" in content


def panel_row(text, *, key="organic.prompt_panel", decision=None):
    run = SimpleNamespace(
        id=uuid4(),
        review_decision=decision,
        canonical_commit_sha=uuid4().hex + "e" * 8,
        artifact_path=prompt_panel.PANEL_PATH,
    )
    return (key, run), text


async def audit_with(db, storage, rows):
    texts = {run.canonical_commit_sha: text for (_, run), text in rows}
    original = storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs.get("path") == prompt_panel.PANEL_PATH:
            return texts[kwargs["commit_sha"]].encode()
        return await original(**kwargs)

    storage.read_canonical_artifact = AsyncMock(side_effect=read)
    db.list_prerequisite_runs = AsyncMock(return_value=[row for row, _ in rows])


async def published(monkeypatch, target="example.com"):
    content, _ = await run_panel(monkeypatch, inputs={"target": target})
    return content


@pytest.mark.asyncio
async def test_the_audit_asks_the_newest_succeeded_panel_without_approval(monkeypatch):
    content = await published(monkeypatch)
    activities, db, storage, _ = await activities_fixture(policy=V14_AUDIT_POLICY)
    run_id = str(db.run.id)
    await audit_with(db, storage, [panel_row(content)])  # nobody approved it
    activities.responses = SimpleNamespace(create=AsyncMock(side_effect=AssertionError))
    # Eight questions, three answers with web search and one without: 32, as before.
    assert await activities.organic_prepare_panel(run_id) == 32
    saved = await activities._result(run_id, "panel")
    assert saved["status"] == "completed" and len(saved["questions"]) == 8
    assert saved["name"] == "Loopwell" and saved["origin"]["workflow"] == "organic.prompt_panel"
    per_family = {}
    for question in saved["questions"]:
        per_family[question["job"]] = per_family.get(question["job"], 0) + 1
    assert per_family["team deadline tracking"] == 3  # core, 0.40 of 8
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["method"] == "buyer_prompt_panel"
    # A retry asks the same questions without reading the panel again.
    db.list_prerequisite_runs.reset_mock()
    assert await activities.organic_prepare_panel(run_id) == 32
    assert db.list_prerequisite_runs.await_count == 0


@pytest.mark.asyncio
async def test_other_workflows_other_sites_and_unready_panels_are_ignored(monkeypatch):
    from tin_lite.organic_audit_panel import panel_questions

    ours = await published(monkeypatch)
    other_site = await published(monkeypatch, target="loopwell.example")
    not_ready = ours.replace('"status": "ready"', '"status": "draft"')
    for row in (
        panel_row(ours, key="organic.audit"),
        panel_row(other_site),  # loopwell.example is not the audited example.com
        panel_row(not_ready),
    ):
        activities, db, storage, _ = await activities_fixture(policy=V14_AUDIT_POLICY)
        run_id = str(db.run.id)
        await audit_with(db, storage, [row])
        scope = await activities._result(run_id, "scope")
        assert await panel_questions(activities, run_id, scope) is None


@pytest.mark.asyncio
async def test_refresh_questions_skips_the_panel(monkeypatch):
    from tin_lite.organic_audit_panel import panel_questions

    activities, db, storage, _ = await activities_fixture(policy=V14_AUDIT_POLICY)
    db.run = replace(db.run, input={**db.run.input, "refresh_questions": True})
    run_id = str(db.run.id)
    await audit_with(db, storage, [panel_row(await published(monkeypatch))])
    scope = await activities._result(run_id, "scope")
    assert await panel_questions(activities, run_id, scope) is None
    assert db.list_prerequisite_runs.await_count == 0


QUALIFICATION = {
    "ordinary": ({}, FILES),
    "no_positioning": ({}, {}),
    "forcing_clause_must_fail": (
        {
            "prompts": answer("which project management tool is best for early-stage startups"),
            "prompts_fix": answer(),
        },
        FILES,
    ),
}


async def test_qualification_cases_pass_on_their_fixtures(monkeypatch):
    qualification = Qualification.model_validate_json(CASES.read_text())
    assert {case.id for case in qualification.cases} == set(QUALIFICATION)
    for case in qualification.cases:
        models, files = QUALIFICATION[case.id]
        module = load(KEY, monkeypatch)
        ctx = context(
            files=files,
            services={"G1_queries": queries()},
            models=models or {"prompts": answer()},
        )
        result = await module.run(ctx, dict(case.inputs))
        verdict = assess_output(case, status="succeeded", content=result["content"].encode())
        assert verdict["status"] == "passed", (case.id, verdict["checks"])
