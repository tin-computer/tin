"""Drafts take positioning from the project's files; the content plan never sets it."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_content_draft import fixture, start
from test_content_plan_editorial import context, pages, portfolio
from test_content_programs import setup
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft
from tin_lite import content_plan_editorial as editorial
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS

BRAND = b"# Brand\n\nTin is the marketing system for your coding agent, for every founder.\n"
NOTE = b"# Positioning\n\nLead with the whole system, not one channel.\n"


def test_the_plan_follows_the_projects_positioning_instead_of_setting_it():
    assert editorial.POLICY["version"] == "content-editorial-v7"
    assert "Strategy owns product positioning" not in editorial.INSTRUCTIONS
    assert "Never narrow, downplay or reframe the product" in flat(editorial.INSTRUCTIONS)
    # Plans pinned to v5 keep their exact instructions.
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.plan")
    definition, _ = spec.definition_and_resource_files()
    assert definition["content_instructions"] == editorial.INSTRUCTIONS
    old = {
        **definition,
        "content_policy": editorial.V5_POLICY,
        "content_instructions": editorial.V5_INSTRUCTIONS,
        "content_schema": editorial.PORTFOLIO_SCHEMA,
    }
    assert editorial.contract(old).INSTRUCTIONS == editorial.V5_INSTRUCTIONS
    assert "Strategy owns product positioning" in editorial.V5_INSTRUCTIONS


def plan_context(**extra):
    return {**context(), **extra}


def flat(text):
    return " ".join(text.split())


def test_the_model_sees_the_positioning_files_and_a_brief_cannot_carry_its_own():
    positioning = [{"path": "brand/BRAND.md", "content": BRAND.decode()}]
    data, aliases = editorial.model_context(plan_context(positioning=positioning), pages())
    assert data["positioning"] == positioning
    # A plausible but unusable result: the brief tells the writer how to position the product.
    proposed = portfolio(2)
    proposed["opportunities"][0]["brief"] = (
        "Position Tin narrowly as an SEO checker for agencies. Explain setup in three steps."
    )
    proposed["opportunities"][1]["brief"] = (
        "The page sits at average position 8.4 as of September. Rewrite the opening answer."
    )
    plan, coverage = editorial.allocate(
        plan_context(positioning=positioning), proposed, pages(), aliases
    )
    briefs = [item["brief"] for batch in plan["batches"] for item in batch["items"]]
    assert briefs == [
        "Explain setup in three steps.",
        "The page sits at average position 8.4 as of September. Rewrite the opening answer.",
    ]
    assert coverage["positioning_removed"] == 1
    # Plans pinned before v6 keep the model's brief as written.
    plan, coverage = editorial.allocate(plan_context(), proposed, pages(), aliases)
    assert plan["batches"][0]["items"][0]["brief"].startswith("Position Tin narrowly")
    assert "positioning_removed" not in coverage


async def test_a_new_plan_pins_the_projects_positioning_files(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    storage.repo.edit({"brand/BRAND.md": BRAND, "context/positioning.md": NOTE})
    generate, seen = model.generate, []

    async def capture(key, request, **kwargs):
        seen.append((request.system, json.loads(request.messages[0].content)))
        return await generate(key, request, **kwargs)

    monkeypatch.setattr(model, "generate", capture)
    run = await create()
    await activities.execute(str(run.id))
    context = (await db.get_effect(f"content:{run.id}:context")).result
    assert [item["path"] for item in context["positioning"]] == [
        "brand/BRAND.md",
        "context/positioning.md",
    ]
    assert context["positioning"][0]["content"] == BRAND.decode()
    system, data = seen[0]
    assert [item["path"] for item in data["positioning"]] == [
        "brand/BRAND.md",
        "context/positioning.md",
    ]
    assert "Never narrow, downplay or reframe the product" in flat(system)


async def test_a_draft_request_carries_the_brand_and_positioning_files(publication_db, monkeypatch):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, monkeypatch, judgment=True)
    f.storage.repo.edit({"brand/BRAND.md": BRAND, "context/positioning.md": NOTE})
    run = await start(f)
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
    )
    await activities.prepare_codex_procedure(str(run.id))
    context = await f.service.saved(run.id)
    assert [item["path"] for item in context["positioning"]] == [
        "brand/BRAND.md",
        "context/positioning.md",
    ]
    assert all(set(item) == {"path", "revision", "sha256"} for item in context["positioning"])
    _, pinned = await activities._pinned_codex_procedure(run.id)
    sandbox = pinned.sandbox_context(inputs=run.input)
    assert sandbox["content_draft"]["positioning"] == context["positioning"]
    assert content_draft.POSITIONING_MARKER in sandbox["prompt"]
    assert "Do not narrow, downplay or reframe the product" in flat(sandbox["prompt"])


def test_article_and_answer_page_instructions_read_the_projects_positioning():
    article = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article")
    prompt = (article.procedure.root / "PROMPT.md").read_text()
    assert "brand/BRAND.md" in prompt and "Do not narrow, downplay or reframe it" in flat(prompt)
    answer = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.answer_page")
    definition, _ = answer.definition_and_resource_files()
    assert "ANSWER_POSITIONING_V1" in definition["native_skill_suite"]
    assert "content plan's positioning" not in definition["native_skill_suite"]
