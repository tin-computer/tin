"""Answer-structure review of the top content pages: one validated text-model call.

The model judges only what needs reading: whether the lead answers the page's main search,
whether sections stand alone, and whether the page carries specific facts. Dates, bylines,
sources and question headings come from the page facts Tin already measured.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from tin_lite.organic_audit_ai import CONTENT_GAPS, ContentReview
from tin_lite.organic_audit_site import (
    client_rendered,
    is_ad_landing_url,
    is_noindex,
    is_utility_url,
    url_key,
)

# Order in which gaps are reported: the model's judgments first, then measured facts.
GAP_ORDER = (*CONTENT_GAPS, "question_headings", "sources", "date", "author")
QUESTION_HEADINGS_MIN_WORDS = 300


def _path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def review_pages(
    *, facts: dict, search_pages: list[dict], queries: list[dict], host: str, cap: int
) -> list[dict]:
    """The pages with the most impressions that Tin read as content, at most `cap`."""
    impressions: dict[str, float] = {}
    for row in search_pages:
        key = url_key(row["url"])
        impressions[key] = impressions.get(key, 0) + row["impressions"]
    top_queries: dict[str, list[tuple[float, str]]] = {}
    for row in queries:
        top_queries.setdefault(url_key(row["url"]), []).append((row["impressions"], row["query"]))
    candidates = [
        record
        for record in facts.values()
        if record.get("fetch") == "observed"
        and 200 <= record.get("status_code", 0) < 300
        and urlsplit(record["url"]).hostname == host
        and url_key(record["url"]) != "/"
        and not is_noindex(record)
        and not client_rendered(record)
        and not is_utility_url(record["url"])
        and not is_ad_landing_url(record["url"])
        and record.get("text_words", 0) >= 100
    ]
    candidates.sort(key=lambda r: (-impressions.get(url_key(r["url"]), 0), r["url"]))
    pages = []
    for record in candidates[:cap]:
        key = url_key(record["url"])
        ranked = sorted(top_queries.get(key, []), key=lambda item: (-item[0], item[1]))
        pages.append(
            {
                "url": record["url"],
                "path": _path(record["url"]),
                "title": record.get("title") or "",
                "h1": record.get("h1_texts") or [],
                "headings": record.get("headings") or [],
                "lead": record.get("lead") or "",
                "words": record.get("text_words", 0),
                "queries": list(dict.fromkeys(query for _, query in ranked))[:3],
                "measured_gaps": measured_gaps(record),
            }
        )
    return pages


def measured_gaps(record: dict) -> list[str]:
    gaps = []
    if (
        record.get("text_words", 0) >= QUESTION_HEADINGS_MIN_WORDS
        and record.get("question_headings", 0) == 0
    ):
        gaps.append("question_headings")
    if record.get("external_links", 0) == 0:
        gaps.append("sources")
    if record.get("dated") is False:
        gaps.append("date")
    if record.get("author") is False:
        gaps.append("author")
    return gaps


def model_input(website: str, pages: list[dict]) -> dict:
    return {
        "website": website,
        "pages": [
            {key: page[key] for key in ("path", "title", "h1", "headings", "lead", "queries")}
            for page in pages
        ],
    }


def validate_review(text: str, pages: list[dict]) -> list[dict]:
    """One entry per supplied page; a claimed direct answer must quote the page's own lead."""
    review = ContentReview.model_validate_json(text)
    by_path = {page["path"]: page for page in pages}
    rows: dict[str, dict] = {}
    for item in review.pages:
        page = by_path.get(item.path)
        if page is None or item.path in rows:
            raise ValueError("The review named a page that was not supplied, or named it twice.")
        quote = item.answer_quote.strip()
        if "direct_answer" in item.gaps:
            if quote:
                raise ValueError("A page without a direct answer cannot quote one.")
        elif not quote or quote not in page["lead"]:
            raise ValueError("A direct answer must be quoted exactly from the page's lead.")
        gaps = set(item.gaps) | set(page["measured_gaps"])
        rows[item.path] = {
            "url": page["url"],
            "path": item.path,
            "query": page["queries"][0] if page["queries"] else None,
            "gaps": [gap for gap in GAP_ORDER if gap in gaps],
            "answer_quote": quote,
        }
    if set(rows) != set(by_path):
        raise ValueError("The review left out a supplied page.")
    return [rows[page["path"]] for page in pages]
