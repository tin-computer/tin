"""RB2B visitors in Slack: good and strong fits at once, the rest in an evening digest."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import Router
from test_rb2b import SEEN, client, payload, rb2b_fixture, set_up

from tin_lite import people_records as records
from tin_lite import rb2b_slack
from tin_lite.integrations import (
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
)
from tin_lite.rb2b_activities import RB2BActivities


def person(fit, *, intent="warm", profile="https://www.linkedin.com/in/jane", reason="Fits."):
    def value(text=None, number=None, why=""):
        return {"text": text, "number": number, "reason": why}

    return {
        "id": "p-1",
        "profile_url": profile,
        "fields": {
            "name": value("Jane Doe"),
            "headline": value("CFO at Acme"),
            "company": value("Acme", why="From RB2B, website https://www.acme.test/"),
            "location": value("Austin, Texas"),
            "icp_fit": value(number=fit, why=reason),
            "intent": value(intent),
        },
    }


VISIT = {
    "summary": "Ad: Google Ads, keyword “snowflake pricing” · read /vs/snowflake/ (1 page, 40s)"
    " · identified by RB2B",
    "replay_url": "https://us.posthog.com/project/1/replay/01a1f827-fa14-795a",
}


def text_of(body):
    return "\n".join(
        block["text"]["text"] for block in body["blocks"] if block["type"] == "section"
    )


def small_of(body):
    """The account and visit lines, in Slack's smaller context text."""
    return "\n".join(
        element["text"]
        for block in body["blocks"]
        if block["type"] == "context"
        for element in block["elements"]
    )


def buttons(body):
    actions = next(block for block in body["blocks"] if block["type"] == "actions")
    return [element["text"]["text"] for element in actions["elements"]]


@pytest.mark.parametrize("fit", [1, 2, None])
def test_only_good_and_strong_fits_get_their_own_message(fit):
    body = rb2b_slack.visitor_message(
        person(fit), VISIT, people_url="https://tin.test/people", project_name="Acme"
    )
    assert body is None


def test_a_strong_fit_reads_verdict_who_account_why_and_visit():
    body = rb2b_slack.visitor_message(
        person(4, intent="hot"), VISIT, people_url="https://tin.test/p", project_name="Acme"
    )
    text, small = text_of(body), small_of(body)
    assert text.startswith("🟢 *Strong fit*  ·  🔥 hot   →   *reach out today*")
    assert "*<https://www.linkedin.com/in/jane|Jane Doe>*  ·  CFO at Acme" in text
    assert "💬  Fits." in text
    assert "🏢 Acme  ·  acme.test  ·  Austin, Texas" in small
    assert "🧭 Ad: Google Ads, keyword “snowflake pricing” · read /vs/snowflake/" in small
    assert "identified by RB2B" not in text + small and "⚠️" not in text + small
    assert buttons(body) == ["LinkedIn", "▶ Replay", "Open in Tin"]
    assert body["text"] == "Strong fit: Jane Doe"


def test_a_good_fit_without_a_replay_or_a_confirmed_visit_says_so():
    visit = {"summary": "On /pricing (no matching visit in PostHog) · identified by RB2B"}
    body = rb2b_slack.visitor_message(
        person(3, intent="cold"), visit, people_url="https://tin.test/p", project_name="Acme"
    )
    text = text_of(body)
    assert text.startswith("🟡 *Good fit*  ·  🧊 cold   →   *worth a message*")
    assert "⚠️ PostHog didn't confirm the visit" in small_of(body)
    assert buttons(body) == ["LinkedIn", "Open in Tin"]


@pytest.mark.parametrize(
    ("fit", "intent", "step"),
    [
        (4, "hot", "reach out today"),
        (4, "warm", "reach out this week"),
        (4, "cold", "worth a message"),
        (3, "hot", "reach out this week"),
        (3, "warm", "worth a message"),
        (3, "", "worth a message"),
    ],
)
def test_the_next_step_follows_fit_and_intent(fit, intent, step):
    assert rb2b_slack.next_step(fit, intent) == step


def test_only_checked_links_become_buttons():
    forged = {"summary": VISIT["summary"], "replay_url": "https://evil.example/"}
    body = rb2b_slack.visitor_message(
        person(4, profile="https://evil.example/in/jane"),
        forged,
        people_url="https://tin.test/p",
        project_name="Acme",
    )
    assert buttons(body) == ["Open in Tin"]
    assert "evil.example" not in str(body["blocks"])
    assert "*Jane Doe*" in text_of(body)


def test_the_digest_groups_the_rest_and_says_nothing_for_nobody():
    assert rb2b_slack.digest_message([], people_url="u", project_name="Acme") is None
    people = [person(2), person(1), person(1), person(None)]
    body = rb2b_slack.digest_message(people, people_url="https://tin.test/p", project_name="Acme")
    text = text_of(body)
    assert text.startswith("🌙 *4 more visitors today*")
    assert "🟠 Weak fit (1): Jane Doe, CFO at Acme" in text
    assert "⚪ Not a fit (2)" in text and "❔ Not rated (1)" in text
    assert buttons(body) == ["See them in Tin"]


def test_the_digest_is_due_from_six_in_the_evening_local_time():
    istanbul = ZoneInfo("Europe/Istanbul")
    assert rb2b_slack.digest_window(datetime(2026, 11, 23, 14, 59, tzinfo=UTC), istanbul) is None
    day, start, end = rb2b_slack.digest_window(datetime(2026, 11, 23, 15, 0, tzinfo=UTC), istanbul)
    assert day == "2026-11-23"
    assert end == datetime(2026, 11, 23, 18, 0, tzinfo=istanbul)
    assert (end - start).days == 1


class Slack:
    """Tin's Slack connection: records each post, and checks Tin recorded it first."""

    def __init__(self, db, *, fail=None):
        self.db, self.fail, self.posts = db, fail, []

    async def post(self, project_id, *, channel_id, text, blocks):
        recorded = await self.db.pool.fetchval(
            "SELECT status FROM rb2b_slack_posts WHERE channel_id = $1 ORDER BY created_at DESC "
            "LIMIT 1",
            channel_id,
        )
        assert recorded == "sending"
        self.posts.append({"project": project_id, "channel": channel_id, "text": text})
        if self.fail:
            raise self.fail
        return f"1795000000.{len(self.posts):06d}"


async def visitor_run(f, monkeypatch, *, headline_title="Founder", seen=SEEN):
    async with client(f, monkeypatch) as http:
        hook = (await set_up(http, f)).removeprefix("https://tin.test")
        await http.post(hook, json=payload(Title=headline_title, **{"Seen At": seen.isoformat()}))
    await f.db.pool.execute(
        "UPDATE rb2b_hooks SET slack_channel_id = 'C0CLIENT', slack_channel_name = 'tin-acme'"
    )
    return str(
        await f.db.pool.fetchval("SELECT run_id FROM rb2b_visits ORDER BY received_at DESC LIMIT 1")
    )


# Tin's own project, whose Slack connection posts to the clients' shared channels.
POSTER = UUID("bb1b682a-0000-4000-8000-000000000000")


def settings(poster=POSTER):
    return SimpleNamespace(
        app_url="https://app.tin.test", switchboard_public_url="x", rb2b_slack_project_id=poster
    )


async def through(f, run_id, slack, *, poster=POSTER):
    work = RB2BActivities(
        database=f.db,
        storage=f.storage,
        router=Router(),
        integrations=SimpleNamespace(slack=slack),
        settings=settings(poster),
    )
    kept = await work.record(run_id)
    await work.enrich(run_id)
    await work.rate(run_id)
    await work.notify(run_id)
    return work, kept


async def test_a_strong_fit_is_posted_once_to_the_clients_channel(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    slack = Slack(f.db)
    work, _ = await through(f, run_id, slack)
    # Through Tin's own Slack connection, not the client project's.
    assert slack.posts == [
        {"project": POSTER, "channel": "C0CLIENT", "text": "Strong fit: Jane Doe"}
    ]
    # A retried step never posts twice.
    await work.notify(run_id)
    assert len(slack.posts) == 1
    row = await f.db.pool.fetchrow("SELECT * FROM rb2b_slack_posts")
    assert row["status"] == "sent" and row["post_key"] == f"visitor:{run_id}"


async def test_a_visitor_who_isnt_a_fit_waits_for_the_digest(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch, headline_title="Student")
    slack = Slack(f.db)
    await through(f, run_id, slack)
    assert slack.posts == []
    person = (await records.people(f.db.pool, f.project.id))[0]
    assert person["fields"]["icp_fit"]["number"] == 1

    runtime = SimpleNamespace(database=f.db, integrations=SimpleNamespace(slack=slack))
    configured = settings()
    # Before 18:00 in the project's zone (UTC here), nothing; after it, one digest, once.
    before = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
    assert await rb2b_slack.post_digests(runtime, configured, now=before) == 0
    after = datetime(2026, 11, 20, 19, 0, tzinfo=UTC)
    assert await rb2b_slack.post_digests(runtime, configured, now=after) == 1
    assert slack.posts[0]["text"] == "1 more visitor today"
    assert await rb2b_slack.post_digests(runtime, configured, now=after) == 0
    assert len(slack.posts) == 1
    # The next day has nobody new: nothing is posted.
    assert (
        await rb2b_slack.post_digests(
            runtime, configured, now=datetime(2026, 11, 21, 19, 0, tzinfo=UTC)
        )
        == 0
    )


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (IntegrationDeliveryRefusedError("Slack refused the post: x", status=200), "failed"),
        (IntegrationDeliveryUnknownError("Slack answered fatal_error"), "unknown"),
    ],
)
async def test_a_post_that_doesnt_go_through_is_recorded_and_never_sent_again(
    collection_db, monkeypatch, error, status
):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    slack = Slack(f.db, fail=error)
    work, _ = await through(f, run_id, slack)
    row = await f.db.pool.fetchrow("SELECT * FROM rb2b_slack_posts")
    assert row["status"] == status and row["error_message"]
    # The run carries on; a retried step doesn't post again.
    await work.notify(run_id)
    assert len(slack.posts) == 1


async def test_nothing_posts_without_tins_slack_project_or_a_channel(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    run_id = await visitor_run(f, monkeypatch)
    slack = Slack(f.db)
    await through(f, run_id, slack, poster=None)
    assert slack.posts == []
    assert await rb2b_slack.destination(f.db, settings(), f.project.id) is not None
    await f.db.pool.execute("UPDATE rb2b_hooks SET slack_channel_id = NULL")
    assert await rb2b_slack.destination(f.db, settings(), f.project.id) is None


async def test_a_post_left_sending_becomes_unknown(collection_db):
    from uuid import uuid4

    f = await rb2b_fixture(collection_db)
    await f.db.pool.execute(
        """INSERT INTO rb2b_slack_posts (id, project_id, post_key, channel_id, status, created_at)
           VALUES ($1, $2, 'visitor:x', 'C0CLIENT', 'sending', now() - interval '1 hour')""",
        uuid4(),
        f.project.id,
    )
    await rb2b_slack.recover(f.db)
    assert await f.db.pool.fetchval("SELECT status FROM rb2b_slack_posts") == "unknown"
