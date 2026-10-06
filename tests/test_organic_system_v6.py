"""organic-traffic-v6: the traffic system's two writer steps go through website.change.

v6 starts website.change for the approved draft (its page source) instead of content.deliver,
and with the latest audit's fixes (`source: audit`) instead of organic.technical_fix. Runs
pinned to v5 and earlier keep their children, receipts and report word for word.
"""

import json
from unittest.mock import AsyncMock
from uuid import UUID

from test_billing import billed as billed
from test_organic_content import approve, draft, system_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_repository_delivery as delivery
from tin_lite import organic_system, website_change_audit
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.codex_api_pricing import api_terms
from tin_lite.organic_audit import digest
from tin_lite.organic_system_activities import boundaries
from tin_lite.service_pricing import service_terms

# organic-traffic-v5 as main shipped it (d69d337), the policy pinned runs carry.
V5_DIGEST = "8bc98c935e546045a9ac6a9eb7f01259712a429b19b69158d6be4700a1e296a4"
# v6 as main shipped it; runs pinned to it keep its steps when v7 is the default.
V6_DIGEST = "0d91792e3fce3969699985a349b08a649ca855aaebcd97659ea969e3ceeb2a27"
V5_BOUNDARIES = (
    "One technical finding at most; others remain for review. No eligible finding means no "
    "technical-fix compute or PR. The next planned article is drafted when this recipe "
    "includes generation. Its final approved copy can become an unmerged PR; without GitHub, "
    "the Markdown stays readable in Tin for manual export or a later delivery. The existing "
    "writing guide is used when available; no style samples are invented. Backlinks and "
    "outreach are not part of this version. Plan dates do not automatically generate the rest "
    "of the roadmap. An open PR is not a published website."
)
TECHNICAL = {
    "technical_fix": True,
    "expected_repository": "owner/site",
    "repository_serves_site": True,
}


def test_v6_steps_and_v5_pins_are_unchanged():
    recipe = next(w for w in BUILTIN_WORKFLOWS if w.key == organic_system.KEY)
    # New runs pin v7, which keeps v6's steps and adds the weekly measurement.
    assert recipe.version_label == "0.7.0"
    assert recipe.definition["organic_system_policy"] == organic_system.POLICY
    assert organic_system.POLICY["version"] == "organic-traffic-v7"
    assert digest(organic_system.WEBSITE_POLICY) == V6_DIGEST
    for policy in (organic_system.WEBSITE_POLICY, organic_system.POLICY):
        v6 = organic_system.policy_steps(policy)
        assert (v6["technical"], v6["delivery"]) == ("website.change", "website.change")
        assert organic_system.writes_with_website_change(policy)
    assert not organic_system.measures_pages(organic_system.WEBSITE_POLICY)
    assert organic_system.measures_pages(organic_system.POLICY)
    # v5 is byte for byte what main shipped: its steps, its children and its report text.
    assert digest(organic_system.REFRESH_POLICY) == V5_DIGEST
    v5 = organic_system.policy_steps(organic_system.REFRESH_POLICY)
    assert (v5["technical"], v5["delivery"]) == ("organic.technical_fix", "content.deliver")
    assert not organic_system.writes_with_website_change(organic_system.REFRESH_POLICY)
    assert boundaries(False)[2] == V5_BOUNDARIES
    for policy in (
        organic_system.REFRESH_POLICY,
        organic_system.WEBSITE_POLICY,
        organic_system.POLICY,
    ):
        assert organic_system.drafts_articles(policy) and organic_system.refreshes_pages(policy)
    assert organic_system.child_executor("website.change") == "codex.procedure"


async def test_v6_puts_the_approved_draft_on_the_site_with_website_change(
    publication_db, monkeypatch
):
    f = await system_fixture(publication_db, monkeypatch, policy=organic_system.POLICY)
    run = await approve(f, await draft(f))
    inputs, reason = await f.system._child_inputs(f.parent, "delivery")
    assert reason is None and inputs == {
        "source": "content_draft",
        "source_run_id": str(run.id),
        "expected_repository": "owner/site",
    }
    payload = {"run_id": str(f.parent.id), "step": "delivery"}
    result = await f.system.organic_system_step(payload)
    assert await f.system.organic_system_step(payload) == result
    child = await f.db.get_run(UUID(result["run_id"]))
    assert (
        child.workflow_id == delivery.WEBSITE_CHANGE_ID and child.input["source"] == "content_draft"
    )
    source = (await f.db.get_effect(delivery.source_key(child.id))).result
    assert source["source_run_id"] == str(run.id) and source["change"]["kind"] == "page"
    # The founder's pick at approval decides: this approval chose a pull request.
    assert source["publish"]["mode"] == "pull_request"
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()


async def test_v6_draft_only_keeps_the_markdown_in_tin(publication_db, monkeypatch):
    f = await system_fixture(
        publication_db,
        monkeypatch,
        policy=organic_system.POLICY,
        inputs={"content_delivery": "draft_only"},
    )
    await approve(f, await draft(f))
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "delivery"})
    assert result["status"] == "skipped" and "run_id" not in result
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE workflow_id=$1", delivery.WEBSITE_CHANGE_ID
    )


async def test_v6_technical_step_starts_website_change_with_the_audit(publication_db, monkeypatch):
    f = await system_fixture(
        publication_db, monkeypatch, policy=organic_system.POLICY, inputs=TECHNICAL
    )
    monkeypatch.setattr(
        "tin_lite.organic_system_activities.system_facts",
        AsyncMock(
            return_value={"steps": [{"step": "audit", "status": "succeeded", "run_id": None}]}
        ),
    )
    previews = []

    async def plan_changes(**kwargs):
        previews.append(kwargs)
        return {"next_run": {"change_ids": ["oa_" + "1" * 20] if len(previews) == 1 else []}}

    monkeypatch.setattr(website_change_audit, "plan_changes", plan_changes)
    inputs, reason = await f.system._child_inputs(f.parent, "technical")
    assert reason is None and inputs == {
        "source": "audit",
        "expected_repository": "owner/site",
        "repository_serves_site": True,
    }
    # The preview records the rows; the system passes no judgment-call answers.
    assert previews[0]["inputs"] == inputs and previews[0]["bind"] is False
    assert "decisions" not in inputs
    # Nothing left to fix is a skipped step, not a failed one.
    assert await f.system._child_inputs(f.parent, "technical") == (None, "no_eligible_findings")


async def test_v6_pins_website_change_for_both_writer_steps(publication_db, monkeypatch):
    f = await system_fixture(
        publication_db, monkeypatch, policy=organic_system.POLICY, inputs=TECHNICAL
    )
    # Prepare again from the registry, as a new v6 run does.
    await f.db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"traffic:{f.parent.id}:prepare"
    )
    await f.system.organic_system_prepare(str(f.parent.id))
    prepared = (await f.db.get_effect(f"traffic:{f.parent.id}:prepare")).result
    # New runs pin v7, which keeps v6's writer steps and adds the measurement packages.
    assert prepared["policy"]["version"] == "organic-traffic-v7"
    assert prepared["definitions"]["snapshot"]["key"] == "organic.traffic_snapshot"
    assert prepared["definitions"]["decisions"]["key"] == "organic.content_efficacy"
    website = next(w for w in BUILTIN_WORKFLOWS if w.key == "website.change").definition
    assert prepared["definitions"]["technical"]["key"] == "website.change"
    assert prepared["definitions"]["delivery"] == prepared["definitions"]["technical"]
    assert prepared["definitions"]["delivery"]["version"] == website["version"]


def test_the_v6_ceiling_still_covers_its_children():
    recipe = next(w for w in BUILTIN_WORKFLOWS if w.key == organic_system.KEY).definition
    website = next(w for w in BUILTIN_WORKFLOWS if w.key == "website.change").definition
    inputs = {"keyword_max_cost_usd": 2, "technical_fix": True, "content_delivery": "auto"}
    v7 = service_terms(recipe, inputs=inputs)["maximum_nanos"]
    v6, v5 = (
        service_terms({**recipe, "organic_system_policy": policy}, inputs=inputs)["maximum_nanos"]
        for policy in (organic_system.WEBSITE_POLICY, organic_system.REFRESH_POLICY)
    )
    assert v6 == v5
    # v7 adds Page decisions' $1 share; the snapshot makes no model call.
    assert v7 == v6 + 1_000_000_000
    # Two website.change children (the technical step and the delivery) fit inside it.
    assert v6 >= 2 * api_terms(website, child=True)["maximum_nanos"]


async def test_v7_budget_admits_its_measurement_children_only_under_their_keys(billed):
    from test_billing import fund
    from test_service_billing import PARENT, admit

    from tin_lite.public_workflows import load_public_workflows

    f = billed
    await fund(f)
    parent = await admit(f, "organic.traffic_system", PARENT)
    packages = {p.key: p.definition for p in await load_public_workflows()}
    definitions = {step: packages[key] for step, key in organic_system.MEASURE_STEPS.items()}
    system = {"run_id": parent.id, "executor": "organic.traffic_system"}

    def child(step):
        return {"start_idempotency_key": f"system:{parent.id}:{step}"}

    async with f.db.pool.acquire() as conn:
        key = f"traffic:{parent.id}:prepare"
        await f.db.start_effect(conn, execution_key=key, operation="organic.traffic_system")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={"policy": organic_system.POLICY, "definitions": definitions},
        )
        for step, definition in definitions.items():
            assert await f.billing.valid_child(conn, system, child(step), definition)
            assert not await f.billing.valid_child(conn, system, child("refresh"), definition)
            assert not await f.billing.valid_child(
                conn, system, child(step), {**definition, "title": "Changed"}
            )
        assert not await f.billing.valid_child(
            conn, system, child("snapshot"), definitions["decisions"]
        )
        # A run pinned to v6 never funds them.
        await f.db.pool.execute(
            "UPDATE effect_receipts SET result=jsonb_set(result, '{policy}', $2::jsonb) "
            "WHERE execution_key=$1",
            key,
            json.dumps(organic_system.WEBSITE_POLICY),
        )
        assert not await f.billing.valid_child(
            conn, system, child("snapshot"), definitions["snapshot"]
        )


async def test_v6_budget_admits_website_change_for_its_writer_steps_only(billed):
    from test_billing import fund
    from test_service_billing import PARENT, SPECS, admit

    f = billed
    await fund(f)
    parent = await admit(f, "organic.traffic_system", PARENT)
    website = SPECS["website.change"].definition
    definitions = {
        "draft": SPECS["content.generate"].definition,
        "technical": website,
        "delivery": website,
    }
    async with f.db.pool.acquire() as conn:
        key = f"traffic:{parent.id}:prepare"
        await f.db.start_effect(conn, execution_key=key, operation="organic.traffic_system")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={"policy": organic_system.POLICY, "definitions": definitions},
        )
        system = {"run_id": parent.id, "executor": "organic.traffic_system"}

        def child(step):
            return {"start_idempotency_key": f"system:{parent.id}:{step}"}

        for step in ("technical", "delivery"):
            assert await f.billing.valid_child(conn, system, child(step), website)
        # Not under another step's key, not a definition the recipe did not pin, and not the
        # workflows v6 replaced.
        assert not await f.billing.valid_child(conn, system, child("draft"), website)
        assert not await f.billing.valid_child(
            conn, system, child("delivery"), {**website, "title": "Changed"}
        )
        for replaced in ("content.deliver", "organic.technical_fix"):
            for step in ("technical", "delivery"):
                assert not await f.billing.valid_child(
                    conn, system, child(step), SPECS[replaced].definition
                )
        assert await f.billing.valid_child(
            conn, system, child("draft"), SPECS["content.generate"].definition
        )
