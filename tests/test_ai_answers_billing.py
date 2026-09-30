"""AI answer requests settle through the pinned native supplier card, once each."""

from __future__ import annotations

from test_ai_answers import FakeDataForSEO, client_for, inputs
from test_billing import billed as billed
from test_billing import finish, fund
from test_procedure_publication import publication_db as publication_db
from test_service_billing import SITE, admit

from tin_lite.ai_answers import AIAnswersRequest, measure
from tin_lite.ai_answers_activities import ReceiptLedger


async def test_reported_dataforseo_costs_settle_once_through_service_pricing(billed):
    f = billed
    await fund(f)
    run = await admit(f, "organic.audit", SITE)
    fake = FakeDataForSEO(pending_rounds=0)
    request = AIAnswersRequest.from_inputs(
        inputs(engines=["chatgpt", "claude"], prompts=["best form builder"])
    )
    ledger = ReceiptLedger(f.db, run_id=run.id, stage="ai_answers")
    first = await measure(client_for(fake), request, ledger=ledger, tag="billing-test")
    again = await measure(client_for(fake), request, ledger=ledger, tag="billing-test")
    assert again["rows"] == first["rows"]
    assert first["summary"]["cost_usd"] == "0.0218"  # One scraper task and one live answer.
    assert len(fake.posts()) == 2
    rows = await f.db.pool.fetch(
        "SELECT status, observed_nanos FROM billing_operations WHERE run_id=$1", run.id
    )
    assert sorted(r["observed_nanos"] for r in rows) == [1_200_000, 20_600_000]
    assert {r["status"] for r in rows} == {"observed"}
    await finish(f, run)
    await f.billing.reconcile()
    assert await f.billing.settle(run.id) == 20_000_000  # $0.0218 rounds to $0.02 at the root.


async def test_a_spending_limit_fails_the_rows_without_a_request(billed, monkeypatch):
    f = billed
    await fund(f)
    run = await admit(f, "organic.audit", SITE)
    fake = FakeDataForSEO(pending_rounds=0)
    from tin_lite.billing_contracts import BillingError

    async def refuse(*args, **kwargs):
        raise BillingError("budget_exhausted", "The run budget is spent.")

    monkeypatch.setattr(f.billing, "begin_operation", refuse)
    request = AIAnswersRequest.from_inputs(inputs(engines=["chatgpt"], prompts=["best form"]))
    result = await measure(
        client_for(fake),
        request,
        ledger=ReceiptLedger(f.db, run_id=run.id, stage="ai_answers"),
        tag="billing-test",
    )
    assert {(r["status"], r["reason"]) for r in result["rows"]} == {("failed", "spending_limit")}
    assert fake.requests == []
