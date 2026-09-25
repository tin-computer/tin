"""Approval-gated Google Ads changes proposed by the monitor: budget and bidding strategy.

Approving applies exactly the proposed change through the manager account under one effect
lock; an unconfirmed attempt is never sent again and the next monitor run reconciles it from
the account. Discarding changes nothing in Google Ads.
"""

from __future__ import annotations

from uuid import UUID

from tin_lite.google_ads_requests import bidding_body, budget_body
from tin_lite.integrations import (
    GoogleAdsCallError,
    IntegrationDeliveryUnknownError,
    IntegrationError,
)

OPERATION = "paid_ads_proposal_apply"


def _bodies(proposal: dict, campaign: dict) -> tuple[str, dict]:
    proposed = proposal["proposed"]
    customer = campaign["customer_id"]
    if proposal["kind"] == "budget_change":
        budget = f"customers/{customer}/campaignBudgets/{campaign['external_budget_id']}"
        return budget_body(budget, proposed["daily_budget_usd"])
    campaign_resource = f"customers/{customer}/campaigns/{campaign['external_campaign_id']}"
    return bidding_body(campaign_resource, proposed["strategy"], proposed.get("target_cpa_usd"))


async def _authorized(runtime, proposal_id: UUID, clerk_user_id: str) -> dict:
    proposal = await runtime.database.get_paid_ads_proposal(proposal_id)
    if proposal is None or not await runtime.database.has_project_access(
        project_id=proposal["project_id"], clerk_user_id=clerk_user_id
    ):
        raise LookupError("proposal not found")
    return proposal


async def list_paid_ads_proposals(*, runtime, project_id: UUID, clerk_user_id: str) -> list:
    if not await runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=clerk_user_id
    ):
        raise LookupError("project not found")
    return await runtime.database.list_paid_ads_proposals(project_id=project_id)


async def discard_paid_ads_proposal(*, runtime, proposal_id: UUID, clerk_user_id: str) -> dict:
    await _authorized(runtime, proposal_id, clerk_user_id)
    return await runtime.database.review_paid_ads_proposal(
        proposal_id=proposal_id, decision="discarded", clerk_user_id=clerk_user_id
    )


async def approve_paid_ads_proposal(*, runtime, proposal_id: UUID, clerk_user_id: str) -> dict:
    """Record the approval, then apply the one change once."""
    proposal = await _authorized(runtime, proposal_id, clerk_user_id)
    proposal = await runtime.database.review_paid_ads_proposal(
        proposal_id=proposal_id, decision="approved", clerk_user_id=clerk_user_id
    )
    if proposal["status"] != "approved":
        return proposal
    campaign = await runtime.database.get_paid_ads_campaign(proposal["campaign_run_id"])
    if campaign is None or campaign["status"] != "live":
        return await runtime.database.settle_paid_ads_proposal(
            proposal_id=proposal_id, status="failed", error_code="campaign_not_live"
        )
    segment, body = _bodies(proposal, campaign)
    key = f"paid_ads_proposal:{proposal_id}:apply"
    database = runtime.database
    async with database.effect_lock(key, OPERATION) as (conn, existing):
        if existing is not None and existing.status == "completed":
            outcome = existing.result or {}
        else:
            if existing is not None and (existing.result or {}).get("attempted_at"):
                outcome = {"status": "unknown"}
            else:
                await database.start_effect(conn, execution_key=key, operation=OPERATION)
                await database.save_effect_progress(
                    conn, execution_key=key, result={"attempted_at": "now"}
                )
                try:
                    await runtime.integrations.google_ads_call(
                        project_id=proposal["project_id"],
                        kind="mutate_resource",
                        request={"segment": segment, "body": body},
                        execution_key=f"{key}:call",
                        run_id=proposal["monitor_run_id"],
                        expected_customer_id=campaign["customer_id"],
                    )
                    outcome = {"status": "applied"}
                except GoogleAdsCallError as exc:
                    outcome = {"status": "failed", "error_code": exc.code}
                except IntegrationDeliveryUnknownError:
                    outcome = {"status": "unknown"}
                except IntegrationError as exc:
                    outcome = {"status": "failed", "error_code": type(exc).__name__[:60]}
                except Exception:
                    outcome = {"status": "unknown"}
            await database.complete_effect(conn, execution_key=key, result=outcome)
    return await database.settle_paid_ads_proposal(
        proposal_id=proposal_id,
        status=outcome.get("status", "unknown"),
        error_code=outcome.get("error_code"),
    )
