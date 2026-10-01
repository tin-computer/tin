"""Keyword policy v7: screening in batches, with one larger retry when a batch is cut off.

Production run a59cded8 (v6) screened every candidate in one call capped at 5,000 output
tokens. The model reasoned for 4,221 of them, stopped mid-JSON, and the run failed after
buying its research. v7 screens at low reasoning effort and sizes each cap as a reasoning
budget plus an answer budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_keyword_plan import finish, fixture, provider_row
from test_keyword_plan_v6 import worst_run

from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v3 as v3
from tin_lite import keyword_plan_v4 as v4
from tin_lite import keyword_plan_v5 as v5
from tin_lite import keyword_plan_v6 as v6
from tin_lite import keyword_plan_v7 as v7
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import RunStatus
from tin_lite.keyword_plan_activities import route_definition
from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelCapabilityError,
    ModelMessage,
    ModelObservation,
    ModelOutputTruncated,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    ModelUsage,
    OpenAIModelProvider,
    ProviderName,
    ReasoningEffort,
    model_failure_reason,
)
from tin_lite.organic_audit import canonical_json
from tin_lite.service_pricing import CARD, NANOS_PER_DOLLAR

SCREENING = {
    "version",
    "triage_batch_size",
    "triage_reasoning_effort",
    "triage_answer_tokens",
    "triage_output_tokens",
    "triage_retry_output_tokens",
    "triage_max_request_bytes",
    "triage_reservation_usd",
    "triage_retry_reservation_usd",
}
LUNA = CARD["models"]["gpt-6-luna"]


def label(keyword):
    """A fixed label per keyword, so one call and many batches must agree."""
    return list(v2.FIT)[hashlib.sha256(keyword.encode()).digest()[0] % len(v2.FIT)]


def many_rows(kind, value):
    """Thirty distinct provider rows per lookup, so research yields well over 50 candidates."""
    base = value if isinstance(value, str) else value.get("seeds", ["consulting"])[0]
    return [provider_row(f"{base} {kind} option {index}") for index in range(30)]


def widen(provider):
    original = provider.query.side_effect

    async def query(kind, **kwargs):
        result = await original(kind, **kwargs)
        if kind not in {"competitors", "serp"}:
            result["items"] = many_rows(kind, kwargs["value"])
            result["items_count"] = result["total_count"] = len(result["items"])
        return result

    provider.query.side_effect = query


def truncated(request, *, reasoning=None):
    """A response cut off at its cap; `reasoning` is how many of those tokens were reasoning."""
    return ModelOutputTruncated(
        "openai model stopped at its output-token cap",
        observation=ModelObservation(
            ProviderName.OPENAI,
            "gpt-6-luna",
            "fixture",
            ModelUsage(output_tokens=request.max_output_tokens, reasoning_tokens=reasoning),
        ),
    )


def mostly_reasoning(request):
    """Most of the cap goes to reasoning, as in a59cded8; the answer is left 400, not 1,000."""
    return truncated(request, reasoning=request.max_output_tokens - 400)


def screening(model, *, cut=lambda request, count: False, error=truncated):
    """Label every slot by keyword. `cut(request, count)` decides which screening calls run out
    of output, and `error(request)` how; `count` numbers the screening calls from 1."""
    original = model.generate.side_effect
    calls = []

    async def generate(route, request, *, timeout_seconds=None):
        if request.output_schema_name != "keyword_triage":
            return await original(route, request, timeout_seconds=timeout_seconds)
        slots = json.loads(request.messages[0].content)["candidates"]
        # Canonical JSON sorts the slot keys (k1, k10, k2, ...); keep candidate order.
        calls.append((request.max_output_tokens, [slots[f"k{i + 1}"] for i in range(len(slots))]))
        if cut(request, len(calls)):
            raise error(request)
        result = await original(route, request, timeout_seconds=timeout_seconds)
        return replace(result, parsed={key: label(keyword) for key, keyword in slots.items()})

    model.generate.side_effect = generate
    return calls


async def collected(version, *, cut=None, error=truncated):
    activities, db, storage, provider, model = await fixture(modern=version)
    widen(provider)
    calls = screening(model, **({"cut": cut} if cut else {}), error=error)
    return activities, db, storage, provider, model, calls


def triage_requests(model):
    return [
        call.args[1]
        for call in model.generate.await_args_list
        if call.args[1].output_schema_name == "keyword_triage"
    ]


# ---------------------------------------------------------------- the pinned contracts


def test_v7_changes_only_screening_and_leaves_earlier_pins_alone():
    assert {k: v for k, v in v7.POLICY.items() if k not in SCREENING} == {
        k: v for k, v in v6.POLICY.items() if k not in SCREENING
    }
    assert v7.INSTRUCTIONS == v6.INSTRUCTIONS and v7.SCHEMAS == v6.SCHEMAS
    assert v7.seed_values is v6.seed_values
    # Runs pinned to v2-v6 keep one 5,000-token screening call at the model's own effort, their
    # reservations, and the route they pinned, which takes no reasoning effort.
    old_route = {
        "key": "organic.keyword_plan.v1",
        "provider": "openai",
        "model": "gpt-6-luna",
        "capabilities": ["json_schema", "text"],
    }
    for policy in (v2.POLICY, v3.POLICY, v4.POLICY, v5.POLICY, v6.POLICY):
        assert policy["triage_output_tokens"] == 5000
        assert "triage_batch_size" not in policy
        assert not any(key.endswith(("_reasoning_effort", "_answer_tokens")) for key in policy)
        assert route_definition(policy) == old_route
    assert v2.POLICY["triage_reservation_usd"] == "0.50"
    assert v6.POLICY["triage_reservation_usd"] == "0.10"
    assert v6.POLICY["version"] == "keyword-plan-v6"
    keyword = next(spec for spec in BUILTIN_WORKFLOWS if spec.key == "organic.keyword_plan")
    assert keyword.definition["keyword_policy"] == v7.POLICY
    assert keyword.version_label == "0.7.0"
    # v7 pins the same route key and says it takes a reasoning effort.
    assert keyword.definition["model_route"] == route_definition(v7.POLICY)
    assert keyword.definition["model_route"] == {
        **old_route,
        "capabilities": ["json_schema", "reasoning_effort", "text"],
    }


def test_v7_screens_at_low_effort_with_room_for_reasoning_and_the_answer():
    policy = v7.POLICY
    assert policy["triage_reasoning_effort"] == "low"
    # One label is about 9 tokens ("k50":"wrong_buyer",), so 50 need about 450; 1,000 holds them.
    assert policy["triage_answer_tokens"] >= 2 * 9 * policy["triage_batch_size"]
    # Each cap is a reasoning budget plus the answer's; the retry adds only reasoning room.
    assert v7.REASONING_TOKENS + policy["triage_answer_tokens"] == policy["triage_output_tokens"]
    assert (
        v7.RETRY_REASONING_TOKENS + policy["triage_answer_tokens"]
        == policy["triage_retry_output_tokens"]
    )
    assert (v7.REASONING_TOKENS, v7.RETRY_REASONING_TOKENS) == (7000, 15000)
    # a59cded8 reasoned 4,221 of 5,000 tokens over about 300 candidates, at the model's own
    # effort. At that rate a 50-candidate batch reasons about 700 tokens; its budget holds
    # nine times that before low effort is counted.
    assert 9 * 4221 / 300 * policy["triage_batch_size"] < v7.REASONING_TOKENS


def test_a_cut_off_answer_is_put_down_to_reasoning_only_when_reasoning_overran_its_budget():
    cause = v7.truncation_cause
    assert (
        cause(ModelUsage(output_tokens=8000, reasoning_tokens=7600), cap=8000, answer_tokens=1000)
        == "reasoning"
    )
    assert (
        cause(ModelUsage(output_tokens=8000, reasoning_tokens=7001), cap=8000, answer_tokens=1000)
        == "reasoning"
    )
    assert (
        cause(ModelUsage(output_tokens=8000, reasoning_tokens=7000), cap=8000, answer_tokens=1000)
        == "answer"
    )
    assert (
        cause(ModelUsage(output_tokens=8000, reasoning_tokens=300), cap=8000, answer_tokens=1000)
        == "answer"
    )
    assert cause(ModelUsage(output_tokens=8000), cap=8000, answer_tokens=1000) == "unknown"


def test_v7_caps_stay_inside_the_route_and_reservations_cover_their_bounds():
    policy = v7.POLICY
    # GPT-6 Luna allows 128,000 output tokens per response.
    assert policy["triage_retry_output_tokens"] == 2 * policy["triage_output_tokens"]
    assert policy["triage_retry_output_tokens"] <= v7.MODEL_OUTPUT_LIMIT == 128_000
    # The recorder counts request bytes plus 4,096 as input tokens; that stays standard band.
    tokens = policy["triage_max_request_bytes"] + 4096
    assert tokens <= CARD["long_context_above_input_tokens"]
    rate = LUNA["standard"]
    for output, reservation in (
        (policy["triage_output_tokens"], policy["triage_reservation_usd"]),
        (policy["triage_retry_output_tokens"], policy["triage_retry_reservation_usd"]),
    ):
        worst = (
            tokens * max(rate["input"], rate["cache_write"]) + output * rate["output"]
        ) / NANOS_PER_DOLLAR
        assert Decimal(worst) < Decimal(reservation)
    # Six batches and six retries still fit the $2 floor that v6 set.
    assert v7.batch_count(policy) == 6
    batched = Decimal(policy["triage_reservation_usd"]) + Decimal(
        policy["triage_retry_reservation_usd"]
    )
    total = (
        worst_run(policy)
        - Decimal(policy["triage_reservation_usd"])
        + v7.batch_count(policy) * batched
    )
    assert total == Decimal("1.79") <= Decimal(policy["minimum_ceiling_usd"])


def test_the_largest_possible_batch_fits_the_request_bound():
    """Bounded inputs (2,000-character contexts, 120-character keywords) never hit the cap."""
    scope = {
        "url": "https://" + "a" * 240 + ".example/",
        "host": "a" * 240 + ".example",
        "market": "US",
        "language": "en",
        "buyer_context": "é" * 2000,
        "started_at": "2026-10-01T00:00:00+00:00",
        "max_cost_usd": "2",
        "policy_version": v7.POLICY["version"],
        "integrations": {
            "status": "observed",
            "connections": [{"provider": "custom.api." + "x" * 60, "status": "connected"}] * 50,
            "truncated": True,
            "meaning": "Availability only.",
        },
        "audit": {"context_excerpt": '"' * 2000, "paths": {"AUDIT.md": "r" * 200}},
        "gsc": {"status": "unavailable", "reason": "x" * 200},
    }
    batch = [
        {"id": f"kw_{index:016x}", "keyword": '"' * 120}
        for index in range(v7.POLICY["triage_batch_size"])
    ]
    request = ModelRequest(
        messages=(
            ModelMessage(
                role=MessageRole.USER,
                content=canonical_json(v2.triage_input(scope, batch)).decode(),
            ),
        ),
        system=v7.INSTRUCTIONS["triage"],
        output_schema=v2.model_schema("triage", batch),
        output_schema_name="keyword_triage",
        max_output_tokens=v7.POLICY["triage_retry_output_tokens"],
        reasoning_effort=ReasoningEffort.LOW,
    )
    size = len(json.dumps(asdict(request), ensure_ascii=False).encode())
    assert size < v7.POLICY["triage_max_request_bytes"]


# ---------------------------------------------------------------- batches


async def test_batches_merge_to_exactly_what_one_call_returns():
    results = {}
    for version in ("v6", "current"):
        activities, db, _storage, _provider, _model, calls = await collected(version)
        await activities.keyword_collect(str(db.run.id))
        results[version] = (await activities._result(str(db.run.id), "collection"), calls)
    (single, single_calls), (batched, batched_calls) = results["v6"], results["current"]
    assert len(single["candidates"]) > 2 * v7.POLICY["triage_batch_size"]
    assert len(single_calls) == 1 and single_calls[0][0] == 5000
    assert len(batched_calls) == -(-len(single["candidates"]) // 50)
    assert all(len(keywords) <= 50 and tokens == 8000 for tokens, keywords in batched_calls)
    # Batches run three at a time, so they may start out of order; each is a consecutive slice.
    order = single_calls[0][1]
    batches = sorted((keywords for _, keywords in batched_calls), key=lambda k: order.index(k[0]))
    assert [keyword for keywords in batches for keyword in keywords] == order

    def labels(collection):
        return [(row["id"], row["buyer_fit"]) for row in collection["candidates"]]

    assert labels(batched) == labels(single)
    assert batched["coverage"]["buyer_fit"] == single["coverage"]["buyer_fit"]
    assert batched["triage_stages"] == [f"triage:{i}" for i in range(len(batched_calls))]
    assert "triage_stages" not in single


@pytest.fixture
def one_batch_at_a_time(monkeypatch):
    """Cutting a named call needs a fixed call order; the merge test keeps the concurrency."""
    monkeypatch.setattr("tin_lite.keyword_plan_activities.TRIAGE_CONCURRENCY", 1)


async def test_a_cut_off_batch_is_asked_once_more_with_twice_the_cap(one_batch_at_a_time):
    activities, db, _storage, _provider, _model, calls = await collected(
        "current", cut=lambda request, count: count == 1
    )
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    collection = await activities._result(run_id, "collection")
    assert collection["triage_stages"][:3] == ["triage:0", "triage:0:retry", "triage:1"]
    assert [tokens for tokens, _ in calls[:3]] == [8000, 16000, 8000]
    assert calls[0][1] == calls[1][1]  # The same batch, unchanged.
    assert all(row["buyer_fit"] == label(row["keyword"]) for row in collection["candidates"])
    first = await activities._result(run_id, "triage:0")
    assert first["status"] == "unknown" and first["reason"] == "output_truncated"
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert ledger["triage:0"] == ledger["triage:0:retry"] == "0.02"
    assert await activities._result(run_id, "failure") is None
    # A repeated activity replays every receipt and asks the model nothing more.
    count = len(calls)
    db.effects.pop(activities.key(run_id, "collection"))
    await activities.keyword_collect(run_id)
    assert len(calls) == count


async def test_a_batch_cut_off_twice_fails_named_and_keeps_paid_research(one_batch_at_a_time):
    state = {"cut": True}
    activities, db, _storage, provider, _model, calls = await collected(
        # The second batch's first attempt and its retry both run out of output.
        "current",
        cut=lambda request, count: state["cut"] and count in {2, 3},
    )
    run_id = str(db.run.id)
    with pytest.raises(ApplicationError, match="stopped at its output limit"):
        await activities.keyword_collect(run_id)
    assert [tokens for tokens, _ in calls] == [8000, 8000, 16000]  # No third batch bought.
    failure = await activities._result(run_id, "failure")
    assert failure["code"] == "output_truncated" and failure["reason"] == "output_truncated"
    assert failure["batch"] == 1 and failure["batches"] >= 3
    # The first batch keeps its receipt, and the research was saved before screening.
    assert (await activities._result(run_id, "triage:0"))["status"] == "completed"
    research = await activities._result(run_id, "research")
    assert research["candidates"] and await activities._result(run_id, "collection") is None
    await activities.keyword_failure(run_id)
    message = db.project_failure.await_args.kwargs["error_message"]
    assert "screening batch 2 of" in message and "so did its retry" in message
    assert "retrying the run reuses the research" in message
    paid = provider.query.await_count

    # Retrying the run screens again but buys none of the research.
    original = replace(db.run, status=RunStatus.FAILED)
    db.run = replace(db.run, id=uuid4(), retry_of_run_id=original.id)

    async def get_run(run_id, **kwargs):
        return original if run_id == original.id else db.run if run_id == db.run.id else None

    db.get_run = get_run
    state["cut"] = False
    retry_id = str(db.run.id)
    await activities.keyword_prepare(retry_id)
    await activities.keyword_collect(retry_id)
    assert provider.query.await_count == paid
    collection = await activities._result(retry_id, "collection")
    assert collection["reused_from_run_id"] == str(original.id)
    assert [row["id"] for row in collection["candidates"]] == [
        row["id"] for row in research["candidates"]
    ]
    assert all(row["buyer_fit"] == label(row["keyword"]) for row in collection["candidates"])


async def test_a_refused_retry_is_named_as_a_spending_stop(one_batch_at_a_time):
    activities, db, *_rest, calls = await collected("current", cut=lambda request, count: True)
    run_id = str(db.run.id)
    reserve = activities._reserve

    async def no_retry_budget(run_id, stage, amount, **kwargs):
        if stage.endswith(":retry"):
            return False
        return await reserve(run_id, stage, amount, **kwargs)

    activities._reserve = no_retry_budget
    with pytest.raises(ApplicationError, match="output limit"):
        await activities.keyword_collect(run_id)
    assert [tokens for tokens, _ in calls] == [8000]  # The retry was never sent.
    failure = await activities._result(run_id, "failure")
    assert failure["code"] == "output_truncated" and failure["reason"] == "spending_limit"
    await activities.keyword_failure(run_id)
    message = db.project_failure.await_args.kwargs["error_message"]
    assert "would have passed the run's spending limit" in message


async def test_v6_pinned_runs_keep_one_call_and_fail_without_a_retry():
    activities, db, *_rest, calls = await collected("v6", cut=lambda request, count: True)
    run_id = str(db.run.id)
    with pytest.raises(ApplicationError, match="no replacement was purchased"):
        await activities.keyword_collect(run_id)
    assert [tokens for tokens, _ in calls] == [5000]
    failure = await activities._result(run_id, "failure")
    assert failure == {"code": "model_unavailable", "stage": "triage", "reason": "output_truncated"}
    assert await activities._result(run_id, "research") is None
    await activities.keyword_failure(run_id)
    message = db.project_failure.await_args.kwargs["error_message"]
    assert "stopped at its output-token limit" in message


# ---------------------------------------------------------------- reasoning


async def test_screening_asks_for_low_effort_and_other_stages_send_what_they_did():
    requests = {}
    for version in ("v6", "current"):
        activities, db, _storage, _provider, model = await fixture(
            modern=version, inputs={"seed_phrases": []}
        )
        await finish(activities, str(db.run.id))
        requests[version] = {
            call.args[1].output_schema_name: call.args[1] for call in model.generate.await_args_list
        }
    old, new = requests["v6"], requests["current"]
    assert set(old) == set(new) == {"keyword_seeds", "keyword_triage", "keyword_review"}
    assert new["keyword_triage"].reasoning_effort is ReasoningEffort.LOW
    assert new["keyword_triage"].max_output_tokens == 8000
    # v6 screening sends no effort, as before; v7 asks for low.
    assert old["keyword_triage"].reasoning_effort is None
    assert old["keyword_triage"].max_output_tokens == 5000
    for name in ("keyword_seeds", "keyword_review"):
        assert new[name].reasoning_effort is None and old[name].reasoning_effort is None
        assert replace(new[name], messages=()) == replace(old[name], messages=())


async def test_a_batch_cut_off_by_reasoning_retries_with_more_reasoning_room(
    one_batch_at_a_time,
):
    activities, db, _storage, _provider, model, calls = await collected(
        "current", cut=lambda request, count: count == 1, error=mostly_reasoning
    )
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    collection = await activities._result(run_id, "collection")
    assert collection["triage_stages"][:3] == ["triage:0", "triage:0:retry", "triage:1"]
    assert all(row["buyer_fit"] == label(row["keyword"]) for row in collection["candidates"])
    # The receipt records how the cut-off call spent its tokens and what ran out.
    first = await activities._result(run_id, "triage:0")
    assert first["status"] == "unknown" and first["reason"] == "output_truncated"
    assert first["truncated_by"] == "reasoning"
    assert first["usage"]["output_tokens"] == 8000
    assert first["usage"]["reasoning_tokens"] == 7600
    # The retry keeps low effort and the same answer budget, with 15,000 tokens to reason in.
    sent = triage_requests(model)
    assert [(r.max_output_tokens, r.reasoning_effort) for r in sent[:3]] == [
        (8000, ReasoningEffort.LOW),
        (16000, ReasoningEffort.LOW),
        (8000, ReasoningEffort.LOW),
    ]
    assert replace(sent[1], max_output_tokens=8000) == sent[0]
    assert calls[0][1] == calls[1][1]
    # A completed screening receipt keeps both counts too.
    retry = await activities._result(run_id, "triage:0:retry")
    assert retry["status"] == "completed"
    assert {"output_tokens", "reasoning_tokens"} <= set(retry["value"]["usage"])
    assert await activities._result(run_id, "failure") is None


async def test_a_batch_cut_off_by_reasoning_twice_says_so(one_batch_at_a_time):
    activities, db, *_rest, calls = await collected(
        "current", cut=lambda request, count: True, error=mostly_reasoning
    )
    run_id = str(db.run.id)
    with pytest.raises(ApplicationError, match="stopped at its output limit"):
        await activities.keyword_collect(run_id)
    assert [tokens for tokens, _ in calls] == [8000, 16000]
    failure = await activities._result(run_id, "failure")
    assert failure["code"] == "output_truncated" and failure["truncated_by"] == "reasoning"
    retry = await activities._result(run_id, "triage:0:retry")
    assert retry["truncated_by"] == "reasoning"
    assert retry["usage"]["reasoning_tokens"] == 15600
    await activities.keyword_failure(run_id)
    message = db.project_failure.await_args.kwargs["error_message"]
    assert "output-token limit, most of it spent on reasoning, and so did its retry" in message


async def test_v6_cut_off_screening_keeps_its_request_and_records_its_tokens():
    activities, db, _storage, _provider, model, calls = await collected(
        "v6", cut=lambda request, count: True, error=mostly_reasoning
    )
    run_id = str(db.run.id)
    with pytest.raises(ApplicationError, match="no replacement was purchased"):
        await activities.keyword_collect(run_id)
    (request,) = triage_requests(model)
    assert request.reasoning_effort is None and request.max_output_tokens == 5000
    receipt = await activities._result(run_id, "triage")
    # v6 has no answer budget, so the receipt names no cause, but it keeps the tokens.
    assert "truncated_by" not in receipt
    assert receipt["usage"]["reasoning_tokens"] == 4600


def capturing_openai(response):
    sent = []

    async def create(**kwargs):
        sent.append(kwargs)
        return response

    return SimpleNamespace(responses=SimpleNamespace(create=create)), sent


async def test_the_router_sends_low_effort_on_the_v7_route_and_names_a_reasoning_cut_off():
    """OpenAI's max_output_tokens covers reasoning too. A response that reasons through its
    whole cap reports incomplete and carries no text at all."""
    response = SimpleNamespace(
        output_text="",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        model="gpt-6-luna",
        id="resp_fixture",
        usage=SimpleNamespace(
            input_tokens=7342,
            output_tokens=8000,
            total_tokens=15342,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens_details=SimpleNamespace(reasoning_tokens=8000),
        ),
    )
    client, sent = capturing_openai(response)
    provider = OpenAIModelProvider(api_key="fixture", client=client)  # noqa: S106
    batch = [{"id": "kw_1", "keyword": "one"}]
    request = ModelRequest(
        messages=(ModelMessage(role=MessageRole.USER, content="{}"),),
        output_schema=v2.model_schema("triage", batch),
        output_schema_name="keyword_triage",
        max_output_tokens=8000,
        reasoning_effort=ReasoningEffort(v7.POLICY["triage_reasoning_effort"]),
    )
    router = ModelRouter(providers={ProviderName.OPENAI: provider}, routes=(v7.ROUTE,))
    with pytest.raises(ModelOutputTruncated) as failed:
        await router.generate(v7.ROUTE.key, request)
    assert sent[0]["reasoning"] == {"effort": "low"}
    assert sent[0]["max_output_tokens"] == 8000
    assert model_failure_reason(failed.value) == "output_truncated"
    usage = failed.value.observation.usage
    assert v7.truncation_cause(usage, cap=8000, answer_tokens=1000) == "reasoning"
    # The route pinned before v7 cannot carry an effort, which is why v7 pins a new one.
    old = ModelRoute(
        key=v7.ROUTE.key,
        provider=ProviderName.OPENAI,
        model="gpt-6-luna",
        capabilities=v7.ROUTE.capabilities - {ModelCapability.REASONING_EFFORT},
    )
    with pytest.raises(ModelCapabilityError):
        await ModelRouter(providers={ProviderName.OPENAI: provider}, routes=(old,)).generate(
            old.key, request
        )


# ---------------------------------------------------------------- the provider's signal


def openai_reply(*, text, output_tokens, status="completed", reason=None):
    async def create(**kwargs):
        return SimpleNamespace(
            output_text=text,
            status=status,
            incomplete_details=SimpleNamespace(reason=reason) if reason else None,
            model="gpt-6-luna",
            id="resp_fixture",
            usage=SimpleNamespace(
                input_tokens=7342,
                output_tokens=output_tokens,
                total_tokens=7342 + output_tokens,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=4221),
            ),
        )

    return SimpleNamespace(responses=SimpleNamespace(create=create))


@pytest.mark.parametrize(
    "reply,expected",
    [
        # OpenAI says the response stopped at max_output_tokens (run a59cded8's case).
        (
            dict(
                text='{"k1":"direct","k2":"wro',
                output_tokens=5000,
                status="incomplete",
                reason="max_output_tokens",
            ),
            "output_truncated",
        ),
        # No status, but the JSON stops at the cap.
        (dict(text='{"k1":"direct","k2":"wro', output_tokens=5000), "output_truncated"),
        # Unparseable well below the cap is a bad answer, not a cut-off one.
        (dict(text="not JSON", output_tokens=40), "invalid_result"),
        # Parseable but outside the schema is a bad answer too.
        (dict(text='{"k1":"maybe"}', output_tokens=5000), "invalid_result"),
        # No status and no text at the cap: reasoning used it all before any answer.
        (dict(text="", output_tokens=5000), "output_truncated"),
        # No text well below the cap is an empty answer, not a cut-off one.
        (dict(text="", output_tokens=40), "invalid_result"),
    ],
)
async def test_the_router_names_a_cut_off_answer(reply, expected):
    provider = OpenAIModelProvider(api_key="fixture", client=openai_reply(**reply))  # noqa: S106
    batch = [{"id": "kw_1", "keyword": "one"}]
    request = ModelRequest(
        messages=(ModelMessage(role=MessageRole.USER, content="{}"),),
        output_schema=v2.model_schema("triage", batch),
        output_schema_name="keyword_triage",
        max_output_tokens=5000,
    )
    with pytest.raises(Exception) as failed:
        await provider.generate(model="gpt-6-luna", request=request)
    assert model_failure_reason(failed.value) == expected
    # Usage survives either way, so the call is still charged and reported.
    assert failed.value.observation.usage.output_tokens == reply["output_tokens"]
