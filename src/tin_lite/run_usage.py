"""A bounded Postgres usage readout. Observations are not invoices or charges."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

from tin_lite.usage_capture import count, dollars, object_value

MAX_OBSERVATIONS = 2048
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "cache_write_input_tokens",
)
CODEX_FIELDS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cachedInputTokens",
    "reasoningOutputTokens",
    "cacheWriteInputTokens",
)
# Supplier public list prices observed 2026-09-10. This is a wall-time reference
# estimate, NOT measured active/billed seconds (paused sandboxes may be cheaper).
E2B_RATE_CARD = {
    "id": "e2b-public-2026-09-10",
    "source": "https://e2b.dev/pricing",
    "vcpu_second_usd": "0.000014",
    "gib_second_usd": "0.0000045",
}


async def read_run_usage(*, database, run):
    """Caller must authorize this exact run's project before entering this service."""
    async with database.pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            own = await _own(conn, run)
            children = []
            warnings = []
            if run.executor == "organic.traffic_system":
                # Only this recipe's actual child ownership counts. Artifact input
                # references and arbitrary receipt run IDs are not new spend.
                from tin_lite.organic_system import STEPS

                seen = {run.id}
                for step in STEPS:
                    row = await conn.fetchrow(
                        """SELECT r.id FROM effect_receipts e JOIN workflow_runs r
                           ON r.id::text = e.result->>'run_id'
                           WHERE e.execution_key=$1 AND e.operation='organic.traffic_system'
                             AND e.status='completed' AND r.project_id=$2
                             AND r.start_idempotency_key=$3""",
                        f"traffic:{run.id}:step:{step}",
                        run.project_id,
                        f"system:{run.id}:{step}",
                    )
                    if row and row["id"] not in seen:
                        seen.add(row["id"])
                        child = await database.get_run(row["id"], conn=conn)
                        children.append({"step": step, **await _own(conn, child)})
                warnings.append("Only owned child runs count; referenced input runs are excluded.")
            observations = own["observations"] + [
                item for child in children for item in child["observations"]
            ]
            rate_cards = [E2B_RATE_CARD]
            if any(item.get("api_list_price") is not None for item in observations):
                from tin_lite.codex_api_pricing import RATE_CARD

                rate_cards.append(RATE_CARD)
            return {
                "schema": "tin-run-usage-v1",
                "run_id": str(run.id),
                "currency": "USD",
                "read_source": "postgres",
                "own": own,
                "children": children,
                "inclusive_totals": totals(observations),
                "stages": stage_totals(observations),
                "coverage": "partial",
                "warnings": [
                    "This is observed usage, not an invoice, charge, or spending guarantee.",
                    "Unknown usage and costs remain null. Known subtotals are not a complete bill.",
                    "Legacy and uninstrumented calls may be absent; no observations is not free.",
                    "Only API Codex receipts with pinned pricing carry a list-price cost; "
                    "Codex ChatGPT OAuth is not API-model billing.",
                    "Compute estimates use observed wall time, not billed active seconds; "
                    "pauses, storage, base plans and discounts are not reconciled.",
                    *warnings,
                ],
                "rate_cards": rate_cards,
            }


async def _own(conn, run):
    rows = await conn.fetch(
        """SELECT execution_key, operation, status,
            jsonb_build_object(
              'provider', result->'provider', 'model', result->'model',
              'requested_model', result->'requested_model', 'outcome', result->'outcome',
              'pricing', result->'pricing', 'service_tier', result->'service_tier',
              'endpoint', result->'endpoint',
              'protocol', result->'protocol', 'response_object', result->'response_object',
              'requested_service_tier', result->'requested_service_tier',
              'step', result->'step', 'category', result->'category', 'usage', result->'usage',
              'reported_cost_usd', result->'reported_cost_usd',
              'observed_wall_seconds', result->'observed_wall_seconds',
              'cpu_count', result->'cpu_count', 'memory_mb', result->'memory_mb',
              'legacy_status', result->'status', 'attempted_at', result->'attempted_at',
              'legacy_usage', result#>'{value,usage}', 'legacy_model', result#>'{value,model}',
              'legacy_provider', result#>'{value,provider}',
              'legacy_cost', result#>'{value,reported_cost_usd}'
            ) AS facts
          FROM effect_receipts
          WHERE (operation IN ('native_model_usage_v1', 'isolated_codex_attempt_v1',
                               'external_usage_v1', 'sandbox_usage_v1', 'codex_api_usage_v1')
                 AND result->>'run_id'=$1)
             OR (operation='organic.audit' AND execution_key LIKE $2)
             OR (operation='organic.keyword_plan' AND execution_key LIKE $3)
          ORDER BY created_at, execution_key LIMIT $4""",
        str(run.id),
        f"organic:{run.id}:%",
        f"keyword:{run.id}:%",
        MAX_OBSERVATIONS + 1,
    )
    rows, truncated = rows[:MAX_OBSERVATIONS], len(rows) > MAX_OBSERVATIONS
    records = [(row, json.loads(row["facts"])) for row in rows]
    legacy_steps = {
        row["execution_key"].split(":", 2)[2]: facts
        for row, facts in records
        if row["operation"] in {"organic.audit", "organic.keyword_plan"}
    }
    covered_steps = set()
    for row, facts in records:
        if row["operation"] in {"native_model_usage_v1", "external_usage_v1"}:
            step = facts.get("step")
            if isinstance(step, str):
                covered_steps.add(step.removeprefix("keyword:"))
    observations = []
    for row, facts in records:
        if row["operation"] == "external_usage_v1" and facts.get("category") == "tool":
            # A lost submit response can later be reconciled by the owning audit
            # receipt. Fill its missing cost, not a second billable observation.
            recovered = dollars(legacy_steps.get(facts.get("step"), {}).get("legacy_cost"))
            if facts.get("reported_cost_usd") is None and recovered and Decimal(recovered) > 0:
                facts["reported_cost_usd"] = recovered
        legacy = row["operation"] in {"organic.audit", "organic.keyword_plan"}
        if legacy:
            step = row["execution_key"].split(":", 2)[2]
            if step in covered_steps:
                continue
            if row["operation"] == "organic.keyword_plan" and step == "gsc":
                continue  # Connected Search Console reads are not model purchases.
            usage, cost = facts.get("legacy_usage"), facts.get("legacy_cost")
            if usage is None and cost is None and not facts.get("attempted_at"):
                continue
            # Budget reservations, publication and available scope are not paid usage.
            facts = {
                "usage": usage,
                # Old adapters sometimes defaulted absent cost to zero; do not
                # promote that historical fallback to an observed free request.
                "reported_cost_usd": cost if dollars(cost) and Decimal(dollars(cost)) > 0 else None,
                "model": facts.get("legacy_model"),
                "provider": facts.get("legacy_provider"),
                "outcome": facts.get("legacy_status") or "unconfirmed",
                "category": "tool"
                if cost is not None
                else "model"
                if usage is not None
                else "unknown",
            }
        observations.append(observation(row, facts, legacy=legacy))
    return {
        "run_id": str(run.id),
        "workflow": run.workflow_name,
        "status": run.status.value,
        "observations": observations,
        "totals": totals(observations),
        "truncated": truncated,
    }


def observation(row, facts, *, legacy=False):
    operation = row["operation"]
    codex = operation == "isolated_codex_attempt_v1"
    compute = operation == "sandbox_usage_v1"
    usage = object_value(facts.get("usage"))
    kind = (
        "compute"
        if compute
        else (
            "codex_chatgpt_oauth"
            if codex
            else (
                "tool"
                if facts.get("category") == "tool"
                else ("unknown" if facts.get("category") == "unknown" else "native_model_service")
            )
        )
    )
    if codex:
        raw = object_value(usage.get("total"))
        usage = {
            field: raw.get(source) for field, source in zip(TOKEN_FIELDS, CODEX_FIELDS, strict=True)
        }
    elif legacy:
        # Historical native results used both Responses and router usage shapes.
        usage = {
            **usage,
            "cached_input_tokens": usage.get(
                "cached_input_tokens",
                object_value(usage.get("input_tokens_details")).get("cached_tokens"),
            ),
            "reasoning_tokens": usage.get(
                "reasoning_tokens",
                object_value(usage.get("output_tokens_details")).get("reasoning_tokens"),
            ),
        }
    if operation == "codex_api_usage_v1":
        kind = "codex_openai_api"
    if facts.get("category") == "connected_api":
        kind = "connected_api"
    api_price = None
    if kind == "codex_openai_api":
        from tin_lite.codex_api_pricing import price_response

        priced = price_response(facts.get("pricing"), facts)
        if priced is not None:
            api_price = {
                **priced[1],
                "total_nanos": priced[0],
                "total_usd": str(Decimal(priced[0]) / 1_000_000_000),
            }
    normalized = {field: count(usage.get(field)) for field in TOKEN_FIELDS}
    for field in ("requests", "web_search_calls"):
        normalized[field] = count(usage.get(field))
    wall = dollars(facts.get("observed_wall_seconds")) if compute else None
    cpu, memory = count(facts.get("cpu_count")), count(facts.get("memory_mb"))
    estimate = None
    if wall is not None and cpu and memory:
        estimate = str(
            (
                Decimal(wall)
                * (
                    Decimal(cpu) * Decimal(E2B_RATE_CARD["vcpu_second_usd"])
                    + Decimal(memory) / 1024 * Decimal(E2B_RATE_CARD["gib_second_usd"])
                )
            ).quantize(Decimal("0.000000001"))
        )
    provider = "e2b" if compute else "openai" if codex else facts.get("provider")
    if kind == "connected_api":
        from tin_lite.project_connections import CUSTOM_KEY

        if provider not in {
            "analytics.gsc",
            "infra.github",
            "workspace.google",
            "ads.google",
            "payments.stripe",
            "analytics.posthog",
            "infra.github_user",
        } and not (isinstance(provider, str) and CUSTOM_KEY.fullmatch(provider)):
            provider = "unknown"
    elif provider not in {
        "e2b",
        "openai",
        "anthropic",
        "gemini",
        "openrouter",
        "dataforseo",
        "gak",
        "google_ads",
        "pagespeed",
    }:
        provider = "dataforseo" if kind == "tool" else "unknown"
    model = (
        object_value(facts.get("usage")).get("model")
        if codex
        else facts.get("model") or facts.get("requested_model")
    )
    # Only exact scalar/numeric fields leave the trusted receipt store.
    outcome = facts.get("outcome")
    if outcome not in {
        "unconfirmed",
        "response_received",
        "invalid_output",
        "failed",
        "cancelled",
        "checkpoint_returned",
        "running",
        "deleted",
        "already_absent",
        "completed",
    }:
        outcome = "unconfirmed"
    return {
        "id": hashlib.sha256(row["execution_key"].encode()).hexdigest()[:24],
        "kind": kind,
        "provider": provider,
        "model": model[:150] if isinstance(model, str) else None,
        "outcome": outcome,
        "source": "legacy_effect" if legacy else "trusted_observation",
        "stage": usage_stage(facts, kind),
        **({"api_list_price": api_price} if kind == "codex_openai_api" else {}),
        **(
            {"billed_by": "connected_provider", "tin_credit_deduction": False}
            if kind == "connected_api"
            else {}
        ),
        "usage": normalized,
        "observed_wall_seconds": wall,
        "provider_reported_cost_usd": dollars(facts.get("reported_cost_usd")),
        "reference_estimate_usd": estimate,
        "estimate_basis": "e2b-public-2026-09-10:observed-wall-time" if estimate else None,
    }


def usage_stage(facts, kind):
    # Only trusted receipt metadata defines a stage. Never infer it from article text.
    endpoint = facts.get("endpoint")
    if endpoint in {"/v1/responses/compact", "/responses/compact", "responses/compact", "compact"}:
        return "context_compaction"
    step = facts.get("step")
    if isinstance(step, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", step):
        return step
    return {"compute": "compute", "connected_api": "connected_api", "tool": "provider_tool"}.get(
        kind, "model_unspecified"
    )


def stage_totals(observations):
    groups = {}
    for item in observations:
        groups.setdefault(item.get("stage", "unspecified"), []).append(item)
    return [{"stage": stage, **totals(items)} for stage, items in sorted(groups.items())]


def _sum_known(values):
    known = [Decimal(value) for value in values if value is not None]
    return str(sum(known)) if known else None


def totals(observations):
    return {
        "observation_count": len(observations),
        "known_provider_reported_cost_usd": _sum_known(
            item["provider_reported_cost_usd"] for item in observations
        ),
        "known_reference_estimate_usd": _sum_known(
            item["reference_estimate_usd"] for item in observations
        ),
        "known_api_list_price_usd": _sum_known(
            (item.get("api_list_price") or {}).get("total_usd") for item in observations
        ),
        "total_cost_usd": None,  # Deliberately not the sum of incomplete/mixed price bases.
        "unpriced_observations": sum(
            item["provider_reported_cost_usd"] is None
            and item["reference_estimate_usd"] is None
            and item.get("api_list_price") is None
            for item in observations
        ),
        "tokens_by_execution_kind": {
            kind: {
                field: (
                    sum(known)
                    if (
                        known := [
                            item["usage"][field]
                            for item in observations
                            if item["kind"] == kind and item["usage"][field] is not None
                        ]
                    )
                    else None
                )
                for field in TOKEN_FIELDS
            }
            for kind in ("native_model_service", "codex_chatgpt_oauth", "codex_openai_api")
        },
        "observed_sandbox_wall_seconds": _sum_known(
            item["observed_wall_seconds"] for item in observations
        ),
    }
