"""People records: recognizing a person across reads, values with lineage, the timeline."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from test_procedure_publication import publication_db as publication_db

from tin_lite import people_records as records
from tin_lite.people_records import RecordError, Value

MEMBER = "ACoAAB" + "x" * 30
OTHER = "ACoAAC" + "y" * 30
RUN = "run-" + "1" * 8


async def project(db) -> UUID:
    created = await db.create_project(name="Synthetic people", state_repo_id="p/" + uuid4().hex)
    async with db.pool.acquire() as conn:
        await records.ensure_fields(conn, created.id)
    return created.id


def test_linkedin_identifiers_name_the_member_or_the_slug():
    assert records.linkedin_identifiers(f"https://www.linkedin.com/in/{MEMBER}/") == {
        "linkedin_member": MEMBER
    }
    assert records.linkedin_identifiers(f"urn:li:fsd_profile:{MEMBER}") == {
        "linkedin_member": MEMBER
    }
    assert records.linkedin_identifiers("https://linkedin.com/in/Jane-Doe-42") == {
        "linkedin_slug": "jane-doe-42"
    }
    assert records.linkedin_identifiers("https://www.linkedin.com/in/j%C3%B6rg") == {
        "linkedin_slug": "jörg"
    }
    assert records.profile_link({"linkedin_slug": "jörg", "linkedin_member": MEMBER}) == (
        "https://www.linkedin.com/in/j%C3%B6rg"
    )
    for bad in (
        "http://www.linkedin.com/in/jane",
        "https://evil.example/in/jane",
        "https://www.linkedin.com/company/tin",
        "https://www.linkedin.com:8443/in/jane",
        "urn:li:fsd_profile:not-a-member",
        "",
    ):
        with pytest.raises(RecordError):
            records.linkedin_identifiers(bad)


async def test_a_person_is_recognized_by_any_identifier(publication_db):
    project_id = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        first = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        again = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER, "linkedin_slug": "Jane"}, source="test"
        )
        by_slug = await records.upsert_person(
            conn, project_id, {"linkedin_slug": "jane"}, source="test"
        )
        listed = await records.people(conn, project_id)
    assert first == again == by_slug
    assert [person["identifiers"] for person in listed] == [
        {"linkedin_member": MEMBER, "linkedin_slug": "jane"}
    ]
    assert listed[0]["profile_url"] == "https://www.linkedin.com/in/jane"


async def test_two_records_merge_when_one_read_names_both(publication_db):
    project_id = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        older = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        newer = await records.upsert_person(
            conn, project_id, {"linkedin_slug": "jane"}, source="test"
        )
        assert older != newer
        await records.set_values(
            conn,
            project_id,
            older,
            [Value("name", "Jane Doe")],
            set_by_kind="run",
            set_by=RUN,
        )
        await records.set_values(
            conn,
            project_id,
            newer,
            [Value("name", "J. Doe"), Value("headline", "Founder at Example")],
            set_by_kind="run",
            set_by=RUN,
        )
        await records.add_event(
            conn, project_id, newer, kind="commented", event_key="linkedin:commented:1:jane"
        )
        merged = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER, "linkedin_slug": "jane"}, source="test"
        )
        assert merged == older
        assert await records.current(conn, project_id, newer) == older
        listed = await records.people(conn, project_id)
        # A write naming the merged record lands on the survivor.
        assert await records.add_event(
            conn, project_id, newer, kind="reacted", event_key="linkedin:reacted:1:jane"
        )
        after = await records.people(conn, project_id)
    assert len(listed) == 1
    assert listed[0]["fields"]["name"]["text"] == "Jane Doe"
    assert listed[0]["fields"]["headline"]["text"] == "Founder at Example"
    assert listed[0]["events"] == {"commented": 1}
    assert after[0]["events"] == {"commented": 1, "reacted": 1}


async def test_each_member_keeps_their_own_relationship_through_a_merge(publication_db):
    project_id = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        older = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        newer = await records.upsert_person(
            conn, project_id, {"linkedin_slug": "jane"}, source="test"
        )
        assert await records.set_relationship(
            conn, project_id, older, "user_a", "none", set_by=RUN, reason="2nd on LinkedIn"
        )
        await records.set_relationship(conn, project_id, newer, "user_a", "connected", set_by=RUN)
        await records.set_relationship(conn, project_id, newer, "user_b", "pending", set_by=RUN)
        # The same read again changes nothing; a reaction's degree can't undo an invitation.
        assert not await records.set_relationship(
            conn, project_id, older, "user_a", "none", set_by=RUN, reason="2nd on LinkedIn"
        )
        assert not await records.set_relationship(
            conn, project_id, newer, "user_b", "none", set_by=RUN, keep_pending=True
        )
        with pytest.raises(RecordError):
            await records.set_relationship(conn, project_id, older, "user_a", "friends", set_by=RUN)
        await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER, "linkedin_slug": "jane"}, source="test"
        )
        a = await records.relationships(conn, project_id, "user_a")
        b = await records.relationships(conn, project_id, "user_b")
        assert await records.relationships(conn, project_id, "user_c") == {}
        assert await records.forget(conn, project_id, older)
        left = await conn.fetchval("SELECT count(*) FROM record_relationships")
    # The survivor keeps its own; one only the merged record had moves over.
    assert {key: (v["state"], v["reason"]) for key, v in a.items()} == {
        older: ("none", "2nd on LinkedIn")
    }
    assert {key: v["state"] for key, v in b.items()} == {older: "pending"}
    assert left == 0


async def test_values_keep_lineage_and_a_members_edit(publication_db):
    project_id = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        person = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        write = [
            Value("name", "  Jane   Doe "),
            Value("icp", "Software founders", "Founder of a devtools startup"),
            Value("icp_fit", 0.82, "Founder of a devtools startup"),
            Value("linkedin_relationship", "none"),
        ]
        assert (
            await records.set_values(
                conn, project_id, person, write, set_by_kind="run", set_by=RUN, definition="icp-1"
            )
            == 4
        )
        # The same write again changes nothing.
        assert (
            await records.set_values(
                conn, project_id, person, write, set_by_kind="run", set_by=RUN, definition="icp-1"
            )
            == 0
        )
        # A new ICP definition is a change, even with the same score.
        assert (
            await records.set_values(
                conn,
                project_id,
                person,
                [Value("icp_fit", 0.82, "Founder of a devtools startup")],
                set_by_kind="run",
                set_by=RUN,
                definition="icp-2",
            )
            == 1
        )
        await records.set_values(
            conn, project_id, person, [Value("name", "Jane")], set_by_kind="member", set_by="u1"
        )
        assert (
            await records.set_values(
                conn, project_id, person, [Value("name", "Jane Doe")], set_by_kind="run", set_by=RUN
            )
            == 0
        )
        # An empty value says why it is empty.
        await records.set_values(
            conn,
            project_id,
            person,
            [Value("headline", None, "LinkedIn showed no headline")],
            set_by_kind="run",
            set_by=RUN,
        )
        for bad in (
            Value("linkedin_relationship", "friends"),
            Value("icp_fit", "high"),
            Value("icp_fit", float("nan")),
            Value("icp_fit", True),
            Value("favourite_colour", "blue"),
        ):
            with pytest.raises(RecordError):
                await records.set_values(
                    conn, project_id, person, [bad], set_by_kind="run", set_by=RUN
                )
        fields = (await records.people(conn, project_id))[0]["fields"]
    assert fields["name"].pop("set_at")
    # A member's value keeps the run's value it replaced.
    replaced = fields["name"].pop("previous")
    assert replaced.pop("set_at")
    assert replaced == {
        "text": "Jane Doe",
        "number": None,
        "reason": "",
        "set_by": RUN,
        "definition": "icp-1",
    }
    assert fields["name"] == {
        "text": "Jane",
        "number": None,
        "reason": "",
        "definition": "",
        "set_by_kind": "member",
        "set_by": "u1",
    }
    assert fields["icp_fit"]["previous"] is None
    assert fields["icp_fit"]["number"] == 0.82 and fields["icp_fit"]["definition"] == "icp-2"
    assert fields["headline"]["text"] is None
    assert fields["headline"]["reason"] == "LinkedIn showed no headline"


async def test_events_are_added_once_per_key(publication_db):
    project_id = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        person = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        key = f"linkedin:reacted:urn:li:ugcPost:1:{MEMBER}"
        assert await records.add_event(
            conn, project_id, person, kind="reacted", event_key=key, detail={"reaction": "LIKE"}
        )
        assert not await records.add_event(conn, project_id, person, kind="reacted", event_key=key)
        with pytest.raises(RecordError):
            await records.add_event(conn, project_id, person, kind="liked", event_key="x")
        listed = await records.people(conn, project_id)
    assert listed[0]["events"] == {"reacted": 1}


async def test_a_forgotten_person_never_comes_back(publication_db):
    project_id = await project(publication_db)
    elsewhere = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        person = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        merged = await records.upsert_person(
            conn, project_id, {"linkedin_slug": "jane"}, source="test"
        )
        await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER, "linkedin_slug": "jane"}, source="test"
        )
        await records.add_event(conn, project_id, merged, kind="commented", event_key="c1")
        kept = await records.upsert_person(
            conn, project_id, {"linkedin_member": OTHER}, source="test"
        )
        assert await records.forget(conn, project_id, merged)
        assert not await records.forget(conn, project_id, person)
        assert (
            await records.upsert_person(conn, project_id, {"linkedin_slug": "Jane"}, source="t")
            is None
        )
        assert (
            await records.upsert_person(
                conn, project_id, {"linkedin_member": MEMBER, "linkedin_slug": "new"}, source="t"
            )
            is None
        )
        # Forgetting is per project: another project still recognizes them.
        assert await records.upsert_person(
            conn, elsewhere, {"linkedin_member": MEMBER}, source="test"
        )
        listed = await records.people(conn, project_id)
        stored = await conn.fetchval(
            "SELECT count(*) FROM record_suppressions WHERE project_id = $1", project_id
        )
        leftovers = await conn.fetchval(
            "SELECT count(*) FROM record_events WHERE project_id = $1", project_id
        )
    assert [person["id"] for person in listed] == [kept]
    assert stored == 2 and leftovers == 0


async def test_deleting_the_project_purges_its_people(publication_db):
    project_id = await project(publication_db)
    async with publication_db.pool.acquire() as conn:
        person = await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER}, source="test"
        )
        await records.upsert_person(conn, project_id, {"linkedin_slug": "jane"}, source="test")
        await records.upsert_person(
            conn, project_id, {"linkedin_member": MEMBER, "linkedin_slug": "jane"}, source="test"
        )
        await records.set_values(
            conn, project_id, person, [Value("name", "Jane")], set_by_kind="run", set_by=RUN
        )
        await records.add_event(conn, project_id, person, kind="reacted", event_key="r1")
        await records.set_relationship(conn, project_id, person, "user_a", "none", set_by=RUN)
        await records.upsert_person(conn, project_id, {"linkedin_member": OTHER}, source="test")
        other = await records.upsert_person(
            conn, project_id, {"linkedin_member": OTHER}, source="test"
        )
        await records.forget(conn, project_id, other)
    async with publication_db.pool.acquire() as conn, conn.transaction():
        await publication_db.purge_project_data(conn, project_id=project_id)
    counts = await publication_db.pool.fetchrow(
        """SELECT (SELECT count(*) FROM records WHERE project_id = $1) AS records,
                  (SELECT count(*) FROM record_identifiers WHERE project_id = $1) AS identifiers,
                  (SELECT count(*) FROM record_values WHERE project_id = $1) AS values,
                  (SELECT count(*) FROM record_events WHERE project_id = $1) AS events,
                  (SELECT count(*) FROM record_relationships WHERE project_id = $1)
                      AS relationships,
                  (SELECT count(*) FROM record_fields WHERE project_id = $1) AS fields,
                  (SELECT count(*) FROM record_suppressions WHERE project_id = $1) AS hashes""",
        project_id,
    )
    assert dict(counts) == {
        "records": 0,
        "identifiers": 0,
        "values": 0,
        "events": 0,
        "relationships": 0,
        "fields": 0,
        "hashes": 0,
    }
