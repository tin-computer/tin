"""Page changes other workflows planned: a pure reader, with nothing wired to it yet.

Two Registry packages plan changes to a site's pages but edit nothing themselves:

- `organic.content_efficacy` writes `content/efficacy.md`, whose JSON decisions block lists
  URL changes (a 301 into a stronger page, or noindex for a utility or ad page) and pages to
  refresh;
- `organic.site_architecture` writes a plan whose `redirects.json` block lists the redirects
  a URL change needs.

This module reads both into one list of URL changes, each with a stable `oa_` ID (the
audit's finding-ID shape) and Tin's suggestion. website.change phase 3 will turn them into
`website_changes` rows the founder approves; the founder dropped the technical fix workflow,
and site-fix-v5 (on main) must not change, so the technical fix does not read them. The
refresh rows (`refresh_candidates`) are kept for the one content.generate work; content.refresh
1.0.0 is on main and does not read them either.

Some paths are protected: the sign-in, sign-up and auth-return pages the site shares with
its login provider (an approved noindex on /sign-in once touched pages another app shares),
plus any path the caller's `protected_paths` names, as website.change's input does. A change
to or from a protected path is still listed, but Tin's suggestion is `ask`, never `apply`.

Pure parsing here; the callers read the files at a pinned project revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit

EFFICACY_PATH = "content/efficacy.md"
EFFICACY_SCHEMA = "content.efficacy/1"
ARCHITECTURE_PATH = "reports/organic/site-architecture/SITE_ARCHITECTURE.md"
ARCHITECTURE_SCHEMA = "site_architecture.redirects/1"
EFFICACY_SOURCE = "organic.content_efficacy"
ARCHITECTURE_SOURCE = "organic.site_architecture"
# A weekly decision older than this has been replaced or ignored; a plan lasts longer.
MAX_AGE_DAYS = {EFFICACY_SOURCE: 14, ARCHITECTURE_SOURCE: 60}
MAX_FILE_BYTES = 200_000
MAX_CHANGES = 20
PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,300}")
# Auth pages a site shares with its login provider; a change to them is always the founder's.
PROTECTED_PATHS = ("/sign-in", "/sign-up", "/auth-complete")
REFRESH_CHECKS = {
    "low_ctr": "search.low_ctr",
    "near_page_one": "search.near_page_one",
    "decline": "search.decay",
    "merge_survivor": "search.decay",
}


def _efficacy_block(text: str) -> dict | None:
    found = re.search(r"## Decisions block\s*```json\s*(\{.*?\})\s*```", text, re.S)
    try:
        block = json.loads(found.group(1)) if found else None
    except ValueError:
        return None
    return block if isinstance(block, dict) and block.get("schema") == EFFICACY_SCHEMA else None


def _architecture_block(text: str) -> dict | None:
    found = re.search(
        r"<!-- redirects\.json:start -->\s*```json\s*(\{.*?\})\s*```\s*"
        r"<!-- redirects\.json:end -->",
        text,
        re.S,
    )
    try:
        block = json.loads(found.group(1)) if found else None
    except ValueError:
        return None
    return block if isinstance(block, dict) and block.get("schema") == ARCHITECTURE_SCHEMA else None


def _fresh(block: dict, source: str, today: date) -> bool:
    try:
        made = date.fromisoformat(str(block.get("generated"))[:10])
    except ValueError:
        return False
    return 0 <= (today - made).days <= MAX_AGE_DAYS[source]


def _path(value) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith(("http://", "https://")):
        value = urlsplit(value).path or "/"
    return value if PATH.fullmatch(value) and ".." not in value else None


def protected(path: str | None, extra: list[str] | tuple[str, ...] = ()) -> bool:
    """Whether a site path is, or sits under, a protected path."""
    if not path:
        return False
    page = path.split("?")[0].rstrip("/") or "/"
    for root in (*PROTECTED_PATHS, *(p for p in extra if isinstance(p, str))):
        root = root.split("?")[0].rstrip("/") or "/"
        if root != "/" and (page == root or page.startswith(root + "/")):
            return True
    return False


def read_changes(
    files: dict[str, str | bytes | None], today: date, protected_paths: list[str] | None = None
) -> list[dict]:
    """Every current URL change:
    {source, kind: redirect|noindex, from, to, reason, confirmed, protected, id, suggestion}.

    `files` maps EFFICACY_PATH and ARCHITECTURE_PATH to their text (None when absent).
    Invalid, stale or oversized files contribute nothing; they never raise. `protected` is
    true when either end of the change is a protected path (PROTECTED_PATHS plus
    `protected_paths`).
    """
    extra = [p for p in protected_paths or [] if isinstance(p, str) and PATH.fullmatch(p)]
    changes: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(source, kind, old, new, reason, confirmed=False):
        old, new = _path(old), _path(new) if new else None
        if not old or (kind == "redirect" and (not new or new == old)):
            return
        if (kind, old) in seen or len(changes) >= MAX_CHANGES:
            return
        seen.add((kind, old))
        changes.append(
            {
                "source": source,
                "kind": kind,
                "from": old,
                "to": new if kind == "redirect" else None,
                "reason": str(reason or "")[:300],
                "confirmed": bool(confirmed),
                "protected": protected(old, extra) or protected(new, extra),
            }
        )
        change = changes[-1]
        change["id"] = finding_id(change)
        change["suggestion"] = "ask" if change["protected"] else "apply"

    for path, parse, source in (
        (ARCHITECTURE_PATH, _architecture_block, ARCHITECTURE_SOURCE),
        (EFFICACY_PATH, _efficacy_block, EFFICACY_SOURCE),
    ):
        raw = files.get(path)
        if raw is None or len(raw) > MAX_FILE_BYTES:
            continue
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        block = parse(text)
        if block is None or not _fresh(block, source, today):
            continue
        if source == ARCHITECTURE_SOURCE:
            for row in block.get("redirects") or []:
                if isinstance(row, dict) and row.get("status") in (301, 308):
                    add(source, "redirect", row.get("old"), row.get("new"), row.get("reason"), True)
        else:
            for row in block.get("url_changes") or []:
                if not isinstance(row, dict):
                    continue
                kind = {"301": "redirect", "noindex": "noindex"}.get(row.get("kind"))
                if kind:
                    add(
                        source,
                        kind,
                        row.get("from"),
                        row.get("to"),
                        row.get("reason"),
                        row.get("confirmed"),
                    )
    return changes


def finding_id(change: dict) -> str:
    """Stable across runs for the same proposal, in the audit's finding-ID shape."""
    key = "|".join(str(change.get(k) or "") for k in ("source", "kind", "from", "to"))
    return "oa_" + hashlib.sha256(key.encode()).hexdigest()[:20]


def refresh_candidates(raw: str | bytes | None, today: date) -> dict[str, set[str]]:
    """Site paths a page refresh may pick, with the audit-style checks each one stands for.

    No workflow reads this yet: content.refresh 1.0.0 is on main and keeps its own selection.
    """
    if raw is None or len(raw) > MAX_FILE_BYTES:
        return {}
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    block = _efficacy_block(text)
    if block is None or not _fresh(block, EFFICACY_SOURCE, today):
        return {}
    found: dict[str, set[str]] = {}
    for row in block.get("decisions") or []:
        if not isinstance(row, dict) or row.get("decision") != "refresh":
            continue
        check, path = REFRESH_CHECKS.get(row.get("rule")), _path(row.get("url"))
        if check and path:
            found.setdefault(path.rstrip("/") or "/", set()).add(check)
    return found
