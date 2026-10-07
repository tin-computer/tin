"""Resolve exact, receipt-verified research publications and bounded project context."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite.keyword_plan import LIMITS as KEYWORD_LIMITS
from tin_lite.keyword_plan import paths as keyword_paths
from tin_lite.organic_audit import ARTIFACT_LIMITS, audit_hosts, audit_paths, canonical_json, digest
from tin_lite.project_files import safe_project_file_path


async def research_sources(
    *, database, storage, project, inputs, typed=False, planned=None, published=(), signals=None
):
    """The pinned audit and keyword research as source rows.

    `typed` (content-editorial-v7) adds one row per page a refresh could fix, from the audit's
    search findings and `planned`, Page decisions' refresh rows (see page_decision_refreshes),
    and the whole site's page list (`site_pages`), with `published`, the pages Tin put on the
    site itself (see published_pages). With `signals` (site_signals), Page decisions and the
    traffic snapshot shape the refresh rows, add one row per page Page decisions refreshes, and
    travel as `site_signals` for the planner.
    """
    sources, loaded = {}, {}
    for kind, executor, prefix, source_paths, limits in (
        ("audit", "organic.audit", "organic", audit_paths, ARTIFACT_LIMITS),
        ("keyword", "organic.keyword_plan", "keyword", keyword_paths, KEYWORD_LIMITS),
    ):
        source = await database.get_run(UUID(inputs[f"{kind}_run_id"]))
        if (
            source is None
            or source.project_id != project.id
            or source.executor != executor
            or source.status.value != "succeeded"
            or not source.canonical_commit_sha
        ):
            raise ValueError(f"Choose a successful {kind} publication in this project.")
        receipt = await database.get_effect(f"{prefix}:{source.id}:publish")
        publication = receipt.result if receipt and receipt.status == "completed" else {}
        if publication.get("canonical_commit_sha") != source.canonical_commit_sha:
            raise ValueError(f"The {kind} publication receipt does not match its run.")
        documents = {}
        for name, path in source_paths(str(source.id)).items():
            content = await storage.read_canonical_artifact(
                repo_id=project.state_repo_id, commit_sha=source.canonical_commit_sha, path=path
            )
            if not 0 < len(content) <= limits[name]:
                raise ValueError(f"The {kind} source exceeds its read contract.")
            documents[path] = content.decode("utf-8")
        if digest(documents) != publication.get("documents_sha256"):
            raise ValueError(f"The {kind} bundle failed receipt verification.")
        loaded[kind] = {
            name: json.loads(documents[path])
            for name, path in source_paths(str(source.id)).items()
            if name.endswith(".json")
        }
        evidence = loaded[kind]["evidence.json"]
        if evidence.get("run_id") != str(source.id) or evidence.get("project_id") != str(
            project.id
        ):
            raise ValueError("Research evidence has a different owner or run.")
        identity = evidence if kind == "audit" else loaded[kind]["keywords.json"]
        if identity.get("definition_commit_sha") != source.definition_commit_sha:
            raise ValueError("The research definition differs from its pinned run.")
        sources[kind] = {
            "run_id": str(source.id),
            "revision": source.canonical_commit_sha,
            "documents_sha256": publication["documents_sha256"],
            "paths": source_paths(str(source.id)),
        }
    audit = loaded["audit"]["evidence.json"]
    keywords = loaded["keyword"]["keywords.json"]
    if (
        keywords["scope"]["host"] not in audit_hosts(audit["scope"])
        or audit["scope"]["market"] != keywords["scope"]["market"]
        or audit["scope"].get("language") != "en"
        or keywords["scope"].get("language") != "en"
    ):
        raise ValueError(
            "Audit and keywords must target the same host, market and English language."
        )
    if digest(keywords) != loaded["keyword"]["evidence.json"].get("inventory_sha256"):
        raise ValueError("Keyword inventory failed evidence verification.")
    findings = loaded["audit"]["findings.json"]
    if digest(audit) != findings.get("evidence_sha256"):
        raise ValueError("Audit findings failed evidence verification.")
    rows = []
    for finding in findings["findings"]:
        if finding.get("category") == "content":
            rows.append({"source_id": f"audit:{finding['id']}", "data": finding})
    for group in keywords["groups"]:
        rows.append({"source_id": f"group:{group['id']}", "data": group})
    # Keep every keyword (including exclusions) but compact repeated provider metadata.
    # The full original observations remain retrievable through the pinned source reference.
    for keyword in keywords["keywords"]:
        observations = keyword.get("observations", [])
        rows.append(
            {
                "source_id": f"keyword:{keyword['id']}",
                "data": {
                    "id": keyword["id"],
                    "keyword": keyword["keyword"],
                    "observations": [
                        {
                            key: row.get(key)
                            for key in (
                                "source_id",
                                "observed_at",
                                "search_volume",
                                "keyword_difficulty",
                                "ranking_url",
                                "position",
                                "clicks",
                                "impressions",
                            )
                        }
                        for row in observations[:2]
                    ],
                    "observations_omitted": keyword.get("observations_omitted", 0)
                    + max(0, len(observations) - 2),
                },
            }
        )
    extra = {}
    if typed:
        typed_rows, extra = typed_research_rows(
            findings, audit, keywords, planned=planned, published=published, signals=signals
        )
        rows.extend(typed_rows)
    return {
        **extra,
        "sources": sources,
        "scope": {
            key: keywords["scope"][key] for key in ("host", "market", "language", "buyer_context")
        },
        "rows": rows,
        "excluded": keywords["excluded"],
        "technical_findings_count": sum(
            finding.get("category") != "content" for finding in findings["findings"]
        ),
        "page_candidates": [
            {"url": page["url"], "title": page.get("title")}
            for page in audit.get("crawl", {}).get("pages", [])
        ],
        "limitations": [
            "Frozen research; no current page contents verified.",
            "Keyword group and exclusion judgments may be wrong.",
        ],
    }


def typed_research_rows(findings, audit, keywords, *, planned=None, published=(), signals=None):
    """The v7 additions to research: refresh rows (shaped by Page decisions and the traffic
    snapshot), one row per page Page decisions refreshes, the site's page list and the signals
    the planner reads. Returns (rows, extra research fields)."""
    decisions = (signals or {}).get("page_decisions") or {"status": "not read"}
    traffic = (signals or {}).get("traffic") or {"status": "not read"}
    used = traffic.get("status") == "used"
    conversion = traffic_signals(traffic, keywords["scope"]["host"]) if used else None
    rows = refresh_rows(
        findings,
        audit,
        planned,
        weak={page["path"]: page for page in (conversion or {}).get("weak", [])},
        skip=[*(decisions.get("cut") or {}), *(decisions.get("keep") or [])],
    ) + efficacy_rows(decisions)
    note = " ".join(
        part
        for part in (
            signal_notes({"page_decisions": decisions, "traffic": traffic}),
            f"Traffic snapshot ({SNAPSHOT_PATH}): {conversion['note']}."
            if (conversion or {}).get("note")
            else None,
        )
        if part
    )
    return rows, {
        "site_pages": site_pages(audit, keywords=keywords, published=published),
        "site_signals": {
            "page_decisions": decisions,
            "traffic": {
                **{k: v for k, v in traffic.items() if k != "pages"},
                **(conversion or {}),
            },
            "note": note or None,
        },
    }


def refresh_rows(findings, evidence, planned=None, *, weak=None, skip=()):
    """Source rows for the pages a refresh could fix, in order of realistic upside."""
    from tin_lite.content_plan_editorial import MAX_REFRESH_SOURCES, REFRESH_SOURCE_PREFIX
    from tin_lite.content_refresh import plan_candidates

    return [
        {
            "source_id": REFRESH_SOURCE_PREFIX + digest(page["path"])[:20],
            "data": {"kind": "refresh_candidate", "title": f"refresh {page['path']}", **page},
        }
        for page in plan_candidates(
            findings, evidence, planned, limit=MAX_REFRESH_SOURCES, weak=weak, skip=skip
        )
    ]


EFFICACY_SOURCE_PREFIX = "efficacy:"


def efficacy_rows(decisions):
    """One source row per page the newest Page decisions refreshes, so its item can cite it."""
    return [
        {
            "source_id": EFFICACY_SOURCE_PREFIX + digest(path)[:20],
            "data": {
                "kind": "page_decision",
                "title": f"page decision refresh {path}",
                "path": path,
                "decision": "refresh",
                "rule": row["rule"],
                "reason": row["reason"],
                "generated": decisions["generated"],
                "file": EFFICACY_PATH,
            },
        }
        for path, row in (decisions.get("refresh") or {}).items()
    ]


# The whole site as Tin knows it (content-editorial-v7). The bounded page inspection reads at
# most 60 pages; this list holds every page address Tin has evidence for, so the plan never
# proposes a page the site already has. Each page appears once, by normalized path, with every
# source that lists it.
SITE_SOURCES = {
    "sitemap": "the audit's sitemap read",
    "search_console": "Search Console pages with impressions",
    "crawl": "the audit's crawl and page reads",
    "tin_published": "pages Tin published and found live",
    "keywords": "ranking pages in the keyword research",
}
MAX_SITE_PAGES = 2000
SITE_PAGES_BYTES = 200_000
SITE_TITLE_CHARS = 120
# Addresses that are files, not pages.
NOT_PAGES = (".xml", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")
PUBLISHED_PAGES_SQL = """
    SELECT receipt.result->'check'->>'url' AS url, receipt.result->'base'->>'title' AS title
    FROM effect_receipts AS receipt
    JOIN workflow_runs AS run ON receipt.execution_key = 'page-url:' || run.id::text
    WHERE run.project_id = $1 AND receipt.operation = 'page_url_projection_v1'
      AND receipt.result->'check'->>'live' = 'true'
    ORDER BY receipt.updated_at DESC
    LIMIT 500
"""


async def published_pages(database, project):
    """Pages Tin published to the site itself and then found live (its page URL records)."""
    rows = await database.pool.fetch(PUBLISHED_PAGES_SQL, project.id)
    return [{"url": row["url"], "title": row["title"]} for row in rows if row["url"]]


def site_path(url, host, aliases=()):
    """The page's normalized path on the audited site, or None for another site or a file."""
    from tin_lite.content_refresh import url_key
    from tin_lite.organic_audit import in_scope_url

    if not in_scope_url(url, host, aliases=aliases):
        return None
    path = url_key(url)
    if len(path) > 500 or "//" in path or path.casefold().endswith(NOT_PAGES):
        return None
    return path


def site_pages(evidence, *, keywords=None, published=()):
    """Every page Tin knows on the audited site: one row per normalized path, with its sources.

    Reads the audit's sitemap URLs, its Search Console pages with impressions, its crawl and page
    reads, `published` (pages Tin published and found live) and the ranking pages in the keyword
    research. At most MAX_SITE_PAGES rows and SITE_PAGES_BYTES are kept, pages with the most
    impressions and the most sources first; the rest are counted in `omitted`.
    """
    scope = evidence.get("scope") or {}
    host = scope.get("host")
    aliases = tuple(name for name in audit_hosts(scope) if name != host) if host else ()
    pages = {}

    def add(url, source, *, title=None, impressions=None):
        path = site_path(url, host, aliases) if isinstance(url, str) and host else None
        if path is None:
            return
        page = pages.setdefault(path, {"path": path, "sources": []})
        if source not in page["sources"]:
            page["sources"].append(source)
        if title and not page.get("title"):
            page["title"] = " ".join(str(title).split())[:SITE_TITLE_CHARS]
        if impressions:
            page["impressions"] = max(page.get("impressions", 0), impressions)

    site = evidence.get("site") or {}
    sitemaps = (site.get("files") or {}).get("sitemaps") or {}
    for entry in sitemaps.get("urls") or []:
        add(entry.get("loc") if isinstance(entry, dict) else entry, "sitemap")
    console = (evidence.get("search_console") or {}).get("value") or {}
    for row in console.get("pages") or []:
        impressions = row.get("impressions") if isinstance(row, dict) else None
        if isinstance(impressions, int | float) and impressions > 0:
            add(row.get("url"), "search_console", impressions=impressions)
    for page in (evidence.get("crawl") or {}).get("pages") or []:
        status = page.get("status_code")
        if not isinstance(status, int) or 200 <= status < 400:
            add(page.get("url"), "crawl", title=page.get("title"))
    for page in site.get("pages") or []:
        if page.get("fetch") == "observed":
            add(page.get("url"), "crawl", title=page.get("title"))
    for page in published:
        add(page.get("url"), "tin_published", title=page.get("title"))
    for keyword in (keywords or {}).get("keywords") or []:
        for observation in keyword.get("observations") or []:
            add(observation.get("ranking_url"), "keywords")
    ranked = sorted(
        pages.values(),
        key=lambda page: (-page.get("impressions", 0), -len(page["sources"]), page["path"]),
    )
    kept, size = [], 0
    for page in ranked[:MAX_SITE_PAGES]:
        size += len(canonical_json(page)) + 1
        if size > SITE_PAGES_BYTES:
            break
        kept.append(page)
    return {
        "host": host,
        "pages": sorted(kept, key=lambda page: page["path"]),
        "omitted": len(pages) - len(kept),
        "by_source": {
            source: sum(source in page["sources"] for page in kept) for source in SITE_SOURCES
        },
        "sitemap_capped": bool(sitemaps.get("urls_capped"))
        or bool((evidence.get("trimmed_for_size") or {}).get("sitemap_urls")),
    }


# competitor.watch: the newest succeeded report's material changes, for comparison items.
WATCH_KEY = "competitor.watch"
WATCH_FOLDER = "reports/competitor-watch/"
WATCH_MAX_BYTES = 80_000
WATCH_FENCE = re.compile(r"^```tin-competitor-watch[ \t]*\n(.*?)\n```[ \t]*$", re.S | re.M)
WATCH_LINE = re.compile(r"^- \*\*(?P<name>[^*\n]{1,120})\*\*:\s*(?P<text>\S.*)$")
WATCH_URL = re.compile(r"\((https://[^\s)]{4,490})\)")
LATEST_WATCH_SQL = """
    SELECT run.id, run.canonical_commit_sha, run.artifact_path
    FROM workflow_runs AS run
    JOIN workflows AS workflow ON workflow.id = run.workflow_id
    WHERE run.project_id = $1 AND workflow.key = 'competitor.watch'
      AND workflow.project_id IS NULL
      AND run.status = 'succeeded' AND run.canonical_commit_sha IS NOT NULL
      AND run.artifact_path IS NOT NULL
    ORDER BY run.finished_at DESC NULLS LAST, run.created_at DESC
    LIMIT 1
"""


def _belongs(url, host):
    """Whether a URL is on the competitor's own site (its host or a subdomain of it)."""
    found = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    return found == host or found.endswith("." + host)


def watch_changes(text):
    """The material changes a competitor.watch report lists, each tied to a watched competitor
    and a page on that competitor's own site; [] for a quiet, baseline or unreadable report.

    Reads the report's `## What changed` lines and its evidence block. A line about a name the
    block does not hold, or without a page of that competitor's to cite, is left out: Tin turns
    only backed changes into plan items.
    """
    status = re.search(r"(?m)^Status:\s*(\S+)\s*$", text)
    blocks = WATCH_FENCE.findall(text)
    if not status or status.group(1) != "changes" or len(blocks) != 1:
        return []
    try:
        evidence = json.loads(blocks[0])
        competitors = [
            {
                "id": str(item["id"]).casefold().removeprefix("www."),
                "name": " ".join(str(item["name"]).split())[:120],
                "pages": [
                    page["url"]
                    for page in item.get("pages") or []
                    if isinstance(page, dict) and page.get("status") == "read"
                ],
            }
            for item in evidence["competitors"]
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
    section = re.search(r"(?ms)^## What changed\s*\n(.*?)(?=^## |\Z)", text)
    changes = []
    for line in (section.group(1) if section else "").splitlines():
        match = WATCH_LINE.match(line.strip())
        if not match:
            continue
        name = " ".join(match.group("name").split()).casefold()
        competitor = next((c for c in competitors if name in {c["name"].casefold(), c["id"]}), None)
        if competitor is None:
            continue
        cited = [
            url for url in WATCH_URL.findall(match.group("text")) if _belongs(url, competitor["id"])
        ]
        pages = [url for url in competitor["pages"] if url.startswith("https://")]
        url = (cited or pages or [None])[0]
        if url is None:
            continue
        changes.append(
            {
                "competitor": competitor["id"],
                "name": competitor["name"],
                "change": match.group("text").strip()[:400],
                "url": url,
            }
        )
    return changes


async def competitor_watch(*, database, storage, project):
    """The newest succeeded competitor.watch report's backed material changes, or None."""
    row = await database.pool.fetchrow(LATEST_WATCH_SQL, project.id)
    if row is None or not str(row["artifact_path"]).startswith(WATCH_FOLDER):
        return None
    raw = await _read_if_exists(storage, project, row["canonical_commit_sha"], row["artifact_path"])
    if not raw or len(raw) > WATCH_MAX_BYTES:
        return None
    changes = watch_changes(raw.decode("utf-8", errors="replace"))
    if not changes:
        return None
    return {
        "run_id": str(row["id"]),
        "revision": row["canonical_commit_sha"],
        "path": row["artifact_path"],
        "changes": changes,
    }


def competitor_rows(watch):
    """One source row per competitor with backed changes, so plan items can cite it."""
    rows = {}
    for change in (watch or {}).get("changes", []):
        row = rows.setdefault(
            change["competitor"],
            {
                "source_id": f"competitor:{change['competitor']}"[:500],
                "data": {
                    "kind": "competitor_change",
                    "title": f"competitor {change['name']}",
                    "competitor": change["competitor"],
                    "name": change["name"],
                    "report": watch["path"],
                    "run_id": watch["run_id"],
                    "changes": [],
                },
            },
        )
        row["data"]["changes"].append({"change": change["change"], "url": change["url"]})
    return list(rows.values())


# What shapes a typed (v7) plan beyond the audit and keyword research: the newest Page decisions
# (organic.content_efficacy, content/efficacy.md) and traffic snapshot (organic.traffic_snapshot,
# analytics/traffic-snapshot.json). Both arrive with #239; a missing, stale, oversized or
# other-schema file changes nothing and the plan says it was not used. Small local parsers: the
# shapes are #239's, read without importing its modules.
EFFICACY_PATH = "content/efficacy.md"
EFFICACY_SCHEMA = "content.efficacy/1"
SNAPSHOT_PATH = "analytics/traffic-snapshot.json"
SNAPSHOT_SCHEMA = "tin.traffic_snapshot/1"
# Both files are weekly; one older than this has been replaced or forgotten.
SIGNAL_MAX_AGE_DAYS = 14
# Both are written to stay under the 64 KB a code workflow reads; Tin uses none larger.
SIGNAL_FILE_BYTES = 64_000
EFFICACY_BLOCK = re.compile(r"## Decisions block\s*```json\s*(\{.*?\})\s*```", re.S)
# The audit check each Page decisions refresh rule stands for (as #239's reader maps them).
EFFICACY_CHECKS = {
    "low_ctr": "search.low_ctr",
    "near_page_one": "search.near_page_one",
    "decline": "search.decay",
    "merge_survivor": "search.decay",
}
EFFICACY_ROWS = 200
# Traffic: a page counts only with this many sessions in the snapshot's 28 days, conversion says
# something only once the counted pages hold this many first-touch signups, a converting page
# needs this many signups of its own, and a weak page this many sessions.
MIN_PAGE_SESSIONS = 30
MIN_SITE_SIGNUPS = 5
MIN_PAGE_SIGNUPS = 3
WEAK_MIN_SESSIONS = 100
# A weak page converts at under half the site's rate; a converting page at the site's rate or more.
WEAK_SHARE = 0.5
MAX_CONVERTING = 10
MAX_WEAK = 5
LABELS = {EFFICACY_PATH: "Page decisions", SNAPSHOT_PATH: "Traffic snapshot"}


def _signal_text(raw, path):
    """The file's text, or why it is not used."""
    if raw is None:
        return None, "none saved yet"
    if len(raw) > SIGNAL_FILE_BYTES:
        return None, "over 64 KB"
    return (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw), None


def _signal_day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _stale(made, today):
    if made is None:
        return "no date"
    if not 0 <= (today - made).days <= SIGNAL_MAX_AGE_DAYS:
        return f"older than {SIGNAL_MAX_AGE_DAYS} days ({made.isoformat()})"
    return None


def _decision_path(value):
    """A Page decisions row's site path, without query or trailing slash."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith(("http://", "https://")):
        value = urlsplit(value).path or "/"
    if not value.startswith("/") or ".." in value or len(value) > 500:
        return None
    return value.split("?")[0].rstrip("/") or "/"


def page_decisions(raw, today):
    """The newest Page decisions, as the plan uses them, or why they are not used.

    `refresh`: pages to refresh, with the audit check each stands for and the reason. `cut`:
    pages to merge or retire (a noindex is a retirement), with the decision; no new article,
    answer or refresh may cover them. `keep`: pages Page decisions keeps as they are, so no
    item targets them. `rewrite`: pages that need a new brief.
    """
    text, problem = _signal_text(raw, EFFICACY_PATH)
    if problem:
        return {"status": problem}
    found = EFFICACY_BLOCK.search(text)
    try:
        block = json.loads(found.group(1)) if found else None
    except ValueError:
        block = None
    if not isinstance(block, dict):
        return {"status": "no readable decisions block"}
    if block.get("schema") != EFFICACY_SCHEMA:
        return {"status": f"not {EFFICACY_SCHEMA}"}
    made = _signal_day(block.get("generated"))
    stale = _stale(made, today)
    if stale:
        return {"status": stale}
    result = {"status": "used", "generated": made.isoformat()}
    result.update(refresh={}, cut={}, keep=[], rewrite={})
    for row in (block.get("decisions") or [])[:EFFICACY_ROWS]:
        if not isinstance(row, dict):
            continue
        path, decision = _decision_path(row.get("url")), row.get("decision")
        reason = " ".join(str(row.get("reason") or "").split())[:300]
        if path is None:
            continue
        if decision == "refresh":
            check = EFFICACY_CHECKS.get(row.get("rule"))
            result["refresh"][path] = {
                "checks": [check] if check else [],
                "rule": str(row.get("rule") or "")[:60],
                "reason": reason,
            }
        elif decision in ("merge", "retire"):
            action = row.get("action")
            result["cut"][path] = {
                "decision": "noindex" if action == "noindex" else decision,
                "target": _decision_path(row.get("target")),
                "reason": reason,
            }
        elif decision == "keep":
            result["keep"].append(path)
        elif decision == "rewrite":
            result["rewrite"][path] = reason
    return result


def _short_rows(snapshot):
    columns = (snapshot.get("definitions") or {}).get("short_columns") or []
    for name in ("more_pages", "entry_only_pages", "dropped_pages"):
        for raw in snapshot.get(name) or []:
            if isinstance(raw, list) and raw:
                row = dict(zip(columns, raw, strict=False))
                yield {
                    "page": row.get("page"),
                    "sessions": row.get("sessions"),
                    "signups": row.get("signups"),
                    "activated": row.get("activated"),
                    "clicks": row.get("clicks"),
                    "impressions": row.get("impressions"),
                }


def _count(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number >= 0 else None


def traffic_snapshot(raw, today):
    """The newest traffic snapshot's per-page rows, or why it is not used.

    Each row: host, path, sessions, signups (first touch), activated, clicks, impressions, from
    the snapshot's detailed pages and its compact rows. A missing measure stays None.
    """
    text, problem = _signal_text(raw, SNAPSHOT_PATH)
    if problem:
        return {"status": problem}
    try:
        snapshot = json.loads(text)
    except ValueError:
        return {"status": "unreadable"}
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        return {"status": f"not {SNAPSHOT_SCHEMA}"}
    made = _signal_day(snapshot.get("generated_at"))
    stale = _stale(made, today)
    if stale:
        return {"status": stale}
    rows = []
    for page in snapshot.get("pages") or []:
        if not isinstance(page, dict):
            continue
        search = (page.get("search") or {}).get("current") or [None, None]
        signups = page.get("signups") or {}
        rows.append(
            {
                "page": page.get("page"),
                "sessions": ((page.get("visits") or {}).get("current") or {}).get("sessions"),
                "signups": (signups.get("first_touch") or [None])[0],
                "activated": (signups.get("activated") or [None])[0],
                "clicks": search[0] if len(search) > 0 else None,
                "impressions": search[1] if len(search) > 1 else None,
            }
        )
    rows.extend(_short_rows(snapshot))
    pages = {}
    for row in rows:
        key = row.get("page")
        if not isinstance(key, str) or "/" not in key:
            continue
        host, _, path = key.partition("/")
        path = ("/" + path).split("?")[0].rstrip("/") or "/"
        entry = {"host": host.lower().removeprefix("www."), "path": path}
        entry.update(
            {
                name: _count(row.get(name))
                for name in ("sessions", "signups", "activated", "clicks", "impressions")
            }
        )
        pages.setdefault((entry["host"], path), entry)
    return {"status": "used", "generated": made.isoformat(), "pages": list(pages.values())}


def traffic_signals(snapshot, host):
    """Which pages convert visitors into signups and which get traffic but convert weakly.

    Only pages with at least MIN_PAGE_SESSIONS sessions count. The site rate is their signups
    over their sessions, and needs MIN_SITE_SIGNUPS signups; below that conversion says nothing.
    A converting page has MIN_PAGE_SIGNUPS signups and at least the site rate; a weak page has
    WEAK_MIN_SESSIONS sessions and under WEAK_SHARE of the site rate.
    """
    own = (host or "").lower().removeprefix("www.")
    counted = [
        page
        for page in snapshot.get("pages") or []
        if page["host"] == own and (page["sessions"] or 0) >= MIN_PAGE_SESSIONS
    ]
    measured = [page for page in counted if page["signups"] is not None]
    sessions = sum(page["sessions"] for page in measured)
    signups = sum(page["signups"] for page in measured)
    result = {"pages_counted": len(counted), "signups_counted": signups}
    if not measured or signups < MIN_SITE_SIGNUPS:
        return {
            **result,
            "site_rate": None,
            "converting": [],
            "weak": [],
            "note": f"fewer than {MIN_SITE_SIGNUPS} signups on pages with "
            f"{MIN_PAGE_SESSIONS}+ sessions, so conversion was not used",
        }
    rate = signups / sessions

    def row(page):
        return {
            "path": page["path"],
            "sessions": page["sessions"],
            "signups": page["signups"],
            "activated": page["activated"],
            "rate": round(page["signups"] / page["sessions"], 4),
        }

    converting = sorted(
        (
            row(page)
            for page in measured
            if page["signups"] >= MIN_PAGE_SIGNUPS and page["signups"] / page["sessions"] >= rate
        ),
        key=lambda page: (-page["rate"], -(page["activated"] or 0), page["path"]),
    )[:MAX_CONVERTING]
    weak = sorted(
        (
            row(page)
            for page in measured
            if page["sessions"] >= WEAK_MIN_SESSIONS
            and page["signups"] / page["sessions"] < WEAK_SHARE * rate
        ),
        key=lambda page: (-page["sessions"], page["path"]),
    )[:MAX_WEAK]
    return {**result, "site_rate": round(rate, 4), "converting": converting, "weak": weak}


async def site_signals(*, storage, project, revision, today):
    """The newest Page decisions and traffic snapshot at `revision`, read once, at most 64 KB
    each. Neither file exists before #239 deploys; then both say why they are not used."""
    raw = {}
    for path in (EFFICACY_PATH, SNAPSHOT_PATH):
        raw[path] = await _read_if_exists(storage, project, revision, path)
    return {
        "page_decisions": page_decisions(raw[EFFICACY_PATH], today),
        "traffic": traffic_snapshot(raw[SNAPSHOT_PATH], today),
    }


def signal_notes(signals):
    """One line naming the files that did not shape the plan, and why; None when both did."""
    unused = [
        f"{LABELS[path].lower()} ({path}): {signals[key]['status']}"
        for key, path in (("page_decisions", EFFICACY_PATH), ("traffic", SNAPSHOT_PATH))
        if (signals.get(key) or {}).get("status") != "used"
    ]
    return "Not used: " + "; ".join(unused) + "." if unused else None


async def page_decision_refreshes(*, storage, project, revision, today):
    """Pages a current Page decisions file (organic.content_efficacy) marks for a refresh, as
    site paths with the audit checks each stands for (the `planned` refresh rows)."""
    raw = await _read_if_exists(storage, project, revision, EFFICACY_PATH)
    decisions = page_decisions(raw, today)
    return {path: set(row["checks"]) for path, row in (decisions.get("refresh") or {}).items()}


async def context_files(*, storage, project, revision, paths):
    if len(paths) > 8 or len(set(paths)) != len(paths):
        raise ValueError("Choose at most eight distinct context files.")
    result = []
    total = 0
    for path in paths:
        if not safe_project_file_path(path):
            raise ValueError("Context file path is unsafe.")
        content = await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=revision, path=path
        )
        total += len(content)
        if len(content) > 20_000 or total > 60_000:
            raise ValueError("Context files must be at most 20 KB each and 60 KB together.")
        result.append(
            {
                "source_id": f"file:{path}",
                "path": path,
                "revision": revision,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": content.decode("utf-8"),
            }
        )
    return result


# Where a project's positioning lives: brand guide, project memory, the Start here plan and up
# to five founder notes. Read at plan time, bounded, and pinned in the plan's saved context.
POSITIONING_PATHS = ("brand/BRAND.md", "wiki/INDEX.md", "reports/GROWTH_ONBOARDING_PLAN.md")
POSITIONING_CONTEXT_PREFIX = "context/"
POSITIONING_NOTES = 5
POSITIONING_FILE_BYTES = 8_000
POSITIONING_TOTAL_BYTES = 30_000


async def _read_if_exists(storage, project, revision, path):
    read = getattr(storage, "read_canonical_artifact_if_exists", None)
    if read is not None:
        return await read(repo_id=project.state_repo_id, commit_sha=revision, path=path)
    try:
        return await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=revision, path=path
        )
    except LookupError:
        return None


async def positioning_files(*, storage, project, revision, include_memory=True):
    """The project's own positioning, bounded. Missing files are simply absent; the plan then
    says so in its gaps instead of inventing a positioning."""
    paths = [path for path in POSITIONING_PATHS if include_memory or path != "wiki/INDEX.md"]
    list_files = getattr(storage, "list_canonical_files_at", None)
    if list_files is not None:
        listed = await list_files(repo_id=project.state_repo_id, revision=revision)
        paths += sorted(
            path
            for path in listed
            if path.startswith(POSITIONING_CONTEXT_PREFIX)
            and path.endswith(".md")
            and safe_project_file_path(path)
        )[:POSITIONING_NOTES]
    result, total = [], 0
    for path in paths:
        raw = await _read_if_exists(storage, project, revision, path)
        if not raw:
            continue
        excerpt = raw[:POSITIONING_FILE_BYTES].decode("utf-8", errors="ignore")
        size = len(excerpt.encode())
        if total + size > POSITIONING_TOTAL_BYTES:
            break
        total += size
        result.append(
            {
                "path": path,
                "revision": revision,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "truncated": len(raw) > POSITIONING_FILE_BYTES,
                "content": excerpt,
            }
        )
    return result
