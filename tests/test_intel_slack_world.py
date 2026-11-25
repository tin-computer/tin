"""The world summary in Slack: its blocks, escaping and limits, and receipted posting."""

from __future__ import annotations

from datetime import date

import pytest
from disposable_db import migrated_database
from intel_fixtures import project

from tin_lite import intel_slack
from tin_lite.integrations import IntegrationDeliveryUnknownError, IntegrationRateLimitedError

DAY = date(2026, 11, 24)
GN = "https://news.google.com/rss/articles/CBMiabcdefghijk?oc=5"


def story(key, title, *, sensitive=False, url="https://www.reuters.com/a"):
    return {
        "key": key,
        "title": title,
        "sensitive": sensitive,
        "items": [{"url": url, "source": "Reuters"}],
    }


STORIES = {
    "quake": story("quake", "Earthquake in <Region> & coast", sensitive=True),
    "nvidia": story("nvidia", "Nvidia reinstates Huang", url=GN),
    "cup": story("cup", "Cup final ends level", url="http://plain.example/x"),
}
BRIEF = {
    "schema": "intel-world-brief/1",
    "headline": "Quake hits <coast> as Nvidia brings back Huang",
    "groups": [
        {"group": "world", "items": [{"story": "quake", "line": "Rescue teams at work."}]},
        {
            "group": "tech_ai",
            "items": [
                {"story": "nvidia", "line": f"[AP reports]({GN}) a reversal; see ai.example"}
            ],
        },
        {"group": "culture", "items": [{"story": "cup", "line": "Replay on Sunday."}]},
    ],
    "note": "Two wires were empty.",
}


def message(**kwargs):
    values = {
        "day": DAY,
        "items": 1723,
        "sources": 61,
        "story_count": 129,
        "high_count": 14,
        "stories_url": "https://app.tin.test/document/r1?project=p1",
        "links": {GN: "https://apnews.com/article/nvidia"},
    }
    values.update(kwargs)
    return intel_slack.world_message(BRIEF, STORIES, **values)


def test_the_world_summary_blocks():
    body = message()
    blocks = body["blocks"]
    assert (
        body["text"]
        == "The world today, Tue 24 Nov: Quake hits &lt;coast&gt; as Nvidia brings back Huang"
    )
    assert blocks[0] == {
        "type": "header",
        "text": {"type": "plain_text", "text": "The world today · Tuesday 24 November"},
    }
    assert blocks[1]["text"]["text"] == "Quake hits &lt;coast&gt; as Nvidia brings back Huang"
    assert blocks[2]["elements"][0]["text"] == "1,723 items · 61 sources · 129 stories, 14 high"
    assert blocks[3] == {"type": "divider"}
    world, tech, culture = (b["text"]["text"] for b in blocks[4:7])
    # Sensitivity and the title's link come from the story, escaped.
    assert world == (
        "🌍 *World*\n• ⚠️ <https://www.reuters.com/a|Earthquake in &lt;Region&gt; &amp; coast>: "
        "Rescue teams at work."
    )
    # The unwrapped publisher URL replaces the Google News link; a bare domain is defused.
    assert tech.startswith(
        "🤖 *Tech and AI*\n• <https://apnews.com/article/nvidia|Nvidia reinstates"
    )
    assert "<https://apnews.com/article/nvidia|AP reports>" in tech
    assert "ai.​example" in tech
    assert culture.startswith("🎭 *Culture and sport*\n• Cup final ends level: ")
    footer = blocks[-1]["elements"][0]["text"]
    assert footer.startswith("⚠️ sensitive: never a hook for a brand · ")
    assert "<https://app.tin.test/document/r1?project=p1|All 129 stories>" in footer


def test_empty_sections_and_unknown_stories_are_left_out():
    brief = {
        "headline": "H",
        "groups": [
            {"group": "world", "items": [{"story": "gone", "line": "x"}]},
            {"group": "culture", "items": [{"story": "cup", "line": "y"}]},
        ],
    }
    body = intel_slack.world_message(
        brief, STORIES, day=DAY, items=1, sources=1, story_count=1, high_count=0, stories_url=None
    )
    sections = [b for b in body["blocks"] if b["type"] == "section"]
    assert len(sections) == 2 and sections[1]["text"]["text"].startswith("🎭")
    assert not any("sensitive" in str(b) for b in body["blocks"])


def test_limits_hold_for_a_long_day():
    long = {k: {**v, "title": "T" * 500} for k, v in STORIES.items()}
    brief = {**BRIEF, "headline": "H" * 1000}
    body = intel_slack.world_message(
        brief, long, day=DAY, items=1, sources=1, story_count=3, high_count=1, stories_url=None
    )
    assert len(body["blocks"]) <= intel_slack.MAX_BLOCKS
    assert len(body["text"]) <= intel_slack.TEXT_LIMIT
    for block in body["blocks"]:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= intel_slack.SECTION_LIMIT
    assert len(body["blocks"][0]["text"]["text"]) <= 150


@pytest.fixture
async def db():
    async with migrated_database(max_size=5) as database:
        yield database


class Slack:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.posts = []

    async def post(self, project_id, *, channel_id, text, blocks):
        self.posts.append((project_id, channel_id, text))
        outcome = self.outcomes.pop(0) if self.outcomes else "1764000000.000100"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_a_post_is_sent_once_and_never_resent(db):
    host = await project(db)
    slack = Slack()
    body = message()
    args = {
        "poster_project_id": host,
        "project_id": None,
        "key": "world:2026-11-24",
        "channel_id": "C0B5W5325JP",
    }
    assert await intel_slack.send(db, slack, body=body, **args) == "sent"
    assert await intel_slack.send(db, slack, body=body, **args) == "already"
    assert len(slack.posts) == 1
    row = await db.pool.fetchrow("SELECT * FROM intel_slack_posts")
    assert row["status"] == "sent" and row["message_ts"] == "1764000000.000100"


async def test_an_unknown_outcome_is_never_resent_and_a_refusal_is(db):
    host = await project(db)
    slack = Slack(IntegrationDeliveryUnknownError("no answer"), IntegrationRateLimitedError("wait"))
    unknown = {"poster_project_id": host, "project_id": None, "key": "world:a", "channel_id": "C1"}
    refused = {**unknown, "key": "world:b"}
    assert await intel_slack.send(db, slack, body=message(), **unknown) == "unknown"
    assert await intel_slack.send(db, slack, body=message(), **unknown) == "already"
    assert await intel_slack.send(db, slack, body=message(), **refused) == "failed"
    assert await intel_slack.send(db, slack, body=message(), **refused) == "sent"
    assert len(slack.posts) == 3


async def test_a_post_left_sending_becomes_unknown(db):
    await db.pool.execute(
        """INSERT INTO intel_slack_posts (id, post_key, channel_id, status, created_at)
           VALUES (gen_random_uuid(), 'world:old', 'C1', 'sending', now() - interval '1 hour')"""
    )
    await intel_slack.recover(db)
    row = await db.pool.fetchrow("SELECT status, error_code FROM intel_slack_posts")
    assert (row["status"], row["error_code"]) == ("unknown", "outcome_unknown")
    slack = Slack()
    host = await project(db)
    again = await intel_slack.send(
        db,
        slack,
        poster_project_id=host,
        project_id=None,
        key="world:old",
        channel_id="C1",
        body=message(),
    )
    assert again == "already" and slack.posts == []


def test_tins_own_summary_names_each_title_once_and_says_so_once():
    """A fallback brief carries neither the title nor a note: the renderer owns both."""
    from tin_lite.intel_brief import fallback_world_brief

    stories = [
        {
            **story("fabric", "Fabric adds billing"),
            "summary": "Billing moves to usage. More.",
            "relevance": "high",
            "tags": ["ai"],
        }
    ]
    made = fallback_world_brief(stories)
    body = intel_slack.world_message(
        made,
        {s["key"]: s for s in stories},
        day=DAY,
        items=10,
        sources=2,
        story_count=1,
        high_count=1,
        stories_url=None,
        fallback=True,
    )
    texts = " ".join(
        e["text"]
        for b in body["blocks"]
        for e in ([b["text"]] if "text" in b else b.get("elements") or [])
        if isinstance(e, dict) and isinstance(e.get("text"), str)
    )
    assert "|Fabric adds billing>: Billing moves to usage." in texts
    assert texts.count("Fabric adds billing") == 2  # the headline and the story's own title
    assert texts.count("from the day's top stories") == 1
