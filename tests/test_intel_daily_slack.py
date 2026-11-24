"""The client's brief in Slack: Block Kit from the checked brief, outlets from the stories."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from tin_lite import intel_daily_slack as slack
from tin_lite.intel_slack import MAX_BLOCKS

GOLDEN = Path(__file__).parent / "fixtures" / "intel_client" / "slack-golden.json"
GN = "https://news.google.com/rss/articles/CBMiAAAexampleArticleOne0001"
DAY = date(2026, 11, 24)

BRIEF = {
    "schema": "intel-brief/1",
    "headline": "Fabric got cheaper to start, and <analysts> keep asking for HR data",
    "items": [
        {
            "lead": "Fabric got cheaper to start",
            "body": "Microsoft added on-demand billing, [per its announcement]"
            "(https://blog.fabric.example/on-demand). That weakens a point on our "
            "`Fabric comparison` page.",
            "kind": "competitor",
            "verify": False,
            "story": "fabric-on-demand",
        },
        {
            "lead": "A model vendor's beta may exclude regulated accounts",
            "body": "Only [one secondary report](" + GN + ") says so; see vendor.example/docs too.",
            "kind": "platform",
            "verify": True,
            "verify_note": "One secondary report; check the vendor's docs.",
            "story": "vendor-beta",
        },
        {
            "lead": "Analysts want HR data next to clinical data",
            "body": "Two threads asked how to join them without a custom pipeline.",
            "kind": "buyer",
            "verify": False,
            "story": "hr-data",
        },
    ],
    "worth_doing": [{"text": "Refresh the Fabric comparison page."}],
    "note": "Quiet day on Reddit.",
}
STORIES = {
    "fabric-on-demand": {
        "key": "fabric-on-demand",
        "items": [
            {"url": "https://blog.fabric.example/on-demand", "source": "Microsoft Fabric blog"},
            {"url": "https://x.com/someone/status/1", "source": "@someone"},
            {"url": "https://news.example/fabric", "source": "Tech Daily"},
        ],
    },
    "vendor-beta": {"key": "vendor-beta", "items": [{"url": GN, "source": "Crypto Daily"}]},
    "hr-data": {
        "key": "hr-data",
        "items": [
            {"url": "https://www.reddit.com/r/healthIT/comments/p1/x/", "source": "r/healthIT"},
            {"url": "https://www.reddit.com/r/healthIT/comments/p2/y/", "source": "r/healthIT"},
            {"url": "https://example.com/world", "source": "World stories"},
        ],
    },
}
LINKS = {GN: "https://cryptodaily.example/beta"}


def message():
    return slack.client_message(
        BRIEF,
        STORIES,
        client_name="Acme Data",
        day=DAY,
        items=822,
        sources=214,
        kinds=slack.source_kinds({"x": 3, "rss": 10, "hn": 0, "reddit": 2}),
        report_url="https://app.example/document/r1?project=p1",
        sources_url="https://app.example/file?project=p1&path=intel%2Freports%2Fsources.json",
        links=LINKS,
        extra_notes=["World stories unavailable today"],
    )


def test_block_kit_golden():
    assert message() == json.loads(GOLDEN.read_text())


def test_the_message_drops_worth_doing_and_keeps_within_slack_limits():
    body = message()
    text = json.dumps(body)
    assert "Refresh the Fabric comparison page" not in text
    assert "Worth doing" not in text
    assert len(body["blocks"]) <= MAX_BLOCKS
    for block in body["blocks"]:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000
    assert len(body["text"]) <= 300


def test_outlets_and_links_come_from_the_story_and_google_news_is_unwrapped():
    blocks = message()["blocks"]
    text = json.dumps(blocks)
    assert "<https://blog.fabric.example/on-demand|Microsoft Fabric blog>" in text
    assert "<https://x.com/someone/status/1|@someone>" in text
    assert "Tech Daily" not in text  # two outlets at most
    assert "<https://cryptodaily.example/beta|one secondary report>" in text
    assert GN not in text
    assert text.count("|r/healthIT>") == 1  # an outlet is named once


def test_verify_note_escaping_and_no_auto_links_or_code():
    sections = [b["text"]["text"] for b in message()["blocks"] if b["type"] == "section"]
    contexts = [b["elements"][0]["text"] for b in message()["blocks"] if b["type"] == "context"]
    assert any(c.startswith("🔎 To verify: One secondary report") for c in contexts)
    assert "&lt;analysts&gt;" in sections[0]
    joined = "\n".join(sections)
    assert "`" not in joined  # pages read as words, never code
    assert "vendor.example/docs" not in joined  # a bare domain can't become a link
    assert any(c.startswith("World stories unavailable today") for c in contexts)


def test_brief_markdown_mirrors_the_message():
    text = slack.brief_markdown(
        BRIEF,
        STORIES,
        client_name="Acme Data",
        day=DAY,
        items=822,
        sources=214,
        links=LINKS,
        extra_notes=["World stories unavailable today"],
    )
    assert text.startswith("# Acme Data market brief, Tuesday 24 November")
    assert "## ⚔️ Fabric got cheaper to start" in text
    assert "[one secondary report](https://cryptodaily.example/beta)" in text
    assert "To verify: One secondary report" in text
    assert "Sources: [Microsoft Fabric blog](https://blog.fabric.example/on-demand)" in text
    assert "Refresh the Fabric comparison page" not in text


def test_google_links_lists_what_the_brief_shows():
    assert slack.google_links(BRIEF, STORIES) == [GN]
