"""Warm paths to a visitor, from the project's own members' LinkedIn networks."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import Router
from test_rb2b import rb2b_fixture
from test_rb2b_slack import Slack, settings, visitor_run

from tin_lite import people_records as records
from tin_lite import warm_paths
from tin_lite.connection_collection import CollectionError
from tin_lite.people_records import Value
from tin_lite.rb2b_activities import RB2BActivities


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Acme, Inc.", "acme"),
        ("The Acme Company", "ACME"),
        ("Ação Ltd", "acao"),
        ("Procter & Gamble Co.", "procter and gamble"),
    ],
)
def test_company_names_match_without_case_accents_or_legal_suffixes(a, b):
    assert warm_paths.company_key(a) == warm_paths.company_key(b)


def test_different_companies_dont_match():
    assert warm_paths.company_key("Acme Health") != warm_paths.company_key("Acme")
    assert warm_paths.company_key("") == "" and warm_paths.company_key(None) == ""


CARDS = [
    {"profile_url": "https://www.linkedin.com/in/jane-doe-1", "name": "Jane Doe",
     "headline": "CFO at Acme", "mutual_count": 12, "mutual_names": ["Mira Chen", "Sam Lee"]},
    {"profile_url": "https://www.linkedin.com/in/jane-doe-2", "name": "Jane Doe",
     "headline": "Nurse at Elsewhere", "mutual_count": 1, "mutual_names": ["Ali Kaya"]},
]  # fmt: skip


def test_the_visitors_card_is_their_profile_or_their_one_namesake_at_their_company():
    found = warm_paths.lead_in(
        CARDS, profile_url="https://www.linkedin.com/in/Jane-Doe-1/", member=None,
        name="Jane Doe", company=None,
    )  # fmt: skip
    assert found is CARDS[0]
    # No profile to match: the one Jane Doe whose headline names Acme.
    assert warm_paths.lead_in(CARDS, profile_url=None, member=None, name="Jane  Doe",
                              company="Acme Inc") is CARDS[0]  # fmt: skip


def test_a_namesake_elsewhere_or_two_at_once_is_no_path():
    assert warm_paths.lead_in(CARDS, profile_url=None, member=None, name="Jane Doe",
                              company="Globex") is None  # fmt: skip
    twins = [CARDS[0], {**CARDS[1], "headline": "VP at Acme"}]
    assert warm_paths.lead_in(twins, profile_url=None, member=None, name="Jane Doe",
                              company="Acme") is None  # fmt: skip
    assert warm_paths.lead_in([], profile_url=None, member=None, name="Jane Doe",
                              company="Acme") is None  # fmt: skip


def test_the_line_says_the_closest_path():
    names = {"u1": "Ege", "u2": "Emre"}
    assert warm_paths.line(known={"connected": ["u1"]}, second=None, names=names,
                           company="Acme") == "🤝 Ege is already connected"  # fmt: skip
    second = {"member": "u1", "mutual_count": 12, "mutual_names": ["Mira Chen", "Sam Lee", "X"]}
    assert (
        warm_paths.line(known={}, second=second, names=names, company="Acme")
        == "🤝 2nd degree via Mira Chen, Sam Lee + 10 others"
    )
    at = [
        {"member": "u1", "name": "Mira Chen", "position": "Treasury Lead"},
        {"member": "u1", "name": "Sam Lee", "position": ""},
        {"member": "u1", "name": "Ali Kaya", "position": "CTO"},
    ]
    assert (
        warm_paths.line(known={"at_company": at}, second=None, names=names, company="Acme")
        == "🤝 Ege knows Mira Chen (Treasury Lead), Sam Lee + 1 more at Acme"
    )
    two = [at[0], {**at[1], "member": "u2"}]
    assert (
        warm_paths.line(known={"at_company": two}, second=None, names=names, company="Acme")
        == "🤝 Ege and Emre know Mira Chen (Treasury Lead), Sam Lee at Acme"
    )
    # Without a member's name it's the team's.
    assert warm_paths.line(known={"connected": ["u9"]}, second=None, names={},
                           company=None) == "🤝 Your team is already connected"  # fmt: skip
    assert warm_paths.line(known={}, second=None, names=names, company="Acme") is None
    assert warm_paths.line(known={}, second={"member": "u1", "mutual_count": 0},
                           names=names, company="Acme") is None  # fmt: skip


async def connection(f, *, name, company, position="", member="member-1"):
    """A member's first-degree connection, as social.linkedin_connections keeps them."""

    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        record_id = await records.upsert_person(
            conn, f.project.id, {"linkedin_slug": f"c-{uuid4().hex[:8]}"}, source="test"
        )
        await records.set_values(
            conn,
            f.project.id,
            record_id,
            [Value("name", name), Value("company", company), Value("position", position)],
            set_by_kind="run",
            set_by="test",
        )
        await records.set_relationship(
            conn, f.project.id, record_id, member, "connected", set_by="test"
        )
    return record_id


class Profiles:
    async def member_profile(self, member):
        return {"display_name": {"member-1": "Ege Demir"}.get(member)}


class Cloud:
    """The search sandbox: the visitor's card, or a refusal."""

    def __init__(self, people=None, fail=None):
        self.people, self.fail, self.calls = people or [], fail, []

    async def engagement(self, conn, *, execution_key, run_id, session, packet, seconds):
        self.calls.append(packet)
        if self.fail:
            raise CollectionError(self.fail)
        return {"people": self.people, "next_page": False}

    async def cleanup(self, sandbox_id):
        pass


def work(f, slack, *, cloud=None):
    return RB2BActivities(
        database=f.db,
        storage=f.storage,
        router=Router(),
        integrations=SimpleNamespace(slack=slack),
        settings=settings(),
        cloud=cloud,
        cipher=object() if cloud else None,
        profiles=Profiles(),
    )


async def run_through(f, steps, run_id):
    await steps.record(run_id)
    await steps.enrich(run_id)
    await steps.rate(run_id)
    await steps.warm(run_id)
    await steps.notify(run_id)


def posted(slack):
    return "\n".join(
        block["text"]["text"]
        for post in slack.bodies
        for block in post["blocks"]
        if block["type"] == "section"
    )


class Bodies(Slack):
    def __init__(self, db):
        super().__init__(db)
        self.bodies = []

    async def post(self, project_id, *, channel_id, text, blocks):
        self.bodies.append({"text": text, "blocks": blocks})
        return await super().post(project_id, channel_id=channel_id, text=text, blocks=blocks)


async def test_a_strong_fit_shows_who_on_the_team_knows_someone_at_their_company(
    collection_db, monkeypatch
):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    await connection(f, name="Mira Chen", company="Acme Software, Inc.", position="Treasury")
    await connection(f, name="Bo Park", company="Globex")
    slack = Bodies(f.db)
    await run_through(f, work(f, slack), run_id)
    assert "🤝 Ege knows Mira Chen (Treasury) at Acme Software" in posted(slack)
    assert "Bo Park" not in posted(slack)


async def test_a_visitor_already_connected_needs_no_search(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    slack, cloud = Bodies(f.db), Cloud()
    steps = work(f, slack, cloud=cloud)
    await steps.record(run_id)
    person = (await records.people(f.db.pool, f.project.id))[0]
    async with f.db.pool.acquire() as conn:
        await records.set_relationship(
            conn, f.project.id, person["id"], "member-1", "connected", set_by="test"
        )
    await run_through(f, steps, run_id)
    assert "🤝 Ege is already connected" in posted(slack)
    assert cloud.calls == []


async def test_a_second_degree_search_finds_the_mutual_connections_once(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    card = {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane Doe",
            "headline": "Founder at Acme Software", "mutual_count": 4,
            "mutual_names": ["Mira Chen"]}  # fmt: skip
    cloud = Cloud(people=[card])
    steps = work(f, Bodies(f.db), cloud=cloud)

    async def session(self, run):
        return "member-1", {"cookies": []}, {"key": "a"}

    monkeypatch.setattr(RB2BActivities, "_session_for_search", session)
    slack = Bodies(f.db)
    steps.integrations = SimpleNamespace(slack=slack)
    await run_through(f, steps, run_id)
    assert "🤝 2nd degree via Mira Chen + 3 others" in posted(slack)
    # The search is the existing network search, for the visitor's name.
    assert cloud.calls[0]["kind"] == "search" and cloud.calls[0]["keywords"] == "Jane Doe"
    # A retried step doesn't search again.
    await f.db.pool.execute("DELETE FROM effect_receipts WHERE execution_key = $1",
                            f"{run_id}:rb2b:warm")  # fmt: skip
    await steps.warm(run_id)
    assert len(cloud.calls) == 1


async def test_a_search_that_cant_run_leaves_the_message_without_a_path(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    cloud = Cloud(fail="rate_limited")

    async def session(self, run):
        return "member-1", {"cookies": []}, {"key": "a"}

    monkeypatch.setattr(RB2BActivities, "_session_for_search", session)
    slack = Bodies(f.db)
    await run_through(f, work(f, slack, cloud=cloud), run_id)
    assert len(slack.bodies) == 1 and "🤝" not in posted(slack)


async def test_any_member_with_linkedin_can_search_starting_with_the_runs_starter(
    collection_db, monkeypatch
):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    run = await f.db.get_run(__import__("uuid").UUID(run_id))
    tried = []

    async def fetch(*args):
        return [{"member": "teammate-no-session"}, {"member": "teammate"}]

    async def saved(db, settings, cipher, project_id, member):
        tried.append(member)
        if member != "teammate":
            from tin_lite.integrations import IntegrationAuthorizationError

            raise IntegrationAuthorizationError("not connected")
        return {"cookies": []}, {"key": "a"}

    monkeypatch.setattr("tin_lite.rb2b_activities.saved_session", saved)
    steps = work(f, Bodies(f.db), cloud=Cloud())
    steps.db = SimpleNamespace(pool=SimpleNamespace(fetch=fetch))
    found = await steps._session_for_search(run)
    assert found[0] == "teammate" and tried == ["teammate-no-session", "teammate"]
