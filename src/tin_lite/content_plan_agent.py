"""content.plan 1.0.0 (content-editorial-v9): a planning agent proposes, Tin fills every week.

Before v9 one model call read a frozen bundle and was told capacity is not a quota, so six-month
programs held one to seven items. From v9 a Codex procedure, `content.plan_research`, does the
thinking: it reads the brief Tin publishes beside the run (the program, the merged competitor
list, the research rows, the pages Tin read and the site's page list) and every project file it
needs (the audit and keyword reports, the Code map, the brand guide, founder notes, Page
decisions), searches the web where a choice depends on what ranks, and writes a portfolio in
priority order: alternatives and comparisons for evidenced competitors, answer pages, page
families the product actually has, guides, refreshes and updates.

Tin owns everything after that. `normalize` keeps each usable proposal and leaves out the rest
with a named reason (a page the site already has, a page Page decisions keeps or retires, an
update of a page Tin cannot find, a duplicate); nothing the agent paid for fails the run.
`fill` then puts the kept items on the calendar in priority order, every week up to its
capacity, so the program ships every week until the portfolio runs out. Weak evidence is a
label on the item (`evidence_strength`), not a reason to drop it.

content.plan 1.1.0 (content-editorial-v10) gives the agent the outline and opening of every page
the site lists, not only those with search signal, and `normalize` names the existing pages
closest to each new item as its evidence and first check (`nearby_pages`). A refresh brief that
needs a table, section or FAQ changed is planned as an update, which a refresh cannot do.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import date
from urllib.parse import urlsplit

from tin_lite import content_plan as legacy
from tin_lite.organic_audit import canonical_json

RESEARCH_KEY = "content.plan_research"
VALIDATOR = "content-plan-portfolio.v1"
PATH_TEMPLATE = "reports/content-plan/{run_id}/PORTFOLIO.md"
SCHEMA = "content-portfolio/1"
START = "<!-- content-portfolio.json:start -->"
END = "<!-- content-portfolio.json:end -->"
MAX_PORTFOLIO_BYTES = 400_000
MAX_OPPORTUNITIES = 120

BRIEF_FOLDER = "reports/content-plan/{run_id}/brief"
# Runaway guards, well above a large site's brief.
BRIEF_FILES = {
    "BRIEF.md": 1_000_000,
    "context.json": 1_000_000,
    "research.json": 4_000_000,
    # v10 adds the opening and outline of up to 150 more pages.
    "pages.json": 4_000_000,
}

# What a planned page is. The format says how content.generate should build it; the item's kind
# (article, answer or refresh) stays the one content.generate already drafts.
FORMATS = {
    "alternative": "an alternatives page for one named competitor",
    "comparison": "a head-to-head page: the product against one or two named competitors",
    "roundup": "a best-tools page for the category that includes the product",
    "workaround": "the product against the non-software way (a spreadsheet, an agency, by hand)",
    "answer": "a page answering one buyer question directly, for search and AI answers",
    "family_hub": "the hub page of a page family",
    "family_page": "one member of a page family (one workflow, integration, use case, audience)",
    "use_case": "a problem or job-to-be-done page",
    "guide": "a how-to or explainer article",
    "refresh": "a refresh of an existing page's title, meta description, H1 and opening, and "
    "up to three body paragraphs when Page decisions allows; never a table, list, link, section "
    "or FAQ (that is an update)",
    "update": "a substantive rewrite of an existing page",
}
NEW_FORMATS = frozenset(FORMATS) - {"refresh", "update"}
COMPARISON_FORMATS = frozenset({"alternative", "comparison", "roundup", "workaround"})
STRENGTHS = ("measured", "inferred", "bet")
RESEARCH_PREFIXES = (
    "keyword:",
    "group:",
    "audit:",
    "refresh:",
    "efficacy:",
    "competitor:",
    "page:",
)
MAX_SOURCES = 12
# v10: the existing pages closest to each new item, kept as its evidence and a check.
NEARBY_PAGES = 3
# The share of an item's distinctive words a page must carry, weighted by rarity on the site.
NEARBY_MIN_SCORE = 0.35
NEARBY_EVIDENCE_BYTES = 24_000
# v10: what a refresh brief cannot ask for. content.generate's refresh replaces the title, meta
# description, H1, lead and up to three body paragraphs in place (content_refresh).
REFRESH_REACH = re.compile(r"(?i)\b(?:tables?|rows?|columns?|sections?|faqs?)\b")
LANGUAGE_SEGMENT = re.compile(r"/(?:[a-z]{2}|[a-z]{2}-[a-z]{2})/", re.I)
NEARBY_SKIPPED_PAGES = frozenset(
    "about privacy terms legal cookies sign-in sign-up signin signup login status".split()
)
NEARBY_STOPWORDS = frozenset(
    "a an and are as at be best by can do does for from get how i in into is it its of on or "
    "the their them this to vs versus what when where which who why with without you your".split()
)
# content.generate reads cited project files into its context: 20 KB each, 60 KB together.
DRAFT_FILE_BYTES = 20_000
DRAFT_FILES_BYTES = 60_000
DEFAULT_CHECK = "Check every product claim against the project's own files before drafting."
# Hosts that are channels or marketplaces, never a competitor to write an alternatives page for.
PLATFORMS = frozenset(
    "amazon apple bing capterra ebay facebook g2 github google instagram linkedin medium "
    "producthunt quora reddit stackoverflow substack tiktok trustpilot twitter wikipedia x "
    "youtube".split()
)


# The portfolio's JSON shape, published in content.plan's definition and checked by the
# procedure's own check script. Tin's reader is lenient beyond `validate`.
_TEXT = {"type": "string"}
PORTFOLIO_JSON_SCHEMA = {
    "type": "object",
    "required": ["schema", "strategy", "opportunities"],
    "properties": {
        "schema": {"const": SCHEMA},
        "strategy": {"type": "string", "maxLength": 4200},
        "competitors": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "name": _TEXT,
                    "host": _TEXT,
                    "use": _TEXT,
                    "why": _TEXT,
                },
            },
        },
        "families": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "id": _TEXT,
                    "name": _TEXT,
                    "why": _TEXT,
                    "members": {"type": "array", "items": _TEXT},
                },
            },
        },
        "opportunities": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_OPPORTUNITIES,
            "items": {
                "type": "object",
                "required": ["id", "format", "title", "brief", "action"],
                "properties": {
                    "id": _TEXT,
                    "format": {"enum": list(FORMATS)},
                    "title": {"type": "string", "maxLength": 180},
                    "target_query": {"type": "string", "maxLength": 200},
                    "intent": {"type": "string", "maxLength": 500},
                    "action": {"enum": ["new_page", "update_page"]},
                    "destination": _TEXT,
                    "brief": {"type": "string", "maxLength": 1800},
                    "sources": {"type": "array", "items": _TEXT},
                    "evidence_strength": {"enum": list(STRENGTHS)},
                    "why_this": _TEXT,
                    "win_case": _TEXT,
                    "metric": _TEXT,
                    "rejected": {"type": "array", "items": _TEXT},
                    "verification": {"type": "array", "items": _TEXT},
                    "competitor": _TEXT,
                    "family": _TEXT,
                },
            },
        },
        "gaps": {"type": "array", "items": _TEXT},
        "excluded": {"type": "array", "items": _TEXT},
    },
}


# content.plan 1.2.0 (content-editorial-v11): the agent runs a competitor research phase
# before planning (four channels, every candidate checked on its own site), labels each
# competitor and records what it searched. Earlier contracts keep PORTFOLIO_JSON_SCHEMA.
COMPETITOR_LABELS = ("direct", "adjacent", "diy", "drift")
# Labels that may get an alternatives or comparison page; drift is a tool for another job.
PAGE_LABELS = frozenset({"direct", "adjacent", "diy"})
CHANNELS = ("roundups", "job_and_stack", "launches", "comparison_pages")
MAX_COMPETITORS = 40
PORTFOLIO_JSON_SCHEMA_V2 = deepcopy(PORTFOLIO_JSON_SCHEMA)
PORTFOLIO_JSON_SCHEMA_V2["properties"]["competitors"] = {
    "type": "array",
    "maxItems": MAX_COMPETITORS,
    "items": {
        "type": "object",
        "properties": {
            "name": _TEXT,
            "host": _TEXT,
            "label": {"enum": list(COMPETITOR_LABELS)},
            "use": _TEXT,
            "why": _TEXT,
            "does": _TEXT,
            "buyer": _TEXT,
            "evidence_url": _TEXT,
            "channels": {"type": "array", "items": {"enum": list(CHANNELS)}},
        },
    },
}
PORTFOLIO_JSON_SCHEMA_V2["properties"]["competitor_research"] = {
    "type": "object",
    "properties": {
        "started_from": _TEXT,
        "channels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel": {"enum": list(CHANNELS)},
                    "queries": {"type": "array", "items": _TEXT},
                    "found": {"type": "array", "items": _TEXT},
                    "notes": _TEXT,
                },
            },
        },
    },
}
# Tin keeps the set the agent settled here, so the next plan starts from it.
COMPETITORS_PATH = "research/competitors.json"
COMPETITORS_SCHEMA = "competitors/1"
COMPETITORS_MAX_BYTES = 200_000
# A set checked this recently is a starting point: check new leads, search launches again.
COMPETITORS_FRESH_DAYS = 30


def path_template(run_id) -> str:
    return PATH_TEMPLATE.replace("{run_id}", str(run_id))


def brief_paths(run_id) -> dict[str, str]:
    folder = BRIEF_FOLDER.replace("{run_id}", str(run_id))
    return {name: f"{folder}/{name}" for name in BRIEF_FILES}


# --- competitors -------------------------------------------------------------------------


def _host(value):
    """A registrable host: example.com for docs.example.com, example.co.uk kept whole."""
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    labels = host.removeprefix("www.").split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})


def _name_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def watch_competitors(text):
    """The competitors a competitor.watch report watches, from its evidence block, whatever its
    status; [] when the block is missing or unreadable."""
    from tin_lite.content_plan_sources import WATCH_FENCE

    blocks = WATCH_FENCE.findall(text or "")
    if len(blocks) != 1:
        return []
    try:
        evidence = json.loads(blocks[0])
        return [
            {"host": _host(item.get("id")), "name": " ".join(str(item["name"]).split())[:120]}
            for item in evidence["competitors"]
            if isinstance(item, dict) and item.get("name")
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        return []


# Path segments a comparison page lives under, and the words that join two names in one.
COMPARISON_SECTIONS = frozenset(
    "alternative alternatives vs versus compare comparison comparisons".split()
)
COMPARISON_JOIN = re.compile(r"-(?:vs|versus)-")
COMPARISON_SUFFIX = re.compile(r"-alternatives?$")
NOT_A_NAME = frozenset("best top all index page list more other others".split())
MAX_SITE_COMPARISONS = 30


# Sections whose single-slug pages each name one rival: /alternatives/<name>, /vs/<name>.
NAMED_SECTIONS = frozenset("alternative alternatives vs versus".split())


def site_comparisons(site_pages, host):
    """The competitors the site already compares itself to, from its own page addresses:
    /alternatives/<name>, /vs/<name>, /compare/<a>-vs-<b>, /<name>-vs-<site>, and
    /<name>-alternatives. Outside a comparison section a "vs" page counts only when the site
    is one side, so /blog/pro-vs-agency (two of its own plans) names nobody; a /compare/<slug>
    without "vs" is usually a topic page and is skipped. Each name comes back once, with the
    first path that names it."""
    own = _name_key(_host(host).split(".")[0])
    found: dict[str, dict] = {}
    for page in site_pages or []:
        path = str((page or {}).get("path") or "").strip("/").casefold()
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            continue
        last = segments[-1]
        section = segments[-2] if len(segments) > 1 else ""
        pair = COMPARISON_JOIN.split(last) if COMPARISON_JOIN.search(last) else []
        if pair:
            sides = {_name_key(COMPARISON_SUFFIX.sub("", side)) for side in pair}
            slugs = pair if own in sides or section in COMPARISON_SECTIONS else []
        elif section in NAMED_SECTIONS:
            slugs = [last]
        elif COMPARISON_SUFFIX.search(last):
            slugs = [COMPARISON_SUFFIX.sub("", last)]
        else:
            slugs = []
        for slug in slugs:
            slug = COMPARISON_SUFFIX.sub("", slug).strip("-")
            key = _name_key(slug)
            if (
                len(key) < 2
                or not re.fullmatch(r"[a-z0-9-]{2,60}", slug)
                or key == own
                or slug in NOT_A_NAME
                or key in found
            ):
                continue
            name = " ".join(word.capitalize() for word in slug.split("-"))
            found[key] = {"name": name, "path": "/" + path}
        if len(found) >= MAX_SITE_COMPARISONS:
            break
    return list(found.values())


# How much a lead's source says it is a real competitor: the site's own comparison pages and an
# earlier checked set come first; sites that only rank for the same searches come last.
SOURCE_WEIGHTS = {
    "site_comparison_page": 3,
    "earlier_research": 3,
    "competitor_watch": 2,
    "audit_buyer_panel": 2,
    "audit_cited_in_ai_answers": 1,
    "keyword_search_competitor": 0.5,
}


def merge_competitors(
    *,
    host,
    audit=None,
    keyword_evidence=None,
    watch=None,
    watch_report=None,
    site_pages=None,
    earlier=None,
):
    """The competitors earlier steps found, with where each came from, and the other sites AI
    answers cite.

    Competitors come from the audit's buyer panel (`competitor_names`, the names a buyer would
    weigh), the keyword plan's search competitors (sites ranking for the same searches) and the
    newest competitor.watch report. A competitor whose site the audit's AI answers also cite
    gains that source. One two sources agree on is `corroborated`. Channels and marketplaces
    are never competitors. The cited sites that are not competitors (documentation, publishers,
    directories) come back separately: they show where answers come from.

    From content-editorial-v11 two more sources lead: the comparison pages the site already has
    (`site_pages`, its page list) and the set an earlier plan checked (`earlier`, the parsed
    research/competitors.json), whose label travels with each row. Leads are then ranked by
    how much their sources say, not only by how many there are.
    """
    own = _host(host)
    rows: list[dict] = []

    def find(name, domain):
        label = domain.split(".")[0] if domain else ""
        key = _name_key(name)
        for row in rows:
            if (domain and row["host"] == domain) or (
                key and key in {_name_key(row["name"]), row["label"]}
            ):
                return row
            if label and label == row["label"]:
                return row
        return None

    def add(*, name=None, domain=None, source, detail=None):
        domain = _host(domain) if domain else ""
        if domain == own:
            return
        label = (domain.split(".")[0] if domain else "") or _name_key(name)
        if not label or label in PLATFORMS or _name_key(name) in PLATFORMS:
            return
        row = find(name, domain)
        if row is None:
            row = {"name": name or domain, "host": domain, "label": label, "sources": []}
            rows.append(row)
        if domain and not row["host"]:
            row["host"] = domain
        if name and (not row["name"] or row["name"] == row["host"]):
            row["name"] = name
        if source not in row["sources"]:
            row["sources"].append(source)
        if detail is not None:
            row.setdefault("detail", {})[source] = detail

    for row in (earlier or {}).get("competitors") or []:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"].strip():
            add(
                name=" ".join(row["name"].split())[:120],
                domain=row.get("host") if isinstance(row.get("host"), str) else None,
                source="earlier_research",
                detail={
                    k: row[k] for k in ("label", "use") if isinstance(row.get(k), str) and row[k]
                },
            )
    for found in site_comparisons(site_pages, host) if site_pages else []:
        add(name=found["name"], source="site_comparison_page", detail={"path": found["path"]})
    ai = (audit or {}).get("ai_visibility") or {}
    panel = ai.get("panel") or {}
    for name in panel.get("competitor_names") or []:
        if isinstance(name, str) and name.strip():
            add(name=" ".join(name.split())[:120], source="audit_buyer_panel")
    for domain in (keyword_evidence or {}).get("competitors") or []:
        value = domain.get("host") if isinstance(domain, dict) else domain
        if isinstance(value, str):
            add(domain=value, source="keyword_search_competitor")
    for item in watch_competitors(watch_report) if watch_report else []:
        add(name=item["name"], domain=item["host"] or None, source="competitor_watch")
    for change in (watch or {}).get("changes") or []:
        add(
            name=change.get("name"),
            domain=change.get("competitor"),
            source="competitor_watch",
            detail={"change": change.get("change"), "url": change.get("url")},
        )
    cited = {}
    for row in ai.get("cited_domains") or []:
        if not isinstance(row, dict) or not row.get("domain"):
            continue
        domain = _host(row["domain"])
        if not domain or domain == own or domain.split(".")[0] in PLATFORMS:
            continue
        match = find(None, domain)
        if match is not None:
            if "audit_cited_in_ai_answers" not in match["sources"]:
                match["sources"].append("audit_cited_in_ai_answers")
            continue
        cited[domain] = cited.get(domain, 0) + (row.get("answers") or 0)

    def weight(row):
        score = sum(SOURCE_WEIGHTS.get(source, 1) for source in row["sources"])
        if (row.get("detail") or {}).get("earlier_research", {}).get("label") == "drift":
            score -= SOURCE_WEIGHTS["earlier_research"]
        return score

    ranked = sorted(
        rows,
        key=lambda row: (-len(row["sources"]), "audit_buyer_panel" not in row["sources"]),
    )
    if earlier is not None or site_pages is not None:
        ranked = sorted(rows, key=lambda row: -weight(row))
    competitors = [
        {
            "name": row["name"],
            "host": row["host"],
            "sources": row["sources"],
            "corroborated": len(row["sources"]) >= 2,
            **({"detail": row["detail"]} if row.get("detail") else {}),
        }
        for row in ranked[:30]
    ]
    cited_sites = [
        {"domain": domain, "answers": answers}
        for domain, answers in sorted(cited.items(), key=lambda item: -item[1])[:20]
    ]
    return competitors, cited_sites


# --- the competitor set Tin keeps -------------------------------------------------------


def _https(value):
    text = _text(value, 500)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    return text if parsed.scheme == "https" and parsed.hostname else ""


def competitor_rows(portfolio):
    """The competitors the agent settled, cleaned to what Tin keeps; rows without a name or a
    known label are left out."""
    rows, seen = [], set()
    for raw in portfolio.get("competitors") or []:
        if not isinstance(raw, dict):
            continue
        name, label = _text(raw.get("name"), 120), raw.get("label")
        key = _name_key(name)
        if not name or label not in COMPETITOR_LABELS or key in seen:
            continue
        seen.add(key)
        channels = raw.get("channels") if isinstance(raw.get("channels"), list) else []
        row = {
            "name": name,
            "host": _host(raw.get("host")),
            "label": label,
            "use": _text(raw.get("use"), 40),
            "why": _text(raw.get("why"), 500),
            "does": _text(raw.get("does"), 300),
            "buyer": _text(raw.get("buyer"), 200),
            "evidence_url": _https(raw.get("evidence_url")),
            "channels": [c for c in CHANNELS if c in channels],
        }
        rows.append({k: v for k, v in row.items() if v})
        if len(rows) >= MAX_COMPETITORS:
            break
    return rows


def research_channels(portfolio):
    """What the agent searched in each channel: query and find counts, a few names each."""
    research = portfolio.get("competitor_research")
    out = []
    for raw in (research.get("channels") if isinstance(research, dict) else None) or []:
        if not isinstance(raw, dict) or raw.get("channel") not in CHANNELS:
            continue
        queries = _texts(raw.get("queries"), 200, 40)
        found = _texts(raw.get("found"), 120, 60)
        out.append(
            {
                "channel": raw["channel"],
                "queries": len(queries),
                "found": found[:30],
                **({"notes": _text(raw.get("notes"), 500)} if raw.get("notes") else {}),
            }
        )
    return out


def competitors_document(portfolio, *, run_id, host, today, portfolio_path):
    """research/competitors.json for the next plan, or None when the agent labelled nothing
    (an earlier set then stays as it was)."""
    rows = competitor_rows(portfolio)
    if not rows:
        return None
    document = {
        "schema": COMPETITORS_SCHEMA,
        "host": _host(host),
        "updated_at": str(today),
        "plan_run_id": str(run_id),
        "portfolio": portfolio_path,
        "note": "The competitive set the content plan's research settled: each competitor's "
        "label (direct, adjacent, diy, or drift for a tool that does another job), what its "
        "own site says and where it was found. The next plan starts from this file; change a "
        "label or use here and it keeps your change unless the competitor's site shows "
        "otherwise.",
        "competitors": rows,
        "channels": research_channels(portfolio),
    }
    encoded = canonical_json(document)
    while len(encoded) > COMPETITORS_MAX_BYTES and document["competitors"]:
        document["competitors"].pop()
        encoded = canonical_json(document)
    return encoded


def read_competitors(raw, *, today):
    """The earlier set from research/competitors.json for the brief, or None when the file is
    missing or unreadable. `age_days` counts from its `updated_at`."""
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("schema") != COMPETITORS_SCHEMA:
        return None
    rows = competitor_rows({"competitors": value.get("competitors")})
    if not rows:
        return None
    age = None
    try:
        age = (today - date.fromisoformat(str(value.get("updated_at")))).days
    except (TypeError, ValueError):
        pass
    return {
        "path": COMPETITORS_PATH,
        "updated_at": str(value.get("updated_at") or ""),
        "age_days": age if isinstance(age, int) and age >= 0 else None,
        "competitors": rows,
    }


# --- the brief the agent reads ----------------------------------------------------------


def slots(context) -> int:
    return len(context["editable"]) * context["capacity"]


def brief_documents(
    context, pages, *, competitors, sources, run_id, cited_sites=(), earlier=None
) -> dict[str, bytes]:
    """The files Tin publishes for the planning agent, keyed by project path.

    `sources` names the project files the agent should read (the audit and keyword reports, the
    competitor.watch report, Page decisions, the traffic snapshot, positioning files).
    """
    plan = context["plan"]
    editable = set(context["editable"])
    weeks = [
        {"id": b["id"], "due_date": b["due_date"]} for b in plan["batches"] if b["id"] in editable
    ]
    research = context["research"] or {}
    site = research.get("site_pages") or {"pages": [], "omitted": 0}
    existing = [
        {
            "id": item["id"],
            "week": batch["id"],
            "title": item["title"],
            "intent": item["intent"],
            "action": item["action"],
            "destination": item["destination"],
            **({"kind": item["kind"]} if item.get("kind") else {}),
            "editable": batch["id"] in editable,
        }
        for batch in plan["batches"]
        for item in batch["items"]
    ]
    program = {
        "run_id": str(run_id),
        "mode": context["mode"],
        "host": plan["host"],
        "market": plan["market"],
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "pieces_per_week": context["capacity"],
        "weeks": weeks,
        "slots": slots(context),
        "instruction": context["instruction"],
        "buyer_context": (research.get("scope") or {}).get("buyer_context"),
    }
    files = {
        "program": program,
        "existing_items": existing,
        "competitors": competitors,
        **(
            {"competitor_research": context["competitor_research"]}
            if context.get("competitor_research")
            else {}
        ),
        **(
            {"earlier_competitor_research": earlier}
            if context.get("competitor_research") and earlier
            else {}
        ),
        "ai_cited_sites": list(cited_sites),
        "sources": sources,
        "formats": FORMATS,
        "site_signals": research.get("site_signals"),
        "positioning": [
            {k: f[k] for k in ("path", "sha256", "truncated")}
            for f in context.get("positioning") or []
        ],
        "context_files": [f["path"] for f in context.get("files") or []],
        "output": {
            "path": None,  # The procedure's output path is in its own run brief.
            "schema": SCHEMA,
            "start_marker": START,
            "end_marker": END,
        },
    }
    research_view = {
        "note": "Source rows you may cite by source_id. Keyword groups and exclusions are "
        "the keyword plan's judgment and can be wrong.",
        "rows": research.get("rows", []),
        "excluded": research.get("excluded", []),
    }
    pages_view = {
        "note": "pages: text Tin read from the live site (cite an inspected page by its "
        "source_id), with its title, meta description and H1-H3 outline when Tin kept them. "
        "site_pages: every address Tin knows on the site, by path, with where it was seen; "
        "an address is not page content.",
        "pages": [
            {
                k: p.get(k)
                for k in PAGE_VIEW_FIELDS
                if k in ("page_id", "source_id", "url", "status", "text") or k in p
            }
            for p in pages["pages"]
        ],
        "omitted_candidates": pages.get("omitted_candidates", 0),
        **(
            {"omitted_known_pages": pages["omitted_known_pages"]}
            if "omitted_known_pages" in pages
            else {}
        ),
        "site_pages": site.get("pages", []),
        "site_pages_omitted": site.get("omitted", 0),
    }
    folder = brief_paths(run_id)
    documents = {
        folder["context.json"]: canonical_json(files),
        folder["research.json"]: canonical_json(research_view),
        folder["pages.json"]: canonical_json(pages_view),
    }
    documents[folder["BRIEF.md"]] = (
        render_research_brief(program, competitors, sources, folder, cited_sites, earlier)
        if context.get("competitor_research")
        else render_brief(program, competitors, sources, folder, cited_sites)
    ).encode()
    for name, limit in BRIEF_FILES.items():
        if len(documents[folder[name]]) > limit:
            raise ValueError(f"The planning brief's {name} exceeds its bound.")
    return documents


# What the agent sees of each page Tin read; v10 pages also carry their title and outline.
PAGE_VIEW_FIELDS = (
    "page_id",
    "source_id",
    "url",
    "status",
    "title",
    "description",
    "outline",
    "text",
)


SOURCE_LABELS = {
    "site_comparison_page": "the site already has a comparison page for it",
    "earlier_research": "in the earlier competitor research",
    "competitor_watch": "watched by competitor.watch",
    "audit_buyer_panel": "named in the audit's buyer panel",
    "audit_cited_in_ai_answers": "cited in the audit's AI answers",
    "keyword_search_competitor": "ranks for the same searches (often a publisher, directory "
    "or a tool for another job)",
}


def _lead_line(row):
    detail = row.get("detail") or {}
    parts = []
    for source in row["sources"]:
        text = SOURCE_LABELS.get(source, source)
        if source == "site_comparison_page" and detail.get(source, {}).get("path"):
            text += f" ({detail[source]['path']})"
        if source == "earlier_research" and detail.get(source, {}).get("label"):
            text += f", labelled {detail[source]['label']}"
        parts.append(text)
    return (
        f"- {row['name']}" + (f" ({row['host']})" if row["host"] else "") + ": " + "; ".join(parts)
    )


def _brief_head(program, sources, folder):
    lines = [
        f"# Content program brief: {program['host']}",
        "",
        f"Mode: {program['mode']}. Market {program['market']}. "
        f"{program['start_date']} to {program['end_date']}: {len(program['weeks'])} weeks to "
        f"plan, {program['pieces_per_week']} pieces a week, {program['slots']} slots.",
        "",
        f"Instruction: {program['instruction']}",
        "",
        "## Files",
        "",
        f"- `{folder['context.json']}`: the program, the weeks, items already planned, the "
        "competitor list, Page decisions and traffic signals, and where every source lives.",
        f"- `{folder['research.json']}`: the research rows you cite by `source_id`.",
        f"- `{folder['pages.json']}`: the pages Tin read and the site's page list.",
    ]
    for label, path in sources.items():
        if path:
            lines.append(f"- {label}: `{path}`")
    return lines


def _cited_lines(cited_sites, heading):
    if not cited_sites:
        return []
    return ["", heading, ""] + [
        f"- {row['domain']} ({row['answers']} answers)" for row in cited_sites
    ]


def render_brief(program, competitors, sources, folder, cited_sites=()):
    lines = _brief_head(program, sources, folder)
    lines += ["", "## Competitors earlier steps found", ""]
    if not competitors:
        lines.append(
            "None. Find them from the positioning files, the audit's AI answers and search "
            "before planning."
        )
    for row in competitors:
        lines.append(
            f"- {row['name']}"
            + (f" ({row['host']})" if row["host"] else "")
            + f": {', '.join(row['sources'])}"
            + (" · corroborated" if row["corroborated"] else "")
        )
    lines += _cited_lines(cited_sites, "## Other sites AI answers cite for the buyer questions")
    return "\n".join(lines) + "\n"


def render_research_brief(program, competitors, sources, folder, cited_sites=(), earlier=None):
    """content-editorial-v11's brief: the merged list is leads for the competitor research
    phase, each with what its sources say, and the earlier checked set when there is one."""
    lines = _brief_head(program, sources, folder)
    lines += [
        "",
        "## Competitor research",
        "",
        "Settle the competitive set before planning, with the research channels in "
        "`$content-portfolio` section 2. The leads below are where to start, not a checked "
        "list: sites that rank for the same searches or that AI answers cite are usually "
        "publishers, directories or tools for another job.",
    ]
    if earlier:
        age = earlier.get("age_days")
        fresh = isinstance(age, int) and age <= COMPETITORS_FRESH_DAYS
        lines += [
            "",
            f"Earlier research: `{earlier['path']}`, checked {earlier.get('updated_at')}"
            + (f" ({age} days ago)" if isinstance(age, int) else "")
            + f", {len(earlier.get('competitors') or [])} competitors with labels. "
            + (
                "It is recent: start from it, check the new leads and run the launches channel "
                "for anything new; run every channel again only if the product or its buyer "
                "changed."
                if fresh
                else f"It is older than {COMPETITORS_FRESH_DAYS} days: run every channel again "
                "and update it."
            ),
        ]
    lines += ["", "### Leads", ""]
    if not competitors:
        lines.append("None. Find them with the research channels.")
    lines += [_lead_line(row) for row in competitors]
    lines += _cited_lines(
        cited_sites, "### Other sites AI answers cite (where answers come from, not competitors)"
    )
    return "\n".join(lines) + "\n"


# --- the agent's portfolio ---------------------------------------------------------------


def extract(text: str) -> dict:
    """The portfolio JSON between its markers."""
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("The portfolio needs exactly one marked JSON block.")
    body = text.split(START, 1)[1].split(END, 1)[0].strip()
    body = re.sub(r"^```(?:json)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body)
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise ValueError("The portfolio block is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("The portfolio block must be a JSON object.")
    return value


def validate(text: str) -> dict:
    """The procedure's output check: the shape Tin needs to read, nothing more. Item-level
    problems are left to `normalize`, which leaves an unusable item out instead of failing."""
    if len(text.encode()) > MAX_PORTFOLIO_BYTES:
        raise ValueError("The portfolio exceeds its size bound.")
    value = extract(text)
    if value.get("schema") != SCHEMA:
        raise ValueError(f"The portfolio must name schema {SCHEMA}.")
    if not isinstance(value.get("strategy"), str) or not value["strategy"].strip():
        raise ValueError("The portfolio needs a strategy.")
    opportunities = value.get("opportunities")
    if not isinstance(opportunities, list) or not opportunities:
        raise ValueError("The portfolio needs at least one opportunity.")
    if len(opportunities) > MAX_OPPORTUNITIES:
        raise ValueError(f"The portfolio holds at most {MAX_OPPORTUNITIES} opportunities.")
    for item in opportunities:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and item[field].strip() for field in ("title", "brief")
        ):
            raise ValueError("Every opportunity needs a title and a brief.")
    return value


def _text(value, limit):
    text = " ".join(str(value or "").split()) if not isinstance(value, list) else ""
    return text[:limit].strip()


def _texts(value, limit, count):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v, limit) for v in value if isinstance(v, str)) if t][:count]


def _item_id(value, index, taken):
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "")).strip("-")[:56] or f"op{index:03d}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate


def _destination(value, host):
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if value.startswith("/") and not value.startswith("//"):
        value = f"https://{host}{value}"
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.removeprefix("www.") == host.removeprefix("www."):
        value = parsed._replace(netloc=host, scheme="https", query="", fragment="").geturl()
    from tin_lite.content_plan_editorial import clean_url

    return value if clean_url(value, host) else None


def known_sources(context, pages):
    ids = {row["source_id"] for row in (context["research"] or {}).get("rows", [])}
    ids |= {f["source_id"] for f in context.get("files") or []}
    ids |= {p["source_id"] for p in pages["pages"] if p.get("source_id")}
    return ids


def normalize(context, pages, portfolio, *, file_sizes=None):
    """The usable items in the agent's order, and the proposals left out with a reason.

    `file_sizes` maps project paths to their byte size at the plan's revision, for `file:`
    sources; a file content.generate could not load into a draft's context is kept in the
    item's rationale instead of its sources.
    """
    from tin_lite.content_plan_editorial import decision_conflict, existing_page, page_path

    host = context["plan"]["host"]
    file_sizes = file_sizes or {}
    known = known_sources(context, pages)
    research = context["research"] or {}
    site = research.get("site_pages") or {"pages": []}
    site_paths = {page["path"] for page in site.get("pages", [])}
    inspected = {page_path(p["url"]): p for p in pages["pages"] if p.get("status") == "inspected"}
    decisions = ((research.get("site_signals") or {}).get("page_decisions")) or {}
    editable = set(context["editable"])
    retained = {
        item["id"]: item
        for batch in context["plan"]["batches"]
        if batch["id"] in editable
        for item in batch["items"]
    }
    fixed = [
        item
        for batch in context["plan"]["batches"]
        if batch["id"] not in editable
        for item in batch["items"]
    ]
    taken = {item["id"] for item in fixed}
    seen_titles = {legacy_title(item["title"]) for item in fixed}
    seen_destinations = {item["destination"].rstrip("/") for item in fixed if item["destination"]}
    kept, left_out = [], []
    # v10 plans name the existing pages closest to each new item (context["page_coverage"]).
    nearby_limit = (context.get("page_coverage") or {}).get("nearby", 0)
    refresh_reach = bool(context.get("page_coverage"))
    read = [p for p in pages["pages"] if p.get("status") == "inspected" and p.get("source_id")]
    page_bytes = {p["source_id"]: len(canonical_json(p)) for p in read}

    def leave(raw, reason):
        left_out.append({"title": _text(raw.get("title"), 180) or "(untitled)", "reason": reason})

    # v11: an alternatives or comparison page is for a competitor the research checked; one it
    # labelled drift does another job, so a page "vs" it answers a search nobody makes.
    drift = (
        {
            _name_key(row["name"]): row["name"]
            for row in competitor_rows(portfolio)
            if row["label"] not in PAGE_LABELS
        }
        if context.get("competitor_research")
        else {}
    )

    for index, raw in enumerate(portfolio.get("opportunities") or [], 1):
        if not isinstance(raw, dict):
            continue
        title, brief = _text(raw.get("title"), 180), _text(raw.get("brief"), 1800)
        if not title or not brief:
            leave(raw, "no title or brief")
            continue
        fmt = raw.get("format") if raw.get("format") in FORMATS else None
        action = raw.get("action")
        if action not in {"new_page", "update_page"}:
            action = "update_page" if fmt in {"refresh", "update"} else "new_page"
        fmt = fmt or ("update" if action == "update_page" else "guide")
        if action == "update_page" and fmt in NEW_FORMATS:
            fmt = "update"
        if action == "new_page" and fmt not in NEW_FORMATS:
            fmt = "guide"
        reformatted = ""
        if refresh_reach and fmt == "refresh" and REFRESH_REACH.search(brief):
            # A refresh replaces lines in place; this brief needs the page itself rewritten.
            fmt = "update"
            reformatted = (
                "Planned as an update: the brief changes a table, section or FAQ, which a "
                "refresh cannot."
            )
        rival = _name_key(raw.get("competitor"))
        if fmt in {"alternative", "comparison"} and rival and rival in drift:
            leave(
                raw,
                f"{drift[rival]} is labelled drift in the competitor research: it does another "
                "job, so it gets no alternatives or comparison page",
            )
            continue
        destination = ""
        if action == "update_page":
            destination = _destination(raw.get("destination"), host)
            if not destination:
                leave(raw, "an update needs a page on the site")
                continue
            path = page_path(destination)
            if path not in site_paths and path not in inspected:
                leave(raw, f"{path} is not a page Tin knows on the site")
                continue
        if legacy_title(title) in seen_titles:
            leave(raw, "repeats another item's title")
            continue
        if destination and destination.rstrip("/") in seen_destinations:
            leave(raw, "another item already changes this page")
            continue
        opportunity = {"id": str(raw.get("id") or ""), "title": title, "action": action}
        if action == "new_page" and opportunity["id"] not in retained:
            found = existing_page(opportunity, site, topics=fmt in {"guide", "use_case"})
            if found:
                page, match = found
                leave(raw, f"the site already has {page['path']} ({match} match)")
                continue
        conflict = (
            None
            if opportunity["id"] in retained
            else decision_conflict(opportunity, destination, decisions)
        )
        if conflict:
            leave(raw, conflict)
            continue
        sources, rationale_files = [], []
        file_bytes = 0
        for value in _texts(raw.get("sources"), 500, 40):
            if value in known or value.startswith("https://"):
                sources.append(value)
            elif value.startswith("file:") or "/" in value and not value.startswith("http"):
                path = value.removeprefix("file:")
                size = file_sizes.get(path)
                if (
                    size is not None
                    and size <= DRAFT_FILE_BYTES
                    and (file_bytes + size <= DRAFT_FILES_BYTES)
                ):
                    file_bytes += size
                    sources.append(f"file:{path}")
                elif size is not None:
                    rationale_files.append(path)
        if destination and inspected.get(page_path(destination), {}).get("source_id"):
            sources.append(inspected[page_path(destination)]["source_id"])
        # A comparison or alternatives page is about one competitor; every such page shares
        # "vs" and the product's words, so word overlap would only name the other comparisons.
        nearby = (
            nearby_pages(title, _text(raw.get("target_query"), 200), read, limit=nearby_limit)
            if action == "new_page" and nearby_limit and fmt not in COMPARISON_FORMATS
            else []
        )
        # content.generate loads each page source into a 64 KB context; nearby pages join only
        # while the item's pages stay within NEARBY_EVIDENCE_BYTES.
        used = sum(page_bytes.get(source, 0) for source in sources)
        near_ids = []
        for page in nearby:
            if page["source_id"] in sources or used + page_bytes[page["source_id"]] > (
                NEARBY_EVIDENCE_BYTES
            ):
                continue
            used += page_bytes[page["source_id"]]
            near_ids.append(page["source_id"])
        nearby = [page for page in nearby if page["source_id"] in near_ids]
        sources = list(dict.fromkeys(sources))[: MAX_SOURCES - len(near_ids)] + near_ids
        strength = raw.get("evidence_strength")
        if strength not in STRENGTHS:
            strength = "inferred" if sources else "bet"
        if strength == "measured" and not any(
            s.startswith(("keyword:", "group:", "refresh:", "efficacy:")) for s in sources
        ):
            strength = "inferred"
        kind = (
            legacy.ANSWER
            if fmt == "answer"
            else legacy.REFRESH
            if fmt == "refresh"
            else legacy.ARTICLE
        )
        if kind == legacy.ANSWER and action != "new_page":
            kind = legacy.ARTICLE
        item = {
            "id": opportunity["id"] if opportunity["id"] in retained else "",
            "title": title,
            "brief": brief,
            "intent": _text(raw.get("intent") or raw.get("target_query") or title, 500),
            "action": action,
            "destination": destination,
            "source_ids": sources,
            "verification": (
                ([nearby_check(nearby)] if nearby else [])
                + (_texts(raw.get("verification"), 500, 8) or [DEFAULT_CHECK])
            )[:8],
            "readiness": "needs_verification",
            "format": fmt,
            "evidence_strength": strength,
        }
        query = _text(raw.get("target_query"), 200)
        if query:
            item["target_query"] = query
        if kind != legacy.ARTICLE:
            item["kind"] = kind
        provenance = retained.get(opportunity["id"], {})
        for key in ("source", "evidence"):
            if provenance.get(key):
                item[key] = provenance[key]
        if not item["id"]:
            item["id"] = _item_id(raw.get("id"), index, taken)
        else:
            taken.add(item["id"])
        seen_titles.add(legacy_title(title))
        if destination:
            seen_destinations.add(destination.rstrip("/"))
        kept.append(
            {
                "item": item,
                "rationale": {
                    key: value
                    for key, value in (
                        ("why", _text(raw.get("why_this"), 900)),
                        ("win_case", _text(raw.get("win_case"), 900)),
                        ("metric", _text(raw.get("metric"), 300)),
                        ("competitor", _text(raw.get("competitor"), 200)),
                        ("family", _text(raw.get("family"), 80)),
                        ("rejected", "; ".join(_texts(raw.get("rejected"), 200, 6))),
                        ("files", ", ".join(rationale_files)),
                        ("nearby", ", ".join(page_path(page["url"]) for page in nearby)),
                        ("format", reformatted),
                    )
                    if value
                },
            }
        )
    return kept, left_out


def _stem(word):
    """A crude English stem, so "replies", "receiving" and "messages" meet "reply", "receive"
    and "message"."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


def _words(text):
    return {
        _stem(word)
        for word in re.findall(r"[a-z0-9]+", str(text).casefold())
        if len(word) > 1 and word not in NEARBY_STOPWORDS
    }


def nearby_pages(title, query, pages, *, limit):
    """The read pages closest to a proposed new page, best first, at most `limit`.

    The item's title and target search are matched against each page's title, meta
    description, outline and address (counting double) and its opening text. Each word counts
    by how rare it is on the site, so the brand name and words every page shares count for
    nothing. A page must carry at least NEARBY_MIN_SCORE of the item's words that appear
    anywhere on the site.
    """
    from tin_lite.content_plan_editorial import page_path

    docs, seen = [], set()
    for page in pages:
        path = page_path(page["url"])
        # A redirect alias is the same page. The home and about pages touch every topic, and
        # legal and sign-in pages none: neither is the page a new article would repeat. A
        # translated copy (/blog/zh/...) is another language's page.
        if (
            path in seen
            or path == "/"
            or path.rsplit("/", 1)[-1] in NEARBY_SKIPPED_PAGES
            or LANGUAGE_SEGMENT.search(path)
        ):
            continue
        seen.add(path)
        head = _words(
            " ".join(
                [
                    page.get("title") or "",
                    page.get("description") or "",
                    " ".join(page.get("outline") or []),
                    path.replace("-", " ").replace("/", " "),
                ]
            )
        )
        docs.append((page, head, _words(page.get("text") or "")))
    if not docs:
        return []
    frequency = Counter(word for _, head, body in docs for word in head | body)
    wanted = {word for word in _words(f"{title} {query}") if frequency[word]}
    weight = {word: math.log((len(docs) + 1) / (frequency[word] + 1)) for word in wanted}
    total = 2 * sum(weight.values())
    if not total:
        return []
    scored = []
    for page, head, body in docs:
        score = sum(
            weight[word] * (2 if word in head else 1 if word in body else 0) for word in wanted
        )
        if score / total >= NEARBY_MIN_SCORE:
            scored.append((score, page))
    scored.sort(key=lambda entry: (-entry[0], entry[1]["url"]))
    return [page for _, page in scored[:limit]]


def nearby_check(pages):
    from tin_lite.content_plan_editorial import page_path

    paths = ", ".join(page_path(page["url"]) for page in pages)
    return (
        f"Read {paths} first: if one already answers this reader, the outcome is "
        "already_covered or an update of that page, not a new page."
    )[:500]


def with_decision_refreshes(context, kept):
    """Page decisions' refreshes the agent neither planned nor excluded: v7's refresh items for
    the pages the newest Page decisions marks, after the agent's own items, so they take open
    weeks or join the backlog without displacing the agent's priorities."""
    from tin_lite.content_plan_editorial import decision_refreshes, page_path

    items = [entry["item"] for entry in kept] + [
        item for batch in context["plan"]["batches"] for item in batch["items"]
    ]
    decisions = ((context["research"] or {}).get("site_signals") or {}).get("page_decisions") or {}
    # A page Page decisions also keeps, merges or retires is not refreshed on its say-so.
    ruled_out = set(decisions.get("keep") or []) | set(decisions.get("cut") or {})
    added = decision_refreshes(
        context,
        context["plan"],
        targeted={page_path(i["destination"]) for i in items if i["destination"]} | ruled_out,
        titles={legacy_title(i["title"]) for i in items},
    )
    for item in added:
        item["format"], item["evidence_strength"] = "refresh", "measured"
    return kept + [
        {"item": item, "rationale": {"why": "Page decisions marked this page for a refresh."}}
        for item in added
    ]


def legacy_title(title):
    return " ".join(str(title).casefold().split())


def fill(context, kept):
    """Put the kept items on the editable weeks in priority order, filling each week up to its
    capacity before the next. A revision keeps a retained item in its week when it still fits.
    Returns the plan and the items that did not fit (the backlog)."""
    plan = deepcopy(context["plan"])
    capacity = context["capacity"]
    editable = [b for b in plan["batches"] if b["id"] in context["editable"]]
    placed = {item["id"]: batch["id"] for batch in editable for item in batch["items"]}
    for batch in editable:
        batch["items"] = []
    by_id = {batch["id"]: batch for batch in editable}
    rest = []
    if context["mode"] == "revision":
        for entry in kept:
            batch = by_id.get(placed.get(entry["item"]["id"], ""))
            if batch is not None and len(batch["items"]) < capacity:
                batch["items"].append(entry["item"])
            else:
                rest.append(entry["item"])
    else:
        rest = [entry["item"] for entry in kept]
    backlog = []
    for item in rest:
        batch = next((b for b in editable if len(b["items"]) < capacity), None)
        if batch is None:
            backlog.append(item)
        else:
            batch["items"].append(item)
    return plan, backlog


def coverage(context, kept, left_out, backlog, portfolio, plan):
    editable = [b for b in plan["batches"] if b["id"] in context["editable"]]
    planned = sum(len(b["items"]) for b in editable)
    items = [i for b in editable for i in b["items"]]
    return {
        "scope": context["mode"],
        "planner": RESEARCH_KEY,
        "selected_batches": len(editable),
        "planned_items": planned,
        "capacity": slots(context),
        "unused_capacity": slots(context) - planned,
        "empty_batches": sum(not b["items"] for b in editable),
        "first_empty_week": next((b["id"] for b in editable if not b["items"]), None),
        "by_format": {
            f: sum(i.get("format") == f for i in items)
            for f in FORMATS
            if any(i.get("format") == f for i in items)
        },
        "by_strength": {s: sum(i.get("evidence_strength") == s for i in items) for s in STRENGTHS},
        "gaps": _texts(portfolio.get("gaps"), 900, 12),
        "excluded": _texts(portfolio.get("excluded"), 900, 20),
        "left_out": left_out,
        "backlog": [{"id": i["id"], "title": i["title"]} for i in backlog],
        "decisions": [
            {"item_id": entry["item"]["id"], **entry["rationale"]}
            for entry in kept
            if entry["item"]["id"] in {i["id"] for i in items}
        ],
        "competitors": (
            competitor_rows(portfolio)
            if context.get("competitor_research")
            else [
                {
                    k: c.get(k)
                    for k in ("name", "host", "use", "why")
                    if isinstance(c, dict) and c.get(k)
                }
                for c in (portfolio.get("competitors") or [])[:30]
                if isinstance(c, dict)
            ]
        ),
        **(
            {"competitor_channels": research_channels(portfolio)}
            if context.get("competitor_research")
            else {}
        ),
        "families": [
            {k: f.get(k) for k in ("id", "name", "why", "members") if f.get(k)}
            for f in (portfolio.get("families") or [])[:10]
            if isinstance(f, dict)
        ],
    }


def strategy(portfolio, quality):
    text = _text(portfolio.get("strategy"), 4200) or "Planned by the content planning agent."
    notes = []
    if quality["left_out"]:
        notes.append(
            f"Tin left out {len(quality['left_out'])} proposal(s): "
            + "; ".join(f"{e['title']} ({e['reason']})" for e in quality["left_out"][:6])
            + "."
        )
    if quality["unused_capacity"] > 0:
        notes.append(
            f"{quality['planned_items']} of {quality['capacity']} slots are planned; weeks from "
            f"{quality['first_empty_week']} on are open."
        )
    return (text + ("\n\n" + " ".join(notes) if notes else ""))[:5000]


def summary_lines(quality, pages):
    """PLAN.md's coverage section for a v9 plan."""
    from tin_lite.keyword_plan import markdown_text

    inspected = sum(p["status"] == "inspected" for p in pages["pages"]) if pages else 0
    formats = ", ".join(
        f"{legacy.FORMAT_LABELS[name]} {count}" for name, count in quality["by_format"].items()
    )
    strengths = ", ".join(f"{s} {n}" for s, n in quality["by_strength"].items() if n)
    lines = [
        "## Coverage",
        "",
        f"{quality['planned_items']} of {quality['capacity']} slots planned over "
        f"{quality['selected_batches']} weeks"
        + (
            f"; weeks from {quality['first_empty_week']} on are open."
            if quality["first_empty_week"]
            else "; every week has its pieces."
        ),
        "",
        *([f"Formats: {formats}.", ""] if formats else []),
        *([f"Evidence: {strengths}.", ""] if strengths else []),
        f"The planning agent read the project's files and {inspected} live pages Tin fetched. "
        "Every brief still needs its checks before drafting.",
        "",
    ]
    if quality["competitors"]:
        lines += ["### Competitors", ""]
        lines += [
            f"- {markdown_text(c.get('name', ''))}"
            + (f" ({markdown_text(c['host'])})" if c.get("host") else "")
            + (f": {markdown_text(c['use'])}" if c.get("use") else "")
            + (f". {markdown_text(c['why'])}" if c.get("why") else "")
            for c in quality["competitors"]
        ] + [""]
    if quality["families"]:
        lines += ["### Page families", ""]
        for family in quality["families"]:
            members = family.get("members")
            count = f" ({len(members)} pages)" if isinstance(members, list) else ""
            lines.append(
                f"- {markdown_text(str(family.get('name') or family.get('id')))}{count}"
                + (f": {markdown_text(str(family['why']))}" if family.get("why") else "")
            )
        lines.append("")
    for heading, key in (
        ("Evidence that would unlock more work", "gaps"),
        ("Excluded", "excluded"),
    ):
        if quality[key]:
            lines += [f"### {heading}", ""]
            lines += [f"- {markdown_text(value)}" for value in quality[key]] + [""]
    if quality["left_out"]:
        lines += ["### Left out by Tin", ""]
        lines += [
            f"- {markdown_text(e['title'])}: {markdown_text(e['reason'])}."
            for e in quality["left_out"]
        ] + [""]
    if quality["backlog"]:
        lines += ["### Backlog (no week left)", ""]
        lines += [f"- {markdown_text(e['title'])}" for e in quality["backlog"]] + [""]
    return lines
