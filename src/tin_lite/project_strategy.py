"""The project's ICP and go-to-market strategy, `strategy/ICP.md` in project Files.

One section per ICP (company, buyer, disqualifiers, problem statement, job to be done, pains,
triggers, fit signals, outcomes, objections, next step) plus messaging themes. Workflows read it
as guidance about who the work is for, the way they read the brand guide, and never as evidence:
it states hypotheses about buyers, so nothing cites it as a source of facts.
"""

from __future__ import annotations

import re

STRATEGY_PATH = "strategy/ICP.md"
STRATEGY_LABEL = "ICP and go-to-market strategy (strategy/ICP.md)"
# 8 KB. A larger file is skipped with a note rather than cut: half a strategy reads as another.
STRATEGY_FILE_BYTES = 8_192
BRAND_PATH = "brand/BRAND.md"
BRAND_DIRECTION = "## Brand direction"
BUYER_CONTEXT_MIN = 20
BUYER_CONTEXT_MAX = 2_000

# The parts of an ICP section that say who buys and why, in the order they are kept.
_FIELDS = (
    ("buyer", re.compile(r"^(?:buyer|buyers|who buys|persona|decision maker)\b", re.I)),
    ("problem", re.compile(r"^(?:problem(?: statement)?|core problem)\b", re.I)),
    ("company", re.compile(r"^(?:company|companies|firmographics?|account)\b", re.I)),
    ("job", re.compile(r"^(?:job to be done|jtbd|job)\b", re.I)),
)
_FIELD_NAMES = {"buyer": "Buyer", "problem": "Problem", "company": "Company", "job": "Job"}
_LABEL = re.compile(
    r"^(?:#{3,6}\s+(?P<heading>.+?)\s*#*$|[-*+]?\s*(?:\*\*|__)?(?P<label>[A-Za-z][\w /()&-]{1,40}?)"
    r"(?:\*\*|__)?\s*[:：]\s*(?:\*\*|__)?\s*(?P<rest>.*))$"
)
_SKIP_SECTION = re.compile(r"messag|theme|overview|summary|contents|how to use", re.I)


def strategy_skipped_note(size: int) -> str:
    return (
        f"{STRATEGY_PATH} is {size:,} bytes, over its 8 KB limit, so this run did not read it. "
        "Shorten it to use it."
    )


def _plain(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#|]+", " ", text)
    return " ".join(text.split())


def _sentence(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a sentence or word end."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("; "))
    if cut > limit // 2:
        return head[: cut + 1]
    return head.rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _sections(text: str) -> list[tuple[str, list[str]]]:
    sections, name, lines = [], None, []
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*#*$", line)
        if heading:
            if name is not None:
                sections.append((name, lines))
            name, lines = _plain(heading.group(1)), []
        elif name is not None:
            lines.append(line)
    if name is not None:
        sections.append((name, lines))
    return [(n, body) for n, body in sections if not _SKIP_SECTION.search(n)]


def _fields(lines: list[str]) -> dict[str, str]:
    found: dict[str, list[str]] = {}
    current = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match = _LABEL.match(line)
        label = match and (match.group("heading") or match.group("label"))
        if label:
            current = next((key for key, pattern in _FIELDS if pattern.match(_plain(label))), None)
            if current is not None and current not in found:
                found[current] = [match.group("rest") or ""]
            elif current is not None:
                current = None  # A repeated label keeps its first value.
            continue
        if current is not None:
            found[current].append(line)
    return {key: _plain(" ".join(parts)) for key, parts in found.items() if _plain(" ".join(parts))}


def buyer_context_from_strategy(text: str) -> str:
    """The ICP names, buyers and problem statements (then company and job when room is left),
    at most 2,000 characters. Falls back to the file's own text when it has no ICP sections."""
    parts = []
    for name, lines in _sections(text):
        fields = _fields(lines)
        if not fields:
            continue
        line = f"{name}: " + " ".join(
            f"{_FIELD_NAMES[key]}: {_sentence(fields[key], 280).rstrip('.')}."
            for key, _ in _FIELDS
            if key in fields
        )
        parts.append(line)
    if not parts:
        return _sentence(_plain(text), BUYER_CONTEXT_MAX)
    # Every ICP keeps its name, buyer and problem before any ICP keeps its company or job.
    joined = " ".join(parts)
    if len(joined) <= BUYER_CONTEXT_MAX:
        return joined
    share = max(120, BUYER_CONTEXT_MAX // len(parts) - 1)
    return _sentence(" ".join(_sentence(part, share) for part in parts), BUYER_CONTEXT_MAX)


def brand_direction(text: str) -> str:
    """The `## Brand direction` section of brand/BRAND.md, as plain text."""
    match = re.search(rf"(?ms)^{re.escape(BRAND_DIRECTION)}\s*$(.*?)(?=^## |\Z)", text or "")
    return _plain(match.group(1)) if match else ""


def derive_buyer_context(strategy: bytes | None, brand: bytes | None) -> tuple[str, str]:
    """Buyer context and where it came from: strategy/ICP.md, else the brand guide's direction.

    Raises ValueError naming both files when neither gives at least 20 characters."""
    if strategy and len(strategy) <= STRATEGY_FILE_BYTES:
        derived = buyer_context_from_strategy(strategy.decode("utf-8", errors="ignore"))
        if len(derived) >= BUYER_CONTEXT_MIN:
            return derived, STRATEGY_PATH
    if brand:
        derived = _sentence(brand_direction(brand.decode("utf-8", errors="ignore")), 2_000)
        if len(derived) >= BUYER_CONTEXT_MIN:
            return derived, f"{BRAND_PATH} ({BRAND_DIRECTION.removeprefix('## ')})"
    oversized = (
        f" {STRATEGY_PATH} is over 8 KB, so it was not read."
        if strategy and len(strategy) > STRATEGY_FILE_BYTES
        else ""
    )
    raise ValueError(
        "Keyword research needs to know who buys. Add the ICP and go-to-market strategy at "
        f"{STRATEGY_PATH}, or a '{BRAND_DIRECTION}' section to {BRAND_PATH}, or fill in "
        f"Product and buyers.{oversized}"
    )
