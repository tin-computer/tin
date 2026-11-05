from __future__ import annotations

import importlib.util
import re
import struct
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from tin_lite.catalog import BUILTIN_WORKFLOWS, CREATIVE_STUDIO_SYSTEM
from tin_lite.domain import CREATIVE_FEATURE_FILM_WORKFLOW_NAME
from tin_lite.page_assets import AssetPolicy
from tin_lite.procedures import (
    PinnedCodexProcedure,
    validate_codex_procedure_definition,
    validate_procedure_artifact,
)
from tin_lite.studio_contracts import (
    FEATURE_FILM_VALIDATOR,
    parse_feature_film,
    validate_feature_film_video,
)
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs

ROOT = Path(__file__).parents[1]
PROJECT_ID = UUID("00000000-0000-4000-8000-0000000000aa")
NOTE = b"""# Draw a circle, and the sheep follow

```tin-video
src: herding.assets/film.mp4
poster: herding.assets/poster.png
captions: herding.assets/captions.vtt
title: Draw a circle, and the sheep follow
```

**Before:** Herding games make you steer one dog at a time.

## What it says

- 0:00 Draw a circle round the sheep, and they follow it.
"""


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _mp4(*, seconds: float = 20.0, width: int = 1080, height: int = 1350) -> bytes:
    mvhd = _box(b"mvhd", bytes(12) + struct.pack(">II", 1000, int(seconds * 1000)) + bytes(80))
    tkhd = _box(b"tkhd", bytes(76) + struct.pack(">II", width << 16, height << 16))
    trak = _box(b"trak", tkhd + _box(b"mdia", _box(b"hdlr", bytes(8) + b"vide" + bytes(12))))
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isomiso2mp41")
    return ftyp + _box(b"moov", mvhd + trak) + _box(b"mdat", bytes(64))


def test_a_film_note_names_its_title_and_the_files_it_plays() -> None:
    note = parse_feature_film(NOTE, folder="herding.assets")
    assert note.title == "Draw a circle, and the sheep follow"
    assert note.video == "herding.assets/film.mp4"
    assert note.poster == "herding.assets/poster.png"
    assert note.captions == "herding.assets/captions.vtt"
    bare = b"# Film\n\n```tin-video\nsrc: ./herding.assets/film.mp4\n```\n"
    assert parse_feature_film(bare, folder="herding.assets").poster is None


@pytest.mark.parametrize(
    ("note", "message"),
    [
        (NOTE.replace(b"# Draw", b"Draw", 1), "title"),
        (NOTE.replace(b"```tin-video", b"```text"), "exactly one film"),
        (NOTE + b"\n```tin-video\nsrc: herding.assets/two.mp4\n```\n", "exactly one film"),
        # A plausible model result that Tin can't keep: the film sits in another note's folder,
        # or is linked from the site rather than carried, or the poster is a video.
        (NOTE.replace(b"src: herding.assets/", b"src: launch.assets/"), "src must be an MP4"),
        (NOTE.replace(b"src: herding.assets/film.mp4", b"url: https://x.test/a.mp4"), "src"),
        (NOTE.replace(b"poster.png", b"poster.mp4"), "poster must be a PNG or JPEG"),
        (NOTE.replace(b"src: herding.assets/film.mp4", b"src: herding.assets/../f.mp4"), "src"),
        (b"\xff" + NOTE, "UTF-8"),
        (b"", "1-64000 bytes"),
    ],
)
def test_a_film_note_that_does_not_carry_its_film_is_refused(note: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_feature_film(note, folder="herding.assets")


def test_the_film_is_a_fast_start_mp4_in_a_kit_size_and_length() -> None:
    for width, height in ((1080, 1350), (1080, 1080), (1920, 1080)):
        assert validate_feature_film_video(_mp4(width=width, height=height)).width == width
    with pytest.raises(ValueError, match="8-90 seconds"):
        validate_feature_film_video(_mp4(seconds=4))
    with pytest.raises(ValueError, match="1080x1350"):
        validate_feature_film_video(_mp4(width=1080, height=1920))


def test_the_workflow_files_its_note_with_the_film_beside_it() -> None:
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == CREATIVE_FEATURE_FILM_WORKFLOW_NAME)
    definition = workflow.definition
    assert definition["system"] == CREATIVE_STUDIO_SYSTEM
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["human_review"]["eligible"] is True
    spec = validate_codex_procedure_definition(definition)
    assert spec.sandbox.studio and spec.sandbox.open_egress
    assert not spec.identity
    # A brief, not a page to film: only the name is required, and a page is only for its look.
    assert normalize_workflow_inputs(
        schema=definition["input_schema"],
        project_id=PROJECT_ID,
        inputs={"slug": "herding"},
    ) == {
        "slug": "herding",
        "feature": "",
        "post": "",
        "angle": "",
        "product_url": "",
        "format": "16:9",
        "notes": "",
    }
    for page in ("", "https://sheepdogs.io/"):
        assert (
            normalize_workflow_inputs(
                schema=definition["input_schema"],
                project_id=PROJECT_ID,
                inputs={"slug": "herding", "product_url": page},
            )["product_url"]
            == page
        )
    for page in ("javascript:alert(1)", "sheepdogs.io", "https:// x"):
        with pytest.raises(WorkflowInputError):
            normalize_workflow_inputs(
                schema=definition["input_schema"],
                project_id=PROJECT_ID,
                inputs={"slug": "herding", "product_url": page},
            )
    pinned = PinnedCodexProcedure(
        workflow_key=CREATIVE_FEATURE_FILM_WORKFLOW_NAME,
        prompt="p",
        entry_skill="s",
        skill_files={"s/SKILL.md": b"---\nname: s\n---\n"},
        output_path="films/herding.md",
        output_media_type="text/markdown",
        output_validator=FEATURE_FILM_VALIDATOR,
        output_max_bytes=64_000,
        output_assets=AssetPolicy(max_files=6, max_bytes=24_000_000, kinds=("mp4", "png")),
    )
    validate_procedure_artifact(NOTE, spec=pinned)
    with pytest.raises(ValueError, match="src must be an MP4"):
        validate_procedure_artifact(NOTE.replace(b"herding.assets", b"x.assets"), spec=pinned)


def test_a_film_validator_needs_a_films_note_that_can_carry_an_mp4() -> None:
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == CREATIVE_FEATURE_FILM_WORKFLOW_NAME)
    procedure = workflow.definition["procedure"]
    for output in (
        {**procedure["output"], "path_template": "demos/{slug}.md"},
        {**procedure["output"], "assets": {"max_files": 6, "max_bytes": 1000, "kinds": ["png"]}},
        {k: v for k, v in procedure["output"].items() if k != "assets"},
    ):
        with pytest.raises(ValueError, match="films/"):
            validate_codex_procedure_definition(
                {**workflow.definition, "procedure": {**procedure, "output": output}}
            )


def test_the_studio_image_carries_the_film_kit_and_tin_film(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "tin_lite_sandbox_template_film", ROOT / "sandbox" / "template.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[tuple[str, tuple]] = []

    class Builder:
        def __init__(self, *, file_context_path):
            assert file_context_path == ROOT

        def __getattr__(self, name):
            def record(*args, **kwargs):
                calls.append((name, args))
                return self

            return record

    monkeypatch.setattr(module, "Template", Builder)
    module.studio_api_template()
    copied = {args[:2] for name, args in calls if name == "copy"}
    assert ("sandbox/tin_film.sh", "/opt/tin-lite/studio/tin-film") in copied
    for name in module.FILM_FILES:
        assert (name, f"/opt/tin-lite/diagram/{name}") in copied
        assert (ROOT / name).is_file()
    # Every module the command imports ships with it.
    kit = {name for name in module.FILM_FILES if name.endswith(".js")}
    for name in kit:
        for target in re.findall(r'from "(\.{1,2}/[^"]+)"', (ROOT / name).read_text()):
            imported = (ROOT / name).parent.joinpath(target).resolve().relative_to(ROOT)
            assert imported.as_posix() in kit, (name, target)
    assert ("make_symlink", ("/opt/tin-lite/studio/tin-film", "/usr/local/bin/tin-film")) in [
        (name, args[:2]) for name, args in calls
    ]
    wrapper = (ROOT / "sandbox" / "tin_film.sh").read_text()
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/tin-lite/diagram/browsers" in wrapper
    assert "unset HTTP_PROXY HTTPS_PROXY" in wrapper
    runner = (ROOT / "sandbox" / "run_procedure.sh").read_text()
    assert "/opt/tin-lite/studio/tin-film" in runner
    bridge = (ROOT / "sandbox" / "procedure_app_server.py").read_text()
    assert 'context.get("workflow_key") == "creative.feature_film"' in bridge


def _check(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — the repository's own toolkit, fixed arguments
        [sys.executable, ROOT / "sandbox" / "studio" / "tin_studio.py", "check", path],
        capture_output=True,
        text=True,
        check=False,
    )


def test_tin_studio_checks_a_delivered_film_as_tin_keeps_it(tmp_path: Path) -> None:
    folder = tmp_path / "herding.assets"
    folder.mkdir()
    (folder / "film.mp4").write_bytes(_mp4())
    (folder / "poster.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(8) + b"IHDR")
    (folder / "captions.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nHello\n")
    note = tmp_path / "herding.md"
    note.write_bytes(NOTE)
    checked = _check(note)
    assert checked.returncode == 0, checked.stdout
    assert checked.stdout.startswith("OK:")
    (folder / "film.mp4").write_bytes(_mp4(seconds=3))
    checked = _check(note)
    assert checked.returncode == 1
    assert "REJECTED: film must run 8-90 seconds" in checked.stdout
    (folder / "film.mp4").unlink()
    assert "REJECTED:" in _check(note).stdout
