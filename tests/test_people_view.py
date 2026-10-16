"""The People view: the list a run wrote, one person with their timeline, and forgetting them.

Reads and scores come from the engagers workflow with a synthetic sandbox and model; the HTTP
API is called as a project member.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import (
    ICP,
    Cloud,
    Router,
    engagers_run,
    fixture,
    member,
    person,
    post,
    run_through,
    work,
)
from test_linkedin_per_member import TEAMMATE, add_teammate

from tin_lite import linkedin_engagers as engagers
from tin_lite import people_records as records
from tin_lite.auth import AuthContext, require_user
from tin_lite.people_api import router
from tin_lite.people_records import Value


def client(f, user=USER):
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=f.db, storage=f.storage)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=user,
        token_type="session_token",  # noqa: S106
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")


async def ran(f, people, *, days=3, by=None):
    item = post(1, days)
    await run_through(
        work(f, Cloud([item], {item["thread_urn"]: people})), await engagers_run(f, by=by)
    )
    return item


def by_name(listed):
    return {p["name"]: p for p in listed["people"]}


async def test_the_list_shows_each_value_with_who_set_it_and_the_last_touch(collection_db):
    f = await fixture(collection_db)
    item = await ran(f, [person(1, degree="1st"), person(2, headline="Designer")])
    async with client(f) as http:
        listed = (await http.get(f"/api/projects/{f.project.id}/people")).json()
    assert listed["truncated"] is False and listed["fit_shown"] == 0.6
    people = by_name(listed)
    assert set(people) == {"Person 1", "Person 2"}
    one = people["Person 1"]
    assert one["headline"] == "Founder at Startup 1"
    assert one["profile_url"] == f"https://www.linkedin.com/in/{member(1)}"
    fields = one["fields"]
    assert fields["icp"]["value"] == "Software founders"
    assert fields["icp_fit"]["value"] == 0.9
    assert fields["icp_fit"]["reason"].startswith("Headline says")
    assert fields["icp_fit"]["by"] == "engagers" and fields["icp_fit"]["stale"] is False
    assert fields["icp_fit"]["definition"] == "strategy/ICP.md"
    assert fields["linkedin_relationship"]["value"] == "connected"
    assert people["Person 2"]["fields"]["linkedin_relationship"]["value"] == "none"
    assert people["Person 2"]["fields"]["icp"]["value"] == "none"
    # LinkedIn gives no reaction time: the last touch is the post's date.
    assert one["last_touch"]["kind"] == "reacted"
    assert one["last_touch"]["at"][:10] == item["published_at"][:10]
    assert one["added"]["by"] == "engagers"

    # A changed ICP file marks every score old until the next run rescores it.
    changed = ICP + "\n## 3. Agencies\n\nBuyer: a CTO.\n"
    f.storage.repo.edit({"strategy/ICP.md": changed.encode()}, message="ICP")
    async with client(f) as http:
        people = by_name((await http.get(f"/api/projects/{f.project.id}/people")).json())
    assert people["Person 1"]["fields"]["icp_fit"]["stale"] is True
    assert people["Person 1"]["fields"]["icp"]["stale"] is True
    assert people["Person 1"]["fields"]["linkedin_relationship"]["stale"] is False


async def test_a_change_in_how_tin_scores_marks_scores_old_until_a_run_rescores_them(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    monkeypatch.setattr(engagers, "SCORER", engagers.SCORER - 1)
    await ran(f, [person(1), person(2, headline="Designer")])
    monkeypatch.undo()
    path = f"/api/projects/{f.project.id}/people"
    async with client(f) as http:
        people = by_name((await http.get(path)).json())
    assert people["Person 1"]["fields"]["icp_fit"]["stale"] is True
    assert people["Person 2"]["fields"]["icp"]["stale"] is True
    # The view still names only the file.
    assert people["Person 1"]["fields"]["icp_fit"]["definition"] == "strategy/ICP.md"

    router = Router()
    item = post(1, 3)
    cloud = Cloud([item], {item["thread_urn"]: [person(1), person(2, headline="Designer")]})
    await run_through(work(f, cloud, router), await engagers_run(f))
    assert len(router.requests) == 1
    assert len(json.loads(router.requests[0].messages[0].content)["people"]) == 2
    kept = await records.people(f.db.pool, f.project.id)
    assert {p["fields"]["icp_fit"]["definition"].rsplit(":", 1)[1] for p in kept} == {
        f"v{engagers.SCORER}"
    }
    async with client(f) as http:
        people = by_name((await http.get(path)).json())
    assert not any(p["fields"]["icp_fit"]["stale"] for p in people.values())


async def test_a_person_shows_their_timeline_and_whose_post_it_was(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    mine = await ran(f, [person(1)], days=3)
    theirs = await ran(f, [person(1)], days=9, by=TEAMMATE)
    async with client(f) as http:
        listed = (await http.get(f"/api/projects/{f.project.id}/people")).json()
        found = listed["people"][0]
        one = (await http.get(f"/api/projects/{f.project.id}/people/{found['id']}")).json()
        as_teammate = await http.get(f"/api/projects/{f.project.id}/people/{found['id']}")
    assert [e["at"][:10] for e in one["events"]] == [
        mine["published_at"][:10],
        theirs["published_at"][:10],
    ]
    newest = one["events"][0]
    assert newest["kind"] == "reacted" and newest["reaction"] == "LIKE"
    assert newest["post_url"] == f"https://www.linkedin.com/feed/update/{mine['activity_urn']}/"
    assert [e["yours"] for e in one["events"]] == [True, False]
    assert as_teammate.status_code == 200

    # A value a member set says who: you, or a teammate.
    async with f.db.pool.acquire() as conn:
        await records.set_values(
            conn,
            f.project.id,
            UUID(found["id"]),
            [Value("icp", "Service agencies", "Runs an agency")],
            set_by_kind="member",
            set_by=TEAMMATE,
        )
    async with client(f) as http:
        me = (await http.get(f"/api/projects/{f.project.id}/people/{found['id']}")).json()
    async with client(f, TEAMMATE) as http:
        them = (await http.get(f"/api/projects/{f.project.id}/people/{found['id']}")).json()
    assert me["fields"]["icp"]["by"] == "a teammate" and them["fields"]["icp"]["by"] == "you"
    assert [e["yours"] for e in them["events"]] == [False, True]


async def test_forgetting_a_person_removes_them_and_keeps_them_out(collection_db):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(2)])
    path = f"/api/projects/{f.project.id}/people"
    async with client(f) as http:
        gone = by_name((await http.get(path)).json())["Person 2"]["id"]
        assert (await http.delete(f"{path}/{gone}")).status_code == 204
        assert (await http.get(f"{path}/{gone}")).status_code == 404
        assert (await http.delete(f"{path}/{gone}")).status_code == 404
        assert set(by_name((await http.get(path)).json())) == {"Person 1"}
    await ran(f, [person(1), person(2)])
    async with client(f) as http:
        assert set(by_name((await http.get(path)).json())) == {"Person 1"}


async def test_only_project_members_see_or_forget_people(collection_db):
    f = await fixture(collection_db)
    await ran(f, [person(1)])
    path = f"/api/projects/{f.project.id}/people"
    async with client(f) as http:
        someone = (await http.get(path)).json()["people"][0]["id"]
    async with client(f, "user_outsider") as http:
        assert (await http.get(path)).status_code == 404
        assert (await http.get(f"{path}/{someone}")).status_code == 404
        assert (await http.delete(f"{path}/{someone}")).status_code == 404
        assert (await http.get(f"/api/projects/{uuid4()}/people")).status_code == 404
    async with client(f) as http:
        assert len((await http.get(path)).json()["people"]) == 1


async def test_without_people_or_an_icp_the_list_is_empty(collection_db):
    f = await fixture(collection_db, icp=None)
    async with client(f) as http:
        listed = (await http.get(f"/api/projects/{f.project.id}/people")).json()
    assert listed["people"] == []


async def test_a_person_forgotten_while_scored_is_skipped(collection_db):
    f = await fixture(collection_db)
    item = post(1, 2)

    class Forgetting(Router):
        async def generate(self, route, request, *, timeout_seconds):
            async with f.db.pool.acquire() as conn:
                for found in await records.people(conn, f.project.id):
                    if found["identifiers"]["linkedin_member"] == member(2):
                        await records.forget(conn, f.project.id, found["id"])
            return await super().generate(route, request, timeout_seconds=timeout_seconds)

    run_id = await engagers_run(f)
    await run_through(
        work(f, Cloud([item], {item["thread_urn"]: [person(1), person(2)]}), Forgetting()),
        run_id,
    )
    left = await records.people(f.db.pool, f.project.id)
    assert [p["fields"]["name"]["text"] for p in left] == ["Person 1"]
    assert left[0]["fields"]["icp_fit"]["number"] == 0.9
    status = await f.db.pool.fetchval("SELECT status FROM workflow_runs WHERE id=$1", UUID(run_id))
    assert status == "succeeded"


def mcp(f, monkeypatch, actor=USER):
    from tin_lite.mcp_server import create_mcp_app

    token = SimpleNamespace(subject=actor, scopes=["openid"], client_id="client_test")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    runtime = SimpleNamespace(database=f.db, storage=f.storage)
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
    )
    return create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)[0]


def structured(result):
    return result[1] if isinstance(result, tuple) else result.structured_content


async def test_a_member_picks_the_fit_and_can_give_tins_score_back(collection_db):
    f = await fixture(collection_db)
    await ran(f, [person(1, headline="Designer")])
    path = f"/api/projects/{f.project.id}/people"
    async with client(f) as http:
        someone = (await http.get(path)).json()
        assert someone["profiles"] == ["Software founders", "Service agencies"]
        one = someone["people"][0]["id"]
        assert someone["people"][0]["fields"]["icp_fit"]["value"] == 0.1

        picked = await http.patch(f"{path}/{one}", json={"fit": {"icp": "Software founders"}})
        assert picked.status_code == 200, picked.text
        fields = picked.json()["fields"]
        assert fields["icp"]["value"] == "Software founders" and fields["icp"]["by"] == "you"
        assert fields["icp_fit"]["value"] == 1.0 and fields["icp_fit"]["stale"] is False
        # Tin's score stays, so "Use Tin's score" can bring it back.
        assert fields["icp_fit"]["previous"] == {"value": 0.1, "reason": "Headline says Designer"}

        assert (
            await http.patch(f"{path}/{one}", json={"fit": {"icp": "Agencies"}})
        ).status_code == 422
        assert (
            await http.patch(f"{path}/{one}", json={"fit": {"icp": "x"}, "y": 1})
        ).status_code == 422

    # A run never scores a member's pick again, so it never pays for it.
    router = Router()
    item = post(1, 3)
    await run_through(
        work(f, Cloud([item], {item["thread_urn"]: [person(1, headline="Designer")]}), router),
        await engagers_run(f),
    )
    assert router.requests == []
    async with client(f) as http:
        fields = (await http.get(f"{path}/{one}")).json()["fields"]
        assert fields["icp_fit"]["value"] == 1.0 and fields["icp_fit"]["by"] == "you"

        restored = (await http.patch(f"{path}/{one}", json={"fit": None})).json()["fields"]
        assert restored["icp"]["value"] == "none" and restored["icp"]["by"] == "engagers"
        assert restored["icp_fit"]["value"] == 0.1 and restored["icp_fit"]["previous"] is None

        not_a_fit = (await http.patch(f"{path}/{one}", json={"fit": {"icp": None}})).json()
        assert not_a_fit["fields"]["icp"]["value"] == "none"
        assert not_a_fit["fields"]["icp_fit"]["value"] == 0.0

        customer = (await http.patch(f"{path}/{one}", json={"customer": True})).json()
        assert customer["fields"]["customer"]["value"] == "yes"
        assert customer["fields"]["customer"]["by"] == "you"
        assert (await http.patch(f"{path}/{one}", json={"customer": False})).json()["fields"][
            "customer"
        ]["value"] == "no"
    async with client(f, "user_outsider") as http:
        assert (await http.patch(f"{path}/{one}", json={"customer": True})).status_code == 404


async def test_a_note_marks_the_score_old_and_the_next_run_reads_it(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    await ran(f, [person(1)])
    path = f"/api/projects/{f.project.id}/people"
    note = "Interned with us in 2024.\n\nA friend of the team, not a buyer."
    async with client(f) as http:
        one = (await http.get(path)).json()["people"][0]["id"]
        assert (await http.post(f"{path}/{one}/notes", json={"body": "  \n "})).status_code == 422
        added = await http.post(f"{path}/{one}/notes", json={"body": f"  {note}\n"})
        assert added.status_code == 201, added.text
        found = added.json()
        assert [(n["body"], n["by"], n["mine"]) for n in found["notes"]] == [(note, "you", True)]
        assert found["fields"]["icp_fit"]["stale"] is True
        assert found["fields"]["icp_fit"]["stale_reason"] == "note"
        listed = (await http.get(path)).json()["people"][0]
        assert listed["note"]["body"] == note and listed["note"]["count"] == 1
        assert "friend of the team" in listed["notes_text"]
    note_id = found["notes"][0]["id"]

    # The next run sends the note with the headline, and the new score is current.
    router = Router()
    item = post(1, 3)
    await run_through(
        work(f, Cloud([item], {item["thread_urn"]: [person(1)]}), router), await engagers_run(f)
    )
    sent = json.loads(router.requests[0].messages[0].content)["people"]
    assert sent[0]["notes"] == [note]
    async with client(f) as http:
        fields = (await http.get(f"{path}/{one}")).json()["fields"]
        assert fields["icp_fit"]["stale"] is False
    # Scored after the note, they aren't sent again.
    again = Router()
    await run_through(
        work(f, Cloud([item], {item["thread_urn"]: [person(1)]}), again), await engagers_run(f)
    )
    assert again.requests == []

    # Only the author changes or deletes a note.
    async with client(f, TEAMMATE) as http:
        seen = (await http.get(f"{path}/{one}")).json()["notes"][0]
        assert seen["by"] == "a teammate" and seen["mine"] is False
        changed = await http.patch(f"{path}/{one}/notes/{note_id}", json={"body": "Mine now"})
        assert changed.status_code == 403
        assert (await http.delete(f"{path}/{one}/notes/{note_id}")).status_code == 403
    async with client(f) as http:
        edited = await http.patch(f"{path}/{one}/notes/{note_id}", json={"body": "Ex-intern."})
        assert edited.json()["notes"][0]["body"] == "Ex-intern."
        assert edited.json()["notes"][0]["edited"] is True
        assert edited.json()["fields"]["icp_fit"]["stale"] is True
        gone = (await http.delete(f"{path}/{one}/notes/{note_id}")).json()
        assert gone["notes"] == []
        assert (await http.delete(f"{path}/{one}/notes/{note_id}")).status_code == 404
        await http.post(f"{path}/{one}/notes", json={"body": "Another"})
        assert (await http.delete(f"{path}/{one}")).status_code == 204
    assert await f.db.pool.fetchval("SELECT count(*) FROM record_notes") == 0


async def test_mcp_reads_and_corrects_people_like_a_member(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(2, headline="Designer")])
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    listed = structured(await server.call_tool("list_people", {"project_id": project}))
    assert [p["name"] for p in listed["people"]] == ["Person 1", "Person 2"]
    assert listed["profiles"] == ["Software founders", "Service agencies"]
    assert listed["links"]["people"].endswith(f"/people?project={project}")
    designer = listed["people"][1]["id"]

    fitting = structured(
        await server.call_tool("list_people", {"project_id": project, "segment": "icp_fit"})
    )
    assert [p["name"] for p in fitting["people"]] == ["Person 1"]
    changed = structured(
        await server.call_tool(
            "update_person",
            {
                "project_id": project,
                "person_id": designer,
                "icp": "Service agencies",
                "customer": True,
            },
        )
    )
    assert changed["fields"]["icp"]["value"] == "Service agencies"
    assert changed["fields"]["customer"]["value"] == "yes"
    noted = structured(
        await server.call_tool(
            "add_person_note",
            {"project_id": project, "person_id": designer, "note": "Started as our intern."},
        )
    )
    assert noted["notes"][0]["body"] == "Started as our intern."
    found = structured(
        await server.call_tool("list_people", {"project_id": project, "query": "intern"})
    )
    assert [p["name"] for p in found["people"]] == ["Person 2"]
    assert found["people"][0]["customer"] is True and found["people"][0]["note_count"] == 1

    with pytest.raises(ToolError, match="conflict"):
        await server.call_tool(
            "forget_person", {"project_id": project, "person_id": designer, "confirm_name": "P2"}
        )
    with pytest.raises(ToolError, match="invalid"):
        await server.call_tool(
            "update_person",
            {"project_id": project, "person_id": designer, "icp": "x", "not_a_fit": True},
        )
    forgotten = structured(
        await server.call_tool(
            "forget_person",
            {"project_id": project, "person_id": designer, "confirm_name": "Person 2"},
        )
    )
    assert forgotten["forgotten"] is True
    with pytest.raises(ToolError, match="not_found"):
        await server.call_tool("get_person", {"project_id": project, "person_id": designer})

    # Someone a visitor journeys run named reads by their email and last visit.
    from tin_lite import people_records

    async with f.db.pool.acquire() as conn:
        visitor = await people_records.upsert_person(
            conn, f.project.id, {"email": "ana@fieldnotes.dev"}, source="product.visitor_journeys"
        )
        await people_records.ensure_fields(conn, f.project.id)
        await people_records.set_values(
            conn,
            f.project.id,
            visitor,
            [
                people_records.Value("web_tier", "new_user", "/connect"),
                people_records.Value("web_summary", "Signed up, stopped at Connect GitHub."),
            ],
            set_by_kind="run",
            set_by="visitors-run",
        )
    site = structured(
        await server.call_tool("list_people", {"project_id": project, "segment": "on_the_site"})
    )
    assert [(p["email"], p["on_the_site"], p["last_visit"]) for p in site["people"]] == [
        ("ana@fieldnotes.dev", "new_user", "Signed up, stopped at Connect GitHub.")
    ]
    found = structured(
        await server.call_tool("list_people", {"project_id": project, "query": "fieldnotes"})
    )
    assert [p["email"] for p in found["people"]] == ["ana@fieldnotes.dev"]

    outsider = mcp(f, monkeypatch, actor="user_outsider")
    with pytest.raises(ToolError, match="not_found"):
        await outsider.call_tool("list_people", {"project_id": project})
