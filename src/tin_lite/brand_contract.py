"""Small, strict brand data contract; taste remains evidence-backed human judgment."""

import json
import re
from datetime import datetime
from urllib.parse import urlsplit

KEY = "brand.capture"
VALIDATOR = "brand-design-capture.v1"
BRAND_PATH, DESIGN_PATH = "brand/BRAND.md", "DESIGN.md"
BRAND_MAX, DESIGN_MAX = 48_000, 64_000
TOKEN_SCHEMA, ASSESSMENT_SCHEMA = "tin-brand.v1", "tin-brand-assessment.v1"
BRAND_SECTIONS = ("Brand direction", "Visual style", "Generation rules", "Assessment and sources")
DESIGN_SECTIONS = (
    "Visual foundations",
    "Components and patterns",
    "Screens and flows",
    "Constraints and evidence",
)
CLASSES = {"distinctive_strong", "distinctive_inconsistent", "competent_generic", "weak", "unknown"}
EVIDENCE = {"adequate", "partial", "insufficient", "conflicting"}
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")
_FENCES = re.compile(r"^```json\s*\n(.*?)\n```[ \t]*$", re.M | re.S)
_REFERENCE = re.compile(r'^\[([a-z][a-z0-9_-]{0,63})\]:\s+(\S+)(?:\s+"[^"\n]*")?\s*$', re.M)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("brand JSON contains a duplicate key")
        result[key] = value
    return result


def block(text, schema, limit, *, required=True):
    found = []
    for match in _FENCES.finditer(text):
        raw = match.group(1)
        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, TypeError) as exc:
            if re.search(r'"schema"\s*:\s*' + re.escape(json.dumps(schema)), raw):
                raise ValueError(f"Invalid {schema} JSON") from exc
            continue
        if not isinstance(value, dict) or value.get("schema") != schema:
            continue
        if len(raw.encode()) > limit:
            raise ValueError(f"{schema} block exceeds its byte limit")
        found.append((value, match.start(), match.end()))
    if len(found) > 1 or (required and not found):
        raise ValueError(f"Expected one {schema} block")
    return found[0] if found else None


def text_value(value, limit, name):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {name}")


def tokens(text):
    value, _, _ = block(text, TOKEN_SCHEMA, 2_000)
    if (
        not {"schema", "name", "light"}
        <= set(value)
        <= {
            "schema",
            "name",
            "light",
            "dark",
            "direction",
            "type",
            "shape",
        }
    ):
        raise ValueError("Brand tokens contain missing or unsupported fields")
    text_value(value["name"], 120, "brand name")
    if "direction" in value:
        text_value(value["direction"], 500, "brand direction")
    for mode in ("light", "dark"):
        if mode not in value:
            continue
        palette = value[mode]
        if (
            not isinstance(palette, dict)
            or not {"ink", "paper", "accent"}
            <= set(palette)
            <= {
                "ink",
                "paper",
                "accent",
                "signal",
            }
            or any(not isinstance(c, str) or not _HEX.fullmatch(c) for c in palette.values())
        ):
            raise ValueError(
                f"Brand {mode} palette requires six-digit ink, paper and accent colors"
            )
    if "type" in value:
        families = value["type"]
        if (
            not isinstance(families, dict)
            or not families
            or set(families) - {"display", "body", "mono"}
        ):
            raise ValueError("Invalid brand type roles")
        for family in families.values():
            text_value(family, 100, "type family")
    if "shape" in value and (
        not isinstance(value["shape"], str) or value["shape"] not in {"sharp", "soft", "round"}
    ):
        raise ValueError("Invalid brand shape")
    return value


def sources(text):
    found = {}
    for source_id, target in _REFERENCE.findall(text):
        if source_id in found:
            raise ValueError("Duplicate source reference")
        if not target.startswith(("https://", "code.storage://")) or any(
            s in target.lower() for s in ("/users/", "/home/", "@localhost", "password=", "token=")
        ):
            raise ValueError("Source references must identify URLs or pinned project files")
        if target.startswith("https://"):
            parsed = urlsplit(target)
            if not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError("Source URLs must not contain credentials")
        elif not re.fullmatch(r"code.storage://[^@\s]+@[0-9a-f]{40}/\S+", target):
            raise ValueError("Project source references require an immutable revision")
        found[source_id] = target
    return found


def assessment(text, *, required=False):
    item = block(text, ASSESSMENT_SCHEMA, 8_000, required=required)
    if item is None:
        return None
    value = item[0]
    if (
        set(value)
        != {
            "schema",
            "identity_class",
            "evidence_status",
            "source_ids",
            "captured_at",
            "findings",
            "method",
        }
        or not isinstance(value["identity_class"], str)
        or value["identity_class"] not in CLASSES
        or not isinstance(value["evidence_status"], str)
        or value["evidence_status"] not in EVIDENCE
    ):
        raise ValueError("Invalid brand assessment fields")
    ids = value["source_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 16
        or any(not isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
        or not set(ids) <= sources(text).keys()
    ):
        raise ValueError("Brand assessment must cite declared sources")
    try:
        stamp = datetime.fromisoformat(value["captured_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid capture timestamp") from exc
    if stamp.tzinfo is None:
        raise ValueError("Capture timestamp must include its timezone")
    if value["method"] != {"kind": "visual_capture", "rubric_version": "marketing-brand.v1"}:
        raise ValueError("Unsupported brand assessment method")
    findings = value["findings"]
    if not isinstance(findings, list) or not 1 <= len(findings) <= 5:
        raise ValueError("Brand assessment requires one to five grounded findings")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "observation",
            "source_ids",
            "generation_implication",
        }:
            raise ValueError("Invalid brand finding")
        text_value(finding["observation"], 1000, "observation")
        text_value(finding["generation_implication"], 1000, "generation implication")
        refs = finding["source_ids"]
        if not isinstance(refs, list) or not refs or any(i not in ids for i in refs):
            raise ValueError("Every finding must cite its evidence")
    return value


def sections(text, headings):
    matches = list(re.finditer(r"^## (.+)$", text, re.M))
    if [m.group(1).strip() for m in matches] != list(headings):
        raise ValueError("New capture must use the four documented sections")
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if len(body) < 20:
            raise ValueError("Capture sections must contain useful observations or specific gaps")
        result[headings[index]] = body
    return result


def validate_new_brand(text):
    parts = sections(text, BRAND_SECTIONS)
    palette = tokens(text)
    assessment(parts["Assessment and sources"], required=True)
    if block(text, TOKEN_SCHEMA, 2_000)[2] != len(text.rstrip()):
        raise ValueError("Brand tokens must be the final block")
    visual = parts["Visual style"].lower()
    for mode in ("light", "dark"):
        for color in palette.get(mode, {}).values():
            if color.lower() not in visual:
                raise ValueError("Visual style must describe each declared palette value")
    return palette


def validate_new_design(text):
    sections(text, DESIGN_SECTIONS)
    if not sources(text):
        raise ValueError("New design documentation must include attributed sources")
