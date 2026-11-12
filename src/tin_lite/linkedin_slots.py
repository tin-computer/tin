"""LinkedIn posting slots: the project's next post, and the times Tin posts it.

A member previews a post and makes it up next instead of publishing it now. That records the
same approval a publish does, for the exact text, images, first comment and account, and keeps
it in `linkedin_up_next`, one post per project: picking another replaces it. The project's
saved "LinkedIn posting slots" workflow carries the times (Monday, Wednesday and Friday at 9:00
to start with). When a slot comes, the scheduled dispatcher hands it to `dispatch_slot`, which
starts the ordinary publish run for that approval, as the member who picked it, or passes when
nothing is up next. A passed slot creates no run.

A post that changes after it was picked leaves up next; so does a post that went out, however
it was published. A slot whose post fails to publish keeps it up next for the next slot. See
docs/linkedin-posting.md.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from tin_lite.integrations import IntegrationError
from tin_lite.workflow_diagrams import DiagramEdge, DiagramNode, WorkflowDiagram

logger = logging.getLogger(__name__)

KEY = "social.linkedin_post_slots"
WORKFLOW_ID = UUID("5e0d3c1a-7f42-4b8e-9a61-2c4f8b3d9e07")
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": {"type": "string", "format": "uuid"}},
    "required": ["project_id"],
}
DEFAULT_SCHEDULE = {
    "cadence": "weekly",
    "weekdays": ["monday", "wednesday", "friday"],
    "local_time": "09:00",
}
PRESENTATION = WorkflowDiagram(
    direction="TD",
    nodes=(
        DiagramNode("slot", "wait", "A posting slot comes", "the days and time you saved"),
        DiagramNode("pick", "gate", "The post up next", "a member picked it from its preview"),
        DiagramNode("check", "step", "Check it hasn't changed", "text, images, comment, page"),
        DiagramNode("publish", "surface", "Post it on LinkedIn", "with its first comment"),
        DiagramNode("pass", "ghost", "Nothing up next", "the slot passes, nothing runs"),
    ),
    edges=(
        DiagramEdge("slot", "pick"),
        DiagramEdge("pick", "check", "signal", "when a post is up next"),
        DiagramEdge("check", "publish"),
        DiagramEdge("slot", "pass", "signal", "if nothing is picked"),
    ),
)
# How long before an empty slot Decisions asks for a pick.
NOTICE_BEFORE = timedelta(hours=24)

UP_NEXT = """
SELECT approval_id, draft_path, post_id, fingerprint, picked_by_clerk_user_id, picked_at
FROM linkedin_up_next WHERE project_id = $1
"""
PICK = """
INSERT INTO linkedin_up_next (
  project_id, approval_id, draft_path, post_id, fingerprint, picked_by_clerk_user_id
) VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (project_id) DO UPDATE SET
  approval_id = EXCLUDED.approval_id, draft_path = EXCLUDED.draft_path,
  post_id = EXCLUDED.post_id, fingerprint = EXCLUDED.fingerprint,
  picked_by_clerk_user_id = EXCLUDED.picked_by_clerk_user_id, picked_at = now()
RETURNING picked_at
"""
CLEAR = (
    "DELETE FROM linkedin_up_next WHERE project_id = $1 AND ($2::uuid IS NULL OR approval_id = $2)"
)
CLEAR_POST = (
    "DELETE FROM linkedin_up_next WHERE project_id = $1 AND draft_path = $2 AND post_id = $3"
)
SLOTS = """
SELECT id, name, schedule, next_run_at, status FROM project_workflows
WHERE project_id = $1 AND workflow_id = $2 AND status <> 'archived'
ORDER BY created_at, id
LIMIT 1
"""
EVENT = """
INSERT INTO activity_events (project_id, event_type, details, summary, audience, dedupe_key)
VALUES ($1, $2, $3::jsonb, $4, 'product', $5)
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
"""


def _title(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= 120 else f"{line[:119].rstrip()}…"


async def up_next(db, project_id: UUID):
    return await db.pool.fetchrow(UP_NEXT, project_id)


async def clear_posted(db, project_id: UUID, path: str, post_id: str) -> None:
    """A post that went out is nobody's next post, whether a slot or a member published it."""

    await db.pool.execute(CLEAR_POST, project_id, path, post_id)


async def clear_changed(db, project_id: UUID, path: str, before: dict, after: dict) -> None:
    """Take the post up next out when a saved edit changed what it would publish.

    The slot checks the exact snapshot again before posting; this makes the reader say so as
    soon as the edit is saved.
    """

    row = await up_next(db, project_id)
    if row is None or row["draft_path"] != path:
        return
    fields = ("text", "target", "attachments", "first_comment")
    old = next((post for post in before.get("posts", []) if post["id"] == row["post_id"]), None)
    new = next((post for post in after.get("posts", []) if post["id"] == row["post_id"]), None)
    if old is not None and new is not None and all(old.get(f) == new.get(f) for f in fields):
        return
    await db.pool.execute(CLEAR, project_id, row["approval_id"])
    await _event(
        db,
        project_id,
        "linkedin_up_next_changed",
        {"draft_path": path, "post_id": row["post_id"]},
        "The post up next changed after it was picked, so it left up next. Pick it again "
        "to post it at the next slot.",
        f"linkedin-up-next:{row['approval_id']}:changed",
    )


async def _event(db, project_id, kind, details, summary, dedupe_key) -> None:
    await db.pool.execute(EVENT, project_id, kind, json.dumps(details), summary, dedupe_key)


async def up_next_title(db, project_id: UUID) -> str | None:
    """The first line of the post up next, for the calendar's next slot."""

    row = await up_next(db, project_id)
    if row is None:
        return None
    receipt = await db.get_effect(f"linkedin:approved:{row['approval_id']}")
    approval = receipt.result if receipt and receipt.status == "completed" else {}
    return _title(approval.get("text", "")) or "the post up next"


async def slots(db, project_id: UUID):
    """The project's posting slots: the saved workflow that carries their times."""

    return await db.pool.fetchrow(SLOTS, project_id, WORKFLOW_ID)


def _slots_view(row) -> dict | None:
    if row is None:
        return None
    schedule = row["schedule"]
    if isinstance(schedule, str):
        schedule = json.loads(schedule)
    return {
        "project_workflow_id": str(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "schedule": schedule,
        "next_at": row["next_run_at"].isoformat() if row["next_run_at"] else None,
    }


class UpNext:
    """Making a previewed post up next, taking it out, and what the reader shows of it."""

    def __init__(self, posts) -> None:
        self.posts = posts
        self.db = posts.db

    async def state(self, project_id: UUID, actor: str) -> dict[str, Any]:
        project = await self.posts.project(project_id, actor)
        row = await up_next(self.db, project_id)
        slot = _slots_view(await slots(self.db, project_id))
        # Where "Pick a post" opens: the newest batch, when the project has slots to pick for.
        batch = await self._newest(project) if slot is not None else None
        if row is None:
            return {"up_next": None, "slots": slot, "batch": batch}
        receipt = await self.db.get_effect(f"linkedin:approved:{row['approval_id']}")
        approval = receipt.result if receipt and receipt.status == "completed" else {}
        account = approval.get("account") or {}
        return {
            "up_next": {
                "path": row["draft_path"],
                "post_id": row["post_id"],
                "title": _title(approval.get("text", "")),
                "account": {key: account.get(key) for key in ("urn", "kind", "name")},
                "picked_at": row["picked_at"].isoformat(),
                "picked_by_you": row["picked_by_clerk_user_id"] == actor,
            },
            "slots": slot,
            "batch": batch,
        }

    async def _newest(self, project) -> str | None:
        paths, _ = await self.posts.storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
        batches = [
            path
            for path in paths
            if path.startswith("social/linkedin/drafts/") and path.endswith(".json")
        ]
        return max(batches) if batches else None

    async def pick(self, project_id: UUID, actor: str, *, preview_token, request_id) -> dict:
        """Approve the previewed post and make it the project's next post."""

        configured = await slots(self.db, project_id)
        if configured is None or configured["status"] != "active":
            raise ValueError(
                "Set up LinkedIn posting slots in My system first, so Tin knows when to post it."
            )
        approval_id, approval = await self.posts.approve(
            project_id, actor, preview_token=preview_token, request_id=request_id
        )
        if approval.get("account", {}).get("kind") != "organization":
            raise ValueError("Posting slots post as a page. Choose the page to post as.")
        await self.db.pool.fetchrow(
            PICK,
            project_id,
            approval_id,
            approval["path"],
            approval["post_id"],
            approval["fingerprint"],
            actor,
        )
        return await self.state(project_id, actor)

    async def clear(self, project_id: UUID, actor: str) -> dict:
        await self.posts.project(project_id, actor)
        await self.db.pool.execute(CLEAR, project_id, None)
        return await self.state(project_id, actor)

    async def notice(self, project_id: UUID, actor: str, *, now: datetime | None = None):
        """What Decisions shows the day before a slot with nothing up next, or None.

        Worked out on read: there is no run or stored row behind it. It says how many posts
        are ready to pick, or that the newest batch is still waiting for its cards.
        """

        now = now or datetime.now(UTC)
        state = await self.state(project_id, actor)
        slot = state["slots"]
        if state["up_next"] is not None or slot is None or slot["status"] != "active":
            return None
        if not slot["next_at"]:
            return None
        at = datetime.fromisoformat(slot["next_at"])
        if not now <= at <= now + NOTICE_BEFORE:
            return None
        ready, waiting, newest = await self._batches(project_id)
        return {
            "id": f"linkedin-slot:{slot['project_workflow_id']}:{at.isoformat()}",
            "slot_at": at.isoformat(),
            "timezone": (slot["schedule"] or {}).get("timezone"),
            "schedule": slot["schedule"],
            "ready": ready,
            "waiting_for_cards": waiting,
            "path": newest,
        }

    async def _batches(self, project_id: UUID) -> tuple[int, int, str | None]:
        """Unposted posts in the project's LinkedIn batches: ready to pick, or still waiting
        for an image, and the newest batch's path."""

        from tin_lite.linkedin_posts import KEY as PUBLISH_KEY
        from tin_lite.linkedin_posts import MAX_DRAFT_BYTES, validate_draft

        project = await self.db.get_project(project_id)
        paths, revision = await self.posts.storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
        batches = sorted(
            (
                path
                for path in paths
                if path.startswith("social/linkedin/drafts/") and path.endswith(".json")
            ),
            reverse=True,
        )[:6]
        published = {
            (row["path"], row["post_id"])
            for row in await self.db.pool.fetch(
                """
                SELECT a.result->>'path' AS path, a.result->>'post_id' AS post_id
                FROM workflow_runs r JOIN effect_receipts a
                  ON a.execution_key = 'linkedin:approved:' || (r.input->>'approval_id')
                WHERE r.project_id = $1 AND r.executor = $2 AND r.status = 'succeeded'
                """,
                project_id,
                PUBLISH_KEY,
            )
        }
        ready = waiting = 0
        for path in batches:
            raw = await self.posts.storage.read_bounded_project_file(
                repo_id=project.state_repo_id,
                commit_sha=revision,
                path=path,
                max_bytes=MAX_DRAFT_BYTES,
            )
            try:
                draft = validate_draft(raw) if raw is not None else {"posts": []}
            except ValueError:
                continue
            for post in draft["posts"]:
                if (path, post["id"]) in published:
                    continue
                if post["readiness"] == "ready" and not post["missing_assets"]:
                    ready += 1
                else:
                    waiting += 1
        return ready, waiting, batches[0] if batches else None


async def dispatch_slot(acts, configured, schedule, scheduled_for) -> dict[str, str]:
    """A posting slot came: start the publish run for the post up next, or pass.

    The run starts as the member who approved the post, through the same gate a publish
    goes through, and the scheduled dispatcher runs it. A passed slot creates no run.
    """

    from tin_lite.billing_contracts import BillingError
    from tin_lite.linkedin_posts import KEY as PUBLISH_KEY
    from tin_lite.linkedin_posts import WORKFLOW_ID as PUBLISH_WORKFLOW_ID
    from tin_lite.linkedin_posts import LinkedInPosts, save_effect
    from tin_lite.run_service import WorkflowExecutorUnavailableError, start_workflow_run
    from tin_lite.schedules import next_run_after

    db = acts._db
    project_id = configured.project_id

    async def advance() -> dict[str, str]:
        await db.advance_project_workflow_schedule(
            project_workflow_id=configured.id,
            next_run_at=next_run_after(schedule, scheduled_for),
            expected_settings_revision=configured.settings_revision,
        )
        return {}

    async def leave(kind: str, summary: str) -> dict[str, str]:
        await db.pool.execute(CLEAR, project_id, row["approval_id"])
        await _event(
            db,
            project_id,
            kind,
            {"draft_path": row["draft_path"], "post_id": row["post_id"]},
            summary,
            f"linkedin-up-next:{row['approval_id']}:{kind}",
        )
        return await advance()

    row = await up_next(db, project_id)
    if row is None:
        return await advance()
    receipt = await db.get_effect(f"linkedin:approved:{row['approval_id']}")
    approval = receipt.result if receipt and receipt.status == "completed" else None
    if approval is None:
        return await leave("linkedin_up_next_changed", "The post up next lost its approval.")
    actor = approval["actor"]
    if not await db.has_project_access(project_id=project_id, clerk_user_id=actor):
        return await leave(
            "linkedin_up_next_changed",
            "The member who approved the post up next left the project, so it left up next.",
        )
    runtime = SimpleNamespace(database=db, storage=acts._storage, integrations=acts._integrations)
    posts = LinkedInPosts(runtime, acts._settings)
    try:
        current = await posts.snapshot(project_id, actor, row["draft_path"], row["post_id"])
    except IntegrationError:
        # LinkedIn posting needs reconnecting. The publish run fails saying so, where the
        # founder sees it, and the post stays up next for the next slot.
        current = None
    except (LookupError, ValueError):
        current = {"fingerprint": None}
    if current is not None and current["fingerprint"] != row["fingerprint"]:
        return await leave(
            "linkedin_up_next_changed",
            "The post up next changed after it was picked, so the slot passed and it left "
            "up next. Pick it again to post it at the next slot.",
        )
    # An earlier slot whose run ended without sending doesn't use the approval up: the next
    # attempt gets its own ID, approved by the same member for the same exact post.
    approval_id = await posts._approval_id(project_id, row["fingerprint"])
    if approval_id != row["approval_id"]:
        await save_effect(db, f"linkedin:approved:{approval_id}", approval)
    start_key = f"linkedin-publish:{approval_id}"
    existing = await db.get_run_by_start_key(project_id=project_id, start_idempotency_key=start_key)
    if existing is not None and existing.status.value == "succeeded":
        return await leave("linkedin_up_next_posted", "The post up next was already posted.")
    if existing is not None and existing.status.value in {"failed", "stopped"}:
        # It was sent and Tin never heard back: it may be on the page already.
        return await leave(
            "linkedin_up_next_uncertain",
            "LinkedIn may already have the post up next, so Tin didn't post it again. Check "
            "the page.",
        )
    workflow = await db.get_workflow(PUBLISH_WORKFLOW_ID)
    if workflow is None:
        logger.warning("LinkedIn publishing is not in the catalog; sync the workflow catalog")
        return await advance()
    try:
        run = await start_workflow_run(
            runtime=runtime,
            settings=acts._settings,
            workflow=workflow,
            project_id=project_id,
            started_by_clerk_user_id=actor,
            start_idempotency_key=start_key,
            input_payload={"approval_id": str(approval_id)},
            trigger_source="schedule",
            _prepare_only=True,
            _linkedin_publication=True,
        )
    except (
        BillingError,
        IntegrationError,
        LookupError,
        ValueError,
        WorkflowExecutorUnavailableError,
    ) as exc:
        logger.warning(
            "A LinkedIn posting slot did not start for %s: %s", project_id, type(exc).__name__
        )
        await _event(
            db,
            project_id,
            "linkedin_slot_not_started",
            {"draft_path": row["draft_path"], "post_id": row["post_id"]},
            "The posting slot didn't start, so the post stays up next for the next slot.",
            f"linkedin-slot:{configured.id}:{scheduled_for.isoformat()}:not-started",
        )
        return await advance()
    await advance()
    if run.executor != PUBLISH_KEY or run.status.value not in {"pending", "running"}:
        return {}
    return {
        "run_id": str(run.id),
        "executor": run.executor,
        "temporal_workflow_id": run.temporal_workflow_id,
    }
