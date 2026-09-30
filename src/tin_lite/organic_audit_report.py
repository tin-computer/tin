"""organic-audit-v10 documents: coverage-honest report, one finding format, bounded evidence.

Earlier policies keep their own renderer in `organic_audit.build_documents`; this module is
used only for pins with `site_checks`.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from tin_lite.organic_audit_checks import (
    SiteView,
    coverage,
    coverage_label,
    site_check_coverage,
    site_findings,
    speed_rows,
)
from tin_lite.organic_audit_format import AREAS, PRIORITIES, urgency_key
from tin_lite.organic_audit_search import brand_terms, search_findings
from tin_lite.organic_audit_site import crawler_stances, url_key

REPORT_TABLE_ROWS = 20
SKIPPED_LINES = 100


def md(value) -> str:
    """Site-derived text is data: escape Markdown and HTML control characters."""
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", " ".join(str(value).split()))


def _path(url: str, host: str) -> str:
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return path if parts.hostname == host else f"{parts.hostname}{path}"


def _ctr(row: dict) -> str:
    rate = 100 * row["clicks"] / row["impressions"] if row["impressions"] else 0
    return f"{rate:.1f}%"


def _grade_cell(grades: dict, name: str, text: str) -> str:
    return f"{text} ({grades[name].replace('_', ' ')})" if grades[name] else "unknown"


def _num(value) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def query_rows(search_queries: dict | None) -> list[dict]:
    if not search_queries or search_queries.get("status") != "completed":
        return []
    return [
        {
            "query": row[0],
            "url": row[1],
            "clicks": row[2],
            "impressions": row[3],
            "position": row[4],
        }
        for row in search_queries["value"]["queries"]
    ]


def page_rows(search_console: dict | None) -> list[dict]:
    if not search_console or search_console.get("status") != "completed":
        return []
    return [row for row in search_console["value"]["pages"] if "position" in row]


def analyze(
    *,
    scope: dict,
    hosts: tuple[str, ...],
    crawl: dict,
    ai: dict,
    policy: dict,
    search_console: dict | None,
    search_queries: dict | None,
    site: dict,
    search_previous: dict | None = None,
) -> dict:
    """All v10 site and search findings plus the coverage summary, from saved evidence."""
    host = scope["host"]
    pages, queries = page_rows(search_console), query_rows(search_queries)
    view = SiteView(
        host=host,
        hosts=hosts,
        site=site,
        crawl_pages=crawl.get("pages", []),
        search_pages=pages,
        search_queries=queries,
        angles=bool(policy.get("site_angles")),
    )
    cover = coverage(view, site.get("plan"), page_cap=scope.get("page_cap"))
    titles = {}
    for row in [*pages, *queries]:
        key = url_key(row["url"])
        if key not in titles:
            titles[key] = view.title(key)
    findings = []
    if pages or queries:
        value = (search_console or {}).get("value", {})
        findings.extend(
            search_findings(
                host=host,
                pages=pages,
                queries=queries,
                window=f"{value.get('start_date')} to {value.get('end_date')}",
                titles={k: v for k, v in titles.items() if v},
                policy=policy,
                brand=brand_terms(host, ai.get("panel")),
                previous=(search_previous or {}).get("value")
                if (search_previous or {}).get("status") == "completed"
                else None,
            )
        )
    pagespeed = site.get("pagespeed") or {"status": "not_collected"}
    findings.extend(site_findings(view, home=scope["url"], pagespeed=pagespeed))
    return {
        "view": view,
        "coverage": cover,
        "findings": findings,
        "site_check_coverage": site_check_coverage(
            view, pagespeed=pagespeed, search=search_queries or {}
        )
        if view.site_collected
        else [
            {
                "check": "site_evidence",
                "status": "unknown",
                "note": "Tin could not read the site's robots.txt, sitemaps or pages."
                if view.site_status == "unavailable"
                else "Site files and page HTML were not collected for this run.",
            }
        ],
        "pagespeed": pagespeed,
    }


def question_set_lines(ai: dict) -> list[str]:
    """Which frozen questions were asked, and how answers moved since the previous audit."""
    question_set = ai.get("question_set")
    if not question_set:
        return []
    count, answers = question_set["questions"], question_set["repetitions"]
    comparison = ai.get("comparison")
    if question_set.get("method") == "reused_frozen_panel" and comparison:
        baseline = comparison["baseline"]
        started = (baseline.get("source_started_at") or "")[:10]
        lines = [
            f"Question set: the same {count} questions as the audit started {started} "
            f"(`{baseline['source_run_id']}`), {answers} answers each, so results compare. "
            "Start an audit with refresh_questions to draft a new set.",
            "",
            "### Change since the previous audit",
            "",
            "Each cell is before → now, as positive answers out of scored answers. Unknown "
            "answers are neither negatives nor positives.",
            "",
            "| Question | Mentioned | Website cited | Shortlisted | Preferred first |",
            "| --- | --- | --- | --- | --- |",
        ]
        keys = ("mentioned", "owned_domain_cited", "shortlisted", "selected_first")
        totals = {
            "before": {
                key: sum(row[key] for row in baseline["questions"]) for key in ("scored", *keys)
            },
            "now": {
                key: sum(row[key] for row in comparison["current"]) for key in ("scored", *keys)
            },
        }
        pairs = list(zip(baseline["questions"], comparison["current"], strict=True))
        for index, (before, now) in enumerate(pairs, 1):
            lines.append(
                f"| Q{index} | "
                + " | ".join(
                    f"{before[key]}/{before['scored']} → {now[key]}/{now['scored']}" for key in keys
                )
                + " |"
            )
        lines.append(
            "| All questions | "
            + " | ".join(
                f"{totals['before'][key]}/{totals['before']['scored']} → "
                f"{totals['now'][key]}/{totals['now']['scored']}"
                for key in keys
            )
            + " |"
        )
        return [*lines, ""]
    return [
        f"Question set: drafted in this run ({count} questions, {answers} answers each). "
        "Later audits of this site and market reuse it so results compare; start one with "
        "refresh_questions to draft a new set.",
        "",
    ]


def _finding_block(item: dict, evidence_limit: int) -> list[str]:
    shown = item["evidence"][:evidence_limit]
    lines = [
        f"#### {md(item['issue'])}",
        "",
        f"- Impact: {item['impact']}",
        f"- Priority: {PRIORITIES[item['priority']]}",
        "- Evidence:",
        *(f"  - {md(line)}" for line in shown),
        *(
            [f"  - {len(item['evidence']) - len(shown)} more lines in `findings.json`."]
            if len(item["evidence"]) > len(shown)
            else []
        ),
        f"- Fix: {md(item['fix'])}",
        f"- Finding ID: `{item['id']}` ({item['check_id']})",
        "",
    ]
    return lines


def report_lines(
    *,
    scope: dict,
    crawl: dict,
    ai: dict,
    findings: list[dict],
    technical_coverage: list[dict],
    analysis: dict,
    search_console: dict | None,
    search_queries: dict | None,
    site: dict,
    complete: bool,
    ai_details: list[str],
    evidence_limit: int = 8,
) -> list[str]:
    host = scope["host"]
    cover = analysis["coverage"]
    counts = {name: sum(f["priority"] == name for f in findings) for name in PRIORITIES}
    label = coverage_label(cover)
    if cover["status"] == "partial":
        result = f"Result: {label}."
        if cover["skipped_sitemap_pages"] or cover["skipped_search_pages"]:
            result += " The pages not inspected are listed under Pages inspected."
    elif complete:
        result = f"Result: completed within the stated scope; {label}."
    else:
        result = f"Result: partial evidence; {label}. Unknown checks are listed at the end."
    lines = [
        "# Organic visibility audit",
        "",
        f"Website: {scope['url']}",
        f"Market: {scope['market']} · English · Observed: {scope['started_at']}",
        "",
        "## Summary",
        "",
        "This is a read-only audit. Nothing on your website changed.",
        result,
        "Findings: "
        + ", ".join(
            f"{counts[name]} {PRIORITIES[name].lower()}{'s' if name == 'quick_win' else ''}"
            for name in PRIORITIES
        )
        + ".",
        "",
    ]
    completion = scope.get("completion")
    if completion:
        lines.extend(
            [
                f"Completion: retained the original crawl and "
                f"{completion['retained_observations']} scored answers; explicitly retried "
                f"the one missing answer. The original partial audit "
                f"`{completion['source_run_id']}` remains unchanged.",
                "",
            ]
        )
    urgent = sorted(findings, key=urgency_key)
    if urgent:
        lines.extend(["### Top issues", ""])
        for index, item in enumerate(urgent[:5], 1):
            lines.append(
                f"{index}. {md(item['issue'])} (impact {item['impact']}; "
                f"{PRIORITIES[item['priority']].lower()})."
                + (f" {md(item['evidence'][0])}" if item["evidence"] else "")
            )
        lines.append("")
        wins = [item for item in urgent if item["priority"] == "quick_win"]
        if wins:
            lines.extend(["### Quick wins", ""])
            lines.extend(f"- {md(item['issue'])}. {md(item['fix'])}" for item in wins[:5])
            lines.append("")
    else:
        lines.extend(
            [
                "No supported findings were observed in the inspected pages. This is not a "
                "clean bill of health for the whole website.",
                "",
            ]
        )
    lines.extend(
        [
            "## Findings",
            "",
            "Ordered by area: crawlability and indexation, technical foundations, on-page, "
            "content, authority. Each finding states the issue, its impact, the evidence, "
            "the fix and its priority.",
            "",
        ]
    )
    for number, (area, title) in enumerate(AREAS.items(), 1):
        lines.extend([f"### {number}. {title}", ""])
        items = [item for item in findings if item["area"] == area]
        if area == "authority":
            if not analysis["view"].angles:
                # v10 as released: authority was not measured at all.
                lines.extend(
                    [
                        "Not measured in this version: backlinks and brand mentions on other "
                        "sites are out of scope.",
                        "",
                    ]
                )
                continue
            lines.extend(
                [
                    "Backlinks and brand mentions on other sites are not measured in this "
                    "version. The sites AI answers cite and your about and contact pages are.",
                    "",
                ]
            )
        if not items:
            lines.extend(["No findings from the checks that ran.", ""])
        for item in items:
            lines.extend(_finding_block(item, evidence_limit))
    lines.extend(["## Action plan", ""])
    for name, title in PRIORITIES.items():
        items = [item for item in urgent if item["priority"] == name]
        lines.extend([f"### {title}", ""])
        lines.extend(
            (f"- {md(item['issue'])} (`{item['id']}`)" for item in items) if items else ["- None."]
        )
        lines.append("")

    lines.extend(["## Pages inspected", ""])
    selection = cover.get("selection")
    if selection:
        lines.append(
            f"Page cap {cover['page_cap']}. Tin chose pages in this order: the homepage, "
            f"{selection['search_impressions']} pages with the most Search Console impressions, "
            f"{selection['section']} more so every URL section has at least one page, then "
            f"{selection['section_fill']} taken in turn from each section. The provider crawl "
            "was asked to fetch the first of these before following the sitemap."
        )
    else:
        lines.append("No page selection was recorded for this run.")
    lines.append("")
    if cover["sitemap_read"]:
        lines.append(
            f"Sitemap: {cover['sitemap_pages']} URLs; {cover['inspected_sitemap_pages']} "
            f"inspected; {cover['skipped_sitemap_pages']} not inspected. "
            f"{cover['inspected_pages']} pages inspected in all."
        )
    else:
        lines.append(f"No sitemap was readable; {cover['inspected_pages']} pages were inspected.")
    lines.append("")
    if cover["sections"]:
        lines.extend(
            [
                "| Section | Pages | Selected | Impressions |",
                "| --- | ---: | ---: | ---: |",
                *(
                    f"| {md(row['section'])} | {row['pages']} | {row['selected']} | "
                    f"{_num(row['impressions'])} |"
                    for row in cover["sections"][:30]
                ),
                "",
            ]
        )
    if cover["skipped"]:
        lines.extend(["### Sitemap pages not inspected", ""])
        lines.extend(
            f"- {md(_path(row['url'], host))}"
            + (f" ({_num(row['impressions'])} impressions)" if row["impressions"] else "")
            for row in cover["skipped"][:SKIPPED_LINES]
        )
        if cover["skipped_sitemap_pages"] > SKIPPED_LINES:
            lines.append(
                f"- …and {cover['skipped_sitemap_pages'] - SKIPPED_LINES} more in `evidence.json`."
            )
        lines.append("")
    if cover["skipped_search_pages"]:
        lines.extend(["### Pages with search impressions not inspected", ""])
        lines.extend(
            f"- {md(_path(row['url'], host))} ({_num(row['impressions'])} impressions)"
            for row in cover["skipped_search_pages"][:SKIPPED_LINES]
        )
        lines.append("")

    lines.extend(["## Search Console", ""])
    pages, queries = page_rows(search_console), query_rows(search_queries)
    if pages or queries:
        value = search_console["value"] if pages else search_queries["value"]
        lines.extend(
            [
                f"{value['start_date']} through {value['end_date']}, property "
                f"`{md(value['property'])}`. Rows describe appearances in Google Search; "
                "pages without rows are not proven unindexed.",
                "",
            ]
        )
    if pages:
        lines.extend(
            [
                "| Page | Clicks | Impressions | CTR | Position |",
                "| --- | ---: | ---: | ---: | ---: |",
                *(
                    f"| {md(_path(row['url'], host))} | {_num(row['clicks'])} | "
                    f"{_num(row['impressions'])} | {_ctr(row)} | {row['position']:g} |"
                    for row in sorted(pages, key=lambda r: (-r["impressions"], r["url"]))[
                        :REPORT_TABLE_ROWS
                    ]
                ),
                "",
            ]
        )
    if queries:
        lines.extend(
            [
                "| Query | Page | Clicks | Impressions | Position |",
                "| --- | --- | ---: | ---: | ---: |",
                *(
                    f"| {md(row['query'])} | {md(_path(row['url'], host))} | "
                    f"{_num(row['clicks'])} | {_num(row['impressions'])} | {row['position']:g} |"
                    for row in sorted(queries, key=lambda r: (-r["impressions"], r["query"]))[
                        :REPORT_TABLE_ROWS
                    ]
                ),
                "",
            ]
        )
    if not pages and not queries:
        lines.extend(
            [
                "No matching Search Console evidence was collected, so the search checks "
                "(competing pages, near page one, low click-through, brand searches) did not run.",
                "",
            ]
        )

    lines.extend(["## robots.txt and AI crawlers", ""])
    robots = analysis["view"].robots
    if robots.get("status") == "observed":
        lines.extend(
            [
                "robots.txt was read. Sitemap references: "
                + (", ".join(md(url) for url in robots.get("sitemaps", [])[:5]) or "none")
                + ".",
                "",
                "| Crawler | Purpose | Stance |",
                "| --- | --- | --- |",
                *(
                    f"| {row['agent']} | {row['kind']} | {row['stance'].replace('_', ' ')}"
                    + (
                        f": {', '.join(md(p) for p in row['closed_paths'])}"
                        if row["stance"] == "partly_blocked"
                        else ""
                    )
                    + f"{' (own group)' if row['group'] == 'named' else ''} |"
                    for row in crawler_stances(robots, angles=analysis["view"].angles)
                ),
                "",
            ]
        )
    else:
        lines.extend(
            [
                {
                    "missing": "No robots.txt (HTTP 4xx): every crawler may fetch every page.",
                    "not_collected": "robots.txt was not collected for this run.",
                    "refused": "The site refused Tin's reader for robots.txt, so its rules "
                    "and AI crawler stance are unknown.",
                }.get(robots.get("status"), "robots.txt could not be read."),
                "",
            ]
        )

    access = analysis["view"].access
    if access.get("rows"):
        agents = list(dict.fromkeys(row["agent"] for row in access["rows"]))
        lines.extend(
            [
                "### What the site returns to each reader",
                "",
                "Each page read as a browser and with each AI crawler's user agent. CDNs can "
                "verify crawlers by IP address, so a difference means likely, not certain.",
                "",
                "| Page | " + " | ".join(agents) + " |",
                "| --- | " + " | ".join("---" for _ in agents) + " |",
            ]
        )
        for url in access.get("pages", []):
            cells = []
            for agent in agents:
                row = next(
                    (r for r in access["rows"] if r["url"] == url and r["agent"] == agent), {}
                )
                cells.append(
                    str(row["status_code"]) + (" refused" if row.get("status") == "refused" else "")
                    if row.get("status_code")
                    else row.get("status", "unknown").replace("_", " ")
                )
            lines.append(f"| {md(_path(url, host))} | " + " | ".join(cells) + " |")
        lines.append("")
    files = analysis["view"].files
    llms = files.get("llms_txt") or {}
    if llms.get("status") in {"observed", "missing"}:
        lines.extend(
            [
                "llms.txt: "
                + ("published." if llms["status"] == "observed" else "not published.")
                + " It is a proposed convention; no major assistant has confirmed it reads it.",
                "",
            ]
        )

    speed = speed_rows(analysis["pagespeed"], angles=analysis["view"].angles)
    if speed:
        lines.extend(
            ["## Speed", "", "| Page | Data | LCP | INP | CLS |", "| --- | --- | --- | --- | --- |"]
        )
        for row in speed:
            if row["status"] != "observed":
                lines.append(f"| {md(_path(row['url'], host))} | unknown | | | |")
                continue
            values, grades = row["values"], row["grades"]
            lines.append(
                f"| {md(_path(row['url'], host))} | {row.get('source_label', row['source'])} | "
                + _grade_cell(grades, "lcp_ms", f"{(values['lcp_ms'] or 0) / 1000:.1f} s")
                + " | "
                + _grade_cell(grades, "inp_ms", f"{values['inp_ms'] or 0:.0f} ms")
                + " | "
                + _grade_cell(grades, "cls", f"{values['cls'] or 0:.2f}")
                + " |"
            )
        lines.append("")
        scored = [row for row in speed if row["status"] == "observed" and row.get("lighthouse")]
        if scored:
            lines.extend(
                [
                    "Google Lighthouse scores (mobile), out of 100:",
                    "",
                    "| Page | SEO | Accessibility | Best practices |",
                    "| --- | ---: | ---: | ---: |",
                    *(
                        f"| {md(_path(row['url'], host))} | "
                        + " | ".join(
                            str(round(score * 100)) if score is not None else "unknown"
                            for score in (
                                row["lighthouse"]["scores"].get(name)
                                for name in ("seo", "accessibility", "best-practices")
                            )
                        )
                        + " |"
                        for row in scored
                    ),
                    "",
                ]
            )

    inspection = analysis["view"].inspection
    if inspection.get("results") and not any(
        row.get("status") == "observed" for row in inspection["results"]
    ):
        lines.extend(
            [
                "## Google index status",
                "",
                "Search Console URL Inspection returned no usable result for the "
                f"{len(inspection['results'])} key pages asked about.",
                "",
            ]
        )
    elif inspection.get("results"):
        lines.extend(
            [
                "## Google index status",
                "",
                "Search Console URL Inspection for the key pages: Google's own view of each.",
                "",
                "| Page | Indexed | Coverage | Last crawled |",
                "| --- | --- | --- | --- |",
                *(
                    f"| {md(_path(row['url'], host))} | "
                    + (
                        ("yes" if row.get("indexed") else "no")
                        if row.get("status") == "observed"
                        else "unknown"
                    )
                    + f" | {md(row.get('coverage_state') or '')} | "
                    + f"{(row.get('last_crawl_time') or '')[:10]} |"
                    for row in inspection["results"]
                ),
                "",
            ]
        )

    review = analysis["view"].content_review
    if review.get("status") == "completed":
        lines.extend(
            [
                "## Answer-engine readiness of top pages",
                "",
                "The pages with the most search impressions, reviewed for how AI answers quote "
                "pages: a direct answer near the top, question-shaped headings, sections that "
                "stand alone, specific facts, sources, a date and an author. The direct-answer, "
                "section and fact judgments come from a model and are hypotheses; the rest is "
                "measured.",
                "",
                "| Page | Main search | Gaps |",
                "| --- | --- | --- |",
                *(
                    f"| {md(_path(row['url'], host))} | {md(row.get('query') or '')} | "
                    + (md(", ".join(gap.replace("_", " ") for gap in row["gaps"])) or "none")
                    + " |"
                    for row in review["pages"]
                ),
                "",
            ]
        )

    lines.extend(["## Technical SEO (provider crawl)", ""])
    crawled = crawl.get("pages", [])
    lines.append(f"{len(crawled)} pages retained from the provider crawl.")
    lines.append("")
    if crawled:
        lines.extend(
            [
                "| Check | Problem | No problem | Not applicable | Unknown |",
                "| --- | ---: | ---: | ---: | ---: |",
                *(
                    f"| {row['check_id']} | "
                    + " | ".join(
                        str(row["outcomes"][key])
                        for key in ("problem", "pass", "not_applicable", "unknown")
                    )
                    + " |"
                    for row in technical_coverage
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## AI visibility",
            "",
            ai.get("summary", "Not measured."),
            "",
            *question_set_lines(ai),
            *ai_details,
            "Branded fact-check answers are separate observations in the evidence. "
            "They do not count toward the unbranded baseline or certify factual accuracy.",
            "",
            "## Evidence and limits",
            "",
            f"Crawl: {crawl.get('status', 'unknown')}. {crawl.get('note', '')}",
            "",
        ]
    )
    unknown = [
        row for row in analysis["site_check_coverage"] if row["status"] in {"unknown", "partial"}
    ]
    unknown_technical = [
        row["check_id"] for row in technical_coverage if row["outcomes"]["unknown"]
    ]
    if unknown or unknown_technical:
        lines.extend(["Checks that returned unknown or partial results:", ""])
        lines.extend(f"- {row['check'].replace('_', ' ')}: {md(row['note'])}" for row in unknown)
        if unknown_technical:
            lines.append(
                "- provider checks with unknown pages: " + ", ".join(unknown_technical[:12])
            )
        lines.append("")
    lines.extend(
        [
            "A check is an observation, not proof of indexing or ranking impact. Unknown checks "
            "are not passes. Tin read the static HTML without running JavaScript. This version "
            "does not measure backlinks, private product analytics or AI engines other than "
            "the one named above.",
            "",
        ]
    )
    return lines
