"""Offline fixture tests for the launch-kit package.

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

KEY = "growth.launch_kit"

GOOD_PRIMARY = {
    "hn_title": "Show HN: Loopwire - rate limiting that explains itself",
    "hn_body": (
        "We built Loopwire because we kept getting paged for rate-limit false positives. "
        "It logs the exact token-bucket state behind every 429 so you can see why a "
        "request was rejected, not just that it was."
    ),
    "ph_tagline": "Rate limiting you can actually debug",
    "ph_description": (
        "Loopwire shows the token-bucket state behind every 429 response, so you can "
        "see exactly why a request was rejected instead of guessing."
    ),
}

GOOD_AMPLIFY = {
    "tweet_thread": [
        "We kept getting paged for rate-limit false positives, so we built a fix.",
        "Loopwire logs the exact token-bucket state behind every 429 you get.",
        "No more guessing why a request was rejected. Live today.",
    ],
    "email_subject": "Loopwire is live: rate limiting you can debug",
    "email_body": (
        "Hi -- Loopwire is live today. It logs the token-bucket state behind every 429 "
        "so you can see exactly why a request was rejected."
    ),
}

GOOD_INPUTS = {
    "product_name": "Loopwire",
    "launch_subject": "Loopwire 1.0",
    "value_prop": "Rate limiting that shows you why a request was rejected, not just that it was.",
    "signup_url": "https://loopwire.example/launch",
}


def _load():
    root = REPOSITORY_ROOT / "workflow_packages" / KEY
    definition = json.loads((root / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(root / "main.py")))
    return module, definition


def _context(primary=None, amplify=None):
    calls = []

    async def generate(**payload):
        calls.append(payload)
        if payload["step"] == "draft_primary":
            return {"parsed": primary or GOOD_PRIMARY, "text": "..."}
        return {"parsed": amplify or GOOD_AMPLIFY, "text": "..."}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


def test_manifest_matches_the_shared_code_workflow_contract():
    _, definition = _load()
    spec = validate_code_definition(definition)
    assert spec.entrypoint == "main.py"
    assert {route.name for route in spec.model_routes} == {"draft_primary", "draft_amplify"}
    assert sum(route.max_calls for route in spec.model_routes) == 2


async def test_calls_both_steps_and_renders_a_bounded_markdown_artifact():
    module, definition = _load()
    spec = validate_code_definition(definition)
    context, calls = _context()
    result = await module.run(context, GOOD_INPUTS)
    validate_code_result(json.dumps(result).encode(), spec)
    assert [c["step"] for c in calls] == ["draft_primary", "draft_amplify"]
    assert result["path"] == "reports/LAUNCH_KIT.md"
    content = result["content"]
    assert "Show HN" in content
    assert "Product Hunt" in content
    assert GOOD_INPUTS["signup_url"] in content
    assert GOOD_PRIMARY["hn_title"] in content
    assert GOOD_AMPLIFY["email_subject"] in content


async def test_rejects_an_oversized_show_hn_title():
    module, _ = _load()
    context, _ = _context(primary={**GOOD_PRIMARY, "hn_title": "x" * 81})
    with pytest.raises(ValueError, match="80 characters"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_hype_language_in_show_hn_copy():
    module, _ = _load()
    context, _ = _context(
        primary={**GOOD_PRIMARY, "hn_body": "Our revolutionary, game-changing platform."}
    )
    with pytest.raises(ValueError, match="hype"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_an_oversized_product_hunt_tagline():
    module, _ = _load()
    context, _ = _context(primary={**GOOD_PRIMARY, "ph_tagline": "x" * 61})
    with pytest.raises(ValueError, match="60 characters"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_primary_copy_that_invents_its_own_link():
    module, _ = _load()
    context, _ = _context(
        primary={**GOOD_PRIMARY, "hn_body": GOOD_PRIMARY["hn_body"] + " https://evil.example"}
    )
    with pytest.raises(ValueError, match="invent its own link"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_duplicate_tweets():
    module, _ = _load()
    context, _ = _context(
        amplify={**GOOD_AMPLIFY, "tweet_thread": [GOOD_AMPLIFY["tweet_thread"][0]] * 3}
    )
    with pytest.raises(ValueError, match="distinct"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_an_oversized_tweet():
    module, _ = _load()
    context, _ = _context(
        amplify={**GOOD_AMPLIFY, "tweet_thread": ["x" * 281, *GOOD_AMPLIFY["tweet_thread"][1:]]}
    )
    with pytest.raises(ValueError, match="280 characters"):
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
    assert spec.output_path == "reports/LAUNCH_KIT.md"
    assert (
        files["main.py"] == (REPOSITORY_ROOT / "workflow_packages" / KEY / "main.py").read_bytes()
    )
