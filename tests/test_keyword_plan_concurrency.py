"""Concurrent keyword lookups buy the same calls, receipts and order as one-by-one lookups."""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_keyword_plan import fixture, provider_row

from tin_lite import keyword_plan_activities
from tin_lite.domain import RunStatus
from tin_lite.keyword_data import KeywordData
from tin_lite.keyword_plan_activities import LOOKUP_CONCURRENCY, KeywordPlanActivities

RUN_ID = UUID("7d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
SEEDS = ["team scheduling", "booking api", "calendar sync", "consulting scheduling software"]


def track(provider, *, delays=None, serp_delay=0.02):
    """Make every lookup return keyword-specific rows, and record overlap and start order."""
    state = {"in_flight": 0, "peak": 0, "serp_peak": 0, "started": []}

    async def query(kind, *, market, value, tag, **kwargs):
        stage = tag.split(":", 2)[2]
        state["started"].append(stage)
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        if kind == "serp":
            state["serp_peak"] = max(state["serp_peak"], state["in_flight"])
        try:
            await asyncio.sleep(serp_delay if kind == "serp" else (delays or {}).get(stage, 0.01))
        finally:
            state["in_flight"] -= 1
        if kind == "competitors":
            items = [{"domain": "one.example"}, {"domain": "two.example"}]
        elif kind == "serp":
            items = [
                {
                    "type": "organic",
                    "rank_group": 1,
                    "url": f"https://competitor.example/{abs(hash(value)) % 1000}",
                    "title": value,
                    "description": "A comparison.",
                }
            ]
        else:
            items = [provider_row(f"{stage} query {index}") for index in range(3)]
        return {
            "items": items,
            "items_count": len(items),
            "total_count": len(items),
            "reported_cost_usd": "0.002" if kind == "serp" else "0.02",
            "provider_task_id": "fixture-only",
        }

    provider.query.side_effect = query
    return state


async def collected(*, budget=10, delays=None, inputs=None, modern="current"):
    activities, db, storage, provider, model = await fixture(
        prepare=False,
        budget=budget,
        modern=modern,
        inputs={"seed_phrases": SEEDS, **(inputs or {})},
    )
    db.run = replace(db.run, id=RUN_ID)
    state = track(provider, delays=delays)
    await activities.keyword_prepare(str(RUN_ID))
    await activities.keyword_collect(str(RUN_ID))
    return activities, db, provider, model, state


def untimed(value):
    """Drop observation clocks, which differ between any two runs."""
    if isinstance(value, dict):
        return {k: untimed(v) for k, v in value.items() if k not in {"observed_at", "attempted_at"}}
    if isinstance(value, list | tuple):
        return type(value)(untimed(item) for item in value)
    return value


def serp_receipts(db, activities, count):
    receipts = {}
    for index in range(count):
        receipt = db.effects.get(activities.key(str(RUN_ID), f"serp:{index}"))
        receipts[index] = untimed(receipt.result) if receipt else None
    return receipts


async def inspect_both_ways(**options):
    """Inspect the same collection one by one and in one batch, from the same saved state."""
    activities, db, provider, _, state = await collected(**options)
    run_id = str(RUN_ID)
    count = await activities.keyword_sample_count(run_id)
    before = deepcopy(db.effects)
    for index in range(count):
        await activities.keyword_inspect({"run_id": run_id, "index": index})
    one_by_one = (
        serp_receipts(db, activities, count),
        untimed(
            await activities._samples(
                run_id, db.effects[activities.key(run_id, "collection")].result["candidates"]
            )
        ),
        dict(db.effects[activities.key(run_id, "budget")].result),
    )
    db.effects = before
    state["peak"] = state["serp_peak"] = 0
    started = time.perf_counter()
    await activities.keyword_inspect_batch(run_id)
    elapsed = time.perf_counter() - started
    batch = (
        serp_receipts(db, activities, count),
        untimed(
            await activities._samples(
                run_id, db.effects[activities.key(run_id, "collection")].result["candidates"]
            )
        ),
        dict(db.effects[activities.key(run_id, "budget")].result),
    )
    return count, one_by_one, batch, state, elapsed


@pytest.mark.asyncio
async def test_batch_inspection_keeps_receipts_fingerprints_and_sample_order():
    count, one_by_one, batch, state, elapsed = await inspect_both_ways()
    assert count > 2 * LOOKUP_CONCURRENCY
    # Same request fingerprints, provider tags, reservations and results per sample index.
    assert batch[0] == one_by_one[0]
    assert all(receipt["status"] == "completed" for receipt in batch[0].values())
    # _samples feeds the review in candidate order; the batch changes neither keys nor order.
    assert list(batch[1][0]) == list(one_by_one[1][0]) and batch[1] == one_by_one[1]
    assert batch[2] == one_by_one[2]
    # The samples overlap, up to the lookup bound, instead of one activity per sample.
    assert state["serp_peak"] == LOOKUP_CONCURRENCY
    assert elapsed < count * 0.02 / 2


@pytest.mark.asyncio
async def test_binding_ceiling_refuses_the_same_samples_in_a_batch():
    # v6 reserves at most $1.65 under a $2 floor, so only an older policy's ceiling binds.
    # Find what collection reserves, then leave room for exactly three search-result samples.
    activities, db, *_ = await collected(modern="v5")
    ledger = db.effects[activities.key(str(RUN_ID), "budget")].result
    serp = activities._policy(await activities._result(str(RUN_ID), "scope"))[
        "serp_reservation_usd"
    ]
    budget = sum(Decimal(value) for value in ledger.values()) + 3 * Decimal(serp)
    count, one_by_one, batch, _, _ = await inspect_both_ways(budget=budget, modern="v5")
    refused = {index for index, receipt in batch[0].items() if receipt["status"] != "completed"}
    assert all(batch[0][index]["reason"] == "spending_limit" for index in refused)
    assert refused == set(range(3, count))
    assert batch[0] == one_by_one[0] and batch[2] == one_by_one[2]


@pytest.mark.asyncio
async def test_collection_runs_lookups_side_by_side_and_keeps_source_order():
    # Later sources answer first; the saved sources and candidates must not notice.
    names = ["competitor:0", "competitor:1", "seed_metrics"] + [
        f"{kind}:{index}" for index in range(len(SEEDS)) for kind in ("suggestions", "related")
    ]
    reversed_delays = {name: 0.002 * (len(names) - position) for position, name in enumerate(names)}
    _, db, _, _, state = await collected(delays=reversed_delays)
    parallel = db.effects[f"keyword:{RUN_ID}:collection"].result
    assert list(parallel["sources"]) == [
        "target",
        "competitors",
        *names,
        "gsc",
        "seed_proposals",
    ]
    assert state["peak"] == LOOKUP_CONCURRENCY

    original = KeywordPlanActivities._bounded

    async def one_at_a_time(calls, limit=LOOKUP_CONCURRENCY):
        return await original(calls, 1)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(KeywordPlanActivities, "_bounded", staticmethod(one_at_a_time))
        _, sequential_db, _, _, sequential_state = await collected()
    sequential = sequential_db.effects[f"keyword:{RUN_ID}:collection"].result
    assert sequential_state["peak"] == 1
    assert untimed(parallel["candidates"]) == untimed(sequential["candidates"])
    assert parallel["coverage"] == sequential["coverage"]
    assert list(parallel["sources"]) == list(sequential["sources"])


@pytest.mark.asyncio
async def test_seed_proposal_overlaps_lookups_that_do_not_need_seeds():
    activities, db, storage, provider, model = await fixture(
        prepare=False, modern="current", inputs={"seed_phrases": []}
    )
    db.run = replace(db.run, id=RUN_ID)
    state = track(provider)
    generate = model.generate.side_effect
    events = []

    async def slow_seeds(route, request, *, timeout_seconds=None):
        if request.output_schema_name == "keyword_seeds":
            events.append(("seeds", "started", list(state["started"])))
            await asyncio.sleep(0.05)
            events.append(("seeds", "finished", list(state["started"])))
        return await generate(route, request, timeout_seconds=timeout_seconds)

    model.generate.side_effect = slow_seeds
    await activities.keyword_prepare(str(RUN_ID))
    await activities.keyword_collect(str(RUN_ID))
    (_, _, at_start), (_, _, at_finish) = events
    assert at_start == []
    # Target footprint and competitor discovery were bought during the seed proposal; nothing
    # that needs seeds was.
    assert set(at_finish) == {"target", "competitors"}
    ledger = list(db.effects[activities.key(str(RUN_ID), "budget")].result)
    assert ledger[:5] == ["review", "triage", "seeds", "target", "competitors"]


@pytest.mark.asyncio
async def test_refused_seed_proposal_buys_no_lookup():
    activities, db, storage, provider, model = await fixture(
        prepare=False, modern="current", inputs={"seed_phrases": []}
    )
    await activities.keyword_prepare(str(db.run.id))
    ledger = db.effects[activities.key(str(db.run.id), "budget")].result
    scope = await activities._result(str(db.run.id), "scope")
    scope["max_cost_usd"] = str(sum(Decimal(value) for value in ledger.values()))
    with pytest.raises(Exception, match="no replacement"):
        await activities.keyword_collect(str(db.run.id))
    assert provider.query.await_count == model.generate.await_count == 0


@pytest.mark.asyncio
async def test_paid_lookup_reserves_on_the_connection_it_already_holds():
    activities, db, provider, _, _ = await collected()
    locks = []
    original = db.effect_lock

    def recording(key, operation, *, conn=None):
        locks.append((key.rsplit(":", 1)[-1], conn))
        return original(key, operation, conn=conn)

    db.effect_lock = recording
    await activities._query(str(RUN_ID), "serp:extra", "serp", "team scheduling")
    assert ("budget", db) in locks


@pytest.mark.asyncio
async def test_keyword_data_session_shares_one_client():
    opened = []

    def handler(request):
        (sent,) = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "status_code": 20000,
                        "cost": 0.002,
                        "id": "task",
                        "data": sent,
                        "result": [{**sent, "items": []}],
                    }
                ],
            },
        )

    provider = KeywordData("login", "password", transport=httpx.MockTransport(handler))
    original = provider._client

    def counting():
        opened.append(1)
        return original()

    provider._client = counting
    async with provider.session():
        await asyncio.gather(
            *(
                provider.query("serp", market="US", value="team scheduling", tag=f"t{index}")
                for index in range(3)
            )
        )
    assert len(opened) == 1
    await provider.query("serp", market="US", value="team scheduling", tag="alone")
    assert len(opened) == 2


def test_lookup_bound_fits_the_database_pool():
    # Each in-flight lookup holds one connection for its receipt lock; the pool allows ten.
    assert 1 < keyword_plan_activities.LOOKUP_CONCURRENCY <= 4


@pytest.mark.asyncio
async def test_stop_during_a_batch_starts_no_further_paid_sample():
    activities, db, provider, _, state = await collected()
    count = await activities.keyword_sample_count(str(RUN_ID))
    query = provider.query.side_effect
    served = []

    async def stop_after_first(kind, **kwargs):
        result = await query(kind, **kwargs)
        if kind == "serp":
            served.append(kwargs["tag"])
            db.run = replace(db.run, status=RunStatus.STOPPED)
        return result

    provider.query.side_effect = stop_after_first
    with pytest.raises(ApplicationError, match="not active"):
        await activities.keyword_inspect_batch(str(RUN_ID))
    # Only the samples already in flight when the stop landed were bought.
    assert 1 <= len(served) <= LOOKUP_CONCURRENCY < count
