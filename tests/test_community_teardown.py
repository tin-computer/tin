"""Offline checks of the community teardown recipe; no network requests or model calls."""

import json
import re
from pathlib import Path

import pytest

RESOURCE = (
    Path(__file__).parents[1]
    / "workflow_packages/growth.community_teardown/skills/community-teardown/RANKING.md"
)
FIXTURES = Path(__file__).parent / "fixtures/community_teardown"


@pytest.fixture
def recipe():
    # This exact reviewed repository resource is executable inside the procedure sandbox.
    # Never use this loader for packages submitted to the qualification service.
    blocks = re.findall(r"```python\n(.*?)\n```", RESOURCE.read_text(), re.S)
    assert len(blocks) == 1
    namespace = {}
    exec(compile(blocks[0], str(RESOURCE), "exec"), namespace)  # noqa: S102 - fixed, reviewed fixture
    return namespace


def stories():
    return json.loads((FIXTURES / "stories.json").read_text())["hits"]


def comments():
    return json.loads((FIXTURES / "comments.json").read_text())["hits"]


def discourse_topics():
    data = json.loads((FIXTURES / "discourse_topics.json").read_text())
    return data["grouped_search_result"]["topics"]


def discourse_posts():
    data = json.loads((FIXTURES / "discourse_posts.json").read_text())
    return data["post_stream"]["posts"]


def hn_story_records(recipe):
    return [recipe["parse_story"]("hacker_news", hit) for hit in stories()]


def hn_threads(recipe, limit=3):
    parsed = [recipe["parse_thread"]("hacker_news", hit, comments()) for hit in stories()]
    records = [row["story"] for row in parsed]
    picked = {row["id"] for row in recipe["select_threads"](records, limit=limit)}
    return [row for row in parsed if row["story"]["id"] in picked]


def test_resolve_community_rejects_unsupported(recipe):
    assert recipe["resolve_community"]("") == "hacker_news"
    assert recipe["resolve_community"](None) == "hacker_news"
    assert recipe["resolve_community"]("Discourse") == "discourse"
    with pytest.raises(ValueError, match="unsupported community"):
        recipe["resolve_community"]("reddit")


def test_epoch_before_is_bounded_and_deterministic(recipe):
    now = 1_800_000_000
    assert recipe["epoch_before"](7, now=now) == now - 7 * 86400
    assert recipe["epoch_before"](730, now=now) == now - 730 * 86400
    for days in (6, 731):
        with pytest.raises(ValueError):
            recipe["epoch_before"](days, now=now)


def test_parse_hn_story_normalizes_fields(recipe):
    story = recipe["parse_story"]("hacker_news", stories()[0])
    assert story == {
        "id": "44520001",
        "title": "Ask HN: What's everyone using for bursty GPU batch jobs?",
        "url": "",
        "points": 86,
        "comment_count": 42,
        "author": "ana",
        "created_at": "2026-06-01T08:00:00Z",
        "community": "hacker_news",
    }
    with pytest.raises(ValueError):
        recipe["parse_story"]("hacker_news", {"title": "no id"})
    empty = {"comment_text": ""}
    assert recipe["parse_comments"]("hacker_news", [empty, comments()[0]]) == [
        {
            "text": comments()[0]["comment_text"],
            "author": "mlguru",
            "created_at": "2026-06-01T09:11:00Z",
        }
    ]


def test_select_threads_prefers_discussion_and_drops_dead_posts(recipe):
    records = hn_story_records(recipe)
    picked = recipe["select_threads"](records, limit=3)
    assert [row["id"] for row in picked] == ["44520003", "44520001", "44520005"]
    assert all(row["score"] > 0 for row in picked)
    # The zero-comment job post never appears, even at a wide limit.
    ids = {row["id"] for row in recipe["select_threads"](records, limit=8)}
    assert "44520006" not in ids
    assert "44520004" in ids


def test_select_threads_dedupes_and_bounds(recipe):
    records = hn_story_records(recipe) + hn_story_records(recipe)[:1]
    assert len(recipe["select_threads"](records, limit=8)) <= len({row["id"] for row in records})
    with pytest.raises(ValueError):
        recipe["select_threads"](records, limit=0)


def test_parse_thread_adds_urls_and_validates(recipe):
    thread = recipe["parse_thread"]("hacker_news", stories()[0], comments())
    assert thread["story"]["thread_url"] == "https://news.ycombinator.com/item?id=44520001"
    assert thread["story"]["thread_path"] == "/api/v1/search_by_date"
    assert recipe["validate_thread"](thread) is thread


def test_validate_thread_rejects_bad_shapes(recipe):
    story = recipe["parse_story"]("hacker_news", stories()[0])
    with pytest.raises(ValueError, match="story id"):
        recipe["validate_thread"]({"story": {}, "comments": []})

    thread = {"story": story, "comments": [{"text": "   "}]}
    with pytest.raises(ValueError, match="empty"):
        recipe["validate_thread"](thread)

    thread = {"story": story, "comments": [{"text": "x" * 20_001}]}
    with pytest.raises(ValueError, match="too large"):
        recipe["validate_thread"](thread)

    for bad in (None, {"story": story}, {"story": story, "comments": "nope"}):
        with pytest.raises(ValueError):
            recipe["validate_thread"](bad)


def test_rank_grades_evidence_fit_and_dedupes(recipe):
    commits = [
        {
            "kind": "content_topic",
            "quote": "cheaper than keeping a big reserved cluster around",
            "author": "clusterfan",
            "thread_id": "44520003",
            "extra_thread_ids": ["44520001"],
        },
        {
            "kind": "content_topic",
            "quote": "per-GPU cost dropped a lot on a spot pool",
            "author": "mlguru",
            "thread_id": "44520001",
        },
        {
            "kind": "product_signal",
            "quote": "pricing pages hide the per-GPU cost",
            "author": "devrel",
            "thread_id": "44520005",
        },
        {
            "kind": "engagement_candidate",
            "quote": "anything that just syncs folders across three devices?",
            "author": "syncseeker",
            "thread_id": "44520007",
        },
    ]
    ledger = recipe["rank_opportunities"](
        hn_threads(recipe, limit=4),
        buyer_context="cheap burst gpu batch computing",
        commits=commits,
    )
    assert ledger[0]["kind"] == "content_topic"
    assert ledger[0]["evidence"] == 2
    assert set(ledger[0]["occurrences"]) == {"44520001", "44520003"}
    assert ledger[0]["url"] == "https://news.ycombinator.com/item?id=44520003"
    assert {row["kind"] for row in ledger} == {
        "content_topic",
        "product_signal",
        "engagement_candidate",
    }


def test_rank_rejects_unknown_kinds_and_unread_threads(recipe):
    threads = hn_threads(recipe, limit=1)
    with pytest.raises(ValueError, match="unknown opportunity kind"):
        recipe["rank_opportunities"](
            threads,
            buyer_context="gpu",
            commits=[{"kind": "backlink", "quote": "x", "thread_id": "44520003"}],
        )
    with pytest.raises(ValueError, match="was not read"):
        recipe["rank_opportunities"](
            threads,
            buyer_context="gpu",
            commits=[{"kind": "content_topic", "quote": "x", "thread_id": "44520099"}],
        )
    assert recipe["rank_opportunities"]([], buyer_context="gpu", commits=[]) == []


def test_rank_dedupes_identical_ideas(recipe):
    commits = [
        {
            "kind": "content_topic",
            "quote": "burst gpu too expensive to rent",
            "author": "a",
            "thread_id": "44520003",
        },
        {
            "kind": "content_topic",
            "quote": "burst gpu too expensive to rent",
            "author": "b",
            "thread_id": "44520003",
        },
    ]
    ledger = recipe["rank_opportunities"](
        hn_threads(recipe, limit=1),
        buyer_context="gpu",
        commits=commits,
    )
    assert len(ledger) == 1


def test_discourse_profile_normalizes_and_urls(recipe):
    topics = discourse_topics()
    story = recipe["parse_story"]("discourse", topics[1])
    assert story["id"] == "1202"
    assert story["comment_count"] == 42
    assert story["points"] == 30
    assert story["community"] == "discourse"

    thread = recipe["parse_thread"](
        "discourse", topics[1], discourse_posts(), community_base="https://forum.example.com/"
    )
    assert thread["story"]["thread_url"] == "https://forum.example.com/t/1202"
    assert thread["story"]["thread_path"] == "/t/1202.json"
    with pytest.raises(ValueError, match="community_base"):
        recipe["parse_thread"]("discourse", topics[1], discourse_posts())


def test_discourse_comments_strip_html(recipe):
    parsed = recipe["parse_comments"]("discourse", discourse_posts())
    assert len(parsed) == 3
    assert (
        parsed[0]["text"]
        == "We moved our bursty batch jobs to a spot pool and the per-GPU cost dropped a lot."
    )
    assert (
        parsed[1]["text"]
        == "Pricing pages hide the per-GPU cost & you have to email sales for a quote."
    )


def test_discourse_select_order(recipe):
    records = [recipe["parse_story"]("discourse", topic) for topic in discourse_topics()]
    picked = recipe["select_threads"](records, limit=3)
    assert [row["id"] for row in picked] == ["1202", "1201", "1203"]
    ids = {row["id"] for row in recipe["select_threads"](records, limit=8)}
    assert "1204" not in ids
