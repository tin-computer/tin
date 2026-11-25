"""One more judge after a temporary provider failure (intel_judge.retry_child), never more, and
never for a failure another child would meet again. The first attempt keeps its receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from disposable_db import migrated_database
from intel_fixtures import fetch, project, run

from tin_lite import intel_judge, intel_store, run_service
from tin_lite.codex_api import ATTEMPT

WORLD = intel_judge.WORLD_JUDGE_KEY


@pytest.fixture
async def db():
    async with migrated_database(max_size=5) as database:
        yield database


@dataclass(frozen=True)
class Template:
    key: str
    executor: str = "codex.procedure"
    definition: dict = field(default_factory=dict)


class Storage:
    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        return json.dumps(
            {
                "key": WORLD,
                "executor": "codex.procedure",
                "input_schema": intel_judge.JUDGE_INPUT_SCHEMA,
            }
        ).encode()


@pytest.fixture
async def world(db, monkeypatch):
    """A running world parent with its fetch and a first judge attempt that failed."""
    host = await project(db)
    parent = await run(db, host, "intel.world")
    await fetch(db, parent)
    started: list[str] = []

    async def registry(key):
        return Template(key)

    async def start_workflow_run(**kwargs):
        started.append(kwargs["start_idempotency_key"])
        child = await run(
            db,
            host,
            WORLD,
            inputs={k: v for k, v in kwargs["input_payload"].items() if k != "project_id"},
        )
        return SimpleNamespace(
            id=child, executor="codex.procedure", temporal_workflow_id=f"w{child}"
        )

    monkeypatch.setattr(db, "get_registry_workflow", registry)
    monkeypatch.setattr(run_service, "start_workflow_run", start_workflow_run)
    acts = SimpleNamespace(db=db, storage=Storage(), settings=None, integrations=None)
    parent_run = await db.get_run(parent)
    first = await intel_judge.start_child(acts, parent_run, key=WORLD, scope="world")
    return SimpleNamespace(
        acts=acts, run=parent_run, first=UUID(first["run_id"]), started=started, host=host
    )


async def fail(db, child: UUID, *, kind="explained", detail="CodexAttemptStopped: x") -> None:
    await db.pool.execute(
        "UPDATE workflow_runs SET status='failed', error_kind=$2, error_message='It stopped.', "
        "error_detail=$3 WHERE id=$1",
        child,
        kind,
        detail,
    )


async def attempt_receipt(db, child: UUID, **result) -> None:
    """The controller's attempt receipt as the relay leaves it: started, so still uncertain."""
    await db.pool.execute(
        """INSERT INTO effect_receipts (execution_key, operation, status, result)
           VALUES ($1, $2, 'started', $3::jsonb)""",
        f"{child}:codex-api:{uuid4().hex}",
        ATTEMPT,
        json.dumps({"run_id": str(child), "outcome": "failed", **result}),
    )


def rejection(reason: str, status: int = 502, upstream: int | None = None) -> dict:
    facts = {"reason": reason, "status": status, "operation": "other"}
    if upstream is not None:
        facts["upstream_status"] = upstream
    return {"relay_rejection": facts}


@pytest.mark.parametrize(
    "result, kind, detail, expected",
    [
        (rejection("stream_unconfirmed"), "explained", "x", "stream_unconfirmed"),
        (rejection("upstream_unreachable"), "explained", "x", "upstream_unreachable"),
        (rejection("relay_timeout", 504), "explained", "x", "relay_timeout"),
        (rejection("upstream_rejected", 502, 503), "explained", "x", "upstream_rejected"),
        (rejection("rate_limited", 429, 429), "explained", "x", "rate_limited"),
        (rejection("upstream_rejected", 502, 400), "explained", "x", None),
        (rejection("parallel_limit", 409), "explained", "x", None),
        (rejection("context_bound", 413), "explained", "x", None),
        ({"failure_type": "ConnectionResetError"}, "explained", "x", "network"),
        ({"failure_type": "CommandExitException"}, "explained", "x", None),
        ({}, "transient", "x", "transient"),
        ({}, "needs_you", "x", None),
        # Staging 2026-11-25: a timeout E2B refuses is Tin's own error, not the provider's.
        ({}, "internal", "SandboxException: 400: Timeout cannot be greater than 1 hours", None),
        ({}, "internal", "SandboxException: 502: Bad Gateway", "sandbox"),
    ],
)
async def test_only_a_temporary_provider_failure_is_transient(
    db, world, result, kind, detail, expected
):
    await fail(db, world.first, kind=kind, detail=detail)
    if result:
        await attempt_receipt(db, world.first, **result)
    child = await db.get_run(world.first)
    assert await intel_judge.transient_cause(db, child) == expected


async def test_a_running_or_succeeded_judge_is_never_retried(db, world):
    assert await intel_judge.transient_cause(db, await db.get_run(world.first)) is None
    await db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", world.first)
    assert await intel_judge.retry_child(world.acts, world.run, key=WORLD, scope="world") == {}


async def test_a_transient_failure_starts_one_more_judge_once(db, world):
    await fail(db, world.first)
    await attempt_receipt(db, world.first, **rejection("stream_unconfirmed"))
    before = await db.pool.fetch(
        "SELECT execution_key, status, result FROM effect_receipts WHERE result->>'run_id'=$1",
        str(world.first),
    )
    notes: list[str] = []

    async def progress(note):
        notes.append(note)

    again = await intel_judge.retry_child(
        world.acts, world.run, key=WORLD, scope="world", progress=progress
    )
    # The history holds identifiers and the attempt number only, never the cause.
    assert set(again) == {"run_id", "executor", "temporal_workflow_id", "attempt"}
    assert again["attempt"] == 2 and UUID(again["run_id"]) != world.first
    assert notes == [intel_judge.RETRY_NOTE]
    parent = world.run.id
    assert world.started == [f"intel:{parent}:judge", f"intel:{parent}:judge:2"]
    saved = await intel_judge.saved_child(db, parent, 2)
    assert saved["retry_of"] == str(world.first) and saved["cause"] == "stream_unconfirmed"
    # An activity retry returns the same child and buys nothing more.
    assert await intel_judge.retry_child(world.acts, world.run, key=WORLD, scope="world") == again
    assert len(world.started) == 2
    # The first attempt's receipts are untouched: an uncertain attempt stays uncertain.
    after = await db.pool.fetch(
        "SELECT execution_key, status, result FROM effect_receipts WHERE result->>'run_id'=$1",
        str(world.first),
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert (await db.get_run(world.first)).status.value == "failed"


async def test_a_non_transient_failure_is_not_retried(db, world):
    await fail(db, world.first)
    await attempt_receipt(db, world.first, **rejection("context_bound", 413))
    assert await intel_judge.retry_child(world.acts, world.run, key=WORLD, scope="world") == {}
    assert len(world.started) == 1


async def test_there_is_no_third_attempt(db, world):
    with pytest.raises(ValueError, match="at most 2 attempts"):
        await intel_judge.start_child(world.acts, world.run, key=WORLD, scope="world", attempt=3)
    assert intel_judge.start_keys(world.run.id) == {
        f"intel:{world.run.id}:judge",
        f"intel:{world.run.id}:judge:2",
    }


async def test_collect_reads_the_retry_and_keeps_the_first_attempts_work(db, world):
    await fail(db, world.first)
    await attempt_receipt(db, world.first, **rejection("stream_unconfirmed"))
    day = (await intel_store.get_fetch(db.pool, world.run.id))["day"]
    async with db.pool.acquire() as conn:
        await intel_store.put_story(
            conn,
            project_id=None,
            key="first-attempt-story",
            day=day,
            run_id=world.first,
            title="Written before the provider failed",
            summary="It stays.",
            relevance="high",
            tags=["ai"],
            sensitive=False,
            continues_id=None,
        )
        await intel_store.put_brief(
            conn,
            run_id=world.first,
            parent_run_id=world.run.id,
            project_id=None,
            day=day,
            schema="intel-world-brief/1",
            brief={"headline": "H"},
            source="judge",
        )
    again = await intel_judge.retry_child(world.acts, world.run, key=WORLD, scope="world")
    second = UUID(again["run_id"])
    # The second attempt reads the first attempt's story as one of today's stories.
    assert [
        s["key"] for s in await intel_store.today_stories(db.pool, project_id=None, day=day)
    ] == ["first-attempt-story"]
    found = await intel_judge.collect(world.acts, world.run)
    assert found["child_run_id"] == str(second) and found["attempts"] == 2
    assert found["stories"] == 1 and found["brief"] is True
    # A second transient failure ends with the second attempt's own reason.
    await db.pool.execute(
        "UPDATE workflow_runs SET status='failed', error_message='Google failed again.' "
        "WHERE id=$1",
        second,
    )
    found = await intel_judge.collect(world.acts, world.run)
    assert found["child_status"] == "failed" and found["reason"] == "Google failed again."
