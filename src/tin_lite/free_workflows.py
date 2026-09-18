"""Tin-funded onboarding, pinned at admission outside the customer's credit ledger."""

OPERATION = "included_workflow_v1"
ONBOARDING = {
    "growth.onboarding": "growth.onboarding",
    "growth.onboarding_plan": "growth.onboarding_plan",
}


def onboarding_is_free(definition):
    return ONBOARDING.get(definition.get("key")) == definition.get("executor") and bool(
        definition.get("executor")
    )


def receipt_key(run_id):
    return f"billing-included:{run_id}"


async def included_execution(db, run_id, *, conn):
    receipt = await db.get_effect(receipt_key(run_id), conn=conn)
    if (
        receipt is not None
        and receipt.operation == OPERATION
        and receipt.status == "completed"
        and (receipt.result or {}).get("run_id") == str(run_id)
    ):
        return receipt.result
    return None


async def api_terms_for_included(db, run_id, *, conn):
    included = await included_execution(db, run_id, conn=conn)
    return included.get("api_terms") or {"codex_auth": "chatgpt_oauth"} if included else None
