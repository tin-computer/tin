"""Approved project guidance and a portable, bounded diagram palette snapshot."""

import hashlib
import json
import re

from tin_lite import brand_contract
from tin_lite.brand_capture import resolve_brand
from tin_lite.diagram_compositions import parse_diagram_v2
from tin_lite.procedure_documents import validate_document

VALIDATOR = "tin-diagram.branded.v1"
PREFIX = "%% tin:brand "


def validate_brand(value):
    if (
        not isinstance(value, dict)
        or not {"revision", "sha256", "light"}
        <= set(value)
        <= {"revision", "sha256", "light", "dark"}
        or not isinstance(value["revision"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["revision"])
        or not isinstance(value["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
    ):
        raise ValueError("Invalid diagram brand snapshot")
    # Reuse the established palette contract, without accepting font URLs or CSS.
    brand_contract.tokens(
        "```json\n"
        + json.dumps(
            {
                "schema": "tin-brand.v1",
                "name": "Diagram",
                **{mode: value[mode] for mode in ("light", "dark") if mode in value},
            }
        )
        + "\n```"
    )
    return value


def parse_diagram(content):
    if len(content.encode("utf-16-le")) // 2 > 64_000:
        raise ValueError("Diagram source is too large")
    lines = content.splitlines()
    brand = None
    if len(lines) > 1 and lines[1].strip().startswith(PREFIX):
        raw = lines.pop(1).strip()[len(PREFIX) :]
        if len(raw) > 2_000:
            raise ValueError("Diagram brand snapshot is too large")
        brand = validate_brand(json.loads(raw, object_pairs_hook=brand_contract.unique_object))
        if json.dumps(brand, separators=(",", ":")) != raw:
            raise ValueError("Copy the compact diagram brand snapshot unchanged")
    flow = parse_diagram_v2("\n".join(lines))
    if brand is not None:
        flow["brand"] = brand
    return flow


async def prepare(storage, project, revision):
    brand = await resolve_brand(storage, project, revision)
    if brand["status"] == "brand_invalid":
        raise ValueError(
            "Fix active brand/BRAND.md before generating a branded diagram: "
            + "; ".join(brand["diagnostics"])
        )
    snapshot = None
    paths = []
    if brand["status"] == "available":
        paths.append(brand_contract.BRAND_PATH)
        snapshot = {
            "revision": revision,
            "sha256": brand["sha256"],
            **{
                mode: brand["tokens"][mode] for mode in ("light", "dark") if mode in brand["tokens"]
            },
        }
    design = await storage.read_output_destination(
        repo_id=project.state_repo_id, revision=revision, path=brand_contract.DESIGN_PATH
    )
    if design is not None:
        validate_document(design[1], brand_contract.DESIGN_MAX)
        paths.append(brand_contract.DESIGN_PATH)
    return {
        "revision": revision,
        "brand": snapshot,
        "source_paths": paths,
        "design_sha256": hashlib.sha256(design[1]).hexdigest() if design else None,
        "source_line": PREFIX + json.dumps(snapshot, separators=(",", ":")) if snapshot else None,
        "fonts": "Bundled Geist Sans/Mono; custom font files are not loaded by this renderer.",
    }


def validate_output(content, context):
    if context is None:
        raise ValueError("Diagram guidance must be resolved at the pinned project revision")
    if parse_diagram(content).get("brand") != context["brand"]:
        raise ValueError("Diagram palette must match the pinned active brand guidance")
