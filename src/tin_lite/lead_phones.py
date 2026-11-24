"""Phone numbers for a project's good and strong leads, from ContactOut through Delegate.

Anyone rated a good or strong fit (`people_icp.FIT_SHOWN` or higher) with a LinkedIn profile
and no phone yet gets one lookup: Delegate's `contactout-linkedin-phone`, which returns the
numbers ContactOut has on file for the profile and isn't billed when it has none. The numbers
land in the person's `phone` value, shown in People's panel; a member's own phone is never
replaced and customers are left out.

Each person is looked up once, ever: the lookup's receipt is keyed by project and person, so a
retry, a later run or another flow finds it and pays nothing. The receipt holds the order and
its price, never the numbers, so forgetting a person removes them. Tin pays, under a daily
guard per project well above a day's leads.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from uuid import UUID

from tin_lite import people_icp
from tin_lite import people_records as records
from tin_lite.delegate import DelegateError

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
SLUG = re.compile(r"[A-Za-z0-9\-_%.]{2,200}")


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
    """Good and strong leads with a LinkedIn profile, no phone and no lookup yet, best first."""

    rows = await conn.fetch(
        """SELECT r.id, i.value AS slug
           FROM records r
           JOIN record_values f ON f.record_id = r.id AND f.field = 'icp_fit'
           JOIN record_identifiers i ON i.record_id = r.id AND i.kind = 'linkedin_slug'
           WHERE r.project_id = $1 AND r.merged_into IS NULL AND f.value_number >= $2
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
    )
    seen: set[UUID] = set()
    people = []
    for row in rows:
        if row["id"] not in seen and SLUG.fullmatch(row["slug"] or ""):
            seen.add(row["id"])
            people.append({"id": row["id"], "slug": row["slug"]})
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
    """One person's lookup under its receipt; what happened: found, none, held or failed."""

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
                # crash returns the same order.
                placed = await delegate.order(
                    SERVICE,
                    {"linkedin_url": f"https://www.linkedin.com/in/{person['slug']}"},
                    max_usd=MAX_USD,
                    idempotency_key=execution_key,
                )
            except DelegateError as error:
                if error.code == "unavailable":
                    raise  # no answer: the order may exist, and the retry finds it
                await db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"outcome": "failed", "price_usd": 0, "error": error.code},
                )
                return "failed"
            await db.save_effect_progress(conn, execution_key=execution_key, result=placed)
    view = {"state": "running"}
    deadline = asyncio.get_running_loop().time() + JOB_SECONDS
    while view["state"] == "running" and asyncio.get_running_loop().time() < deadline:
        view = await delegate.result(placed["conversation_id"], wait=30)
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

    counts = {"looked_up": 0, "found": 0, "left": 0, "held": False}
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
                *(look_up(db, delegate, project_id, person, run_id=run_id) for person in batch)
            )
        finally:
            beating.cancel()
        counts["looked_up"] += sum(1 for outcome in outcomes if outcome != "held")
        counts["found"] += outcomes.count("found")
        counts["held"] = "held" in outcomes
        heartbeat({"phones": counts["looked_up"]})
    async with db.pool.acquire() as conn:
        counts["left"] = len(await wanted(conn, project_id, limit=1000, only=only))
    return counts
