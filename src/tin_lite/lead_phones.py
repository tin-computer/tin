"""Phone numbers for a project's good and strong leads, from ContactOut through Delegate.

Anyone rated a good or strong fit (`people_icp.FIT_SHOWN` or higher) who did something Tin saw
(a visit, a reaction, a comment, a profile view or a star), with a LinkedIn profile and no phone
yet, gets one lookup: Delegate's `contactout-linkedin-phone`, which returns the numbers
ContactOut has on file for the profile and isn't billed when it has none. Someone known only
from a bulk import of connections never does. The numbers land in the person's `phone` value,
shown in People's panel; a member's own phone is never replaced and customers are left out.

Each person is looked up once, ever: the lookup's receipt is keyed by project and person, so a
retry, a later run or another flow finds it and pays nothing. The receipt holds the order and
its price, never the numbers, so forgetting a person removes them. Tin pays, under a daily
guard per project well above a day's leads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from uuid import UUID

from tin_lite import people_icp
from tin_lite import people_records as records
from tin_lite.delegate import DelegateError

logger = logging.getLogger(__name__)

SERVICE = "contactout-linkedin-phone"
OPERATION = "people.phones"
# The quote is $0.15; the cap leaves room for a small price change, nothing more.
MAX_USD = 0.20
# What one project's lookups may order at Delegate in a day: a runaway guard, about 130 leads.
USD_PER_DAY = 20.0
# How long one lookup may take before Tin stops waiting (they take seconds to a minute).
JOB_SECONDS = 3 * 60
# Lookups at once.
PARALLEL = 5
# Numbers kept per person, as ContactOut lists them.
NUMBERS = 10
PHONE = re.compile(r"\+?\(?[0-9][0-9 ().-]{5,24}[0-9]")
# Delegate statuses that are its answer about the input itself: the lookup is done. Anything
# else (a 5xx, out of funds, a changed price, no answer) leaves it for a later run.
DEFINITE = frozenset({400, 404, 422})
# Refusals that come before Delegate creates an order: an empty balance (402), a conflict or
# rate limit (409, 429), and Tin's own checks before ordering.
BEFORE_ORDER_STATUSES = frozenset({402, 409, 429})
BEFORE_ORDER = frozenset({"price_above_max", "missing_scope", "offer_selection"})


def key(project_id: UUID, record_id: UUID) -> str:
    return f"phones:{project_id}:{record_id}"


def numbers(result: dict | None) -> list[str]:
    """The numbers in Delegate's answer, cleaned and deduplicated; anything else is dropped."""

    found = ((result or {}).get("artifacts") or {}).get("phones")
    kept: list[str] = []
    for value in found if isinstance(found, list) else []:
        text = " ".join(str(value).split()) if isinstance(value, str | int) else ""
        digits = re.sub(r"\D", "", text)
        if PHONE.fullmatch(text) and 7 <= len(digits) <= 15:
            if digits not in {re.sub(r"\D", "", known) for known in kept}:
                kept.append(text)
    return kept[:NUMBERS]


def reason(found: list[str]) -> str:
    said = f"ContactOut has {len(found)} number{'s' if len(found) != 1 else ''} on file"
    return said + ("; it doesn't say which is mobile or still current." if len(found) > 1 else ".")


async def wanted(
    conn, project_id: UUID, *, limit: int, only: UUID | None = None, skip: list[UUID] = ()
) -> list[dict]:
    """Good and strong leads with a LinkedIn profile (its public address, else LinkedIn's member
    ID), no phone and no finished lookup, best first."""

    rows = await conn.fetch(
        """SELECT r.id,
               (SELECT jsonb_object_agg(i.kind, i.value) FROM record_identifiers i
                WHERE i.record_id = r.id AND i.kind IN ('linkedin_slug', 'linkedin_member'))
                   AS identifiers
           FROM records r
           JOIN record_values f ON f.record_id = r.id AND f.field = 'icp_fit'
           WHERE r.project_id = $1
             AND EXISTS (SELECT 1 FROM record_identifiers i WHERE i.record_id = r.id
                         AND i.kind IN ('linkedin_slug', 'linkedin_member'))
             -- A lead did something Tin saw: a visit, a reaction, a comment, a profile view or
             -- a star. Someone known only from a bulk import of connections, however rated,
             -- gets no lookup.
             AND EXISTS (SELECT 1 FROM record_events e WHERE e.project_id = $1
                         AND (e.record_id = r.id OR e.record_id IN
                              (SELECT m.id FROM records m WHERE m.merged_into = r.id))
                         AND e.kind = ANY($6::text[]))
             AND r.merged_into IS NULL AND f.value_number >= $2
             AND ($3::uuid IS NULL OR r.id = $3)
             AND NOT (r.id = ANY($5::uuid[]))
             AND NOT EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                             AND v.field = 'phone' AND v.value_text IS NOT NULL)
             AND NOT EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                             AND v.field = 'customer' AND v.value_text = 'yes')
             AND NOT EXISTS (SELECT 1 FROM effect_receipts e
                             WHERE e.execution_key = 'phones:' || $1::text || ':' || r.id::text
                               AND e.status = 'completed')
           ORDER BY f.value_number DESC, r.created_at, r.id
           LIMIT $4""",
        project_id,
        people_icp.FIT_SHOWN,
        only,
        limit,
        list(skip),
        list(records.ACTIVE),
    )
    people = []
    for row in rows:
        identifiers = row["identifiers"]
        identifiers = json.loads(identifiers) if isinstance(identifiers, str) else identifiers
        url = records.profile_link(identifiers or {})
        if url:
            people.append({"id": row["id"], "url": url})
    return people


async def spent_today(conn, project_id: UUID) -> float:
    """What this project's lookups ordered at Delegate in the last day, from their receipts."""

    return float(
        await conn.fetchval(
            """SELECT COALESCE(sum((result->>'price_usd')::numeric), 0) FROM effect_receipts
               WHERE execution_key LIKE 'phones:' || $1 || ':%'
                 AND created_at > now() - interval '1 day' AND result ? 'price_usd'""",
            str(project_id),
        )
    )


async def _reserve(db, conn, project_id: UUID, execution_key: str) -> bool:
    """Reserve one lookup's most under the daily guard, in one transaction under a per-project
    lock, so lookups running at once count each other."""

    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"phones-spend:{project_id}"
        )
        if await spent_today(conn, project_id) + MAX_USD > USD_PER_DAY:
            return False
        await db.save_effect_progress(
            conn, execution_key=execution_key, result={"price_usd": MAX_USD, "reserved": True}
        )
        return True


async def look_up(db, delegate, project_id: UUID, person: dict, *, run_id: UUID) -> str:
    """One person's lookup under its receipt; what happened: found or none (done), failed
    (Delegate refused the input: done), held (the day's guard) or later (Delegate didn't
    answer: left open for a later run). Never raises a Delegate error: a phone lookup is
    extra, and never fails the run around it."""

    execution_key = key(project_id, person["id"])
    async with db.effect_lock(execution_key, OPERATION) as (conn, saved):
        if saved and saved.status == "completed":
            return str((saved.result or {}).get("outcome") or "none")
        placed = (saved.result or {}) if saved else {}
        if saved is None:
            await db.start_effect(conn, execution_key=execution_key, operation=OPERATION)
        if not placed.get("conversation_id"):
            if not placed.get("reserved") and not await _reserve(
                db, conn, project_id, execution_key
            ):
                # Over the day's guard: nothing is ordered, and the receipt is left open so a
                # later run looks them up.
                await db.save_effect_progress(conn, execution_key=execution_key, result={})
                return "held"
            try:
                # The receipt's key is the order's idempotency key: placing it again after a
                # crash or a missing answer returns the same order.
                placed = await delegate.order(
                    SERVICE,
                    {"linkedin_url": person["url"]},
                    max_usd=MAX_USD,
                    idempotency_key=execution_key,
                )
            except DelegateError as error:
                if error.status not in DEFINITE:
                    # No answer, or one that may change (funds, price, a 5xx): the receipt
                    # stays open and a later run asks again. A refusal known to come before
                    # any order exists (funds, a changed price, no offer to buy) bought nothing
                    # and drops its reservation; anything else (no answer, a 5xx, an answer
                    # Tin can't read) may hide a placed order, so it keeps counting.
                    if error.status in BEFORE_ORDER_STATUSES or error.code in BEFORE_ORDER:
                        await db.save_effect_progress(conn, execution_key=execution_key, result={})
                    return "later"
                await db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"outcome": "failed", "price_usd": 0, "error": error.code},
                )
                return "failed"
            await db.save_effect_progress(conn, execution_key=execution_key, result=placed)
    view = {"state": "running"}
    deadline = asyncio.get_running_loop().time() + JOB_SECONDS
    try:
        while view["state"] == "running" and asyncio.get_running_loop().time() < deadline:
            view = await delegate.result(placed["conversation_id"], wait=30)
    except DelegateError:
        # The order is placed and saved on the receipt; a later run reads its answer.
        return "later"
    found = numbers(view.get("result")) if view["state"] == "delivered" else []
    async with db.effect_lock(execution_key, OPERATION) as (conn, saved):
        if saved and saved.status == "completed":
            return str((saved.result or {}).get("outcome") or "none")
        if found:
            try:
                await records.ensure_fields(conn, project_id)
                await records.set_values(
                    conn,
                    project_id,
                    person["id"],
                    [records.Value("phone", ", ".join(found), reason(found))],
                    set_by_kind="run",
                    set_by=str(run_id),
                )
            except records.RecordError as error:
                # The founder forgot this person while the lookup ran.
                if error.code != "record_gone":
                    raise
        outcome = "found" if found else ("none" if view["state"] == "delivered" else "failed")
        await db.complete_effect(
            conn,
            execution_key=execution_key,
            result={
                "outcome": outcome,
                "conversation_id": placed["conversation_id"],
                "state": view["state"],
                # A profile with no number on file isn't billed, nor is a failed job; one still
                # running when Tin stopped waiting may be, so it counts.
                "price_usd": float(placed["price_usd"])
                if found or view["state"] == "running"
                else 0.0,
                "count": len(found),
            },
        )
        return outcome


async def look_up_wanted(
    db,
    delegate,
    project_id: UUID,
    *,
    run_id: UUID,
    seconds: float,
    only: UUID | None = None,
    heartbeat: Callable[[dict], None] = lambda detail: None,
) -> dict:
    """Look up everyone wanted (or `only` that person), a few at once, until nobody is left, the
    day's guard holds or `seconds` are up. Returns counts."""

    counts = {"looked_up": 0, "found": 0, "left": 0, "held": False, "later": 0}
    if delegate is None:
        return counts
    loop = asyncio.get_running_loop()
    stop = loop.time() + seconds
    tried: list[UUID] = []
    while loop.time() < stop and not counts["held"]:
        async with db.pool.acquire() as conn:
            batch = await wanted(conn, project_id, limit=PARALLEL, only=only, skip=tried)
        if not batch:
            break
        tried += [person["id"] for person in batch]

        async def beat() -> None:
            # A lookup can take minutes; the heartbeat says the activity lives.
            while True:
                heartbeat({"phones": counts["looked_up"]})
                await asyncio.sleep(30)

        beating = asyncio.create_task(beat())
        try:
            outcomes = await asyncio.gather(
                *(look_up(db, delegate, project_id, person, run_id=run_id) for person in batch),
                return_exceptions=True,
            )
        finally:
            beating.cancel()
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                # Never the run's failure: the person is looked up by a later run.
                logger.warning("Phone lookup ended without an answer: %s", type(outcome).__name__)
        outcomes = ["later" if isinstance(o, BaseException) else o for o in outcomes]
        counts["looked_up"] += sum(1 for o in outcomes if o in ("found", "none", "failed"))
        counts["found"] += outcomes.count("found")
        counts["later"] += outcomes.count("later")
        counts["held"] = "held" in outcomes
        if all(outcome == "later" for outcome in outcomes):
            break  # Delegate isn't answering; a later run goes on
        heartbeat({"phones": counts["looked_up"]})
    async with db.pool.acquire() as conn:
        counts["left"] = len(await wanted(conn, project_id, limit=1000, only=only))
    return counts
