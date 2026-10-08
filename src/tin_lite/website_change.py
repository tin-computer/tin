"""website.change: approved changes to a founder's website.

In the organic traffic system every change to the site is meant to flow through here after
the founder approves it: content drafts, page decisions (URL changes), the page tree, the blog
index and technical fixes. Each is a change row: a source, a stable ID, a kind, the site
paths it touches and its approval (ChangeRow). Two sources are implemented:

- `content_draft` (phase 1): an approved content.generate article, answer page or public
  article, adapted into the site's own format and route with content.deliver's machinery
  (content_repository_delivery);
- `audit` (phase 2, website_change_audit): what the latest organic audit found, repaired
  under site-fix-v5's rules, one change row per fixable finding;
- `planned` (phase 3, website_change_planned): the redirects and noindex changes page
  decisions and the site architecture plan made, one row per change, written the same way;
- `blog_index` (phase 3, website_change_blog_index): the newest blog index plan, one row,
  its files applied as they are;
- `feedback` (website_change_feedback): the newest copy fix qa.feedback_to_fix planned from
  what people said in public, one row, its files applied as they are once approved.

Two modes, decided by whether the change is pre-approved to commit to main:

- Pre-approved with the founder's commit-to-main delivery, and touching no protected path: Tin
  opens the pull request and merges it once the repository's required checks pass, under
  content.deliver's merge rules (`page_only`, `chosen_route`).
- Anything else: Tin opens an unmerged pull request, and the founder merges it.

Pre-approved means a recorded approve action in Postgres: who approved it, when, and the exact
revision and SHA-256 it covers. For a page that is its own review decision, when the review
records the approver. For every other source it is a row in `website_changes`, decided once
per stable change ID, so a row the founder approved or declined never comes back. No project
file is ever read as approval, so editing a file cannot publish anything.

Protected paths always open a pull request, even when approved: the auth pages a site shares
with another app (/sign-in, /sign-up, /auth-complete), the project's protected pages (a setting
in `project_protected_paths`, with who changed it and when) and the run's `protected_paths`. On
a site repository a merge is a deploy.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from tin_lite import content_draft
from tin_lite import content_repository_delivery as delivery
from tin_lite import technical_repair_plan as repair_plan

KEY = "website.change"
WORKFLOW_ID = delivery.WEBSITE_CHANGE_ID
OPERATION = delivery.WEBSITE_CHANGE_OPERATION
# Every source a change row can come from, with the kinds of change it makes. An audit row's
# kind is the site-fix-v5 repair it plans (html_noindex, sitemap_add_urls, merge_redirect, …).
SOURCES: dict[str, tuple[str, ...]] = {
    "content_draft": ("page",),
    "audit": tuple(sorted({repair.kind for repair in repair_plan.REPAIRS.values()})),
    "planned": ("redirect", "noindex"),
    "blog_index": ("index",),
    "feedback": ("copy",),
}
# Sources website.change takes: an approved page, the latest audit's fixes, the URL changes
# page decisions and the site architecture plan made, the blog index plan and the copy fix.
IMPLEMENTED_SOURCES = ("content_draft", "audit", "planned", "blog_index", "feedback")
# Sources whose plan names its files, applied as they are (website_change_patch).
PATCH_SOURCES = ("blog_index", "feedback")
# Sources whose approval is a row in website_changes. A page's approval is its own review.
RECORDED_SOURCES = tuple(source for source in SOURCES if source != "content_draft")
DECISIONS = {"approve": "approved", "decline": "declined"}
# Auth pages a site shares with another app, such as its login provider. A change to them is
# always the founder's to merge (an approved noindex on /sign-in once touched such pages).
PROTECTED_PATHS = ("/sign-in", "/sign-up", "/auth-complete")
MAX_PROTECTED_PATHS = 20
PROTECTED_PATH_PATTERN = r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,200}$"
CHANGE_ID = re.compile(r"[a-z]{2}_[0-9a-f]{20}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
SITE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/{}-]{0,300}")
# Tin's own folder for answer-page drafts. A site does not serve it; a page never lands there.
TIN_DRAFT_FOLDERS = ("content/answers/",)
# The founder's page type (content/page-routes.json) for each kind of approved page.
SOURCE_PAGE_TYPES = {"answer_page": "answer_page", "public_article": "article"}


class WebsiteChangeConflict(ValueError):
    """The change row is not in the state the caller read; nothing was recorded."""


class RouteNotChosen(ValueError):
    """No one has chosen where this type of page lives on the site; ask the founder once."""

    def __init__(self, question: dict[str, Any]) -> None:
        self.question = question
        then = question["then"]["arguments"]
        super().__init__(
            f"{question['question']} Nobody has chosen yet, so Tin will not guess. Ask the "
            f"founder (suggest {question['suggestion']} or the folder the site's articles "
            f"already use), save the answer with save_page_route(page_type="
            f"{then['page_type']}), then start website.change again."
        )


# The change-row contract.


def site_path(value: Any) -> str | None:
    """A site path from a path, a route pattern such as /blog/{slug}, or a full URL."""
    if not isinstance(value, str):
        return None
    if value.startswith(("http://", "https://")):
        value = urlsplit(value).path or "/"
    if not SITE_PATH.fullmatch(value) or ".." in value:
        return None
    if re.sub(r"\{slug\}", "", value).count("{") or re.sub(r"\{slug\}", "", value).count("}"):
        return None
    return value


@dataclass(frozen=True)
class ChangeRow:
    """One change to the site: where it comes from, what it does, where it lands, and the
    exact content an approval must cover.

    `change_id` is stable across runs for the same proposal (the audit's `oa_` finding IDs,
    `pg_` for a page), so a decided row is never asked about again. `paths` are site paths or
    route patterns (/blog/{slug}). `content_sha256` (and `content_revision`, the project
    revision it was read at) is what the founder approved; different content is a different
    approval.
    """

    change_id: str
    source: str
    kind: str
    title: str
    paths: tuple[str, ...]
    content_sha256: str
    content_revision: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.change_id, str) or not CHANGE_ID.fullmatch(self.change_id):
            raise ValueError("A change ID is two letters, an underscore and 20 hex digits.")
        if self.kind not in SOURCES.get(self.source, ()):
            raise ValueError(f"A {self.source} change cannot be of kind {self.kind!r}.")
        title = " ".join(str(self.title or "").split())[:200]
        if not title:
            raise ValueError("A change needs a title the founder can read.")
        object.__setattr__(self, "title", title)
        paths = []
        for value in self.paths:
            path = site_path(value)
            if path is None:
                raise ValueError(f"{value!r} is not a site path.")
            if path not in paths:
                paths.append(path)
        if len(paths) > 20:
            raise ValueError("A change touches at most 20 site paths.")
        object.__setattr__(self, "paths", tuple(paths))
        if not isinstance(self.content_sha256, str) or not SHA256.fullmatch(self.content_sha256):
            raise ValueError("A change names the SHA-256 of the exact content it makes.")
        if self.content_revision is not None and not REVISION.fullmatch(self.content_revision):
            raise ValueError("A change's revision is a 40-character commit SHA.")
        if not isinstance(self.detail, dict) or len(json.dumps(self.detail)) > 16_000:
            raise ValueError("A change's details stay under 16 KB.")

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "paths": list(self.paths)}


def page_change_id(run_id: Any) -> str:
    """The stable change ID of one approved page run."""
    digest = hashlib.sha256(f"content_draft|{UUID(str(run_id))}".encode()).hexdigest()
    return f"pg_{digest[:20]}"


def page_change(source: dict[str, Any], route: str | None) -> ChangeRow:
    """The change row of an approved page, from its pinned source (page_source)."""
    paths: list[str] = []
    if route:
        paths.append(route)
    else:
        destination = site_path((source.get("item") or {}).get("destination"))
        if destination:
            paths.append(destination)
    return ChangeRow(
        change_id=page_change_id(source["source_run_id"]),
        source="content_draft",
        kind="page",
        title=source["title"],
        paths=tuple(paths),
        content_sha256=source["source_sha256"],
        content_revision=source["source_revision"],
        detail={"source_run_id": source["source_run_id"], "page_type": source["source_kind"]},
    )


# Protected paths.


def _root(path: str) -> str:
    return path.split("?")[0].rstrip("/") or "/"


def protected_paths(*extras: Any) -> list[str]:
    """The paths that always open a pull request: the shared auth pages, then each of
    `extras` (the project's protected pages, the run's input). The defaults never drop out.
    """
    roots = list(PROTECTED_PATHS)
    for extra in extras:
        for value in extra or ():
            if (
                isinstance(value, str)
                and re.fullmatch(PROTECTED_PATH_PATTERN, value)
                and ".." not in value
                and _root(value) != "/"
                and _root(value) not in roots
            ):
                roots.append(_root(value))
    return roots[: len(PROTECTED_PATHS) + MAX_PROTECTED_PATHS * max(1, len(extras))]


def protected(path: Any, roots: list[str] | tuple[str, ...]) -> str | None:
    """The protected path this site path is, or sits under; None when it is free."""
    page = site_path(path)
    if page is None:
        return None
    page = _root(page.replace("{slug}", "page"))
    for root in roots:
        root = _root(root)
        if root != "/" and (page == root or page.startswith(root + "/")):
            return root
    return None


def file_protected(path: str, roots: list[str] | tuple[str, ...]) -> str | None:
    """The protected path a repository file serves, judged by its folders and name.

    `src/app/sign-in/[[...sign-in]]/page.tsx`, `pages/sign-in.tsx` and
    `app/(auth)/sign-up/page.tsx` serve /sign-in and /sign-up; route groups in parentheses
    are skipped.
    """
    parts = path.split("/")
    stem = parts[-1].split(".", 1)[0]
    segments = [
        part for part in (*parts[:-1], stem) if part and not (part[0] == "(" and part[-1] == ")")
    ]
    for root in roots:
        wanted = [part for part in _root(root).strip("/").split("/") if part]
        if wanted and any(
            segments[index : index + len(wanted)] == wanted
            for index in range(len(segments) - len(wanted) + 1)
        ):
            return _root(root)
    return None


# The project's protected pages: a setting in Postgres, with its history.


def normalize_protected_paths(values: Any) -> list[str]:
    """The founder's protected pages as Tin stores them: site paths, without the defaults.

    Each entry is a site path or a full URL. Tin keeps only the path, drops a query, a
    fragment and a trailing slash, and collapses repeated slashes. A path the defaults
    already cover (/sign-in/sso) or a repeat is dropped. The whole site (/), route patterns
    and anything else that is not a site path are refused, as are more than
    MAX_PROTECTED_PATHS pages.
    """
    if not isinstance(values, list | tuple):
        raise ValueError("List the protected pages as site paths, such as /partners.")
    paths: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Each protected page is a site path, such as /partners.")
        text = value.strip()
        if text.startswith(("http://", "https://")):
            text = urlsplit(text).path or "/"
        text = re.sub(r"/{2,}", "/", text.split("#", 1)[0].split("?", 1)[0])
        path = _root(text)
        if path == "/":
            raise ValueError(
                "Tin cannot protect the whole site (/). Name the pages that must wait for "
                "the founder, such as /partners."
            )
        if not re.fullmatch(PROTECTED_PATH_PATTERN, path) or ".." in path:
            raise ValueError(f"{value!r} is not a site path such as /partners.")
        if protected(path, PROTECTED_PATHS) or path in paths:
            continue
        paths.append(path)
    if len(paths) > MAX_PROTECTED_PATHS:
        raise ValueError(
            f"Protect at most {MAX_PROTECTED_PATHS} pages besides /sign-in, /sign-up and "
            "/auth-complete. A protected path also covers every page under it."
        )
    return paths


def _setting_view(row: Any) -> dict[str, Any]:
    paths = json.loads(row["paths"]) if isinstance(row["paths"], str) else list(row["paths"])
    return {
        "revision": row["revision"],
        "paths": paths,
        "changed_by": row["changed_by_clerk_user_id"],
        "changed_at": row["changed_at"].isoformat(),
    }


async def project_protected_paths(executor: Any, *, project_id: UUID) -> dict[str, Any]:
    """The project's protected pages as they stand now: the newest saved revision.

    `paths` holds the founder's pages only; `effective` adds the defaults, which no one can
    remove. Revision 0 means nobody saved any.
    """
    row = await executor.fetchrow(
        "SELECT * FROM project_protected_paths WHERE project_id=$1 ORDER BY revision DESC LIMIT 1",
        project_id,
    )
    current = (
        _setting_view(row)
        if row
        else {"revision": 0, "paths": [], "changed_by": None, "changed_at": None}
    )
    return {
        "defaults": list(PROTECTED_PATHS),
        **current,
        "effective": protected_paths(current["paths"]),
    }


async def read_protected_paths(
    database, *, project_id: UUID, actor: str, history: int = 20
) -> dict[str, Any]:
    """The setting for a project member, with its newest `history` revisions: who changed it,
    when and to what."""
    if not await database.has_project_access(project_id=project_id, clerk_user_id=actor):
        raise LookupError("Project not found.")
    rows = await database.pool.fetch(
        "SELECT * FROM project_protected_paths WHERE project_id=$1 ORDER BY revision DESC LIMIT $2",
        project_id,
        max(1, min(int(history), 100)),
    )
    return {
        **await project_protected_paths(database.pool, project_id=project_id),
        "history": [_setting_view(row) for row in rows],
    }


async def save_protected_paths(
    database,
    *,
    project_id: UUID,
    actor: str,
    paths: Any,
    expected_revision: int,
    request_id: UUID,
) -> dict[str, Any]:
    """Save the project's protected pages as a new revision, recording who and when.

    The caller names the revision it read (0 before the first save), so two people never
    overwrite each other unseen. Replaying the same request returns what it saved. The
    defaults stay protected whatever the list holds; an empty list keeps only them.
    """
    normalized = normalize_protected_paths(paths)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision is the revision you read, 0 before the first save.")
    async with database.pool.acquire() as conn, conn.transaction():
        # Same order as change decisions: the project row, then the setting.
        await conn.execute("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
        if not await database.has_project_access(
            project_id=project_id, clerk_user_id=actor, conn=conn
        ):
            raise LookupError("Project not found.")
        replay = await conn.fetchrow(
            "SELECT * FROM project_protected_paths WHERE project_id=$1 AND request_id=$2",
            project_id,
            request_id,
        )
        if replay is not None:
            saved = _setting_view(replay)
            if saved["changed_by"] != actor or saved["paths"] != normalized:
                raise WebsiteChangeConflict(
                    "This request ID already saved other protected pages. Use a new request ID."
                )
        else:
            current = await conn.fetchval(
                "SELECT coalesce(max(revision), 0) FROM project_protected_paths "
                "WHERE project_id=$1",
                project_id,
            )
            if current != expected_revision:
                raise WebsiteChangeConflict(
                    "The protected pages changed since you read them. Read them again first."
                )
            await conn.execute(
                "INSERT INTO project_protected_paths "
                "(project_id, revision, paths, changed_by_clerk_user_id, request_id) "
                "VALUES ($1, $2, $3::jsonb, $4, $5)",
                project_id,
                current + 1,
                json.dumps(normalized),
                actor,
                request_id,
            )
    return await read_protected_paths(database, project_id=project_id, actor=actor)


# Approvals: recorded in Postgres, never read from a file.


async def review_approval(executor: Any, run_id: UUID) -> dict[str, Any] | None:
    """A page run's review decision as an approval, only when it records the approver."""
    row = await executor.fetchrow(
        "SELECT status, review_decision, reviewed_by_clerk_user_id, reviewed_at, "
        "canonical_commit_sha FROM workflow_runs WHERE id=$1",
        run_id,
    )
    if (
        row is None
        or row["status"] != "succeeded"
        or row["review_decision"] != "approved"
        or not row["reviewed_by_clerk_user_id"]
        or row["reviewed_at"] is None
    ):
        return None
    return {
        "decision": "approved",
        "by": row["reviewed_by_clerk_user_id"],
        "at": row["reviewed_at"].isoformat(),
        "revision": row["canonical_commit_sha"],
        "via": "review",
    }


async def approval_for(executor: Any, *, project_id: UUID, change: dict[str, Any]) -> dict | None:
    """The recorded approval that covers exactly this change, or None.

    A page is approved by its own review. Every other change is approved by its row in
    website_changes, and only for the content the founder read: a row whose content changed
    since, a pending row or a declined row is not an approval. Raises for a declined row.
    """
    if change["source"] == "content_draft":
        approval = await review_approval(executor, UUID(change["detail"]["source_run_id"]))
        if approval is None or approval["revision"] != change.get("content_revision"):
            return None
        return {**approval, "content_sha256": change["content_sha256"]}
    row = await executor.fetchrow(
        "SELECT * FROM website_changes WHERE project_id=$1 AND change_id=$2",
        project_id,
        change["change_id"],
    )
    if row is None or row["status"] == "pending":
        return None
    if row["status"] == "declined":
        raise WebsiteChangeConflict("The founder declined this change. Tin will not make it.")
    if row["content_sha256"] != change["content_sha256"] or (
        row["content_revision"] is not None
        and row["content_revision"] != change.get("content_revision")
    ):
        return None
    return {
        "decision": "approved",
        "by": row["decided_by_clerk_user_id"],
        "at": row["decided_at"].isoformat(),
        "revision": row["content_revision"],
        "content_sha256": row["content_sha256"],
        "via": "decision",
    }


def publish_mode(
    change: dict[str, Any],
    approval: dict[str, Any] | None,
    *,
    roots: list[str],
    asked: str | None = None,
) -> dict[str, str]:
    """Whether Tin may publish this change (merge its PR) or leaves the PR for the founder.

    `asked` is the page's delivery as content.deliver reads it (`chosen_mode`): the choice
    recorded with the approval, else the delivery its draft pinned. Only commit to main lets
    Tin merge, the same rule content.deliver follows; a pull request, keeping the page in Tin
    or no choice at all leaves the PR for the founder.
    """
    hit = next((root for path in change["paths"] if (root := protected(path, roots))), None)
    if approval is None:
        return {
            "mode": "pull_request",
            "reason": "No one approved this change in Tin before it was made, so the pull "
            "request waits for your review and merge.",
        }
    if asked != "github_commit":
        return {
            "mode": "pull_request",
            "reason": "The approval asked for a pull request, so it waits for your merge."
            if asked
            else "The approval did not ask Tin to commit the page to main, so the pull "
            "request waits for your merge.",
        }
    if hit:
        return {
            "mode": "pull_request",
            "reason": f"It touches {hit}, a protected page, so it waits for your review "
            "even though you approved it.",
        }
    return {
        "mode": "direct",
        "reason": "You approved it to commit to main, so Tin merges it once your site's build "
        "and your repository's required checks pass.",
    }


def patch_module(source_kind: str):
    """The module that reads, records and applies a planned patch of this source."""
    if source_kind == "blog_index":
        from tin_lite import website_change_blog_index

        return website_change_blog_index
    if source_kind == "feedback":
        from tin_lite import website_change_feedback

        return website_change_feedback
    raise ValueError(f"{source_kind} is not a planned patch.")


# Admission of the page source.


async def select_source(*, database, storage, integrations, project_id, inputs) -> dict:
    """Pin the change, the approved page, the repository and the publish mode for one run."""
    from tin_lite.content_delivery import ContentDelivery, adapted, choice_key, chosen_mode
    from tin_lite.page_routes import PageRouteService, ask_the_founder

    source_kind = inputs.get("source", "content_draft")
    if source_kind not in IMPLEMENTED_SOURCES:
        raise ValueError(
            "website.change makes approved pages, audit fixes, planned URL changes, the "
            "blog index and copy fixes."
        )
    if source_kind in PATCH_SOURCES:
        return await patch_module(source_kind).select_source(
            database=database,
            storage=storage,
            integrations=integrations,
            project_id=project_id,
            inputs=inputs,
        )
    if source_kind in {"audit", "planned"}:
        from tin_lite import website_change_audit

        return await website_change_audit.select_source(
            database=database,
            storage=storage,
            integrations=integrations,
            project_id=project_id,
            inputs=inputs,
        )
    if not inputs.get("source_run_id"):
        raise ValueError("Choose the approved page to put on the site (source_run_id).")
    source = await delivery.page_source(
        database=database,
        storage=storage,
        project_id=project_id,
        source_run_id=inputs["source_run_id"],
    )
    run = await database.get_run(UUID(source["source_run_id"]))
    intent = await ContentDelivery(database=database).intent(run)
    if intent and not adapted(intent):
        raise ValueError(
            "This page already ships through its Markdown delivery. Use its existing "
            "delivery action."
        )
    choice = await database.get_effect(choice_key(run.id))
    chosen = (choice.result or {}) if choice and choice.status == "completed" else {}
    binding = await integrations.github_repository_binding(
        project_id=project_id, expected_repository=inputs["expected_repository"]
    )
    selected = await database.get_effect(content_draft.selection_key(run.id))
    system_delivery = (selected.result or {}).get("system_delivery") if selected else None
    if system_delivery and system_delivery["mode"] == "github_pr":
        from tin_lite.organic_content import check_destination

        check_destination(system_delivery, binding)
    route = None
    # The page's type comes from its source: content.answer_page's pages and content.generate
    # answer pages are answer pages, public articles are articles, and planned articles keep
    # their own destination.
    kind = SOURCE_PAGE_TYPES.get(source["source_kind"])
    if kind is not None:
        # The route pinned at approval, else the one the founder saved since. Never a guess:
        # an answer page or public article without one asks the founder first.
        route = chosen.get("route") or await PageRouteService(
            database=database, storage=storage
        ).route_for(run, page_type=kind)
        if not route:
            raise RouteNotChosen(ask_the_founder(kind, None))
    change = page_change(source, route).as_dict()
    approval = await approval_for(database.pool, project_id=project_id, change=change)
    # The defaults, the project's protected pages and this run's input, in that order.
    setting = await project_protected_paths(database.pool, project_id=project_id)
    roots = protected_paths(setting["paths"], inputs.get("protected_paths"))
    asked = chosen_mode(intent) if intent else None
    return {
        **source,
        "binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
        "change": {**change, "approval": approval},
        "route": route,
        "protected_paths": roots,
        "protected_paths_revision": setting["revision"],
        "publish": publish_mode(change, approval, roots=roots, asked=asked),
    }


async def guard_source(conn, *, project_id, inputs, source) -> None:
    """Called under create_run's project lock, in the run/budget/receipt transaction."""
    patched = next(
        (kind for kind in (inputs.get("source"), source.get("source")) if kind in PATCH_SOURCES),
        None,
    )
    if patched:
        return await patch_module(patched).guard_source(
            conn, project_id=project_id, inputs=inputs, source=source
        )
    if inputs.get("source") in {"audit", "planned"} or source.get("source") in {
        "audit",
        "planned",
    }:
        from tin_lite import website_change_audit

        return await website_change_audit.guard_source(
            conn, project_id=project_id, inputs=inputs, source=source
        )
    change = source.get("change") or {}
    if (
        inputs["source_run_id"] != source["source_run_id"]
        or change.get("source") != "content_draft"
        or change.get("change_id") != page_change_id(source["source_run_id"])
    ):
        raise ValueError("The selected page is not the change this run was admitted for.")
    await delivery.guard_page(conn, project_id=project_id, source=source)
    current = await approval_for(conn, project_id=project_id, change=change)
    if current != change.get("approval"):
        raise ValueError("The page's approval changed while starting. Start again.")
    await delivery.guard_attempts(conn, project_id=project_id, inputs=inputs, source=source)


# After the pull request opens.


def in_draft_folder(path: str | None) -> bool:
    return bool(path) and any(
        path.startswith(folder) or f"/{folder}" in path for folder in TIN_DRAFT_FOLDERS
    )


async def hold_reason(database, run, source, manifest, proof) -> str | None:
    """Why Tin leaves this website.change pull request open, or None when it may merge.

    Checked again after the pull request opens, from Postgres and the saved patch: the
    publish mode pinned at admission, the approval as it stands now, the protected paths
    against the files and the page's address, and the founder's chosen route. The protected
    paths are the pinned ones plus the project's protected pages as they stand now, so a page
    the founder protects after the run started still holds the merge.
    """
    from tin_lite.page_routes import matches

    publish = source.get("publish") or {}
    if publish.get("mode") != "direct":
        return publish.get("reason") or "It waits for your review and merge."
    change = source["change"]
    try:
        current = await approval_for(database.pool, project_id=run.project_id, change=change)
    except WebsiteChangeConflict:
        current = None
    if current != change.get("approval"):
        return "Its approval changed after Tin made the change, so it waits for your review."
    setting = await project_protected_paths(database.pool, project_id=run.project_id)
    roots = protected_paths(source.get("protected_paths"), setting["paths"])
    address = proof.get("public_route")
    hit = protected(address, roots) or next(
        (root for item in manifest["files"] if (root := file_protected(item["path"], roots))),
        None,
    )
    if hit:
        return f"It touches {hit}, a protected page, so it waits for your review."
    if in_draft_folder(proof.get("article_path")):
        return (
            "It puts the page in content/answers/, Tin's draft folder rather than a page on "
            "the site, so it waits for your review."
        )
    route = source.get("route")
    if route and not matches(route, address):
        return (
            f"It does not put the page at your chosen route {route}, so it waits for your review."
        )
    return None


# The decision rows of the other sources: proposed by a source, decided once by the founder.


def view(row: Any) -> dict[str, Any]:
    """One change row as HTTP and MCP show it."""
    paths = row["paths"]
    detail = row["detail"]
    return {
        "change_id": row["change_id"],
        "source": row["source"],
        "kind": row["kind"],
        "title": row["title"],
        "paths": json.loads(paths) if isinstance(paths, str) else list(paths),
        "content_revision": row["content_revision"],
        "content_sha256": row["content_sha256"],
        "detail": json.loads(detail) if isinstance(detail, str) else dict(detail),
        "status": row["status"],
        "proposed_at": row["proposed_at"].isoformat(),
        "proposed_by_run_id": str(row["proposed_by_run_id"]) if row["proposed_by_run_id"] else None,
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "decided_by": row["decided_by_clerk_user_id"],
    }


async def propose(database, *, project_id: UUID, rows: list[ChangeRow], run_id=None) -> list:
    """Record the rows a source proposes and return each one as stored.

    A decided row stays exactly as the founder left it, so a later weekly run that proposes
    the same stable ID does not ask again. A pending row takes the newer content; the founder
    decides on the content they read (decide checks its SHA-256).
    """
    stored = []
    async with database.pool.acquire() as conn, conn.transaction():
        for row in rows:
            if row.source not in RECORDED_SOURCES:
                raise ValueError("A page is approved through its review in Decisions.")
            if not row.paths:
                raise ValueError("A proposed change names the site paths it touches.")
            stored.append(
                await conn.fetchrow(
                    """
                    INSERT INTO website_changes (id, project_id, change_id, source, kind, title,
                        paths, content_revision, content_sha256, detail, proposed_by_run_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10::jsonb,$11)
                    ON CONFLICT (project_id, change_id) DO UPDATE SET
                        kind = EXCLUDED.kind, title = EXCLUDED.title, paths = EXCLUDED.paths,
                        content_revision = EXCLUDED.content_revision,
                        content_sha256 = EXCLUDED.content_sha256, detail = EXCLUDED.detail,
                        proposed_by_run_id = EXCLUDED.proposed_by_run_id
                    WHERE website_changes.status = 'pending'
                      AND website_changes.source = EXCLUDED.source
                    RETURNING *
                    """,
                    uuid4(),
                    project_id,
                    row.change_id,
                    row.source,
                    row.kind,
                    row.title,
                    json.dumps(list(row.paths)),
                    row.content_revision,
                    row.content_sha256,
                    json.dumps(row.detail),
                    run_id,
                )
                or await conn.fetchrow(
                    "SELECT * FROM website_changes WHERE project_id=$1 AND change_id=$2",
                    project_id,
                    row.change_id,
                )
            )
    return [view(row) for row in stored]


async def retire(database, *, project_id: UUID, source: str, keep: list[str]) -> int:
    """Remove the pending rows of a source that its newest plan no longer proposes.

    Only pending rows go; a decided row stays decided whatever later plans say. Returns how
    many were removed.
    """
    if source not in RECORDED_SOURCES:
        raise ValueError("A page is approved through its review in Decisions.")
    result = await database.pool.execute(
        "DELETE FROM website_changes WHERE project_id=$1 AND source=$2 AND status='pending' "
        "AND NOT (change_id = ANY($3::text[]))",
        project_id,
        source,
        list(keep),
    )
    return int(result.split()[-1])


async def decide(
    database,
    *,
    project_id: UUID,
    change_id: str,
    action: str,
    actor: str,
    request_id: UUID,
    content_sha256: str,
) -> dict[str, Any]:
    """Record the founder's one decision on a change row: approve or decline.

    The caller names the SHA-256 of the content they read, the way a review token binds the
    version a reviewer read. Replaying the same request returns the recorded decision; any
    other decision on a decided row is refused, and a decided row never changes again.
    """
    status = DECISIONS.get(action)
    if status is None:
        raise ValueError("Approve or decline the change.")
    if not isinstance(change_id, str) or not CHANGE_ID.fullmatch(change_id):
        raise LookupError("Change not found.")
    import asyncpg

    async with database.pool.acquire() as conn, conn.transaction():
        # Same order as review arbitration: the project row, then the change row.
        await conn.execute("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
        member = await conn.fetchval(
            "SELECT true FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
            project_id,
            actor,
        )
        row = await conn.fetchrow(
            "SELECT * FROM website_changes WHERE project_id=$1 AND change_id=$2 FOR UPDATE",
            project_id,
            change_id,
        )
        if not member or row is None:
            raise LookupError("Change not found.")
        if row["status"] != "pending":
            if (
                row["decision_request_id"] == request_id
                and row["decided_by_clerk_user_id"] == actor
                and row["status"] == status
            ):
                return view(row)
            raise WebsiteChangeConflict(
                f"This change is already {row['status']}. A decided change stays decided."
            )
        if row["content_sha256"] != content_sha256:
            raise WebsiteChangeConflict(
                "This change was updated after you read it. Read it again before deciding."
            )
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE website_changes
                    SET status=$3, decided_at=now(), decided_by_clerk_user_id=$4,
                        decision_request_id=$5
                    WHERE project_id=$1 AND change_id=$2 AND status='pending'
                    RETURNING *
                    """,
                    project_id,
                    change_id,
                    status,
                    actor,
                    request_id,
                )
        except asyncpg.UniqueViolationError as exc:
            raise WebsiteChangeConflict(
                "This request ID already decided another change. Use a new request ID."
            ) from exc
        if row["proposed_by_run_id"] is not None:
            await database.add_activity(
                conn=conn,
                run_id=row["proposed_by_run_id"],
                event_type=f"website_change_{status}",
                audience="product",
                summary=f"You {status} a change to your site: {row['title']}"[:240],
                details={"kind": "your_edits", "change_id": change_id, "decision": status},
                dedupe_key=f"website-change:{project_id}:{change_id}:{status}",
            )
    return view(row)


async def get_change(database, *, project_id: UUID, change_id: str) -> dict[str, Any] | None:
    row = await database.pool.fetchrow(
        "SELECT * FROM website_changes WHERE project_id=$1 AND change_id=$2",
        project_id,
        change_id,
    )
    return view(row) if row else None


async def list_changes(
    database, *, project_id: UUID, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Change rows, pending first (oldest first), then decided ones (newest first).

    Each says which protected page it touches, if any (`protected`): the defaults and the
    project's setting as they stand now. Such a change always opens a pull request.
    """
    rows = await database.pool.fetch(
        """
        SELECT * FROM website_changes
        WHERE project_id=$1 AND ($2::text IS NULL OR status=$2)
        ORDER BY status <> 'pending', CASE WHEN status = 'pending' THEN proposed_at END,
                 decided_at DESC NULLS LAST, change_id
        LIMIT $3
        """,
        project_id,
        status,
        max(1, min(int(limit), 200)),
    )
    setting = await project_protected_paths(database.pool, project_id=project_id)
    roots = setting["effective"]
    views = []
    for row in rows:
        item = view(row)
        files = [
            entry.get("path")
            for entry in item["detail"].get("files") or []
            if isinstance(entry, dict)
        ]
        item["protected"] = next(
            (root for path in item["paths"] if (root := protected(path, roots))), None
        ) or next(
            (
                root
                for path in files
                if isinstance(path, str) and (root := file_protected(path, roots))
            ),
            None,
        )
        views.append(item)
    return views


async def decided(database, *, project_id: UUID, change_ids: list[str]) -> dict[str, str]:
    """Which of these stable IDs the founder already decided: ID to 'approved' or 'declined'.

    A source drops these before proposing, so a decided row never comes back.
    """
    if not change_ids:
        return {}
    rows = await database.pool.fetch(
        "SELECT change_id, status FROM website_changes "
        "WHERE project_id=$1 AND change_id = ANY($2::text[]) AND status <> 'pending'",
        project_id,
        list(change_ids),
    )
    return {row["change_id"]: row["status"] for row in rows}
