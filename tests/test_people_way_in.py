"""People's way in: who on the team can reach a person, read from People for the person API and
MCP, in the words Slack uses, and only ever from this project's own members."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import engagers_run, fixture
from test_people_view import mcp, structured

from tin_lite import people_records as records
from tin_lite import people_service
from tin_lite.auth import AuthContext, require_user
from tin_lite.people_api import router
from tin_lite.people_records import Value

TEAMMATE = "user_wayinteammate"
OUTSIDER = "user_wayinoutsider"
NAMES = {USER: "Ege Demir", TEAMMATE: "Emre Şarbak", OUTSIDER: "Olga Other"}


class Profiles:
    """Clerk's member profiles: each member's display name."""

    async def member_profile(self, member):
        return {"display_name": NAMES.get(member)}


@pytest.fixture(autouse=True)
def _fresh_names(monkeypatch):
    # Teammates' names are kept for ten minutes; each test reads its own.
    monkeypatch.setattr(people_service, "_names", {})


def client(f, user=USER):
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=f.db, storage=f.storage)
    app.state.auth = Profiles()
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=user,
        token_type="session_token",  # noqa: S106
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")


async def someone(db, project_id, slug, *, name, company=None, position=None, connected=()):
    """A person People keeps, connected on LinkedIn to the given members."""

    async with db.pool.acquire() as conn:
        await records.ensure_fields(conn, project_id)
        record_id = await records.upsert_person(
            conn, project_id, {"linkedin_slug": slug}, source="test"
        )
        values = [Value("name", name)]
        values += [Value("company", company)] if company else []
        values += [Value("position", position)] if position else []
        await records.set_values(
            conn, project_id, record_id, values, set_by_kind="run", set_by="test"
        )
        for member in connected:
            await records.set_relationship(
                conn, project_id, record_id, member, "connected", set_by="test"
            )
    return record_id


async def way_in(f, record_id, user=USER):
    async with client(f, user) as http:
        answer = await http.get(f"/api/projects/{f.project.id}/people/{record_id}")
    assert answer.status_code == 200, answer.text
    return answer.json()


async def team(collection_db):
    f = await fixture(collection_db)
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id=TEAMMATE)
    return f


async def test_a_teammates_connection_is_named_and_yours_is_you(collection_db):
    f = await team(collection_db)
    rosa = await someone(
        f.db, f.project.id, "rosa-alvarez", name="Rosa Alvarez", company="Valley Bank",
        position="Head of Treasury", connected=[TEAMMATE],
    )  # fmt: skip
    async with f.db.pool.acquire() as conn:
        await records.add_event(
            conn, f.project.id, rosa, kind="connected", event_key="c:rosa",
            detail={"member": TEAMMATE},
        )  # fmt: skip
    found = await way_in(f, rosa)
    assert found["way_in"] == "🤝 Emre is already connected"
    # The panel's work line, and the grid's company.
    assert (found["company"], found["position"]) == ("Valley Bank", "Head of Treasury")
    # Last touch names the teammate too, never "you".
    assert found["last_touch"]["kind"] == "connected" and found["last_touch"]["who"] == "Emre"
    # The teammate reads it as theirs.
    assert (await way_in(f, rosa, TEAMMATE))["way_in"] == "🤝 You are already connected"
    # Both connected: you first.
    async with f.db.pool.acquire() as conn:
        await records.set_relationship(conn, f.project.id, rosa, USER, "connected", set_by="t")
    assert (await way_in(f, rosa))["way_in"] == "🤝 You and Emre are already connected"


async def test_someone_the_team_knows_at_their_company_is_the_way_in(collection_db):
    f = await team(collection_db)
    rosa = await someone(f.db, f.project.id, "rosa-alvarez", name="Rosa Alvarez",
                         company="Valley Bank")  # fmt: skip
    await someone(
        f.db, f.project.id, "mira-chen", name="Mira Chen", company="Valley Bank, Inc.",
        position="Treasury", connected=[TEAMMATE],
    )  # fmt: skip
    await someone(f.db, f.project.id, "bo-park", name="Bo Park", company="Globex",
                  connected=[TEAMMATE])  # fmt: skip
    assert (await way_in(f, rosa))["way_in"] == (
        "🤝 Emre knows Mira Chen (Treasury) at Valley Bank"
    )
    await someone(f.db, f.project.id, "sam-lee", name="Sam Lee", company="valley bank",
                  connected=[USER])  # fmt: skip
    line = (await way_in(f, rosa))["way_in"]
    assert line.startswith("🤝 You and Emre know ") and line.endswith(" at Valley Bank")
    assert "Mira Chen (Treasury)" in line and "Sam Lee" in line and "Bo Park" not in line


async def test_without_a_path_there_is_no_way_in(collection_db):
    f = await team(collection_db)
    await someone(f.db, f.project.id, "mira-chen", name="Mira Chen", company="Valley Bank",
                  connected=[TEAMMATE])  # fmt: skip
    elsewhere = await someone(f.db, f.project.id, "ana", name="Ana Souza", company="Fieldnotes")
    nowhere = await someone(f.db, f.project.id, "ben", name="Ben Carter")
    assert (await way_in(f, elsewhere))["way_in"] is None
    assert (await way_in(f, nowhere))["way_in"] is None


async def test_another_projects_members_are_never_a_way_in(collection_db):
    f = await team(collection_db)
    rosa = await someone(f.db, f.project.id, "rosa-alvarez", name="Rosa Alvarez",
                         company="Valley Bank")  # fmt: skip
    other = await f.db.create_project(name="Other", state_repo_id="projects/" + uuid4().hex)
    await f.db.grant_project_membership(project_id=other.id, clerk_user_id=OUTSIDER)
    # The same person and a colleague of hers, both connections of the other project's member.
    await someone(f.db, other.id, "rosa-alvarez", name="Rosa Alvarez", company="Valley Bank",
                  connected=[OUTSIDER])  # fmt: skip
    await someone(f.db, other.id, "mira-chen", name="Mira Chen", company="Valley Bank",
                  connected=[OUTSIDER])  # fmt: skip
    # A second-degree path the other project's RB2B run found, pinned to an event of hers here.
    there = await engagers_run(SimpleNamespace(db=f.db, project=other), by=OUTSIDER)
    await second_degree(f, rosa, there, OUTSIDER)
    assert (await way_in(f, rosa))["way_in"] is None
    # The outsider can't read her at all.
    async with client(f, OUTSIDER) as http:
        answer = await http.get(f"/api/projects/{f.project.id}/people/{rosa}")
    assert answer.status_code == 404


async def second_degree(f, record_id, run_id, member):
    async with f.db.pool.acquire() as conn:
        await records.add_event(
            conn, f.project.id, record_id, kind="visited", event_key=f"v:{run_id}",
            detail={"source": "rb2b"}, run_id=UUID(run_id),
        )  # fmt: skip
        key = f"{run_id}:rb2b:warm"
        await f.db.start_effect(conn, execution_key=key, operation="rb2b.warm")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={
                "known": {"connected": [], "at_company": []},
                "second": {"member": member, "mutual_count": 4, "mutual_names": ["Mira Chen"]},
                "company": "Valley Bank",
            },
        )


async def test_a_second_degree_path_an_rb2b_run_found_is_shown(collection_db):
    f = await team(collection_db)
    rosa = await someone(f.db, f.project.id, "rosa-alvarez", name="Rosa Alvarez",
                         company="Valley Bank")  # fmt: skip
    await second_degree(f, rosa, await engagers_run(f, by=TEAMMATE), TEAMMATE)
    assert (await way_in(f, rosa))["way_in"] == "🤝 2nd degree via Mira Chen + 3 others"
    # A direct connection is closer.
    async with f.db.pool.acquire() as conn:
        await records.set_relationship(conn, f.project.id, rosa, TEAMMATE, "connected", set_by="t")
    assert (await way_in(f, rosa))["way_in"] == "🤝 Emre is already connected"


async def test_found_for_a_company_visit_and_visitors_in_the_list(collection_db, monkeypatch):
    f = await team(collection_db)
    run = UUID(await engagers_run(f))
    found = await someone(f.db, f.project.id, "omar", name="Omar Haddad", company="Northwind")
    came = await someone(f.db, f.project.id, "rosa", name="Rosa Alvarez", company="Valley Bank")
    await someone(f.db, f.project.id, "conn", name="Conn Ection", connected=[USER])
    async with f.db.pool.acquire() as conn:
        await records.add_event(
            conn, f.project.id, found, kind="visited", event_key="v:omar", run_id=run,
            detail={"source": "rb2b_company", "summary": "Someone at Northwind visited"},
        )  # fmt: skip
        await records.add_event(
            conn, f.project.id, came, kind="visited", event_key="v:rosa", run_id=run,
            detail={"source": "rb2b", "summary": "Read pricing"},
        )  # fmt: skip
    async with client(f) as http:
        listed = (await http.get(f"/api/projects/{f.project.id}/people")).json()
    people = {p["name"]: p for p in listed["people"]}
    assert (people["Omar Haddad"]["visited"], people["Omar Haddad"]["company_visit"]) == (
        True,
        True,
    )
    assert people["Omar Haddad"]["last_touch"]["company_visit"] is True
    assert (people["Rosa Alvarez"]["visited"], people["Rosa Alvarez"]["company_visit"]) == (
        True,
        False,
    )
    assert people["Conn Ection"]["visited"] is False
    # Visitors list ahead of imported connections, so a limit never cuts them.
    assert [p["name"] for p in listed["people"]][:2] in (
        ["Omar Haddad", "Rosa Alvarez"],
        ["Rosa Alvarez", "Omar Haddad"],
    )

    # MCP lists the company, the way in and the Visitors segment.
    await someone(f.db, f.project.id, "mira-chen", name="Mira Chen", company="Valley Bank",
                  connected=[USER])  # fmt: skip
    server = mcp(f, monkeypatch)
    visitors = structured(
        await server.call_tool(
            "list_people", {"project_id": str(f.project.id), "segment": "visitors"}
        )
    )
    rows = {p["name"]: p for p in visitors["people"]}
    assert set(rows) == {"Omar Haddad", "Rosa Alvarez"}
    assert rows["Rosa Alvarez"]["company"] == "Valley Bank"
    assert rows["Rosa Alvarez"]["way_in"] == "🤝 You know Mira Chen at Valley Bank"
    assert rows["Omar Haddad"]["way_in"] is None and rows["Omar Haddad"]["company_visit"] is True
