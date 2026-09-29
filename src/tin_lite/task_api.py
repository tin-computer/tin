"""Per-turn Codex API authentication and checkpoint recovery for interactive tasks."""

from tin_lite import codex_api, design_api
from tin_lite.e2b_runtime import SandboxTaskResult


async def auth_contract(db, conn, run, settings, *, legacy_turn=False):
    key = f"{run.id}:task_codex_auth"
    async with db.effect_lock(key, "task_codex_auth", conn=conn) as (locked, receipt):
        if receipt is not None and receipt.status == "completed":
            return await codex_api.pinned_contract(
                db, run.id, conn=locked, create_operation="task_codex_auth"
            )
        # Started/paused tasks from before this migration retain their pooled login.
        # New paid admissions pin API in their budget even if compute starts later.
        budget = codex_api.decode_record(
            await locked.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id)
        )
        contract = (
            {"mode": "chatgpt_oauth"}
            if (legacy_turn or run.task_turn_number > 0) and not budget.get("codex_auth")
            else await codex_api.select_contract(
                db=db,
                conn=locked,
                run=run,
                procedure=design_api.procedure(timeout_seconds=settings.sandbox_timeout_seconds),
                settings=settings,
            )
        )
        await db.start_effect(locked, execution_key=key, operation="task_codex_auth")
        await db.complete_effect(locked, execution_key=key, result={"codex_auth": contract})
        return contract


async def recovered_turn(db, conn, run, turn_number):
    receipt = await db.get_effect(codex_api.attempt_key(run.id, turn_number), conn=conn)
    if receipt is None:
        return None
    record = receipt.result or {}
    if (
        receipt.status == "completed"
        and record.get("outcome") == "checkpoint_returned"
        and record.get("generation") == run.generation
        and record.get("turn_number") == turn_number
        and isinstance(record.get("task_result"), dict)
    ):
        value = dict(record["task_result"])
        value["delivered_entry_ids"] = tuple(value.get("delivered_entry_ids", ()))
        return SandboxTaskResult(**value)
    raise codex_api.attempt_failure(record)
