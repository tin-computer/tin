"""Calibrated estimates: admission checks the usual cost, the ceiling still stops spend."""

from decimal import Decimal

import pytest
from test_billing import ACTOR, fund
from test_billing import billed as billed
from test_incremental_billing import direct, operation
from test_procedure_publication import publication_db as publication_db
from test_service_billing import SPECS

from tin_lite import codex_api_pricing, workflow_estimates
from tin_lite.billing_contracts import NANOS_PER_CENT, NANOS_PER_DOLLAR, BillingError, digest
from tin_lite.service_pricing import service_terms
from tin_lite.workflow_costs import BOUND_POLICY, admitted_amount, configured_terms
from tin_lite.workflow_estimates import estimate_nanos

DOLLAR = NANOS_PER_DOLLAR


def usd(value):
    return int(Decimal(value) * DOLLAR)


def test_a_listed_workflow_uses_its_entry_clamped_to_its_ceiling(monkeypatch):
    monkeypatch.setitem(workflow_estimates.ESTIMATES_USD, "organic.keyword_plan", "2.00")
    definition = SPECS["organic.keyword_plan"].definition
    assert estimate_nanos(definition, {}, 9 * DOLLAR) == 2 * DOLLAR
    # A founder's $1 research ceiling caps the estimate too.
    assert estimate_nanos(definition, {"max_cost_usd": 1}, DOLLAR) == DOLLAR


def test_unlisted_workflows_use_their_family_default():
    procedure = {"key": "custom.launch_post", "executor": "codex.procedure"}
    assert estimate_nanos(procedure, {}, 10 * DOLLAR) == DOLLAR
    native = {"key": "custom.native", "executor": "scan.report"}
    assert estimate_nanos(native, {}, 2 * DOLLAR) == 25 * NANOS_PER_CENT
    # A code package without a measured p90: 30% of its derived bound, up to the cent.
    code = {"key": "custom.digest", "executor": "workflow.code"}
    assert estimate_nanos(code, {}, 4 * DOLLAR) == 120 * NANOS_PER_CENT
    assert estimate_nanos(code, {}, 11 * NANOS_PER_CENT) == 4 * NANOS_PER_CENT
    measured = {"key": "social.x_compose", "executor": "workflow.code"}
    assert estimate_nanos(measured, {}, DOLLAR) == 3 * NANOS_PER_CENT


def test_versions_with_their_own_estimate():
    audit = SPECS["organic.audit"].definition
    assert audit["audit_policy"]["version"] == "organic-audit-v15"
    assert estimate_nanos(audit, {}, 6 * DOLLAR) == 75 * NANOS_PER_CENT
    earlier = {**audit, "audit_policy": {**audit["audit_policy"], "version": "organic-audit-v14"}}
    assert estimate_nanos(earlier, {}, 6 * DOLLAR) == 55 * NANOS_PER_CENT
    website = SPECS["website.change"].definition
    assert estimate_nanos(website, {"source": "audit"}, 4 * DOLLAR) == 80 * NANOS_PER_CENT
    assert estimate_nanos(website, {"source": "content_draft"}, 4 * DOLLAR) == 60 * NANOS_PER_CENT
    assert estimate_nanos(website, {"source": "planned"}, 4 * DOLLAR) == 60 * NANOS_PER_CENT


def test_an_estimate_is_never_above_the_ceiling_or_free():
    procedure = {"key": "custom.launch_post", "executor": "codex.procedure"}
    assert estimate_nanos(procedure, {}, 30 * NANOS_PER_CENT) == 30 * NANOS_PER_CENT
    assert estimate_nanos(procedure, {}, 1) == 1
    code = {"key": "custom.digest", "executor": "workflow.code"}
    # Any paid route is at least a cent; a share rounds up to whole cents.
    assert estimate_nanos(code, {}, 2 * NANOS_PER_CENT) == NANOS_PER_CENT
    assert estimate_nanos(code, {}, DOLLAR + 1) % NANOS_PER_CENT == 0


@pytest.mark.parametrize(
    ("inputs", "dollars"),
    [
        # min(keyword limit, $0.50) + $3.00 + $0.80 for fixes + $0.60 for delivery.
        ({"keyword_max_cost_usd": 2, "technical_fix": True}, "4.90"),
        (
            {"keyword_max_cost_usd": 2, "technical_fix": False, "content_delivery": "draft_only"},
            "3.50",
        ),
        ({"keyword_max_cost_usd": 2, "technical_fix": False}, "4.10"),
        ({"keyword_max_cost_usd": "0.20", "technical_fix": True}, "4.60"),
    ],
)
def test_the_v8_traffic_system_estimate(inputs, dollars):
    definition = SPECS["organic.traffic_system"].definition
    assert definition["organic_system_policy"]["version"] == "organic-traffic-v8"
    terms = service_terms(definition, inputs=inputs)
    priced = configured_terms(terms, definition, inputs)
    assert priced["estimate"]["amount_nanos"] == usd(dollars)
    assert priced["estimate"]["basis"] == "calibrated_p90"
    # The ceiling is the one service_pricing composes, unchanged by the estimate.
    assert priced["maximum_nanos"] == terms["maximum_nanos"]


def test_earlier_traffic_systems_sum_the_children_their_run_starts():
    from tin_lite import organic_system

    definition = {
        **SPECS["organic.traffic_system"].definition,
        "organic_system_policy": organic_system.MEASUREMENT_POLICY,
    }
    inputs = {"keyword_max_cost_usd": 2, "technical_fix": True}
    # Audit $0.55, keywords $0.50, one-call plan $0.02, Page decisions $0.01, draft $1.25,
    # refresh $0.20, then website.change fixes $0.80 and delivery $0.60.
    assert estimate_nanos(definition, inputs, 100 * DOLLAR) == usd("3.93")


def test_spending_parents():
    plan = SPECS["content.plan"].definition
    assert (plan.get("content_policy") or {}).get("planner")
    assert estimate_nanos(plan, {}, 6 * DOLLAR) == usd("1.50")
    one_call = {**plan, "content_policy": {}}
    assert estimate_nanos(one_call, {}, DOLLAR // 4) == 2 * NANOS_PER_CENT
    # Measured as a whole: a captured voice skips its style step.
    x_draft = {"key": "social.x_draft", "executor": "social.x_draft"}
    assert estimate_nanos(x_draft, {}, DOLLAR) == 3 * NANOS_PER_CENT


def test_included_terms_and_issued_bound_quotes_keep_their_meaning():
    included = {"kind": "included", "maximum_nanos": 0}
    assert configured_terms(included, {}, {}) is included
    definition = SPECS["organic.keyword_plan"].definition
    inputs = {"max_cost_usd": 3}
    terms = service_terms(definition, inputs=inputs)
    bound = configured_terms(terms, definition, inputs, policy=BOUND_POLICY)
    # Exactly what configured-cost-bound-v1 issued: the ceiling, under its original id.
    assert bound["estimate"] == {
        "id": digest([BOUND_POLICY, digest(definition), digest(inputs), digest(terms), 3 * DOLLAR]),
        "policy": BOUND_POLICY,
        "basis": "conservative_configured_bound",
        "amount_nanos": 3 * DOLLAR,
    }
    calibrated = configured_terms(bound, definition, inputs)
    assert calibrated["estimate"]["policy"] == "calibrated-p90-v1"
    assert calibrated["estimate"]["id"] != bound["estimate"]["id"]
    assert {k: v for k, v in calibrated.items() if k != "estimate"} == {
        k: v for k, v in bound.items() if k != "estimate"
    }


def test_a_session_budget_is_admitted_at_the_ceiling_it_holds():
    per_call = {"maximum_nanos": 10 * DOLLAR, "funding": "per_operation_v1"}
    per_call["estimate"] = {"amount_nanos": 2 * DOLLAR}
    assert admitted_amount(per_call) == 2 * DOLLAR
    session = {**per_call, "funding": "procedure_session_v1"}
    assert admitted_amount(session) == 10 * DOLLAR


def test_a_procedure_ceiling_comes_from_its_validator_then_its_key(monkeypatch):
    def ceiling(key, **options):
        return codex_api_pricing.api_terms(SPECS[key].definition, **options)["maximum_nanos"]

    # website.change has no validator entry; its key entry sets $4, root and child alike.
    assert ceiling("website.change") == ceiling("website.change", child=True) == 4 * DOLLAR
    assert ceiling("content.generate", child=True) == 6 * DOLLAR
    # The validator's entry wins over a key entry.
    monkeypatch.setitem(codex_api_pricing.PROCEDURE_KEY_MAXIMUMS, "content.refresh", 9 * DOLLAR)
    assert ceiling("content.refresh") == DOLLAR
    # Neither: the defaults.
    assert ceiling("product.code_map") == 10 * DOLLAR
    assert ceiling("product.code_map", child=True) == 5 * DOLLAR
    # Quotes issued before v5 predate key entries.
    assert ceiling("website.change", before_v5=True) == 5 * DOLLAR
    # An entry above $10 applies, like the $10 default, only where the room covers it.
    assert ceiling("product.deep_dive") == 12 * DOLLAR
    assert ceiling("product.deep_dive", room=12 * DOLLAR) == 12 * DOLLAR
    assert ceiling("product.deep_dive", room=11 * DOLLAR) == 10 * DOLLAR
    assert ceiling("product.deep_dive", room=9 * DOLLAR) == 5 * DOLLAR
    # Packages are found by the key in their definition, as built-ins are.
    package = {**SPECS["product.code_map"].definition, "key": "competitor.watch"}
    assert codex_api_pricing.api_terms(package, room=DOLLAR)["maximum_nanos"] == 3 * DOLLAR


async def spend_down(f, cents):
    """Leave a small wallet: the smallest top-up is $10."""
    await f.db.pool.execute(
        "UPDATE billing_accounts SET balance_nanos=balance_nanos-$2 WHERE workspace_id=$1",
        f.project.workspace_id,
        cents * NANOS_PER_CENT,
    )


@pytest.fixture
def audit_estimate(monkeypatch):
    monkeypatch.setattr(workflow_estimates, "AUDIT_V15_USD", "1.00")


async def test_a_run_whose_estimate_fits_the_wallet_starts_below_its_ceiling(
    billed, audit_estimate
):
    f = billed
    await fund(f, 1000)
    await spend_down(f, 700)
    # The audit's ceiling is $6; $3 of credit used to refuse it. Its $1 estimate fits.
    run = await direct(f)
    terms = await f.db.pool.fetchval(
        "SELECT terms->'estimate'->>'amount_nanos' FROM billing_run_budgets WHERE run_id=$1",
        run.id,
    )
    assert int(terms) == DOLLAR
    view = await f.billing.overview(f.project.id, ACTOR)
    assert (view["set_aside_usd"], view["available_usd"]) == ("1.00", "2.00")
    # Each paid call is still checked against the wallet it would overdraw.
    await operation(f, run, "one", 250 * NANOS_PER_CENT)
    with pytest.raises(BillingError) as error:
        await operation(f, run, "two", DOLLAR)
    assert error.value.code == "insufficient_funds"


async def test_the_ceiling_still_stops_a_run_that_spends_past_its_estimate(billed, audit_estimate):
    f = billed
    await fund(f, 10000)
    run = await direct(f)
    await operation(f, run, "one", 5 * DOLLAR)
    with pytest.raises(BillingError) as error:
        await operation(f, run, "two", 150 * NANOS_PER_CENT)
    assert error.value.code == "run_limit"


async def test_a_refusal_names_the_usual_cost_and_the_ceiling(billed, audit_estimate):
    f = billed
    await fund(f, 1000)
    await spend_down(f, 950)
    with pytest.raises(BillingError) as error:
        await direct(f)
    assert error.value.code == "insufficient_funds"
    assert str(error.value).startswith(
        "This workflow usually costs about $1.00, never more than $6.00. Available credits: $0.50"
    )
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
