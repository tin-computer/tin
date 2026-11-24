"""People a member brings over MCP (add_people), their tags (tag_people), connection requests
to someone known only by a profile link, and how each tag's requests went."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import fixture, member, person
from test_linkedin_requests_flow import (
    MONDAY,
    Clock,
    InviteCloud,
    client,
    no_temporal,  # noqa: F401
    ran,
    requests_run,
    sender,
    turn_on,
)
from test_people_view import mcp, structured

from tin_lite import linkedin_connections_export as export
from tin_lite import linkedin_requests_activities
from tin_lite import people_records as records


class LinkCloud(InviteCloud):
    """The sending sandbox, also answering a lookup of a profile link's ID."""

    def __init__(self, db, *, ids=None, **kwargs):
        super().__init__(db, **kwargs)
        self.ids = ids or {}

    async def engagement(self, conn, *, execution_key, run_id, session, packet, seconds=270):
        if packet["kind"] != "linkedin_profile_ids":
            return await super().engagement(
                conn,
                execution_key=execution_key,
                run_id=run_id,
                session=session,
                packet=packet,
                seconds=seconds,
            )
        self.calls.append(packet)
        return {
            "people": [
                {"slug": slug, "member": self.ids.get(slug), "state": "none"}
                for slug in packet["slugs"]
            ]
        }


async def test_a_member_adds_a_list_with_tags_and_filters_by_them(collection_db, monkeypatch):
    f = await fixture(collection_db)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    added = structured(
        await server.call_tool(
            "add_people",
            {
                "project_id": project,
                "tags": ["Wave 1"],
                "people": [
                    {
                        # As people paste it: no https://, a tracking query.
                        "linkedin_url": "linkedin.com/in/Ada-Yilmaz/?trk=public",
                        "name": "Ada Yilmaz",
                        "headline": "Head of Growth at Acme",
                        "position": "Head of Growth",
                        "tags": ["  Wave   2 "],
                    },
                    {"linkedin_url": "https://tr.linkedin.com/in/bob-builder", "name": "Bob B"},
                    {"email": "CEM@kaya.dev", "name": "Cem Kaya"},
                    {"name": "Nobody to match"},
                ],
            },
        )
    )
    assert added["added"] == 3 and added["matched"] == 0
    assert added["skipped"] == [
        {"index": 3, "reason": "no LinkedIn profile link or email Tin can read"}
    ]
    ada = added["person_ids"][0]

    listed = structured(
        await server.call_tool("list_people", {"project_id": project, "tag": "wave 1"})
    )
    people = {p["name"]: p for p in listed["people"]}
    assert set(people) == {"Ada Yilmaz", "Bob B", "Cem Kaya"}
    assert people["Ada Yilmaz"]["tags"] == ["Wave 1", "Wave 2"]
    assert people["Ada Yilmaz"]["profile_url"] == "https://www.linkedin.com/in/ada-yilmaz"
    # A list a member brought is in All people, not Connections, and nothing was rated for it.
    assert people["Ada Yilmaz"]["imported"] is False
    assert people["Ada Yilmaz"]["icp_fit"] is None
    assert people["Cem Kaya"]["email"] == "cem@kaya.dev"
    searched = structured(
        await server.call_tool("list_people", {"project_id": project, "query": "wave 2"})
    )
    assert [p["name"] for p in searched["people"]] == ["Ada Yilmaz"]

    # The same person again is matched, and keeps what Tin already has.
    again = structured(
        await server.call_tool(
            "add_people",
            {
                "project_id": project,
                "people": [{"linkedin_url": "https://www.linkedin.com/in/ada-yilmaz", "name": "A"}],
            },
        )
    )
    assert again["added"] == 0 and again["matched"] == 1 and again["person_ids"] == [ada]
    shown = structured(
        await server.call_tool("get_person", {"project_id": project, "person_id": ada})
    )
    assert shown["name"] == "Ada Yilmaz" and shown["headline"] == "Head of Growth at Acme"
    assert shown["position"] == "Head of Growth"
    assert (
        await f.db.pool.fetchval(
            "SELECT set_by FROM record_values WHERE record_id=$1 AND field='position'", UUID(ada)
        )
        == records.IMPORT_WRITER
    )

    # Someone's own connections go under Connections.
    mine = structured(
        await server.call_tool(
            "add_people",
            {
                "project_id": project,
                "connections": True,
                "people": [{"linkedin_url": "linkedin.com/in/dan-ek", "name": "Dan Ek"}],
            },
        )
    )
    connections = structured(
        await server.call_tool("list_people", {"project_id": project, "segment": "connections"})
    )
    assert [p["name"] for p in connections["people"]] == ["Dan Ek"]
    assert connections["people"][0]["linkedin"] == "connected"
    assert mine["added"] == 1

    changed = structured(
        await server.call_tool(
            "tag_people",
            {"project_id": project, "person_ids": [ada], "add": ["VIP"], "remove": ["Wave 2"]},
        )
    )
    assert changed == {"added": 1, "removed": 1}
    shown = structured(
        await server.call_tool("get_person", {"project_id": project, "person_id": ada})
    )
    assert shown["tags"] == ["VIP", "Wave 1"]
    with pytest.raises(ToolError, match="short label"):
        await server.call_tool(
            "tag_people", {"project_id": project, "person_ids": [ada], "add": ["x" * 61]}
        )

    # Forgetting someone takes their tags too.
    async with f.db.pool.acquire() as conn:
        assert await records.forget(conn, f.project.id, UUID(ada))
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM record_tags WHERE record_id=$1", UUID(ada))
        == 0
    )


async def test_a_merge_keeps_both_records_tags(collection_db):
    f = await fixture(collection_db)
    async with f.db.pool.acquire() as conn:
        one = (
            await records.add_person(
                conn, f.project.id, USER, {"linkedin_url": "linkedin.com/in/ada"}, tags=["A"]
            )
        )["id"]
        two = (
            await records.add_person(
                conn, f.project.id, USER, {"email": "ada@acme.dev"}, tags=["A", "B"]
            )
        )["id"]
        # One record learns both: they're the same person.
        merged = await records.upsert_person(
            conn,
            f.project.id,
            {"linkedin_slug": "ada", "email": "ada@acme.dev"},
            source=records.ADD_SOURCE,
        )
    assert merged == one and merged != two
    tags = await f.db.pool.fetch("SELECT record_id, tag FROM record_tags ORDER BY tag")
    assert [(row["record_id"], row["tag"]) for row in tags] == [(one, "A"), (one, "B")]


async def test_a_request_to_someone_known_by_their_link_looks_up_their_id_first(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    # A read that asks nobody: the member's LinkedIn is connected and read in the cloud.
    await ran(f, [person(3, headline="Designer")])
    async with client(f) as http:
        await turn_on(http, f)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    added = structured(
        await server.call_tool(
            "add_people",
            {
                "project_id": project,
                "tags": ["Wave 1"],
                "people": [
                    {"linkedin_url": "linkedin.com/in/ada-yilmaz", "name": "Ada Yilmaz"},
                    {"linkedin_url": "linkedin.com/in/gone-person", "name": "Gone"},
                ],
            },
        )
    )
    ada, gone = added["person_ids"]
    listed = structured(
        await server.call_tool("list_people", {"project_id": project, "tag": "Wave 1"})
    )
    assert {p["name"]: p["can_request"] for p in listed["people"]} == {
        "Ada Yilmaz": True,
        "Gone": True,
    }
    sent = structured(
        await server.call_tool(
            "send_connection_requests",
            {"project_id": project, "person_ids": [ada, gone], "request_id": str(uuid4())},
        )
    )
    assert sent["queued"] == 2
    rows = await f.db.pool.fetch("SELECT target, slug FROM linkedin_requests ORDER BY queued_at")
    assert [(r["target"], r["slug"]) for r in rows] == [
        (None, "ada-yilmaz"),
        (None, "gone-person"),
    ]

    monkeypatch.setattr(linkedin_requests_activities, "datetime", Clock(MONDAY)())
    cloud = LinkCloud(f.db, ids={"ada-yilmaz": member(7)})
    acts = sender(f, cloud)
    run_id = await requests_run(f, sent["batch_id"])
    await acts.prepare(run_id)
    step = await acts.next(run_id)
    first = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert first["outcome"] == "sent"
    assert [c["kind"] for c in cloud.calls] == ["linkedin_profile_ids", "linkedin_invite"]
    assert cloud.calls[1]["member"] == member(7)
    # The ID is kept on the person and the request.
    assert await f.db.pool.fetchval(
        "SELECT value FROM record_identifiers WHERE record_id=$1 AND kind='linkedin_member'",
        UUID(ada),
    ) == member(7)
    assert await f.db.pool.fetchval(
        "SELECT target FROM linkedin_requests WHERE record_id=$1", UUID(ada)
    ) == member(7)
    # A retried send asks LinkedIn nothing more.
    again = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert again["outcome"] == "sent" and len(cloud.calls) == 2

    # Nobody at the other link: nothing is sent.
    clock = Clock(MONDAY.replace(hour=11))
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    step = await acts.next(run_id)
    second = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert second["outcome"] == "unknown_person"
    assert [c["kind"] for c in cloud.calls][2:] == ["linkedin_profile_ids"]

    # Ada accepts: the member's connections export finds her in their list.
    async with f.db.pool.acquire() as conn, conn.transaction():
        await export.record_connections(
            conn,
            project_id=f.project.id,
            run_id=UUID(run_id),
            member=USER,
            connections=[
                {
                    "member": member(7),
                    "slug": "ada-yilmaz",
                    "first_name": "Ada",
                    "last_name": "Yilmaz",
                    "headline": "",
                    "connected_at": datetime.now(UTC).isoformat(),
                }
            ],
        )
    assert (
        await f.db.pool.fetchval(
            "SELECT status FROM linkedin_requests WHERE record_id=$1", UUID(ada)
        )
        == "accepted"
    )
    results = structured(
        await server.call_tool("connection_request_results", {"project_id": project})
    )
    assert results["groups"] == [
        {
            "tag": "Wave 1",
            "waiting": 0,
            "went_out": 1,
            "accepted": 1,
            "pending": 0,
            "not_asked": 1,
            "uncertain": 0,
            "acceptance_rate": 1.0,
        }
    ]


async def test_tags_are_capped_per_call_and_per_person(collection_db, monkeypatch):
    f = await fixture(collection_db)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    many = [f"t{i}" for i in range(records.TAGS_PER_PERSON + 1)]
    with pytest.raises(ToolError, match="up to 20 tags"):
        await server.call_tool(
            "add_people",
            {"project_id": project, "people": [{"email": "a@acme.dev"}], "tags": many},
        )
    added = structured(
        await server.call_tool(
            "add_people",
            {"project_id": project, "people": [{"email": "a@acme.dev", "tags": many}]},
        )
    )
    assert added["skipped"] == [{"index": 0, "reason": "more than 20 tags"}]
    one = structured(
        await server.call_tool(
            "add_people",
            {"project_id": project, "people": [{"email": "a@acme.dev"}], "tags": many[:15]},
        )
    )["person_ids"][0]
    # Ten more fit only five: a person keeps at most 20.
    changed = structured(
        await server.call_tool(
            "tag_people", {"project_id": project, "person_ids": [one], "add": many[10:]}
        )
    )
    assert changed == {"added": 5, "removed": 0}
    assert await f.db.pool.fetchval("SELECT count(*) FROM record_tags") == 20


async def test_a_lookup_that_finds_a_customer_sends_nothing(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await ran(f, [person(3, headline="Designer")])
    async with client(f) as http:
        await turn_on(http, f)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    # A customer Tin knows by their profile ID, and the same person added again by link.
    async with f.db.pool.acquire() as conn:
        customer = await records.upsert_person(
            conn, f.project.id, {"linkedin_member": member(8)}, source=records.ADD_SOURCE
        )
        await records.set_by_member(
            conn, f.project.id, customer, [records.Value("customer", "yes")], member=USER
        )
    added = structured(
        await server.call_tool(
            "add_people",
            {"project_id": project, "people": [{"linkedin_url": "linkedin.com/in/same-one"}]},
        )
    )
    sent = structured(
        await server.call_tool(
            "send_connection_requests",
            {
                "project_id": project,
                "person_ids": added["person_ids"],
                "request_id": str(uuid4()),
            },
        )
    )
    monkeypatch.setattr(linkedin_requests_activities, "datetime", Clock(MONDAY)())
    cloud = LinkCloud(f.db, ids={"same-one": member(8)})
    acts = sender(f, cloud)
    run_id = await requests_run(f, sent["batch_id"])
    await acts.prepare(run_id)
    step = await acts.next(run_id)
    answer = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    # The lookup merged them with the customer: nothing is sent.
    assert answer["outcome"] == "skip"
    assert [c["kind"] for c in cloud.calls] == ["linkedin_profile_ids"]
    assert await f.db.pool.fetchval("SELECT status FROM linkedin_requests") == "declined"
