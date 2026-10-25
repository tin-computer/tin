"""Report evidence corrections are new-policy behavior; old contracts stay immutable."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_organic_audit import activities_fixture
from test_organic_audit_angles import BASE, CONTENT, HOST, facts, html_page

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V16_AUDIT_POLICY,
    audit_policy,
    canonical_json,
    digest,
)
from tin_lite.organic_audit_ai import ai_contract, ai_schemas
from tin_lite.organic_audit_completion import KIND, NEUTRAL_KEYS
from tin_lite.organic_audit_report import analyze
from tin_lite.organic_audit_site import parse_robots


def test_v16_keeps_the_shipped_policy_instructions_and_schemas():
    assert audit_policy("organic-audit-v16") is V16_AUDIT_POLICY
    assert (
        digest(V16_AUDIT_POLICY)
        == "4ea692b9b5cf10dcaa2c06b37b4471869c8adfb9cd350e5333b5ae4cb5d2b43d"
    )
    assert (
        digest(ai_contract("organic-audit-v16"))
        == "5cb54dc5c922fc65c396978744a3ddf0242828381e19957240a56b6d349e6148"
    )
    assert (
        digest(ai_schemas("organic-audit-v16"))
        == "9697781502162fb2f38dfa57224b8dd1b521a1a7b93633c68aa5b68fdaa5a878"
    )


def test_v17_changes_report_interpretation_without_more_calls_or_answer_changes():
    assert audit_policy() is AUDIT_POLICY and AUDIT_POLICY["version"] == "organic-audit-v17"
    assert AUDIT_POLICY == {
        **V16_AUDIT_POLICY,
        "version": "organic-audit-v17",
        "crawler_access_policy": True,
        "report_engine_coverage": True,
    }
    assert {k: v for k, v in AUDIT_POLICY.items() if k not in NEUTRAL_KEYS} == {
        k: v for k, v in V16_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS
    }
    assert ai_contract(AUDIT_POLICY["version"]) == ai_contract(V16_AUDIT_POLICY["version"])
    assert ai_schemas(AUDIT_POLICY["version"]) == ai_schemas(V16_AUDIT_POLICY["version"])
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    assert workflow.version_label == "0.12.1"
    assert workflow.definition["audit_policy"] == AUDIT_POLICY


def test_real_analysis_selects_new_crawler_policy_only_for_new_pins():
    site = {
        "files": {
            "status": "observed",
            "robots": {
                "status": "observed",
                **parse_robots("User-agent: *\nAllow: /\n\nUser-agent: GPTBot\nDisallow: /\n"),
            },
            "sitemaps": {"files": [], "urls": []},
        },
        "pages": [facts("/", html_page(title="Product information", body=CONTENT))],
        "access": {
            "status": "observed",
            "rows": [
                {"url": BASE + "/", "agent": "browser", "status": "observed", "status_code": 200},
                {"url": BASE + "/", "agent": "GPTBot", "status": "refused", "status_code": 403},
            ],
        },
    }
    args = dict(
        scope={"host": HOST, "url": BASE + "/"},
        hosts=(HOST,),
        crawl={},
        ai={},
        search_console=None,
        search_queries=None,
        site=site,
    )
    old = analyze(**args, policy=V16_AUDIT_POLICY)
    new = analyze(**args, policy=AUDIT_POLICY)
    assert "access.ai_crawlers_refused" in {f["check_id"] for f in old["findings"]}
    assert "access.ai_crawlers_refused" not in {f["check_id"] for f in new["findings"]}


@pytest.mark.parametrize("completion", [False, True])
async def test_v16_pinned_activity_still_accepts_normal_and_completion_runs(
    monkeypatch, completion
):
    activities, db, storage, provider = await activities_fixture()
    db.effects.clear()
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    definition.update(
        audit_policy=V16_AUDIT_POLICY,
        audit_instructions=ai_contract(V16_AUDIT_POLICY["version"]),
        audit_schemas=ai_schemas(V16_AUDIT_POLICY["version"]),
    )
    storage.read_canonical_artifact.return_value = canonical_json(definition)
    provider.validate_target.reset_mock()
    seed = AsyncMock()
    monkeypatch.setattr("tin_lite.organic_audit_completion.prepare_completion", seed)
    if completion:
        db.run = replace(db.run, prerequisite_evidence={"kind": KIND})

    await activities.organic_prepare(str(db.run.id))

    if completion:
        seed.assert_awaited_once_with(activities, db.run, target_policy=V16_AUDIT_POLICY)
        provider.validate_target.assert_not_awaited()
    else:
        scope = await activities._result(str(db.run.id), "scope")
        assert scope["policy_version"] == "organic-audit-v16"
        seed.assert_not_awaited()
