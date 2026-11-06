"""Films a project's coding agent makes, kept in project Files and decided on in Decisions.

The founder's coding agent (Claude Code, Codex, any agent with Tin's MCP) makes a film on its own
machine, with Tin's film kit `tin-film` (film_kit) or any tool it likes, and uploads each
version through the link `get_film` gives it. Tin checks the upload, keeps it at
films/<slug>.md with its folder films/<slug>.assets/, and shows it in Decisions. The founder
picks a direction, approves the film or asks for changes; the agent reads that back through
`get_film`. Tin runs nothing to make a film and never waits for one. See docs/feature-films.md.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from tin_lite import film_kit, film_notes, page_assets
from tin_lite.code_storage import ProjectStateChangedError
from tin_lite.domain import MEMORY_INDEX_PATH, Project
from tin_lite.product_urls import dashboard_url
from tin_lite.project_files import ProjectFileMutation
from tin_lite.project_media import MAX_VIDEO_BYTES

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SLUG = 80
LINK_LIFETIME = timedelta(minutes=45)
MAX_DECISION_NOTE = 2000
# Runaway guards, well above a film's note, its stills and a minute of 1080p.
MAX_UPLOAD_BYTES = 100_000_000
MAX_FILES = 24
MAX_FILES_BYTES = 100_000_000
_FILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,80}\.(?:png|jpg|jpeg|mp4|vtt|html|json)")
_KINDS = ("png", "jpeg", "mp4", "vtt", "html", "json")
# What the agent reads besides the guide, when the project has it.
_CONTEXT_PATHS = (
    (MEMORY_INDEX_PATH, "what Tin knows about the product; its Feature map names the features"),
    ("brand/BRAND.md", "the brand: voice, colours and type"),
    ("DESIGN.md", "the design system"),
    (".agents/skills/writing-style/SKILL.md", "how the founder writes; captions follow it"),
)
DECISIONS = {
    "directions": ("pick", "others", "discard"),
    "film": ("approve", "changes", "discard"),
}
_STATUS_OF = {"pick": "picked", "others": "others", "approve": "approved", "changes": "changes"}


class FilmError(ValueError):
    """Something the agent or the founder can fix, said so; `problems` lists each one."""

    def __init__(self, message: str, *, status: int = 422, problems: tuple[str, ...] = ()):
        super().__init__(message)
        self.status = status
        self.problems = problems


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or len(slug) > MAX_SLUG or not SLUG.fullmatch(slug):
        raise FilmError("A film's name is lowercase words joined by hyphens, like people-ranked.")
    return slug


def note_path(slug: str) -> str:
    return f"films/{slug}.md"


def folder_path(slug: str) -> str:
    return f"films/{slug}.assets"


@dataclass
class Upload:
    """An upload's note and the files Tin keeps beside it, checked."""

    note: bytes
    parsed: film_notes.Film | film_notes.Directions
    files: dict[str, bytes]
    dropped: list[dict[str, str]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "directions" if isinstance(self.parsed, film_notes.Directions) else "film"


def _problem(name: str, content: bytes) -> str | None:
    """Why a file in the folder can't be kept, or None."""
    if name.endswith(".mp4"):
        if len(content) > MAX_VIDEO_BYTES:
            return f"it is larger than {MAX_VIDEO_BYTES // 1_000_000} MB"
        try:
            film_notes.validate_video(content)
        except ValueError as exc:
            return str(exc)
        return None
    return page_assets.problem(name, content, _KINDS)


def _members(archive: tarfile.TarFile):
    try:
        yield from archive
    except (tarfile.TarError, OSError, EOFError):
        raise FilmError("The upload's tar is damaged; make it again.") from None


def read_upload(body: bytes, slug: str) -> Upload:
    """The note and its folder from an upload: a tar (gzipped or not) holding `<slug>.md` and
    `<slug>.assets/<file>`, either at its root or under `films/`. Every file the note shows must
    be there and sound; any other file of a kind Tin keeps stays beside it, and the rest are
    named as left out."""
    if not body or len(body) > MAX_UPLOAD_BYTES:
        raise FilmError(f"Upload 1 byte to {MAX_UPLOAD_BYTES // 1_000_000} MB.", status=413)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:*")
    except (tarfile.TarError, OSError, EOFError):
        raise FilmError(
            f"Upload a tar.gz of {slug}.md and {slug}.assets/ (tin-film upload makes one)."
        ) from None
    note: bytes | None = None
    files: dict[str, bytes] = {}
    dropped: list[dict[str, str]] = []
    total = 0
    with archive:
        # Member by member, so a tar that grows past the limits stops there, unread.
        for member in _members(archive):
            name = member.name.removeprefix("./").removeprefix("films/")
            if member.isdir() or not name:
                continue
            if name.rsplit("/", 1)[-1].startswith("._"):
                continue  # macOS resource forks, never part of the film
            if not member.isfile():
                dropped.append({"path": name, "reason": "it is not a regular file"})
                continue
            if name != f"{slug}.md" and not name.startswith(f"{slug}.assets/"):
                dropped.append({"path": name, "reason": "it is outside the note and its folder"})
                continue
            base = name.removeprefix(f"{slug}.assets/")
            if name != f"{slug}.md" and not _FILE_NAME.fullmatch(base):
                dropped.append(
                    {
                        "path": name,
                        "reason": "Tin keeps only flat, lowercase names of PNG, JPEG, MP4, "
                        "WebVTT, HTML and JSON files",
                    }
                )
                continue
            if len(files) >= MAX_FILES or total + member.size > MAX_FILES_BYTES:
                raise FilmError(
                    f"A film keeps at most {MAX_FILES} files and "
                    f"{MAX_FILES_BYTES // 1_000_000} MB.",
                    status=413,
                )
            stream = archive.extractfile(member)
            content = stream.read(member.size + 1) if stream is not None else b""
            if len(content) != member.size:
                raise FilmError("The upload's tar is damaged; make it again.")
            total += len(content)
            if name == f"{slug}.md":
                note = content
            else:
                files[base] = content
    if note is None:
        raise FilmError(f"The upload has no {slug}.md: upload the note with its folder.")
    folder = f"{slug}.assets"
    try:
        parsed = film_notes.parse_note(note, folder=folder)
    except ValueError as exc:
        raise FilmError(f"The note can't be kept: {exc}.") from None
    problems = []
    for path in parsed.files:
        base = path.removeprefix(f"{folder}/")
        if base not in files:
            problems.append(f"{path} is missing")
        elif reason := _problem(base, files[base]):
            problems.append(f"{path}: {reason}")
    if problems:
        raise FilmError("The note shows files Tin can't keep.", problems=tuple(problems))
    shown = {path.removeprefix(f"{folder}/") for path in parsed.files}
    for base in sorted(set(files) - shown):
        if reason := _problem(base, files[base]):
            dropped.append({"path": f"{folder}/{base}", "reason": reason})
            del files[base]
    if isinstance(parsed, film_notes.Film):
        for base in [name for name in files if name.endswith(".mp4") and name not in shown]:
            dropped.append({"path": f"{folder}/{base}", "reason": "the note plays another film"})
            del files[base]
    return Upload(note=note, parsed=parsed, files=files, dropped=dropped)


def _view(upload: Upload, slug: str) -> dict[str, Any]:
    """What Decisions shows for a version, as project paths."""
    if isinstance(upload.parsed, film_notes.Directions):
        view = upload.parsed.view()
        for sketch in view["sketches"]:
            sketch["still"] = f"films/{sketch['still']}"
            if sketch["page"]:
                sketch["page"] = f"films/{sketch['page']}"
        return {"directions": view}
    film = upload.parsed
    video = film_notes.validate_video(upload.files[film.video.removeprefix(f"{slug}.assets/")])
    return {
        "film": {
            "video": f"films/{film.video}",
            "poster": f"films/{film.poster}" if film.poster else None,
            "captions": f"films/{film.captions}" if film.captions else None,
            "width": video.width,
            "height": video.height,
            "seconds": round(video.duration_seconds, 1),
        }
    }


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _version(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "version": row["version"],
        "kind": row["kind"],
        "title": row["title"],
        "status": row["status"],
        "revision": row["commit_sha"],
        "picked": row["picked"],
        "note": row["decision_note"],
        "uploaded_at": row["created_at"].isoformat(),
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        **_json(row["view"]),
    }


def _next(latest: dict[str, Any] | None, slug: str, decisions: str) -> tuple[str, str]:
    """The film's state and the one thing its agent does next."""
    if latest is None:
        return "new", (
            "Read the guide, then sketch two to five directions and upload them for the founder "
            "to pick from. If the founder already said how to show it, build the film and "
            "upload it instead."
        )
    status, kind = latest["status"], latest["kind"]
    if status == "waiting":
        what = "pick a direction" if kind == "directions" else "watch the film"
        return f"waiting_for_{'pick' if kind == 'directions' else 'review'}", (
            f"The founder will {what} in Decisions ({decisions}). Tell them it's there; call "
            "get_film again to read their decision. Uploading again replaces this version."
        )
    if status == "picked":
        sketch = next(
            (item for item in latest["directions"]["sketches"] if item["n"] == latest["picked"]),
            None,
        )
        name = sketch["name"] if sketch else f"direction {latest['picked']}"
        return "build", (
            f"Build direction {latest['picked']}, {name}"
            + (f", with the founder's note: {latest['note']}" if latest["note"] else "")
            + ". Start from its sketch page, follow the guide from the storyboard, and upload "
            "the film."
        )
    if status == "others":
        return "sketch_again", (
            "Sketch other directions, far from the ones already sketched"
            + (f", with the founder's note: {latest['note']}" if latest["note"] else "")
            + ", and upload them."
        )
    if status == "changes":
        return "revise", (
            f"Change the film as the founder asked: {latest['note']}. Restore it with "
            f"tin-film restore films/{slug}.md, change it, take it again and upload it."
        )
    if status == "approved":
        return "approved", (
            f"The founder approved version {latest['version']}. The film is in project Files at "
            f"films/{slug}.md; nothing else to do unless they ask."
        )
    return "discarded", "The founder dropped this film. Start another only if they ask."


class Films:
    """Film versions in Postgres, their files in the project's canonical Files."""

    def __init__(self, *, database: Any, storage: Any, settings: Any) -> None:
        self.db = database
        self.storage = storage
        self.settings = settings

    def _decisions_url(self, project_id: UUID) -> str:
        return f"{dashboard_url(self.settings)}/decisions?project={project_id}"

    async def versions(self, project_id: UUID, slug: str) -> list[dict[str, Any]]:
        rows = await self.db.pool.fetch(
            """SELECT * FROM film_versions WHERE project_id = $1 AND slug = $2
               ORDER BY version DESC""",
            project_id,
            check_slug(slug),
        )
        return [_version(row) for row in rows]

    async def link(self, project_id: UUID, slug: str, clerk_user_id: str) -> dict[str, str]:
        """A link the agent uploads one version through, good for the next 45 minutes."""
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + LINK_LIFETIME
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """DELETE FROM film_upload_links
                   WHERE project_id = $1 AND version_id IS NULL
                     AND expires_at < now() - interval '1 day'""",
                project_id,
            )
            await conn.execute(
                """INSERT INTO film_upload_links
                       (token_sha256, id, project_id, slug, created_by_clerk_user_id, expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                hashlib.sha256(token.encode()).hexdigest(),
                uuid4(),
                project_id,
                check_slug(slug),
                clerk_user_id,
                expires,
            )
        url = f"{self.settings.switchboard_public_url.rstrip('/')}/api/film-uploads/{token}"
        return {"url": url, "expires_at": expires.isoformat()}

    async def get(
        self, project: Project, slug: str, clerk_user_id: str, *, guide: bool = False
    ) -> dict[str, Any]:
        """Everything the agent needs for its next step on one film."""
        versions = await self.versions(project.id, slug)
        latest = versions[0] if versions else None
        state, step = _next(latest, slug, self._decisions_url(project.id))
        kit = film_kit.command(self.settings)
        result: dict[str, Any] = {
            "project_id": str(project.id),
            "slug": slug,
            "note": note_path(slug),
            "folder": f"{folder_path(slug)}/",
            "state": state,
            "next": step,
            "latest": latest,
            "earlier": [
                {key: item[key] for key in ("version", "kind", "status", "picked", "note")}
                for item in versions[1:6]
            ],
            "decisions": self._decisions_url(project.id),
            "kit": kit,
        }
        if state not in {"approved", "discarded"} or guide:
            upload = await self.link(project.id, slug, clerk_user_id)
            result["upload"] = {
                **upload,
                "command": f"{kit} upload films/{slug}.md '{upload['url']}'",
            }
        if state == "new" or guide:
            paths, _head = await self.storage.list_canonical_files(
                repo_id=project.state_repo_id, branch=project.canonical_branch
            )
            present = set(paths)
            result["read_first"] = [
                {"path": path, "why": why} for path, why in _CONTEXT_PATHS if path in present
            ]
            result["guide"] = film_kit.guide()
        else:
            result["guide"] = f"Run `{kit} guide` to read the guide again."
        return result

    async def open(self, project_id: UUID) -> list[dict[str, Any]]:
        """Versions waiting for the founder, oldest first, for Decisions."""
        rows = await self.db.pool.fetch(
            """SELECT * FROM film_versions WHERE project_id = $1 AND status = 'waiting'
               ORDER BY created_at""",
            project_id,
        )
        return [{"slug": row["slug"], **_version(row)} for row in rows]

    async def receive(self, token: str, body: bytes) -> dict[str, Any]:
        """Keep an upload as the film's next version. A link makes one version; sending the
        same link again after it worked answers with that version."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise FilmError("This upload link isn't Tin's.", status=404)
        digest = hashlib.sha256(token.encode()).hexdigest()
        link = await self.db.pool.fetchrow(
            "SELECT * FROM film_upload_links WHERE token_sha256 = $1", digest
        )
        if link is None:
            raise FilmError("This upload link isn't Tin's.", status=404)
        if link["version_id"] is not None:
            return await self._received(link)
        if link["expires_at"] <= datetime.now(UTC):
            raise FilmError("This upload link expired: call get_film for a new one.", status=410)
        project = await self.db.get_project(link["project_id"])
        if project is None or project.deleted_at is not None:
            raise FilmError("This upload link's project is gone.", status=410)
        slug = link["slug"]
        upload = read_upload(body, slug)
        view = _view(upload, slug)
        async with self.db.pool.acquire() as conn, conn.transaction():
            # One version at a time per film; the files and the row land together or not at all.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"film:{project.id}:{slug}",
            )
            again = await conn.fetchrow(
                "SELECT * FROM film_upload_links WHERE token_sha256 = $1 FOR UPDATE", digest
            )
            if again["version_id"] is not None:
                return await self._received(again)
            number = 1 + (
                await conn.fetchval(
                    """SELECT coalesce(max(version), 0) FROM film_versions
                       WHERE project_id = $1 AND slug = $2""",
                    project.id,
                    slug,
                )
            )
            revision = await self._commit(project, slug, upload, request_id=link["id"], n=number)
            version_id = uuid4()
            await conn.execute(
                """UPDATE film_versions SET status = 'superseded'
                   WHERE project_id = $1 AND slug = $2 AND status = 'waiting'""",
                project.id,
                slug,
            )
            await conn.execute(
                """INSERT INTO film_versions
                       (id, project_id, slug, version, kind, title, commit_sha, view,
                        uploaded_by_clerk_user_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)""",
                version_id,
                project.id,
                slug,
                number,
                upload.kind,
                upload.parsed.title[:300],
                revision,
                json.dumps(view),
                link["created_by_clerk_user_id"],
            )
            await conn.execute(
                "UPDATE film_upload_links SET version_id = $2 WHERE token_sha256 = $1",
                digest,
                version_id,
            )
        result = await self._received({**dict(link), "version_id": version_id})
        result["dropped"] = upload.dropped
        return result

    async def _received(self, link: Any) -> dict[str, Any]:
        row = await self.db.pool.fetchrow(
            "SELECT * FROM film_versions WHERE id = $1", link["version_id"]
        )
        version = _version(row)
        what = "pick a direction" if version["kind"] == "directions" else "watch the film"
        return {
            "ok": True,
            "slug": row["slug"],
            "version": version["version"],
            "kind": version["kind"],
            "note": note_path(row["slug"]),
            "revision": version["revision"],
            "decisions": self._decisions_url(row["project_id"]),
            "next": (
                f"Kept as version {version['version']}. The founder will {what} in Decisions; "
                "tell them, and call get_film to read their decision."
            ),
            "dropped": [],
        }

    async def _commit(
        self, project: Project, slug: str, upload: Upload, *, request_id: UUID, n: int
    ):
        """The note and its folder as one commit; the folder holds exactly this version's files
        (earlier versions stay in the project's history)."""
        folder = folder_path(slug)
        for _attempt in range(4):
            paths, head = await self.storage.list_canonical_files(
                repo_id=project.state_repo_id, branch=project.canonical_branch
            )
            stale = [
                path
                for path in paths
                if path.startswith(f"{folder}/") and path[len(folder) + 1 :] not in upload.files
            ]
            changes = (
                ProjectFileMutation("upsert", note_path(slug), upload.note),
                *(
                    ProjectFileMutation("upsert", f"{folder}/{name}", content)
                    for name, content in sorted(upload.files.items())
                ),
                *(ProjectFileMutation("delete", path) for path in stale),
            )
            try:
                revision, _changed = await self.storage.commit_project_changes(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    expected_head_sha=head,
                    request_id=str(request_id),
                    message=f"Film {slug}, version {n}",
                    changes=changes,
                )
                return revision
            except ProjectStateChangedError:
                continue
        raise FilmError(
            "Project files kept changing while the film was saved; upload again.", status=409
        )

    async def decide(
        self,
        project_id: UUID,
        slug: str,
        clerk_user_id: str,
        *,
        version: int,
        decision: str,
        direction: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """The founder's decision on a waiting version: pick a direction (or ask for others),
        approve the film (or ask for changes), or drop the film. Deciding the same thing twice
        answers with the version as decided."""
        note = " ".join((note or "").split())
        if len(note) > MAX_DECISION_NOTE:
            raise FilmError(f"Keep the note under {MAX_DECISION_NOTE} characters.")
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"film:{project_id}:{check_slug(slug)}",
            )
            row = await conn.fetchrow(
                """SELECT * FROM film_versions WHERE project_id = $1 AND slug = $2
                   AND version = $3 FOR UPDATE""",
                project_id,
                slug,
                version,
            )
            if row is None:
                raise FilmError("There's no such version of this film.", status=404)
            if decision not in DECISIONS[row["kind"]]:
                allowed = ", ".join(DECISIONS[row["kind"]])
                raise FilmError(f"A {row['kind']} version is decided with {allowed}.")
            status = "discarded" if decision == "discard" else _STATUS_OF[decision]
            if decision == "pick":
                sketches = [item["n"] for item in _json(row["view"])["directions"]["sketches"]]
                if direction not in sketches:
                    raise FilmError("Pick one of the sketches by its number.")
            else:
                direction = None
            if decision == "changes" and not note:
                raise FilmError("Say what to change.")
            if row["status"] != "waiting":
                if row["status"] == status and row["picked"] == direction:
                    return _version(row)
                raise FilmError(
                    f"Version {version} is no longer waiting ({row['status']}).", status=409
                )
            decided = await conn.fetchrow(
                """UPDATE film_versions
                   SET status = $2, picked = $3, decision_note = $4,
                       decided_by_clerk_user_id = $5, decided_at = now()
                   WHERE id = $1 RETURNING *""",
                row["id"],
                status,
                direction,
                note,
                clerk_user_id,
            )
        return _version(decided)
