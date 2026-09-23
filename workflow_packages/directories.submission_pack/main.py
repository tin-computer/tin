"""
Directory submission pack — deterministic, no model calls, stdlib only.

Given product metadata, renders one Markdown section per target directory
with fields trimmed to that directory's exact character limits. Returns a
single project artifact: reports/DIRECTORY_SUBMISSION_PACK.md.
"""

import json
import re
from pathlib import Path

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "directories.json"
OUTPUT_PATH = "reports/DIRECTORY_SUBMISSION_PACK.md"


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Strip ends and collapse internal whitespace."""
    return re.sub(r"\s+", " ", text.strip())


def _trim_to(text: str, max_chars: int) -> tuple[str, bool]:
    """
    Trim text to at most max_chars characters, breaking at a word boundary.
    Returns (result, was_truncated).
    """
    if len(text) <= max_chars:
        return text, False
    candidate = text[: max_chars - 1]  # -1 to leave room for ellipsis
    last_space = candidate.rfind(" ")
    if last_space > max_chars // 2:
        candidate = candidate[:last_space]
    return candidate.rstrip() + "\u2026", True


def _build_description(tagline: str, short_description: str, max_chars: int) -> str:
    """
    Pick the best description candidate and trim to max_chars.
    Prefer short_description if it fits; fall back to tagline.
    """
    candidate = short_description if short_description else tagline
    result, _ = _trim_to(candidate, max_chars)
    return result


def _render_field(label: str, value: str | list[str], warning: str = "") -> str:
    if isinstance(value, list):
        display = ", ".join(value) if value else "(none provided)"
    else:
        display = value
    warn_suffix = f"  \u26a0\ufe0f {warning}" if warning else ""
    return f"**{label}:** `{display}`{warn_suffix}"


# ---------------------------------------------------------------------------
# Per-directory assembly
# ---------------------------------------------------------------------------

def _build_payload(directory: dict, inputs: dict) -> tuple[dict, list[str], bool]:
    """
    Build the submission payload for one directory.
    Returns (payload_ordered_dict, warnings, passed).
    """
    spec = directory["field_spec"]
    product_name: str = inputs["product_name"]
    tagline: str = inputs["tagline"]
    short_description: str = inputs.get("short_description", "")
    pricing_summary: str = inputs.get("pricing_summary", "")
    category_tags: list[str] = inputs.get("category_tags", [])
    founder_email: str = inputs.get("founder_email", "")
    website_url: str = inputs["website_url"]

    warnings: list[str] = []
    fail = False

    # Product name
    name, name_trimmed = _trim_to(product_name, spec["name_max"])
    if name_trimmed:
        warnings.append(f"product name trimmed to {spec['name_max']} chars")

    # Tagline
    tagline_result, tagline_trimmed = _trim_to(tagline, spec["tagline_max"])
    if tagline_trimmed:
        warnings.append(f"tagline trimmed to {spec['tagline_max']} chars")

    # Description
    desc = _build_description(tagline, short_description, spec["description_max"])

    # Pricing
    pricing_result = ""
    pricing_warning = ""
    if pricing_summary:
        pricing_result, pricing_trimmed = _trim_to(pricing_summary, spec.get("pricing_max", 200))
        if pricing_trimmed:
            pricing_warning = f"pricing trimmed to {spec.get('pricing_max', 200)} chars"
            warnings.append(pricing_warning)

    # Tags
    tags_max = spec.get("tags_max", 5)
    tag_max_each = spec.get("tag_max_each", 30)
    trimmed_tags = [t[:tag_max_each] for t in category_tags[:tags_max]]
    if len(category_tags) > tags_max:
        dropped = len(category_tags) - tags_max
        warnings.append(
            f"{dropped} tag(s) dropped — {directory['name']} limit is {tags_max}"
        )

    # Validation: assert our trimming produced values within spec
    for field_label, value, limit in [
        ("Product name", name, spec["name_max"]),
        ("Tagline", tagline_result, spec["tagline_max"]),
        ("Description", desc, spec["description_max"]),
    ]:
        if len(value) > limit:
            warnings.append(f"INTERNAL FAIL: {field_label} is {len(value)} chars, limit {limit}")
            fail = True

    payload: dict = {"Product name": name, "Tagline": tagline_result, "Description": desc}

    if pricing_result:
        payload["Pricing"] = pricing_result

    payload["Tags / Categories"] = trimmed_tags
    payload["Website URL"] = website_url

    if directory.get("requires_email") and founder_email:
        payload["Contact email"] = founder_email
    elif directory.get("requires_email") and not founder_email:
        warnings.append(
            f"{directory['name']} expects a contact email — provide founder_email input"
        )

    passed = not fail
    return payload, warnings, passed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_directory_section(directory: dict, payload: dict, warnings: list[str]) -> str:
    lines = [
        f"## {directory['name']}",
        "",
        f"**Submission URL:** <{directory['submission_url']}>",
        f"**Domain Authority:** {directory.get('da', 'N/A')}",
        f"**Notes:** {directory.get('notes', '')}",
        "",
        "### Fields to paste",
        "",
    ]

    for label, value in payload.items():
        # Find if there's a per-field warning (heuristic: check warnings list)
        field_key = label.lower().replace(" / ", "_").replace(" ", "_")
        field_warn = next(
            (w for w in warnings if field_key.split("_")[0] in w.lower()), ""
        )
        lines.append(_render_field(label, value, ""))

    lines.append("")

    if warnings:
        lines.append("**Warnings:**")
        for w in warnings:
            lines.append(f"- \u26a0\ufe0f {w}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _render_tracker(directories: list[dict], warnings_map: dict[str, list[str]]) -> str:
    header = [
        "## Submission tracker",
        "",
        "Update Status as submissions go live. Add login or confirmation notes in the Notes column.",
        "",
        "| Directory | DA | Submission URL | Status | Notes |",
        "|-----------|-----|----------------|--------|-------|",
    ]
    rows = []
    for d in directories:
        warn_count = len(warnings_map.get(d["name"], []))
        note = f"{warn_count} warning(s) — see section above" if warn_count else ""
        rows.append(
            f"| {d['name']} | {d.get('da', '?')} "
            f"| {d['submission_url']} "
            f"| \u2b1c Not started | {note} |"
        )
    return "\n".join(header + rows) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(ctx, inputs):  # noqa: ARG001 — ctx unused (no model calls)
    # Step 1: Load fixture
    raw = FIXTURES_PATH.read_text(encoding="utf-8")
    all_directories: list[dict] = json.loads(raw)

    # Step 2: Normalize string inputs
    product_name = _normalize(inputs["product_name"])
    tagline = _normalize(inputs["tagline"])
    short_description = _normalize(inputs.get("short_description", ""))
    pricing_summary = _normalize(inputs.get("pricing_summary", ""))
    category_tags = [_normalize(t) for t in inputs.get("category_tags", [])]
    founder_email = inputs.get("founder_email", "").strip()
    target_names = [_normalize(t) for t in inputs.get("target_directories", [])]
    website_url = inputs["website_url"].strip()

    normalized = {
        "product_name": product_name,
        "tagline": tagline,
        "short_description": short_description,
        "pricing_summary": pricing_summary,
        "category_tags": category_tags,
        "founder_email": founder_email,
        "website_url": website_url,
    }

    # Step 3: Validate required fields
    errors: list[str] = []
    if not product_name:
        errors.append("product_name is required and must be non-empty")
    if len(product_name) > 60:
        errors.append(f"product_name exceeds 60 chars ({len(product_name)})")
    if not tagline or len(tagline) < 10:
        errors.append("tagline must be at least 10 characters")
    if len(tagline) > 120:
        errors.append(f"tagline exceeds 120 chars ({len(tagline)})")
    if not website_url.startswith("https://"):
        errors.append("website_url must start with https://")
    if errors:
        raise ValueError("Input validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    # Step 4: Filter directories
    if target_names:
        lookup = {d["name"].lower(): d for d in all_directories}
        target_set = {t.lower() for t in target_names}
        unknown = target_set - lookup.keys()
        if unknown:
            valid = ", ".join(sorted(d["name"] for d in all_directories))
            raise ValueError(
                f"Unknown directories: {', '.join(sorted(unknown))}.\n"
                f"Valid names: {valid}"
            )
        directories = [lookup[t] for t in target_set]
        # Preserve fixture order
        directories = [d for d in all_directories if d["name"].lower() in target_set]
    else:
        directories = all_directories

    if not directories:
        raise ValueError("No directories to process. Check fixtures/directories.json.")

    # Steps 5–7: Per-directory build, validate, and render
    sections: list[str] = []
    warnings_map: dict[str, list[str]] = {}
    passed_count = 0

    for directory in directories:
        payload, warnings, passed = _build_payload(directory, normalized)
        warnings_map[directory["name"]] = warnings
        if passed:
            passed_count += 1
        sections.append(_render_directory_section(directory, payload, warnings))

    # Step 8: Tracker table
    tracker = _render_tracker(directories, warnings_map)

    # Step 9: Assemble final report
    total = len(directories)
    summary_lines = [
        "# Directory submission pack",
        "",
        f"**Product:** {product_name}",
        f"**Website:** {website_url}",
        f"**Generated:** {total} director{'y' if total == 1 else 'ies'}, "
        f"{passed_count}/{total} passed field validation",
        "",
        "> Copy each section into the corresponding directory's submission form.",
        "> Fields marked \u26a0\ufe0f were trimmed to fit that site's character limit.",
        "> Do not submit without reviewing. This workflow does not auto-submit.",
        "",
        "---",
        "",
    ]

    content = "\n".join(summary_lines) + "".join(sections) + "\n" + tracker

    return {"path": OUTPUT_PATH, "content": content}
