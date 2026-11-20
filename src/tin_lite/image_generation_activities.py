"""Temporal activities for `brand.image`: draw one image in the project's house style and keep it.

generate_image starts the run; the run reads STYLE.json, sends one request to the kind's fal
model through StudioService (receipts and usage as Studio voice keeps them), checks the ground,
the shape and stray text, draws once more when a check fails, and keeps the image in the
library. Only the run ID enters Temporal history; the prompt, references and bytes stay in
Postgres and Files.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.image_library import (
    DEFAULT_GROUND,
    KEY,
    ImageLibrary,
    ImageLibraryError,
    build_prompt,
    check_model,
    image_problems,
    kind_style,
    request_body,
)
from tin_lite.image_text_check import StrayTextChecker
from tin_lite.project_media import validate_media
from tin_lite.studio import StudioError

ATTEMPTS = 2
STEPS = 3


class ImageGenerationActivities:
    def __init__(
        self,
        *,
        database: Any,
        storage: Any,
        studio: Any,
        settings: Any = None,
        text_checker: Any = None,
    ) -> None:
        self.db = database
        self.storage = storage
        self.studio = studio
        self.text_checker = text_checker or StrayTextChecker(settings=settings)
        self.library = ImageLibrary(database=database, storage=storage, settings=settings)

    async def _progress(self, run_id: UUID, step: str, number: int, summary: str) -> None:
        await self.db.project_run_progress(
            run_id=run_id, mode="steps", step=step, current=number, total=STEPS, summary=summary
        )
        if activity.in_activity():
            activity.heartbeat({"step": step})

    @activity.defn(name="image_generate")
    async def generate(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(str(run_id)))
        if run is None or run.executor != KEY:
            raise ApplicationError("This image run is gone.", non_retryable=True)
        if run.status.value == "succeeded":
            return
        if run.status.value not in {"pending", "running"}:
            raise ApplicationError("This image run is no longer active.", non_retryable=True)
        actor = run.started_by_clerk_user_id
        if not actor or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise ApplicationError(
                "The member who asked for this image left the project.", non_retryable=True
            )
        await self.db.mark_run_running(run.id)
        inputs = run.input or {}
        kind = str(inputs["kind"])
        subject = str(inputs["subject"])
        size = str(inputs.get("size") or "square")
        ground = str(inputs.get("ground") or DEFAULT_GROUND[kind])
        tags = [str(tag) for tag in inputs.get("tags") or []]
        try:
            project = await self.library.project(run.project_id)
            snap = await self.library.snapshot(project)
            style = kind_style(snap.style, kind)
            if not style["style"]:
                raise ImageLibraryError(
                    f"The {kind} style is not set. Set it with set_image_style first."
                )
            missing = [path for path in style["references"] if path not in snap.paths]
            if missing:
                raise ImageLibraryError(
                    f"A reference image is no longer in Files: {missing[0]}. "
                    "Set the references again with set_image_style."
                )
            await self._progress(run.id, "draw", 0, f"Drawing {subject}"[:240])
            references = await self.library.references(project, snap.revision, style["references"])
            endpoint = check_model(style["model"] if references else style["model_text"])
            prompt = build_prompt(subject=subject, kind=kind, style=style["style"], ground=ground)
            body = request_body(
                endpoint=endpoint,
                prompt=prompt,
                size=size,
                ground=ground,
                kind=kind,
                references=references,
            )
            drawn = None
            problems: list[str] = []
            text_note: str | None = None
            for attempt in range(1, ATTEMPTS + 1):
                drawn = await self.studio.image(
                    run_id=run.id, request_id=f"a{attempt}", endpoint=endpoint, body=body
                )
                metadata = (
                    {"media_type": drawn.media_type}
                    if drawn.media_type == "image/svg+xml"
                    else validate_media(
                        "image.png" if drawn.media_type == "image/png" else "image.jpg",
                        drawn.content,
                        drawn.media_type,
                    )
                )
                problems = (
                    []
                    if drawn.media_type == "image/svg+xml"
                    else image_problems(
                        metadata=metadata, content=drawn.content, size=size, ground=ground
                    )
                )
                text_note = None
                if not problems:
                    # Only a drawing that passed the free checks is read for text.
                    text = await self.text_checker.check(
                        content=drawn.content, media_type=drawn.media_type, subject=subject
                    )
                    if text.problem:
                        problems.append(text.problem)
                    text_note = text.note
                if not problems:
                    break
                if attempt < ATTEMPTS:
                    await self._progress(run.id, "redraw", 1, f"Drawing again: {problems[0]}"[:240])
            assert drawn is not None
            await self._progress(run.id, "save", 2, "Saving the image in the library")
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg"}[
                drawn.media_type
            ]
            content: bytes | str = drawn.content
            if extension == "svg":
                try:
                    content = drawn.content.decode("utf-8")
                except UnicodeDecodeError:
                    raise ImageLibraryError("The vector image is not UTF-8 text.") from None
            saved = await self.library.save(
                project,
                actor,
                kind=kind,
                subject=subject,
                tags=tags,
                files={extension: content},
                details={
                    "model": endpoint,
                    "prompt": prompt,
                    "style_version": style["version"],
                    "references": style["references"][: len(references)],
                    "size": size,
                    "ground": ground,
                    "width": metadata.get("width"),
                    "height": metadata.get("height"),
                    # Kept, not refused: the second drawing was paid for and may still serve.
                    "checks": problems + ([text_note] if text_note else []),
                },
                client_id="brand.image",
                run_id=run.id,
            )
        except (ImageLibraryError, StudioError) as error:
            raise ApplicationError(str(error), non_retryable=True) from None
        image = saved["image"]
        files = image.get("files") or {}
        path = next(iter(files.values()), None) if files else None
        artifact_path = path or "brand/illustrations/INDEX.json"
        save_key = f"{run.id}:image_save"
        async with self.db.effect_lock(save_key, KEY) as (conn, receipt):
            if receipt is None:
                await self.db.start_effect(conn, execution_key=save_key, operation=KEY)
            if receipt is None or receipt.status != "completed":
                await self.db.complete_effect(
                    conn,
                    execution_key=save_key,
                    result={
                        "image_id": image["id"],
                        "revision": saved["revision"],
                        "path": path,
                    },
                )
        projection_key = f"{run.id}:image_projection"
        async with self.db.effect_lock(projection_key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            if receipt is None:
                await self.db.start_effect(conn, execution_key=projection_key, operation=KEY)
            await self.db.complete_image_projection(
                conn,
                execution_key=projection_key,
                run_id=run.id,
                canonical_commit_sha=saved["revision"],
                artifact_path=artifact_path,
                artifact_ref=(
                    f"code.storage://{project.state_repo_id}@{saved['revision']}/{artifact_path}"
                ),
                summary=f"Image saved: {image['id']}"[:240],
            )

    @activity.defn(name="image_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        await self.db.project_failure(
            run_id=run.id,
            **failure_projection(
                passed,
                "Tin could not draw this image. Nothing was saved. Try again.",
            ),
        )
