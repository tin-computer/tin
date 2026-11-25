"""The judges' structured briefs, the story tag taxonomy, and the briefs Tin builds itself.

A judge writes its Slack brief as JSON (`intel brief put`), never as Slack markup: code checks
it here and renders the message (intel_slack, intel_daily_slack). What code owns, the brief
can't claim: whether a story is sensitive, which outlets carried it and the default link all
come from the story rows. A link inside the brief's text must lead to an item the run saw, or
it is dropped with a warning the agent can act on. A brief that breaks the shape is refused
with a sentence that says how to fix it. When no usable brief arrives, Tin builds one from the
day's high stories (`fallback_world_brief`, `fallback_client_brief`), so the day still posts.
A fallback carries no note of its own: the renderers say that Tin wrote it (the brief's
source is "fallback").
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from tin_lite.intel_fetch.items import canon

WORLD_SCHEMA = "intel-world-brief/1"
CLIENT_SCHEMA = "intel-brief/1"
SCHEMAS = (WORLD_SCHEMA, CLIENT_SCHEMA)

WORLD_TAGS = (
    "ai",
    "tech",
    "devtools",
    "enterprise-software",
    "security",
    "outages",
    "business",
    "markets",
    "m-and-a",
    "funding",
    "earnings",
    "labor",
    "policy",
    "regulation",
    "legal",
    "elections",
    "geopolitics",
    "conflict",
    "disasters",
    "health",
    "healthcare-industry",
    "science",
    "space",
    "climate",
    "energy",
    "crypto",
    "consumer",
    "gaming",
    "entertainment",
    "sports",
    "culture",
    "media",
)
CLIENT_TAGS = WORLD_TAGS + (
    "competitor",
    "platform",
    "buyer-voice",
    "product-launch",
    "pricing",
    "partnership",
    "hiring",
    "event",
    "research",
)
TAGS = {"world": WORLD_TAGS, "client": CLIENT_TAGS}
# The world summary's sections, in order, and the tags that place a story in each. Tin's
# clients are tech and consumer companies: tech, business and entertainment lead, world
# politics comes last and holds at most MAX_WORLD_ITEMS lines.
WORLD_GROUPS = {
    "tech_ai": ("ai", "tech", "devtools", "enterprise-software", "security", "outages", "crypto"),
    "business": ("business", "markets", "m-and-a", "funding", "earnings", "labor", "energy"),
    "culture": ("consumer", "gaming", "entertainment", "sports", "culture", "media"),
    "science_health": ("science", "space", "health", "healthcare-industry"),
    "world": (
        "geopolitics",
        "conflict",
        "elections",
        "disasters",
        "policy",
        "regulation",
        "legal",
        "climate",
    ),
}
MAX_WORLD_ITEMS = 2
KINDS = ("competitor", "platform", "buyer", "regulation", "market", "world", "other")
MAX_TAGS = 12

LINK = re.compile(r"\[([^\]\n]{1,200})\]\(([^()\s]{1,2000})\)")
# Anything outside a checked [words](url) link that a reader's client could turn into a link:
# a scheme URL anywhere (even glued to a word or a bracket), or a domain with a path.
BARE_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>]*|\b[\w-]+(?:\.[\w-]+)+/[^\s<>]*")


class BriefError(ValueError):
    """A brief that breaks the schema, said as a fix the agent can make."""


class LinkIndex:
    """The canonical URLs a brief may link to: the run's items and earlier stories' items."""

    def __init__(self, urls: Iterable[str | None] = ()) -> None:
        self._canon: set[str] = set()
        for url in urls:
            self.add(url)

    def add(self, url: str | None) -> None:
        if url:
            self._canon.add(canon(url))

    def __contains__(self, url: str) -> bool:
        return canon(url) in self._canon

    def __len__(self) -> int:
        return len(self._canon)


def clean_tags(scope: str, tags: Iterable[object]) -> tuple[list[str], list[str]]:
    """The tags in the scope's taxonomy (at most 12, lowercase, deduped) and the ones dropped."""
    allowed = set(TAGS[scope])
    kept: list[str] = []
    dropped: list[str] = []
    for raw in tags:
        tag = str(raw).strip().lower()
        if not tag:
            continue
        if tag in allowed and tag not in kept and len(kept) < MAX_TAGS:
            kept.append(tag)
        elif tag not in kept:
            dropped.append(tag[:40])
    return kept, dropped


def check_links(text: str, links: LinkIndex, *, where: str) -> tuple[str, list[str]]:
    """Keep `[words](url)` links that lead to an item the run saw; others lose their URL."""
    warnings: list[str] = []

    def keep(match: re.Match) -> str:
        words, url = match.group(1), match.group(2)
        if not url.startswith("https://"):
            warnings.append(f"{where}: the link on '{words[:40]}' is not https, so Tin removed it.")
            return words
        if url not in links:
            warnings.append(
                f"{where}: the link on '{words[:40]}' isn't an item from today's data or an "
                "earlier story, so Tin removed it. Link only URLs that `intel read` shows."
            )
            return words
        return f"[{words}]({url})"

    text = LINK.sub(keep, text)
    # A URL outside [words](url) would post unchecked: Slack links bare URLs itself.
    parts: list[str] = []
    last = 0
    removed = False
    for match in [*LINK.finditer(text), None]:
        end = match.start() if match else len(text)
        plain, count = BARE_URL.subn("", text[last:end])
        removed = removed or bool(count)
        parts.append(plain)
        if match:
            parts.append(match.group(0))
            last = match.end()
    if removed:
        warnings.append(
            f"{where}: write links as [a few words](https://...); Tin removed the bare URL."
        )
        text = re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()
    return text, warnings


def _text(value: object, *, field: str, limit: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise BriefError(f"`{field}` must be text" + (" and can't be empty." if required else "."))
    value = " ".join(value.split())
    if len(value) > limit:
        raise BriefError(
            f"`{field}` is {len(value)} characters; keep it to {limit} or fewer and send the "
            "brief again."
        )
    return value


def _story(value: object, stories: dict[str, dict] | None, *, where: str, required: bool) -> str:
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str):
        raise BriefError(f"{where}: `story` must be a story key from `intel stories`.")
    key = value.strip().removeprefix("s-")
    if stories is not None and key not in stories:
        raise BriefError(
            f"{where}: there is no story '{key}' today. Record it with `intel story put` first, "
            "or name an existing story's key."
        )
    return key


def _style(text: str, where: str) -> list[str]:
    return [f"{where}: Tin's briefs use no em dashes; rewrite it."] if "—" in text else []


# A client brief item that tells the client what to do ("Worth refreshing our pricing page",
# "We should answer the thread"). Staging 2026-11-25: Databasin's brief carried these although
# worth_doing was empty. A warning back to the analyst, never a refusal.
SUGGESTION = re.compile(
    r"\b(?:worth (?:doing|refreshing|updating|revisiting|adding|writing|publishing|keeping|"
    r"checking|considering|a (?:post|reply|page|look))|(?:we|you) (?:should|could|"
    r"ought to|might want to)|consider (?:adding|updating|refreshing|writing|"
    r"publishing|replying)|time to (?:refresh|update|write|publish|reply)|next step)\b",
    re.IGNORECASE,
)


def _suggestion(text: str, where: str) -> list[str]:
    if SUGGESTION.search(text) is None:
        return []
    return [
        f"{where} reads as a suggestion: a brief item says what happened and why it matters to "
        "the client, nothing to do. Move the suggestion to report.md section 5 and send the "
        "brief again."
    ]


def validate(
    schema: str,
    value: object,
    *,
    links: LinkIndex,
    stories: dict[str, dict] | None = None,
) -> tuple[dict, list[str]]:
    """The brief as Tin keeps it, and warnings for the agent. Raises BriefError (one sentence
    the agent can act on) when the brief breaks the schema. `stories` maps today's story keys
    to their rows; when given, every `story` must name one of them."""
    if not isinstance(value, dict):
        raise BriefError(
            "The brief must be one JSON object; run `intel brief schema` for its shape."
        )
    declared = value.get("schema", schema)
    if declared != schema:
        raise BriefError(f"This run's brief uses schema {schema}, not {str(declared)[:40]}.")
    allowed = (
        {"schema", "headline", "groups", "note"}
        if schema == WORLD_SCHEMA
        else {"schema", "headline", "items", "worth_doing", "note"}
    )
    extra = sorted(set(value) - allowed)
    if extra:
        raise BriefError(
            f"The brief has fields Tin doesn't use: {', '.join(extra[:5])}. "
            "Run `intel brief schema` for its shape."
        )
    if schema == WORLD_SCHEMA:
        return _world(value, links=links, stories=stories)
    if schema == CLIENT_SCHEMA:
        return _client(value, links=links, stories=stories)
    raise BriefError(f"Unknown brief schema {schema!r}.")


def _world(value: dict, *, links: LinkIndex, stories) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    headline = _text(value.get("headline"), field="headline", limit=200)
    groups = value.get("groups")
    if not isinstance(groups, list) or not groups:
        raise BriefError("`groups` must list the sections, each with its stories.")
    seen_groups: set[str] = set()
    seen_stories: set[str] = set()
    kept_groups = []
    total = 0
    for index, group in enumerate(groups, 1):
        where = f"group {index}"
        if not isinstance(group, dict) or set(group) - {"group", "items"}:
            raise BriefError(f'{where} must be {{"group": ..., "items": [...]}}.')
        name = group.get("group")
        if name not in WORLD_GROUPS:
            raise BriefError(f"{where}: `group` must be one of {', '.join(WORLD_GROUPS)}.")
        if name in seen_groups:
            raise BriefError(f"{where}: the {name} section appears twice; merge them.")
        seen_groups.add(name)
        items = group.get("items")
        if not isinstance(items, list) or not items:
            raise BriefError(f"{where} ({name}) has no items; leave out an empty section.")
        kept_items = []
        for number, item in enumerate(items, 1):
            at = f"{name} item {number}"
            if not isinstance(item, dict) or set(item) - {"story", "line"}:
                raise BriefError(f'{at} must be {{"story": "<key>", "line": "..."}}.')
            key = _story(item.get("story"), stories, where=at, required=True)
            if key in seen_stories:
                raise BriefError(f"{at}: story '{key}' is already in the brief.")
            seen_stories.add(key)
            line = _text(item.get("line"), field=f"{at} line", limit=240)
            line, found = check_links(line, links, where=at)
            warnings += found + _style(line, at)
            kept_items.append({"story": key, "line": line})
        if name == "world" and len(kept_items) > MAX_WORLD_ITEMS:
            dropped = [item["story"] for item in kept_items[MAX_WORLD_ITEMS:]]
            kept_items = kept_items[:MAX_WORLD_ITEMS]
            warnings.append(
                f"The world section holds at most {MAX_WORLD_ITEMS} stories; Tin dropped "
                + ", ".join(dropped)
                + ". Put tech, entertainment or business stories in their place."
            )
        total += len(kept_items)
        kept_groups.append({"group": name, "items": kept_items})
    if not 5 <= total <= 8:
        raise BriefError(
            f"The brief has {total} stories; pick the 5 to 8 that matter most today, at most "
            f"{MAX_WORLD_ITEMS} of them in the world section."
        )
    note = _text(value.get("note"), field="note", limit=300, required=False)
    order = list(WORLD_GROUPS)
    kept_groups.sort(key=lambda group: order.index(group["group"]))
    warnings += _style(headline, "headline")
    brief = {"schema": WORLD_SCHEMA, "headline": headline, "groups": kept_groups}
    if note:
        brief["note"] = note
    return brief, warnings


def _client(value: dict, *, links: LinkIndex, stories) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    headline = _text(value.get("headline"), field="headline", limit=220)
    items = value.get("items")
    if not isinstance(items, list) or not 3 <= len(items) <= 5:
        raise BriefError("`items` must hold the 3 to 5 developments that matter most today.")
    kept = []
    for number, item in enumerate(items, 1):
        at = f"item {number}"
        if not isinstance(item, dict) or set(item) - {
            "lead",
            "body",
            "kind",
            "verify",
            "verify_note",
            "story",
        }:
            raise BriefError(f"{at} has fields Tin doesn't use; run `intel brief schema`.")
        lead = _text(item.get("lead"), field=f"{at} lead", limit=120)
        body = _text(item.get("body"), field=f"{at} body", limit=450)
        body, found = check_links(body, links, where=at)
        warnings += found + _style(lead + body, at) + _suggestion(f"{lead}. {body}", at)
        kind = item.get("kind", "other")
        if kind not in KINDS:
            raise BriefError(f"{at}: `kind` must be one of {', '.join(KINDS)}.")
        verify = item.get("verify", False)
        if not isinstance(verify, bool):
            raise BriefError(f"{at}: `verify` is true or false.")
        note = _text(item.get("verify_note"), field=f"{at} verify_note", limit=160, required=False)
        if verify and not note:
            warnings.append(f"{at}: say in `verify_note` what needs checking.")
        entry = {"lead": lead, "body": body, "kind": kind, "verify": verify}
        if note:
            entry["verify_note"] = note
        story = _story(item.get("story"), stories, where=at, required=False)
        if story:
            entry["story"] = story
        kept.append(entry)
    if value.get("worth_doing") not in (None, []):
        # Suggestions live in report.md section 5; no brief renders them.
        warnings.append(
            "worth_doing: leave it empty; suggestions go in report.md section 5. Tin dropped "
            "them from the brief."
        )
    note = _text(value.get("note"), field="note", limit=300, required=False)
    warnings += _style(headline, "headline")
    brief = {
        "schema": CLIENT_SCHEMA,
        "headline": headline,
        "items": kept,
        "worth_doing": [],
    }
    if note:
        brief["note"] = note
    return brief, warnings


# A story's links: what code owns, the brief can't claim.


def story_links(story: dict | None, links: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """A story's items as (outlet, https URL), strongest first: the order the judge attached
    them. `links` swaps a Google News link for the publisher URL Tin unwrapped. The one rule
    the default link, a client brief's outlets and a world story's item in a client's day use."""
    found = []
    for entry in (story or {}).get("items") or []:
        url = (links or {}).get(entry.get("url"), entry.get("url"))
        if isinstance(url, str) and url.startswith("https://"):
            found.append(((entry.get("source") or "").strip(), url))
    return found


def default_link(story: dict | None, links: dict[str, str] | None = None) -> str | None:
    """A story's strongest item: the first one the judge attached that has an https URL."""
    found = story_links(story, links)
    return found[0][1] if found else None


def brief_links(schema: str, brief: dict | None, stories: dict[str, dict]) -> list[str]:
    """Every link a rendered brief shows, in order: the [words](url) links in its text and the
    links code adds from its stories (a world line's default link, a client item's outlets).
    Which of them to unwrap is gnews.resolve_many's call."""
    brief = brief or {}
    texts = [brief.get("headline") or "", brief.get("note") or ""]
    found: list[str] = []
    if schema == WORLD_SCHEMA:
        for group in brief.get("groups") or []:
            for entry in group.get("items") or []:
                texts.append(entry.get("line") or "")
                url = default_link(stories.get(entry.get("story") or ""))
                if url:
                    found.append(url)
    else:
        for entry in brief.get("items") or []:
            texts.append(entry.get("body") or "")
            found += [url for _name, url in story_links(stories.get(entry.get("story") or ""))]
    for text in texts:
        found += [match.group(2) for match in LINK.finditer(text)]
    return list(dict.fromkeys(found))


# Tin's own briefs, when the judge's is missing or unusable.

_RELEVANCE = {"high": 0, "medium": 1, "low": 2}


def _ranked(stories: Iterable[dict]) -> list[dict]:
    return sorted(
        stories,
        key=lambda s: (
            _RELEVANCE.get(s.get("relevance") or "", 3),
            -len(s.get("items") or []),
            s.get("key") or "",
        ),
    )


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def group_of(tags: Iterable[str]) -> str | None:
    tags = list(tags or [])
    for tag in tags:
        for name, members in WORLD_GROUPS.items():
            if tag in members:
                return name
    return None


def fallback_world_brief(stories: list[dict], *, headline_hint: str | None = None) -> dict | None:
    """Up to 8 of the day's stories, high ones first, in their sections. None with no stories."""
    chosen: dict[str, list[dict]] = {name: [] for name in WORLD_GROUPS}
    count = 0
    for story in _ranked(s for s in stories if s.get("relevance") in {"high", "medium"}):
        name = group_of(story.get("tags") or []) or "world"
        if count >= 8:
            break
        if name == "world" and len(chosen["world"]) >= MAX_WORLD_ITEMS:
            continue
        # The renderers put the story's title before its line, so the line is the summary's.
        line = _first_sentence(story.get("summary") or "", 240)
        chosen[name].append({"story": story["key"], "line": line})
        count += 1
    if not count:
        return None
    lead = headline_hint or _ranked(stories)[0]["title"]
    return {
        "schema": WORLD_SCHEMA,
        "headline": lead[:200],
        "groups": [{"group": name, "items": items} for name, items in chosen.items() if items],
    }


_KIND_TAGS = (
    ("competitor", "competitor"),
    ("platform", "platform"),
    ("buyer-voice", "buyer"),
    ("regulation", "regulation"),
    ("policy", "regulation"),
    ("markets", "market"),
    ("funding", "market"),
    ("m-and-a", "market"),
)


def fallback_client_brief(stories: list[dict], *, headline_hint: str | None = None) -> dict | None:
    """Up to 5 of the day's high and medium stories. None with no stories."""
    picked = _ranked(s for s in stories if s.get("relevance") in {"high", "medium"})[:5]
    if not picked:
        return None
    items = []
    for story in picked:
        tags = set(story.get("tags") or [])
        kind = next((kind for tag, kind in _KIND_TAGS if tag in tags), "other")
        items.append(
            {
                "lead": story["title"][:120],
                "body": _first_sentence(story.get("summary") or "", 450),
                "kind": kind,
                "verify": False,
                "story": story["key"],
            }
        )
    return {
        "schema": CLIENT_SCHEMA,
        "headline": (headline_hint or picked[0]["title"])[:220],
        "items": items,
        "worth_doing": [],
    }


def schema_text(schema: str) -> str:
    """What `intel brief schema` prints: the shape, the limits and one full example."""
    if schema == WORLD_SCHEMA:
        return WORLD_SCHEMA_TEXT
    return CLIENT_SCHEMA_TEXT


WORLD_SCHEMA_TEXT = """intel-world-brief/1: the world summary Tin posts in Slack.

{"schema": "intel-world-brief/1",
 "headline": "<the day in one line, at most 200 characters>",
 "groups": [{"group": "tech_ai|business|culture|science_health|world",
             "items": [{"story": "<story key>", "line": "<at most 240 characters>"}]}],
 "note": "<optional, at most 300 characters>"}

5 to 8 stories in total, each once, most important first within a section, at most 2 in
world (Tin drops the rest). Sections show in the order above: tech and AI, business,
entertainment and culture (gaming, internet culture, consumer launches), science and health,
world.
Leave out empty sections. A line may link a few words to an item URL:
[a few words](https://...). Only links to items in today's data (or earlier stories) are
kept. Tin adds each story's outlets, marks sensitive stories from the story itself and renders
the Slack message; send plain text, no Slack markup, no em dashes.

Example:
{"schema": "intel-world-brief/1",
 "headline": "Chip stocks slide on export curbs as OpenAI shows its first device",
 "groups": [
  {"group": "tech_ai", "items": [
    {"story": "us-chip-export-curbs",
     "line": "New US rules cover more Nvidia parts; Nvidia fell 4% before the open."},
    {"story": "openai-device-launch",
     "line": "OpenAI shows its first device, a screenless assistant shipping in March."}]},
  {"group": "business", "items": [
    {"story": "fed-minutes",
     "line": "Minutes show most members expect one more cut this year."}]},
  {"group": "culture", "items": [
    {"story": "gta-vi-launch-week",
     "line": "39 million players in four days, [Polygon reports](https://www.polygon.com/...)."}]},
  {"group": "world", "items": [
    {"story": "gaza-ceasefire-talks",
     "line": "Talks resume in Cairo, [Reuters reports](https://www.reuters.com/world/...)."}]}]}
"""

CLIENT_SCHEMA_TEXT = """intel-brief/1: the client's executive brief Tin posts in Slack.

{"schema": "intel-brief/1",
 "headline": "<the day in one line, at most 220 characters>",
 "items": [{"lead": "<at most 120>", "body": "<at most 450, may link [a few words](https://...)>",
            "kind": "competitor|platform|buyer|regulation|market|world|other",
            "verify": false, "verify_note": "<at most 160, when verify is true>",
            "story": "<story key>"}],
 "worth_doing": [],
 "note": "<optional, at most 300 characters>"}

3 to 5 items. Each says what happened and why it matters to the client: no suggestions, no
to-dos, no "worth doing". Leave worth_doing empty: suggestions go in report.md section 5, and
Tin drops any sent here and warns about an item that reads as one. Only links to items in
today's data (or earlier stories) are kept. Mark anything resting on one secondary or
unverified source verify: true and say what to check. Tin adds each story's outlets and
renders the Slack message; send plain text, no Slack markup, no em dashes.
"""
