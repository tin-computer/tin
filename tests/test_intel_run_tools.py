"""The intel judges' routes: grant auth, scope from the run, missing ids, tags, sizes, briefs."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from disposable_db import migrated_database
from intel_fixtures import DAY, fetch, item, project, run
from starlette.applications import Starlette

from tin_lite import intel_run_tools, intel_store
from tin_lite.intel_fetch.items import canon, short_id

URL = "https://www.reuters.com/world/talks-resume/"


@pytest.fixture
async def db():
    async with migrated_database(max_size=5) as database:
        yield database


async def judge(db, *, project_id, key="intel.world_judge", scope="world", parent_scope=None):
    parent_executor = "intel.world" if (parent_scope or scope) == "world" else "intel.daily"
    parent = await run(db, project_id, parent_executor)
    await fetch(
        db,
        parent,
        scope=parent_scope or scope,
        project_id=None if (parent_scope or scope) == "world" else project_id,
    )
    await intel_store.put_items(
        db.pool, run_id=parent, items=[item(URL), item("https://b.example/2")]
    )
    await intel_store.assign_item_keys(db.pool, parent)
    child = await run(
        db,
        project_id,
        key,
        inputs={"parent_run_id": str(parent), "scope": scope, "day": DAY.isoformat()},
    )
    sandbox = f"sbx-{uuid4().hex[:8]}"
    await db.pool.execute(
        "UPDATE workflow_runs SET sandbox_id = $2, lease_active = true WHERE id = $1",
        child,
        sandbox,
    )
    token = uuid4().hex
    await db.create_run_tool_grant(
        run_id=child,
        sandbox_id=sandbox,
        token=token,
        connection_id=None,
        external_account_id="",
        provider_key="tin.intel",
        capabilities=("intel.write",),
        ttl_seconds=600,
    )
    return parent, child, token


def client(db, host):
    app = Starlette()
    runtime = SimpleNamespace(database=db)
    intel_run_tools.mount(
        app, runtime=lambda: runtime, settings=SimpleNamespace(intel_host_project_id=host)
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_a_world_judge_writes_world_stories_and_sees_missing_ids(db):
    host = await project(db)
    _parent, child, token = await judge(db, project_id=host)
    key = short_id(canon(URL))
    async with client(db, host) as http:
        put = await http.post(
            "/intel/story",
            headers=bearer(token),
            json={
                "action": "put",
                "key": "cairo-talks",
                "title": "Talks resume in Cairo",
                "summary": "Delegations returned.",
                "relevance": "high",
                "tags": ["geopolitics", "made-up", "competitor"],
                "sensitive": True,
                "items": [key, "zzzzz"],
                # A request can't choose its scope or day: these are ignored.
                "scope": "client",
                "day": "2020-01-01",
            },
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["sid"] == "s-cairo-talks" and body["created"] is True
        assert (body["added"], body["total"], body["missing"]) == (1, 1, ["zzzzz"])
        assert "made-up" in body["warnings"][0] and "competitor" in body["warnings"][0]
        listed = (await http.get("/intel/stories", headers=bearer(token))).json()
        assert [s["sid"] for s in listed["stories"]] == ["s-cairo-talks"]
        assert listed["brief"] == {"state": "missing"}
        one = (await http.get("/intel/story/s-cairo-talks", headers=bearer(token))).json()
        assert one["items"][0]["url"] == URL and one["sensitive"] is True
    stored = await db.pool.fetchrow("SELECT * FROM intel_stories WHERE key = 'cairo-talks'")
    assert stored["project_id"] is None and stored["first_day"] == DAY
    assert stored["tags"] == ["geopolitics"] and stored["updated_run_id"] == child


async def test_a_cut_dollar_figure_warns_and_a_story_continuing_itself_updates(db):
    """Prod 2026-11-26: "$20 billion" reached a story as "0 billion" (the shell ate `$2`), and
    `--continues` naming the story's own key was refused although an update was meant."""
    host = await project(db)
    _parent, _child, token = await judge(db, project_id=host)
    key = short_id(canon(URL))
    async with client(db, host) as http:
        cut = await http.post(
            "/intel/story",
            headers=bearer(token),
            json={
                "key": "a16z-fundraising",
                "title": "a16z in talks to raise a new fund",
                "summary": "The firm is raising roughly 0 billion across funds.",
                "items": [key],
            },
        )
        assert cut.status_code == 200
        assert any("dollar amount looks cut" in w for w in cut.json()["warnings"])
        whole = await http.post(
            "/intel/story",
            headers=bearer(token),
            json={
                "key": "a16z-fundraising",
                "summary": "The firm is raising roughly $20 billion across funds.",
                "continues": "s-a16z-fundraising",
            },
        )
        assert whole.status_code == 200, whole.text
        warnings = whole.json()["warnings"]
        assert not any("dollar" in w for w in warnings)
        assert any("is this story, so Tin updated it" in w for w in warnings)
    stored = await db.pool.fetchrow(
        "SELECT summary, continues_id FROM intel_stories WHERE key = 'a16z-fundraising'"
    )
    assert "$20 billion" in stored["summary"] and stored["continues_id"] is None


async def test_grants_and_scope_come_from_the_run(db):
    host = await project(db)
    other = await project(db, "Client")
    _p, _c, token = await judge(db, project_id=host)
    async with client(db, host) as http:
        assert (await http.get("/intel/stories")).status_code == 401
        assert (await http.get("/intel/stories", headers=bearer("nope"))).status_code == 403
    # A world judge on any project but the host is refused.
    _p, _c, elsewhere = await judge(db, project_id=other)
    async with client(db, host) as http:
        refused = await http.get("/intel/stories", headers=bearer(elsewhere))
    assert refused.status_code == 403 and "host project" in refused.json()["error"]
    # A grant for a run that isn't a judge is refused.
    plain = await run(db, host, "content.generate", inputs={"scope": "world"})
    await db.pool.execute(
        "UPDATE workflow_runs SET sandbox_id = 'sbx-x', lease_active = true WHERE id = $1", plain
    )
    await db.create_run_tool_grant(
        run_id=plain,
        sandbox_id="sbx-x",
        token="plain-token",  # noqa: S106 — a test grant
        connection_id=None,
        external_account_id="",
        provider_key="tin.intel",
        capabilities=("intel.write",),
        ttl_seconds=600,
    )
    async with client(db, host) as http:
        assert (await http.get("/intel/stories", headers=bearer("plain-token"))).status_code == 403
    # A client judge writes its own project's stories, never world ones.
    _p, _c, client_token = await judge(
        db, project_id=other, key="intel.client_judge", scope="client"
    )
    async with client(db, host) as http:
        put = await http.post(
            "/intel/story",
            headers=bearer(client_token),
            json={"key": "fabric-news", "title": "T", "summary": "S", "tags": ["competitor"]},
        )
    assert put.status_code == 200 and put.json()["warnings"][0].startswith(
        "this story has no items"
    )
    row = await db.pool.fetchrow(
        "SELECT project_id, tags FROM intel_stories WHERE key='fabric-news'"
    )
    assert row["project_id"] == other and row["tags"] == ["competitor"]


async def test_refusals_are_sentences(db):
    host = await project(db)
    _p, _c, token = await judge(db, project_id=host)
    async with client(db, host) as http:
        cases = [
            ({"key": "Bad Key"}, "lowercase slug"),
            ({"key": "new-story"}, "needs --title and --summary"),
            ({"key": "x-story", "title": "T", "summary": "S", "relevance": "huge"}, "high, medium"),
            (
                {"key": "x-story", "title": "T", "summary": "S", "continues": "s-nope"},
                "no such story",
            ),
            ({"action": "attach", "sid": "s-missing", "items": ["a"]}, "no story s-missing"),
            ({"key": "x-story", "title": "T" * 201, "summary": "S"}, "200 or fewer"),
        ]
        for body, said in cases:
            answer = await http.post("/intel/story", headers=bearer(token), json=body)
            assert answer.status_code == 400 and said in answer.json()["error"], body
        big = await http.post(
            "/intel/story", headers=bearer(token), content=b"{" + b" " * (70 * 1024) + b"}"
        )
        assert big.status_code == 413
        garbage = await http.post("/intel/story", headers=bearer(token), content=b"not json")
        assert garbage.status_code == 400


async def test_briefs_are_checked_and_links_kept_only_for_the_days_items(db):
    host = await project(db)
    parent, child, token = await judge(db, project_id=host)
    key = short_id(canon(URL))
    async with client(db, host) as http:
        for n in range(5):
            await http.post(
                "/intel/story",
                headers=bearer(token),
                json={"key": f"story-{n}", "title": f"T{n}", "summary": "S", "items": [key]},
            )
        brief = {
            "schema": "intel-world-brief/1",
            "headline": "The day",
            "groups": [
                {
                    "group": "tech_ai",
                    "items": [
                        {
                            "story": f"story-{n}",
                            "line": f"[source]({URL}) and [x](https://evil.example/)",
                        }
                        for n in range(5)
                    ],
                }
            ],
        }
        put = await http.put("/intel/brief", headers=bearer(token), json=brief)
        assert put.status_code == 200, put.text
        assert len(put.json()["warnings"]) == 5
        too_few = {
            **brief,
            "groups": [{"group": "tech_ai", "items": brief["groups"][0]["items"][:2]}],
        }
        refused = await http.put("/intel/brief", headers=bearer(token), json=too_few)
        assert refused.status_code == 400 and "5 to 8" in refused.json()["error"]
        state = (await http.get("/intel/stories", headers=bearer(token))).json()
        assert state["brief"] == {"state": "set"}
    kept = await intel_store.get_brief(db.pool, parent_run_id=parent)
    assert kept["source"] == "judge" and kept["run_id"] == str(child)
    line = kept["brief"]["groups"][0]["items"][0]["line"]
    assert line == f"[source]({URL}) and x"


async def test_a_runaway_judge_hits_the_story_limit(db, monkeypatch):
    monkeypatch.setattr(intel_run_tools, "MAX_STORIES", 2)
    host = await project(db)
    _p, _c, token = await judge(db, project_id=host)
    async with client(db, host) as http:
        for n in range(2):
            ok = await http.post(
                "/intel/story",
                headers=bearer(token),
                json={"key": f"s-{n}x", "title": "T", "summary": "S"},
            )
            assert ok.status_code == 200
        stopped = await http.post(
            "/intel/story",
            headers=bearer(token),
            json={"key": "third", "title": "T", "summary": "S"},
        )
        again = await http.post(
            "/intel/story", headers=bearer(token), json={"key": "s-0x", "title": "T2"}
        )
    assert stopped.status_code == 429 and "limit" in stopped.json()["error"]
    assert again.status_code == 200
