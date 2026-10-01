"""Configured estimates and atomic per-call funding, using disposable Postgres only."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from test_billing import ACTOR, finish, fund
from test_billing import billed as billed
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_service_billing import KEYWORDS, PARENT, SITE, SPECS, admit, install

from tin_lite.billing_contracts import BillingError, ProjectSpendingPolicy
from tin_lite.service_pricing import service_terms
from tin_lite.workflow_costs import _estimate, configured_terms
from tin_lite.workflow_inputs import normalize_workflow_inputs


async def direct(f, key="organic.audit", inputs=None, *, project_id=None, configured=None):
    workflow = await install(f, key)
    project_id = project_id or f.project.id
    normalized = normalize_workflow_inputs(
        schema=workflow.definition["input_schema"], project_id=project_id, inputs=inputs or SITE
    )
    run, _ = await f.db.create_run(
        project_id=project_id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=normalized,
        pinned_definition=workflow.definition,
        definition_commit_sha=workflow.current_commit_sha,
        start_idempotency_key=str(uuid4()),
        project_workflow_id=configured.id if configured else None,
        trigger_source="schedule" if configured else "api",
    )
    return run


async def operation(f, run, name, maximum, *, kind="tool"):
    async with f.db.pool.acquire() as conn:
        return await f.billing.begin_operation(
            conn, run_id=run.id, operation_id=name, kind=kind, maximum=maximum
        )


async def observe(f, name, nanos):
    async with f.db.pool.acquire() as conn:
        await f.billing.observe_operation(conn, operation_id=name, nanos=nanos, observation={})


async def test_audit_runtime_uses_real_exposure_not_released_call_estimates(billed):
    from types import SimpleNamespace

    from tin_lite.organic_audit import AUDIT_POLICY
    from tin_lite.organic_audit_activities import OrganicAuditActivities

    f = billed
    await fund(f)
    run = await direct(f)
    activities = OrganicAuditActivities(
        database=f.db, storage=f.storage, settings=SimpleNamespace()
    )
    await activities._save(
        str(run.id),
        "scope",
        {
            "host": "example.com",
            "url": "https://example.com/",
            "max_cost_usd": "2",
            "policy_version": AUDIT_POLICY["version"],
        },
    )
    for index in range(26):
        assert await activities._reserve(str(run.id), f"answer:{index}", "0.20")
        await operation(f, run, f"sample-{index}", 200_000_000)
        await observe(f, f"sample-{index}", 10_000_000)
    async with f.db.pool.acquire() as conn:
        assert await f.billing.run_operation_exposure(conn, run.id) == 260_000_000
        assert await f.billing.run_operation_exposure(conn, uuid4()) is None
    # $0.26 observed plus $1.60 unconfirmed leaves less than one $0.20 answer under $2.
    await operation(f, run, "unconfirmed", 1_600_000_000)
    assert not await activities._reserve(str(run.id), "another-answer", "0.20")


def test_configured_estimate_reuses_policy_and_invalidates_scope_definition_or_price():
    definition = SPECS["organic.keyword_plan"].definition
    terms = service_terms(definition, inputs={"max_cost_usd": 3})
    _estimate.cache_clear()
    first = configured_terms(terms, definition, {"max_cost_usd": 3})
    assert first == configured_terms(terms, definition, {"max_cost_usd": 3})
    assert _estimate.cache_info().hits == 1
    assert first["estimate"]["amount_nanos"] == 3_000_000_000
    variants = [
        configured_terms(terms, definition, {"max_cost_usd": 4}),
        configured_terms(terms, {**definition, "description": "Changed definition"}, {}),
        configured_terms({**terms, "rate_card": "new-card"}, definition, {}),
    ]
    assert len({first["estimate"]["id"], *(v["estimate"]["id"] for v in variants)}) == 4
    changed = deepcopy(first)
    changed["estimate"]["amount_nanos"] = 0
    assert configured_terms(terms, definition, {"max_cost_usd": 3}) == first


async def test_read_only_http_mcp_preview_and_saved_configuration(billed, monkeypatch):
    f = billed
    workflow = await install(f, "organic.keyword_plan")
    inputs = normalize_workflow_inputs(
        schema=workflow.definition["input_schema"],
        project_id=f.project.id,
        inputs={**KEYWORDS, "max_cost_usd": 5},
    )
    saved = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        name="Keyword research",
        inputs=inputs,
        definition_commit_sha=workflow.current_commit_sha,
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    server = mcp(f, monkeypatch)
    preview = structured(
        await server.call_tool(
            "estimate_workflow_run",
            {
                "project_id": str(f.project.id),
                "project_workflow_id": str(saved.id),
            },
        )
    )
    assert preview["estimated_usd"] == "5.00"
    assert preview["approval_required"] is False and "id" not in preview
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(
            f"/api/projects/{f.project.id}/billing/estimate",
            json={
                "project_workflow_id": str(saved.id),
            },
        )
        assert response.status_code == 200 and response.json() == preview
        assert response.headers["Cache-Control"] == "no-store"
        denied = await client.post(
            f"/api/projects/{uuid4()}/billing/estimate",
            json={
                "project_workflow_id": str(saved.id),
            },
        )
        assert denied.status_code == 404
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0


async def test_zero_upfront_liability_release_and_single_charge(billed):
    f = billed
    await fund(f, 1000)
    run = await direct(f)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "10.00"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0
    await operation(f, run, "one", 500_000_000)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "9.50"
    await asyncio.gather(*(observe(f, "one", 125_000_000) for _ in range(3)))
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "9.87"
    await operation(f, run, "two", 500_000_000)
    await observe(f, "two", 125_000_000)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "9.75"
    await finish(f, run)
    assert await asyncio.gather(*(f.billing.settle(run.id) for _ in range(3))) == [250_000_000] * 3
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["available_usd"] == "9.75" and view["reserved_usd"] == "0.00"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
    charge = await f.billing.run_charge(run.id, ACTOR)
    assert charge["estimated_usd"] == "2.00" and charge["charged_usd"] == "0.25"
    assert charge["released_usd"] is None  # Never imply a $2 upfront hold existed.


async def test_estimate_rejects_unfunded_start_without_creating_run(billed):
    f = billed
    with pytest.raises(BillingError, match=r"estimated at up to \$2.00") as error:
        await direct(f)
    assert error.value.code == "insufficient_funds"
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0


def test_project_limit_message_names_the_limit_that_blocks_admission():
    from tin_lite.billing import project_limit_message

    policy = {"per_run_nanos": 10_000_000_000, "monthly_nanos": 10_000_000_000}
    idle = {"exposure": 0}
    assert project_limit_message(policy, 5_000_000_000, idle) is None
    no_policy = project_limit_message(None, 5_000_000_000, idle)
    assert no_policy.startswith("This project has no spending policy yet.")
    assert "set_project_spending_limits" in no_policy
    per_run = project_limit_message(policy, 12_500_000_000, idle)
    assert per_run.startswith(
        "This workflow is estimated at up to $12.50; the project's per-run limit is $10.00."
    )
    assert "set_project_spending_limits" in per_run
    monthly = project_limit_message(policy, 5_000_000_000, {"exposure": 7_250_000_000})
    assert monthly.startswith(
        "This workflow is estimated at up to $5.00, which would exceed this month's "
        "$10.00 project limit ($7.25 already committed)."
    )


async def test_project_limit_admission_says_which_limit_and_keeps_its_code(billed):
    f = billed
    await fund(f, 1000)
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=1_500_000_000,
            monthly_nanos=100_000_000_000,
            expected_revision=1,
        ),
    )
    with pytest.raises(BillingError, match=r"per-run limit is \$1\.50") as error:
        await direct(f)
    assert error.value.code == "project_limit" and error.value.status == 402
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0


async def test_cost_preview_accepts_the_same_project_id_start_workflow_strips(billed, monkeypatch):
    f = billed
    await install(f, "organic.audit")
    server = mcp(f, monkeypatch)
    arguments = {"project_id": str(f.project.id), "workflow_id": "organic.audit"}
    expected = structured(
        await server.call_tool("estimate_workflow_run", {**arguments, "inputs": SITE})
    )
    for tool in ("estimate_workflow_run", "quote_workflow_run"):
        bound = {**SITE, "project_id": str(f.project.id).upper()}
        preview = structured(await server.call_tool(tool, {**arguments, "inputs": bound}))
        assert preview["estimated_usd"] == expected["estimated_usd"]
        with pytest.raises(Exception, match="inputs.project_id conflicts"):
            await server.call_tool(
                tool, {**arguments, "inputs": {**SITE, "project_id": str(uuid4())}}
            )


async def test_parallel_projects_cannot_spend_same_wallet(billed, monkeypatch):
    f = billed
    await fund(f, 1000)
    sibling = await f.db.create_workspace_project(
        workspace_id=f.project.workspace_id,
        project_id=uuid4(),
        name="Sibling",
        state_repo_id="projects/sibling",
        clerk_user_id=ACTOR,
        request_id=uuid4(),
    )
    await f.billing.update_policy(
        sibling.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=10_000_000_000,
            monthly_nanos=100_000_000_000,
            expected_revision=0,
        ),
    )
    ten = {**KEYWORDS, "max_cost_usd": 10}
    unstarted = await direct(f, "organic.keyword_plan", ten)
    # Admission holds the first run's $10 estimate against the sibling's start.
    with pytest.raises(BillingError, match="set aside for runs in progress") as error:
        await direct(f, "organic.keyword_plan", ten, project_id=sibling.id)
    assert error.value.code == "insufficient_funds"
    await finish(f, unstarted)
    assert await f.billing.settle(unstarted.id) == 0
    # With calibrated estimates below the maximum, both start and every paid call
    # still contends for the same credits under the wallet lock.
    import tin_lite.workflow_costs as workflow_costs

    real = workflow_costs._estimate
    monkeypatch.setattr(workflow_costs, "_estimate", lambda *key: (real(*key)[0], key[-1] // 2))
    runs = [
        await direct(f, "organic.keyword_plan", ten, project_id=p)
        for p in (f.project.id, sibling.id)
    ]
    results = await asyncio.gather(
        *(operation(f, run, str(run.id), 6_000_000_000) for run in runs), return_exceptions=True
    )
    assert sum(isinstance(r, BillingError) for r in results) == 1
    assert next(r for r in results if isinstance(r, BillingError)).code == "insufficient_funds"
    winner = next(
        run
        for run, result in zip(runs, results, strict=True)
        if not isinstance(result, BillingError)
    )
    loser = next(run for run in runs if run.id != winner.id)
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 1
    await observe(f, str(winner.id), 200_000_000)
    await operation(f, loser, str(loser.id), 6_000_000_000)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "3.80"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 2


async def test_monthly_limit_checks_actual_plus_pending_calls(billed):
    f = billed
    await fund(f)
    runs = [await direct(f), await direct(f)]
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=5_000_000_000,
            monthly_nanos=1_000_000_000,
            expected_revision=1,
        ),
    )
    await operation(f, runs[0], "first", 600_000_000)
    with pytest.raises(BillingError, match="Current project limits"):
        await operation(f, runs[1], "second", 600_000_000)
    await observe(f, "first", 200_000_000)
    await operation(f, runs[1], "second", 600_000_000)
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 2


async def test_scheduled_parent_rechecks_standing_authority_for_children(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f)
    workflow = await install(f, "organic.traffic_system")
    inputs = normalize_workflow_inputs(
        schema=workflow.definition["input_schema"], project_id=f.project.id, inputs=PARENT
    )
    saved = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        name="Organic traffic",
        inputs=inputs,
        definition_commit_sha=workflow.current_commit_sha,
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    await f.db.project_workflow_synced(
        project_workflow_id=saved.id,
        temporal_schedule_id=None,
        next_run_at=None,
    )
    with pytest.raises(BillingError, match="standing spending"):
        await direct(f, "organic.traffic_system", PARENT, configured=saved)
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=30_000_000_000,
            monthly_nanos=100_000_000_000,
            expected_revision=1,
            schedule_max_nanos=30_000_000_000,
        ),
    )
    parent = await direct(f, "organic.traffic_system", PARENT, configured=saved)
    child = await admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:audit")
    assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=20_000_000_000,
            monthly_nanos=100_000_000_000,
            expected_revision=2,
            schedule_max_nanos=None,
        ),
    )
    with pytest.raises(BillingError, match="Current project limits"):
        await operation(f, child, "child-call", 100_000_000)
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0


async def test_zero_work_or_zero_cost_releases_everything(billed):
    f = billed
    await fund(f)
    for with_operation in (False, True):
        run = await direct(f)
        if with_operation:
            await operation(f, run, str(run.id), 100_000_000)
            await observe(f, str(run.id), 0)
        await finish(f, run)
        assert await f.billing.settle(run.id) == 0
        assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "100.00"


async def test_runs_start_while_another_is_active_and_an_unknown_bill_keeps_its_liability(billed):
    f = billed
    await fund(f, 1000)
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=10_000_000_000,
            monthly_nanos=100_000_000_000,
            expected_revision=1,
        ),
    )
    first = await direct(f)
    await operation(f, first, "uncertain-call", 100_000_000)
    # No run limit: the money limits bound spending, not how many runs are active.
    concurrent = await direct(f)
    await finish(f, concurrent)
    assert await f.billing.settle(concurrent.id) == 0
    await finish(f, first)
    assert await f.billing.settle(first.id) is None
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "9.90"
    second = await direct(f)
    assert second.id != first.id
    assert (
        await f.db.pool.fetchval("SELECT status FROM billing_operations WHERE id='uncertain-call'")
        == "pending"
    )
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM billing_ledger WHERE kind='charge' AND run_id=$1", first.id
    )


async def limits(f, *, monthly_usd, revision, per_run_usd=10):
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=per_run_usd * 1_000_000_000,
            monthly_nanos=monthly_usd * 1_000_000_000,
            expected_revision=revision,
        ),
    )


async def test_admission_counts_unstarted_per_call_runs_at_their_estimates(billed):
    """A run admitted before its first paid call still occupies the month and the wallet.

    Per-call funding reserves nothing at admission. Counting such runs at their committed
    amount (zero) admitted parallel starts past the monthly limit and the balance; each
    then failed at a paid call in the middle of its work.
    """
    f = billed
    await fund(f, 1000)
    await limits(f, monthly_usd=10, revision=1)
    six = {**KEYWORDS, "max_cost_usd": 6}
    first = await direct(f, "organic.keyword_plan", six)
    assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
    with pytest.raises(BillingError, match=r"\(\$6\.00 already committed\)") as error:
        await direct(f, "organic.keyword_plan", six)
    assert error.value.code == "project_limit"
    # With paid work committed, the larger of its estimate and its commitment counts.
    await operation(f, first, "first-call", 5_000_000_000)
    await observe(f, "first-call", 5_000_000_000)
    with pytest.raises(BillingError, match=r"\(\$6\.00 already committed\)"):
        await direct(f, "organic.keyword_plan", six)
    # A settled run counts at its charge, as before.
    await finish(f, first)
    assert await f.billing.settle(first.id) == 5_000_000_000
    with pytest.raises(BillingError, match=r"\(\$5\.00 already committed\)"):
        await direct(f, "organic.keyword_plan", six)
    await direct(f, "organic.keyword_plan", {**KEYWORDS, "max_cost_usd": 5})

    # The shared wallet: the unstarted $5 run holds its estimate against new starts,
    # though nothing is reserved or shown as held.
    await limits(f, monthly_usd=100, revision=2)
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["available_usd"] == "5.00" and view["reserved_usd"] == "0.00"
    with pytest.raises(BillingError, match=r"Available credits: \$0\.00") as error:
        await direct(f, "organic.keyword_plan", {**KEYWORDS, "max_cost_usd": 3})
    assert error.value.code == "insufficient_funds"
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 2
