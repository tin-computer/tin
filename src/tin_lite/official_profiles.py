"""A project setting: the business's official profiles and the company that publishes the site.

The organic audit only proposes candidates (schema.entity_profiles_missing and
schema.entity_parent_unclear). A project member confirms the ones that are the business's own
here, and website.change writes exactly these into the homepage's structured data: `sameAs`
for the profiles, `publisher` or `parentOrganization` for the company. Nothing is written from
a candidate alone.

Each save is a new revision with who saved it and when, like protected pages. Saved
revisions never change, so a change row that names a revision names exact URLs.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from tin_lite.website_change import WebsiteChangeConflict

MAX_PROFILES = 20
MAX_URL_CHARS = 300
MAX_NAME_CHARS = 120


def _url(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an https URL.")
    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an https URL.") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or "." not in parts.hostname
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or len(text) > MAX_URL_CHARS
        or any(ord(c) < 33 for c in text)
    ):
        raise ValueError(f"{field} must be an https URL of at most {MAX_URL_CHARS} characters.")
    # A profile is a page, not a tracked visit to it.
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/") or "/", "", ""))


def normalize(profiles: Any, parent: Any) -> tuple[list[str], dict | None]:
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
        raise ValueError(f"profiles is a list of at most {MAX_PROFILES} https URLs.")
    urls = list(dict.fromkeys(_url(item, field="Each profile") for item in profiles))
    if parent is None:
        return urls, None
    if not isinstance(parent, dict) or set(parent) - {"name", "url"}:
        raise ValueError("parent_organization takes a name and an https url.")
    name = parent.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME_CHARS:
        raise ValueError(f"parent_organization.name is 1 to {MAX_NAME_CHARS} characters.")
    return urls, {
        "name": " ".join(name.split()),
        "url": _url(parent.get("url"), field="parent_organization.url"),
    }


def _view(row: Any) -> dict[str, Any]:
    def loaded(value):
        return json.loads(value) if isinstance(value, str) else value

    return {
        "revision": row["revision"],
        "profiles": list(loaded(row["profiles"])),
        "parent_organization": loaded(row["parent_organization"]),
        "changed_by": row["changed_by_clerk_user_id"],
        "changed_at": row["changed_at"].isoformat(),
    }


async def project_official_profiles(executor: Any, *, project_id: UUID) -> dict[str, Any]:
    """The setting as it stands: the newest saved revision. Revision 0 means none saved."""
    row = await executor.fetchrow(
        "SELECT * FROM project_official_profiles WHERE project_id=$1 "
        "ORDER BY revision DESC LIMIT 1",
        project_id,
    )
    if row is None:
        return {"revision": 0, "profiles": [], "parent_organization": None}
    current = _view(row)
    return {key: current[key] for key in ("revision", "profiles", "parent_organization")}


async def read_official_profiles(
    database, *, project_id: UUID, actor: str, history: int = 20
) -> dict[str, Any]:
    """The setting for a project member, with its newest `history` revisions."""
    if not await database.has_project_access(project_id=project_id, clerk_user_id=actor):
        raise LookupError("Project not found.")
    rows = await database.pool.fetch(
        "SELECT * FROM project_official_profiles WHERE project_id=$1 "
        "ORDER BY revision DESC LIMIT $2",
        project_id,
        max(1, min(int(history), 100)),
    )
    return {
        **await project_official_profiles(database.pool, project_id=project_id),
        "history": [_view(row) for row in rows],
    }


async def save_official_profiles(
    database,
    *,
    project_id: UUID,
    actor: str,
    profiles: Any,
    parent_organization: Any,
    expected_revision: int,
    request_id: UUID,
) -> dict[str, Any]:
    """Save what the founder confirmed as a new revision, recording who and when.

    The caller names the revision it read (0 before the first save), so two people never
    overwrite each other unseen. Replaying the same request returns what it saved.
    """
    urls, parent = normalize(profiles, parent_organization)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision is the revision you read, 0 before the first save.")
    async with database.pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
        if not await database.has_project_access(
            project_id=project_id, clerk_user_id=actor, conn=conn
        ):
            raise LookupError("Project not found.")
        replay = await conn.fetchrow(
            "SELECT * FROM project_official_profiles WHERE project_id=$1 AND request_id=$2",
            project_id,
            request_id,
        )
        if replay is not None:
            saved = _view(replay)
            if (
                saved["changed_by"] != actor
                or saved["profiles"] != urls
                or saved["parent_organization"] != parent
            ):
                raise WebsiteChangeConflict(
                    "This request ID already saved other profiles. Use a new request ID."
                )
        else:
            current = await conn.fetchval(
                "SELECT coalesce(max(revision), 0) FROM project_official_profiles "
                "WHERE project_id=$1",
                project_id,
            )
            if current != expected_revision:
                raise WebsiteChangeConflict(
                    "The official profiles changed since you read them. Read them again first."
                )
            await conn.execute(
                "INSERT INTO project_official_profiles (project_id, revision, profiles, "
                "parent_organization, changed_by_clerk_user_id, request_id) "
                "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)",
                project_id,
                current + 1,
                json.dumps(urls),
                json.dumps(parent) if parent is not None else None,
                actor,
                request_id,
            )
    return await read_official_profiles(database, project_id=project_id, actor=actor)
