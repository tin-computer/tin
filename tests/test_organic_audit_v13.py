"""Audit policy v13 asks the buyer prompt panel and six AI engines; v11 and v12 are unchanged.

v11 and v12 are on main and may deploy at any time, so everything v13 adds is read from
v13-only policy keys. Offline only.
"""

from __future__ import annotations

import hashlib

import pytest
from test_organic_audit import activities_fixture
from test_organic_audit_v12 import RUN_ID, documents, site_evidence
from test_prompt_panel import audit_with, panel_row, published

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import (
    AI_ENGINE_POLICY_KEYS,
    PANEL_PREPARATION_POLICY_KEYS,
    V11_AUDIT_POLICY,
    V12_AUDIT_POLICY,
    V13_AUDIT_POLICY,
    audit_paths,
    audit_policy,
    digest,
    summary_paths,
)
from tin_lite.organic_audit_ai import ai_contract, ai_schemas
from tin_lite.organic_audit_completion import NEUTRAL_KEYS

V12 = "organic-audit-v12"
V13 = "organic-audit-v13"
# What main shipped for v12 at d69d337, before v13 existed: the policy, the AI instructions
# and schemas, and the page facts and files the synthetic v12 run produces.
V12_POLICY_DIGEST = "50d6673a39ae301721b32d7432de9e5b00b1b80607f90a0af2cdaa345d8778e3"
V12_CONTRACT_DIGEST = "8e4247192536b81a59d29fd43a50678ed50a809d53633b4699cc41f6ee4a7a3f"
V12_SCHEMAS_DIGEST = "7c0ac931e8e612cd09b2a9a55b1425f145fb71c5af613eb1b15ac02c70a1ada8"
V12_PAGE_FACTS_DIGEST = "5338ba3c3c18abf7b7f8cf9055ead3af61e5d73ef170d1c3274d0c914ceb25dc"
V12_FILES = {
    "AUDIT.md": "8de7baa6bc764bd88914b0526829528a333a64ea1d8b5ed117afa3cba8619c4f",
    "findings.json": "9f4207855092f971f59a19d4792d01a8bf543904fe0177495ddf37c8a3d63e39",
    "evidence.json": "15d9bc7ee982e7605e93ae458f10e1d39691851b18d1892991d55c84b49d0f58",
    "SUMMARY.json": "80654e6f966429be6ff91a060172974c693cf9d291ab84cb950c4a13e6cac6ed",
    "LATEST.json": "80654e6f966429be6ff91a060172974c693cf9d291ab84cb950c4a13e6cac6ed",
}


def test_v12_is_exactly_what_main_shipped():
    v12 = audit_policy(V12)
    assert v12 is V12_AUDIT_POLICY
    assert digest(v12) == V12_POLICY_DIGEST
    assert digest(ai_contract(V12)) == V12_CONTRACT_DIGEST
    assert digest(ai_schemas(V12)) == V12_SCHEMAS_DIGEST


@pytest.mark.asyncio
async def test_a_v12_run_reads_and_writes_exactly_what_it_did_before_v13():
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    assert digest(pages) == V12_PAGE_FACTS_DIGEST
    docs = documents(V12_AUDIT_POLICY, files, pages)
    paths = {**audit_paths(RUN_ID), **summary_paths(RUN_ID)}
    assert set(docs) == set(paths.values())
    assert {name: hashlib.sha256(docs[path]).hexdigest() for name, path in paths.items()} == (
        V12_FILES
    )


def test_v13_adds_the_panel_and_the_engines_to_v12():
    assert V13_AUDIT_POLICY["version"] == V13 and audit_policy(V13) is V13_AUDIT_POLICY
    assert {k: v for k, v in V13_AUDIT_POLICY.items() if k != "version"} == {
        **{k: v for k, v in V12_AUDIT_POLICY.items() if k != "version"},
        "prompt_panel": True,
        "ai_engines": [
            "chatgpt",
            "gemini",
            "google_ai_mode",
            "google_ai_overview",
            "claude",
            "perplexity",
        ],
        "ai_engines_priority": "standard",
        "ai_engines_deadline_seconds": 2700,
        "ai_engines_max_cost_usd": "1.00",
    }
    assert "prompt_panel" in PANEL_PREPARATION_POLICY_KEYS
    assert set(V13_AUDIT_POLICY) - set(V12_AUDIT_POLICY) - {"prompt_panel"} == AI_ENGINE_POLICY_KEYS
    # The same answers and grading as v12, so an answer completion may cross them.
    assert ai_contract(V13) == ai_contract(V12) and ai_schemas(V13) == ai_schemas(V12)
    assert {k: v for k, v in V13_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS} == {
        k: v for k, v in V12_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS
    }
    # The catalog now pins v14 (catalog 0.10.0); tests/test_organic_audit_v14.py checks it.
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    assert {k: v for k, v in workflow.definition["audit_policy"].items() if k != "version"} == {
        **{k: v for k, v in V13_AUDIT_POLICY.items() if k != "version"},
        "follow_links_without_sitemap": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [V11_AUDIT_POLICY, V12_AUDIT_POLICY])
async def test_runs_pinned_before_v13_never_read_the_panel(monkeypatch, policy):
    content = await published(monkeypatch)
    activities, db, storage, _ = await activities_fixture(policy=policy)
    run_id = str(db.run.id)
    assert (await activities._result(run_id, "scope"))["policy_version"] == policy["version"]
    await audit_with(db, storage, [panel_row(content)])
    # No model is configured, so a run that drafts its own questions measures nothing.
    assert await activities.organic_prepare_panel(run_id) == 0
    # The question reuse still looks for earlier audits; nothing asks for a panel run.
    asked = [call.kwargs["workflow_keys"] for call in db.list_prerequisite_runs.await_args_list]
    assert asked and all("organic.prompt_panel" not in keys for keys in asked)
    assert await activities._result(run_id, "prompt_panel") is None


@pytest.mark.asyncio
async def test_a_v13_run_asks_the_panel(monkeypatch):
    content = await published(monkeypatch)
    activities, db, storage, _ = await activities_fixture(policy=V13_AUDIT_POLICY)
    run_id = str(db.run.id)
    await audit_with(db, storage, [panel_row(content)])
    assert await activities.organic_prepare_panel(run_id) == 32
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["method"] == "buyer_prompt_panel"
