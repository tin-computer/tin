"""Activities for an RB2B visit that named only a company (rb2b_company has the why).

judge → (worth chasing) find → rate → notify, each safe to repeat. History holds the run ID and
whether the company is worth chasing; the verdict, the people found and what Delegate charged
are on the visit (`rb2b_visits.company`). Every model call and every Delegate order has its own
receipt: an order is placed under its receipt's key as Delegate's idempotency key, so an order
repeated after a crash is the same order, and a call whose answer never came isn't bought again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from temporalio import activity

from tin_lite import people_icp, people_rating, rb2b, rb2b_company, rb2b_slack, warm_paths
from tin_lite import people_records as records
from tin_lite.delegate import DelegateError
from tin_lite.model_providers import (
    MessageRole,
    ModelMessage,
    ModelOutputTruncated,
    ModelProviderError,
    ModelRequest,
    WebSearchRequest,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.people_service import first_names
from tin_lite.product_urls import dashboard_url
from tin_lite.rb2b_activities import KEY, RB2BActivities, _heartbeat, _payload

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _beating(detail: dict):
    """Heartbeats every 30 s while a slow call (a model searching, a Delegate job) runs."""

    async def beat():
        while True:
            _heartbeat(detail)
            await asyncio.sleep(30)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()


def _document(value) -> dict:
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


class RB2BCompanyActivities(RB2BActivities):
    def __init__(self, *, delegate=None, **kwargs):
        super().__init__(**kwargs)
        self.delegate = delegate

    async def _company(self, run) -> dict:
        return _document(
            await self.db.pool.fetchval(
                "SELECT company FROM rb2b_visits WHERE id = $1 AND project_id = $2",
                UUID(str(run.input["visit_id"])),
                run.project_id,
            )
        )

    async def _keep_company(self, run, values: dict) -> None:
        await self.db.pool.execute(
            """UPDATE rb2b_visits SET company = COALESCE(company, '{}'::jsonb) || $3::jsonb
               WHERE id = $1 AND project_id = $2""",
            UUID(str(run.input["visit_id"])),
            run.project_id,
            json.dumps(values),
        )

    async def _reserve(self, run, conn, key: str, max_usd: float) -> bool:
        """Reserve this order's most under the project's daily Delegate guard: the day's spend
        is read and the reservation written in one transaction under a per-project lock, so
        concurrent runs count each other's reservations. The order's receipt then settles to
        the quoted price, or to nothing when Delegate refuses or refunds it."""

        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"rb2b-delegate-spend:{run.project_id}",
            )
            spent = await self._spent_today(run, conn=conn)
            if spent + max_usd > rb2b_company.DELEGATE_USD_PER_DAY:
                return False
            await self.db.save_effect_progress(
                conn, execution_key=key, result={"price_usd": max_usd, "reserved": True}
            )
            return True

    async def _spent_today(self, run, *, conn=None) -> float:
        """What this project's company runs of the last day ordered at Delegate, from their
        receipts: placed orders count at their quoted price until Delegate refunds them."""

        return float(
            await (conn or self.db.pool).fetchval(
                """SELECT COALESCE(sum((e.result->>'price_usd')::numeric), 0)
                   FROM workflow_runs r JOIN effect_receipts e ON e.execution_key IN (
                       r.id::text || ':rb2b:company:finder',
                       r.id::text || ':rb2b:company:search',
                       r.id::text || ':rb2b:company:profiles')
                   WHERE r.project_id = $1 AND r.executor = $2
                     AND r.created_at > now() - interval '1 day'
                     AND e.result ? 'price_usd'""",
                run.project_id,
                KEY,
            )
        )

    async def _ask(self, run, step: str, request: ModelRequest):
        """One model call under its receipt: its parsed answer, or None when it can't be used
        or an earlier call's answer never came."""

        key = f"{run.id}:rb2b:company:{step}"
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return (saved.result or {}).get("output")
            if saved:
                return None
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            output = None
            try:
                with model_usage_scope(run_id=run.id, step=f"rb2b:company:{step}", conn=conn):
                    async with (
                        _beating({"step": step}),
                        asyncio.timeout(people_icp.MODEL_SECONDS + 15),
                    ):
                        result = await self.router.generate(
                            people_icp.ROUTE.key, request, timeout_seconds=people_icp.MODEL_SECONDS
                        )
                output = result.parsed
            except (ModelProviderError, ModelOutputTruncated, TimeoutError) as error:
                # A check on model output never fails the run that paid for it.
                logger.warning("RB2B company %s failed for run %s: %s", step, run.id, type(error))
            await self.db.complete_effect(conn, execution_key=key, result={"output": output})
            return output

    async def _hire(
        self, run, step: str, service: str, task_input: dict, max_usd: float, keep, scope=None
    ):
        """One Delegate job under its receipt: (what `keep` made of its answer, or None, and
        what it cost in USD). A refused order buys nothing; a job that fails is refunded."""

        key = f"{run.id}:rb2b:company:{step}"
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                done = saved.result or {}
                return done.get("kept"), float(done.get("price_usd") or 0)
            placed = (saved.result or {}) if saved else {}
            if saved is None:
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
            if not placed.get("conversation_id"):
                if not placed.get("reserved") and not await self._reserve(run, conn, key, max_usd):
                    # The project's daily guard on Delegate spend: nothing is ordered.
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={"kept": None, "price_usd": 0, "error": "daily_limit"},
                    )
                    return None, 0.0
                try:
                    # The receipt's key is the order's idempotency key: placing it again after
                    # a crash returns the same order.
                    placed = await self.delegate.order(
                        service, task_input, max_usd=max_usd, idempotency_key=key, scope=scope
                    )
                except DelegateError as error:
                    if error.code == "unavailable":
                        raise  # no answer: the order may exist, and the retry finds it
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={"kept": None, "price_usd": 0, "error": error.code},
                    )
                    return None, 0.0
                await self.db.save_effect_progress(conn, execution_key=key, result=placed)
        view = {"state": "running"}
        deadline = asyncio.get_running_loop().time() + rb2b_company.JOB_SECONDS
        async with _beating({"step": step}):
            while view["state"] == "running" and asyncio.get_running_loop().time() < deadline:
                view = await self.delegate.result(placed["conversation_id"], wait=30)
        delivered = view["state"] == "delivered"
        price = float(placed["price_usd"]) if view["state"] != "failed" else 0.0
        result = {
            **placed,
            "kept": keep(view["result"]) if delivered else None,
            # Delegate refunds a failed job; one still running when Tin stopped waiting may
            # still be charged, so it counts.
            "price_usd": price,
            "state": view["state"],
        }
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                done = saved.result or {}
                return done.get("kept"), float(done.get("price_usd") or 0)
            await self.db.complete_effect(conn, execution_key=key, result=result)
        return result["kept"], price

    async def _own(self, run) -> set[str]:
        async with self.db.pool.acquire() as conn:
            return await rb2b_company.own_hosts(conn, run.project_id)

    async def _judged_before(self, run, site: str) -> dict | None:
        row = await self.db.pool.fetchrow(
            """SELECT id, company FROM rb2b_visits
               WHERE project_id = $1 AND id <> $2 AND kind = 'company'
                 AND company->>'site' = $3 AND company->>'judged' = 'true'
                 AND company->>'repeat_of' IS NULL
                 AND received_at > now() - make_interval(days => $4)
               ORDER BY received_at DESC LIMIT 1""",
            run.project_id,
            UUID(str(run.input["visit_id"])),
            site,
            rb2b_company.REPEAT_DAYS,
        )
        return {**_document(row["company"]), "visit_id": str(row["id"])} if row else None

    @activity.defn(name="rb2b_company_judge")
    async def judge(self, run_id: str) -> dict:
        """Whether the company is worth chasing, its fit and intent, and whom to look for."""

        run = await self._active(run_id)
        await self._progress(run, "judge", 2, "Judging the company the visit came from")
        found = rb2b.lead(_payload(await self._visit(run)))
        own = await self._own(run)
        facts = rb2b_company.facts(found, own)
        about = {
            "name": found["company"],
            "site": rb2b_company.domain(found["website"]),
            "linkedin_page": found["linkedin"],
            "industry": found["industry"],
            "employees": found["employees"],
            "location": found["location"],
            # Shown in Slack as the model read them: a path on the project's site, a channel.
            "page": facts.get("page"),
            "came_from": facts.get("came_from"),
        }
        about = {key: value for key, value in about.items() if value}
        if about.get("site") and (earlier := await self._judged_before(run, about["site"])):
            # The same company again within the week: its verdict and people stand, nothing
            # is bought again and nothing is posted again.
            kept = {key: earlier[key] for key in rb2b_company.REUSED if key in earlier}
            await self._keep_company(run, {**about, **kept, "repeat_of": earlier["visit_id"]})
            return {"worth": False}
        project = await self.db.get_project(run.project_id)
        icp = await people_icp.read_icp(self.storage, project)
        if not people_rating.available(self.router) or not icp.get("profiles"):
            await self._keep_company(run, about)
            return {"worth": False}
        text = await people_rating.icp_text(self.storage, project, icp)
        async with self.db.pool.acquire() as conn:
            known = await rb2b_company.known_people(conn, run.project_id, found["company"])
        output = await self._ask(
            run,
            "judge",
            WebSearchRequest(
                system=rb2b_company.judge_instructions(icp["profiles"]),
                messages=(
                    ModelMessage(
                        role=MessageRole.USER,
                        content=rb2b_company.judge_input(text, facts, known),
                    ),
                ),
                output_schema=rb2b_company.judge_schema(icp["profiles"]),
                output_schema_name="company_verdict",
                max_output_tokens=people_icp.MAX_OUTPUT_TOKENS,
                reasoning_effort=people_icp.REASONING,
                web_search_calls=rb2b_company.JUDGE_SEARCHES,
            ),
        )
        judged = rb2b_company.verdict(output, icp["profiles"])
        await self._keep_company(run, {**about, **(judged or {}), "judged": judged is not None})
        return {"worth": bool(judged and judged["worth"])}

    @activity.defn(name="rb2b_company_find")
    async def find(self, run_id: str) -> dict:
        """Up to PICKED people to contact there, each checked against their LinkedIn profile,
        kept in People."""

        run = await self._active(run_id)
        company = await self._company(run)
        if isinstance(company.get("people"), list):
            return {"people": len(company["people"])}
        await self._progress(run, "find", 3, "Looking for people to contact there")
        name, site = company.get("name") or "", company.get("site")
        why = None
        if self.delegate is None:
            why = "no people search"
        elif not site or not company.get("titles") or not name:
            why = "no website or titles"
        elif await self._spent_today(run) + rb2b_company.MOST_PER_COMPANY > (
            rb2b_company.DELEGATE_USD_PER_DAY
        ):
            why = "daily_limit"
        if why:
            await self._keep_company(
                run, {"people": [], "found": 0, "spent_usd": 0, "not_looked": why}
            )
            return {"people": 0}
        titles = company["titles"]
        listed, spent = await self._hire(
            run,
            "finder",
            rb2b_company.FINDER,
            rb2b_company.finder_input(site, titles),
            rb2b_company.FINDER_MAX_USD,
            rb2b_company.candidates,
        )
        people = listed or []
        if not people:
            listed, cost = await self._hire(
                run,
                "search",
                rb2b_company.SEARCH,
                rb2b_company.search_input(site, titles),
                rb2b_company.SEARCH_MAX_USD,
                rb2b_company.candidates,
            )
            people, spent = listed or [], spent + cost
        chosen = people[: rb2b_company.PICKED]
        if len(people) > rb2b_company.PICKED:
            project = await self.db.get_project(run.project_id)
            icp = await people_icp.read_icp(self.storage, project)
            text = await people_rating.icp_text(self.storage, project, icp) if icp else ""
            output = await self._ask(
                run,
                "pick",
                ModelRequest(
                    system=rb2b_company.pick_instructions(),
                    messages=(
                        ModelMessage(
                            role=MessageRole.USER,
                            content=rb2b_company.pick_input(
                                text,
                                rb2b_company.facts(
                                    rb2b.lead(_payload(await self._visit(run))),
                                    await self._own(run),
                                ),
                                company,
                                people,
                            ),
                        ),
                    ),
                    output_schema=rb2b_company.pick_schema(),
                    output_schema_name="people_to_contact",
                    max_output_tokens=8000,
                    reasoning_effort=people_icp.REASONING,
                ),
            )
            numbers = rb2b_company.picked(output, len(people))
            # An unusable answer still leaves the list's first few to check.
            chosen = [people[n] for n in numbers] if numbers is not None else chosen
        reads: dict = {}
        if chosen:
            page = company.get("linkedin_page")
            reads, cost = await self._hire(
                run,
                "profiles",
                rb2b_company.PROFILES,
                rb2b_company.profiles_input([person["linkedin"] for person in chosen]),
                rb2b_company.PROFILES_MAX_USD,
                lambda payload: rb2b_company.checked(payload, name, page),
                # The profile read prices by how many profiles it reads.
                scope={"requested_items": len(chosen)},
            )
            reads, spent = reads or {}, spent + cost
        kept = []
        for person in chosen:
            read = reads.get(person["linkedin"])
            if read is not None and not read["current"]:
                continue  # their profile shows they left
            kept.append({**person, **(read or {}), "checked": read is not None})
        ids = await self._people(run, company, kept)
        await self._keep_company(
            run,
            {
                "people": [str(record_id) for record_id in ids],
                "found": len(people),
                "left": sum(1 for read in reads.values() if not read["current"]),
                "spent_usd": round(spent, 6),
            },
        )
        return {"people": len(ids)}

    async def _people(self, run, company: dict, kept: list[dict]) -> list[UUID]:
        """Each person in People, with what nobody set yet and the company's visit."""

        found = rb2b.lead(_payload(await self._visit(run)))
        if found["at"] is None:
            found["at"] = (await self._visit(run))["received_at"]
        ids: list[UUID] = []
        own = await self._own(run)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await records.ensure_fields(conn, run.project_id)
            for person in kept:
                identifiers = records.linkedin_identifiers(person["linkedin"])
                record_id = await records.upsert_person(
                    conn, run.project_id, identifiers, source=rb2b.SOURCE, run_id=run.id
                )
                if record_id is None:
                    continue  # someone the team asked Tin to forget
                record_id = await records.current(conn, run.project_id, record_id)
                have = {
                    row["field"]
                    for row in await conn.fetch(
                        """SELECT field FROM record_values WHERE record_id = $1
                           AND (value_text IS NOT NULL OR value_number IS NOT NULL)""",
                        record_id,
                    )
                }
                values = [
                    value
                    for value in rb2b_company.person_values(person, company["name"])
                    if value.field not in have
                ]
                if values:
                    await records.set_values(
                        conn,
                        run.project_id,
                        record_id,
                        values,
                        set_by_kind="run",
                        set_by=str(run.id),
                    )
                await records.add_event(
                    conn,
                    run.project_id,
                    record_id,
                    kind="visited",
                    event_key=f"rb2b-company:{run.input['visit_id']}:{record_id}",
                    occurred_at=found["at"],
                    detail=rb2b_company.visit_detail(found, company["name"], company, own),
                    run_id=run.id,
                )
                ids.append(record_id)
        return ids

    @activity.defn(name="rb2b_company_rate")
    async def rate_people(self, run_id: str) -> None:
        """The people found, rated by the one ICP rater."""

        run = await self._active(run_id)
        ids = (await self._company(run)).get("people") or []
        if not ids or not people_rating.available(self.router):
            return
        project = await self.db.get_project(run.project_id)
        icp = await people_icp.read_icp(self.storage, project)
        if not icp.get("profiles"):
            return
        await self._progress(run, "rate", 4, "Rating the people found there")
        text = await people_rating.icp_text(self.storage, project, icp)
        await asyncio.gather(
            *(
                people_rating.rate_unrated(
                    self.db,
                    self.router,
                    run,
                    text=text,
                    profiles=icp["profiles"],
                    definition=people_rating.definition_for(icp),
                    operation=KEY,
                    step="rb2b",
                    only=UUID(record_id),
                    heartbeat=_heartbeat,
                )
                for record_id in ids
            )
        )

    async def _company_warm(self, run, company: dict) -> str | None:
        """Who on the team knows someone at the company, from their LinkedIn connections in
        People; free, read when the message is written."""

        name = company.get("name")
        async with self.db.pool.acquire() as conn:
            people = await warm_paths.at_company(conn, run.project_id, name)
        if not people:
            return None
        names = await first_names(self.profiles, {person["member"] for person in people})
        return warm_paths.line(known={"at_company": people}, second=None, names=names, company=name)

    @activity.defn(name="rb2b_company_notify")
    async def notify_company(self, run_id: str) -> None:
        """A good or strong company worth chasing gets its own Slack message, with the people
        found; the rest wait for the evening digest."""

        run = await self._active(run_id)
        place = await rb2b_slack.destination(self.db, self.settings, run.project_id)
        if place is None:
            return
        company = await self._company(run)
        people: list[dict[str, Any]] = []
        async with self.db.pool.acquire() as conn:
            for record_id in company.get("people") or []:
                found = await records.people(conn, run.project_id, only=UUID(record_id))
                people += found
        people.sort(key=lambda person: -(rb2b_slack._fit(person) or 0))
        project = await self.db.get_project(run.project_id)
        base = dashboard_url(self.settings) if self.settings is not None else ""
        body = rb2b_slack.company_message(
            company,
            people,
            people_url=f"{base}/people?project={run.project_id}",
            project_name=project.name,
            warm=await self._company_warm(run, company),
        )
        if body is None:
            return
        await self._progress(run, "notify", 5, "Posting the company in Slack")
        await rb2b_slack.send(
            self.db,
            self.integrations.slack,
            project_id=run.project_id,
            key=f"company:{run.id}",
            place=place,
            body=body,
        )
