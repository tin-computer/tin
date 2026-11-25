"""The intel judges' writes: stories and the brief, through the run's own grant.

Plain HTTP routes beside the studio voice route (run_tools.py), called by the `intel` CLI in
the judge's sandbox with `Authorization: Bearer <grant>`:

  POST /intel/story       put or attach a story
  GET  /intel/stories     today's stories in this run's scope, and whether a brief is set
  GET  /intel/story/<sid> one story with its items, from any day
  PUT  /intel/brief       the Slack brief (intel_brief schema)
  GET  /intel/help        the brief's schema text and the tags a story may carry

Scope, day and project come from the grant's run (its workflow, input and parent fetch),
never from a request: only an `intel.world_judge` run on the host project writes world
stories. Every write is checked and answered in a sentence the agent can act on; an item id
the run never saw is listed as missing, not refused, and a tag outside the taxonomy is
dropped with a warning. Limits are runaway guards far above a real day.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse

from tin_lite import intel_brief, intel_store
from tin_lite.domain import INTEL_PROVIDER, INTEL_WRITE_CAPABILITY
from tin_lite.intel_judge import SCOPES

MAX_BODY = 64 * 1024
MAX_WRITES = 3000
MAX_STORIES = 400
MAX_ITEMS_PER_STORY = 400
LINK_DAYS = 30
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
SCHEMA = {"world": intel_brief.WORLD_SCHEMA, "client": intel_brief.CLIENT_SCHEMA}


class Refused(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Judge:
    run_id: UUID
    project_id: UUID | None  # None is the world scope
    run_project_id: UUID
    scope: str
    day: date
    fetch_run_id: UUID


async def judge_of(runtime, settings, token: str) -> Judge:
    database = runtime.database
    grant = await database.authorize_run_tool_grant(token=token, capability=INTEL_WRITE_CAPABILITY)
    if grant is None or grant.provider_key != INTEL_PROVIDER:
        raise Refused("this run does not grant intel writes", 403)
    run = await database.get_run(grant.run_id)
    if run is None or run.status.value not in {"pending", "running"}:
        raise Refused("this run is no longer active", 403)
    key = await database.pool.fetchval("SELECT key FROM workflows WHERE id = $1", run.workflow_id)
    scope = SCOPES.get(key)
    inputs = run.input or {}
    if scope is None or inputs.get("scope") != scope:
        raise Refused("this run is not an intelligence judge", 403)
    try:
        parent_id = UUID(str(inputs.get("parent_run_id")))
    except ValueError:
        raise Refused("this run has no intelligence parent", 403) from None
    fetch = await intel_store.get_fetch(database.pool, parent_id)
    parent = await database.get_run(parent_id)
    if (
        fetch is None
        or parent is None
        or parent.project_id != run.project_id
        or fetch["scope"] != scope
        or fetch["day"].isoformat() != inputs.get("day")
    ):
        raise Refused("this run's day is not prepared", 403)
    if scope == "world":
        if getattr(settings, "intel_host_project_id", None) != run.project_id:
            raise Refused("only the world feed's host project writes world stories", 403)
        project_id = None
    else:
        if fetch["project_id"] != run.project_id:
            raise Refused("this run's day belongs to another project", 403)
        project_id = run.project_id
    return Judge(run.id, project_id, run.project_id, scope, fetch["day"], parent_id)


async def _body(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise Refused("the request is larger than 64 KB; send less at once", 413)
    try:
        return json.loads(raw)
    except ValueError:
        raise Refused("the request body must be JSON") from None


async def _count_write(conn, judge: Judge) -> None:
    count = await conn.fetchval(
        """INSERT INTO effect_receipts (execution_key, operation, status, result)
           VALUES ($1, 'intel.writes', 'started', '{"n": 1}'::jsonb)
           ON CONFLICT (execution_key) DO UPDATE SET
               result = jsonb_build_object(
                   'n', COALESCE((effect_receipts.result->>'n')::int, 0) + 1),
               updated_at = now()
           RETURNING (result->>'n')::int""",
        f"intel:{judge.run_id}:writes",
    )
    if count > MAX_WRITES:
        raise Refused(
            f"this run has made {MAX_WRITES} writes, Tin's limit for one day; finish with what "
            "you have",
            429,
        )


def _field(body: dict, name: str, limit: int) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"`{name}` must be text")
    value = " ".join(value.split())
    if len(value) > limit:
        raise Refused(f"`{name}` is {len(value)} characters; keep it to {limit} or fewer")
    return value


def _items(body: dict) -> list[str]:
    value = body.get("items", [])
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Refused("`items` is a list of item ids such as k3f9a")
    value = [v.strip() for v in value if v.strip()]
    if len(value) > MAX_ITEMS_PER_STORY:
        raise Refused(f"attach at most {MAX_ITEMS_PER_STORY} items to a story")
    return value


def _sid_key(value: object) -> str:
    if not isinstance(value, str):
        raise Refused("name the story by its id, such as s-nvidia-ceo-reinstated")
    key = value.strip().removeprefix("s-")
    if not KEY.fullmatch(key):
        raise Refused(f"{value[:70]!r} is not a story id such as s-nvidia-ceo-reinstated")
    return key


async def put_story(runtime, judge: Judge, body: object) -> dict:
    if not isinstance(body, dict):
        raise Refused("the request must be a JSON object")
    action = body.get("action", "put")
    database = runtime.database
    warnings: list[str] = []
    items = _items(body)
    async with database.pool.acquire() as conn, conn.transaction():
        await _count_write(conn, judge)
        if action == "attach":
            key = _sid_key(body.get("sid"))
            row = await conn.fetchrow(
                "SELECT id FROM intel_stories WHERE project_id IS NOT DISTINCT FROM $1 "
                "AND key = $2 FOR UPDATE",
                judge.project_id,
                key,
            )
            if row is None:
                raise Refused(f"there is no story s-{key}; create it with intel story put first")
            story_id, created = row["id"], False
        elif action == "put":
            key = body.get("key")
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise Refused("`key` is a lowercase slug like nvidia-ceo-reinstated")
            title = _field(body, "title", 200)
            summary = _field(body, "summary", 2000)
            relevance = body.get("relevance")
            if relevance not in (None, "high", "medium", "low"):
                raise Refused("`relevance` is high, medium or low")
            tags = None
            if body.get("tags") is not None:
                raw = body["tags"]
                if isinstance(raw, str):
                    raw = raw.split(",")
                if not isinstance(raw, list):
                    raise Refused('`tags` is a list such as ["ai", "markets"]')
                tags, dropped = intel_brief.clean_tags(judge.scope, raw)
                if dropped:
                    warnings.append(
                        f"dropped tags outside the taxonomy: {', '.join(dropped[:8])} "
                        "(intel tags lists them)"
                    )
            sensitive = body.get("sensitive")
            if sensitive is not None and not isinstance(sensitive, bool):
                raise Refused("`sensitive` is true or false")
            continues_id = None
            if body.get("continues"):
                target = _sid_key(body["continues"])
                if target == key:
                    raise Refused("a story can't continue itself")
                continues_id = await conn.fetchval(
                    "SELECT id FROM intel_stories WHERE project_id IS NOT DISTINCT FROM $1 "
                    "AND key = $2",
                    judge.project_id,
                    target,
                )
                if continues_id is None:
                    raise Refused(
                        f"--continues s-{target}: no such story. Find it with "
                        'intel stories --q "<words>"'
                    )
            exists = await conn.fetchval(
                "SELECT 1 FROM intel_story_days d JOIN intel_stories s ON s.id = d.story_id "
                "WHERE d.run_id = $1 AND s.project_id IS NOT DISTINCT FROM $2 AND s.key = $3",
                judge.run_id,
                judge.project_id,
                key,
            )
            if not exists:
                touched = await conn.fetchval(
                    "SELECT count(*) FROM intel_story_days WHERE run_id = $1", judge.run_id
                )
                if touched >= MAX_STORIES:
                    raise Refused(
                        f"this run has written {MAX_STORIES} stories, Tin's limit for one day", 429
                    )
            try:
                story_id, created = await intel_store.put_story(
                    conn,
                    project_id=judge.project_id,
                    key=key,
                    day=judge.day,
                    run_id=judge.run_id,
                    title=title,
                    summary=summary,
                    relevance=relevance,
                    tags=tags,
                    sensitive=sensitive,
                    continues_id=continues_id,
                )
            except ValueError:
                raise Refused("a new story needs --title and --summary") from None
        else:
            raise Refused("`action` is put or attach")
        if action == "attach":
            await conn.execute(
                "UPDATE intel_stories SET last_day = GREATEST(last_day, $2), updated_run_id = $3, "
                "updated_at = now() WHERE id = $1",
                story_id,
                judge.day,
                judge.run_id,
            )
            await conn.execute(
                """INSERT INTO intel_story_days (story_id, day, run_id, title, summary, relevance,
                       sensitive)
                   SELECT id, $2, $3, title, summary, relevance, sensitive FROM intel_stories
                   WHERE id = $1 ON CONFLICT (story_id, day) DO NOTHING""",
                story_id,
                judge.day,
                judge.run_id,
            )
        total = await conn.fetchval(
            "SELECT count(*) FROM intel_story_items WHERE story_id = $1", story_id
        )
        if total + len(items) > MAX_ITEMS_PER_STORY * 2:
            raise Refused("this story already has more items than Tin keeps; attach fewer")
        added, missing = await intel_store.attach_items(
            conn,
            story_id=story_id,
            day=judge.day,
            run_id=judge.run_id,
            fetch_run_id=judge.fetch_run_id,
            project_id=judge.project_id,
            keys=items,
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM intel_story_items WHERE story_id = $1", story_id
        )
    key = await database.pool.fetchval("SELECT key FROM intel_stories WHERE id = $1", story_id)
    if not total:
        warnings.append("this story has no items yet; attach the items that cover it")
    return {
        "sid": intel_store.sid_of(key),
        "created": created,
        "added": added,
        "total": total,
        "missing": missing,
        "warnings": warnings,
    }


async def today(runtime, judge: Judge) -> dict:
    pool = runtime.database.pool
    found = await intel_store.today_stories(pool, project_id=judge.project_id, day=judge.day)
    source = await pool.fetchval("SELECT source FROM intel_briefs WHERE run_id = $1", judge.run_id)
    return {
        "scope": judge.scope,
        "day": judge.day.isoformat(),
        "stories": found,
        "brief": {"state": "set" if source == "judge" else "missing"},
    }


async def links_for(conn, judge: Judge) -> intel_brief.LinkIndex:
    """The URLs a brief may link: today's items and the scope's recent stories' items."""
    rows = await conn.fetch(
        "SELECT url, discussion_url FROM intel_items WHERE run_id = $1", judge.fetch_run_id
    )
    index = intel_brief.LinkIndex()
    for row in rows:
        index.add(row["url"])
        index.add(row["discussion_url"])
    for row in await conn.fetch(
        """SELECT i.url FROM intel_story_items i JOIN intel_stories s ON s.id = i.story_id
           WHERE s.project_id IS NOT DISTINCT FROM $1 AND i.day >= $2""",
        judge.project_id,
        judge.day - timedelta(days=LINK_DAYS),
    ):
        index.add(row["url"])
    return index


async def put_brief(runtime, judge: Judge, body: object) -> dict:
    database = runtime.database
    async with database.pool.acquire() as conn:
        await _count_write(conn, judge)
        state = await today(runtime, judge)
        stories = {s["key"]: s for s in state["stories"]}
        links = await links_for(conn, judge)
        try:
            brief, warnings = intel_brief.validate(
                SCHEMA[judge.scope], body, links=links, stories=stories
            )
        except intel_brief.BriefError as exc:
            raise Refused(str(exc)) from None
        await intel_store.put_brief(
            conn,
            run_id=judge.run_id,
            parent_run_id=judge.fetch_run_id,
            project_id=judge.project_id,
            day=judge.day,
            schema=SCHEMA[judge.scope],
            brief=brief,
            source="judge",
        )
    return {"ok": True, "warnings": warnings}


def mount(app, *, runtime, settings) -> None:
    """Add the /intel routes to the run-tools app."""

    async def handle(request: Request, action) -> JSONResponse:
        authorization = request.headers.get("authorization", "")
        scheme, _separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return JSONResponse({"error": "active run grant required"}, status_code=401)
        try:
            judge = await judge_of(runtime(), settings, token.strip())
            return JSONResponse(await action(judge))
        except Refused as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def story(request: Request) -> JSONResponse:
        async def act(judge):
            return await put_story(runtime(), judge, await _body(request))

        return await handle(request, act)

    async def stories(request: Request) -> JSONResponse:
        return await handle(request, lambda judge: today(runtime(), judge))

    async def one(request: Request) -> JSONResponse:
        async def act(judge):
            key = _sid_key(request.path_params["sid"])
            found = await intel_store.story(
                runtime().database.pool, project_id=judge.project_id, key=key
            )
            if found is None:
                raise Refused(f"no story s-{key}. List them with: intel stories --since 7d", 404)
            return found

        return await handle(request, act)

    async def brief(request: Request) -> JSONResponse:
        async def act(judge):
            return await put_brief(runtime(), judge, await _body(request))

        return await handle(request, act)

    async def help_(request: Request) -> JSONResponse:
        async def act(judge):
            return {
                "brief_schema": intel_brief.schema_text(SCHEMA[judge.scope]),
                "tags": list(intel_brief.TAGS[judge.scope]),
            }

        return await handle(request, act)

    app.add_route("/intel/story", story, methods=["POST"])
    app.add_route("/intel/stories", stories, methods=["GET"])
    app.add_route("/intel/story/{sid}", one, methods=["GET"])
    app.add_route("/intel/brief", brief, methods=["PUT"])
    app.add_route("/intel/help", help_, methods=["GET"])
