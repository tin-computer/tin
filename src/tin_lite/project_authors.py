"""Project author bindings. Names and editable files never establish member identity."""

from __future__ import annotations

from uuid import UUID

from tin_lite.project_files import credential_findings, safe_project_file_path
from tin_lite.writing_style import STYLE_PATH

OPERATION = "project_author_v1"
MAX_GUIDE_BYTES = 24_000


def source_key(run_id):
    return f"project-author:{run_id}"


def guide_path(author_id):
    return f".agents/skills/authors/{UUID(str(author_id))}/SKILL.md"


def validate_guide_path(path):
    if not safe_project_file_path(path) or not path.endswith(".md"):
        raise ValueError("Choose a Markdown writing guide in this project's Files.")
    if path == STYLE_PATH:
        raise ValueError(
            "The project-wide guide is shared. Copy it to this author's own file before binding it."
        )
    return path


def view(row):
    return {
        "id": str(row["id"]),
        "display_name": row["display_name"],
        "member_clerk_user_id": row["member_clerk_user_id"],
        "guide_path": row["guide_path"],
        "version": row["version"],
        "confirmed_by": row["confirmed_by_clerk_user_id"],
    }


class ProjectAuthors:
    def __init__(self, database, storage):
        self.db, self.storage = database, storage

    async def project(self, project_id, actor):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=actor):
            raise LookupError("project not found")
        return await self.db.get_project(project_id)

    async def list(self, project_id, actor):
        await self.project(project_id, actor)
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM project_authors WHERE project_id=$1 ORDER BY display_name, id",
                project_id,
            )
        authors = [view(row) for row in rows]
        return {
            "authors": authors,
            "default_author_id": next(
                (a["id"] for a in authors if a["member_clerk_user_id"] == actor), None
            ),
        }

    async def save(
        self,
        *,
        project_id,
        actor,
        author_id,
        display_name,
        expected_version,
        selected_guide=None,
        link_to_me=None,
    ):
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Use the current author version, or 0 for a new author.")
        project = await self.project(project_id, actor)
        name = display_name.strip()
        if not name or len(name) > 200 or credential_findings(name):
            raise ValueError("Give this author a name of 1–200 characters.")
        path = validate_guide_path(
            selected_guide if selected_guide is not None else guide_path(author_id)
        )
        # A supplied guide is an explicit association, checked before confirming it.
        if selected_guide:
            revision = await self.storage.head_sha(
                await self.storage.get_repo(project.state_repo_id), project.canonical_branch
            )
            await read_guide(self.storage, project, revision, path)
        async with self.db.pool.acquire() as conn, conn.transaction():
            # Same lock order as run admission and deletion; membership is checked again.
            await conn.fetchval("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
            if not await self.db.has_project_access(
                project_id=project_id, clerk_user_id=actor, conn=conn
            ):
                raise LookupError("project not found")
            current = await conn.fetchrow(
                "SELECT * FROM project_authors WHERE project_id=$1 AND id=$2 FOR UPDATE",
                project_id,
                author_id,
            )
            member = current["member_clerk_user_id"] if current else None
            if link_to_me is not None:
                member = actor if link_to_me else None
            if (
                not current
                and await conn.fetchval(
                    "SELECT count(*) FROM project_authors WHERE project_id=$1", project_id
                )
                >= 50
            ):
                raise ValueError("This project already has 50 authors.")
            if current and current["member_clerk_user_id"] not in (None, actor):
                if link_to_me:
                    raise ValueError("This author is already linked to another member.")
                member = current["member_clerk_user_id"]
            if current:
                # Omitting a replacement guide preserves the confirmed association.
                if selected_guide is None:
                    path = current["guide_path"]
                if all(
                    current[k] == v
                    for k, v in {
                        "display_name": name,
                        "guide_path": path,
                        "member_clerk_user_id": member,
                    }.items()
                ):
                    return view(current)
            if current and current["guide_path"] != path:
                # Destination ownership cannot move while an admitted capture can still write.
                active = await conn.fetchval(
                    "SELECT id FROM workflow_runs WHERE project_id=$1 "
                    "AND executor='style.capture' AND lower(input->>'author_id')=$2 "
                    "AND status IN ('pending','running','needs_input','paused') LIMIT 1",
                    project_id,
                    str(author_id),
                )
                if active:
                    raise ValueError(
                        "Finish or discard this author's capture before changing guides."
                    )
            if (current["version"] if current else 0) != expected_version:
                raise ValueError("This author changed. Reload the author before saving.")
            if await conn.fetchval(
                "SELECT id FROM project_authors WHERE project_id=$1 AND id<>$2 "
                "AND (guide_path=$3 OR ($4::text IS NOT NULL AND member_clerk_user_id=$4))",
                project_id,
                author_id,
                path,
                member,
            ):
                raise ValueError("This guide or member is already linked to another author.")
            row = await conn.fetchrow(
                "INSERT INTO project_authors(project_id,id,display_name,member_clerk_user_id,"
                "guide_path,confirmed_by_clerk_user_id) VALUES($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT(project_id,id) DO UPDATE SET display_name=$3,"
                "member_clerk_user_id=$4,guide_path=$5,version=project_authors.version+1,"
                "confirmed_by_clerk_user_id=$6,updated_at=now() RETURNING *",
                project_id,
                author_id,
                name,
                member,
                path,
                actor,
            )
        return view(row)

    async def resolve(self, project_id, author_id):
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM project_authors WHERE project_id=$1 AND id=$2",
                project_id,
                UUID(str(author_id)),
            )
        if row is None:
            raise ValueError("Choose an author configured for this project.")
        return {"project_id": str(project_id), **view(row)}


async def read_guide(storage, project, revision, path):
    validate_guide_path(path)
    entry = await storage.read_output_destination(
        repo_id=project.state_repo_id, revision=revision, path=path
    )
    raw = entry[1] if entry else None
    if raw is None:
        raise ValueError("This author's writing guide is missing. Capture or choose a guide.")
    if not raw.strip() or len(raw) > MAX_GUIDE_BYTES or b"\0" in raw:
        raise ValueError("Use a nonempty writing guide of at most 24 KB.")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError("Save this writing guide as UTF-8 text.") from None
    if credential_findings(text):
        raise ValueError("Remove credentials from the writing guide before using it.")
    return text


async def guard(conn, *, project_id, inputs, source):
    if source is None or source.get("project_id") != str(project_id):
        raise ValueError("Choose an author before starting.")
    row = await conn.fetchrow(
        "SELECT * FROM project_authors WHERE project_id=$1 AND id=$2 FOR SHARE",
        project_id,
        UUID(inputs["author_id"]),
    )
    if row is None or any(view(row).get(k) != source.get(k) for k in view(row)):
        raise ValueError("This author changed. Check the writing guide and start again.")


async def capture_author(database, run):
    """An existing run never follows a subsequently edited author binding."""
    if not run.input.get("author_id"):
        return None
    receipt = await database.get_effect(source_key(run.id))
    if not receipt or receipt.status != "completed" or receipt.operation != OPERATION:
        raise ValueError("This capture has no pinned author destination.")
    source = receipt.result
    if source.get("project_id") != str(run.project_id) or source.get("id") != str(
        UUID(run.input["author_id"])
    ):
        raise ValueError("This capture's author does not match its project.")
    validate_guide_path(source["guide_path"])
    return source


async def capture_destination(database, run):
    author = await capture_author(database, run)
    return author["guide_path"] if author else STYLE_PATH
