"""Where a project's adapted pages live on its site, chosen once by the founder.

Answer pages and public articles have no route until Tin adapts one to the site. Rather than
let each adaptation pick a folder, the coding agent asks the founder once per page type,
suggests a route from the site it can read, and saves the answer here. Approval pins the
saved route into the adaptation's instructions, and a commit-to-main setting may then merge
the first pull request that adds exactly that route (see content_repository_delivery).
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tin_lite.content_delivery import ANSWER_PAGE_WORKFLOW_ID, PUBLIC_ARTICLE_WORKFLOW_ID

PATH = "content/page-routes.json"
PageType = Literal["answer_page", "article"]
PAGE_TYPES: dict[UUID, PageType] = {
    ANSWER_PAGE_WORKFLOW_ID: "answer_page",
    PUBLIC_ARTICLE_WORKFLOW_ID: "article",
}
NOUNS: dict[str, str] = {"answer_page": "answer pages", "article": "articles"}
# What the agent suggests when the site has no route for such pages yet.
SUGGESTED: dict[str, str] = {"answer_page": "/answers/{slug}", "article": "/blog/{slug}"}
# Lowercase path segments ending in one {slug}: /answers/{slug}, /resources/guides/{slug}.
ROUTE = re.compile(r"/(?:[a-z0-9][a-z0-9-]{0,39}/){0,3}\{slug\}")
SLUG = r"[a-z0-9][a-z0-9-]{0,119}"


class PageRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    routes: dict[PageType, str] = Field(default_factory=dict)

    @field_validator("routes")
    @classmethod
    def valid(cls, routes):
        for route in routes.values():
            if not ROUTE.fullmatch(route):
                raise ValueError(
                    "Use a site path that ends in {slug}, such as /answers/{slug}: lowercase "
                    "letters, digits and hyphens, at most three folders."
                )
        return routes


def page_type(run: Any) -> PageType | None:
    return PAGE_TYPES.get(getattr(run, "workflow_id", None))


def matches(pattern: str, address: str | None) -> bool:
    """Whether a page's public address (a path or full URL) follows the chosen route."""
    if not pattern or not isinstance(address, str):
        return False
    path = urlsplit(address).path if "://" in address else address
    path = path.rstrip("/") or "/"
    prefix, _, suffix = pattern.partition("{slug}")
    return bool(re.fullmatch(re.escape(prefix) + SLUG + re.escape(suffix), path))


def direction(pattern: str) -> str:
    """The adaptation's routing instruction for a route the founder chose."""
    return (
        f"The founder chose where these pages live: {pattern}, with {{slug}} a short "
        "kebab-case slug from the title. Publish the page at exactly that route and give it "
        "on the Public URL line. If the site has no route that renders pages there yet, add "
        "one minimal route for exactly that pattern."
    )


def ask_the_founder(kind: PageType, host: str | None) -> dict[str, Any]:
    """The one question a coding agent asks before the first page of this type publishes."""
    noun = NOUNS[kind]
    return {
        "question": f"Where on the site should Tin publish {noun}?",
        "suggestion": SUGGESTED[kind],
        "how_to_suggest": (
            f"Look at the site's existing routes first. If it already shows {noun} or similar "
            "pages under a folder (for example /blog/{slug} or /guides/{slug}), suggest that "
            f"folder; otherwise suggest {SUGGESTED[kind]}. Say the suggestion in one line "
            + (f"with the full address on {host}, " if host else "")
            + "and ask the founder to confirm or name another."
        ),
        "then": {
            "name": "save_page_route",
            "arguments": {"page_type": kind, "route": "<the route the founder confirmed>"},
        },
    }


class PageRouteService:
    def __init__(self, *, database: Any, storage: Any) -> None:
        self.db, self.storage = database, storage

    async def read(self, project_id: UUID, *, revision: str | None = None) -> dict[str, Any]:
        project = await self.db.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
        if revision is None:
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
        raw = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=revision, path=PATH
        )
        if raw is not None and len(raw) > 8_000:
            raise ValueError("Page routes exceed 8 KB.")
        routes = PageRoutes.model_validate_json(raw) if raw else PageRoutes()
        return {"revision": revision, "path": PATH, "routes": dict(routes.routes)}

    async def route_for(self, run: Any) -> str | None:
        kind = page_type(run)
        if kind is None:
            return None
        try:
            saved = await self.read(run.project_id)
        except (LookupError, ValueError):
            return None
        return saved["routes"].get(kind)

    async def save(
        self,
        *,
        project_id: UUID,
        kind: PageType,
        route: str,
        request_id: UUID,
        actor: str,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        from tin_lite.organic_audit import canonical_json
        from tin_lite.project_files import ProjectFileService

        current = await self.read(project_id)
        routes = PageRoutes.model_validate({"routes": {**current["routes"], kind: route}})
        project = await self.db.get_project(project_id)
        result = await ProjectFileService(database=self.db, storage=self.storage).commit(
            project=project,
            actor_clerk_user_id=actor,
            client_id=client_id,
            request_id=request_id,
            expected_revision=current["revision"],
            message=f"Publish {NOUNS[kind]} at {route}",
            changes=[
                {
                    "operation": "upsert",
                    "path": PATH,
                    "content": canonical_json(routes.model_dump()).decode(),
                }
            ],
        )
        return {**asdict(result), "routes": dict(routes.routes)}
