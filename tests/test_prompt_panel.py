"""organic.prompt_panel: its weights and checks, and the organic audit asking an approved panel."""

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_organic_audit import activities_fixture

from tin_lite import prompt_panel
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = Path(__file__).parents[1]
PANEL_MD = ROOT / "workflow_packages/organic.prompt_panel/skills/prompt-panel/PANEL.md"
CASES = ROOT / "workflow_evals/organic.prompt_panel/qualification.json"
STAGES = ("discovery", "comparison", "problem", "buying_intent")


def resource():
    blocks = re.findall(r"```python\n(.*?)\n```", PANEL_MD.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    exec(compile(blocks[0], str(PANEL_MD), "exec"), namespace)  # noqa: S102
    return namespace


PANEL = resource()
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
TOPICS = ["team deadlines", "project status", "weekly reviews", "task handoffs"]


def panel(**overrides):
    impressions = {"F2": 9000, "F3": 700, "F4": 300}
    weights = PANEL["family_weights"](impressions)
    families = [
        {
            "id": f"F{i + 1}",
            "role": "core" if i == 0 else "adjacent",
            "name": f"Buyers tracking {topic}",
            "head_words": [topic.split()[0]],
            "impressions": impressions.get(f"F{i + 1}"),
            "volume": 100 if i == 0 else 5000,
            "volume_share": 0.05 if i == 0 else 0.3,
            "weight": weights[f"F{i + 1}"],
            "top_queries": [topic],
        }
        for i, topic in enumerate(TOPICS)
    ]
    prompts = []
    for i, topic in enumerate(TOPICS):
        n = 0
        for stage in STAGES:
            for text in TEXTS[stage]:
                n += 1
                named = ["competitor:asana", "competitor:trello"] if "Asana" in text else []
                prompts.append(
                    {
                        "id": f"F{i + 1}-{n}",
                        "family": f"F{i + 1}",
                        "stage": stage,
                        "text": text.format(x=topic),
                        "flags": named,
                        "source_queries": [topic],
                    }
                )
    value = {
        "schema": "tin.prompt_panel/1",
        "status": "draft",
        "target": "loopwell.example",
        "name": "Loopwell",
        "aliases": ["Loopwell app"],
        "generated_at": "2026-09-29T10:00:00Z",
        "low_confidence": False,
        "sources": {"positioning": ["brand/BRAND.md"], "competitors": ["Asana", "Trello"]},
        "families": families,
        "prompts": prompts,
        "branded": [
            {
                "id": "B1",
                "topic": "pricing",
                "text": "How much does Loopwell cost per month for a team?",
                "fact_check": "pricing page",
            },
            {
                "id": "B2",
                "topic": "reviews",
                "text": "What do people say about Loopwell after a few months?",
                "fact_check": "reviews",
            },
            {
                "id": "B3",
                "topic": "integrations",
                "text": "Does Loopwell work with Slack and Google Calendar?",
                "fact_check": "integrations",
            },
            {
                "id": "B4",
                "topic": "versus",
                "text": "Loopwell or Asana for keeping a small team on track?",
                "fact_check": "comparison",
            },
        ],
    }
    value.update(overrides)
    return value


def report(value):
    return (
        "The panel leads with team deadline tracking.\n\n## Intent families\n\n## Checks\n\nOK\n\n"
        "## Approval\n\nStatus: draft\n\n<!-- prompts.json:start -->\n```json\n"
        + json.dumps(value, indent=2)
        + "\n```\n<!-- prompts.json:end -->\n"
    )


def test_the_core_family_leads_even_when_adjacent_keywords_have_the_volume():
    """The private panel inherited an SEO-tool keyword plan and gave the core category 5.2%."""
    weights = PANEL["family_weights"]({"F2": 50_000, "F3": 10, "F4": 10})
    assert weights["F1"] == 0.40 and max(weights[k] for k in ("F2", "F3", "F4")) < 0.40
    assert abs(sum(weights.values()) - 1) < 0.001 and min(weights.values()) >= 0.12
    even = PANEL["family_weights"]({"F2": None, "F3": 100, "F4": 5})
    assert even == {"F1": 0.40, "F2": 0.2, "F3": 0.2, "F4": 0.2}


def test_a_good_panel_passes_the_check():
    assert PANEL["check_panel"](panel(), "loopwell.example", ["Asana", "Trello"]) == []


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        (
            lambda p: p["prompts"][0].update(
                text="Which project tool is best for early-stage startups?"
            ),
            "forcing clause",
        ),
        (
            lambda p: p["prompts"][1].update(text="Is Loopwell good for tracking team deadlines?"),
            "names the product",
        ),
        (lambda p: p["families"][0].update(weight=0.052), "F1 weighs 0.052"),
        (lambda p: p.update(status="frozen"), "status must be draft"),
        (lambda p: p["branded"].pop(), "branded prompts; the panel needs 4"),
        (lambda p: p["prompts"][2].update(flags=[]), "names Asana but carries no competitor"),
    ],
)
def test_the_check_catches_a_plausible_but_unusable_panel(change, failure):
    value = panel()
    change(value)
    fails = PANEL["check_panel"](value, "loopwell.example", ["Asana", "Trello"])
    assert any(failure in line for line in fails), fails


def test_an_approved_panel_becomes_eight_questions_allocated_by_weight():
    block = prompt_panel.parse(report(panel()))
    asked = prompt_panel.questions(block, 8, "https://loopwell.example/")
    per_family = {}
    for question in asked:
        per_family[question["job"]] = per_family.get(question["job"], 0) + 1
    assert len(asked) == 8
    assert per_family["Buyers tracking team deadlines"] == 3  # core, 0.40 of 8
    assert {q["family"] for q in asked} <= {"discovery", "comparison", "problem", "constraint"}
    first_core = [q for q in asked if q["job"] == "Buyers tracking team deadlines"]
    assert [q["family"] for q in first_core] == ["discovery", "comparison", "problem"]
    assert prompt_panel.identity(block, "loopwell.example")["competitor_names"] == [
        "Asana",
        "Trello",
    ]
    assert prompt_panel.parse("no block") is None


def approved_run(text, *, decision="approved", key="organic.prompt_panel"):
    run = SimpleNamespace(
        id=uuid4(),
        review_decision=decision,
        canonical_commit_sha="e" * 40,
        artifact_path=prompt_panel.PANEL_PATH,
    )
    return (key, run), text


async def audit_with(activities, db, storage, rows):
    texts = {run.canonical_commit_sha: text for (_, run), text in rows}
    original = storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs.get("path") == prompt_panel.PANEL_PATH:
            return texts[kwargs["commit_sha"]].encode()
        return await original(**kwargs)

    storage.read_canonical_artifact = AsyncMock(side_effect=read)
    db.list_prerequisite_runs = AsyncMock(return_value=[row for row, _ in rows])


@pytest.mark.asyncio
async def test_the_audit_asks_the_founders_approved_panel_without_drafting():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    value = panel(target="example.com")
    await audit_with(activities, db, storage, [approved_run(report(value))])
    activities.responses = SimpleNamespace(create=AsyncMock(side_effect=AssertionError))
    # Eight questions, three answers with web search and one without: 32, as before.
    assert await activities.organic_prepare_panel(run_id) == 32
    saved = await activities._result(run_id, "panel")
    assert saved["status"] == "completed" and len(saved["questions"]) == 8
    assert saved["name"] == "Loopwell" and saved["origin"]["workflow"] == "organic.prompt_panel"
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["method"] == "founder_approved_panel"
    # A retry asks the same questions without reading the panel again.
    db.list_prerequisite_runs.reset_mock()
    assert await activities.organic_prepare_panel(run_id) == 32
    assert db.list_prerequisite_runs.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {"decision": None},  # a draft nobody approved
        {"key": "organic.audit"},
    ],
)
async def test_unapproved_or_foreign_panels_are_ignored(row):
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    await audit_with(
        activities, db, storage, [approved_run(report(panel(target="example.com")), **row)]
    )
    from tin_lite.organic_audit_panel import founder_panel

    scope = await activities._result(run_id, "scope")
    assert await founder_panel(activities, run_id, scope) is None


@pytest.mark.asyncio
async def test_a_panel_for_another_site_is_ignored_and_refresh_questions_skips_it():
    from tin_lite.organic_audit_panel import founder_panel

    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    await audit_with(activities, db, storage, [approved_run(report(panel()))])
    scope = await activities._result(run_id, "scope")
    assert await founder_panel(activities, run_id, scope) is None  # loopwell.example ≠ example.com

    activities, db, storage, _ = await activities_fixture()
    db.run = replace(db.run, input={**db.run.input, "refresh_questions": True})
    run_id = str(db.run.id)
    await audit_with(activities, db, storage, [approved_run(report(panel(target="example.com")))])
    scope = await activities._result(run_id, "scope")
    assert await founder_panel(activities, run_id, scope) is None
    assert db.list_prerequisite_runs.await_count == 0


def test_qualification_ordinary_case_passes_on_a_fixture_report():
    qualification = Qualification.model_validate_json(CASES.read_text())
    ordinary = next(case for case in qualification.cases if case.id == "ordinary")
    verdict = assess_output(ordinary, status="succeeded", content=report(panel()).encode())
    assert verdict["status"] == "passed", verdict["checks"]
    assert {case.id for case in qualification.cases} == {
        "ordinary",
        "no_positioning",
        "forcing_clause_must_fail",
    }
