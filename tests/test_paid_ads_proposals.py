"""Approving a monitor proposal applies exactly one Google Ads change, once; discarding and
refusals touch nothing in the account."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_organic_audit import MemoryDB

from tin_lite.integrations import (
    GoogleAdsCallError,
    IntegrationDeliveryUnknownError,
    IntegrationUpstreamError,
)
from tin_lite.paid_ads_proposals import (
    approve_paid_ads_proposal,
    discard_paid_ads_proposal,
    list_paid_ads_proposals,
)

CID = "7235744335"
USER = "user_Alpha123"


class ProposalDB(MemoryDB):
    def __init__(self):
        super().__init__()
        self.proposals: dict[UUID, dict] = {}
        self.campaigns: dict[UUID, dict] = {}
        self.members = {USER}

    async def has_project_access(self, *, project_id, clerk_user_id):
        return project_id == self.project.id and clerk_user_id in self.members

    async def get_paid_ads_proposal(self, proposal_id):
        return dict(self.proposals[proposal_id]) if proposal_id in self.proposals else None

    async def list_paid_ads_proposals(self, *, project_id, status=None):
        return [
            dict(row)
            for row in self.proposals.values()
            if row["project_id"] == project_id and (status is None or row["status"] == status)
        ]

    async def get_paid_ads_campaign(self, run_id):
        return dict(self.campaigns[run_id]) if run_id in self.campaigns else None

    async def review_paid_ads_proposal(self, *, proposal_id, decision, clerk_user_id):
        row = self.proposals[proposal_id]
        if row["status"] == decision or (
            decision == "approved" and row["status"] in {"applied", "unknown", "failed"}
        ):
            return dict(row)
        if row["status"] != "pending":
            raise RuntimeError("this proposal has already been decided")
        row.update(status=decision, reviewed_by_clerk_user_id=clerk_user_id)
        return dict(row)

    async def settle_paid_ads_proposal(self, *, proposal_id, status, error_code=None):
        row = self.proposals[proposal_id]
        if row["status"] in {"approved", "unknown"}:
            row.update(status=status, error_code=error_code)
        return dict(row)


def fixture(kind="budget_change", *, campaign_status="live", call=None):
    db = ProposalDB()
    campaign_run = uuid4()
    db.campaigns[campaign_run] = {
        "run_id": campaign_run,
        "project_id": db.project.id,
        "customer_id": CID,
        "status": campaign_status,
        "external_campaign_id": "22",
        "external_budget_id": "11",
    }
    proposal_id = uuid4()
    db.proposals[proposal_id] = {
        "id": proposal_id,
        "project_id": db.project.id,
        "campaign_run_id": campaign_run,
        "monitor_run_id": db.run.id,
        "proposal_number": 1,
        "kind": kind,
        "status": "pending",
        "previous": {"daily_budget_usd": 20.0}
        if kind == "budget_change"
        else {"strategy": "maximize_clicks"},
        "proposed": (
            {"daily_budget_usd": 24.0}
            if kind == "budget_change"
            else {"strategy": "maximize_conversions"}
        ),
        "rationale": "because",
        "error_code": None,
    }
    calls: list[dict] = []

    async def google_ads_call(**kwargs):
        calls.append(kwargs)
        if call is not None:
            return await call(**kwargs)
        return {"results": [{"resourceName": "x"}], "provider_request_id": "req"}

    runtime = SimpleNamespace(
        database=db, integrations=SimpleNamespace(google_ads_call=google_ads_call)
    )
    return runtime, db, proposal_id, calls


@pytest.mark.asyncio
async def test_approving_a_budget_change_applies_one_budget_update_once():
    runtime, db, proposal_id, calls = fixture("budget_change")
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "applied" and row["error_code"] is None
    assert len(calls) == 1
    request = calls[0]
    assert request["kind"] == "mutate_resource"
    assert request["project_id"] == db.project.id
    assert request["run_id"] == db.run.id
    assert request["expected_customer_id"] == CID
    assert request["execution_key"] == f"paid_ads_proposal:{proposal_id}:apply:call"
    assert request["request"]["segment"] == "campaignBudgets"
    operation = request["request"]["body"]["operations"][0]
    assert operation == {
        "update": {
            "resourceName": f"customers/{CID}/campaignBudgets/11",
            "amountMicros": "24000000",
        },
        "updateMask": "amount_micros",
    }
    replay = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert replay["status"] == "applied"
    assert len(calls) == 1
    receipt = db.effects[f"paid_ads_proposal:{proposal_id}:apply"]
    assert receipt.status == "completed" and receipt.result == {"status": "applied"}


@pytest.mark.asyncio
async def test_approving_a_bid_change_switches_to_maximize_conversions():
    runtime, _, proposal_id, calls = fixture("bid_strategy_change")
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "applied"
    assert calls[0]["request"]["segment"] == "campaigns"
    assert calls[0]["request"]["body"]["operations"][0] == {
        "update": {"resourceName": f"customers/{CID}/campaigns/22", "maximizeConversions": {}},
        "updateMask": "maximize_conversions.target_cpa_micros",
    }


@pytest.mark.asyncio
async def test_a_provider_refusal_settles_failed_with_its_code_and_is_not_retried():
    async def refuse(**kwargs):
        raise GoogleAdsCallError("CampaignError.CANNOT_MODIFY_START_DATE")

    runtime, db, proposal_id, calls = fixture("bid_strategy_change", call=refuse)
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "failed"
    assert row["error_code"] == "CampaignError.CANNOT_MODIFY_START_DATE"
    again = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert again["status"] == "failed" and len(calls) == 1


@pytest.mark.asyncio
async def test_an_integration_error_settles_failed_by_type():
    async def down(**kwargs):
        raise IntegrationUpstreamError("Google Ads did not answer")

    runtime, _, proposal_id, calls = fixture(call=down)
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "failed" and row["error_code"] == "IntegrationUpstreamError"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_attempt_settles_unknown_and_a_retry_does_not_resend():
    async def lost(**kwargs):
        raise TimeoutError

    runtime, db, proposal_id, calls = fixture(call=lost)
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "unknown" and row["error_code"] is None
    receipt = db.effects[f"paid_ads_proposal:{proposal_id}:apply"]
    assert receipt.status == "completed" and receipt.result == {"status": "unknown"}
    again = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert again["status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_lost_google_ads_answer_settles_unknown_not_failed():
    async def lost(**kwargs):
        raise IntegrationDeliveryUnknownError("Google Ads did not confirm the change")

    runtime, db, proposal_id, calls = fixture(call=lost)
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "unknown" and row["error_code"] is None
    receipt = db.effects[f"paid_ads_proposal:{proposal_id}:apply"]
    assert receipt.result == {"status": "unknown"}
    again = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert again["status"] == "unknown" and len(calls) == 1


@pytest.mark.asyncio
async def test_a_started_but_unfinished_attempt_is_never_resent():
    runtime, db, proposal_id, calls = fixture()
    key = f"paid_ads_proposal:{proposal_id}:apply"
    await db.start_effect(None, execution_key=key, operation="paid_ads_proposal_apply")
    await db.save_effect_progress(None, execution_key=key, result={"attempted_at": "earlier"})
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "unknown"
    assert calls == []
    assert db.effects[key].status == "completed"


@pytest.mark.asyncio
async def test_discarding_never_calls_the_provider():
    runtime, db, proposal_id, calls = fixture()
    row = await discard_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "discarded"
    assert calls == []
    assert f"paid_ads_proposal:{proposal_id}:apply" not in db.effects
    with pytest.raises(RuntimeError, match="already been decided"):
        await approve_paid_ads_proposal(
            runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
        )
    assert calls == []


@pytest.mark.asyncio
async def test_a_campaign_that_is_not_live_settles_failed_without_a_call():
    runtime, _, proposal_id, calls = fixture(campaign_status="stopped")
    row = await approve_paid_ads_proposal(
        runtime=runtime, proposal_id=proposal_id, clerk_user_id=USER
    )
    assert row["status"] == "failed" and row["error_code"] == "campaign_not_live"
    assert calls == []


@pytest.mark.asyncio
async def test_membership_is_the_boundary_for_every_verb():
    runtime, db, proposal_id, calls = fixture()
    stranger = "user_Stranger1"
    for verb in (approve_paid_ads_proposal, discard_paid_ads_proposal):
        with pytest.raises(LookupError, match="proposal not found"):
            await verb(runtime=runtime, proposal_id=proposal_id, clerk_user_id=stranger)
    with pytest.raises(LookupError, match="proposal not found"):
        await approve_paid_ads_proposal(runtime=runtime, proposal_id=uuid4(), clerk_user_id=USER)
    with pytest.raises(LookupError, match="project not found"):
        await list_paid_ads_proposals(
            runtime=runtime, project_id=db.project.id, clerk_user_id=stranger
        )
    listed = await list_paid_ads_proposals(
        runtime=runtime, project_id=db.project.id, clerk_user_id=USER
    )
    assert [row["id"] for row in listed] == [proposal_id]
    assert calls == []
    assert db.proposals[proposal_id]["status"] == "pending"
