"""Site checks for organic-audit-v10, computed from saved evidence only.

Every function here is deterministic over the saved robots.txt, sitemap, static HTML facts,
provider crawl and Search Console rows, so a later reader can recompute the findings.
Unobserved facts are unknown, never passes: a page Tin could not read has no H1 verdict,
and structured data is never reported missing from unrendered HTML.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from tin_lite.organic_audit_format import count, site_finding
from tin_lite.organic_audit_site import (
    AI_SEARCH_CRAWLERS,
    GOOGLE_AGENT,
    crawler_stances,
    is_ad_landing_url,
    is_noindex,
    is_utility_url,
    language_prefix,
    named_groups_missing_wildcard_rules,
    robots_allows,
    url_key,
)

VITALS = {"lcp_ms": (2500, 4000), "inp_ms": (200, 500), "cls": (0.1, 0.25)}
VITAL_LABELS = {"lcp_ms": "LCP", "inp_ms": "INP", "cls": "CLS"}


def _path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


class SiteView:
    """Lookup tables over one run's saved evidence, keyed by host-independent URL keys."""

    def __init__(
        self,
        *,
        host: str,
        hosts: tuple[str, ...],
        site: dict,
        crawl_pages: list[dict],
        search_pages: list[dict],
        search_queries: list[dict],
    ) -> None:
        self.host, self.hosts = host, hosts
        files = site.get("files") or {}
        self.site_status = files.get("status", "not_collected")
        self.robots = files.get("robots") or {
            "status": "unreachable" if self.site_status == "unavailable" else "not_collected"
        }
        self.sitemaps = files.get("sitemaps") or {}
        self.entries = [
            entry for entry in self.sitemaps.get("urls", []) if self.in_scope(entry["loc"])
        ]
        self.sitemap_keys = {url_key(entry["loc"]) for entry in self.entries}
        self.facts: dict[str, dict] = {}
        for record in site.get("pages", []):
            key = url_key(record["url"])
            current = self.facts.get(key)
            if current is None or (
                current.get("fetch") != "observed" and record.get("fetch") == "observed"
            ):
                self.facts[key] = record
        self.crawl = {url_key(page["url"]): page for page in crawl_pages}
        self.search: dict[str, dict] = {}
        for row in search_pages:
            entry = self.search.setdefault(
                url_key(row["url"]),
                {"url": row["url"], "clicks": 0, "impressions": 0, "position": row["position"]},
            )
            entry["clicks"] += row["clicks"]
            entry["impressions"] += row["impressions"]
        self.top_query: dict[str, dict] = {}
        for row in search_queries:
            key = url_key(row["url"])
            best = self.top_query.get(key)
            if best is None or row["impressions"] > best["impressions"]:
                self.top_query[key] = row
        self.site_collected = files.get("status") == "observed"

    def in_scope(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return parts.scheme in {"https", "http"} and parts.hostname in self.hosts

    def observed(self) -> list[dict]:
        return sorted(
            (f for f in self.facts.values() if f.get("fetch") == "observed"),
            key=lambda f: f["url"],
        )

    def title(self, key: str) -> str | None:
        facts = self.facts.get(key) or {}
        if facts.get("fetch") == "observed" and facts.get("title"):
            return facts["title"]
        crawl = self.crawl.get(key) or {}
        return crawl.get("title") or None

    def googlebot_allowed(self, url: str) -> bool | None:
        return robots_allows(self.robots, GOOGLE_AGENT, url)

    def open_to_index(self, key: str) -> bool | None:
        """Served with 2xx, no noindex and not closed to Googlebot. None when unknown."""
        facts = self.facts.get(key)
        if not facts or facts.get("fetch") != "observed":
            if facts and facts.get("fetch") in {"redirect", "http_error"}:
                return False
            return None
        if not 200 <= facts["status_code"] < 300 or is_noindex(facts):
            return False
        allowed = self.googlebot_allowed(facts["url"])
        return None if allowed is None else allowed

    def canonical_elsewhere(self, key: str) -> str | None:
        facts = self.facts.get(key) or {}
        canonical = facts.get("canonical")
        if facts.get("fetch") != "observed" or not canonical:
            return None
        if not self.in_scope(canonical) or url_key(canonical) != key:
            return canonical
        return None

    def search_note(self, key: str) -> str:
        entry = self.search.get(key)
        if not entry or not entry["impressions"]:
            return ""
        query = self.top_query.get(key)
        where = (
            f'ranks position {query["position"]:g} for "{query["query"]}"'
            if query
            else f"average position {entry['position']:g}"
        )
        return f"{where} ({_fmt(entry['impressions'])} impressions, {_fmt(entry['clicks'])} clicks)"


def coverage(view: SiteView, plan: dict | None, *, page_cap: int | None) -> dict:
    """How many sitemap pages Tin actually read, and which it did not.

    A page counts as inspected when Tin received a response for it. Without collected site
    evidence (an explicit answer completion of an older run) the provider crawl is counted.
    """
    if view.site_collected:
        inspected = {
            key
            for key, facts in view.facts.items()
            if facts.get("fetch") in {"observed", "redirect", "http_error", "not_html"}
        }
    else:
        inspected = set(view.crawl)
    sitemap_read = any(
        row.get("status") == "observed" and row.get("kind") == "urlset"
        for row in view.sitemaps.get("files", [])
    )
    # URLs past the stored list were never available to read; they count as not inspected.
    unlisted = (
        max(0, view.sitemaps.get("total_urls", 0) - len(view.sitemaps.get("urls", [])))
        if sitemap_read
        else 0
    )
    skipped = sorted(
        view.sitemap_keys - inspected,
        key=lambda k: (-view.search.get(k, {}).get("impressions", 0), k),
    )
    missed_search = sorted(
        (k for k, v in view.search.items() if v["impressions"] > 0 and k not in inspected),
        key=lambda k: (-view.search[k]["impressions"], k),
    )
    forms = {url_key(entry["loc"]): entry["loc"] for entry in view.entries}
    inspected_sitemap = len(view.sitemap_keys & inspected)
    complete = (
        sitemap_read
        and not skipped
        and not missed_search
        and not view.sitemaps.get("urls_capped")
        and not view.sitemaps.get("unread")
    )
    return {
        "status": "complete" if complete else "partial",
        "site_collected": view.site_collected,
        "site_status": view.site_status,
        "sitemap_read": sitemap_read,
        "sitemap_pages": len(view.sitemap_keys) + unlisted,
        "inspected_sitemap_pages": inspected_sitemap,
        "inspected_pages": len(inspected),
        "page_cap": page_cap,
        "skipped_sitemap_pages": len(skipped) + unlisted,
        "skipped": [
            {"url": forms.get(k, k), "impressions": view.search.get(k, {}).get("impressions", 0)}
            for k in skipped[:500]
        ],
        "skipped_search_pages": [
            {"url": view.search[k]["url"], "impressions": view.search[k]["impressions"]}
            for k in missed_search[:100]
        ],
        "selection": {
            reason: sum(1 for row in (plan or {}).get("selected", []) if row["reason"] == reason)
            for reason in ("homepage", "search_impressions", "section", "section_fill")
        }
        if plan
        else None,
        "sections": (plan or {}).get("sections", [])[:50],
    }


def coverage_label(summary: dict) -> str:
    cap = f" (page cap {summary['page_cap']})" if summary.get("page_cap") else ""
    if summary["sitemap_read"] and summary["status"] == "complete":
        return f"complete: all {summary['sitemap_pages']} sitemap pages inspected{cap}"
    if summary["sitemap_read"]:
        return (
            f"partial: {summary['inspected_sitemap_pages']} of {summary['sitemap_pages']} "
            f"sitemap pages inspected{cap}"
        )
    if not summary.get("site_collected", True):
        reason = (
            "Tin could not read the site"
            if summary.get("site_status") == "unavailable"
            else "site files were not collected"
        )
        return f"partial: {reason}; {count(summary['inspected_pages'], 'page')} crawled"
    return (
        f"partial: no readable sitemap; {count(summary['inspected_pages'], 'page')} inspected{cap}"
    )


def _robots_findings(view: SiteView, host: str, important: dict[str, str]) -> list[dict]:
    robots = view.robots
    status = robots.get("status")
    findings = []
    if status in {"unreachable", "server_error", "redirect"}:
        findings.append(
            site_finding(
                host=host,
                check_id="robots.unavailable",
                category="site",
                area="crawlability_indexation",
                issue="robots.txt could not be read",
                impact="high",
                evidence=[
                    f"{robots.get('url', '/robots.txt')}: "
                    + (
                        f"HTTP {robots['status_code']}"
                        if robots.get("status_code")
                        else "no response"
                    )
                    + ".",
                    "Google treats a robots.txt server error as a temporary block on crawling "
                    "the whole site.",
                ],
                fix=(
                    "Serve /robots.txt with status 200 (or 404 when you have no rules) from "
                    "the site's own host."
                ),
                priority="critical",
                evidence_kind="site_files",
                evidence_refs=["site.files.robots"],
            )
        )
        return findings
    if status not in {"observed", "missing"}:
        return findings
    blocked = [url for key, url in important.items() if view.googlebot_allowed(url) is False]
    if blocked:
        home_blocked = any(url_key(url) == "/" for url in blocked)
        searched = [u for u in blocked if view.search.get(url_key(u), {}).get("impressions")]
        findings.append(
            site_finding(
                host=host,
                check_id="robots.blocks_important_pages",
                category="site",
                area="crawlability_indexation",
                issue="robots.txt stops Google from crawling pages you want found",
                impact="high",
                evidence=[
                    "Disallowed for Googlebot: "
                    f"{count(len(blocked), 'page')} in the sitemap, with search impressions, "
                    "or the homepage.",
                    *(f"{_path(url)}" for url in blocked[:8]),
                ],
                fix=(
                    "Remove or narrow the Disallow rules that match these pages, then confirm "
                    "with Search Console's robots.txt report. Use noindex, not robots.txt, for "
                    "pages that should stay out of results."
                ),
                priority="critical" if home_blocked or searched else "high_impact",
                evidence_kind="site_files",
                urls=blocked,
                status="fail",
                evidence_refs=["site.files.robots"],
            )
        )
    if not robots.get("sitemaps"):
        findings.append(
            site_finding(
                host=host,
                check_id="robots.sitemap_reference_missing",
                category="site",
                area="crawlability_indexation",
                issue="robots.txt does not point crawlers to a sitemap",
                impact="low",
                evidence=[
                    "robots.txt returned 404."
                    if status == "missing"
                    else "robots.txt has no Sitemap: line."
                ],
                fix="Add `Sitemap: https://" + host + "/sitemap.xml` (your real sitemap URL).",
                priority="quick_win",
                evidence_kind="site_files",
                evidence_refs=["site.files.robots"],
            )
        )
    if status != "observed":
        return findings
    stances = crawler_stances(robots)
    closed = [row for row in stances if row["agent"] in AI_SEARCH_CRAWLERS]
    closed = [row for row in closed if row["stance"] == "blocked"]
    if closed:
        findings.append(
            site_finding(
                host=host,
                check_id="robots.ai_search_crawlers_blocked",
                category="site",
                area="crawlability_indexation",
                issue="robots.txt blocks AI search crawlers that fetch pages to cite them",
                impact="medium",
                evidence=[
                    ", ".join(row["agent"] for row in closed)
                    + " cannot fetch the homepage under your robots.txt.",
                    "Training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot) are a separate "
                    "choice and can stay blocked.",
                ],
                fix=(
                    "If you want to be cited in ChatGPT search and Perplexity answers, allow "
                    "OAI-SearchBot, ChatGPT-User and PerplexityBot in robots.txt."
                ),
                priority="high_impact",
                evidence_kind="site_files",
                evidence_refs=["site.files.robots"],
            )
        )
    reopened = named_groups_missing_wildcard_rules(robots)
    if reopened:
        findings.append(
            site_finding(
                host=host,
                check_id="robots.named_group_rules",
                category="site",
                area="crawlability_indexation",
                issue="Named robots.txt groups do not repeat the rules for all crawlers",
                impact="medium",
                evidence=[
                    "A crawler follows only its own group; it does not inherit the "
                    "User-agent: * rules (RFC 9309).",
                    *(
                        f"{row['agent']} may crawl {', '.join(row['reopened'][:5])}, which the "
                        "* group disallows."
                        for row in reopened[:8]
                    ),
                ],
                fix=(
                    "Copy the Disallow lines from the User-agent: * group into each named group "
                    "that should respect them, or confirm the difference is intended."
                ),
                priority="quick_win",
                evidence_kind="site_files",
                evidence_refs=["site.files.robots"],
            )
        )
    return findings


def _sitemap_findings(view: SiteView, host: str) -> list[dict]:
    findings = []
    files = view.sitemaps.get("files", [])
    read = [row for row in files if row.get("status") == "observed"]
    if not view.site_collected:
        return findings
    if not read:
        findings.append(
            site_finding(
                host=host,
                check_id="sitemap.missing",
                category="site",
                area="crawlability_indexation",
                issue="No readable XML sitemap was found",
                impact="medium",
                evidence=[
                    f"{row['url']}: {row.get('status')}"
                    + (f" (HTTP {row['status_code']})" if row.get("status_code") else "")
                    for row in files[:5]
                ]
                or ["No sitemap was referenced or found at /sitemap.xml."],
                fix=(
                    "Publish an XML sitemap of your canonical, indexable pages, reference it in "
                    "robots.txt and submit it in Search Console."
                ),
                priority="quick_win",
                evidence_kind="site_files",
                evidence_refs=["site.files.sitemaps"],
            )
        )
        return findings
    unread = [row for row in files if row.get("status") != "observed"]
    if unread:
        findings.append(
            site_finding(
                host=host,
                check_id="sitemap.unreadable_files",
                category="site",
                area="crawlability_indexation",
                issue="Some sitemap files could not be read",
                impact="low",
                evidence=[
                    f"{row['url']}: {row.get('status')}"
                    + (f" (HTTP {row['status_code']})" if row.get("status_code") else "")
                    for row in unread[:8]
                ],
                fix="Make every sitemap file listed in robots.txt or the sitemap index return "
                "valid XML with status 200.",
                priority="quick_win",
                evidence_kind="site_files",
                evidence_refs=["site.files.sitemaps"],
            )
        )
    reasons: dict[str, list[str]] = {}
    checked = 0
    for entry in view.entries:
        key = url_key(entry["loc"])
        if urlsplit(entry["loc"]).scheme == "http":
            reasons.setdefault("http", []).append(entry["loc"])
        facts = view.facts.get(key)
        if facts is None or facts.get("fetch") not in {"observed", "redirect", "http_error"}:
            crawl = view.crawl.get(key)
            code = (crawl or {}).get("status_code")
            if type(code) is int:
                checked += 1
                if code >= 300:
                    reasons.setdefault("redirect" if code < 400 else "error", []).append(
                        entry["loc"]
                    )
            continue
        checked += 1
        if facts.get("fetch") == "redirect":
            reasons.setdefault("redirect", []).append(entry["loc"])
        elif facts.get("fetch") == "http_error":
            reasons.setdefault("error", []).append(entry["loc"])
        elif facts.get("fetch") == "observed":
            if is_noindex(facts):
                reasons.setdefault("noindex", []).append(entry["loc"])
            elif view.canonical_elsewhere(key):
                reasons.setdefault("canonical_elsewhere", []).append(entry["loc"])
        if view.googlebot_allowed(entry["loc"]) is False:
            reasons.setdefault("blocked_by_robots", []).append(entry["loc"])
    labels = {
        "http": "on plain HTTP",
        "noindex": "marked noindex",
        "redirect": "redirecting",
        "error": "with an error status",
        "canonical_elsewhere": "canonicalized to another URL",
        "blocked_by_robots": "disallowed for Googlebot",
    }
    affected = sorted({url for urls in reasons.values() for url in urls})
    if affected:
        findings.append(
            site_finding(
                host=host,
                check_id="sitemap.non_indexable_urls",
                category="site",
                area="crawlability_indexation",
                issue="The sitemap lists pages that cannot or should not be indexed",
                impact="high" if len(affected) * 10 >= max(1, checked) else "medium",
                evidence=[
                    f"Of {count(checked, 'sitemap URL')} checked, {len(affected)} cannot or "
                    "should not be indexed: "
                    + "; ".join(f"{len(urls)} {labels[r]}" for r, urls in sorted(reasons.items()))
                    + ".",
                    *(
                        f"{labels[r][0].upper()}{labels[r][1:]}: "
                        + ", ".join(_path(u) for u in urls[:4])
                        for r, urls in sorted(reasons.items())
                    ),
                ],
                fix=(
                    "List only canonical HTTPS URLs that return 200 and are open to indexing. "
                    "Remove redirecting, noindexed, erroring and canonicalized URLs from the "
                    "sitemap."
                ),
                priority="quick_win",
                evidence_kind="site_files",
                urls=affected,
                status="fail",
                evidence_refs=["site.files.sitemaps", "site.pages"],
            )
        )
    ads = [entry["loc"] for entry in view.entries if is_ad_landing_url(entry["loc"])]
    if ads:
        open_ads = [u for u in ads if view.open_to_index(url_key(u)) is True]
        findings.append(
            site_finding(
                host=host,
                check_id="sitemap.ad_landing_urls",
                category="site",
                area="crawlability_indexation",
                issue="The sitemap lists paid-ad landing pages",
                impact="medium",
                evidence=[
                    f"Ad landing pages in the sitemap: {len(ads)}; open to indexing: "
                    f"{len(open_ads)}.",
                    "Examples: " + ", ".join(_path(u) for u in ads[:5]),
                ],
                fix=(
                    "Remove ad landing pages from the sitemap and add noindex to them, or point "
                    "their canonical to the matching organic page."
                ),
                priority="quick_win",
                evidence_kind="site_files",
                urls=ads,
                evidence_refs=["site.files.sitemaps"],
            )
        )
    missing = []
    for key, entry in sorted(view.search.items(), key=lambda item: -item[1]["impressions"]):
        url = entry["url"]
        if (
            entry["impressions"] <= 0
            or key in view.sitemap_keys
            or is_utility_url(url)
            or is_ad_landing_url(url)
            or view.open_to_index(key) is False
            or view.canonical_elsewhere(key)
        ):
            continue
        missing.append(entry)
    if missing:
        findings.append(
            site_finding(
                host=host,
                check_id="sitemap.missing_search_pages",
                category="site",
                area="crawlability_indexation",
                issue="Pages that get search impressions are missing from the sitemap",
                impact="medium",
                evidence=[
                    "Pages with Search Console impressions that are not in the sitemap: "
                    f"{len(missing)}.",
                    *(
                        f"{_path(e['url'])}: {_fmt(e['impressions'])} impressions, "
                        f"{_fmt(e['clicks'])} clicks"
                        + (
                            ""
                            if view.open_to_index(url_key(e["url"])) is True
                            else " (indexability not observed)"
                        )
                        for e in missing[:8]
                    ),
                ],
                fix="Add these canonical pages to the sitemap, or noindex them if they should "
                "not appear in search.",
                priority="quick_win",
                evidence_kind="search_console",
                urls=[e["url"] for e in missing],
                evidence_refs=["search_console.value.pages", "site.files.sitemaps"],
            )
        )
    stamps = [entry.get("lastmod") for entry in view.entries if entry.get("lastmod")]
    if len(stamps) >= 5 and len(set(stamps)) == 1 and len(stamps) == len(view.entries):
        findings.append(
            site_finding(
                host=host,
                check_id="sitemap.uniform_lastmod",
                category="site",
                area="crawlability_indexation",
                issue="Every sitemap URL has the same lastmod date",
                impact="low",
                evidence=[
                    f"All {len(stamps)} URLs report lastmod {stamps[0]}, which usually means "
                    "the build time is stamped on every page. Google then ignores the field."
                ],
                fix="Set lastmod to each page's real last content change, or omit it.",
                priority="quick_win",
                evidence_kind="site_files",
                evidence_refs=["site.files.sitemaps"],
            )
        )
    return findings


def _page_line(view: SiteView, facts: dict, *, include_search: bool = True) -> str:
    key = url_key(facts["url"])
    parts = [_path(facts["url"]), "open to indexing (no noindex tag or header)"]
    note = view.search_note(key) if include_search else ""
    if note:
        parts.append(note)
    if facts.get("title"):
        parts.append(f'title "{facts["title"]}"')
    if facts.get("h1_count") == 0:
        parts.append("no H1")
    canonical = view.canonical_elsewhere(key)
    if canonical:
        parts.append(f"canonical points to {canonical}")
    return ": ".join(parts[:1]) + (" — " + "; ".join(parts[1:]) if len(parts) > 1 else "")


def _indexation_findings(view: SiteView, host: str) -> list[dict]:
    findings = []
    observed = view.observed()
    utility = [
        f for f in observed if is_utility_url(f["url"]) and view.open_to_index(url_key(f["url"]))
    ]
    if utility:
        searched = any(view.search.get(url_key(f["url"]), {}).get("impressions") for f in utility)
        findings.append(
            site_finding(
                host=host,
                check_id="indexation.utility_pages_indexable",
                category="site",
                area="crawlability_indexation",
                issue="Sign-in and account pages are open to indexing",
                impact="high" if searched else "medium",
                evidence=[_page_line(view, f) for f in utility[:8]],
                fix=(
                    'Add <meta name="robots" content="noindex"> to sign-in, sign-up and account '
                    "pages, keep them crawlable so Google sees the tag, and leave them out of "
                    "the sitemap."
                ),
                priority="quick_win",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in utility],
                status="fail",
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    ads = [
        f for f in observed if is_ad_landing_url(f["url"]) and view.open_to_index(url_key(f["url"]))
    ]
    if ads:
        listed = sum(url_key(f["url"]) in view.sitemap_keys for f in ads)
        findings.append(
            site_finding(
                host=host,
                check_id="indexation.ad_landing_pages_indexable",
                category="site",
                area="crawlability_indexation",
                issue="Paid-ad landing pages are open to indexing",
                impact="medium",
                evidence=[
                    f"Ad landing pages without noindex: {len(ads)}; in the sitemap: {listed}.",
                    "Examples: " + ", ".join(_path(f["url"]) for f in ads[:5]),
                ],
                fix=(
                    "Add noindex to ad landing pages and remove them from the sitemap, or give "
                    "them a canonical pointing to the matching organic page."
                ),
                priority="quick_win",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in ads],
                status="fail",
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    elsewhere = [
        f
        for f in observed
        if view.open_to_index(url_key(f["url"])) and view.canonical_elsewhere(url_key(f["url"]))
    ]
    if elsewhere:
        searched = [f for f in elsewhere if view.search.get(url_key(f["url"]), {}).get("clicks")]
        findings.append(
            site_finding(
                host=host,
                check_id="indexation.canonical_elsewhere",
                category="site",
                area="crawlability_indexation",
                issue="Indexable pages name a different page as canonical",
                impact="high"
                if any(view.search.get(url_key(f["url"])) for f in elsewhere)
                else "medium",
                evidence=[
                    "Open to indexing but canonicalized to another URL: "
                    f"{count(len(elsewhere), 'page')}"
                    + (f", {len(searched)} with clicks" if searched else "")
                    + ".",
                    *(
                        f"{_path(f['url'])} → {view.canonical_elsewhere(url_key(f['url']))}"
                        for f in elsewhere[:8]
                    ),
                ],
                fix=(
                    "Give each page a self-referencing canonical, or redirect or noindex it if "
                    "it really duplicates the other page."
                ),
                priority="high_impact",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in elsewhere],
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    hidden = [
        f
        for f in observed
        if is_noindex(f)
        and not is_utility_url(f["url"])
        and not is_ad_landing_url(f["url"])
        and view.search.get(url_key(f["url"]), {}).get("impressions")
    ]
    if hidden:
        findings.append(
            site_finding(
                host=host,
                check_id="indexation.noindex_with_search_traffic",
                category="site",
                area="crawlability_indexation",
                issue="Pages that still get search traffic are marked noindex",
                impact="high",
                evidence=[
                    f"{_path(f['url'])}: noindex; {view.search_note(url_key(f['url']))}"
                    for f in hidden[:8]
                ],
                fix="Remove noindex from pages you want in search results; keep it only on "
                "pages that should disappear.",
                priority="critical",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in hidden],
                status="fail",
                evidence_refs=["site.pages", "search_console.value.pages"],
            )
        )
    multiple = [f for f in observed if f.get("canonical_count", 0) > 1]
    if multiple:
        findings.append(
            site_finding(
                host=host,
                check_id="indexation.multiple_canonicals",
                category="site",
                area="crawlability_indexation",
                issue="Pages declare more than one canonical URL",
                impact="medium",
                evidence=[f"Pages with two or more canonical link tags: {len(multiple)}."]
                + [f"Examples: {', '.join(_path(f['url']) for f in multiple[:5])}"],
                fix="Keep exactly one canonical link tag per page.",
                priority="quick_win",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in multiple],
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    return findings


def _onpage_findings(view: SiteView, host: str) -> list[dict]:
    findings = []
    pages = [f for f in view.observed() if 200 <= f["status_code"] < 300 and not is_noindex(f)]
    no_h1 = [f for f in pages if f.get("h1_count") == 0]
    if no_h1:
        findings.append(
            site_finding(
                host=host,
                check_id="onpage.h1_missing",
                category="site",
                area="on_page",
                issue="Pages have no H1 heading in their HTML",
                impact="medium",
                evidence=[
                    "Indexable pages without an <h1> in the HTML served before JavaScript "
                    f"runs: {len(no_h1)} of the {len(pages)} Tin read.",
                    "Examples: " + ", ".join(_path(f["url"]) for f in no_h1[:6]),
                ],
                fix="Give each page one H1 that states its topic in the searcher's words.",
                priority="quick_win",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in no_h1],
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    many_h1 = [f for f in pages if f.get("h1_count", 0) > 1]
    if many_h1:
        findings.append(
            site_finding(
                host=host,
                check_id="onpage.h1_multiple",
                category="site",
                area="on_page",
                issue="Pages have more than one H1",
                impact="low",
                evidence=[
                    f"Pages with two or more <h1> elements: {len(many_h1)}.",
                    "Examples: " + ", ".join(_path(f["url"]) for f in many_h1[:6]),
                ],
                fix="Keep one H1 per page and use H2 and H3 for sections.",
                priority="long_term",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in many_h1],
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    no_lang = [f for f in pages if not f.get("lang")]
    if no_lang:
        findings.append(
            site_finding(
                host=host,
                check_id="onpage.lang_missing",
                category="site",
                area="technical_foundations",
                issue="Pages do not declare their language",
                impact="low",
                evidence=[
                    f"Pages without a lang attribute on <html>: {len(no_lang)}.",
                    "Examples: " + ", ".join(_path(f["url"]) for f in no_lang[:6]),
                ],
                fix='Set <html lang="…"> to the language of each page\'s content.',
                priority="quick_win",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in no_lang],
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    localized: dict[str, list[dict]] = {}
    for facts in pages:
        prefix = language_prefix(facts["url"])
        if prefix:
            localized.setdefault(prefix, []).append(facts)
    mismatched = []
    for prefix, rows in sorted(localized.items()):
        expected = prefix.split("-")[0].split("_")[0]
        for facts in rows:
            declared = (facts.get("lang") or "").lower().replace("_", "-").split("-")[0]
            if declared and declared != expected:
                mismatched.append((prefix, facts))
    if mismatched:
        by_prefix: dict[str, dict[str, int]] = {}
        for prefix, facts in mismatched:
            counts = by_prefix.setdefault(prefix, {})
            counts[facts["lang"]] = counts.get(facts["lang"], 0) + 1
        findings.append(
            site_finding(
                host=host,
                check_id="onpage.lang_mismatch",
                category="site",
                area="technical_foundations",
                issue="Translated pages declare the wrong language",
                impact="medium",
                evidence=[
                    f"/{prefix}/: "
                    + ", ".join(
                        f'lang="{lang}" on {count(n, "page")}' for lang, n in sorted(counts.items())
                    )
                    + f"; expected {prefix}."
                    for prefix, counts in sorted(by_prefix.items())
                ]
                + ["Examples: " + ", ".join(_path(f["url"]) for _, f in mismatched[:5])],
                fix=(
                    "Set the lang attribute on each translated page to its language, for "
                    'example <html lang="nl"> under /nl/.'
                ),
                priority="quick_win",
                evidence_kind="page_fetch",
                urls=[f["url"] for _, f in mismatched],
                next_action="technical_fix",
                evidence_refs=["site.pages"],
            )
        )
    if localized:
        missing = [f for rows in localized.values() for f in rows if not f.get("hreflang")]
        default = [f for f in pages if not language_prefix(f["url"])]
        default_with = sum(bool(f.get("hreflang")) for f in default)
        if missing:
            findings.append(
                site_finding(
                    host=host,
                    check_id="onpage.hreflang_missing",
                    category="site",
                    area="technical_foundations",
                    issue="Language versions are not linked with hreflang",
                    impact="medium",
                    evidence=[
                        *(
                            f"/{prefix}/: no hreflang alternates on "
                            f"{sum(not f.get('hreflang') for f in rows)} of {len(rows)} pages."
                            for prefix, rows in sorted(localized.items())
                        ),
                        "Default-language pages with hreflang alternates: "
                        f"{default_with} of {len(default)}.",
                    ],
                    fix=(
                        "Link each page to its translations with reciprocal "
                        '<link rel="alternate" hreflang="…"> tags (including x-default), so '
                        "Google shows the right language and does not treat them as duplicates."
                    ),
                    priority="high_impact",
                    evidence_kind="page_fetch",
                    urls=[f["url"] for f in missing],
                    next_action="technical_fix",
                    evidence_refs=["site.pages"],
                )
            )
    heavy = [f for f in view.observed() if f.get("truncated")]
    if heavy:
        findings.append(
            site_finding(
                host=host,
                check_id="onpage.html_over_limit",
                category="site",
                area="technical_foundations",
                issue="Pages serve more HTML than Google reads",
                impact="low",
                evidence=[
                    f"Pages over 2 MB of HTML: {len(heavy)}. Googlebot is reported to read only "
                    "the first 2 MB.",
                    "Examples: " + ", ".join(_path(f["url"]) for f in heavy[:5]),
                ],
                fix="Move inline data and scripts out of the HTML so content fits in 2 MB.",
                priority="long_term",
                evidence_kind="page_fetch",
                urls=[f["url"] for f in heavy],
                evidence_refs=["site.pages"],
            )
        )
    return findings


def vital_grade(metric: str, value: float | None) -> str | None:
    if value is None:
        return None
    good, poor = VITALS[metric]
    return "good" if value < good else "poor" if value >= poor else "needs_improvement"


def speed_rows(pagespeed: dict) -> list[dict]:
    rows = []
    for item in pagespeed.get("results", []):
        result = item.get("result") or {}
        if result.get("status") != "observed":
            rows.append({"url": item["url"], "status": "unknown"})
            continue
        field, lab = result["field"], result["lab"]
        values = {
            "lcp_ms": field["lcp_ms"] if field["lcp_ms"] is not None else lab["lcp_ms"],
            "inp_ms": field["inp_ms"],
            "cls": field["cls"] if field["cls"] is not None else lab["cls"],
        }
        rows.append(
            {
                "url": item["url"],
                "status": "observed",
                "source": "field" if field["lcp_ms"] is not None else "lab",
                "values": values,
                "grades": {name: vital_grade(name, value) for name, value in values.items()},
            }
        )
    return rows


def _speed_findings(pagespeed: dict, host: str) -> list[dict]:
    rows = [row for row in speed_rows(pagespeed) if row["status"] == "observed"]
    slow = [
        row
        for row in rows
        if any(grade in {"poor", "needs_improvement"} for grade in row["grades"].values())
    ]
    if not slow:
        return []

    def show(name, value):
        if value is None:
            return f"{VITAL_LABELS[name]} unknown"
        text = (
            f"{value / 1000:.1f} s"
            if name == "lcp_ms"
            else (f"{value:.0f} ms" if name == "inp_ms" else f"{value:.2f}")
        )
        return f"{VITAL_LABELS[name]} {text}"

    return [
        site_finding(
            host=host,
            check_id="speed.core_web_vitals",
            category="site",
            area="technical_foundations",
            issue="Pages are slower than Google's Core Web Vitals targets",
            impact="high" if any("poor" in row["grades"].values() for row in slow) else "medium",
            evidence=[
                "Targets: LCP under 2.5 s, INP under 200 ms, CLS under 0.1 (PageSpeed Insights, "
                "mobile).",
                *(
                    f"{_path(row['url'])} ({row['source']} data): "
                    + ", ".join(
                        show(name, row["values"][name])
                        + (
                            f" ({row['grades'][name].replace('_', ' ')})"
                            if row["grades"][name]
                            else ""
                        )
                        for name in VITALS
                    )
                    for row in slow
                ),
            ],
            fix=(
                "Start with the largest element above the fold (image size, fonts, render-"
                "blocking scripts), then long JavaScript tasks for INP and reserved space for "
                "late content for CLS."
            ),
            priority="long_term",
            evidence_kind="pagespeed",
            urls=[row["url"] for row in slow],
            evidence_refs=["site.pagespeed"],
        )
    ]


def important_urls(view: SiteView, home: str) -> dict[str, str]:
    result = {url_key(home): home}
    for entry in view.entries:
        result.setdefault(url_key(entry["loc"]), entry["loc"])
    for key, entry in view.search.items():
        if entry["impressions"] > 0:
            result.setdefault(key, entry["url"])
    return result


def site_findings(view: SiteView, *, home: str, pagespeed: dict) -> list[dict]:
    if not view.site_collected:
        return []
    return [
        *_robots_findings(view, view.host, important_urls(view, home)),
        *_sitemap_findings(view, view.host),
        *_indexation_findings(view, view.host),
        *_onpage_findings(view, view.host),
        *_speed_findings(pagespeed, view.host),
    ]


def site_check_coverage(view: SiteView, *, pagespeed: dict, search: dict) -> list[dict]:
    """Which site checks ran on evidence and which are unknown, stated in the report."""
    observed = view.observed()
    structured = [f for f in observed if f.get("json_ld_blocks") or f.get("microdata")]
    speed = [row for row in speed_rows(pagespeed) if row["status"] == "observed"]
    robots_status = view.robots.get("status")
    return [
        {
            "check": "robots_txt",
            "status": "observed" if robots_status in {"observed", "missing"} else "unknown",
            "note": {
                "observed": "robots.txt read.",
                "missing": "No robots.txt (HTTP 4xx): crawlers may fetch every page.",
            }.get(robots_status, "robots.txt could not be read."),
        },
        {
            "check": "sitemap",
            "status": "observed"
            if any(r.get("status") == "observed" for r in view.sitemaps.get("files", []))
            else "unknown",
            "note": f"{view.sitemaps.get('total_urls', 0)} URLs in "
            f"{sum(r.get('status') == 'observed' for r in view.sitemaps.get('files', []))} "
            "readable sitemap files.",
        },
        {
            "check": "page_html",
            "status": "observed" if observed else "unknown",
            "note": f"Static HTML read for {count(len(observed), 'page')} (noindex, canonical, "
            "H1, lang, hreflang). JavaScript was not run.",
        },
        {
            "check": "structured_data",
            "status": "partial" if structured else "unknown",
            "note": (
                f"Structured data found in the static HTML of {count(len(structured), 'page')}. "
                if structured
                else ""
            )
            + "Pages were not rendered, so structured data added by JavaScript is unknown; "
            "a page without it in static HTML is not reported as missing schema.",
        },
        {
            "check": "speed",
            "status": "observed" if speed else "unknown",
            "note": f"PageSpeed Insights measured {count(len(speed), 'page')}."
            if speed
            else {
                "not_configured": "PageSpeed Insights is not configured on this deployment.",
            }.get(pagespeed.get("status"), "PageSpeed Insights returned no measurement."),
        },
        {
            "check": "search_console_queries",
            "status": "observed" if search.get("status") == "completed" else "unknown",
            "note": "Query-level Search Console rows read."
            if search.get("status") == "completed"
            else "No matching Search Console property, so search checks did not run.",
        },
    ]
