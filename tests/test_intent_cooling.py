"""Intent reads as the model rated it, cooled by days since the person last did something."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import fixture

from tin_lite import people_icp
from tin_lite import people_records as records
from tin_lite.people_records import Value

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("rated", "days", "reads"),
    [
        ("hot", 2, "hot"),
        ("hot", 7, "hot"),
        ("hot", 8, "warm"),
        ("hot", 31, "cold"),
        ("warm", 20, "warm"),
        ("warm", 31, "cold"),
        ("cold", 1, "cold"),
    ],
)
def test_intent_cools_with_quiet_days(rated, days, reads):
    assert records.cooled_intent(rated, NOW - timedelta(days=days), NOW) == reads


def test_without_activity_or_a_rating_nothing_cools():
    assert records.cooled_intent("hot", None, NOW) == "hot"
    assert records.cooled_intent(None, NOW - timedelta(days=90), NOW) is None


def test_the_rater_is_told_what_each_intent_looks_like():
    text = people_icp.instructions(["A"])
    assert "searched the brand and read pricing" in text
    assert "a long idle single page" in text


async def visitor(f, name, intent, days_ago):
    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        record_id = await records.upsert_person(
            conn, f.project.id, {"linkedin_slug": name.lower().replace(" ", "-")}, source="test"
        )
        await records.set_values(
            conn,
            f.project.id,
            record_id,
            [Value("name", name), Value("intent", intent, "Read pricing.")],
            set_by_kind="run",
            set_by="test",
        )
        await records.add_event(
            conn,
            f.project.id,
            record_id,
            kind="visited",
            event_key=f"visited:{record_id}",
            occurred_at=datetime.now(UTC) - timedelta(days=days_ago),
            detail={},
        )


async def test_people_show_the_cooled_intent_and_what_was_rated(collection_db):
    f = await fixture(collection_db)
    await visitor(f, "Fresh Hot", "hot", 2)
    await visitor(f, "Week Old", "hot", 10)
    await visitor(f, "Month Old", "warm", 40)
    async with f.db.pool.acquire() as conn:
        listed = {p["name"]: p for p in await records.directory(conn, f.project.id, viewer=USER)}
    fresh = listed["Fresh Hot"]["fields"]["intent"]
    assert fresh["value"] == "hot" and "rated" not in fresh
    week = listed["Week Old"]["fields"]["intent"]
    assert week["value"] == "warm" and week["rated"] == "hot" and week["active_at"]
    month = listed["Month Old"]["fields"]["intent"]
    assert month["value"] == "cold" and month["rated"] == "warm"


async def test_a_reaction_to_an_old_post_counts_from_when_tin_saw_it(collection_db):
    # LinkedIn gives no time for a reaction: it is dated by the post. Someone reacting today to
    # a 35-day-old post is warm today, as the rater saw them, not cold.
    f = await fixture(collection_db)
    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        record_id = await records.upsert_person(
            conn, f.project.id, {"linkedin_slug": "late-reactor"}, source="test"
        )
        await records.set_values(
            conn,
            f.project.id,
            record_id,
            [Value("name", "Late Reactor"), Value("intent", "warm", "Reacted.")],
            set_by_kind="run",
            set_by="test",
        )
        posted = (datetime.now(UTC) - timedelta(days=35)).isoformat()
        await records.add_event(
            conn,
            f.project.id,
            record_id,
            kind="reacted",
            event_key=f"reacted:{record_id}",
            detail={"posted_at": posted},
        )
        listed = {p["name"]: p for p in await records.directory(conn, f.project.id, viewer=USER)}
    intent = listed["Late Reactor"]["fields"]["intent"]
    assert intent["value"] == "warm" and "rated" not in intent
