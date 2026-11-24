"""Daily intelligence briefs: shapes, the link check, the tag taxonomy and Tin's fallbacks."""

from __future__ import annotations

import pytest

from tin_lite import intel_brief as brief
from tin_lite.intel_slack import mrkdwn

ITEM = "https://www.reuters.com/world/talks-resume-2026-11-24/"
LINKS = brief.LinkIndex([ITEM, "https://news.ycombinator.com/item?id=1"])


def story(key, relevance="high", tags=("geopolitics",), sensitive=False):
    return {
        "key": key,
        "title": f"Title of {key}",
        "summary": f"What happened in {key}. More detail follows.",
        "relevance": relevance,
        "tags": list(tags),
        "sensitive": sensitive,
        "items": [{"url": ITEM, "source": "Reuters"}],
    }


STORIES = {
    key: story(key)
    for key in ("talks", "chips", "device", "strike", "fed", "quake", "game", "moon", "extra")
}


def world(lines=5, **extra):
    keys = list(STORIES)[:lines]
    return {
        "schema": brief.WORLD_SCHEMA,
        "headline": "Talks resume as chips slide",
        "groups": [
            {"group": "world", "items": [{"story": k, "line": f"Line for {k}."} for k in keys]}
        ],
        **extra,
    }


def client(items=3, **extra):
    return {
        "schema": brief.CLIENT_SCHEMA,
        "headline": "Fabric adds Clarity mirroring",
        "items": [
            {"lead": f"Lead {n}", "body": f"Body {n}.", "kind": "competitor"} for n in range(items)
        ],
        "worth_doing": [{"text": "Refresh the Fabric comparison page."}],
        **extra,
    }


def test_a_world_brief_in_shape_is_kept_with_its_sections_in_order():
    value = world()
    value["groups"].insert(0, {"group": "culture", "items": [value["groups"][0]["items"].pop()]})
    kept, warnings = brief.validate(brief.WORLD_SCHEMA, value, links=LINKS, stories=STORIES)
    assert [g["group"] for g in kept["groups"]] == ["world", "culture"]
    assert sum(len(g["items"]) for g in kept["groups"]) == 5
    assert warnings == []


@pytest.mark.parametrize("count", [4, 9])
def test_a_world_brief_needs_five_to_eight_stories(count):
    keys = list(STORIES) + ["tenth"]
    stories = {**STORIES, "tenth": story("tenth")}
    value = world()
    value["groups"][0]["items"] = [{"story": k, "line": "A line."} for k in keys[:count]]
    with pytest.raises(brief.BriefError, match="5 to 8"):
        brief.validate(brief.WORLD_SCHEMA, value, links=LINKS, stories=stories)


def test_an_unknown_story_or_field_is_refused_with_a_fix():
    value = world()
    value["groups"][0]["items"][0]["story"] = "never-recorded"
    with pytest.raises(brief.BriefError, match="intel story put"):
        brief.validate(brief.WORLD_SCHEMA, value, links=LINKS, stories=STORIES)
    with pytest.raises(brief.BriefError, match="intel brief schema"):
        brief.validate(brief.WORLD_SCHEMA, world(extra=1), links=LINKS, stories=STORIES)
    with pytest.raises(brief.BriefError, match="240 or fewer"):
        too_long = world()
        too_long["groups"][0]["items"][0]["line"] = "x" * 241
        brief.validate(brief.WORLD_SCHEMA, too_long, links=LINKS, stories=STORIES)


def test_a_client_brief_in_shape_is_kept():
    kept, warnings = brief.validate(brief.CLIENT_SCHEMA, client(), links=LINKS)
    assert kept["items"][0] == {
        "lead": "Lead 0",
        "body": "Body 0.",
        "kind": "competitor",
        "verify": False,
    }
    assert kept["worth_doing"] == [{"text": "Refresh the Fabric comparison page."}]
    assert warnings == []


@pytest.mark.parametrize("count", [2, 6])
def test_a_client_brief_needs_three_to_five_items(count):
    with pytest.raises(brief.BriefError, match="3 to 5"):
        brief.validate(brief.CLIENT_SCHEMA, client(count), links=LINKS)


def test_verify_without_a_note_warns():
    value = client()
    value["items"][0]["verify"] = True
    _kept, warnings = brief.validate(brief.CLIENT_SCHEMA, value, links=LINKS)
    assert any("verify_note" in w for w in warnings)


def test_links_to_items_stay_and_others_lose_their_url_with_a_warning():
    text = f"[Reuters reports]({ITEM}) and [a rumour](https://evil.example/x) and [old](http://{ITEM[8:]})"
    kept, warnings = brief.check_links(text, LINKS, where="item 1")
    assert kept == f"[Reuters reports]({ITEM}) and a rumour and old"
    assert len(warnings) == 2
    # www and tracking parameters don't matter: canonical URLs compare.
    kept, warnings = brief.check_links(
        "[same](https://reuters.com/world/talks-resume-2026-11-24?utm_source=x)", LINKS, where="x"
    )
    assert warnings == [] and kept.startswith("[same](")


@pytest.mark.parametrize(
    "text",
    [
        "see (https://x.example) now",
        "xhttps://x.example here",
        "go to evil.example/path please",
        "plain https://x.example/a end",
    ],
)
def test_no_unchecked_url_survives_in_brief_text(text):
    kept, warnings = brief.check_links(text, LINKS, where="item 1")
    assert "x.example" not in kept and "evil.example" not in kept
    assert warnings and "bare URL" in warnings[0]
    rendered = mrkdwn(kept)
    assert "://" not in rendered and "<http" not in rendered


def test_slack_never_auto_links_domain_shaped_text():
    rendered = mrkdwn("visit evil.example today, or ftp://x")
    assert "evil.example" not in rendered
    assert "evil.​example" in rendered
    assert "://" not in rendered
    assert mrkdwn(f"[Reuters]({ITEM})") == f"<{ITEM}|Reuters>"


def test_text_is_escaped_for_slack():
    assert mrkdwn("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_tags_outside_the_taxonomy_are_dropped():
    kept, dropped = brief.clean_tags("world", ["AI", "competitor", "ai", "sports", "made-up"])
    assert kept == ["ai", "sports"]
    assert dropped == ["competitor", "made-up"]
    kept, _ = brief.clean_tags("client", ["competitor", "pricing"])
    assert kept == ["competitor", "pricing"]


def test_fallback_world_brief_uses_high_stories_in_their_sections():
    stories = [
        story("talks", tags=["conflict"], sensitive=True),
        story("chips", tags=["ai", "markets"]),
        story("cup", tags=["sports"]),
        story("noise", relevance="low"),
    ]
    made = brief.fallback_world_brief(stories)
    groups = {g["group"]: [i["story"] for i in g["items"]] for g in made["groups"]}
    assert groups == {"world": ["talks"], "tech_ai": ["chips"], "culture": ["cup"]}
    assert all(len(i["line"]) <= 240 for g in made["groups"] for i in g["items"])
    assert brief.fallback_world_brief([story("x", relevance="low")]) is None


def test_fallback_client_brief_picks_kinds_from_tags():
    made = brief.fallback_client_brief(
        [story("fabric", tags=["competitor"]), story("hipaa", relevance="medium", tags=["policy"])]
    )
    assert [(i["story"], i["kind"]) for i in made["items"]] == [
        ("fabric", "competitor"),
        ("hipaa", "regulation"),
    ]
    assert brief.fallback_client_brief([]) is None


def test_an_unusable_model_brief_is_refused_and_the_fallback_still_posts():
    """A plausible but unusable judge result: an empty brief, and links outside the day."""
    with pytest.raises(brief.BriefError):
        brief.validate(brief.WORLD_SCHEMA, {}, links=LINKS, stories=STORIES)
    outside = world()
    outside["groups"][0]["items"][0]["line"] = "[Big news](https://not-in-the-day.example/a)."
    kept, warnings = brief.validate(brief.WORLD_SCHEMA, outside, links=LINKS, stories=STORIES)
    assert kept["groups"][0]["items"][0]["line"] == "Big news."
    assert warnings
    assert brief.fallback_world_brief(list(STORIES.values())) is not None
