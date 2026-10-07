"""organic-audit-v12 SUMMARY.json: the part of an audit a code workflow can read.

Code workflows read project files of at most 64,000 bytes (`code_project_files`), and a real
crawl's findings.json and evidence.json are larger: tin.computer's evidence.json was 188 KB.
v12 therefore also writes this summary: the audited host and run, one compact row per crawled
page, finding counts by check and the AI-visibility headline. It is computed from the same
saved evidence after the findings, and cut to its pinned byte budget: the columns of least use
go first, then the pages of least use, and `truncated` says what went.

Inbound links and click depth come from the links on the pages Tin read. They are measured
inside the crawl, not across the whole site, and the summary says so.

From organic-audit-v13 it also holds `ai_engines`: one row per AI engine the questions were
asked on, with answers from the apps and from API models labelled apart.
"""

from __future__ import annotations

from collections import deque

from tin_lite.organic_audit import CHECKS, canonical_json, check_outcome
from tin_lite.organic_audit_checks import SiteView
from tin_lite.organic_audit_format import PRIORITIES
from tin_lite.organic_audit_site import is_noindex, url_key

SCHEMA_VERSION = 1
# Every column a page row can hold, in row order. A row is a list in this order; read it with
# dict(zip(summary["pages"]["columns"], row)).
PAGE_COLUMNS = (
    "path",
    "status",
    "read",
    "indexable",
    "noindex",
    "canonical",
    "title",
    "description",
    "words",
    "inbound",
    "depth",
    "checks",
)
# Over budget, columns go in this order. Path, status, indexability, inbound links and click
# depth always stay; the next step drops whole pages instead.
DROP_ORDER = ("description", "title", "read", "canonical", "words", "checks")
MAX_CHECKS = 100
MAX_CANONICAL_CHARS = 200
TOP_FINDINGS = 5
CITED_DOMAINS = 5
CRAWL_FLAGS = {check_id: flag for flag, check_id, *_ in CHECKS}


def link_graph(view: SiteView, home: str) -> dict | None:
    """Inbound links and click depth over the links on the pages Tin read.

    A link costs one click; a redirect Tin observed costs none. Pages Tin did not read add no
    links, so a depth is the fewest clicks through pages Tin read: exact only when every
    sitemap page was read and no page's link list was capped. A link with a query string
    reaches the crawled page at its path, unless that exact address was crawled itself.
    None before audit policy v12.
    """
    known = set(view.facts) | set(view.crawl)

    def resolve(target: str) -> str:
        path = target.split("?", 1)[0]
        return path if target not in known and path in known else target

    links: dict[str, list[str]] = {}
    redirects: dict[str, str] = {}
    capped = 0
    for key, facts in view.facts.items():
        if facts.get("fetch") == "observed" and "internal_links" in facts:
            targets = dict.fromkeys(resolve(target) for target in facts["internal_links"])
            links[key] = [target for target in targets if target != key]
            capped += bool(facts.get("internal_links_capped"))
        elif facts.get("fetch") == "redirect" and view.in_scope(facts.get("location") or ""):
            redirects[key] = url_key(facts["location"])
    if not links:
        return None
    inbound: dict[str, int] = {}
    for targets in links.values():
        for target in set(targets):
            inbound[target] = inbound.get(target, 0) + 1
    depth = {home: 0}
    queue = deque([home])
    while queue:
        key = queue.popleft()
        steps = [(redirects[key], 0)] if key in redirects else []
        steps += [(target, 1) for target in links.get(key, [])]
        for target, cost in steps:
            if target not in depth or depth[key] + cost < depth[target]:
                depth[target] = depth[key] + cost
                # Redirects cost nothing, so they go to the front: a 0-1 breadth-first search.
                (queue.appendleft if cost == 0 else queue.append)(target)
    return {
        "inbound": inbound,
        "depth": depth,
        "pages_with_links": len(links),
        "links": sum(len(targets) for targets in links.values()),
        "capped_pages": capped,
    }


def _page_checks(findings: list[dict], view: SiteView, crawl_pages: list[dict]) -> tuple:
    """Finding counts by check, and which crawled pages each check names.

    Provider-crawl findings list only ten example URLs, so their pages are recomputed from the
    crawl flags; other findings name the pages in their own `urls`.
    """
    by_check: dict[str, dict] = {}
    pages: dict[str, set[str]] = {}
    for finding in findings:
        check = str(finding.get("check_id") or "unknown")[:80]
        entry = by_check.setdefault(
            check, {"check": check, "findings": 0, "pages": 0, "priority": None}
        )
        entry["findings"] += 1
        entry["pages"] += int(finding.get("affected_count") or 0)
        priority = finding.get("priority")
        if priority in PRIORITIES and (
            entry["priority"] is None
            or list(PRIORITIES).index(priority) < list(PRIORITIES).index(entry["priority"])
        ):
            entry["priority"] = priority
        flag = (finding.get("affected_url_evidence") or {}).get("flag")
        if flag and CRAWL_FLAGS.get(check) == flag:
            urls = [page["url"] for page in crawl_pages if check_outcome(page, flag) == "problem"]
        else:
            urls = [url for url in finding.get("urls") or [] if isinstance(url, str)]
        named = pages.setdefault(check, set())
        named.update(url_key(url) for url in urls if view.in_scope(url))
    ordered = sorted(
        by_check.values(),
        key=lambda entry: (
            list(PRIORITIES).index(entry["priority"]) if entry["priority"] else len(PRIORITIES),
            -entry["pages"],
            entry["check"],
        ),
    )
    return ordered, pages


def _canonical(view: SiteView, key: str, facts: dict) -> str:
    canonical = facts.get("canonical")
    if not canonical:
        return "missing"
    elsewhere = view.canonical_elsewhere(key)
    if not elsewhere:
        return "self"
    target = url_key(elsewhere) if view.in_scope(elsewhere) else elsewhere
    return target[:MAX_CANONICAL_CHARS]


def _crawl_flag(crawl: dict, flag: str) -> bool | None:
    """True when the provider crawl saw the item present, False when missing, else None."""
    outcome = check_outcome(crawl, flag) if crawl else "unknown"
    return True if outcome == "pass" else False if outcome == "problem" else None


def _row(key: str, view: SiteView, graph: dict | None, checks: list[int]) -> dict:
    facts = view.facts.get(key) or {}
    crawl = view.crawl.get(key) or {}
    observed = facts.get("fetch") == "observed"
    status = facts.get("status_code", crawl.get("status_code"))
    status = status if type(status) is int else None
    if observed and view.canonical_elsewhere(key):
        indexable = False
    elif not facts and status is not None and not 200 <= status < 300:
        indexable = False
    else:
        indexable = view.open_to_index(key)
    if observed:
        title = bool(facts.get("title"))
        description = bool(facts["description_length"]) if "description_length" in facts else None
    else:
        title = True if crawl.get("title") else _crawl_flag(crawl, "no_title")
        description = _crawl_flag(crawl, "no_description")
    return {
        "path": key,
        "status": status,
        # How Tin's own reader saw the page; None when only the provider crawl reached it.
        "read": facts.get("fetch"),
        "indexable": indexable,
        "noindex": is_noindex(facts) if observed else None,
        "canonical": _canonical(view, key, facts) if observed else None,
        "title": title,
        "description": description,
        "words": facts.get("text_words") if observed else None,
        "inbound": graph["inbound"].get(key, 0) if graph else None,
        "depth": graph["depth"].get(key) if graph else None,
        "checks": checks,
    }


def build_summary(
    *,
    run_id: str,
    scope: dict,
    hosts: tuple[str, ...],
    policy: dict,
    view: SiteView,
    crawl_pages: list[dict],
    cover: dict,
    findings: list[dict],
    top_issue_ids: list[str],
    ai: dict,
    paths: dict[str, str],
    findings_sha256: str,
    evidence_sha256: str,
) -> dict:
    """The summary before it is fitted to its byte budget."""
    home = url_key(scope["url"])
    graph = link_graph(view, home)
    by_check, named = _page_checks(findings, view, crawl_pages)
    shown = by_check[:MAX_CHECKS]
    index = {entry["check"]: position for position, entry in enumerate(shown)}
    keys = set(view.crawl) | set(view.facts)
    tagged: dict[str, list[int]] = {key: [] for key in keys}
    for check, pages in named.items():
        if check in index:
            for key in pages & keys:
                tagged[key].append(index[check])
    for entry in shown:
        entry["listed"] = sum(index[entry["check"]] in tagged[key] for key in keys)
    rows = [_row(key, view, graph, sorted(tagged[key])) for key in keys]

    def priority(row):
        impressions = (view.search.get(row["path"]) or {}).get("impressions", 0)
        return (
            row["path"] != home,
            -impressions,
            row["depth"] if row["depth"] is not None else float("inf"),
            row["path"],
        )

    rows.sort(key=priority)
    complete = cover.get("status") == "complete" and not (graph or {}).get("capped_pages")
    by_priority = {name: 0 for name in PRIORITIES}
    for finding in findings:
        if finding.get("priority") in by_priority:
            by_priority[finding["priority"]] += 1
    by_id = {finding.get("id"): finding for finding in findings}
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "organic_audit_summary",
        "run_id": run_id,
        "host": scope["host"],
        "hosts": list(hosts),
        "site_url": scope["url"],
        "market": scope.get("market"),
        "audited_at": scope.get("started_at"),
        "policy_version": policy["version"],
        "files": {
            "report": paths["AUDIT.md"],
            "findings": paths["findings.json"],
            "evidence": paths["evidence.json"],
            "summary": paths["SUMMARY.json"],
        },
        "findings_sha256": findings_sha256,
        "evidence_sha256": evidence_sha256,
        "coverage": {
            key: cover.get(key)
            for key in (
                "status",
                "sitemap_read",
                "sitemap_pages",
                "inspected_sitemap_pages",
                "inspected_pages",
                "page_cap",
                "skipped_sitemap_pages",
            )
        }
        | {"crawled_pages": len(view.crawl), "read_pages": len(view.observed())},
        "findings": {
            "total": len(findings),
            "by_priority": by_priority,
            "top": [
                {
                    "id": issue_id,
                    "check": by_id[issue_id].get("check_id"),
                    "priority": by_id[issue_id].get("priority"),
                }
                for issue_id in top_issue_ids[:TOP_FINDINGS]
                if issue_id in by_id
            ],
            # `pages` sums the findings' affected counts; `listed` is how many rows below name
            # the check. Fewer listed than pages means the finding named examples only, or
            # pages outside the crawl.
            "by_check": shown,
            "checks_omitted": len(by_check) - len(shown),
        },
        "ai_visibility": ai_headline(ai),
        "links": {
            "status": "observed" if graph else "not_collected",
            "start": home,
            "pages_with_links": graph["pages_with_links"] if graph else 0,
            "links": graph["links"] if graph else 0,
            "capped_pages": graph["capped_pages"] if graph else 0,
            "depth": ("exact" if complete else "at_most") if graph else None,
            "unreached": sum(row["depth"] is None for row in rows) if graph else None,
            "note": (
                "From the links in the static HTML of the pages Tin read. inbound is how many "
                "of those pages link to the page; depth is the fewest clicks from the homepage "
                "through them (a redirect costs none), and null when none of them leads there. "
                "Tin does not see links that JavaScript adds."
            )
            if graph
            else "Tin kept no page links for this run, so inbound and depth are null.",
        },
        "pages": {
            "columns": list(PAGE_COLUMNS),
            "rows": [[row[column] for column in PAGE_COLUMNS] for row in rows],
            "total": len(rows),
            "note": "Read a row with dict(zip(columns, row)). checks holds positions in "
            "findings.by_check.",
        },
        "truncated": False,
    }
    if policy.get("ai_engines"):
        # organic-audit-v13: one row per AI engine, apps and API models labelled apart.
        from tin_lite.organic_audit_engines import headline

        summary["ai_engines"] = headline(ai.get("engines"))
    return summary


def ai_headline(ai: dict) -> dict:
    """The AI-visibility numbers a reader needs, without answers, quotes or sources."""
    panel = ai.get("panel") or {}
    ladder = ai.get("ladder")
    memory = ai.get("memory")
    question_set = ai.get("question_set")
    return {
        "status": ai.get("status"),
        "planned": ai.get("planned", 0),
        "completed": ai.get("completed", 0),
        "questions": len(panel.get("questions") or []),
        # Full-panel counts, or null while any planned answer is unknown; `observed` then
        # holds the counts among scored answers. Unknown answers are not negatives.
        "metrics": ai.get("metrics"),
        "observed": ai.get("observed_metrics"),
        "ladder": {
            "scored": ladder["scored"],
            "counts": ladder["counts"],
            "main_break": ladder["bottleneck"]["stage"],
        }
        if ladder
        else None,
        "without_search": {
            key: memory.get(key) for key in ("planned", "completed", "mentioned", "shortlisted")
        }
        if memory
        else None,
        "question_set": {
            "sha256": question_set.get("sha256"),
            "reused_from": question_set.get("source_run_id"),
        }
        if question_set
        else None,
        "cited_instead": [
            [row["domain"][:100], row["answers"]]
            for row in (ai.get("cited_domains") or [])[:CITED_DOMAINS]
        ],
    }


def fit_summary(summary: dict, limit: int) -> bytes:
    """Encode the summary within `limit` bytes, dropping columns, then the least useful pages.

    Rows are already ordered most useful first: the homepage, then pages by search
    impressions, then by click depth.
    """
    encoded = canonical_json(summary)
    if len(encoded) <= limit:
        return encoded
    pages = summary["pages"]
    rows = pages["rows"]
    dropped: list[str] = []
    for column in DROP_ORDER:
        position = pages["columns"].index(column)
        pages["columns"].pop(position)
        for row in rows:
            row.pop(position)
        dropped.append(column)
        summary["truncated"] = {"columns": dropped, "pages": 0}
        encoded = canonical_json(summary)
        if len(encoded) <= limit:
            return encoded
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high + 1) // 2
        pages["rows"] = rows[:middle]
        summary["truncated"]["pages"] = len(rows) - middle
        if len(canonical_json(summary)) <= limit:
            low = middle
        else:
            high = middle - 1
    pages["rows"] = rows[:low]
    summary["truncated"]["pages"] = len(rows) - low
    encoded = canonical_json(summary)
    if len(encoded) > limit:
        raise ValueError("Audit summary exceeded its byte budget without any page rows.")
    return encoded


def summary_document(**values) -> bytes:
    """SUMMARY.json for one v12 run, at most the pinned `summary_max_bytes`."""
    summary = build_summary(**values)
    return fit_summary(summary, values["policy"]["summary_max_bytes"])
