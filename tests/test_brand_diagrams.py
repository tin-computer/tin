import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tin_lite import brand_diagrams
from tin_lite.codex_api import DIAGRAM_CONTRACT, procedure_contract
from tin_lite.procedures import PinnedCodexProcedure, validate_procedure_artifact

REVISION = "a" * 40
PALETTE = {"ink": "#212132", "paper": "#FAFAF5", "accent": "#476ABC"}
TOKENS = {"schema": "tin-brand.v1", "name": "Example", "light": PALETTE}
GUIDE = ("# Approved guidance\n\n```json\n" + json.dumps(TOKENS) + "\n```\n").encode()
SOURCE = 'graph LR\n a["Ask"]:::step\n b["Done"]:::receipt\n a --> b\n'


def storage(brand=GUIDE, design=b"# Existing design\n\nObserved patterns only."):
    async def read(**kwargs):
        assert kwargs["revision"] == REVISION
        content = {"brand/BRAND.md": brand, "DESIGN.md": design}[kwargs["path"]]
        return ("blob", content) if content is not None else None

    return SimpleNamespace(read_output_destination=AsyncMock(side_effect=read))


@pytest.mark.asyncio
async def test_active_guidance_is_pinned_and_output_cannot_change_or_drop_palette():
    store = storage()
    context = await brand_diagrams.prepare(
        store, SimpleNamespace(state_repo_id="project"), REVISION
    )
    assert context["source_paths"] == ["brand/BRAND.md", "DESIGN.md"]
    assert context["brand"]["sha256"] == hashlib.sha256(GUIDE).hexdigest()
    assert context["brand"]["revision"] == REVISION
    assert "Geist" in context["fonts"]
    source = SOURCE.replace("graph LR\n", "graph LR\n" + context["source_line"] + "\n")
    spec = PinnedCodexProcedure(
        workflow_key="content.diagram",
        prompt="Draw",
        entry_skill="content-diagram",
        skill_files={},
        output_validator=brand_diagrams.VALIDATOR,
        diagram_brand_context=context,
    )
    validate_procedure_artifact(source.encode(), spec=spec)
    for wrong in (SOURCE, source.replace("#476ABC", "#FF0000"), source.replace(REVISION, "b" * 40)):
        with pytest.raises(ValueError, match="palette"):
            validate_procedure_artifact(wrong.encode(), spec=spec)
    with pytest.raises(ValueError, match="pinned"):
        validate_procedure_artifact(source.encode(), spec=replace(spec, diagram_brand_context=None))
    for legacy in ("tin-diagram.v2", "tin-diagram.reviewed.v1"):
        validate_procedure_artifact(SOURCE.encode(), spec=replace(spec, output_validator=legacy))
        with pytest.raises(ValueError):
            validate_procedure_artifact(
                source.encode(), spec=replace(spec, output_validator=legacy)
            )
    assert procedure_contract(brand_diagrams.VALIDATOR) == DIAGRAM_CONTRACT


@pytest.mark.asyncio
async def test_missing_brand_uses_tin_but_invalid_active_brand_never_uses_proposals():
    project = SimpleNamespace(state_repo_id="project")
    context = await brand_diagrams.prepare(storage(brand=None), project, REVISION)
    assert context["brand"] is None and context["source_line"] is None
    assert context["source_paths"] == ["DESIGN.md"]
    brand_diagrams.validate_output(SOURCE, context)
    with pytest.raises(ValueError, match="active brand/BRAND.md"):
        await brand_diagrams.prepare(storage(brand=b"invalid guide"), project, REVISION)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [None, "sharp", "soft", "round"])
async def test_shape_is_approved_and_pinned_without_changing_legacy_run_snapshots(shape):
    tokens = {**TOKENS, **({"shape": shape} if shape else {})}
    guide = ("# Guidance\n```json\n" + json.dumps(tokens) + "\n```\n").encode()
    store = storage(brand=guide, design=b"# Observations\nCards have 99px corners.")
    project = SimpleNamespace(state_repo_id="project")
    current = await brand_diagrams.prepare(store, project, REVISION)
    legacy = await brand_diagrams.prepare(
        store, project, REVISION, validator=brand_diagrams.LEGACY_VALIDATOR
    )
    assert current["brand"].get("shape") == shape
    assert "shape" not in legacy["brand"]
    assert current["brand"]["sha256"] == hashlib.sha256(guide).hexdigest()
    for validator, context in [
        (brand_diagrams.VALIDATOR, current),
        (brand_diagrams.LEGACY_VALIDATOR, legacy),
    ]:
        source = SOURCE.replace("graph LR\n", "graph LR\n" + context["source_line"] + "\n")
        spec = PinnedCodexProcedure(
            workflow_key="content.diagram",
            prompt="Draw",
            entry_skill="content-diagram",
            skill_files={},
            output_validator=validator,
            diagram_brand_context=context,
        )
        validate_procedure_artifact(source.encode(), spec=spec)
        assert procedure_contract(validator) == DIAGRAM_CONTRACT
        tampered = dict(context["brand"])
        tampered["shape"] = "sharp" if shape != "sharp" else "round"
        changed = SOURCE.replace(
            "graph LR\n",
            "graph LR\n"
            + brand_diagrams.PREFIX
            + json.dumps(tampered, separators=(",", ":"))
            + "\n",
        )
        with pytest.raises(ValueError, match="shape"):
            validate_procedure_artifact(changed.encode(), spec=spec)
    if shape:
        with pytest.raises(ValueError, match="shape"):
            brand_diagrams.validate_output(
                SOURCE.replace("graph LR\n", "graph LR\n" + legacy["source_line"] + "\n"), current
            )


@pytest.mark.parametrize("shape", [None, "sharp", "soft", "round"])
def test_branded_source_parity_and_injection_rejection(shape):
    value = {
        "revision": REVISION,
        "sha256": "b" * 64,
        "light": PALETTE,
        **({"shape": shape} if shape else {}),
    }
    line = brand_diagrams.PREFIX + json.dumps(value, separators=(",", ":"))
    source = SOURCE.replace("graph LR\n", "graph LR\n" + line + "\n")
    bad = [
        source.replace('"light":', '"url":"https://untrusted.test/font","light":'),
        source.replace('"ink":"#212132"', '"ink":"red; color:black"'),
        source.replace('"ink":"#212132"', '"ink":"#FFFFFF","ink":"#212132"'),
        source + line,
        source.replace("graph LR", "graph LR\n" + line),
        source.replace('"revision":"' + REVISION, '"revision":"short'),
        source.replace('"revision":"' + REVISION + '"', '"revision":["' + REVISION + '"]'),
        source.replace('"light":{', '"light":null,"extra":{'),
    ]
    for unsupported in (None, 12, True, ["round"], {"node": 12}, "pill", "round;rx:99"):
        invalid = {**value, "shape": unsupported}
        bad.append(
            SOURCE.replace(
                "graph LR\n",
                "graph LR\n"
                + brand_diagrams.PREFIX
                + json.dumps(invalid, separators=(",", ":"))
                + "\n",
            )
        )
    for invalid in bad:
        with pytest.raises(ValueError):
            brand_diagrams.parse_diagram(invalid)
    result = subprocess.run(  # noqa: S603 — fixed local parser; source only travels on stdin
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            """
import fs from 'node:fs';
import {parseSource, sourceForFlow} from './web/diagram-contract.js';
const sources = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(sources.map(source => {
  try { const flow = parseSource(source); return parseSource(sourceForFlow(flow)); }
  catch { return null; }
})));""",
        ],
        input=json.dumps([source, SOURCE, *bad]),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        brand_diagrams.parse_diagram(source),
        brand_diagrams.parse_diagram(SOURCE),
        *([None] * len(bad)),
    ]
