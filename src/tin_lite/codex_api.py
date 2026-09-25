"""Opt-in Codex API attempts. Credentials and execution stay separate from pricing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from tin_lite.domain import SideEffectConflictError

MODE = "openai_api_v1"
ATTEMPT = "codex_api_attempt_v1"
USAGE = "codex_api_usage_v1"
STOP_MESSAGES = {
    "run_limit": "Codex stopped at this run's quoted spending maximum.",
    "project_limit": "Codex stopped because the project's spending limit was reached.",
    "insufficient_funds": "Codex stopped because no credits remained for the next step.",
    "spending_stopped": "Further paid work was no longer authorized.",
    "request_limit": "Codex reached this run's pinned request limit.",
    "token_limit": "Codex reached this run's pinned token limit.",
}


class CodexAttemptStopped(SideEffectConflictError):
    """A paid attempt has no completed checkpoint; automatic repurchase is forbidden."""


def attempt_failure(record):
    reason = STOP_MESSAGES.get(record.get("stop_reason"))
    if reason is None and record.get("failure_type") == "TimeoutError":
        reason = "Codex execution timed out."
    if reason is None:
        reason = {
            "cancelled": "Codex execution was interrupted.",
            "timed_out": "Codex execution timed out.",
            "failed": "Codex execution failed before a completed result was recovered.",
        }.get(record.get("outcome"), "Codex execution ended without a confirmed result.")
    return CodexAttemptStopped(reason + " The paid attempt will not be repeated automatically.")


async def record_attempt_failure(conn, key, exc=None, *, stop_reason=None):
    # A previously running attempt without a surviving controller is unconfirmed.
    outcome = (
        "unconfirmed"
        if exc is None
        else ("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
    )
    facts = {"outcome": outcome, "finished_at": datetime.now(UTC).isoformat()}
    if exc is not None:
        # Keep the established attempt outcome/type contract for task turns too.
        # Exception bodies can contain provider payloads and are never stored here.
        facts["failure_type"] = type(exc).__name__
    # A relay stop recorded first stays authoritative; only fill a missing reason.
    await conn.execute(
        """UPDATE effect_receipts
           SET result=result || $2::jsonb || CASE
               WHEN $3::text IS NOT NULL AND NOT result ? 'stop_reason'
               THEN jsonb_build_object('stop_reason', $3::text) ELSE '{}'::jsonb END
           WHERE execution_key=$1 AND status='started' AND result->>'outcome'='running'""",
        key,
        json.dumps(facts),
        stop_reason,
    )


MODEL = "gpt-6-sol"
# Execution bounds; enrolled API runs separately pin their billing terms.
CONTRACT = {
    "mode": MODE,
    "model": MODEL,
    "protocol": "tin-codex-api-v1",
    "max_requests": 8,
    "max_request_bytes": 262_144,
    "max_output_tokens": 4096,
    "max_observed_tokens": 100_000,
}
# A new contract, not a reinterpretation of admitted pilot runs/quotes.
PROCEDURE_CONTRACT_V2 = {
    **CONTRACT,
    "protocol": "tin-codex-api-v2",
    "max_requests": 64,
    "max_request_bytes": 1_048_576,
    "max_output_tokens": 8192,
    "max_observed_tokens": 2_000_000,
    "context_window": 128_000,
    "auto_compact_tokens": 96_000,
}
# Search may search, open, and find within one response. Keep old admitted
# contracts intact; v3 removes only the per-response built-in tool-call ceiling.
PROCEDURE_CONTRACT = {**PROCEDURE_CONTRACT_V2, "protocol": "tin-codex-api-v3"}
# Same protocol and token/spending limits, with room for the bounded diagram
# render/repair loop's inline images. Historical text-only grants stay unchanged.
DIAGRAM_CONTRACT = {
    **PROCEDURE_CONTRACT,
    "max_request_bytes": 8 * 1024 * 1024,
    "max_non_image_bytes": PROCEDURE_CONTRACT["max_request_bytes"],
}
# Ordinary, funded procedures use the model's per-response/context capacity.
# Session spend and the existing sandbox timeout bound the job, not lifetime tokens.
# Keep v1-v3 byte-for-byte intact for admitted runs, included work and other profiles.
SESSION_CONTRACT = {
    "mode": MODE,
    "model": MODEL,
    "protocol": "tin-codex-api-v4",
    "max_request_bytes": 8 * 1024 * 1024,
    "max_output_tokens": 128_000,
    "context_window": 1_050_000,
    "auto_compact_tokens": 922_000,
}


def procedure_contract(validator=None):
    return (
        DIAGRAM_CONTRACT
        if validator in {"tin-diagram.reviewed.v1", "tin-diagram.branded.v1", "demo-video.v1"}
        else PROCEDURE_CONTRACT
    )


def api_enabled(settings, project_id):
    return bool(getattr(settings, "billing_hosted_defaults_enabled", False)) or (
        project_id in getattr(settings, "codex_api_projects", ())
    )


def supports_api_definition(definition):
    return (
        definition.get("executor") == "codex.procedure"
        and definition.get("procedure", {}).get("sandbox", {}).get("profile", "default")
        in {"default", "isolated", "browser", "studio"}
    ) or (
        definition.get("key") == definition.get("executor")
        and definition.get("key") in {"content.design_md", "project.task"}
    )


def is_procedure_contract(value):
    return value in (PROCEDURE_CONTRACT_V2, PROCEDURE_CONTRACT, DIAGRAM_CONTRACT, SESSION_CONTRACT)


def is_api_contract(value):
    return value == CONTRACT or is_procedure_contract(value)


def execution_profile(profile, contract):
    """Protect the controller without changing the workflow's workspace/result contract."""
    if is_api_contract(contract):
        if profile.profile in {"browser", "studio"} and profile.egress == "open":
            return replace(profile, profile=f"{profile.profile}_api")
        if profile.profile not in {"default", "isolated"} or profile.egress != "fenced":
            raise ValueError("This procedure profile is not supported by the Codex API route")
        return replace(profile, profile="isolated")
    if contract != {"mode": "chatgpt_oauth"}:
        raise ValueError("Unknown pinned Codex authentication contract")
    raise ValueError("Pooled OAuth execution is retired; start a new protected API run")


def attempt_key(run_id, turn_number=None):
    if turn_number is not None:
        return f"{run_id}:task_turn:{turn_number}:codex-api-attempt"
    return f"{run_id}:procedure_artifact_persist:codex-api-attempt"


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def select_contract(*, db, conn, run, procedure, settings):
    from tin_lite.free_workflows import api_terms_for_included

    budget = await conn.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id)
    terms = (
        decode_record(budget)
        if budget is not None
        else (await api_terms_for_included(db, run.id, conn=conn) or {})
    )
    # Admission fixes the auth path too. An operator flag change cannot reprice
    # an already admitted OAuth run, or strand an already reserved API run.
    # Hosted defaults apply at admission, where new runs receive pinned terms.
    # An older unbudgeted/queued run must not change auth when that switch flips.
    enabled = (
        terms.get("codex_auth") == MODE
        if terms
        else run.project_id in getattr(settings, "codex_api_projects", ())
    )
    if procedure.sandbox.profile not in {"default", "isolated", "browser", "studio"} or not enabled:
        return {"mode": "chatgpt_oauth"}
    if settings.luna_api_key is None:
        raise ValueError("Codex API execution requires Tin's server-side OpenAI credential")
    # Never bypass a workspace that enrolled after an unbilled run was admitted.
    if (
        await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM projects p JOIN billing_accounts b
           ON b.workspace_id=p.workspace_id WHERE p.id=$1 AND b.run_billing_enabled)""",
            run.project_id,
        )
        and terms.get("kind") != "codex_api"
    ):
        raise ValueError("Codex API execution requires its reserved credit budget")
    contract = terms.get("codex_contract") if terms else None
    if terms:
        # Missing on v1 credit quotes. Never upgrade those implicitly.
        contract = contract or CONTRACT
        if not is_api_contract(contract):
            raise ValueError("Unknown reserved Codex execution contract")
    else:
        contract = (
            CONTRACT
            if procedure.sandbox.isolated
            else procedure_contract(getattr(procedure, "output_validator", None))
        )
    execution_profile(procedure.sandbox, contract)
    return dict(contract)


async def pinned_contract(db, run_id, *, conn, create_operation="procedure_sandbox_create"):
    receipt = await db.get_effect(f"{run_id}:{create_operation}", conn=conn)
    value = (receipt.result or {}).get("codex_auth") if receipt is not None else None
    if value is None:
        return {"mode": "chatgpt_oauth"}  # Old persisted histories keep their original path.
    if value == {"mode": "chatgpt_oauth"} or is_api_contract(value):
        return value
    raise ValueError("Unknown pinned Codex authentication contract")


async def run_api_attempt(*, db, conn, run, sandbox_id, run_input, call, turn_number=None):
    """A retry must recover a durable checkpoint before it can reach this function."""
    key = attempt_key(run.id, turn_number)
    async with db.effect_lock(key, ATTEMPT, conn=conn) as (locked, existing):
        if existing is not None:
            raise attempt_failure(existing.result or {})
        contract = await pinned_contract(
            db,
            run.id,
            conn=locked,
            create_operation=(
                "task_codex_auth"
                if turn_number is not None
                else "sandbox_create"
                if run.executor == "content.design_md"
                else "procedure_sandbox_create"
            ),
        )
        if not is_api_contract(contract) or not run_input.isolated:
            raise ValueError("Codex API requires its pinned isolated execution contract")
        grant = secrets.token_urlsafe(32)
        from tin_lite.codex_api_pricing import RATE_CARD

        terms = decode_record(
            await locked.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id)
        )
        if not terms:
            from tin_lite.free_workflows import api_terms_for_included

            terms = await api_terms_for_included(db, run.id, conn=locked) or {}
        record = {
            "version": 1,
            "execution_kind": "codex_openai_api",
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "generation": run.generation,
            "fencing_token": run.fencing_token,
            "sandbox_id": sandbox_id,
            "thread_id": str(run.thread_id),
            "lease_owner": run.lease_owner,
            "definition_commit_sha": run.definition_commit_sha,
            "grant_sha256": token_hash(grant),
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=run_input.timeout_seconds + 60)
            ).isoformat(),
            "contract": contract,
            "pricing": terms.get("pricing", RATE_CARD),
            "outcome": "running",
            "attempted_at": datetime.now(UTC).isoformat(),
        }
        if turn_number is not None:
            if run.executor != "project.task" or turn_number != run.task_turn_number + 1:
                raise ValueError("Codex API task attempt must match the next active turn")
            record["turn_number"] = turn_number
        await db.start_effect(locked, execution_key=key, operation=ATTEMPT)
        await db.save_effect_progress(locked, execution_key=key, result=record)

        controller_stop = {}

        async def controller_usage(value):
            # The isolated stream is still checked by E2BRuntime. API response receipts,
            # not this overlapping thread total, are the supplier usage authority. Its
            # limit flag only explains why the controller interrupted the turn.
            if value.get("limit_reached") is True:
                controller_stop["reason"] = "token_limit"

        async def failed(exc):
            # Revoke admission before salvage/cleanup. Merge in SQL so a relay's
            # independently recorded budget stop cannot be overwritten here.
            await record_attempt_failure(
                locked, key, exc, stop_reason=controller_stop.get("reason")
            )

        try:
            result = await call(
                replace(
                    run_input,
                    api_grant=grant,
                    api_contract=contract,
                    usage_sink=controller_usage,
                    **(
                        {"failure_sink": failed}
                        if turn_number is None and hasattr(run_input, "failure_sink")
                        else {}
                    ),
                )
            )
        except BaseException as exc:
            try:
                await asyncio.wait_for(failed(exc), timeout=5)
            except BaseException:  # noqa: S110 — retain original error; never log raw transport
                pass
            raise
        else:
            latest = await db.get_effect(key, conn=locked)
            record = latest.result or record
            record.update(outcome="checkpoint_returned", finished_at=datetime.now(UTC).isoformat())
            if turn_number is not None:
                # A completed turn can replay its sanitized result/projection without
                # buying another response or needing its already-deleted sandbox.
                result = replace(
                    result,
                    delivered_entry_ids=tuple(
                        sorted(
                            set(result.delivered_entry_ids) | set(run_input.context_delivery_ids)
                        )
                    ),
                )
                record["task_result"] = asdict(result)
            elif run.executor == "codex.procedure":
                # Immutable recovery does not depend on the discovery branch remaining visible.
                record["procedure_revision"] = result.ephemeral_commit_sha
            await db.complete_effect(locked, execution_key=key, result=record)
            return result


def decode_record(value):
    return json.loads(value) if isinstance(value, str) else dict(value or {})
