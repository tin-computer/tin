"""Exporting a founder's own LinkedIn connections: the reads, People, positions and the CSV.

LinkedIn's answers copy the structure of its normalized Voyager JSON (entities by URN in
`included`, the list's order in `*elements`); every name and position here is made up. The
founder's saved session is a real one from the collection fixtures; the sandbox is synthetic.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_connection_collection import ACTOR, USER, session_envelope
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagement import me, no_wait
from test_linkedin_engagers import fixture

from tin_lite import linkedin_connections as reader
from tin_lite import linkedin_connections_export as export
from tin_lite import people_records as records
from tin_lite.connection_collection import CollectionError
from tin_lite.integrations import IntegrationAuthorizationError, IntegrationError
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_connections_activities import LinkedInConnectionsActivities
from tin_lite.linkedin_session import validate_session
from tin_lite.workflows import LinkedInConnectionsWorkflow

NOW = datetime(2026, 11, 23, 9, tzinfo=UTC)


def member(index: int) -> str:
    return f"ACoAAFriend{index:04d}" + "x" * 21


def millis(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def profile(index: int, first: str, last: str, *, headline="Founder", slug=None) -> dict:
    return {
        "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
        "entityUrn": f"urn:li:fsd_profile:{member(index)}",
        "firstName": first,
        "lastName": last,
        "headline": headline,
        "publicIdentifier": slug or f"{first.lower()}-{last.lower()}",
    }


def connection(index: int, when: datetime) -> dict:
    urn = f"urn:li:fsd_profile:{member(index)}"
    return {
        "$type": "com.linkedin.voyager.dash.relationships.Connection",
        "entityUrn": f"urn:li:fsd_connection:{member(index)}",
        "createdAt": millis(when),
        "connectedMember": urn,
        "*connectedMemberResolutionResult": urn,
    }


def page(entries: list[tuple[dict, dict | None]], *, total=None, extra=()) -> dict:
    """A page of the connections list: each connection, and its profile when LinkedIn sends
    one."""

    included = [c for c, _ in entries] + [p for _, p in entries if p] + list(extra)
    data = {"*elements": [c["entityUrn"] for c, _ in entries]}
    if total is not None:
        data["paging"] = {"start": 0, "count": 40, "total": total}
    return {"data": data, "included": included}


def position(index: int, n: int, *, company, title, start=(2024, 1), end=None, linked=None):
    item = {
        "$type": "com.linkedin.voyager.dash.identity.profile.Position",
        "entityUrn": f"urn:li:fsd_profilePosition:({member(index)},{n})",
        "title": title,
        "dateRange": {
            "start": {"year": start[0], "month": start[1]},
            **({"end": {"year": end[0], "month": end[1]}} if end else {}),
        },
    }
    if linked:
        item["*company"] = linked
    else:
        item["companyName"] = company
    return item


# The reader.


def test_a_page_of_connections_is_read_in_linkedins_order():
    first, second = NOW - timedelta(days=1), NOW - timedelta(days=400)
    payload = page(
        [
            (connection(1, first), profile(1, "Ada", "Yilmaz", headline="Head of Growth at Acme")),
            (connection(2, second), profile(2, "Bob", "Builder", slug="Bob-B")),
            # A closed account: LinkedIn lists the connection, with no profile behind it.
            (connection(3, second), None),
        ],
        total=3,
    )
    found, listed, total, unnamed = reader.connections_page(payload)
    assert listed == 3 and total == 3 and unnamed == 1
    assert [c["member"] for c in found] == [member(1), member(2)]
    assert found[0] == {
        "member": member(1),
        "slug": "ada-yilmaz",
        "first_name": "Ada",
        "last_name": "Yilmaz",
        "headline": "Head of Growth at Acme",
        "connected_at": first.isoformat(),
    }
    assert found[1]["slug"] == "Bob-B"


def test_a_page_tin_cant_read_is_refused_not_guessed():
    # Plausible, but unusable: the list has entries and none of them is a connection Tin reads.
    unusable = {"data": {"*elements": ["urn:li:x:1"]}, "included": [{"entityUrn": "urn:li:x:1"}]}
    for payload in (unusable, {"data": {"paging": {}}}, []):
        with pytest.raises(CollectionError) as error:
            reader.connections_page(payload)
        assert error.value.code == "unsupported_contract"
    # An empty list is just empty.
    assert reader.connections_page(page([])) == ([], 0, None, 0)


def test_a_profiles_current_position_is_the_latest_that_hasnt_ended():
    company = "urn:li:fsd_company:42"
    payload = {
        "data": {},
        "included": [
            {
                **profile(1, "Ada", "Yilmaz", headline="Building Acme"),
                "geoLocation": {"*geo": "urn:li:fsd_geo:1"},
            },
            {"entityUrn": "urn:li:fsd_geo:1", "defaultLocalizedName": "Istanbul, Türkiye"},
            {"entityUrn": company, "name": "Acme Robotics"},
            position(1, 1, company="Old Co", title="Engineer", start=(2018, 1), end=(2021, 5)),
            position(1, 2, company="", title="Advisor", start=(2022, 3)),
            position(1, 3, company=None, title="Co-founder", start=(2024, 6), linked=company),
            # Someone else's position in the same answer is never theirs.
            position(2, 4, company="Elsewhere", title="CEO", start=(2026, 1)),
        ],
    }
    assert reader.profile_position(payload, member(1)) == {
        "member": member(1),
        "found": True,
        "company": "Acme Robotics",
        "position": "Co-founder",
        "location": "Istanbul, Türkiye",
        "headline": "Building Acme",
    }
    # A profile with no current position says so; no profile at all isn't found.
    bare = {"data": {}, "included": [profile(3, "Cem", "Kaya")]}
    assert reader.profile_position(bare, member(3))["position"] == ""
    assert reader.profile_position({"data": {}}, member(4))["found"] is False


def responder(pages: dict[int, dict], seen: list, profiles: dict[str, dict] | None = None):
    def respond(req):
        seen.append(req)
        if req.url.path == "/voyager/api/me":
            return httpx.Response(200, json=me())
        assert req.method == "GET"
        if req.url.path == "/voyager/api/identity/dash/profiles":
            assert req.url.params["decorationId"] == reader.FULL_PROFILE
            return httpx.Response(200, json=profiles[req.url.params["memberIdentity"]])
        assert req.url.path == reader.PATH
        assert req.url.params["decorationId"] == reader.DECORATION
        assert req.url.params["sortType"] == "RECENTLY_ADDED"
        assert req.headers["referer"] == reader.REFERER
        return httpx.Response(200, json=pages[int(req.url.params["start"])])

    return respond


def full_page(start: int, n: int = reader.PAGE_SIZE) -> dict:
    return page(
        [
            (connection(i, NOW - timedelta(days=i)), profile(i, f"P{i}", "Person"))
            for i in range(start, start + n)
        ]
    )


async def test_a_read_pages_to_the_end_of_the_list():
    seen = []
    pages = {0: full_page(0), 40: full_page(40), 80: full_page(80, 7)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(pages, seen))) as client:
        result = await reader.read_connections(
            validate_session(session_envelope()), ACTOR, client=client, now=NOW, sleep=no_wait
        )
    parsed = reader.ConnectionsResult.model_validate(result)
    assert parsed.pages == 3 and parsed.next is None and len(parsed.connections) == 87
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == ["me"] + ["connections"] * 3


async def test_a_read_stops_at_its_pages_and_says_where_to_go_on():
    seen = []
    pages = {start: full_page(start) for start in range(0, 200, 40)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(pages, seen))) as client:
        result = await reader.read_connections(
            validate_session(session_envelope()),
            ACTOR,
            client=client,
            start=40,
            pages=2,
            sleep=no_wait,
        )
    assert result["pages"] == 2 and result["next"] == 120
    assert [int(r.url.params["start"]) for r in seen[1:]] == [40, 80]


async def test_positions_are_read_one_profile_at_a_time():
    seen = []
    profiles = {
        member(1): {
            "data": {},
            "included": [profile(1, "Ada", "Yilmaz"), position(1, 1, company="Acme", title="CTO")],
        },
        member(2): {"data": {}, "included": []},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder({}, seen, profiles))
    ) as client:
        result = await reader.read_positions(
            validate_session(session_envelope()),
            ACTOR,
            client=client,
            members=[member(1), member(2)],
            sleep=no_wait,
        )
    parsed = reader.PositionsResult.model_validate(result)
    assert [(p.member, p.found, p.company, p.position) for p in parsed.people] == [
        (member(1), True, "Acme", "CTO"),
        (member(2), False, "", ""),
    ]
    with pytest.raises(CollectionError):
        await reader.read_positions(
            validate_session(session_envelope()), ACTOR, client=client, members=["not-an-id"]
        )


@pytest.mark.parametrize(
    ("status", "code"), [(401, "session_expired"), (429, "rate_limited"), (403, "access_denied")]
)
async def test_linkedin_refusals_stop_the_read(status, code):
    def respond(req):
        if req.url.path == "/voyager/api/me":
            return httpx.Response(200, json=me())
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CollectionError) as error:
            await reader.read_connections(
                validate_session(session_envelope()), ACTOR, client=client, sleep=no_wait
            )
    assert error.value.code == code


# The workflow's activities, with a synthetic sandbox.


def friend(index, first, last, *, days, headline="Founder at Startup", slug=None):
    return {
        "member": member(index),
        "slug": slug or f"{first.lower()}-{last.lower()}",
        "first_name": first,
        "last_name": last,
        "headline": headline,
        "connected_at": (NOW - timedelta(days=days)).isoformat(),
    }


class Cloud:
    """The LinkedIn sandbox: the member's connections, page by page, and their profiles."""

    def __init__(self, connections, *, positions=None, per_read=2, errors=None):
        self.connections, self.positions = connections, positions or {}
        self.per_read, self.errors = per_read, errors or {}
        self.calls, self.cleaned = [], []

    async def engagement(self, conn, *, execution_key, run_id, session, packet, seconds=270):
        assert session["cookies"] and packet["actor"]["key"]
        self.calls.append(packet)
        if packet["kind"] in self.errors:
            raise CollectionError(self.errors[packet["kind"]])
        if packet["kind"] == "linkedin_positions":
            return {
                "people": [
                    {
                        "member": m,
                        "found": m in self.positions,
                        "company": self.positions.get(m, ("", ""))[0],
                        "position": self.positions.get(m, ("", ""))[1],
                        "location": "Istanbul" if m in self.positions else "",
                        "headline": "",
                    }
                    for m in packet["members"]
                ],
                "read_at": NOW.isoformat(),
            }
        assert packet["kind"] == "linkedin_connections"
        start = packet["start"]
        taken = self.connections[start : start + self.per_read]
        after = start + len(taken)
        return {
            "connections": taken,
            "pages": 1,
            "next": after if after < len(self.connections) else None,
            "total": len(self.connections),
            "unnamed": 1 if start == 0 else 0,
            "read_at": NOW.isoformat(),
        }

    async def cleanup(self, sandbox_id):
        self.cleaned.append(sandbox_id)


async def export_run(f, *, by=None):
    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", export.KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await f.db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','c.json',$3,'1','{}')""",
            workflow_id,
            export.KEY,
            "c" * 40,
        )
    run_id = uuid4()
    await f.db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,temporal_workflow_id,
            thread_id,generation,fencing_token,status,lease_active,started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        f.project.id,
        workflow_id,
        export.KEY,
        "c" * 40,
        f"{export.KEY}:{run_id}",
        str(run_id),
        by or USER,
        json.dumps({"project_id": str(f.project.id)}),
    )
    return str(run_id)


def work(f, cloud):
    return LinkedInConnectionsActivities(
        database=f.db,
        storage=f.storage,
        settings=f.store.settings,
        cipher=f.cipher,
        cloud=cloud,
        router=None,
    )


async def run_through(w, run_id):
    """What the Temporal workflow does, called directly."""

    await w.connections_prepare(run_id)
    notes = {"people": 0, "new": 0, "unnamed": 0, "positions": 0, "placed": 0, "reads": 0}
    for index in range(export.LIST_READS):
        read = await w.connections_read({"run_id": run_id, "read": index})
        notes["reads"] += 1
        notes["people"] += read["people"] or 0
        notes["new"] += read["new"] or 0
        notes["unnamed"] += read["unnamed"] or 0
        if read.get("joined"):
            notes["joined"] = notes.get("joined", 0) + read["joined"]
        if read.get("caught_up"):
            notes["caught_up"] = True
        if read["outcome"] == "stopped":
            notes["stopped"] = read["reason"]
            break
        if read["outcome"] != "read" or not read["more"]:
            break
    if not notes.get("stopped"):
        for index in range(export.POSITION_READS):
            read = await w.connections_positions({"run_id": run_id, "read": index})
            notes["positions"] += read["read"] or 0
            notes["placed"] += read["found"] or 0
            if read["outcome"] == "stopped":
                notes["stopped"] = read["reason"]
                break
            if read["outcome"] != "read" or not read["more"]:
                break
    await w.connections_publish({"run_id": run_id, "notes": notes})
    return notes


def listed(storage):
    tree = storage.repo.trees[storage.repo.head]
    rows = list(csv.DictReader(io.StringIO(tree[export.CSV_PATH][1].decode())))
    return rows, tree[export.SUMMARY_PATH][1].decode()


def sample():
    return Cloud(
        [
            friend(1, "Ada", "Yilmaz", days=1, headline="Head of Growth at Acme"),
            friend(2, "Bob", "Builder", days=30),
            friend(3, "Cem", "Kaya", days=900, headline="=HYPERLINK(evil)"),
        ],
        positions={member(1): ("Acme Robotics", "Head of Growth"), member(2): ("", "")},
    )


async def test_a_run_exports_every_connection_with_their_position(collection_db):
    f = await fixture(collection_db)
    cloud = sample()
    run_id = await export_run(f)
    notes = await run_through(work(f, cloud), run_id)
    assert notes["people"] == 3 and notes["new"] == 3 and notes["reads"] == 2
    assert notes["positions"] == 2 and notes["placed"] == 1 and notes["unnamed"] == 1
    # The second read starts where the first stopped.
    reads = [c["start"] for c in cloud.calls if c["kind"] == "linkedin_connections"]
    assert reads == [0, 2]
    # Never-read profiles, newest connection first.
    asked = [c["members"] for c in cloud.calls if c["kind"] == "linkedin_positions"]
    assert asked == [[member(1), member(2), member(3)]]

    people = {p["fields"]["name"]["text"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert set(people) == {"Ada Yilmaz", "Bob Builder", "Cem Kaya"}
    ada = people["Ada Yilmaz"]
    assert ada["identifiers"] == {"linkedin_member": member(1), "linkedin_slug": "ada-yilmaz"}
    assert ada["events"] == {"connected": 1}
    assert ada["fields"]["company"]["text"] == "Acme Robotics"
    assert ada["fields"]["position"]["text"] == "Head of Growth"
    assert ada["fields"]["location"]["text"] == "Istanbul"
    # LinkedIn showed Bob no current position: read, and said so, not guessed.
    assert people["Bob Builder"]["fields"]["position"]["text"] is None
    assert people["Bob Builder"]["fields"]["position"]["reason"] == export.NO_POSITION
    mine = await records.relationships(f.db.pool, f.project.id, USER)
    assert {mine[p["id"]]["state"] for p in people.values()} == {"connected"}
    when = await f.db.pool.fetchval(
        "SELECT occurred_at FROM record_events WHERE record_id=$1 AND kind='connected'",
        people["Cem Kaya"]["id"],
    )
    assert when == NOW - timedelta(days=900)

    rows, summary = listed(f.storage)
    assert list(rows[0]) == list(export.COLUMNS)
    assert [r["First Name"] for r in rows] == ["Ada", "Bob", "Cem"]
    assert rows[0] == {
        "First Name": "Ada",
        "Last Name": "Yilmaz",
        "URL": "https://www.linkedin.com/in/ada-yilmaz",
        "Company": "Acme Robotics",
        "Position": "Head of Growth",
        # LinkedIn's own format in Connections.csv.
        "Connected On": f"{NOW - timedelta(days=1):%d %b %Y}",
        "Headline": "Head of Growth at Acme",
        "Location": "Istanbul",
        "Profile read": rows[0]["Profile read"],
    }
    assert rows[0]["Profile read"] and rows[2]["Profile read"] == ""
    # A spreadsheet never runs a cell as a formula.
    assert rows[2]["Headline"] == "'=HYPERLINK(evil)"
    assert "3 connections in the list." in summary
    assert "Company and position read for 2: 1 show a current position, 1 show none" in summary
    assert "1 still to read." in summary and "LinkedIn listed without a profile" in summary
    state = await f.db.pool.fetchrow(
        "SELECT status, artifact_path, result_summary FROM workflow_runs WHERE id=$1",
        UUID(run_id),
    )
    assert state["status"] == "succeeded" and state["artifact_path"] == export.CSV_PATH
    assert state["result_summary"] == (
        "3 LinkedIn connections read; company and position read for 2 profiles this run."
    )
    # No receipt keeps anyone's name once their page is written.
    kept = await f.db.pool.fetch(
        "SELECT result::text AS result FROM effect_receipts WHERE operation=$1",
        ENGAGEMENT_OPERATION,
    )
    assert kept and not any(name in row["result"] for name in ("Ada", "Yilmaz") for row in kept)

    # Everyone listed on the first run is the member's existing network: a connection, not a
    # lead.
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM record_events WHERE project_id=$1 AND kind='new_connection'",
            f.project.id,
        )
        == 0
    )
    # The first run read the list to the end; Tin's first read is the baseline.
    await f.db.pool.execute(
        "UPDATE linkedin_connection_lists SET baseline_at=$2 WHERE project_id=$1",
        f.project.id,
        NOW - timedelta(hours=12),
    )

    # The next day: one new connection. The run reads only until it reaches connections it
    # has, nobody is counted twice, and only the profiles not yet read are read.
    cloud.calls.clear()
    cloud.connections.insert(0, friend(4, "Deniz", "Ak", days=0))
    cloud.positions[member(3)] = ("Kaya Labs", "Founder")
    again = await export_run(f)
    notes = await run_through(work(f, cloud), again)
    assert notes["reads"] == 1 and notes["caught_up"] is True
    assert notes["people"] == 2 and notes["new"] == 1 and notes["joined"] == 1
    assert [c["start"] for c in cloud.calls if c["kind"] == "linkedin_connections"] == [0]
    asked = [c["members"] for c in cloud.calls if c["kind"] == "linkedin_positions"]
    assert asked == [[member(4), member(3)]]
    # Deniz connected after the baseline: a new connection, listed with everyone who engaged.
    # Ada was already a connection and stays under Connections.
    listed_people = {
        p["name"]: p for p in await records.directory(f.db.pool, f.project.id, viewer=USER)
    }
    assert listed_people["Deniz Ak"]["imported"] is False
    assert listed_people["Deniz Ak"]["social"] is True
    assert listed_people["Ada Yilmaz"]["imported"] is True
    assert (
        await f.db.pool.fetchval(
            "SELECT occurred_at FROM record_events WHERE project_id=$1 AND kind='new_connection'",
            f.project.id,
        )
        == NOW
    )
    assert await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(again)
    ) == (
        # LinkedIn answered for Cem's profile, not Deniz's.
        "1 new LinkedIn connection since the last run; company and position read for 1 "
        "profiles this run."
    )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM record_events WHERE project_id=$1 AND kind='connected'",
            f.project.id,
        )
        == 4
    )
    rows, _summary = listed(f.storage)
    assert [r["First Name"] for r in rows] == ["Deniz", "Ada", "Bob", "Cem"]
    assert rows[3]["Company"] == "Kaya Labs"


async def test_connections_copied_in_without_a_run_are_still_the_members(collection_db):
    f = await fixture(collection_db)
    cloud = sample()
    await run_through(work(f, cloud), await export_run(f))
    # As when a member's connections are copied into another project: same events and keys,
    # no run behind them.
    await f.db.pool.execute("UPDATE record_events SET run_id=NULL WHERE kind='connected'")
    async with f.db.pool.acquire() as conn:
        assert await export.need_positions(conn, f.project.id, USER, limit=20) == [member(3)]
        assert [p["first_name"] for p in await export.connections(conn, f.project.id, USER)] == [
            "Ada",
            "Bob",
            "Cem",
        ]
        # Someone else's list is still theirs alone.
        assert await export.connections(conn, f.project.id, "user_someoneelse") == []
    people = {p["name"]: p for p in await records.directory(f.db.pool, f.project.id, viewer=USER)}
    assert people["Ada Yilmaz"]["last_touch"]["member"] == USER


async def test_a_list_never_read_to_the_end_is_read_to_the_end(collection_db):
    f = await fixture(collection_db)
    cloud = sample()
    await run_through(work(f, cloud), await export_run(f))
    # As for a member whose list was read before Tin kept where it ended (migration 091).
    await f.db.pool.execute("UPDATE linkedin_connection_lists SET read_to_end_at=NULL")
    cloud.calls.clear()
    notes = await run_through(work(f, cloud), await export_run(f))
    assert [c["start"] for c in cloud.calls if c["kind"] == "linkedin_connections"] == [0, 2]
    assert notes["new"] == 0 and not notes.get("caught_up") and not notes.get("joined")
    assert await f.db.pool.fetchval(
        "SELECT read_to_end_at IS NOT NULL FROM linkedin_connection_lists WHERE project_id=$1",
        f.project.id,
    )
    # Now caught up: one read.
    cloud.calls.clear()
    notes = await run_through(work(f, cloud), await export_run(f))
    assert [c["start"] for c in cloud.calls if c["kind"] == "linkedin_connections"] == [0]
    assert notes["caught_up"] is True


async def test_a_read_position_is_read_again_once_it_is_stale(collection_db):
    f = await fixture(collection_db)
    cloud = sample()
    await run_through(work(f, cloud), await export_run(f))
    async with f.db.pool.acquire() as conn:
        assert await export.need_positions(conn, f.project.id, USER, limit=20) == [member(3)]
        await conn.execute(
            "UPDATE record_values SET set_at = now() - interval '31 days' "
            "WHERE project_id=$1 AND field='position'",
            f.project.id,
        )
        # Never-read first, then the read longest ago.
        assert (await export.need_positions(conn, f.project.id, USER, limit=20))[0] == member(3)
        assert set(await export.need_positions(conn, f.project.id, USER, limit=20)) == {
            member(1),
            member(2),
            member(3),
        }


async def test_a_signed_out_session_stops_the_run_with_what_to_do(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud([friend(1, "Ada", "Yilmaz", days=1)], errors={"linkedin_connections": "x"})
    cloud.errors["linkedin_connections"] = "session_expired"
    w = work(f, cloud)
    run_id = await export_run(f)
    await w.connections_prepare(run_id)
    with pytest.raises(IntegrationAuthorizationError, match="saved LinkedIn session has expired"):
        await w.connections_read({"run_id": run_id, "read": 0})


async def test_an_answer_tin_cant_read_fails_the_first_read_with_its_cause(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud([], errors={"linkedin_connections": "unsupported_contract"})
    w = work(f, cloud)
    run_id = await export_run(f)
    await w.connections_prepare(run_id)
    with pytest.raises(IntegrationError, match="form Tin doesn't read yet"):
        await w.connections_read({"run_id": run_id, "read": 0})


async def test_linkedin_objecting_to_profile_reads_keeps_the_list(collection_db):
    f = await fixture(collection_db)
    cloud = sample()
    cloud.errors["linkedin_positions"] = "rate_limited"
    run_id = await export_run(f)
    notes = await run_through(work(f, cloud), run_id)
    assert notes["stopped"] == "rate_limited" and notes["people"] == 3
    rows, summary = listed(f.storage)
    # No profile read: a company is listed only where the headline names one.
    assert len(rows) == 3
    assert sorted(r["Company"] for r in rows if r["Company"]) == ["Acme", "Startup"]
    assert "LinkedIn asked Tin to slow down" in summary and "3 still to read." in summary


async def test_a_member_exports_only_their_own_connections(collection_db):
    f = await fixture(collection_db)
    await run_through(work(f, sample()), await export_run(f))
    async with f.db.pool.acquire() as conn:
        assert len(await export.connections(conn, f.project.id, USER)) == 3
        assert await export.connections(conn, f.project.id, "user_teammate") == []


# The Temporal workflow.


def stubs(calls, reads, positions):
    @activity.defn(name="linkedin_connections_prepare")
    async def prepare(run_id):
        calls.append("prepare")

    @activity.defn(name="linkedin_connections_read")
    async def read(control):
        calls.append(("read", control["read"]))
        return reads[control["read"]]

    @activity.defn(name="linkedin_connections_positions")
    async def position_read(control):
        calls.append(("positions", control["read"]))
        return positions[control["read"]]

    @activity.defn(name="linkedin_connections_publish")
    async def publish(control):
        calls.append(("publish", control["notes"]))

    @activity.defn(name="linkedin_connections_failure")
    async def failure(argument):
        raise AssertionError("The run must not fail")

    return [prepare, read, position_read, publish, failure]


def listing(*, people=400, more=True, kind="read", reason=None):
    return {
        "outcome": kind,
        "reason": reason,
        "people": people,
        "new": people,
        "unnamed": 0,
        "pages": 10,
        "more": more,
    }


def profiles(*, more=True, kind="read", reason=None):
    return {"outcome": kind, "reason": reason, "read": 20, "found": 18, "more": more}


@pytest.mark.parametrize(
    ("reads", "positions", "expected"),
    [
        # The list is read to its end, then profiles while there are more to read.
        (
            [listing(), listing(more=False)],
            [profiles(), profiles(more=False)],
            ([0, 1], [0, 1]),
        ),
        # LinkedIn objecting to the list stops all reading; the list so far is written.
        ([listing(), listing(kind="stopped", reason="rate_limited", people=0)], [], ([0, 1], [])),
        # Objecting to a profile read stops the profile reads.
        (
            [listing(more=False)],
            [profiles(kind="stopped", reason="challenge"), profiles()],
            ([0], [0]),
        ),
        # Up to five profile reads a run: a hundred profiles.
        ([listing(more=False)], [profiles()] * 5, ([0], [0, 1, 2, 3, 4])),
    ],
)
async def test_the_workflow_reads_the_list_then_profiles_and_keeps_only_counts(
    reads, positions, expected
):
    calls = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue, run_id = f"connections-{uuid4()}", str(uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[LinkedInConnectionsWorkflow],
            activities=stubs(calls, reads, positions),
        ):
            handle = await env.client.start_workflow(
                LinkedInConnectionsWorkflow.run,
                run_id,
                id=f"{export.KEY}:{run_id}",
                task_queue=queue,
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[LinkedInConnectionsWorkflow]).replay_workflow(history)
    steps = [c for c in calls if isinstance(c, tuple)]
    assert [n for kind, n in steps if kind == "read"] == expected[0]
    assert [n for kind, n in steps if kind == "positions"] == expected[1]
    notes = next(n for kind, n in steps if kind == "publish")
    assert notes["people"] == sum(reads[i]["people"] for i in expected[0])
    assert notes["positions"] == 20 * len(expected[1])
    assert notes.get("stopped") == next(
        (r["reason"] for r in [*reads, *positions] if r["outcome"] == "stopped"), None
    )


def test_the_workflow_reads_as_many_times_as_the_module_says():
    assert LinkedInConnectionsWorkflow.list_reads == export.LIST_READS
    assert LinkedInConnectionsWorkflow.position_reads == export.POSITION_READS
    assert export.MAX_CONNECTIONS == 8000 and export.POSITIONS_PER_RUN == 100


async def test_a_long_sandbox_read_heartbeats_while_it_waits(collection_db, monkeypatch):
    """A paced read of 20 profiles takes minutes; the activity heartbeats meanwhile, so
    Temporal never cancels it as stuck (a real run lost its first profile read that way)."""

    import tin_lite.linkedin_connections_activities as acts

    beats = []
    monkeypatch.setattr(acts, "BEAT_SECONDS", 0.01)
    monkeypatch.setattr(acts, "_heartbeat", beats.append)
    f = await fixture(collection_db)
    cloud = sample()
    slow = cloud.engagement

    async def engagement(*args, **kwargs):
        await asyncio.sleep(0.1)
        return await slow(*args, **kwargs)

    cloud.engagement = engagement
    w = work(f, cloud)
    run_id = await export_run(f)
    await w.connections_prepare(run_id)
    await w.connections_read({"run_id": run_id, "read": 0})
    waited = len(beats)
    assert waited >= 3 and {"waiting": "linkedin_connections"} in beats
    read = await w.connections_positions({"run_id": run_id, "read": 0})
    assert read["outcome"] == "read" and read["read"] == 2
    assert {"waiting": "linkedin_positions"} in beats[waited:]
