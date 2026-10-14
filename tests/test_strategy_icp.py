"""Workflows read the project's ICP and go-to-market strategy at strategy/ICP.md.

Each reader is checked three ways: the file present (read, labelled, never evidence), absent
(the request is exactly what it was before) and over 8 KB (skipped with a note, never cut).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_prompt_panel as panel_tests
import test_social_content_plan as plan_tests
import test_social_post_batch_weekly as batch_tests
import test_social_x_compose as x_tests
from temporalio.exceptions import ApplicationError
from test_content_draft import fixture as draft_fixture
from test_content_draft import start
from test_content_programs import setup
from test_content_refresh_flow import fixture as refresh_fixture
from test_content_refresh_flow import refresh_run
from test_procedure_publication import publication_db as publication_db

from tin_lite import keyword_plan, organic_system
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.content_plan_sources import positioning_files
from tin_lite.keyword_plan_activities import KeywordPlanActivities
from tin_lite.project_strategy import (
    STRATEGY_FILE_BYTES,
    STRATEGY_LABEL,
    STRATEGY_PATH,
    buyer_context_from_strategy,
    derive_buyer_context,
)

ICP = """# ICP and go-to-market strategy

## ICP 1: Seed-stage B2B software founders

**Company:** Seed-stage B2B software company, 2 to 15 people, no marketing hire yet.
**Buyer:** The technical founder who still runs marketing on the side.
**Disqualifiers:** Agencies; consumer apps.
**Problem statement:** Marketing happens in bursts between releases, so signups stall.
**Job to be done:** Keep a steady marketing rhythm without hiring.
**Triggers:** A launch slips; a competitor ships a comparison page.

## ICP 2: Small product teams in regulated fields

- Buyer: Head of product at a 20 to 80 person team.
- Problem statement: Every public claim needs review, so content never ships.
- Company: Healthcare or finance software.

## Messaging themes

- Ship marketing like you ship code.
"""
BIG = ("## ICP\n\n**Buyer:** " + "founders " * 1200).encode()
assert len(BIG) > STRATEGY_FILE_BYTES
BRAND = (
    b"# Brand\n\n## Brand direction\n\nA marketing system for founders who code.\n\n"
    b"## Voice\n\nPlain.\n"
)


# ---------- buyer context from the ICP (organic.keyword_plan) ----------


def test_buyer_context_keeps_icp_names_buyers_and_problems_but_not_messaging():
    text = buyer_context_from_strategy(ICP)
    assert text.startswith("ICP 1: Seed-stage B2B software founders: Buyer: The technical founder")
    assert "Problem: Marketing happens in bursts" in text
    assert "ICP 2: Small product teams in regulated fields: Buyer: Head of product" in text
    assert "Ship marketing like you ship code" not in text
    assert "Agencies" not in text  # disqualifiers are not who buys
    assert len(text) <= 2_000


def test_buyer_context_is_capped_and_every_icp_keeps_its_name():
    many = "\n".join(
        f"## ICP {i}\n\n**Buyer:** {'operations lead ' * 30}\n**Problem statement:** {'x ' * 200}"
        for i in range(1, 7)
    )
    text = buyer_context_from_strategy(many)
    assert len(text) <= 2_000
    assert all(f"ICP {i}:" in text for i in range(1, 7))


def test_derive_prefers_the_icp_then_the_brand_direction_then_names_both_files():
    text, source = derive_buyer_context(ICP.encode(), BRAND)
    assert source == STRATEGY_PATH and "technical founder" in text
    text, source = derive_buyer_context(None, BRAND)
    assert source == "brand/BRAND.md (Brand direction)"
    assert text == "A marketing system for founders who code."
    # An oversized ICP is skipped, never cut; the brand guide then answers.
    assert derive_buyer_context(BIG, BRAND)[1] == "brand/BRAND.md (Brand direction)"
    with pytest.raises(ValueError, match="strategy/ICP.md.*brand/BRAND.md") as error:
        derive_buyer_context(None, b"# Brand\n\n## Voice\n\nPlain.\n")
    assert "over 8 KB" not in str(error.value)
    with pytest.raises(ValueError, match="over 8 KB"):
        derive_buyer_context(BIG, None)


class KeywordStorage:
    def __init__(self, files):
        self.files = files

    async def get_repo(self, repo_id):
        return SimpleNamespace(id=repo_id)

    async def head_sha(self, repo, branch):
        return "a" * 40

    async def read_canonical_artifact_if_exists(self, *, repo_id, commit_sha, path):
        return self.files.get(path)


def keyword_activities(files):
    activities = object.__new__(KeywordPlanActivities)
    project = SimpleNamespace(state_repo_id="repo", canonical_branch="main")
    activities.db = SimpleNamespace(get_project=AsyncMock(return_value=project))
    activities.storage = KeywordStorage(files)
    return activities


def keyword_run(**inputs):
    return SimpleNamespace(project_id="p", input={"site_url": "https://example.com", **inputs})


async def test_keyword_plan_takes_the_buyer_from_the_icp_and_a_supplied_value_wins():
    activities = keyword_activities({STRATEGY_PATH: ICP.encode(), "brand/BRAND.md": BRAND})
    text, source = await activities._buyer_context(keyword_run())
    assert source == STRATEGY_PATH and text.startswith("ICP 1:")
    supplied = "Developer tools for platform teams who own internal deploys."
    assert await activities._buyer_context(keyword_run(buyer_context=supplied)) == (supplied, None)
    activities.db.get_project.assert_awaited_once()  # the override reads no project file


async def test_keyword_plan_without_icp_or_brand_direction_fails_naming_both():
    activities = keyword_activities({})
    with pytest.raises(ApplicationError) as error:
        await activities._buyer_context(keyword_run())
    assert error.value.type == "WorkflowInputError" and error.value.non_retryable
    assert "strategy/ICP.md" in error.value.message and "brand/BRAND.md" in error.value.message


def test_buyer_context_is_optional_with_its_length_rules_kept():
    for key in ("organic.keyword_plan",):
        spec = next(w for w in BUILTIN_WORKFLOWS if w.key == key)
        schema = spec.definition_and_resource_files()[0]["input_schema"]
        assert "buyer_context" not in schema["required"]
        assert schema["properties"]["buyer_context"]["minLength"] == 20
        assert schema["properties"]["buyer_context"]["maxLength"] == 2000
    assert "buyer_context" not in organic_system.INPUT_SCHEMA["required"]
    assert organic_system.INPUT_SCHEMA["properties"]["buyer_context"]["minLength"] == 20
    keyword_plan.check_inputs({"site_url": "https://example.com", "market": "US"})


# ---------- the shared positioning read (content.plan, content.generate) ----------


class PositioningStorage:
    def __init__(self, files):
        self.files = files

    async def read_canonical_artifact_if_exists(self, *, repo_id, commit_sha, path):
        return self.files.get(path)

    async def list_canonical_files_at(self, *, repo_id, revision):
        return sorted(self.files)


async def read_positioning(files, **options):
    return await positioning_files(
        storage=PositioningStorage(files),
        project=SimpleNamespace(state_repo_id="repo"),
        revision="r",
        **options,
    )


async def test_positioning_reads_the_icp_last_only_when_asked():
    files = {"brand/BRAND.md": BRAND, STRATEGY_PATH: ICP.encode()}
    before = await read_positioning(files)
    assert [f["path"] for f in before] == ["brand/BRAND.md"]
    notes = []
    after = await read_positioning(files, include_strategy=True, notes=notes)
    assert after[:-1] == before and notes == []
    assert after[-1]["path"] == STRATEGY_PATH and after[-1]["label"] == STRATEGY_LABEL
    assert after[-1]["content"] == ICP and after[-1]["truncated"] is False


async def test_positioning_without_the_icp_is_unchanged_and_an_oversized_one_is_noted():
    files = {"brand/BRAND.md": BRAND}
    notes = []
    assert await read_positioning(files, include_strategy=True, notes=notes) == (
        await read_positioning(files)
    )
    assert notes == []
    files[STRATEGY_PATH] = BIG
    assert [
        f["path"] for f in await read_positioning(files, include_strategy=True, notes=notes)
    ] == ["brand/BRAND.md"]
    assert notes and "over its 8 KB limit" in notes[0]


async def test_a_plan_pins_the_icp_and_lists_it_for_the_planner(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    storage.repo.edit({"brand/BRAND.md": BRAND, STRATEGY_PATH: ICP.encode()})
    run = await create()
    await activities.execute(str(run.id))
    context = (await db.get_effect(f"content:{run.id}:context")).result
    assert [item["path"] for item in context["positioning"]] == ["brand/BRAND.md", STRATEGY_PATH]
    assert "positioning_notes" not in context
    _, _, files = await activities.research_inputs(project, context)
    assert files[STRATEGY_LABEL] == STRATEGY_PATH


async def test_a_plan_skips_an_oversized_icp_with_a_note(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    storage.repo.edit({"brand/BRAND.md": BRAND, STRATEGY_PATH: BIG})
    run = await create()
    await activities.execute(str(run.id))
    context = (await db.get_effect(f"content:{run.id}:context")).result
    assert [item["path"] for item in context["positioning"]] == ["brand/BRAND.md"]
    assert "over its 8 KB limit" in context["positioning_notes"][0]
    _, _, files = await activities.research_inputs(project, context)
    assert STRATEGY_LABEL not in files


async def drafted_context(publication_db, monkeypatch, files):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await draft_fixture(publication_db, monkeypatch, judgment=True)
    f.storage.repo.edit(files)
    run = await start(f)
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
    )
    await activities.prepare_codex_procedure(str(run.id))
    return await f.service.saved(run.id)


@pytest.mark.parametrize(
    ("icp", "paths", "noted"),
    [
        (ICP.encode(), ["brand/BRAND.md", STRATEGY_PATH], False),
        (None, ["brand/BRAND.md"], False),
        (BIG, ["brand/BRAND.md"], True),
    ],
    ids=["present", "absent", "oversized"],
)
async def test_a_draft_lists_the_icp_for_the_writer(publication_db, monkeypatch, icp, paths, noted):
    files = {"brand/BRAND.md": BRAND, **({STRATEGY_PATH: icp} if icp else {})}
    context = await drafted_context(publication_db, monkeypatch, files)
    assert [item["path"] for item in context["positioning"]] == paths
    assert ("positioning_notes" in context) is noted


def test_writers_and_planners_read_the_icp_as_guidance_never_as_a_source():
    procedures = REPOSITORY_ROOT / "codex_procedures"
    for path in (
        "content.generate/PROMPT.md",
        "content.generate/skills/page-refresh/SKILL.md",
        "content.plan_research/PROMPT.md",
        "content.plan_research/skills/content-portfolio/SKILL.md",
    ):
        text = " ".join((procedures / path).read_text().split())
        assert "strategy/ICP.md" in text, path
        assert "never cite it" in text or "never supports a claim" in text, path


# ---------- content.refresh ----------


@pytest.mark.parametrize(
    ("icp", "expected"),
    [
        (ICP.encode(), {"icp_source": STRATEGY_PATH}),
        (None, {}),
        (BIG, {"icp_note": "over its 8 KB limit"}),
    ],
    ids=["present", "absent", "oversized"],
)
async def test_a_refresh_names_the_icp_apart_from_its_positioning(publication_db, icp, expected):
    f = await refresh_fixture(publication_db)
    if icp:
        f.storage.repo.edit({STRATEGY_PATH: icp})
    context = await f.sources.prepare(await refresh_run(f))
    assert STRATEGY_PATH not in context["positioning_sources"]
    found = {k: context[k] for k in ("icp_source", "icp_note") if k in context}
    assert found.keys() == expected.keys()
    for key, value in expected.items():
        assert value in found[key]


# ---------- workflow packages ----------


def oversized_text():
    return "## ICP\n\n**Buyer:** " + "founders " * 1200


async def test_social_plan_aims_at_the_icp_and_is_unchanged_without_it():
    module, definition = plan_tests.package()
    spec = plan_tests.validate_code_definition(definition)
    base = {"context/product-marketing.md": plan_tests.CONTEXT}
    absent = plan_tests.Context(dict(base), spec)
    plain = await module.run(absent, plan_tests.inputs())
    assert "icp" not in absent.calls[0]["data"]
    assert absent.calls[0]["instructions"] == module.INSTRUCTIONS
    assert "ICP and go-to-market strategy" not in plain["content"]

    present = plan_tests.Context({**base, STRATEGY_PATH: ICP}, spec)
    result = await module.run(present, plan_tests.inputs())
    assert present.calls[0]["data"]["icp"] == ICP.strip()
    assert present.calls[0]["data"]["product_context"] == plan_tests.CONTEXT
    assert "not a source of product facts" in present.calls[0]["instructions"]
    assert "ICP and go-to-market strategy: `strategy/ICP.md`" in result["content"]

    big = plan_tests.Context({**base, STRATEGY_PATH: oversized_text()}, spec)
    result = await module.run(big, plan_tests.inputs())
    assert "icp" not in big.calls[0]["data"]
    assert "over 8 KB, so it was skipped" in result["content"]


async def test_weekly_batch_aims_at_the_icp_and_is_unchanged_without_it():
    module, spec = batch_tests.package()
    base = {"social/PLAN.md": batch_tests.PLAN, "context/social-updates.md": batch_tests.NOTES}
    run_id = "a0000000-0000-0000-0000-000000000091"
    absent = batch_tests.Context(batch_tests.Files(dict(base)), spec, batch_tests.FIRST, run_id)
    plain = await module.run(absent, batch_tests.inputs())
    assert "icp" not in absent.calls[0]["data"]
    assert absent.calls[0]["instructions"] == module.WEEKLY_INSTRUCTIONS
    assert "ICP and go-to-market" not in plain["content"]

    files = batch_tests.Files({**base, STRATEGY_PATH: ICP})
    present = batch_tests.Context(files, spec, batch_tests.FIRST, run_id)
    result = await module.run(present, batch_tests.inputs())
    assert present.calls[0]["data"]["icp"] == ICP.strip()
    assert "never factual evidence" in present.calls[0]["instructions"]
    assert "- ICP and go-to-market strategy: `strategy/ICP.md`" in result["content"]

    files = batch_tests.Files({**base, STRATEGY_PATH: oversized_text()})
    big = batch_tests.Context(files, spec, batch_tests.FIRST, run_id)
    result = await module.run(big, batch_tests.inputs())
    assert "icp" not in big.calls[0]["data"]
    assert "`strategy/ICP.md` over 8 KB, so it was skipped" in result["content"]


async def test_x_posts_aim_at_the_icp_without_citing_it():
    module, definition = x_tests.package()
    spec = x_tests.validate_code_definition(definition)
    base = {"context/product-marketing.md": "Tin is a project workflow service."}
    absent = x_tests.Context(dict(base), spec)
    await module.run(absent, x_tests.inputs())
    assert "icp" not in absent.calls[0]["data"]
    assert absent.calls[0]["instructions"] == module.INSTRUCTIONS

    present = x_tests.Context({**base, STRATEGY_PATH: ICP}, spec)
    result = await module.run(present, x_tests.inputs())
    data = present.calls[0]["data"]
    assert data["icp"] == ICP.strip()
    assert all(unit["source_path"] != STRATEGY_PATH for unit in data["source_units"])
    assert "never cite it" in present.calls[0]["instructions"]
    assert "strategy/ICP.md" not in json.loads(result["content"])["posts"][0]["editor_notes"]

    big = x_tests.Context({**base, STRATEGY_PATH: oversized_text()}, spec)
    result = await module.run(big, x_tests.inputs())
    assert "icp" not in big.calls[0]["data"]
    notes = json.loads(result["content"])["posts"][0]["editor_notes"]
    assert "strategy/ICP.md is over 8 KB, so it was skipped" in notes


async def test_prompt_panel_writes_in_the_icp_buyers_words(monkeypatch):
    content, absent = await panel_tests.run_panel(monkeypatch)
    request = absent.models.calls[0]
    assert all("ICP" not in key for key in request["data"]["positioning"])
    assert request["instructions"] == panel_tests.load(panel_tests.KEY).INSTRUCTIONS

    files = {**panel_tests.FILES, STRATEGY_PATH: ICP}
    content, present = await panel_tests.run_panel(monkeypatch, files=files)
    request = present.models.calls[0]
    key = "strategy/ICP.md (ICP and go-to-market strategy)"
    assert request["data"]["positioning"][key] == ICP.strip()
    assert "never quote it as the core category line" in " ".join(request["instructions"].split())
    assert "strategy/ICP.md" in content.split("Positioning:", 1)[1].split("\n", 1)[0]

    files = {**panel_tests.FILES, STRATEGY_PATH: oversized_text()}
    content, big = await panel_tests.run_panel(monkeypatch, files=files)
    assert all("ICP" not in key for key in big.models.calls[0]["data"]["positioning"])
    assert "strategy/ICP.md is over 8 KB, so it was skipped" in content


@pytest.mark.parametrize("supplied", ["", "   ", None])
async def test_the_traffic_system_leaves_an_empty_buyer_context_to_the_keyword_plan(
    monkeypatch, supplied
):
    from tin_lite import organic_system_activities as module

    async def facts(**_):
        return {"steps": []}

    monkeypatch.setattr(module, "system_facts", facts)
    activities = object.__new__(module.OrganicSystemActivities)
    activities.db = None
    inputs = {"site_url": "https://example.com", "market": "US", "keyword_max_cost_usd": 2}
    if supplied is not None:
        inputs["buyer_context"] = supplied
    run = SimpleNamespace(input=inputs, project_id="p", id="r")
    child, skipped = await activities._child_inputs(run, "keywords")
    assert skipped is None and "buyer_context" not in child
    run.input["buyer_context"] = "Developer tools for platform teams who own deploys."
    child, _ = await activities._child_inputs(run, "keywords")
    assert child["buyer_context"] == run.input["buyer_context"]
