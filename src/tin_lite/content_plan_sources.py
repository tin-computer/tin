"""Resolve exact, receipt-verified research publications and bounded project context."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite.keyword_plan import LIMITS as KEYWORD_LIMITS
from tin_lite.keyword_plan import paths as keyword_paths
from tin_lite.organic_audit import ARTIFACT_LIMITS, audit_hosts, audit_paths, canonical_json, digest
from tin_lite.project_files import safe_project_file_path


async def research_sources(
    *, database, storage, project, inputs, typed=False, planned=None, published=()
):
    """The pinned audit and keyword research as source rows.

    `typed` (content-editorial-v7) adds one row per page a refresh could fix, from the audit's
    search findings and `planned`, Page decisions' refresh rows (see page_decision_refreshes),
    and the whole site's page list (`site_pages`), with `published`, the pages Tin put on the
    site itself (see published_pages).
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
        rows.extend(refresh_rows(findings, audit, planned))
        extra["site_pages"] = site_pages(audit, keywords=keywords, published=published)
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


def refresh_rows(findings, evidence, planned=None):
    """Source rows for the pages a refresh could fix, most impressions at stake first."""
    from tin_lite.content_plan_editorial import MAX_REFRESH_SOURCES, REFRESH_SOURCE_PREFIX
    from tin_lite.content_refresh import plan_candidates

    return [
        {
            "source_id": REFRESH_SOURCE_PREFIX + digest(page["path"])[:20],
            "data": {"kind": "refresh_candidate", "title": f"refresh {page['path']}", **page},
        }
        for page in plan_candidates(findings, evidence, planned, limit=MAX_REFRESH_SOURCES)
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


async def page_decision_refreshes(*, storage, project, revision, today):
    """Pages a current Page decisions file (organic.content_efficacy) marks for a refresh, as
    site paths with the audit checks each stands for.

    organic.content_efficacy and its reader (planned_url_changes) arrive with PR #239; until
    then no project has such a file, so there are none. #239 returns
    planned_url_changes.refresh_candidates here, the same rows content.refresh reads.
    """
    return {}


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
