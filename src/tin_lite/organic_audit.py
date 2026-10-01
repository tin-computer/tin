"""Versioned, deterministic contracts for the read-only organic audit.

Provider flags are observations, not an SEO score or permission to edit a site.
This module has no network, database, or Temporal dependencies.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

AUDIT_KEY = "organic.audit"
LEGACY_AUDIT_POLICY = {
    "version": "organic-audit-v1",
    "max_pages": 100,
    "max_poll_attempts": 120,
    "poll_seconds": 30,
    "max_questions": 12,
    "repetitions": 2,
    "brand_checks": 2,
    "model": "gpt-6-luna",
    "provider": "openai",
    "search_tool": "web_search",
    "max_tool_calls": 3,
    "max_output_tokens": 6000,
    # Conservative reservations, not a provider price quote. Uncertain requests
    # retain their entire reservation. Deployment must explicitly enable spending.
    "crawl_reservation_usd": "0.05",
    "search_reservation_usd": "0.20",
    "text_reservation_usd": "0.03",
    "pricing_checked_on": "2026-09-07",
}
V2_AUDIT_POLICY = {**LEGACY_AUDIT_POLICY, "version": "organic-audit-v2"}
V3_AUDIT_POLICY = {
    **V2_AUDIT_POLICY,
    "version": "organic-audit-v3",
    "max_research_attempts": 2,
    "max_panel_attempts": 2,
}
V4_AUDIT_POLICY = {**V3_AUDIT_POLICY, "version": "organic-audit-v4"}
V5_AUDIT_POLICY = {
    **V4_AUDIT_POLICY,
    "version": "organic-audit-v5",
    "verified_www_redirects": True,
    "max_response_bytes": 64_000,
    "max_observation_bytes": 80_000,
    "max_evidence_bytes": 3_000_000,
    "runtime_budget": "observed_supplier_exposure_when_metered",
}
V6_AUDIT_POLICY = {
    **V5_AUDIT_POLICY,
    "version": "organic-audit-v6",
    "standalone_question_review": True,
}
V7_AUDIT_POLICY = {
    **V6_AUDIT_POLICY,
    "version": "organic-audit-v7",
    "blind_question_interpretation": True,
    "question_interpretation_concurrency": 4,
}
V8_AUDIT_POLICY = {**V7_AUDIT_POLICY, "version": "organic-audit-v8", "answer_timeout_seconds": 180}
V9_AUDIT_POLICY = {
    **V8_AUDIT_POLICY,
    "version": "organic-audit-v9",
    "check_applicability": True,
    "respect_sitemap": True,
}
V10_AUDIT_POLICY = {
    **V9_AUDIT_POLICY,
    "version": "organic-audit-v10",
    # The ceiling for an operator-configured page cap; each run pins its own cap in scope.
    # At the published basic rate ($0.00015/page) 300 pages stay inside the $0.05 reservation.
    "max_pages": 300,
    "default_page_cap": 100,
    "max_priority_urls": 20,
    "max_crawl_evidence_bytes": 600_000,
    # Tin reads robots.txt, sitemaps and the static HTML of selected pages itself.
    "site_checks": True,
    "max_sitemap_files": 20,
    "max_sitemap_urls": 5000,
    "max_page_bytes": 2_000_000,
    "page_fetch_concurrency": 4,
    "search_console_days": 28,
    "search_console_page_rows": 1000,
    "search_console_query_rows": 5000,
    "near_page_one_positions": [4, 15],
    "near_page_one_min_impressions": 20,
    "low_ctr_max_position": 10,
    "low_ctr_min_impressions": 50,
    "pagespeed_max_urls": 3,
    "finding_format": "issue_impact_evidence_fix_priority",
    # A project's buyer questions are frozen by its first v10 audit of a site and market and
    # reused, three answers each, until a run asks for new ones. Keeping the first two
    # proposed buyer jobs (eight questions) holds answers at the earlier 12 x 2 = 24.
    "reuse_questions": True,
    "repetitions": 3,
    "max_panel_jobs": 2,
    # Each buyer job asks at most one question per family (four), so two jobs ask at most
    # eight. The cost ceiling is computed from this bound: 8 x 3 answers, like 12 x 2 before.
    "max_questions": 8,
}
# v11 keeps v10 and adds the audit angles. v10 is deployed, so none of this may change a run
# pinned to it: every addition below is read from the pinned policy, never assumed.
V11_AUDIT_POLICY = {
    **V10_AUDIT_POLICY,
    "version": "organic-audit-v11",
    # New site reads and checks: llms.txt, the plain-HTTP homepage, a made-up URL, redirect
    # hops, crawler access, page basics, structured data, answer-engine and trust signals,
    # accessibility and Lighthouse categories.
    "site_angles": True,
    # A validated panel keeps its accepted questions when the validator rejects a few, as
    # long as this many remain; fewer means a new draft.
    "min_panel_questions": 3,
    # Site and search evidence beyond the page facts. Translations of one page do not
    # compete, and a search needs this many impressions before two pages count as competing.
    "cannibalization_min_impressions": 10,
    # A page that had at least this many clicks in the previous 28 days and lost this share.
    "decay_min_previous_clicks": 10,
    "decay_drop_share": 0.4,
    "max_redirect_hops": 5,
    # Google's URL Inspection allows 2,000 inspections a day per property.
    "url_inspection_max_urls": 10,
    "access_check_pages": 2,
    # One text-model review of the top content pages' structure, within the ceiling.
    "content_review_pages": 5,
    # AI grading follows the visibility audit's ladder: found, mentioned, evaluated,
    # shortlisted, picked first. Each question also gets one answer without web search.
    "answer_ladder": True,
    "unsearched_answers": True,
    # Each finding's next_action says where the technical fix's repair plan puts it, so the
    # report and the fix never disagree about who handles a finding.
    "next_action_from_repair_plan": True,
}
# v12 keeps v11 and adds SUMMARY.json. Code workflows read project files of at most 64,000
# bytes, and a real crawl's findings.json and evidence.json are larger (tin.computer's
# evidence.json was 188 KB). v11 can deploy any time, so a run pinned to it writes exactly
# v11's files: nothing below is read unless the pinned policy carries it.
AUDIT_POLICY = {
    **V11_AUDIT_POLICY,
    "version": "organic-audit-v12",
    # One compact row per crawled page, finding counts by check and the AI headline, cut to
    # fit this many bytes and copied to reports/organic-audit/LATEST.json.
    "summary_max_bytes": 60_000,
    # Tin's page reader keeps up to this many distinct links to the audited site per page, so
    # the summary can count inbound internal links and click depth from the homepage.
    "max_internal_links": 250,
}

# Crawl, site-file and Search Console settings. They never change how an AI answer is
# requested or graded, so an explicit answer completion may ignore them.
SITE_EVIDENCE_POLICY_KEYS = frozenset(
    {
        "max_pages",
        "default_page_cap",
        "max_priority_urls",
        "max_crawl_evidence_bytes",
        "site_checks",
        "max_sitemap_files",
        "max_sitemap_urls",
        "max_page_bytes",
        "page_fetch_concurrency",
        "search_console_days",
        "search_console_page_rows",
        "search_console_query_rows",
        "near_page_one_positions",
        "near_page_one_min_impressions",
        "low_ctr_max_position",
        "low_ctr_min_impressions",
        "pagespeed_max_urls",
        "finding_format",
        "site_angles",
        "cannibalization_min_impressions",
        "decay_min_previous_clicks",
        "decay_drop_share",
        "max_redirect_hops",
        "url_inspection_max_urls",
        "access_check_pages",
        "content_review_pages",
        "summary_max_bytes",
        "max_internal_links",
        "next_action_from_repair_plan",
    }
)

# How a NEW question panel is drafted. An existing panel records its own answer count, so an
# explicit answer completion of an older run may ignore these too.
PANEL_PREPARATION_POLICY_KEYS = frozenset(
    {
        "reuse_questions",
        "repetitions",
        "max_panel_jobs",
        "max_questions",
        "answer_ladder",
        "unsearched_answers",
        "min_panel_questions",
    }
)
AI_RESULT_KEYS = ("mentioned", "owned_domain_cited", "shortlisted", "selected_first")


def panel_repetitions(panel: dict | None) -> int:
    """Answers per question. Panels drafted before v10 carry no count and used two."""
    return (panel or {}).get("repetitions", 2)


def question_results(panel: dict, observations: list[dict]) -> list[dict]:
    """Scored counts per frozen question. Unknown answers are neither negatives nor positives."""
    rows = []
    for index in range(len(panel["questions"])):
        scored = [
            row
            for row in observations
            if row.get("question_index") == index
            and row.get("status") == "completed"
            and row.get("mode") != "memory"
        ]
        rows.append(
            {
                "scored": len(scored),
                **{
                    key: sum(bool(row["classification"][key]) for row in scored)
                    for key in AI_RESULT_KEYS
                },
            }
        )
    return rows


def audit_policy(version: str = AUDIT_POLICY["version"]) -> dict:
    for policy in (
        LEGACY_AUDIT_POLICY,
        V2_AUDIT_POLICY,
        V3_AUDIT_POLICY,
        V4_AUDIT_POLICY,
        V5_AUDIT_POLICY,
        V6_AUDIT_POLICY,
        V7_AUDIT_POLICY,
        V8_AUDIT_POLICY,
        V9_AUDIT_POLICY,
        V10_AUDIT_POLICY,
        V11_AUDIT_POLICY,
        AUDIT_POLICY,
    ):
        if version == policy["version"]:
            return policy
    raise ValueError("Unsupported organic audit policy.")


def grounded_preparation(policy_version: str) -> bool:
    return audit_policy(policy_version) in (
        V3_AUDIT_POLICY,
        V4_AUDIT_POLICY,
        V5_AUDIT_POLICY,
        V6_AUDIT_POLICY,
        V7_AUDIT_POLICY,
        V8_AUDIT_POLICY,
        V9_AUDIT_POLICY,
        V10_AUDIT_POLICY,
        V11_AUDIT_POLICY,
        AUDIT_POLICY,
    )


# Only Tin-owned reason codes cross into evidence or the readable report. Never
# copy provider exceptions, response bodies, or model-generated error text here.
AUDIT_GAP_REASONS = {
    "response_incomplete": "The provider did not return a completed answer.",
    "response_refused": "The provider declined to answer.",
    "response_empty": "The answer was empty.",
    "response_too_large": "The answer exceeded the saved-evidence size limit.",
    "search_incomplete": "The answer lacked a completed, bounded web search.",
    "search_not_called": "The provider returned an answer without using the required web search.",
    "search_not_completed": "No web-search call completed successfully.",
    "search_limit_exceeded": "Completed web searches exceeded the pinned request limit.",
    "research_sources_missing": (
        "Product research returned no usable evidence from the requested website."
    ),
    "panel_identity_invalid": (
        "The proposed questions described a different or unsupported product."
    ),
    "panel_source_unobserved": (
        "A proposed question cited a page absent from the saved public research."
    ),
    "panel_questions_invalid": (
        "The proposed buyer questions did not meet the neutral question contract."
    ),
    "panel_review_rejected": (
        "The proposed buyer questions were not supported by the saved product research."
    ),
    "panel_review_invalid": "The question review named a question the panel does not have.",
    "panel_questions_too_few": (
        "Too few proposed buyer questions passed review to measure AI visibility."
    ),
    "evidence_too_large": "The answer and its sources exceeded the saved-evidence size limit.",
    "response_invalid": "The response did not match the expected structure.",
    "judgment_invalid": "The grading response did not match the required structure.",
    "mention_quote_invalid": "The mention grade lacked an exact quote naming the target.",
    "shortlist_quote_invalid": "The recommendation grade lacked an exact quote naming the target.",
    "first_choice_quote_invalid": "The first-choice grade lacked an exact quote naming the target.",
    "evaluation_quote_invalid": "The evaluation grade lacked an exact quote naming the target.",
    "judgment_inconsistent": "The mention and recommendation grades contradicted each other.",
    "invalid_answer_judgment": "The AI grade could not be verified against the saved answer.",
    "provider_result_unavailable": "The provider request outcome could not be confirmed.",
    "unconfirmed_previous_request": "An earlier request was not confirmed and was not repeated.",
    "spending_limit": "The spending limit prevented this request.",
    "model_not_configured": "The model provider was not configured.",
    "classification_exceeded_evidence_budget": (
        "The scored result exceeded the evidence size limit."
    ),
    "public_identity_or_panel_not_validated": (
        "The website identity or buyer panel was not validated."
    ),
    "not_recorded": "No scored observation was recorded.",
}

MARKETS = {"US": 2840, "GB": 2826, "CA": 2124, "AU": 2036}
ARTIFACT_LIMITS = {"AUDIT.md": 150_000, "findings.json": 500_000, "evidence.json": 3_000_000}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def audit_paths(run_id: str) -> dict[str, str]:
    run_id = str(UUID(run_id))
    return {name: f"reports/organic-audit/{run_id}/{name}" for name in ARTIFACT_LIMITS}


# Audit policy v12's summary, for code workflows. They read project files of at most 64,000
# bytes (code_project_files.MAX_FILE_BYTES); the pinned budget keeps each copy below that.
# LATEST.json is the newest published audit's SUMMARY.json, byte for byte, at a path a reader
# can find without listing runs. Each v12 publication replaces it; every other path is new.
SUMMARY_READ_LIMIT = 64_000
LATEST_SUMMARY_PATH = "reports/organic-audit/LATEST.json"


def summary_paths(run_id: str) -> dict[str, str]:
    run_id = str(UUID(run_id))
    return {
        "SUMMARY.json": f"reports/organic-audit/{run_id}/SUMMARY.json",
        "LATEST.json": LATEST_SUMMARY_PATH,
    }


def bundle_sha256(run_id: str, documents: dict[str, str]) -> str:
    """The publish receipt's `documents_sha256`: AUDIT.md, findings.json and evidence.json.

    Downstream workflows verify an audit by reading exactly those three files back, so the
    v12 summary files stay outside it; LATEST.json also changes with every later audit.
    """
    return digest({path: documents[path] for path in audit_paths(run_id).values()})


def publication_contract(run_id: str, policy: dict) -> tuple[dict, dict, frozenset[str]]:
    """The run's paths, their byte limits and the paths it may replace, per its pinned policy."""
    paths, limits = audit_paths(run_id), dict(ARTIFACT_LIMITS)
    if not policy.get("summary_max_bytes"):
        return paths, limits, frozenset()
    paths |= summary_paths(run_id)
    limits |= {"SUMMARY.json": SUMMARY_READ_LIMIT, "LATEST.json": SUMMARY_READ_LIMIT}
    return paths, limits, frozenset({LATEST_SUMMARY_PATH})


def public_site(value: str) -> tuple[str, str]:
    """Require an explicit HTTPS origin, never a guessed or credential-bearing URL."""
    if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 33 for c in value):
        raise ValueError("Enter a public HTTPS website origin.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a public HTTPS origin without a path, query, or credentials.")
    host = parsed.hostname.encode("idna").decode().lower()
    if (
        len(host) > 253
        or host.endswith(".")
        or "." not in host
        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")
        )
    ):
        raise ValueError("The audit requires a public DNS hostname.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}/", host
    raise ValueError("Use the public site's DNS hostname, not an IP address.")


def in_scope_url(value: Any, host: str, *, aliases: tuple[str, ...] = ()) -> bool:
    if not isinstance(value, str) or len(value) > 2000:
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme in {"https", "http"}
            and url.hostname in (host, *aliases)
            and url.username is None
            and url.password is None
            and url.port in {None, 80, 443}
            and not url.fragment
        )
    except ValueError:
        return False


def audit_hosts(scope: dict) -> tuple[str, ...]:
    """Allow an exact www pair only from a validated, publication-bound redirect receipt."""
    host = scope["host"]
    if not audit_policy(scope.get("policy_version", "organic-audit-v1")).get(
        "verified_www_redirects"
    ):
        return (host,)
    peer = host[4:] if host.startswith("www.") else f"www.{host}"
    redirects = (scope.get("site_identity") or {}).get("redirects", [])
    if not isinstance(redirects, list) or len(redirects) > 5:
        raise ValueError("Invalid audit redirect evidence.")
    current = scope["url"]
    observed = {host}
    for redirect in redirects:
        if redirect["from"] != current or redirect["status_code"] not in {301, 302, 303, 307, 308}:
            raise ValueError("Invalid audit redirect evidence.")
        target = redirect["to"]
        parsed = urlsplit(target)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {host, peer}
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(target) > 2000
            or any(ord(c) < 33 for c in target)
        ):
            raise ValueError("Audit redirect left its verified site scope.")
        observed.add(parsed.hostname)
        current = target
    return (host, *sorted(observed - {host}))


# Flag, check ID, status, severity, observation, remedy. Deliberate exclusions
# are review items; absent fields never become either a pass or a failure.
CHECKS = (
    (
        "is_redirect",
        "http.redirect",
        "review",
        "low",
        "Page redirects",
        "Confirm the destination is intentional; do not remove a valid redirect.",
    ),
    (
        "no_title",
        "metadata.title_missing",
        "fail",
        "medium",
        "HTML title is missing",
        "Add an accurate, distinct title describing the page.",
    ),
    (
        "no_description",
        "metadata.description_missing",
        "review",
        "low",
        "Meta description is missing",
        "Consider a useful summary; this is not a ranking penalty.",
    ),
    (
        "duplicate_title",
        "metadata.title_duplicate",
        "review",
        "medium",
        "Duplicate title observed",
        "Check the affected pages' intent before changing a shared title template.",
    ),
    (
        "duplicate_description",
        "metadata.description_duplicate",
        "review",
        "low",
        "Duplicate description observed",
        "Distinguish pages where their purposes differ.",
    ),
    (
        "is_orphan_page",
        "discovery.possible_orphan",
        "review",
        "medium",
        "Provider flagged a possible orphan page",
        "Verify incoming internal links; a bounded crawl cannot establish a whole-site orphan.",
    ),
    (
        "canonical_to_broken",
        "canonical.broken",
        "fail",
        "high",
        "Canonical target is broken",
        "Verify the intended canonical target and make it reachable or correct the canonical.",
    ),
    (
        "canonical_to_redirect",
        "canonical.redirect",
        "review",
        "medium",
        "Canonical target redirects",
        "Check whether the final preferred URL should be canonical.",
    ),
    (
        "broken_links",
        "links.broken",
        "review",
        "high",
        "Page contains broken links",
        "Inspect the broken destinations and repair or remove links whose targets should exist.",
    ),
    (
        "redirect_chain",
        "http.redirect_chain",
        "review",
        "medium",
        "Redirect chain observed",
        "Verify the intended destination, then simplify avoidable intermediate redirects.",
    ),
    (
        "is_4xx_code",
        "http.client_error",
        "fail",
        "high",
        "HTTP 4xx observed",
        "Check access rules and intended page availability before restoring or redirecting.",
    ),
    (
        "is_5xx_code",
        "http.server_error",
        "fail",
        "high",
        "HTTP 5xx observed",
        "Investigate the server error and verify the public URL responds successfully.",
    ),
)


def normalize_pages(
    items: list[dict],
    host: str,
    *,
    aliases: tuple[str, ...] = (),
    policy_version: str = LEGACY_AUDIT_POLICY["version"],
) -> list[dict]:
    policy = audit_policy(policy_version)
    applicability = policy.get("check_applicability", False)
    if len(items) > policy["max_pages"]:
        raise ValueError("Provider page collection exceeded its pinned limit.")
    pages = []
    seen: set[str] = set()
    for item in items:
        url = item.get("url")
        if (
            not in_scope_url(url, host, aliases=aliases)
            or url in seen
            or item.get("resource_type") not in ({"html", "broken"} if applicability else {"html"})
        ):
            continue
        seen.add(url)
        meta = item.get("meta") or {}
        checks = dict(item.get("checks") or {})
        if applicability and type(item.get("status_code")) is int:
            checks["is_4xx_code"] = 400 <= item["status_code"] < 500
            checks["is_5xx_code"] = 500 <= item["status_code"] < 600
        for flag in ("duplicate_title", "duplicate_description", "broken_links"):
            if type(item.get(flag)) is bool:
                checks[flag] = item[flag]
        pages.append(
            {
                "url": url,
                "status_code": item.get("status_code"),
                "title": str(meta.get("title") or "")[:200],
                **(
                    {
                        "provider_context": {
                            "canonical": checks.get("canonical")
                            if type(checks.get("canonical")) is bool
                            else None,
                            "respect_sitemap": bool(
                                audit_policy(policy_version).get("respect_sitemap")
                            ),
                        }
                    }
                    if applicability
                    else {}
                ),
                "checks": {
                    flag: checks[flag] for flag, *_ in CHECKS if type(checks.get(flag)) is bool
                },
            }
        )
    pages = sorted(pages, key=lambda page: page["url"])
    if len(canonical_json(pages)) > policy.get("max_crawl_evidence_bytes", 240_000):
        raise ValueError("Normalized crawl exceeded its evidence budget.")
    return pages


def check_outcome(page: dict, flag: str) -> str:
    """Interpret provider preconditions before flags, including historical missing context."""
    context = page.get("provider_context", {})
    if flag in {"no_title", "no_description"}:
        canonical = context.get("canonical")
        if canonical is False:
            return "not_applicable"
        if canonical is not True:
            return "unknown"
    if flag == "is_orphan_page" and context.get("respect_sitemap") is not True:
        return "unknown"
    value = page.get("checks", {}).get(flag)
    return "problem" if value is True else "pass" if value is False else "unknown"


def technical_findings(
    pages: list[dict],
    host: str,
    *,
    policy_version: str = LEGACY_AUDIT_POLICY["version"],
) -> tuple[list[dict], list[dict]]:
    findings, coverage = [], []
    applicability = audit_policy(policy_version).get("check_applicability", False)
    for flag, check_id, status, severity, observation, remedy in CHECKS:
        if applicability:
            outcomes = [check_outcome(p, flag) for p in pages]
            counts = {
                name: outcomes.count(name)
                for name in ("problem", "pass", "not_applicable", "unknown")
            }
            observed = [
                p
                for p, outcome in zip(pages, outcomes, strict=True)
                if outcome in {"problem", "pass"}
            ]
            affected = [
                p["url"] for p, outcome in zip(pages, outcomes, strict=True) if outcome == "problem"
            ]
        else:
            counts = {}
            observed = [p for p in pages if flag in p["checks"]]
            affected = [p["url"] for p in observed if p["checks"][flag]]
        coverage.append(
            {
                "check_id": check_id,
                "observed_pages": len(observed),
                "status": (
                    "partial"
                    if counts.get("unknown") and observed
                    else "observed"
                    if observed
                    else "unknown"
                ),
                **({"outcomes": counts} if applicability else {}),
            }
        )
        if not affected:
            continue
        findings.append(
            {
                "id": f"oa_{digest([host, check_id])[:20]}",
                "check_id": check_id,
                "check_version": 1,
                "category": "technical",
                "status": status,
                "severity": severity,
                "confidence": "observed",
                "evidence_kind": "provider_crawl",
                "urls": affected[:10],
                "urls_capped": len(affected) > 10,
                "affected_url_evidence": {"collection": "crawl.pages", "flag": flag},
                "affected_count": len(affected),
                "observation": observation,
                "expected_behavior": remedy,
                "suggested_remedy": remedy,
                "ownership": "site_owner",
                "effort": "requires_inspection",
                "dependencies": [],
                "verification": {
                    "kind": "recrawl_and_inspect",
                    "check_id": check_id,
                },
                "next_action": "technical_fix",
                "evidence_refs": ["crawl.pages"],
            }
        )
        if audit_policy(policy_version).get("finding_format"):
            from tin_lite.organic_audit_format import TECHNICAL_FORMAT, evidence_lines

            area, priority = TECHNICAL_FORMAT[check_id]
            unknown = coverage[-1].get("outcomes", {}).get("unknown", 0)
            findings[-1].update(
                area=area,
                issue=observation,
                impact=severity,
                evidence=evidence_lines(
                    [
                        f"The provider crawl flagged {len(affected)} of {len(observed)} pages "
                        "it could check"
                        + (f"; {unknown} pages could not be checked." if unknown else "."),
                        "Examples: " + ", ".join(affected[:5]),
                    ]
                ),
                fix=remedy,
                priority=priority,
            )
    if audit_policy(policy_version).get("finding_format"):
        from tin_lite.organic_audit_format import order_key

        return sorted(findings, key=order_key), coverage
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda item: (order[item["severity"]], item["id"])), coverage


def content_review_findings(
    ai: dict,
    host: str,
    *,
    policy_version: str = AUDIT_POLICY["version"],
    aliases: tuple[str, ...] = (),
) -> list[dict]:
    """Measured absence can prioritize investigation, never prove a missing page."""
    if not ai.get("panel") or (
        audit_policy(policy_version) == LEGACY_AUDIT_POLICY and ai.get("status") != "completed"
    ):
        return []
    groups: dict[str, list[dict]] = {}
    for index, question in enumerate(ai["panel"]["questions"]):
        answers = [
            row
            for row in ai["observations"]
            if row.get("question_index") == index and row.get("mode") != "memory"
        ]
        if len(answers) != panel_repetitions(ai["panel"]) or any(
            row.get("status") != "completed" for row in answers
        ):
            continue
        if any(row["classification"]["owned_domain_cited"] for row in answers):
            continue
        groups.setdefault(question["job"], []).append(
            {
                "question": question["question"],
                "source_url": question["source_url"],
                "observation_indexes": [row["index"] for row in answers],
            }
        )
    findings = []
    for job, questions in groups.items():
        urls = sorted(
            {
                q["source_url"]
                for q in questions
                if in_scope_url(q["source_url"], host, aliases=aliases)
            }
        )
        check = "content.buyer_answer_coverage"
        findings.append(
            {
                "id": f"oa_{digest([host, check, job])[:20]}",
                "check_id": check,
                "check_version": 1,
                "category": "content",
                "status": "review",
                "severity": "medium",
                "confidence": "hypothesis",
                "evidence_kind": "sampled_ai_answers",
                "urls": urls,
                "affected_count": len(questions),
                "observation": f"Review buyer-answer coverage: {job}",
                "expected_behavior": (
                    "Buyers can find clear, accurate answers on the public website."
                ),
                "suggested_remedy": (
                    "The website was not cited in "
                    + ("either" if panel_repetitions(ai["panel"]) == 2 else "any")
                    + " sampled answer to these questions. "
                    "Inspect existing public answers before deciding whether to improve, "
                    "add, or link content. This is not proof of missing content or a promise "
                    "that a new page will gain citations."
                ),
                "ownership": "content_owner",
                "effort": "requires_inspection",
                "dependencies": [],
                "verification": {"kind": "inspect_existing_buyer_answers", "questions": questions},
                "next_action": "content_plan",
                "evidence_refs": [
                    f"ai_visibility.observations.{index}"
                    for q in questions
                    for index in q["observation_indexes"]
                ],
                **(
                    {"audit_coverage": "partial", "requires_complete_question_pair": True}
                    if ai.get("status") != "completed"
                    else {}
                ),
            }
        )
        if audit_policy(policy_version).get("finding_format"):
            from tin_lite.organic_audit_format import evidence_lines

            findings[-1].update(
                area="content",
                issue=f"AI answers to buyer questions about {job} do not cite your website",
                impact="medium",
                evidence=evidence_lines(
                    [
                        f"{len(questions)} buyer questions; the website was not cited in any "
                        "sampled answer to them.",
                        *(f'"{q["question"]}"' for q in questions[:4]),
                    ]
                ),
                fix=findings[-1]["suggested_remedy"],
                priority="long_term",
            )
    findings.extend(_cited_instead_findings(ai, host, policy_version=policy_version))
    return findings


def _cited_instead_findings(ai: dict, host: str, *, policy_version: str) -> list[dict]:
    """The sites AI answers cite when they do not cite yours: where to earn a mention."""
    domains = ai.get("cited_domains") or []
    ladder = ai.get("ladder") or {}
    if not domains or not audit_policy(policy_version).get("finding_format"):
        return []
    from tin_lite.organic_audit_format import count, site_finding

    scored = ladder.get("scored", 0)
    cited = sum(
        bool(row["classification"].get("owned_domain_cited"))
        for row in ai.get("observations", [])
        if row.get("status") == "completed"
    )
    if not scored or cited * 2 >= scored:
        return []
    return [
        site_finding(
            host=host,
            check_id="ai.cited_instead",
            category="content",
            area="authority",
            issue="AI answers to your buyer questions cite other sites",
            impact="medium",
            evidence=[
                f"Your website was cited in {cited} of {count(scored, 'searched answer')}.",
                *(
                    f"{row['domain']}: cited in {count(row['answers'], 'answer')}"
                    for row in domains[:8]
                ),
            ],
            fix=(
                "Earn a place on the sources assistants cite for these questions (directories, "
                "comparison articles, community threads) and publish pages that answer the same "
                "questions directly."
            ),
            priority="high_impact",
            evidence_kind="sampled_ai_answers",
            next_action="content_plan",
            ownership="content_owner",
            evidence_refs=["ai_visibility.cited_domains"],
        )
    ]


def ai_report_details(ai: dict) -> list[str]:
    """A bounded readable view of each frozen question, including honest gaps."""
    panel = ai.get("panel")
    if not panel:
        reason = (ai.get("preparation") or {}).get("reason") or (
            ai.get("public_research") or {}
        ).get("reason")
        return [
            "### Why AI visibility was not measured",
            "",
            AUDIT_GAP_REASONS.get(
                reason, AUDIT_GAP_REASONS["public_identity_or_panel_not_validated"]
            ),
            "No visibility score or content-coverage recommendation was inferred.",
            "",
        ]
    lines = [
        "### Buyer-question results",
        "",
        "Each row is one frozen question. Counts include only scored answers. "
        "Unknown answers are not counted as negatives; "
        "missing answers are not automatically replaced.",
        "",
        "| Question | Scored | Mentioned | Website cited | Shortlisted | Preferred first |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    observations = {row["index"]: row for row in ai.get("observations", [])}
    repetitions = panel_repetitions(panel)
    limited_search = sum(
        bool(
            {"failed", "searching", "in_progress"}.intersection(
                row.get("answer", {})
                .get("value", {})
                .get("diagnostics", {})
                .get("search_statuses", [])
            )
        )
        for row in observations.values()
    )
    gaps = []
    for index, question in enumerate(panel["questions"]):
        rows = [observations.get(index * repetitions + repeat, {}) for repeat in range(repetitions)]
        scored = [row for row in rows if row.get("status") == "completed"]
        label = re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", question["question"])
        label = " ".join(label.split())
        counts = [
            str(sum(row["classification"][key] for row in scored)) if scored else "Unknown"
            for key in ("mentioned", "owned_domain_cited", "shortlisted", "selected_first")
        ]
        lines.append(
            f"| Q{index + 1}. {label} | {len(scored)}/{repetitions} | " + " | ".join(counts) + " |"
        )
        for repeat, row in enumerate(rows, 1):
            if row.get("status") == "completed":
                continue
            reason = row.get("reason") or row.get("answer", {}).get("reason")
            explanation = AUDIT_GAP_REASONS.get(reason, AUDIT_GAP_REASONS["not_recorded"])
            stage = "Grading" if row.get("failure_stage") == "grading" else "Observation"
            gaps.append(f"- Q{index + 1}, answer {repeat} — {stage}: {explanation}")
    lines.extend(
        ["", "Full saved answers and source links are in the adjacent `evidence.json`.", ""]
    )
    if limited_search:
        lines.extend(
            [
                f"{limited_search} answers had completed web research and an extra search "
                "attempt that did not complete. "
                "Attempt statuses remain in the evidence; these are not missing answers.",
                "",
            ]
        )
    if gaps:
        lines.extend(["### Missing evidence", "", *gaps, ""])
    dropped = (panel or {}).get("dropped_questions") or []
    if dropped:
        lines.extend(
            [
                "### Questions dropped in review",
                "",
                "The reviewer rejected these before any answer was requested; the rest were asked.",
                "",
                *(
                    "- " + " ".join(re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text).split())
                    for text in (f"{row['question']} ({row['reason']})" for row in dropped)
                ),
                "",
            ]
        )
    lines.extend(ladder_report_lines(ai))
    return lines


def ladder_report_lines(ai: dict) -> list[str]:
    """The recommendation ladder, answers without web search, and the sites answers cite."""
    ladder = ai.get("ladder")
    if not ladder:
        return []
    labels = {
        "found": "Found (named, or its site read or cited)",
        "mentioned": "Mentioned in the answer",
        "evaluated": "Evaluated against the buyer's needs",
        "shortlisted": "Recommended",
        "selected_first": "Picked first",
    }
    lines = [
        "### Recommendation ladder",
        "",
        f"Out of {ladder['scored']} scored answers with web search, the same ladder as the AI "
        "visibility audit:",
        "",
        "| Stage | Answers |",
        "| --- | ---: |",
        *(f"| {labels[key]} | {ladder['counts'][key]} |" for key in labels),
        "",
        f"Main break: {ladder['bottleneck']['label']}. {ladder['bottleneck']['why']}",
        "",
    ]
    memory = ai.get("memory") or {}
    if memory.get("planned"):
        lines.extend(
            [
                "### Answers without web search",
                "",
                f"One answer per question from the model's own knowledge: {memory['completed']} "
                f"of {memory['planned']} scored; the target was mentioned in "
                f"{memory['mentioned']} and recommended in {memory['shortlisted']}. This shows "
                "what the model knows before it searches.",
                "",
            ]
        )
    domains = ai.get("cited_domains") or []
    if domains:
        lines.extend(
            [
                "### Sites AI answers cite",
                "",
                "The sites cited most often in the searched answers, other than yours:",
                "",
                *(f"- {row['domain']}: {row['answers']} answers" for row in domains),
                "",
            ]
        )
    return lines


def search_console_pages(raw: dict, host: str, *, aliases=()) -> dict:
    """Bounded appearance evidence, never an indexing verdict for absent URLs."""
    import math

    rows = raw.get("rows", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Search Console exceeded its row contract")
    result = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("keys"), list)
            or len(row["keys"]) != 1
        ):
            raise ValueError("Invalid Search Console page row")
        url = row["keys"][0]
        if not in_scope_url(url, host, aliases=aliases):
            continue
        counts = {}
        for key in ("clicks", "impressions"):
            value = row.get(key)
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid Search Console metric")
            counts[key] = value
        if counts["clicks"] > counts["impressions"]:
            raise ValueError("Search Console clicks exceed impressions")
        result.append({"url": url, **counts})
    return {
        "pages": result,
        "returned_rows": len(rows),
        "note": (
            "Top page rows for the selected property and dates; "
            "omitted pages are not proven unindexed. Results are not market-filtered."
        ),
    }


def build_documents(
    *,
    run_id: str,
    project_id: str,
    definition_sha: str,
    scope: dict,
    crawl: dict,
    ai: dict,
    spending: dict,
    policy_version: str = AUDIT_POLICY["version"],
    search_console: dict | None = None,
    search_queries: dict | None = None,
    site: dict | None = None,
    search_previous: dict | None = None,
) -> dict[str, bytes]:
    policy = audit_policy(policy_version)
    if policy.get("site_checks"):
        return site_check_documents(
            run_id=run_id,
            project_id=project_id,
            definition_sha=definition_sha,
            scope=scope,
            crawl=crawl,
            ai=ai,
            spending=spending,
            policy=policy,
            search_console=search_console,
            search_queries=search_queries,
            site=site or {},
            search_previous=search_previous,
        )
    modern = policy != LEGACY_AUDIT_POLICY
    hosts = audit_hosts(scope)
    pages = crawl.get("pages", [])
    findings, coverage = technical_findings(pages, scope["host"], policy_version=policy_version)
    findings.extend(
        content_review_findings(ai, scope["host"], policy_version=policy_version, aliases=hosts)
    )
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "definition_commit_sha": definition_sha,
        "policy": policy,
        "scope": scope,
        "crawl": crawl,
        "ai_visibility": ai,
        "spending": spending,
    }
    if policy.get("check_applicability"):
        evidence["search_console"] = search_console or {"status": "not_available"}
    inventory = {
        "schema_version": 1,
        "run_id": run_id,
        "target_host": scope["host"],
        "evidence_sha256": digest(evidence),
        "findings": findings,
        "check_coverage": coverage,
        "downstream_authority": "recommendations_only",
    }
    if policy.get("check_applicability"):
        inventory["schema_version"] = 2
        inventory["evidence_status"] = (
            "partial"
            if not pages or any(c["outcomes"]["unknown"] for c in coverage)
            else "complete"
        )
    page_unit = "pages" if policy.get("check_applicability") else "HTML pages"
    complete = crawl.get("status") == "completed" and ai.get("status") == "completed"
    if policy.get("check_applicability"):
        complete = complete and inventory["evidence_status"] == "complete"
    lines = [
        "# Organic visibility audit",
        "",
        f"Website: {scope['url']}",
        f"Market: {scope['market']} · English · Observed: {scope['started_at']}",
        "",
        "## In plain English",
        "",
        "This is a read-only, sampled audit. Nothing on your website changed.",
        f"Result: {'completed within the stated scope' if complete else 'partial evidence'}. "
        f"Inspected {len(pages)} {page_unit} (limit {policy['max_pages']}).",
        "",
        "## What to tackle first",
        "",
    ]
    completion = scope.get("completion")
    if completion:
        lines[10:10] = [
            f"Completion: retained the original crawl and {completion['retained_observations']} "
            "scored answers; explicitly retried the one missing answer. "
            f"The original partial audit `{completion['source_run_id']}` remains unchanged. "
            f"Completion requested: {completion['requested_at']}. "
            "This is a completed evidence set, not a claim that every original request succeeded.",
            "",
        ]
    if not findings and (pages or not policy.get("verified_www_redirects")):
        lines.append(
            "No supported technical findings were observed in the available pages. "
            "This is not a clean bill of health for the whole website."
        )
        if modern:
            lines.append("")
    if policy.get("verified_www_redirects"):
        if not pages:
            lines.extend(
                ["Technical SEO was not measured: no in-scope HTML pages were retained.", ""]
            )
        if len(hosts) > 1:
            lines.extend(
                [f"The verified website redirect also includes `{hosts[1]}` in this audit.", ""]
            )
    for item in findings:
        lines.extend(
            [
                f"### {item['observation']} ({item['severity']}; {item['status']})",
                "",
                f"{item['affected_count']} "
                f"{'buyer questions' if item['category'] == 'content' else 'inspected URLs'}. "
                f"{item['suggested_remedy']}",
                "",
                f"Finding ID: `{item['id']}`. Examples and verification: `findings.json`; "
                "full affected-page evidence: `evidence.json`.",
                "",
            ]
        )
    if policy.get("verified_www_redirects"):
        lines.extend(
            [
                "## Technical SEO",
                "",
                f"{len(pages)} {page_unit} inspected across {len(CHECKS)} supported checks.",
                "",
            ]
        )
        if pages and not policy.get("check_applicability"):
            lines.extend(["| Check | Pages checked | Pages flagged |", "| --- | ---: | ---: |"])
            for flag, check_id, _, _, label, _ in CHECKS:
                checked = next(
                    row["observed_pages"] for row in coverage if row["check_id"] == check_id
                )
                flagged = sum(page["checks"].get(flag) is True for page in pages)
                lines.append(f"| {label} | {checked} | {flagged if checked else 'Not measured'} |")
            lines.extend(
                [
                    "",
                    "Flags are review candidates, not an SEO score; "
                    "unobserved checks are not passes.",
                    "",
                ]
            )
        if pages and policy.get("check_applicability"):
            lines.extend(
                [
                    "| Check | Problem | No problem | Not applicable | Unknown |",
                    "| --- | ---: | ---: | ---: | ---: |",
                ]
            )
            for row in coverage:
                counts = row["outcomes"]
                lines.append(
                    f"| {row['check_id']} | "
                    + " | ".join(
                        str(counts[key]) for key in ("problem", "pass", "not_applicable", "unknown")
                    )
                    + " |"
                )
            lines.extend(
                [
                    "",
                    "Counts describe provider checks that applied to these pages. "
                    "Unknown results need evidence; a noncanonical page is not a metadata defect. "
                    "Orphan checks require recorded sitemap discovery; "
                    "missing historical context remains unknown.",
                    "",
                ]
            )
        collection = crawl.get("collection")
        if collection and collection["excluded_html_pages"]:
            lines.extend(
                [
                    f"The provider returned {collection['provider_html_pages']} HTML pages; "
                    f"{collection['excluded_html_pages']} were excluded as outside the verified "
                    "scope, duplicates, or unsupported records.",
                    "",
                ]
            )
    if policy.get("check_applicability"):
        gsc = evidence["search_console"]
        lines.extend(["## Search appearance", ""])
        if gsc.get("status") == "completed":
            value = gsc["value"]
            lines.extend(
                [
                    f"Search Console: {value['start_date']} through {value['end_date']}. "
                    + value["note"],
                    "",
                    "| Page | Clicks | Impressions |",
                    "| --- | ---: | ---: |",
                ]
            )
            for page in value["pages"][:20]:
                lines.append(
                    f"| {page['url'].replace('|', '%7C')} | "
                    f"{page['clicks']} | {page['impressions']} |"
                )
            lines.append("")
        else:
            lines.extend(
                [
                    "No matching Search Console evidence was collected. "
                    "Crawl observations alone do not establish search appearance.",
                    "",
                ]
            )
    lines.extend(
        [
            "## AI visibility",
            "",
            ai.get("summary", "Not measured."),
            "",
            *(ai_report_details(ai) if modern else []),
            "Branded fact-check answers are separate observations in the evidence. "
            "They do not count toward the unbranded baseline or certify factual accuracy.",
            "",
            "## Evidence and limits",
            "",
            f"Crawl: {crawl.get('status', 'unknown')}. {crawl.get('note', '')}",
            "",
            "A provider check is an observation, not proof of indexing or ranking impact. "
            "Unobserved checks are unknown, not passes. "
            "Lab diagnostics are not field Core Web Vitals.",
            "",
            (
                "This version does not measure field performance, private product analytics, "
                "JavaScript-rendered pages, backlinks, or other AI engines."
                if policy.get("check_applicability")
                else "This version does not measure field performance, private analytics, "
                "Search Console "
                "data, backlinks, or other AI engines. It does not claim a whole-site certificate."
            ),
            "",
            "## For the next workflow",
            "",
            "Use this run's immutable artifact revision, the findings digest, "
            "and selected finding IDs. "
            "Recheck the affected URLs and repository ownership before proposing a fix. "
            "This report grants no authority to edit, publish, or contact anyone.",
            "",
            f"Findings SHA-256: `{digest(inventory)}`",
            "",
            "The adjacent `findings.json` and `evidence.json` contain bounded, "
            "machine-readable evidence.",
        ]
    )
    paths = audit_paths(run_id)
    documents = {
        paths["AUDIT.md"]: ("\n".join(lines) + "\n").encode(),
        paths["findings.json"]: canonical_json(inventory),
        paths["evidence.json"]: canonical_json(evidence),
    }
    for name, limit in ARTIFACT_LIMITS.items():
        if name == "evidence.json":
            limit = policy.get("max_evidence_bytes", 900_000)
        if not 0 < len(documents[paths[name]]) <= limit:
            raise ValueError(f"Audit {name} exceeded its bounded artifact contract.")
    return documents


def _fit_evidence(evidence: dict, limit: int) -> dict:
    """Keep evidence inside its artifact bound by dropping the least useful detail first.

    Findings are computed before this step; dropped rows are counted, never silently lost.
    """
    steps = (
        ("internal_links", None),
        ("search_console_queries", 1000),
        ("site_pages", None),
        ("sitemap_urls", 1000),
        ("search_console_queries", 200),
    )
    for step, keep in steps:
        if len(canonical_json(evidence)) <= limit:
            break
        if step == "internal_links":
            # v12 page links; SUMMARY.json already holds the inbound counts and click depth
            # computed from them. Pages before v12 carry none, and this step records nothing.
            pages = evidence["site"].get("pages", [])
            listed = sum("internal_links" in row for row in pages)
            if listed:
                evidence.setdefault("trimmed_for_size", {})[step] = listed
                evidence["site"]["pages"] = [
                    {key: value for key, value in row.items() if key != "internal_links"}
                    for row in pages
                ]
            continue
        trimmed = evidence.setdefault("trimmed_for_size", {})
        if step == "search_console_queries":
            value = (evidence.get("search_console_queries") or {}).get("value")
            if value and len(value["queries"]) > keep:
                trimmed[step] = trimmed.get(step, 0) + len(value["queries"]) - keep
                value["queries"] = value["queries"][:keep]
        elif step == "site_pages":
            pages = evidence["site"].get("pages", [])
            kept = [row for row in pages if row.get("fetch") == "observed"]
            trimmed[step] = len(pages) - len(kept)
            evidence["site"]["pages"] = kept
        else:
            sitemaps = (evidence["site"].get("files") or {}).get("sitemaps") or {}
            if len(sitemaps.get("urls", [])) > keep:
                trimmed[step] = len(sitemaps["urls"]) - keep
                sitemaps["urls"] = sitemaps["urls"][:keep]
    return evidence


def site_check_documents(
    *,
    run_id: str,
    project_id: str,
    definition_sha: str,
    scope: dict,
    crawl: dict,
    ai: dict,
    spending: dict,
    policy: dict,
    search_console: dict | None,
    search_queries: dict | None,
    site: dict,
    search_previous: dict | None = None,
) -> dict[str, bytes]:
    """organic-audit-v10: coverage-honest report with site, search and crawl findings."""
    import copy

    from tin_lite.organic_audit_format import PRIORITIES, order_key, urgency_key
    from tin_lite.organic_audit_report import analyze, report_lines

    hosts = audit_hosts(scope)
    host = scope["host"]
    pages = crawl.get("pages", [])
    technical, coverage_rows = technical_findings(pages, host, policy_version=policy["version"])
    content = content_review_findings(ai, host, policy_version=policy["version"], aliases=hosts)
    analysis = analyze(
        scope=scope,
        hosts=hosts,
        crawl=crawl,
        ai=ai,
        policy=policy,
        search_console=search_console,
        search_queries=search_queries,
        site=site,
        search_previous=search_previous,
    )
    findings = sorted([*technical, *content, *analysis["findings"]], key=order_key)
    if policy.get("next_action_from_repair_plan"):
        from tin_lite.technical_repair_plan import next_action

        findings = [{**f, "next_action": next_action(f["check_id"])} for f in findings]
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "definition_commit_sha": definition_sha,
        "policy": policy,
        "scope": scope,
        "crawl": crawl,
        "ai_visibility": ai,
        "spending": spending,
        "search_console": search_console or {"status": "not_available"},
        "search_console_queries": copy.deepcopy(search_queries) or {"status": "not_available"},
        **(
            {
                "search_console_previous": copy.deepcopy(search_previous)
                or {"status": "not_available"}
            }
            if policy.get("decay_min_previous_clicks")
            else {}
        ),
        "site": {
            "status": "observed" if site else "not_collected",
            "files": copy.deepcopy(site.get("files")),
            "plan": site.get("plan"),
            "pages": sorted(site.get("pages", []), key=lambda row: row["url"]),
            "pages_status": site.get("pages_status", "not_collected"),
            "pagespeed": analysis["pagespeed"],
            # v11's added evidence; a v10 evidence file keeps v10's shape.
            **(
                {
                    "access": site.get("access") or {"status": "not_collected"},
                    "url_inspection": site.get("url_inspection") or {"status": "not_collected"},
                    "content_review": site.get("content_review") or {"status": "not_collected"},
                }
                if policy.get("site_angles")
                else {}
            ),
        },
        "coverage": analysis["coverage"],
    }
    evidence = _fit_evidence(evidence, policy["max_evidence_bytes"])
    technical_status = (
        "partial"
        if not pages or any(row["outcomes"]["unknown"] for row in coverage_rows)
        else "complete"
    )
    cover = analysis["coverage"]
    urgent = sorted(findings, key=urgency_key)
    inventory = {
        "schema_version": 3,
        "run_id": run_id,
        "target_host": host,
        "evidence_sha256": digest(evidence),
        "finding_format": policy["finding_format"],
        "findings": findings,
        "check_coverage": coverage_rows,
        "evidence_status": technical_status,
        "coverage_status": cover["status"],
        "coverage": {
            key: cover[key]
            for key in (
                "status",
                "site_collected",
                "sitemap_read",
                "sitemap_pages",
                "inspected_sitemap_pages",
                "inspected_pages",
                "page_cap",
                "skipped_sitemap_pages",
            )
        },
        "site_check_coverage": analysis["site_check_coverage"],
        "summary": {
            "by_priority": {
                name: sum(f["priority"] == name for f in findings) for name in PRIORITIES
            },
            "top_issue_ids": [f["id"] for f in urgent[:5]],
            "quick_win_ids": [f["id"] for f in urgent if f["priority"] == "quick_win"][:10],
        },
        "downstream_authority": "recommendations_only",
    }
    complete = (
        crawl.get("status") == "completed"
        and ai.get("status") == "completed"
        and technical_status == "complete"
        and cover["status"] == "complete"
    )
    paths, limits, _ = publication_contract(run_id, policy)
    summary = None
    if policy.get("summary_max_bytes"):
        from tin_lite.organic_audit_summary import summary_document

        summary = summary_document(
            run_id=run_id,
            scope=scope,
            hosts=hosts,
            policy=policy,
            view=analysis["view"],
            crawl_pages=pages,
            cover=cover,
            findings=findings,
            top_issue_ids=inventory["summary"]["top_issue_ids"],
            ai=ai,
            paths=paths,
            findings_sha256=digest(inventory),
            evidence_sha256=inventory["evidence_sha256"],
        )
    for evidence_limit in (8, 3, 1):
        lines = report_lines(
            scope=scope,
            crawl=crawl,
            ai=ai,
            findings=findings,
            technical_coverage=coverage_rows,
            analysis=analysis,
            search_console=search_console,
            search_queries=search_queries,
            site=site,
            complete=complete,
            ai_details=ai_report_details(ai),
            evidence_limit=evidence_limit,
        )
        lines.extend(
            [
                "## For the next workflow",
                "",
                "Use this run's immutable artifact revision, the findings digest, "
                "and selected finding IDs. "
                "Recheck the affected URLs and repository ownership before proposing a fix. "
                "This report grants no authority to edit, publish, or contact anyone.",
                "",
                f"Findings SHA-256: `{digest(inventory)}`",
                "",
                "The adjacent `findings.json` and `evidence.json` contain bounded, "
                "machine-readable evidence.",
                *(
                    [
                        "",
                        "Code workflows read files of at most 64,000 bytes. The adjacent "
                        "`SUMMARY.json`, copied to `reports/organic-audit/LATEST.json`, fits: "
                        "one row per crawled page with its status, indexability, inbound "
                        "links and click depth, finding counts by check and the AI "
                        "visibility headline.",
                    ]
                    if summary is not None
                    else []
                ),
            ]
        )
        report = ("\n".join(lines) + "\n").encode()
        if len(report) <= ARTIFACT_LIMITS["AUDIT.md"]:
            break
    documents = {
        paths["AUDIT.md"]: report,
        paths["findings.json"]: canonical_json(inventory),
        paths["evidence.json"]: canonical_json(evidence),
    }
    if summary is not None:
        documents[paths["SUMMARY.json"]] = documents[paths["LATEST.json"]] = summary
    for name, limit in limits.items():
        if name == "evidence.json":
            limit = policy["max_evidence_bytes"]
        if not 0 < len(documents[paths[name]]) <= limit:
            raise ValueError(f"Audit {name} exceeded its bounded artifact contract.")
    return documents
