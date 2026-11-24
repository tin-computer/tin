"""The model step that rates People against the ICP file, for every run that rates them.

LinkedIn reads rate everyone they found who has no current rating; an RB2B visitor's run rates
that one person. Each person is one model call (people_icp's prompt, web search on), kept in an
effect receipt before any value is written: a call that can't be confirmed isn't bought again,
and an answer that came but can't be used is kept as such under the ICP definition, so it isn't
bought again either. A check on the answer never fails the run that paid for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from uuid import UUID

from tin_lite import people_icp
from tin_lite import people_records as records
from tin_lite.model_providers import (
    MessageRole,
    ModelMessage,
    ModelOutputTruncated,
    ModelProviderError,
    WebSearchRequest,
)
from tin_lite.model_usage import model_usage_scope

logger = logging.getLogger(__name__)


def available(router) -> bool:
    """Whether this deployment can rate: the rating model's provider has a key."""

    return router is not None and people_icp.ROUTE.provider in router.configured_providers


def definition_for(icp: dict) -> str:
    """The ICP definition under the current scorer; ratings made before carry an older one."""

    return icp["definition"].rsplit(":v", 1)[0] + f":v{people_icp.SCORER}"


async def icp_text(storage, project, icp: dict) -> str:
    raw = await storage.read_canonical_artifact_if_exists(
        repo_id=project.state_repo_id, commit_sha=icp["revision"], path=icp["path"]
    )
    return (raw or b"")[: people_icp.ICP_BYTES].decode("utf-8", "replace")


async def answer(db, router, run, person, *, text, profiles, definition, operation, step):
    """The model's answer for one person, or None when no call answered or an earlier call
    can't be confirmed.

    A call that fails, most often because the model searched past its deadline, is asked once
    more with fewer searches and less reasoning, so it can finish in time. A person neither
    call answers keeps no current rating, so a later run tries them again."""

    digest = hashlib.sha256(f"{definition}:{person['id']}".encode()).hexdigest()[:24]
    asked = {
        "db": db,
        "router": router,
        "run": run,
        "person": person,
        "text": text,
        "profiles": profiles,
        "operation": operation,
    }
    output, failed = await _ask(
        **asked,
        key=f"{run.id}:{step}:rate:{digest}",
        usage=f"{step}:icp:{digest}",
        searches=people_icp.SEARCHES,
        reasoning=people_icp.REASONING,
        seconds=people_icp.MODEL_SECONDS,
    )
    if not failed:
        return output
    output, _ = await _ask(
        **asked,
        key=f"{run.id}:{step}:rate:{digest}:again",
        usage=f"{step}:icp:{digest}:again",
        searches=people_icp.RETRY_SEARCHES,
        reasoning=people_icp.RETRY_REASONING,
        seconds=people_icp.RETRY_SECONDS,
    )
    return output


async def _ask(
    *, db, router, run, person, text, profiles, operation, key, usage, searches, reasoning, seconds
) -> tuple[object, bool]:
    """One receipted call: its output, and whether it failed (no usable answer came back). A
    call whose receipt was started but never settled is uncertain: no output, not failed, so
    it isn't bought again."""

    async with db.effect_lock(key, operation) as (conn, saved):
        if saved and saved.status == "completed":
            output = (saved.result or {}).get("output")
            return output, output is None
        if saved:
            return None, False
        await db.start_effect(conn, execution_key=key, operation=operation)
        output = None
        try:
            with model_usage_scope(run_id=run.id, step=usage, conn=conn):
                async with asyncio.timeout(seconds + 15):
                    result = await router.generate(
                        people_icp.ROUTE.key,
                        WebSearchRequest(
                            system=people_icp.instructions(profiles),
                            messages=(
                                ModelMessage(
                                    role=MessageRole.USER,
                                    content=people_icp.person_input(text, person),
                                ),
                            ),
                            output_schema=people_icp.schema(profiles),
                            output_schema_name="icp_rating",
                            max_output_tokens=people_icp.MAX_OUTPUT_TOKENS,
                            reasoning_effort=reasoning,
                            web_search_calls=searches,
                        ),
                        timeout_seconds=seconds,
                    )
            output = result.parsed
        except (ModelProviderError, ModelOutputTruncated, TimeoutError) as error:
            # A check on model output never fails the run that paid for it.
            logger.warning("ICP rating failed for run %s: %s", run.id, type(error))
        await db.complete_effect(conn, execution_key=key, result={"output": output})
        return output, output is None


async def rate(db, router, run, person, *, text, profiles, definition, operation, step) -> None:
    """Rate one person and write their icp, icp_fit and intent."""

    output = await answer(
        db,
        router,
        run,
        person,
        text=text,
        profiles=profiles,
        definition=definition,
        operation=operation,
        step=step,
    )
    values = people_icp.values(output, profiles) if output is not None else None
    # An answer that came but can't be used is kept as such under this definition, so it isn't
    # bought again; a call that didn't answer leaves them for a later run.
    current = output is not None
    async with db.pool.acquire() as conn:
        try:
            await records.set_values(
                conn,
                run.project_id,
                person["id"],
                values or people_icp.unscored_values(),
                set_by_kind="run",
                set_by=str(run.id),
                definition=definition if current else "",
                refresh=current,
            )
        except records.RecordError as error:
            # The founder forgot this person while the model rated them.
            if error.code != "record_gone":
                raise


async def rate_unrated(
    db,
    router,
    run,
    *,
    text: str,
    profiles: list[str],
    definition: str,
    operation: str,
    step: str,
    only: UUID | None = None,
    heartbeat: Callable[[dict], None] = lambda detail: None,
) -> int:
    """Rate everyone (or `only` that person) without a current rating, a few at once, until
    nobody is left, the run's guard is reached or its time is up. Returns how many it tried.

    A new person starts as soon as one finishes, so one slow rating doesn't hold up the rest."""

    tried: list[UUID] = []
    loop = asyncio.get_running_loop()
    stop = loop.time() + people_icp.SCORE_SECONDS
    running: set[asyncio.Task] = set()
    failed: BaseException | None = None
    exhausted = False

    async def beat() -> None:
        # A rating that searches can take minutes; the heartbeat says the activity lives.
        while True:
            heartbeat({"rated": len(tried)})
            await asyncio.sleep(30)

    beating = asyncio.create_task(beat())
    try:
        while True:
            room = min(people_icp.PARALLEL - len(running), people_icp.MAX_SCORED - len(tried))
            if room > 0 and not (exhausted or failed) and loop.time() < stop:
                async with db.pool.acquire() as conn:
                    batch = await people_icp.unscored(
                        conn, run.project_id, definition, limit=room, skip=tried, only=only
                    )
                exhausted = len(batch) < room
                tried += [person["id"] for person in batch]
                running |= {
                    asyncio.create_task(
                        rate(
                            db,
                            router,
                            run,
                            person,
                            text=text,
                            profiles=profiles,
                            definition=definition,
                            operation=operation,
                            step=step,
                        )
                    )
                    for person in batch
                }
            if not running:
                break
            done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                # Ratings already under way finish (they're paid for); nothing new starts.
                if task.exception() is not None and failed is None:
                    failed = task.exception()
    finally:
        beating.cancel()
    if failed is not None:
        raise failed
    heartbeat({"rated": len(tried)})
    return len(tried)
