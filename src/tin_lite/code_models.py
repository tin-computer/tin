"""Run-bound model calls for isolated code. No author source executes here."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from copy import deepcopy
from dataclasses import asdict

from tin_lite.billing_contracts import NANOS_PER_CENT, BillingError, digest
from tin_lite.domain import RunStatus
from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelRoute,
    ProviderName,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.service_pricing import CARD, model_maximum
from tin_lite.workflow_code import (
    MODEL_TARGETS,
    RUN_MODEL_INPUT_WARNING_BYTES,
    validate_code_definition,
)

logger = logging.getLogger(__name__)

OPERATION = "code_model_call_v1"
MAX_RESPONSE_BYTES = 1_000_000
# Serialized request bytes per input token assumed when reserving a model call. The o200k
# tokenizer measures 4.4-4.7 bytes per token on prose, code and JSON, and real workflow input
# about 3.5, so three stays above the tokens a call is charged for. Tin absorbs any excess.
BYTES_PER_INPUT_TOKEN = 3
# Fixed per-call allowance for the provider's own request envelope, in tokens.
REQUEST_ENVELOPE_TOKENS = 4096
ERRORS = {
    "invalid_model_request": "The model request exceeds or differs from its declared contract.",
    "model_unavailable": "The declared managed model route is unavailable.",
    "model_step_conflict": "This model step already has a different request. Use a distinct step.",
    "model_call_limit": "The workflow reached its declared model-call limit.",
    "model_result_unconfirmed": (
        "A model call has an unconfirmed result. Tin will not purchase it again automatically."
    ),
    "model_output_invalid": (
        "The model response failed output validation; incurred usage remains recorded."
    ),
    "model_access_revoked": "The run no longer has permission to use managed models.",
    "model_spending_stopped": "The managed model call has no authorized spending allocation.",
}


class CodeModelError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


def registered_routes():
    return tuple(
        ModelRoute(
            key=f"workflow.code:{provider}:{model}",
            provider=ProviderName(provider),
            model=model,
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
        )
        for provider, model in sorted(MODEL_TARGETS)
    )


def call_input_tokens(input_bytes):
    """The input tokens one call reserves for its serialized request."""
    return -(-input_bytes // BYTES_PER_INPUT_TOKEN) + REQUEST_ENVELOPE_TOKENS


def model_routes_maximum(terms, routes):
    """The most a run's declared model calls can cost at these terms.

    Every call may send its route's whole max_input_bytes, at BYTES_PER_INPUT_TOKEN, and use
    its whole output allowance. The run's input warning does not lower this: it refuses nothing.
    """
    return sum(
        route.max_calls
        * model_maximum(
            terms,
            provider=route.provider,
            model=route.model,
            input_tokens=call_input_tokens(route.max_input_bytes),
            output_tokens=route.max_output_tokens,
        )
        for route in routes
    )


def model_terms(definition):
    """Metered code terms: declared model routes plus paid managed service reads."""
    from tin_lite.managed_services import CALL_CEILING_USD
    from tin_lite.service_pricing import amount_nanos

    spec = validate_code_definition(definition)
    terms = {
        "rate_card": CARD["id"],
        "service_pricing": deepcopy(CARD),
        "currency": "USD",
        "definition_sha256": digest(definition),
        "kind": "metered_workflow",
        "operations": ["native_model"] + (["tool"] if spec.paid_services else []),
        "execution_fee_nanos": 0,
        "rounding": "half_up_cent_at_root_settlement",
        "failure_policy": "verified_usage; platform_duplicates_and_overages_absorbed",
        "unknown_policy": "pending_up_to_24h_then_unresolved_cost_absorbed",
    }
    terms["maximum_nanos"] = model_routes_maximum(terms, spec.model_routes) + sum(
        # Each paid read reserves its provider's per-call ceiling; reported cost settles it.
        service.max_calls * amount_nanos(CALL_CEILING_USD[service.provider_key])
        for service in spec.paid_services
    )
    # The shared UI/ledger settles cents. A paid route must not display a $0.00
    # ceiling; this rounds only its conservative bound, never its actual usage.
    terms["maximum_nanos"] = (
        (terms["maximum_nanos"] + NANOS_PER_CENT - 1) // NANOS_PER_CENT * NANOS_PER_CENT
    )
    return terms


def validate_output_schema(schema, *, depth=0):
    """Closed, bounded JSON Schema; no references, regexes or remote resolution."""
    if not isinstance(schema, dict) or depth > 5:
        raise ValueError("unsupported model output schema")
    kind = schema.get("type")
    fields = {"type", "description", "enum"}
    if kind == "object":
        fields |= {"properties", "required", "additionalProperties"}
        props = schema.get("properties")
        if (
            not isinstance(props, dict)
            or not 1 <= len(props) <= 32
            or schema.get("additionalProperties") is not False
            or not isinstance(schema.get("required"), list)
            or sorted(schema["required"]) != sorted(props)
        ):
            raise ValueError("model objects require all properties and must be closed")
        for value in props.values():
            validate_output_schema(value, depth=depth + 1)
    elif kind == "array":
        fields |= {"items", "minItems", "maxItems"}
        validate_output_schema(schema.get("items"), depth=depth + 1)
        if type(schema.get("maxItems")) is not int or not 1 <= schema["maxItems"] <= 100:
            raise ValueError("model arrays require maxItems of 1-100")
    elif kind == "string":
        fields |= {"minLength", "maxLength"}
    elif kind in {"number", "integer"}:
        fields |= {"minimum", "maximum"}
    elif kind not in {"boolean", "null"}:
        raise ValueError("unsupported model schema type")
    if set(schema) - fields:
        raise ValueError("unsupported model schema fields")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 100
    ):
        raise ValueError("model enums must be bounded")
    if depth == 0:
        import jsonschema

        if kind != "object" or len(json.dumps(schema, allow_nan=False).encode()) > 8000:
            raise ValueError("model output schema must be one bounded object")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError:
            raise ValueError("invalid model output schema") from None


def request_bytes(request):
    """Serialized request size: what max_input_bytes and the run's input budget count."""
    return len(json.dumps(asdict(request), ensure_ascii=False, allow_nan=False).encode())


def request_contract(spec, payload):
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "route",
            "step",
            "instructions",
            "data",
            "output_schema",
        }:
            raise ValueError("invalid model request fields")
        if not isinstance(payload["step"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", payload["step"]
        ):
            raise ValueError("invalid model step")
        route = next(r for r in spec.model_routes if r.name == payload["route"])
        instructions = payload["instructions"]
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("model instructions are required")
        schema = payload["output_schema"]
        if schema is not None:
            validate_output_schema(schema)
        request = ModelRequest(
            messages=(
                ModelMessage(MessageRole.USER, json.dumps(payload["data"], allow_nan=False)),
            ),
            system=instructions,
            max_output_tokens=route.max_output_tokens,
            output_schema=schema,
        )
        if request_bytes(request) > route.max_input_bytes:
            raise ValueError("model input exceeds declared allowance")
        return route, request
    except (ValueError, TypeError, KeyError, StopIteration, RecursionError):
        raise CodeModelError("invalid_model_request") from None


class CodeModels:
    def __init__(self, *, database, router, settings):
        self.db, self.router, self.settings = database, router, settings

    async def authorize(self, *, conn, run, workflow, require_budget=False):
        from tin_lite.private_workflows import require_private_execution

        fresh = await self.db.get_run(run.id, conn=conn)
        if (
            fresh is None
            or fresh.status not in {RunStatus.PENDING, RunStatus.RUNNING}
            or not run.lease_owner
            or not run.sandbox_id
            or not await self.db.validate_lease(
                project_id=run.project_id,
                thread_id=run.thread_id,
                generation=run.generation,
                lease_owner=run.lease_owner,
                fencing_token=run.fencing_token,
                sandbox_id=run.sandbox_id,
                conn=conn,
            )
            or not fresh.started_by_clerk_user_id
            or not await self.db.has_project_access(
                project_id=run.project_id, clerk_user_id=fresh.started_by_clerk_user_id, conn=conn
            )
        ):
            raise CodeModelError("model_access_revoked")
        require_private_execution(self.settings, workflow, run.project_id)
        # Paid managed reads ask for the reserved budget here. A model call doesn't: billing's
        # begin_operation funds it from that budget, or from Tin for a run admission included
        # (an approved onboarding setup step), which has no budget row.
        if (
            require_budget
            and self.db.billing is not None
            and not await conn.fetchval(
                "SELECT true FROM billing_run_budgets WHERE run_id=$1 AND status='reserved'", run.id
            )
        ):
            raise CodeModelError("model_spending_stopped")

    async def generate(self, *, conn, run, workflow, spec, payload):
        route, request = request_contract(spec, payload)
        input_bytes = request_bytes(request)
        step = payload["step"]
        fingerprint = digest(
            {
                "definition": run.definition_commit_sha,
                "route": asdict(route),
                "request": asdict(request),
            }
        )
        prefix = f"{run.id}:code-model:"
        key = prefix + digest(step)
        # One per-run lock serializes author concurrency and reuses the activity connection.
        async with self.db.effect_lock(f"{run.id}:code-model-gate", OPERATION, conn=conn):
            await self.authorize(conn=conn, run=run, workflow=workflow)
            saved = await self.db.get_effect(key, conn=conn)
            if saved:
                record = saved.result or {}
                if record.get("fingerprint") != fingerprint:
                    raise CodeModelError("model_step_conflict")
                if saved.status == "completed":
                    if record.get("error"):
                        raise CodeModelError(record["error"])
                    return record["response"]
                raise CodeModelError("model_result_unconfirmed")
            if await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM effect_receipts WHERE operation=$1
                   AND execution_key LIKE $2 AND status <> 'completed')""",
                OPERATION,
                prefix + "%",
            ):
                # Changing a step ID cannot bypass an unresolved supplier attempt.
                raise CodeModelError("model_result_unconfirmed")
            if (
                self.router is None
                or ProviderName(route.provider) not in self.router.configured_providers
            ):
                raise CodeModelError("model_unavailable")
            used = await conn.fetchval(
                """SELECT count(*) FROM effect_receipts WHERE operation=$1
                   AND execution_key LIKE $2 AND result->>'route'=$3""",
                OPERATION,
                prefix + "%",
                route.name,
            )
            if used >= route.max_calls:
                raise CodeModelError("model_call_limit")
            # A soft gate: a run whose model input passes the expected bound still sends the
            # call, and the crossing is logged once for operators. Every started call counts,
            # whatever its outcome; receipts written before input_bytes was recorded count as 0.
            sent = await conn.fetchval(
                """SELECT COALESCE(sum((result->>'input_bytes')::bigint), 0)
                   FROM effect_receipts WHERE operation=$1 AND execution_key LIKE $2""",
                OPERATION,
                prefix + "%",
            )
            if sent <= RUN_MODEL_INPUT_WARNING_BYTES < sent + input_bytes:
                logger.warning(
                    "code workflow model input passed %s bytes: run=%s workflow=%s step=%s "
                    "sent=%s call=%s",
                    RUN_MODEL_INPUT_WARNING_BYTES,
                    run.id,
                    workflow.key,
                    step,
                    sent,
                    input_bytes,
                )
            record = {
                "version": 1,
                "run_id": str(run.id),
                "definition_commit_sha": run.definition_commit_sha,
                "step": step,
                "route": route.name,
                "fingerprint": fingerprint,
                "input_bytes": input_bytes,
            }
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            await self.db.save_effect_progress(conn, execution_key=key, result=record)
            try:
                # Recorder owns paid intent, reservation and observed usage. The owning
                # operation below owns recoverable output, just like native activities.
                with model_usage_scope(
                    run_id=run.id,
                    step=f"code:{step}",
                    conn=conn,
                    input_tokens=call_input_tokens(input_bytes),
                ):
                    # The package's declared runtime bounds its model calls: a slower answer
                    # arrives after the package has given up on it. The provider waits as
                    # long, not its shorter default, so a slow answer within the window lands.
                    async with asyncio.timeout(spec.timeout_seconds):
                        result = await self.router.generate(
                            route.router_key, request, timeout_seconds=spec.timeout_seconds
                        )
            except ModelProviderError as exc:
                if exc.observation is not None:
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={**record, "error": "model_output_invalid"},
                    )
                    raise CodeModelError("model_output_invalid") from None
                raise CodeModelError("model_result_unconfirmed") from None
            except TimeoutError:
                raise CodeModelError("model_result_unconfirmed") from None
            except BillingError:
                await self.db.complete_effect(
                    conn, execution_key=key, result={**record, "error": "model_spending_stopped"}
                )
                raise CodeModelError("model_spending_stopped") from None
            response = {"text": result.text, "parsed": result.parsed}
            try:
                valid = (
                    len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode())
                    <= MAX_RESPONSE_BYTES
                )
            except (ValueError, TypeError, UnicodeError):
                valid = False
            if not valid:
                await self.db.complete_effect(
                    conn, execution_key=key, result={**record, "error": "model_output_invalid"}
                )
                raise CodeModelError("model_output_invalid")
            from tin_lite.code_progress import project_completed_steps

            async with conn.transaction():
                await self.db.complete_effect(
                    conn, execution_key=key, result={**record, "response": response}
                )
                await project_completed_steps(conn, run.id)
            return response
