"""Offline fixture tests for the developer challenge package.

No network, no model provider and no execution runtime are used: the manifest is checked
against the shared workflow.code contract, and main.py's run() is exercised directly against
a synthetic ctx.models.generate, exactly like the two shipped code examples.
"""

import json
import runpy
from types import SimpleNamespace

import pytest
from test_public_workflows import PublishedSnapshots, select
from test_registry_recipe_publication import WIKI, catalog_database

from tin_lite import catalog
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import load_code_package, validate_code_definition, validate_code_result

KEY = "growth.dev_challenge"

GOOD_CHALLENGE = {
    "title": "The Rate Limiter That Lies",
    "hook": "Our API answers 429 sometimes even under the documented limit. Why?",
    "statement": (
        "Given a token bucket with capacity 5 and a refill rate of 1 token per whole "
        "second, and five requests that all arrive at t=0, compute the earliest second "
        "at which a sixth request would be allowed. Submit that second as your flag."
    ),
    "example_input": "capacity=5 refill=1/s requests=[t=0]*5",
    "example_output": "the bucket is empty immediately after the five requests",
    "flag": "t=1",
    "hints": [
        "The docs describe a token bucket, but read the refill units carefully.",
        "Refill happens once per whole second, not continuously.",
    ],
    "estimated_minutes": 30,
}

GOOD_PROMO = {
    "teaser": (
        "We turned a real production bug into a puzzle. Our rate limiter used to reject "
        "requests early under specific timing -- solve it and tell us why."
    ),
    "social_post": "Our rate limiter lies sometimes. We turned the real bug into a puzzle.",
}

GOOD_INPUTS = {
    "product_name": "Loopwire",
    "topic": "why our API rate limiter sometimes rejects requests under the documented limit",
    "difficulty": "intermediate",
    "signup_url": "https://loopwire.example/careers/challenge",
}


def _load():
    root = REPOSITORY_ROOT / "workflow_packages" / KEY
    definition = json.loads((root / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(root / "main.py")))
    return module, definition


def _context(challenge=None, promo=None):
    calls = []

    async def generate(**payload):
        calls.append(payload)
        if payload["step"] == "design_challenge":
            return {"parsed": challenge or GOOD_CHALLENGE, "text": "..."}
        return {"parsed": promo or GOOD_PROMO, "text": "..."}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


def test_manifest_matches_the_shared_code_workflow_contract():
    _, definition = _load()
    spec = validate_code_definition(definition)
    assert spec.entrypoint == "main.py"
    assert {route.name for route in spec.model_routes} == {"design_challenge", "write_promo"}
    assert sum(route.max_calls for route in spec.model_routes) == 2


async def test_calls_both_steps_and_renders_a_bounded_markdown_artifact():
    module, definition = _load()
    spec = validate_code_definition(definition)
    context, calls = _context()
    result = await module.run(context, GOOD_INPUTS)
    validate_code_result(json.dumps(result).encode(), spec)
    assert [c["step"] for c in calls] == ["design_challenge", "write_promo"]
    assert result["path"] == "reports/DEV_CHALLENGE.md"
    content = result["content"]
    assert GOOD_CHALLENGE["title"] in content
    assert GOOD_INPUTS["signup_url"] in content
    assert "## Submit your flag" in content
    # The flag only appears after the private answer-key marker, never earlier on the page.
    marker = "Answer key (private"
    assert marker in content
    before, after = content.split(marker, 1)
    assert GOOD_CHALLENGE["flag"] not in before
    assert GOOD_CHALLENGE["flag"] in after


async def test_rejects_a_schema_valid_but_placeholder_flag():
    module, _ = _load()
    context, _ = _context(challenge={**GOOD_CHALLENGE, "flag": "FLAG"})
    with pytest.raises(ValueError, match="placeholder"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_a_trivial_example_with_no_signal():
    module, _ = _load()
    context, _ = _context(
        challenge={**GOOD_CHALLENGE, "example_input": "same", "example_output": "Same"}
    )
    with pytest.raises(ValueError, match="distinct"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_a_hint_that_repeats_the_flag():
    module, _ = _load()
    context, _ = _context(
        challenge={**GOOD_CHALLENGE, "hints": [GOOD_CHALLENGE["flag"], "a second hint"]}
    )
    with pytest.raises(ValueError, match="give away the flag"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_promo_copy_that_invents_its_own_link():
    module, _ = _load()
    context, _ = _context(promo={**GOOD_PROMO, "social_post": "Solve it at https://evil.example"})
    with pytest.raises(ValueError, match="invent its own link"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_an_oversized_social_post():
    module, _ = _load()
    context, _ = _context(promo={**GOOD_PROMO, "social_post": "x" * 281})
    with pytest.raises(ValueError):
        await module.run(context, GOOD_INPUTS)


async def test_publishes_through_the_real_catalog_sync_like_the_shipped_examples(monkeypatch):
    (entry,) = select(monkeypatch, KEY)
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    assert row.project_id is None
    assert row.executor == "workflow.code"
    assert row.definition_repo_id == "registry/workflows"
    assert row.definition_path == f"workflow_packages/{KEY}/workflow.json"
    definition, spec, files = await load_code_package(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=row.current_commit_sha,
        definition_path=row.definition_path,
    )
    assert definition["key"] == KEY
    assert spec.output_path == "reports/DEV_CHALLENGE.md"
    assert (
        files["main.py"] == (REPOSITORY_ROOT / "workflow_packages" / KEY / "main.py").read_bytes()
    )
