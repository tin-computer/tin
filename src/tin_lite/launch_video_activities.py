"""Bounded Anthropic film generation, offline rendering and the normal Files review."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.launch_video import (
    KEY,
    LaunchVideoError,
    LaunchVideoOutputError,
    model_route_definition,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.project_files import safe_project_file_path
from tin_lite.studio import DEFAULT_VOICE_STYLE, STUDIO_VOICES
from tin_lite.studio_contracts import validate_launch_video

MAX_ATTEMPTS = 3
MAX_ASSET_BYTES = 20_000_000
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def launch_path(slug: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 80:
        raise LaunchVideoError("the launch film name is invalid")
    return f"launches/{slug}.mp4"


def _approved_path(value: object, *, audio: bool = False) -> str:
    if not isinstance(value, str) or not safe_project_file_path(value):
        raise LaunchVideoError("an approved launch asset has an unsafe path")
    name = PurePosixPath(value).name
    suffixes = _AUDIO_SUFFIXES if audio else _ASSET_SUFFIXES
    if not _ASSET_NAME.fullmatch(name) or PurePosixPath(name).suffix.lower() not in suffixes:
        raise LaunchVideoError("the selected launch asset type is not supported")
    return value


def _problems(report: dict) -> list[str]:
    issues = report.get("issues")
    if not isinstance(issues, list) or not issues:
        return ["The offline film check did not pass."]
    return [str(item)[:500] for item in issues[:12]]


class LaunchVideoActivities:
    def __init__(self, *, database, storage, generator, sandboxes, studio) -> None:
        self.db, self.storage, self.generator = database, storage, generator
        self.sandboxes, self.studio = sandboxes, studio

    async def _run(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.executor != KEY:
            raise LaunchVideoError("this run is not a launch film")
        return run

    @activity.defn(name="launch_build")
    async def launch_build(self, run_id: str) -> None:
        try:
            await self.build(run_id)
        except LaunchVideoError as exc:
            raise ApplicationError(str(exc)[:500], non_retryable=True) from None

    async def build(self, run_id: str) -> None:
        if self.generator is None:
            raise LaunchVideoError("the Anthropic launch model is not configured")
        run = await self._run(run_id)
        key = f"{run.id}:launch:commit"
        if (committed := await self.db.get_effect(key)) and committed.status == "completed":
            return
        project = await self.db.get_project(run.project_id)
        if project is None:
            raise LaunchVideoError("project not found")
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path=f"workflows/{KEY}.json",
            )
        )
        if definition.get("model_route") != model_route_definition():
            raise LaunchVideoError("this worker does not serve the pinned launch model route")
        inputs = run.input or {}
        path = launch_path(str(inputs.get("slug", "")))
        await self.db.mark_run_running(run.id)
        await self._progress(run, "prepare", 0, "Reading the selected product and sound assets")
        context = await self._context(run, project, inputs)
        assets, music = await self._assets(project, context["source_revision"], inputs)
        # Even a silent film checks that the Studio image has the launch runtime
        # before buying a model draft.
        cues = await self.sandboxes.analyse_launch_music(
            run_id=str(run.id), music=music, duration_seconds=inputs["duration_seconds"]
        )
        previous = None
        problems = None
        for attempt in range(MAX_ATTEMPTS):
            await self._progress(run, "generate", 1, "Writing the launch film with Anthropic")
            draft = await self._generate(run, context, cues, attempt, previous, problems)
            if draft.get("invalid_output") is True:
                previous, problems = None, draft["problems"]
                continue
            render_key = f"{run.id}:launch:render:{attempt}"
            existing = await self.db.get_effect(render_key)
            if existing is not None and existing.status == "completed" and existing.result:
                previous, problems = draft, _problems(existing.result)
                continue
            voice, captions = await self._voice(run, draft)
            await self._progress(run, "render", 2, "Rendering and checking the film offline")
            result = await self.sandboxes.render_launch_video(
                run_id=str(run.id),
                attempt=attempt,
                html=draft["html"],
                assets=assets,
                duration_seconds=inputs["duration_seconds"],
                format=inputs["format"],
                music=music,
                voice=voice,
                captions=captions,
            )
            report = result["report"]
            if report.get("passed") is True:
                content = result["content"]
                validate_launch_video(content)
                await self._publish(run, project, path, content, report, attempt)
                return
            # Failed deterministic checks can be repaired. Failed provider/sandbox calls
            # raise instead, so an unavailable service does not buy another film draft.
            async with self.db.effect_lock(render_key, "launch_render_check") as (conn, existing):
                await self.db.start_effect(
                    conn, execution_key=render_key, operation="launch_render_check"
                )
                await self.db.complete_effect(
                    conn,
                    execution_key=render_key,
                    result={"passed": False, "issues": _problems(report)},
                )
            previous, problems = draft, _problems(report)
        raise LaunchVideoError("the film still failed its offline checks after two repairs")

    async def _progress(self, run, step, current, summary) -> None:
        await self.db.project_run_progress(
            run_id=run.id, mode="steps", step=step, current=current, total=4, summary=summary
        )

    async def _context(self, run, project, inputs) -> dict:
        key = f"{run.id}:launch:context"
        async with self.db.effect_lock(key, "launch_context") as (conn, existing):
            if existing is not None and existing.status == "completed" and existing.result:
                return existing.result
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
            if not revision:
                raise LaunchVideoError("the project has no saved source revision")
            await self.db.start_effect(conn, execution_key=key, operation="launch_context")
            context = {"project_name": project.name, "source_revision": revision}
            for path, limit in (("wiki/INDEX.md", 8000), ("BRAND.md", 6000), ("DESIGN.md", 6000)):
                raw = await self.storage.read_bounded_project_file(
                    repo_id=project.state_repo_id,
                    commit_sha=revision,
                    path=path,
                    max_bytes=100_000,
                )
                context[path] = (raw or b"")[:limit].decode("utf-8", errors="replace")
            context["asset_names"] = [
                PurePosixPath(_approved_path(path)).name for path in inputs.get("asset_paths", [])
            ]
            await self.db.complete_effect(conn, execution_key=key, result=context)
            return context

    async def _assets(self, project, revision, inputs) -> tuple[dict[str, bytes], bytes | None]:
        paths = inputs.get("asset_paths", [])
        if not isinstance(paths, list) or len(paths) > 12:
            raise LaunchVideoError("select at most twelve launch picture or font assets")
        assets = {}
        for item in paths:
            path = _approved_path(item)
            name = PurePosixPath(path).name
            if name in assets:
                raise LaunchVideoError("launch assets must have different file names")
            assets[name] = await self.storage.read_project_media(
                repo_id=project.state_repo_id, revision=revision, path=path
            )
            if sum(map(len, assets.values())) > MAX_ASSET_BYTES:
                raise LaunchVideoError("the selected launch pictures and fonts exceed 20 MB")
        music = None
        if inputs.get("sound_mode") != "silent":
            if not str(inputs.get("music_license", "")).strip():
                raise LaunchVideoError(
                    "confirm the selected music's usage rights before generation"
                )
            path = _approved_path(inputs.get("music_path"), audio=True)
            music = await self.storage.read_project_media(
                repo_id=project.state_repo_id, revision=revision, path=path
            )
            if not 0 < len(music) <= MAX_ASSET_BYTES:
                raise LaunchVideoError("the selected launch music must be under 20 MB")
        return assets, music

    async def _generate(self, run, context, cues, attempt, previous, problems) -> dict:
        key = f"{run.id}:launch:model:{attempt}"
        async with self.db.effect_lock(key, "launch_model") as (conn, existing):
            if existing is not None:
                if existing.status == "completed" and existing.result:
                    return existing.result
                raise LaunchVideoError(
                    "the previous Anthropic request is unconfirmed; it was not purchased again"
                )
            await self.db.start_effect(conn, execution_key=key, operation="launch_model")
            try:
                with model_usage_scope(run_id=run.id, step=f"launch:attempt:{attempt}", conn=conn):
                    draft = await self.generator.generate(
                        inputs=run.input or {},
                        context=context,
                        cues=cues,
                        previous=previous,
                        problems=problems,
                    )
            except LaunchVideoOutputError as exc:
                draft = {"invalid_output": True, "problems": [str(exc)[:500]]}
            except Exception:
                await self.db.fail_effect(
                    conn, execution_key=key, error_message="Anthropic launch generation failed"
                )
                raise LaunchVideoError("the Anthropic launch request did not complete") from None
            await self.db.complete_effect(conn, execution_key=key, result=draft)
            return draft

    async def _voice(self, run, draft) -> tuple[bytes | None, list[dict] | None]:
        inputs = run.input or {}
        if inputs.get("sound_mode") != "voice_and_music":
            return None, None
        line = " ".join(draft["narration"].split())
        voice = inputs.get("voice", STUDIO_VOICES[0])
        language = inputs.get("language", "English (US)")
        transcription_language = inputs.get("stt_language", "en")
        request_id = hashlib.sha256(
            json.dumps(
                [line, voice, DEFAULT_VOICE_STYLE, language, transcription_language],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        spoken = await self.studio.voice(
            run_id=run.id,
            request_id=request_id,
            text=line,
            voice=voice,
            style=DEFAULT_VOICE_STYLE,
            language_code=language,
            transcription_language=transcription_language,
        )
        return spoken.audio, spoken.words if inputs.get("captions") is True else None

    async def _publish(self, run, project, path, content, report, attempt) -> None:
        await self._progress(run, "publish", 3, "Saving the checked MP4 to project Files")
        key = f"{run.id}:launch:commit"
        async with self.db.effect_lock(key, "launch_commit") as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation="launch_commit")
            async with self.db.project_state_lock(conn, project.id):
                sha, changed = await self.storage.publish_state_document(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    path=path,
                    content=content,
                    workflow_key=KEY,
                    execution_key=key,
                    run_id=str(run.id),
                )
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "canonical_commit_sha": sha,
                    "artifact_path": path,
                    "changed": changed,
                    "repairs": attempt,
                    "warnings": report.get("warnings", [])[:24],
                    "model_route": model_route_definition(),
                },
            )

    async def _committed(self, run):
        receipt = await self.db.get_effect(f"{run.id}:launch:commit")
        if receipt is None or receipt.status != "completed" or not receipt.result:
            raise LaunchVideoError("the checked launch film has not been saved")
        project = await self.db.get_project(run.project_id)
        result = receipt.result
        return (
            project,
            result,
            (
                f"code.storage://{project.state_repo_id}@{result['canonical_commit_sha']}/"
                f"{result['artifact_path']}"
            ),
        )

    @activity.defn(name="launch_review")
    async def launch_review(self, run_id: str) -> bool:
        run = await self._run(run_id)
        project, result, ref = await self._committed(run)
        raw = await self.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=result["canonical_commit_sha"],
            path=result["artifact_path"],
        )
        validate_launch_video(raw)
        return await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=result["canonical_commit_sha"],
            artifact_ref=ref,
            artifact_path=result["artifact_path"],
            summary="Watch the launch film and check its product story, picture and sound.",
        )

    @activity.defn(name="launch_approval")
    async def launch_approval(self, run_id: str) -> None:
        await self.db.record_human_review(run_id=UUID(run_id), decision="approved")

    @activity.defn(name="launch_project")
    async def launch_project(self, run_id: str) -> None:
        run = await self._run(run_id)
        _, result, ref = await self._committed(run)
        await self.db.project_success(
            run_id=run.id,
            canonical_commit_sha=result["canonical_commit_sha"],
            artifact_ref=ref,
            artifact_path=result["artifact_path"],
        )

    @activity.defn(name="launch_failure")
    async def launch_failure(self, payload: dict[str, str]) -> None:
        await self.db.project_failure(
            run_id=UUID(payload["run_id"]),
            error_message=payload.get("reason", "the launch film did not finish"),
        )
