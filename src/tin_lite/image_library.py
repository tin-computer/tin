"""A project's image library: illustrations, icons, photos and diagrams in its house style.

Every image lives in project Files under `brand/illustrations/`, so file history and Files
work as they do for any other file:

- `brand/illustrations/<kind>/<slug>.png` (or `.jpg`, `.svg` for vector icons, `.mmd` for
  diagrams) is the image itself.
- `brand/illustrations/<kind>/<slug>.json` beside it holds the subject, tags, model, prompt,
  style version, author, time and status (saved or retired).
- `brand/illustrations/INDEX.json` lists every image. Search reads it.
- `brand/illustrations/STYLE.json` holds the house style for each kind: the style text, the
  reference images and the fal model. A missing model falls back to DEFAULT_MODELS.

A model draws illustrations, icons and photos. That runs as the agent-only `brand.image`
workflow, so it has a run, receipts and usage like Studio voice (image_generation_activities.py).
Diagrams skip the image models: the caller writes the tin-diagram source and Tin keeps it as a
`.mmd` file, which the dashboard draws with Tin's diagram renderer.

A discard within UNDO_WINDOW removes the image in a new commit; history keeps it. After that the
image stays and is marked retired.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4, uuid5

from tin_lite.code_storage import ProjectStateChangedError
from tin_lite.domain import Project
from tin_lite.project_files import (
    MAX_PROJECT_FILE_BYTES,
    ProjectFileMutation,
    ProjectFileService,
    StaleProjectRevisionError,
    safe_project_file_path,
)
from tin_lite.project_media import MAX_IMAGE_BYTES, validate_media

KEY = "brand.image"
WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000048")
ROOT = "brand/illustrations"
INDEX_PATH = f"{ROOT}/INDEX.json"
STYLE_PATH = f"{ROOT}/STYLE.json"
REFERENCE_FOLDER = f"{ROOT}/reference"
WIKI_INDEX_PATH = "wiki/INDEX.md"
WIKI_LINE = (
    "- `brand/illustrations/`: the image library (illustrations, icons, photos and "
    "diagrams in the house style). Search it with search_images before making a new image."
)

KINDS = ("illustration", "icon", "photo", "diagram")
DRAWN_KINDS = ("illustration", "icon", "photo")
GROUNDS = ("transparent", "light", "dark", "any")
DEFAULT_GROUND = {"illustration": "transparent", "icon": "transparent", "photo": "any"}
SIZES = {
    "square": (1024, 1024),
    "landscape": (1536, 1024),
    "portrait": (1024, 1536),
    "wide": (1536, 864),
}
ASPECTS = {"square": "1:1", "landscape": "3:2", "portrait": "2:3", "wide": "16:9"}
# fal's named sizes for the seedream, gpt-image and recraft families (checked Nov 19).
PRESETS = {
    "square": "square_hd",
    "landscape": "landscape_4_3",
    "portrait": "portrait_4_3",
    "wide": "landscape_16_9",
}

# The fal model for each kind when STYLE.json names none. `model` takes the style's reference
# images; `model_text` draws from words alone when a kind has no references yet. These follow
# the suggested picks from the Nov 19 bake-off on Databasin and ZeroTB (illustrations and photos
# Nano Banana 2.1, icons GPT Image 2.6). Emre's pick is pending: change one line here, or set
# `model` per project with set_image_style.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "illustration": {
        "model": "google/nano-banana-2.1/edit",
        "model_text": "google/nano-banana-2.1",
    },
    "icon": {
        "model": "openai/gpt-image-2.6/flare/edit",
        "model_text": "openai/gpt-image-2.6/flare/text-to-image",
    },
    "photo": {
        "model": "google/nano-banana-2.1/edit",
        "model_text": "google/nano-banana-2.1",
    },
}
# Request shapes Tin knows how to send. A model whose endpoint names none of these is refused.
MODEL_FAMILIES = ("nano-banana", "seedream", "gpt-image", "recraft")
_ENDPOINT = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*){1,4}$")

# The pinned input of one brand.image run. generate_image fills it after its own checks.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "subject": {"type": "string", "minLength": 1, "maxLength": 300},
        "kind": {"type": "string", "enum": ["illustration", "icon", "photo"]},
        "size": {"type": "string", "enum": ["square", "landscape", "portrait", "wide"]},
        "ground": {"type": "string", "enum": ["transparent", "light", "dark", "any"]},
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 40},
            "maxItems": 12,
            "default": [],
        },
    },
    "required": ["project_id", "subject", "kind", "size", "ground"],
}

UNDO_WINDOW = timedelta(hours=48)
DOWNLOAD_LINK_LIFETIME = timedelta(minutes=15)
MAX_SUBJECT = 300
MAX_TAGS = 12
MAX_TAG = 40
MAX_STYLE_TEXT = 4_000
MAX_REFERENCES = 20
# References sent with one request; the rest stay on record for people to look at.
MAX_SENT_REFERENCES = 4
MAX_SEARCH_RESULTS = 50
COMMIT_TRIES = 4
_SLUG_MAX = 60
_ID = re.compile(r"^(illustration|icon|photo|diagram)/[a-z0-9]+(?:-[a-z0-9]+)*$")


class ImageLibraryError(Exception):
    """Why Tin did not do it, in words the agent can act on, with the HTTP status."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


# The contract, without storage.


def slugify(subject: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
    return words[:_SLUG_MAX].rstrip("-") or "image"


def check_kind(kind: str, *, drawn: bool = False) -> str:
    allowed = DRAWN_KINDS if drawn else KINDS
    if kind not in allowed:
        raise ImageLibraryError(f"kind must be one of {', '.join(allowed)}")
    return kind


def check_subject(subject: str) -> str:
    cleaned = " ".join(str(subject or "").split())
    if not cleaned or len(cleaned) > MAX_SUBJECT:
        raise ImageLibraryError(
            f"Give a concrete subject of 1-{MAX_SUBJECT} characters: an object or an action, "
            "like 'a hand turning a brass key'."
        )
    return cleaned


def check_tags(tags: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for tag in tags or []:
        word = " ".join(str(tag).lower().split())
        if not word or len(word) > MAX_TAG:
            raise ImageLibraryError(f"each tag must have 1-{MAX_TAG} characters")
        if word not in cleaned:
            cleaned.append(word)
    if len(cleaned) > MAX_TAGS:
        raise ImageLibraryError(f"give at most {MAX_TAGS} tags")
    return cleaned


def check_image_id(image_id: str) -> str:
    if not isinstance(image_id, str) or not _ID.fullmatch(image_id):
        raise ImageLibraryError(
            "image_id is the kind and slug, like illustration/hand-turning-key; "
            "search_images returns it."
        )
    return image_id


def model_family(endpoint: str) -> str | None:
    return next((family for family in MODEL_FAMILIES if family in endpoint), None)


def check_model(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint):
        raise ImageLibraryError("model must be a fal endpoint id, like google/nano-banana-2.1/edit")
    if model_family(endpoint) is None:
        raise ImageLibraryError(
            f"Tin sends requests to {', '.join(MODEL_FAMILIES)} models; {endpoint} is none of them"
        )
    return endpoint


def empty_style() -> dict[str, Any]:
    return {"version": 1, "kinds": {}}


def parse_style(content: bytes | None) -> dict[str, Any]:
    if not content:
        return empty_style()
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ImageLibraryError(
            f"{STYLE_PATH} is not valid JSON; fix it in Files or set the style again"
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get("kinds", {}), dict):
        raise ImageLibraryError(f"{STYLE_PATH} must be an object with a `kinds` object")
    data.setdefault("version", 1)
    data.setdefault("kinds", {})
    return data


def kind_style(style: dict[str, Any], kind: str) -> dict[str, Any]:
    """One kind's style as generation uses it, with the server's model where none is set."""
    saved = style.get("kinds", {}).get(kind) or {}
    defaults = DEFAULT_MODELS.get(kind, {})
    return {
        "kind": kind,
        "style": str(saved.get("style") or ""),
        "references": [str(path) for path in saved.get("references") or []],
        "model": str(saved.get("model") or defaults.get("model") or ""),
        "model_text": str(saved.get("model_text") or defaults.get("model_text") or ""),
        "model_is_default": not saved.get("model"),
        "version": int(saved.get("version") or 0),
        "updated_at": saved.get("updated_at"),
        "updated_by": saved.get("updated_by"),
    }


def parse_index(content: bytes | None) -> dict[str, Any]:
    if not content:
        return {"version": 1, "images": []}
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ImageLibraryError(f"{INDEX_PATH} is not valid JSON; fix it in Files") from None
    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        raise ImageLibraryError(f"{INDEX_PATH} must be an object with an `images` list")
    return data


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def search(
    index: dict[str, Any],
    query: str = "",
    *,
    kind: str | None = None,
    include_retired: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Images whose subject, tags or name hold the query's words, best match first."""
    words = [word for word in re.split(r"[^a-z0-9]+", query.lower()) if len(word) > 1]
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in index.get("images", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        if kind and entry.get("kind") != kind:
            continue
        if entry.get("status") == "retired" and not include_retired:
            continue
        subject = str(entry.get("subject", "")).lower()
        tags = " ".join(str(tag) for tag in entry.get("tags") or []).lower()
        name = entry["id"].replace("-", " ")
        score = 0
        for word in words:
            score += 3 * (word in subject.split()) + 2 * (word in tags.split())
            score += (word in subject) + (word in tags) + (word in name)
        if words and score == 0:
            continue
        scored.append((score, str(entry.get("created_at", "")), entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[: max(1, min(limit, MAX_SEARCH_RESULTS))]]


def png_has_alpha(content: bytes) -> bool:
    """Whether a PNG can hold a transparent ground: an alpha channel or a tRNS chunk."""
    if not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) < 33:
        return False
    if content[25] in {4, 6}:
        return True
    offset = 8
    while offset + 8 <= len(content):
        length, kind = struct.unpack(">I4s", content[offset : offset + 8])
        if kind == b"tRNS":
            return True
        if kind in {b"IDAT", b"IEND"}:
            return False
        offset += 12 + length
    return False


def image_problems(
    *, metadata: dict[str, Any], content: bytes, size: str, ground: str
) -> list[str]:
    """What a drawing gets wrong that Tin can check without a model: the ground and the shape.

    Stray text and made-up signatures need a vision check, which is not built yet."""
    problems: list[str] = []
    if ground == "transparent" and not (
        metadata.get("media_type") == "image/png" and png_has_alpha(content)
    ):
        problems.append("the ground is not transparent")
    width, height = SIZES[size]
    got = metadata.get("width", 0) / max(1, metadata.get("height", 1))
    if abs(got - width / height) > 0.06 * (width / height):
        problems.append(
            f"the image is {metadata.get('width')}x{metadata.get('height')}, not {size}"
        )
    return problems


def build_prompt(*, subject: str, kind: str, style: str, ground: str) -> str:
    look = {
        "illustration": "An illustration",
        "icon": "A single icon, centered, simple enough to read at 32 pixels",
        "photo": "A photograph",
    }[kind]
    grounds = {
        "transparent": "on a fully transparent background",
        "light": "on a plain light background",
        "dark": "on a plain dark background",
        "any": "",
    }
    parts = [
        f"{look} of {subject}",
        grounds[ground],
        f"House style: {style}" if style else "",
        "No text, letters, numbers, logos, watermarks or signatures anywhere in the image.",
    ]
    return ". ".join(part.rstrip(".") for part in parts if part) + "."


def request_body(
    *,
    endpoint: str,
    prompt: str,
    size: str,
    ground: str,
    kind: str,
    references: list[str],
) -> dict[str, Any]:
    """The fal request for one drawing. Each family names its fields its own way."""
    family = model_family(endpoint)
    body: dict[str, Any] = {"prompt": prompt, "num_images": 1}
    if family == "nano-banana":
        body.update(output_format="png", aspect_ratio=ASPECTS[size], resolution="1K")
        if references:
            body["image_urls"] = references
    elif family == "seedream":
        body.update(
            image_size=PRESETS[size],
            background="transparent" if ground == "transparent" else "auto",
        )
        if references:
            body["image_urls"] = references
    elif family == "gpt-image":
        body.update(
            image_size=PRESETS[size],
            output_format="png",
            quality="high",
            background="transparent" if ground == "transparent" else "auto",
        )
        if references:
            body["image_urls"] = references
    elif family == "recraft":
        # recraft/v4/style/text-to-image or text-to-vector: the style comes from the
        # references attached directly (or a style_id); there is no named style field.
        body.pop("num_images")
        body["image_size"] = PRESETS[size]
        if references:
            body["image_urls"] = references
    else:  # pragma: no cover - check_model refuses unknown families first
        raise ImageLibraryError(f"Tin does not know how to call {endpoint}")
    return body


def data_url(path: str, content: bytes) -> str:
    media_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def sidecar_path(image_id: str) -> str:
    return f"{ROOT}/{image_id}.json"


def entry_from(sidecar: dict[str, Any]) -> dict[str, Any]:
    """The INDEX.json line for one image: enough to search and draw a grid."""
    keys = ("id", "kind", "subject", "tags", "files", "status", "created_at", "width", "height")
    return {key: sidecar.get(key) for key in keys} | {"run_id": sidecar.get("run_id")}


def with_wiki_line(content: bytes | None) -> str | None:
    """wiki/INDEX.md with the library's line, or None when the line is already there."""
    text = content.decode("utf-8") if content else "# Project index\n"
    if f"`{ROOT}/`" in text:
        return None
    return text.rstrip("\n") + "\n" + WIKI_LINE + "\n"


@dataclass(frozen=True)
class Snapshot:
    """Project Files at one revision, as the library reads them."""

    revision: str
    paths: frozenset[str]
    style: dict[str, Any]
    index: dict[str, Any]
    # wiki/INDEX.md with the library's line added, when a save should add it.
    wiki: str | None = None


class ImageLibrary:
    def __init__(self, *, database: Any, storage: Any, settings: Any = None) -> None:
        self.db = database
        self.storage = storage
        self.settings = settings

    async def project(self, project_id: UUID) -> Project:
        project = await self.db.get_project(project_id)
        if project is None or getattr(project, "deleted_at", None) is not None:
            raise ImageLibraryError("project not found", status=404)
        return project

    async def read(self, project: Project, path: str, revision: str) -> bytes:
        return await self.storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=revision, path=path
        )

    async def snapshot(self, project: Project) -> Snapshot:
        paths, revision = await self.storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
        listed = frozenset(paths)
        style = parse_style(
            await self.read(project, STYLE_PATH, revision) if STYLE_PATH in listed else None
        )
        index = parse_index(
            await self.read(project, INDEX_PATH, revision) if INDEX_PATH in listed else None
        )
        return Snapshot(revision=revision, paths=listed, style=style, index=index)

    # Reading.

    async def search(
        self,
        project_id: UUID,
        query: str = "",
        *,
        kind: str | None = None,
        include_retired: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        if kind is not None:
            check_kind(kind)
        if len(query) > MAX_SUBJECT:
            raise ImageLibraryError(f"query must have at most {MAX_SUBJECT} characters")
        snap = await self.snapshot(await self.project(project_id))
        images = search(snap.index, query, kind=kind, include_retired=include_retired, limit=limit)
        return {
            "revision": snap.revision,
            "images": images,
            "total": len(snap.index.get("images", [])),
            "undo_hours": int(UNDO_WINDOW.total_seconds() // 3600),
        }

    async def get(
        self, project_id: UUID, image_id: str, clerk_user_id: str | None = None
    ) -> dict[str, Any]:
        check_image_id(image_id)
        project = await self.project(project_id)
        snap = await self.snapshot(project)
        sidecar = await self._sidecar(project, snap, image_id)
        result: dict[str, Any] = {"revision": snap.revision, "image": sidecar}
        if clerk_user_id is not None:
            links = DownloadLinks(database=self.db, settings=self.settings)
            result["downloads"] = {
                label: await links.link(project.id, path, snap.revision, clerk_user_id)
                for label, path in (sidecar.get("files") or {}).items()
                if path and path in snap.paths
            }
        created = _time(sidecar.get("created_at"))
        if sidecar.get("status") == "saved" and created is not None:
            result["discard_until"] = (created + UNDO_WINDOW).isoformat()
        return result

    async def _sidecar(self, project: Project, snap: Snapshot, image_id: str) -> dict[str, Any]:
        path = sidecar_path(image_id)
        if path not in snap.paths:
            raise ImageLibraryError(
                "image not found; search_images lists the saved images", status=404
            )
        try:
            sidecar = json.loads((await self.read(project, path, snap.revision)).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ImageLibraryError(f"{path} is not valid JSON; fix it in Files") from None
        if not isinstance(sidecar, dict):
            raise ImageLibraryError(f"{path} must be a JSON object")
        return sidecar

    async def style(self, project_id: UUID) -> dict[str, Any]:
        snap = await self.snapshot(await self.project(project_id))
        return {
            "revision": snap.revision,
            "path": STYLE_PATH,
            "kinds": {kind: kind_style(snap.style, kind) for kind in DRAWN_KINDS},
            "diagram": "Diagrams use Tin's diagram renderer, not a model: no style to set.",
            "default_models": DEFAULT_MODELS,
        }

    # Writing.

    async def set_style(
        self,
        project_id: UUID,
        clerk_user_id: str,
        *,
        kind: str,
        style: str | None = None,
        references: list[str] | None = None,
        model: str | None = None,
        model_text: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        check_kind(kind, drawn=True)
        if style is None and references is None and model is None and model_text is None:
            raise ImageLibraryError("give style, references, model or model_text to change")
        if style is not None and len(style) > MAX_STYLE_TEXT:
            raise ImageLibraryError(f"style must have at most {MAX_STYLE_TEXT} characters")
        for value in (model, model_text):
            if value:
                check_model(value)
        project = await self.project(project_id)

        def build(snap: Snapshot) -> tuple[list[ProjectFileMutation], dict[str, Any]]:
            if references is not None:
                if len(references) > MAX_REFERENCES:
                    raise ImageLibraryError(f"give at most {MAX_REFERENCES} reference images")
                for path in references:
                    if not safe_project_file_path(path) or PurePosixPath(
                        path
                    ).suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                        raise ImageLibraryError(
                            f"{path} is not a PNG or JPEG path; put references in "
                            f"{REFERENCE_FOLDER}/ with prepare_file_upload"
                        )
                    if path not in snap.paths:
                        raise ImageLibraryError(f"{path} is not in project Files")
            current = dict(snap.style.get("kinds", {}).get(kind) or {})
            changed = {
                key: value
                for key, value in {
                    "style": " ".join(style.split()) if style is not None else None,
                    "references": list(references) if references is not None else None,
                    "model": model,
                    "model_text": model_text,
                }.items()
                if value is not None
            }
            for key in ("model", "model_text"):
                # An empty string goes back to the server's default model.
                if changed.get(key) == "":
                    current.pop(key, None)
                    changed.pop(key)
            current.update(changed)
            current["version"] = int(current.get("version") or 0) + 1
            current["updated_at"] = datetime.now(UTC).isoformat()
            current["updated_by"] = clerk_user_id
            new_style = {**snap.style, "kinds": {**snap.style.get("kinds", {}), kind: current}}
            return [ProjectFileMutation("upsert", STYLE_PATH, dump(new_style))], new_style

        revision, new_style = await self._commit(
            project,
            clerk_user_id,
            client_id,
            f"Set the {kind} image style",
            build,
        )
        return {"revision": revision, "path": STYLE_PATH, "style": kind_style(new_style, kind)}

    async def save(
        self,
        project: Project,
        clerk_user_id: str,
        *,
        kind: str,
        subject: str,
        tags: list[str],
        files: dict[str, bytes | str],
        details: dict[str, Any],
        client_id: str | None = None,
        run_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Keep one image, its sidecar and its INDEX.json line in one commit.

        `files` maps an extension (png, jpg, svg or mmd) to the file's content. A run saves at
        most once: a retry finds its own image in INDEX.json and returns it."""
        base = slugify(subject)

        def build(snap: Snapshot) -> tuple[list[ProjectFileMutation], dict[str, Any]]:
            if run_id is not None:
                for entry in snap.index.get("images", []):
                    if entry.get("run_id") == str(run_id):
                        return [], {"id": entry["id"], "replayed": True}
            taken = {entry.get("id") for entry in snap.index.get("images", [])}
            slug, number = base, 1
            while f"{kind}/{slug}" in taken or sidecar_path(f"{kind}/{slug}") in snap.paths:
                number += 1
                slug = f"{base[: _SLUG_MAX - 4]}-{number}"
            image_id = f"{kind}/{slug}"
            stored = {extension: f"{ROOT}/{image_id}.{extension}" for extension in sorted(files)}
            sidecar = {
                **details,
                "id": image_id,
                "kind": kind,
                "subject": subject,
                "tags": tags,
                "files": stored,
                "author": clerk_user_id,
                "created_at": datetime.now(UTC).isoformat(),
                "status": "saved",
                "retired_at": None,
                "run_id": str(run_id) if run_id else None,
            }
            index = {
                **snap.index,
                "images": [entry_from(sidecar), *snap.index.get("images", [])],
            }
            changes = [
                ProjectFileMutation("upsert", stored[extension], files[extension])
                for extension in sorted(files)
            ]
            changes += [
                ProjectFileMutation("upsert", sidecar_path(image_id), dump(sidecar)),
                ProjectFileMutation("upsert", INDEX_PATH, dump(index)),
            ]
            if snap.wiki is not None:
                changes.append(ProjectFileMutation("upsert", WIKI_INDEX_PATH, snap.wiki))
            return changes, sidecar

        revision, sidecar = await self._commit(
            project,
            clerk_user_id,
            client_id,
            f"Save image {subject}"[:200],
            build,
            wiki=True,
            request_seed=f"image:{run_id}" if run_id else None,
        )
        return {"revision": revision, "image": sidecar}

    async def discard(
        self,
        project_id: UUID,
        clerk_user_id: str,
        image_id: str,
        *,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Within UNDO_WINDOW, remove the image in a new commit. After it, mark it retired."""
        check_image_id(image_id)
        project = await self.project(project_id)
        outcome: dict[str, Any] = {}
        snap = await self.snapshot(project)
        sidecar = await self._sidecar(project, snap, image_id)
        if sidecar.get("status") == "retired":
            raise ImageLibraryError("this image is already retired", status=409)
        created = _time(sidecar.get("created_at"))
        undo = created is not None and datetime.now(UTC) - created < UNDO_WINDOW

        def build(snap: Snapshot) -> tuple[list[ProjectFileMutation], dict[str, Any]]:
            images = [
                entry for entry in snap.index.get("images", []) if entry.get("id") != image_id
            ]
            if undo:
                paths = [
                    path for path in (sidecar.get("files") or {}).values() if path in snap.paths
                ]
                changes = [ProjectFileMutation("delete", path) for path in paths]
                if sidecar_path(image_id) in snap.paths:
                    changes.append(ProjectFileMutation("delete", sidecar_path(image_id)))
                outcome.update(discarded=True, retired=False)
            else:
                retired = {
                    **sidecar,
                    "status": "retired",
                    "retired_at": datetime.now(UTC).isoformat(),
                    "retired_by": clerk_user_id,
                }
                changes = [ProjectFileMutation("upsert", sidecar_path(image_id), dump(retired))]
                images = [
                    entry_from(retired) if entry.get("id") == image_id else entry
                    for entry in snap.index.get("images", [])
                ]
                outcome.update(discarded=False, retired=True)
            changes.append(
                ProjectFileMutation("upsert", INDEX_PATH, dump({**snap.index, "images": images}))
            )
            return changes, outcome

        verb = "Discard" if undo else "Retire"
        revision, _ = await self._commit(
            project, clerk_user_id, client_id, f"{verb} image {image_id}", build
        )
        return {"image_id": image_id, "revision": revision, **outcome}

    async def save_diagram(
        self,
        project_id: UUID,
        clerk_user_id: str,
        *,
        subject: str,
        source: str,
        tags: list[str],
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Keep a diagram the caller wrote in Tin's diagram dialect. No model draws it."""
        from tin_lite.diagram_compositions import parse_diagram_v2

        text = (source or "").strip() + "\n"
        if len(text.encode()) > MAX_PROJECT_FILE_BYTES // 4:
            raise ImageLibraryError("diagram_source is too large")
        try:
            flow = parse_diagram_v2(text)
        except ValueError as error:
            raise ImageLibraryError(
                f"diagram_source is not Tin's diagram dialect: {error}. Start with "
                '`graph TD`, then nodes like a["Name"]:::step and edges like a --> b.'
            ) from None
        project = await self.project(project_id)
        return await self.save(
            project,
            clerk_user_id,
            kind="diagram",
            subject=subject,
            tags=tags,
            files={"mmd": text},
            details={
                "model": None,
                "prompt": None,
                "renderer": "tin-diagram.v2",
                "nodes": len(flow.get("nodes", [])),
                "style_version": None,
            },
            client_id=client_id,
        )

    async def _commit(
        self,
        project: Project,
        clerk_user_id: str,
        client_id: str | None,
        message: str,
        build,
        *,
        wiki: bool = False,
        request_seed: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Read Files, build the change on that revision, commit it; take a newer head and
        build again when another write lands first."""
        files = ProjectFileService(database=self.db, storage=self.storage)
        for attempt in range(COMMIT_TRIES):
            snap = await self.snapshot(project)
            if wiki and WIKI_INDEX_PATH in snap.paths:
                line = with_wiki_line(await self.read(project, WIKI_INDEX_PATH, snap.revision))
                snap = Snapshot(
                    revision=snap.revision,
                    paths=snap.paths,
                    style=snap.style,
                    index=snap.index,
                    wiki=line,
                )
            changes, value = build(snap)
            if not changes:
                return snap.revision, value
            # A run's save keeps one request ID per revision, so a retried activity replays
            # the commit it already made instead of making a second one.
            request_id = (
                uuid5(UUID(int=0), f"{request_seed}:{snap.revision}") if request_seed else uuid4()
            )
            try:
                result = await files.commit_files(
                    project=project,
                    actor_clerk_user_id=clerk_user_id,
                    client_id=client_id,
                    request_id=request_id,
                    expected_revision=snap.revision,
                    message=message,
                    changes=tuple(changes),
                )
                return result.revision, value
            except (StaleProjectRevisionError, ProjectStateChangedError):
                if attempt + 1 == COMMIT_TRIES:
                    raise ImageLibraryError(
                        "Files kept changing while Tin saved this; try again.", status=409
                    ) from None
            except ValueError as error:
                raise ImageLibraryError(str(error)) from None
        raise AssertionError("unreachable")

    async def references(self, project: Project, revision: str, paths: list[str]) -> list[str]:
        """The style's reference images as data URLs fal accepts, at most MAX_SENT_REFERENCES."""
        urls: list[str] = []
        for path in paths[:MAX_SENT_REFERENCES]:
            content = await self.read(project, path, revision)
            try:
                validate_media(path, content)
            except ValueError as error:
                raise ImageLibraryError(f"reference {path} cannot be used: {error}") from None
            urls.append(data_url(path, content))
        return urls


class DownloadLinks:
    """A short-lived link to one file at one revision, for an agent that cannot send a session.

    Only a hash of the link is kept. The member who asked for it must still be on the project
    when the link is used."""

    def __init__(self, *, database: Any, settings: Any) -> None:
        self.db = database
        self.settings = settings

    async def link(self, project_id: UUID, path: str, revision: str, clerk_user_id: str) -> dict:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + DOWNLOAD_LINK_LIFETIME
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM file_download_links WHERE expires_at < now() - interval '1 day'"
            )
            await conn.execute(
                """INSERT INTO file_download_links
                       (token_sha256, project_id, path, revision, created_by_clerk_user_id,
                        expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                sha256(token.encode()).hexdigest(),
                project_id,
                path,
                revision,
                clerk_user_id,
                expires,
            )
        base = getattr(self.settings, "switchboard_public_url", "") or ""
        return {
            "url": f"{base.rstrip('/')}/api/file-downloads/{token}",
            "expires_at": expires.isoformat(),
        }

    async def open(self, token: str, storage: Any) -> tuple[str, bytes]:
        """The file a link names, as (path, bytes)."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise ImageLibraryError("This download link isn't Tin's.", status=404)
        row = await self.db.pool.fetchrow(
            "SELECT * FROM file_download_links WHERE token_sha256 = $1",
            sha256(token.encode()).hexdigest(),
        )
        if row is None:
            raise ImageLibraryError("This download link isn't Tin's.", status=404)
        if row["expires_at"] <= datetime.now(UTC):
            raise ImageLibraryError("This download link expired: call get_image again.", status=410)
        project = await self.db.get_project(row["project_id"])
        if project is None or getattr(project, "deleted_at", None) is not None:
            raise ImageLibraryError("This download link's project is gone.", status=410)
        if not await self.db.has_project_access(
            project_id=project.id, clerk_user_id=row["created_by_clerk_user_id"]
        ):
            raise ImageLibraryError(
                "The member who asked for this link is no longer on the project.", status=403
            )
        content = await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=row["revision"], path=row["path"]
        )
        return row["path"], content


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_MODELS",
    "INDEX_PATH",
    "KEY",
    "MAX_IMAGE_BYTES",
    "STYLE_PATH",
    "UNDO_WINDOW",
    "WORKFLOW_ID",
    "DownloadLinks",
    "ImageLibrary",
    "ImageLibraryError",
]
