"""One finding format for organic-audit-v10: Issue, Impact, Evidence, Fix and Priority.

Findings keep their earlier machine fields (id, check_id, status, severity, urls, ...) so
downstream workflows read them unchanged, and add the five report fields plus an area.
Order follows the audit's priority order: crawlability and indexation, technical
foundations, on-page, content, authority.
"""

from __future__ import annotations

import hashlib
import json

AREAS = {
    "crawlability_indexation": "Crawlability and indexation",
    "technical_foundations": "Technical foundations",
    "on_page": "On-page",
    "content": "Content",
    "authority": "Authority",
}
PRIORITIES = {
    "critical": "Critical",
    "high_impact": "High impact",
    "quick_win": "Quick win",
    "long_term": "Long term",
}
IMPACTS = ("high", "medium", "low")
MAX_EVIDENCE_LINES = 12
MAX_EVIDENCE_CHARS = 400

# Provider crawl checks mapped onto the format: area and priority tier.
TECHNICAL_FORMAT = {
    "http.redirect": ("technical_foundations", "long_term"),
    "metadata.title_missing": ("on_page", "quick_win"),
    "metadata.description_missing": ("on_page", "quick_win"),
    "metadata.title_duplicate": ("on_page", "quick_win"),
    "metadata.description_duplicate": ("on_page", "long_term"),
    "discovery.possible_orphan": ("crawlability_indexation", "high_impact"),
    "canonical.broken": ("crawlability_indexation", "critical"),
    "canonical.redirect": ("crawlability_indexation", "quick_win"),
    "links.broken": ("technical_foundations", "high_impact"),
    "http.redirect_chain": ("technical_foundations", "quick_win"),
    "http.client_error": ("crawlability_indexation", "high_impact"),
    "http.server_error": ("crawlability_indexation", "critical"),
}


def count(number: float, noun: str, plural: str | None = None) -> str:
    shown = f"{number:,.0f}" if float(number).is_integer() else f"{number:,.1f}"
    return f"{shown} {noun if number == 1 else plural or noun + 's'}"


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def evidence_lines(lines) -> list[str]:
    result = []
    for line in lines:
        text = " ".join(str(line).split())
        if text:
            result.append(
                text if len(text) <= MAX_EVIDENCE_CHARS else text[: MAX_EVIDENCE_CHARS - 1] + "…"
            )
        if len(result) == MAX_EVIDENCE_LINES:
            break
    return result


def order_key(finding: dict) -> tuple:
    return (
        list(AREAS).index(finding["area"]),
        list(PRIORITIES).index(finding["priority"]),
        IMPACTS.index(finding["impact"]),
        finding["id"],
    )


def urgency_key(finding: dict) -> tuple:
    """For the summary's top issues: most urgent first, whatever the area."""
    return (
        list(PRIORITIES).index(finding["priority"]),
        IMPACTS.index(finding["impact"]),
        list(AREAS).index(finding["area"]),
        finding["id"],
    )


def site_finding(
    *,
    host: str,
    check_id: str,
    category: str,
    area: str,
    issue: str,
    impact: str,
    evidence,
    fix: str,
    priority: str,
    evidence_kind: str,
    urls=(),
    affected_count: int | None = None,
    status: str = "review",
    confidence: str = "observed",
    next_action: str = "review",
    ownership: str = "site_owner",
    evidence_refs=(),
    verification: dict | None = None,
) -> dict:
    if area not in AREAS or priority not in PRIORITIES or impact not in IMPACTS:
        raise ValueError("Unsupported finding format value.")
    urls = list(urls)
    return {
        "id": f"oa_{_digest([host, check_id])[:20]}",
        "check_id": check_id,
        "check_version": 1,
        "category": category,
        "area": area,
        "issue": issue,
        "impact": impact,
        "evidence": evidence_lines(evidence),
        "fix": fix,
        "priority": priority,
        "status": status,
        "severity": impact,
        "confidence": confidence,
        "evidence_kind": evidence_kind,
        "urls": urls[:10],
        "urls_capped": len(urls) > 10,
        "affected_count": len(urls) if affected_count is None else affected_count,
        "observation": issue,
        "expected_behavior": fix,
        "suggested_remedy": fix,
        "ownership": ownership,
        "effort": "requires_inspection",
        "dependencies": [],
        "verification": verification or {"kind": "recheck_evidence", "check_id": check_id},
        "next_action": next_action,
        "evidence_refs": list(evidence_refs),
    }
