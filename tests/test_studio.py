from __future__ import annotations

import base64
import importlib.util
import json
import struct
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from tin_lite.catalog import BUILTIN_WORKFLOWS, CREATIVE_STUDIO_SYSTEM, WORKFLOW_SYSTEMS
from tin_lite.domain import (
    CREATIVE_CHARACTER_WORKFLOW_NAME,
    CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
    STUDIO_PROVIDER,
    STUDIO_VOICE_CAPABILITY,
    RunToolGrant,
    StudioUsage,
)
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.procedures import (
    STUDIO_SANDBOX_PROFILE,
    PinnedCodexProcedure,
    SandboxProfile,
    validate_codex_procedure_definition,
    validate_procedure_artifact,
)
from tin_lite.run_tools import create_run_tools_app
from tin_lite.studio import StudioError, StudioService, StudioVoice
from tin_lite.studio_contracts import (
    CHARACTER_SVG_VALIDATOR,
    DEMO_VIDEO_VALIDATOR,
    character_variant_svg,
    validate_character_svg,
    validate_demo_video,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs

ROOT = Path(__file__).parents[1]
LARRY = (ROOT / "src" / "tin_lite" / "example_character.svg").read_bytes()
RUN_ID = UUID("00000000-0000-4000-8000-0000000000cc")
PROJECT_ID = UUID("00000000-0000-4000-8000-0000000000aa")
GRANT = "opaque-studio-grant"  # noqa: S105


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _mp4(
    *,
    seconds: float = 20.0,
    width: int = 1080,
    height: int = 1920,
    audio: bool = True,
    faststart: bool = True,
) -> bytes:
    timescale = 1000
    mvhd = _box(
        b"mvhd", bytes(12) + struct.pack(">II", timescale, int(seconds * timescale)) + bytes(80)
    )

    def trak(handler: bytes) -> bytes:
        tkhd = _box(b"tkhd", bytes(76) + struct.pack(">II", width << 16, height << 16))
        hdlr = _box(b"hdlr", bytes(8) + handler + bytes(12))
        return _box(b"trak", tkhd + _box(b"mdia", hdlr))

    tracks = trak(b"vide") + (trak(b"soun") if audio else b"")
    moov = _box(b"moov", mvhd + tracks)
    mdat = _box(b"mdat", bytes(64))
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isomiso2mp41")
    return ftyp + (moov + mdat if faststart else mdat + moov)


def test_sandbox_and_switchboard_agree_on_the_default_voice_style() -> None:
    from tin_lite.studio import DEFAULT_VOICE_STYLE

    voice_source = (ROOT / "sandbox" / "studio" / "voice.py").read_text()
    assert f'DEFAULT_STYLE = "{DEFAULT_VOICE_STYLE}"' in voice_source
    # fal's content checker refused ordinary lines under the earlier "short-form voiceover" style.
    assert "voiceover" not in DEFAULT_VOICE_STYLE.lower()


def test_sandbox_and_switchboard_share_one_contracts_module() -> None:
    sandbox_copy = (ROOT / "sandbox" / "studio" / "studio_contracts.py").read_bytes()
    switchboard_copy = (ROOT / "src" / "tin_lite" / "studio_contracts.py").read_bytes()
    assert sandbox_copy == switchboard_copy


def test_character_contract_accepts_the_example_and_flips_states() -> None:
    summary = validate_character_svg(LARRY)
    assert summary.viewbox == "0 0 512 512"
    assert summary.optional_states == ("expr-happy",)
    variant = character_variant_svg(
        LARRY, mouth="mouth-open", eyes="eyes-closed", expression="expr-happy"
    )
    text = variant.decode("utf-8")
    assert 'id="mouth-open" visibility="visible"' in text or 'visibility="visible"' in text
    assert text.count('display="none"') == 3  # mouth-closed, mouth-mid, eyes-open
    assert "<ns0:" not in text
    with pytest.raises(ValueError, match="unknown mouth"):
        character_variant_svg(LARRY, mouth="grin")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s: s.replace('viewBox="0 0 512 512"', 'viewBox="0 0 400 400"'), "viewBox"),
        (lambda s: s.replace("</svg>", '<text x="1" y="1">hi</text></svg>'), "<text>"),
        (lambda s: s.replace("</svg>", "<script>alert(1)</script></svg>"), "<script>"),
        (lambda s: s.replace("</svg>", '<image href="https://x/y.png"/></svg>'), "<image>"),
        (lambda s: s.replace('<g id="mouth-mid"', '<g id="mouth-medium"'), "mouth-mid"),
        (
            lambda s: s.replace('<g id="eyes-open">', '<g id="eyes-open" onclick="x()">'),
            "event handler",
        ),
        (lambda s: s.replace("url(#soft)", "url(https://evil/f)"), "external URL"),
        (lambda s: s.replace("<style>", "<style>@import url(x);"), "external content"),
        (lambda s: '<!DOCTYPE svg [<!ENTITY a "b">]>' + s, "DOCTYPE"),
        (
            lambda s: s.replace('<g id="mouth-closed">', '<path id="mouth-closed"/><g id="mc">'),
            "must be a <g>",
        ),
    ],
)
def test_character_contract_rejects_unsafe_or_incomplete_files(mutation, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_character_svg(mutation(LARRY.decode("utf-8")).encode("utf-8"))


def test_demo_video_contract_reads_duration_and_track_shape() -> None:
    video = validate_demo_video(_mp4())
    assert video.duration_seconds == 20.0
    assert (video.width, video.height) == (1080, 1920)
    with pytest.raises(ValueError, match="8-90"):
        validate_demo_video(_mp4(seconds=4))
    with pytest.raises(ValueError, match="portrait"):
        validate_demo_video(_mp4(width=1920, height=1080))
    with pytest.raises(ValueError, match="voice track"):
        validate_demo_video(_mp4(audio=False))
    with pytest.raises(ValueError, match="faststart"):
        validate_demo_video(_mp4(faststart=False))
    with pytest.raises(ValueError, match="ftyp"):
        validate_demo_video(b"\x00" * 100)


def _definition(*, media_type: str, path_template: str, validator: str, max_bytes: int) -> dict:
    return {
        "key": "creative.x",
        "executor": "codex.procedure",
        "procedure": {
            "prompt_path": "procedures/creative.x/PROMPT.md",
            "skills_path": "procedures/creative.x/skills",
            "skill_files": ["procedures/creative.x/skills/studio/SKILL.md"],
            "entry_skill": "studio",
            "workspace": {"kind": "project.state"},
            "output": {
                "kind": "project.artifact",
                "path_template": path_template,
                "media_type": media_type,
                "max_bytes": max_bytes,
                "validator": validator,
            },
            "sandbox": {"profile": "studio", "timeout_seconds": 3600, "egress": "open"},
        },
    }


def test_procedure_contract_bounds_binary_and_character_outputs() -> None:
    spec = validate_codex_procedure_definition(
        _definition(
            media_type="video/mp4",
            path_template="demos/{slug}.mp4",
            validator=DEMO_VIDEO_VALIDATOR,
            max_bytes=16_000_000,
        )
    )
    assert spec.sandbox.studio and spec.sandbox.open_egress
    assert spec.output_media_type == "video/mp4"
    with pytest.raises(ValueError, match="max_bytes"):
        validate_codex_procedure_definition(
            _definition(
                media_type="video/mp4",
                path_template="demos/{slug}.mp4",
                validator=DEMO_VIDEO_VALIDATOR,
                max_bytes=16_000_001,
            )
        )
    with pytest.raises(ValueError, match="max_bytes"):
        validate_codex_procedure_definition(
            _definition(
                media_type="text/markdown",
                path_template="diagrams/{slug}.mmd",
                validator="tin-diagram.v1",
                max_bytes=2_000_000,
            )
        )
    with pytest.raises(ValueError, match="characters/"):
        validate_codex_procedure_definition(
            _definition(
                media_type="image/svg+xml",
                path_template="assets/{slug}.svg",
                validator=CHARACTER_SVG_VALIDATOR,
                max_bytes=64_000,
            )
        )
    with pytest.raises(ValueError, match="require a validator"):
        validate_codex_procedure_definition(
            {
                **_definition(
                    media_type="video/mp4",
                    path_template="demos/{slug}.mp4",
                    validator=DEMO_VIDEO_VALIDATOR,
                    max_bytes=16_000_000,
                ),
                "procedure": {
                    **_definition(
                        media_type="video/mp4",
                        path_template="demos/{slug}.mp4",
                        validator=DEMO_VIDEO_VALIDATOR,
                        max_bytes=16_000_000,
                    )["procedure"],
                    "output": {
                        "kind": "project.artifact",
                        "path": "demos/x.mp4",
                        "media_type": "video/mp4",
                        "max_bytes": 16_000_000,
                    },
                },
            }
        )


def test_procedure_artifact_validation_dispatches_binary_before_decoding() -> None:
    video_spec = PinnedCodexProcedure(
        workflow_key="creative.product_demo",
        prompt="p",
        entry_skill="s",
        skill_files={"s/SKILL.md": b"---\nname: s\n---\n"},
        output_path="demos/x.mp4",
        output_media_type="video/mp4",
        output_validator=DEMO_VIDEO_VALIDATOR,
        output_max_bytes=16_000_000,
    )
    validate_procedure_artifact(_mp4(), spec=video_spec)
    with pytest.raises(ValueError, match="portrait"):
        validate_procedure_artifact(_mp4(width=720, height=1280), spec=video_spec)
    character_spec = PinnedCodexProcedure(
        workflow_key="creative.character",
        prompt="p",
        entry_skill="s",
        skill_files={"s/SKILL.md": b"---\nname: s\n---\n"},
        output_path="characters/larry.svg",
        output_media_type="image/svg+xml",
        output_validator=CHARACTER_SVG_VALIDATOR,
        output_max_bytes=64_000,
    )
    validate_procedure_artifact(LARRY, spec=character_spec)
    with pytest.raises(ValueError, match="mouth-open"):
        validate_procedure_artifact(
            LARRY.replace(b'id="mouth-open"', b'id="mo"'), spec=character_spec
        )


def test_output_checkpoint_accepts_a_binary_video_without_decoding() -> None:
    from tin_lite.publication import OutputCheckpoint

    video = _mp4()
    run = SimpleNamespace(
        id=RUN_ID,
        project_id=PROJECT_ID,
        generation=2,
        definition_commit_sha="a" * 40,
        expected_head_sha="b" * 40,
    )
    checkpoint = OutputCheckpoint.create(
        run=run, revision="c" * 40, path="demos/x.mp4", media_type="video/mp4", content=video
    )
    checkpoint.validate_content(video)
    big = OutputCheckpoint.load({**checkpoint.to_dict(), "byte_count": 15_000_000}, run=run)
    assert big.binary and big.max_bytes == 16_000_000
    with pytest.raises(ValueError, match="validated checkpoint"):
        OutputCheckpoint.load({**checkpoint.to_dict(), "byte_count": 16_000_001}, run=run)
    text = OutputCheckpoint.create(
        run=run, revision="c" * 40, path="r.md", media_type="text/markdown", content=b"# hi\n"
    )
    with pytest.raises(ValueError, match="validated checkpoint"):
        OutputCheckpoint.load({**text.to_dict(), "byte_count": 1_000_001}, run=run)
    with pytest.raises(UnicodeDecodeError):
        OutputCheckpoint.create(
            run=run, revision="c" * 40, path="r.md", media_type="text/markdown", content=b"\xff\xfe"
        ).validate_content(b"\xff\xfe")


def test_provider_detail_keeps_the_reason_and_drops_the_echoed_prompt() -> None:
    from tin_lite.studio import _provider_detail

    response = httpx.Response(
        422,
        json={
            "detail": [
                {
                    "loc": ["body", "prompt"],
                    "msg": "The content could not be processed because it was flagged.",
                    "type": "content_policy_violation",
                    "input": {"prompt": "the narrated line"},
                }
            ]
        },
    )
    detail = _provider_detail(response)
    assert detail.startswith("content_policy_violation: The content could not be processed")
    assert "the narrated line" not in detail
    assert _provider_detail(httpx.Response(500, content=b"<html>")) == "no detail"


def test_creative_studio_workflows_are_registered_with_studio_sandboxes() -> None:
    assert any(system.id == CREATIVE_STUDIO_SYSTEM for system in WORKFLOW_SYSTEMS)
    workflows = {item.key: item for item in BUILTIN_WORKFLOWS}
    character = workflows[CREATIVE_CHARACTER_WORKFLOW_NAME].definition
    demo = workflows[CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME].definition
    for definition in (character, demo):
        assert definition["system"] == CREATIVE_STUDIO_SYSTEM
        assert definition["human_review"]["eligible"] is True
        assert definition["schedule_modes"] == ["on_demand"]
        assert "integration_requirements" not in definition
    # The character is a native model workflow; only the demo needs the studio sandbox.
    assert "procedure" not in character
    assert character["model_route"]["model"] == "gpt-6-sol"
    assert demo["procedure"]["sandbox"]["profile"] == STUDIO_SANDBOX_PROFILE
    assert demo["procedure"]["sandbox"]["egress"] == "open"
    assert demo["procedure"]["output"] == {
        "kind": "project.artifact",
        "path_template": "demos/{slug}.mp4",
        "media_type": "video/mp4",
        "max_bytes": 16_000_000,
        "validator": DEMO_VIDEO_VALIDATOR,
    }
    assert normalize_workflow_inputs(
        schema=demo["input_schema"],
        project_id=PROJECT_ID,
        inputs={"slug": "launch", "product_url": "https://example.com/"},
    ) == {
        "slug": "launch",
        "product_url": "https://example.com/",
        "angle": "",
        "character": "",
        "voice": "Kore",
        "backdrop": "mesh",
        "language": "English (US)",
        "notes": "",
    }
    assert normalize_workflow_inputs(
        schema=character["input_schema"], project_id=PROJECT_ID, inputs={"slug": "larry"}
    ) == {"slug": "larry", "brief": "", "product_url": "", "notes": ""}


def test_studio_template_pins_ffmpeg_resvg_and_the_toolkit(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "tin_lite_sandbox_template_studio", ROOT / "sandbox" / "template.py"
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
    module.studio_template()
    copied = {args[:2] for name, args in calls if name == "copy"}
    assert ("sandbox/camoufox_mcp.py", "/opt/tin-lite/camoufox-mcp") in copied
    assert ("sandbox/tin_studio.sh", "/opt/tin-lite/studio/tin-studio") in copied
    for name in module.STUDIO_FILES:
        assert (f"sandbox/studio/{name}", f"/opt/tin-lite/studio/{name}") in copied
        assert (ROOT / "sandbox" / "studio" / name).is_file()
    commands = "\n".join(str(args[0]) for name, args in calls if name == "run_cmd")
    assert f"ffmpeg={module.FFMPEG_PACKAGE_VERSION}" in commands
    assert module.RESVG_SHA256 in commands and module.RESVG_VERSION in commands
    assert f"pillow=={module.PILLOW_PACKAGE_VERSION}" in commands
    assert f"numpy=={module.NUMPY_PACKAGE_VERSION}" in commands
    assert "google-chrome" not in str(calls)
    assert module.TEMPLATES["tin-lite-codex-studio"][1] == 4


def test_sandbox_runner_fences_fal_and_wires_the_studio_profile() -> None:
    runner = (ROOT / "sandbox" / "run_procedure.sh").read_text()
    bridge = (ROOT / "sandbox" / "procedure_app_server.py").read_text()
    assert '-n "${FAL_KEY:-}"' in runner
    assert "-u FAL_KEY" in runner
    assert 'TIN_PROCEDURE_STUDIO:-}" == "1"' in runner
    assert "/opt/tin-lite/studio/tin-studio" in runner
    assert "requires the run-bound studio voice grant" in runner
    assert '"FAL_KEY",' in bridge
    assert 'os.environ.get("TIN_PROCEDURE_STUDIO") == "1"' in bridge
    assert "tin-studio" in bridge
    toolkit = "\n".join(path.read_text() for path in (ROOT / "sandbox" / "studio").glob("*.py"))
    assert "FAL_KEY" not in toolkit
    assert "fal.run" not in toolkit
    assert "TIN_RUN_TOOLS_GRANT" in toolkit


def _load_studio_tool(name: str, monkeypatch: pytest.MonkeyPatch):
    # The browser and imaging stacks exist only in the studio sandbox; the step bookkeeping
    # under test never calls them.
    camoufox = ModuleType("camoufox.sync_api")
    camoufox.Camoufox = object
    pil = ModuleType("PIL")
    for attr in ("Image", "ImageDraw", "ImageFilter", "ImageFont"):
        setattr(pil, attr, SimpleNamespace())
    monkeypatch.setitem(sys.modules, "camoufox", ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", camoufox)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location(
        f"tin_lite_sandbox_studio_{name}", ROOT / "sandbox" / "studio" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_voice_pairs_each_script_step_with_its_own_keyframe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = _load_studio_tool("capture", monkeypatch)
    voice = _load_studio_tool("voice", monkeypatch)

    class Browser:
        vw, vh, dpr = 390, 693, 2.77

        def __init__(self, script):
            self.url = ""
            self.page = SimpleNamespace(
                screenshot=lambda path, **kwargs: None,
                keyboard=SimpleNamespace(insert_text=lambda ch: None),
            )

        def goto(self, url, settle_ms=None):
            self.url = url

        def settle(self, ms=None):
            pass

        def evaluate(self, expression):
            return 0

        def locate(self, selector):
            return {"x": 100, "y": 200}

        def click_at(self, x, y):
            pass

        def state(self):
            return {"url": self.url, "scrollY": 0}

        def close(self):
            pass

    # A type step logs focus and typing keyframes, none of which is a goto/scroll/click/hold.
    script = {
        "steps": [
            {"goto": "https://example.com/", "label": "home", "vo": "Line A"},
            {"type": {"selector": "#q", "text": "shoes"}, "label": "search", "vo": "Line B"},
            {"click": {"x": 50, "y": 60}, "label": "results", "vo": "Line C"},
        ]
    }
    (tmp_path / "in.json").write_text(json.dumps(script))
    monkeypatch.setattr(capture, "Session", Browser)
    capture.capture(str(tmp_path / "in.json"), str(tmp_path))

    requested: list[str] = []

    def request_voice(text, *args):
        requested.append(text)
        return {"audio_base64": base64.b64encode(b"mp3").decode(), "words": []}

    monkeypatch.setattr(voice, "request_voice", request_voice)
    monkeypatch.setattr(voice, "probe_duration", lambda path: 1.5)
    monkeypatch.setattr(sys, "argv", ["voice.py", str(tmp_path)])
    voice.main()

    log = json.loads((tmp_path / "log.json").read_text())
    keyframes = {step["idx"]: (step["kind"], step["label"]) for step in log["steps"]}
    clips = json.loads((tmp_path / "vo.json").read_text())["clips"]
    assert requested == ["Line A", "Line B", "Line C"]
    assert [(keyframes[clip["step_idx"]], clip["text"]) for clip in clips] == [
        (("goto", "home"), "Line A"),
        (("focus", "search"), "Line B"),
        (("click", "results"), "Line C"),
    ]


def test_render_plays_a_type_step_line_on_its_focus_keyframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = _load_studio_tool("render", monkeypatch)
    log = {
        "steps": [
            {"idx": 0, "kind": "goto", "hold": 1200},
            {"idx": 1, "kind": "focus", "hold": 300},
            {"idx": 2, "kind": "typing", "hold": 70},
        ]
    }
    vo = {"clips": [{"step_idx": 1, "duration": 2.0, "words": []}]}
    segs, total, audio = render.build_timeline(log, vo)
    holds = {seg["key"]["idx"]: seg for seg in segs if seg["kind"] == "hold"}
    assert [(round(start, 2), clip["step_idx"]) for start, clip in audio] == [(1.87, 1)]
    assert holds[1]["start"] + holds[1]["dur"] >= 1.87 + 2.0
    assert holds[2]["dur"] == pytest.approx(0.07)
    assert total == pytest.approx(4.39)


def test_render_stretches_a_hold_step_until_its_line_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = _load_studio_tool("render", monkeypatch)
    log = {
        "steps": [{"idx": 0, "kind": "goto", "hold": 1200}, {"idx": 1, "kind": "hold", "hold": 500}]
    }
    vo = {"clips": [{"step_idx": 1, "duration": 4.0, "words": []}]}
    segs, total, audio = render.build_timeline(log, vo)
    holds = {seg["key"]["idx"]: seg for seg in segs if seg["kind"] == "hold"}
    assert [(round(start, 2), clip["step_idx"]) for start, clip in audio] == [(1.87, 1)]
    assert holds[1]["start"] + holds[1]["dur"] >= 1.87 + 4.0
    assert total == pytest.approx(6.2)


def test_e2b_runtime_selects_the_studio_template_and_flags_the_profile() -> None:
    runtime = E2BRuntime(
        api_key="k",
        template="tin-lite-codex",
        browser_template="tin-lite-codex-browser",
        studio_template="tin-lite-codex-studio",
        timeout_seconds=9,
        egress_allow_hosts=(),
    )
    assert runtime.template_for(SandboxProfile("studio", 3600, "open")) == "tin-lite-codex-studio"
    assert runtime.template_for(SandboxProfile("browser", 1800, "open")) == "tin-lite-codex-browser"
    with pytest.raises(RuntimeError, match="studio template"):
        E2BRuntime(
            api_key="k", template="tin-lite-codex", timeout_seconds=9, egress_allow_hosts=()
        ).template_for(SandboxProfile("studio", 3600, "open"))


class FakeStudioDatabase:
    def __init__(self, *, lines: int = 0, characters: int = 0) -> None:
        self.receipts: dict[str, dict] = {}
        self.usage = StudioUsage(lines=lines, characters=characters)
        self.failed: list[str] = []
        self.statuses = {}

    @asynccontextmanager
    async def effect_lock(self, execution_key: str, operation: str, *, conn=None):
        from contextlib import nullcontext

        existing = self.receipts.get(execution_key)
        yield (
            SimpleNamespace(transaction=nullcontext),
            (
                SimpleNamespace(status=self.statuses[execution_key], result=existing)
                if existing
                else None
            ),
        )

    async def start_effect(self, conn, *, execution_key: str, operation: str) -> None:
        self.receipts.setdefault(execution_key, {})
        self.statuses.setdefault(execution_key, "started")

    async def get_effect(self, key, *, conn=None):
        if key not in self.receipts:
            return None
        return SimpleNamespace(status=self.statuses[key], result=self.receipts[key])

    async def save_effect_progress(self, conn, *, execution_key, result):
        self.receipts[execution_key] = result

    async def complete_effect(self, conn, *, execution_key: str, result: dict) -> None:
        self.receipts[execution_key] = result
        self.statuses[execution_key] = "completed"

    async def fail_effect(self, conn, *, execution_key: str, error_message: str) -> None:
        self.failed.append(error_message)

    async def studio_voice_usage(self, run_id: UUID, *, conn=None) -> StudioUsage:
        return self.usage


def _studio_settings(**overrides) -> SimpleNamespace:
    values = {
        "fal_key": SecretStr("fal-secret"),
        "studio_max_voice_lines_per_run": 24,
        "studio_max_voice_characters_per_run": 3000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fal_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "fal.run" and request.url.path.endswith("gemini-3.1-flash-tts"):
            assert request.headers["authorization"] == "Key fal-secret"
            body = json.loads(request.content)
            assert body["output_format"] == "mp3" and body["voice"] == "Kore"
            return httpx.Response(200, json={"audio": {"url": "https://cdn.fal.test/a.mp3"}})
        if request.url.host == "cdn.fal.test":
            return httpx.Response(
                200, content=b"ID3mp3bytes", headers={"content-type": "audio/mpeg"}
            )
        if request.url.host == "fal.run" and request.url.path.endswith("whisper"):
            body = json.loads(request.content)
            assert body["audio_url"] == "https://cdn.fal.test/a.mp3"
            assert body["chunk_level"] == "word"
            return httpx.Response(
                200,
                json={
                    "chunks": [
                        {"timestamp": [0.0, 0.4], "text": " Meet "},
                        {"timestamp": [0.4, 0.9], "text": "Claw"},
                    ]
                },
            )
        return httpx.Response(500)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_studio_voice_records_one_receipt_and_replays_it() -> None:
    seen: list[httpx.Request] = []
    database = FakeStudioDatabase()
    service = StudioService(
        settings=_studio_settings(),
        database=database,
        client=httpx.AsyncClient(transport=_fal_transport(seen)),
    )
    result = await service.voice(
        run_id=RUN_ID,
        request_id="abc123",
        text="Meet   Claw.",
        voice="Kore",
        style="",
        language_code="English (US)",
        transcription_language="en",
    )
    assert result.audio == b"ID3mp3bytes" and result.media_type == "audio/mpeg"
    assert result.words == [
        {"word": "Meet", "start": 0.0, "end": 0.4},
        {"word": "Claw", "start": 0.4, "end": 0.9},
    ]
    assert result.characters == len("Meet Claw.") and not result.replayed
    receipt = database.receipts[f"{RUN_ID}:studio-voice:abc123"]
    assert receipt["characters"] == 10 and receipt["audio_url"] == "https://cdn.fal.test/a.mp3"
    assert "fal-secret" not in json.dumps(receipt)
    assert len(seen) == 3
    replay = await service.voice(
        run_id=RUN_ID,
        request_id="abc123",
        text="Meet Claw.",
        voice="Kore",
        style="",
        language_code="English (US)",
        transcription_language="en",
    )
    assert replay.replayed and replay.audio == b"ID3mp3bytes"
    assert len(seen) == 4  # only the audio download, no new synthesis
    await service.close()


@pytest.mark.asyncio
async def test_studio_voice_enforces_quotas_validation_and_configuration() -> None:
    seen: list[httpx.Request] = []
    exhausted = StudioService(
        settings=_studio_settings(),
        database=FakeStudioDatabase(lines=24),
        client=httpx.AsyncClient(transport=_fal_transport(seen)),
    )
    with pytest.raises(StudioError, match="voice lines"):
        await exhausted.voice(
            run_id=RUN_ID,
            request_id="r1",
            text="hi",
            voice="Kore",
            style="",
            language_code="",
            transcription_language="",
        )
    budget = StudioService(
        settings=_studio_settings(),
        database=FakeStudioDatabase(characters=2995),
        client=httpx.AsyncClient(transport=_fal_transport(seen)),
    )
    with pytest.raises(StudioError, match="character budget"):
        await budget.voice(
            run_id=RUN_ID,
            request_id="r1",
            text="hello there",
            voice="Kore",
            style="",
            language_code="",
            transcription_language="",
        )
    service = StudioService(
        settings=_studio_settings(),
        database=FakeStudioDatabase(),
        client=httpx.AsyncClient(transport=_fal_transport(seen)),
    )
    with pytest.raises(StudioError, match="voice must be one of"):
        await service.voice(
            run_id=RUN_ID,
            request_id="r1",
            text="hi",
            voice="Nobody",
            style="",
            language_code="",
            transcription_language="",
        )
    with pytest.raises(StudioError, match="request_id"):
        await service.voice(
            run_id=RUN_ID,
            request_id="../x",
            text="hi",
            voice="Kore",
            style="",
            language_code="",
            transcription_language="",
        )
    with pytest.raises(StudioError, match="1-400"):
        await service.voice(
            run_id=RUN_ID,
            request_id="r1",
            text="x" * 401,
            voice="Kore",
            style="",
            language_code="",
            transcription_language="",
        )
    assert seen == []
    disabled = StudioService(
        settings=_studio_settings(fal_key=None),
        database=FakeStudioDatabase(),
        client=httpx.AsyncClient(transport=_fal_transport(seen)),
    )
    assert not disabled.enabled
    with pytest.raises(StudioError, match="not configured"):
        await disabled.voice(
            run_id=RUN_ID,
            request_id="r1",
            text="hi",
            voice="Kore",
            style="",
            language_code="",
            transcription_language="",
        )


class GrantDatabase:
    def __init__(self, provider_key: str = STUDIO_PROVIDER) -> None:
        self.provider_key = provider_key

    async def authorize_run_tool_grant(self, *, token: str, capability: str | None = None):
        if token != GRANT or (capability is not None and capability != STUDIO_VOICE_CAPABILITY):
            return None
        return RunToolGrant(
            project_id=PROJECT_ID,
            run_id=RUN_ID,
            connection_id=None,
            external_account_id="",
            sandbox_id="sbx-studio",
            provider_key=self.provider_key,
            capabilities=(STUDIO_VOICE_CAPABILITY,),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


class FakeStudio:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def voice(self, **values):
        self.calls.append(values)
        if values["text"] == "boom":
            raise StudioError("this run has used all of its voice lines")
        return StudioVoice(
            audio=b"mp3", media_type="audio/mpeg", words=[], characters=3, replayed=False
        )


@pytest.mark.asyncio
async def test_studio_voice_route_requires_the_studio_grant_and_returns_audio() -> None:
    studio = FakeStudio()
    _server, app = create_run_tools_app(
        settings=SimpleNamespace(switchboard_public_url="https://tin.test"),
        runtime=lambda: SimpleNamespace(database=GrantDatabase(), studio=studio),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        denied = await client.post("/studio/voice", json={"text": "hi"})
        assert denied.status_code == 401
        wrong = await client.post(
            "/studio/voice", json={"text": "hi"}, headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 403
        ok = await client.post(
            "/studio/voice",
            json={"request_id": "r1", "text": "hi", "voice": "Puck"},
            headers={"Authorization": f"Bearer {GRANT}"},
        )
        assert ok.status_code == 200
        payload = ok.json()
        assert base64.b64decode(payload["audio_base64"]) == b"mp3"
        assert payload["media_type"] == "audio/mpeg" and payload["characters"] == 3
        assert studio.calls[0]["run_id"] == RUN_ID and studio.calls[0]["voice"] == "Puck"
        quota = await client.post(
            "/studio/voice",
            json={"request_id": "r2", "text": "boom"},
            headers={"Authorization": f"Bearer {GRANT}"},
        )
        assert quota.status_code == 422 and "voice lines" in quota.json()["error"]
        huge = await client.post(
            "/studio/voice",
            content=b"x" * 9000,
            headers={"Authorization": f"Bearer {GRANT}", "content-type": "application/json"},
        )
        assert huge.status_code == 413
    workspace_only = GrantDatabase(provider_key="workspace.google")
    _server, app = create_run_tools_app(
        settings=SimpleNamespace(switchboard_public_url="https://tin.test"),
        runtime=lambda: SimpleNamespace(database=workspace_only, studio=studio),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        response = await client.post(
            "/studio/voice", json={"text": "hi"}, headers={"Authorization": f"Bearer {GRANT}"}
        )
        assert response.status_code == 403
