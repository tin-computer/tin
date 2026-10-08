"""website.change: the copy fix qa.feedback_to_fix planned, applied after the founder approves it.

qa.feedback_to_fix (Product QA) reads public threads where people react to the product and,
when at least two different people misread the same existing copy, plans the wording change in
`reports/feedback-to-fix/{run_id}/PLAN.md` (feedback_fix_plan). It opens no pull request.

When the run publishes its plan, Tin records it as one `feedback` row in `website_changes`
(kind `copy`, the changed page's route as its path), so the founder reads the before and after
wording and the quotes behind it in Decisions. Approving the row starts website.change with
`source: feedback`: Tin opens the pull request with exactly the planned files and merges it once
the repository's required checks pass, unless it touches a protected page. The rules are the
other planned patches' (website_change_patch).

The row's ID comes from the quotes and files behind the change, not its wording, so a change
the founder declined is not proposed again until new voices join it. A newer run replaces a
pending change; a newer run that plans nothing withdraws it.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from tin_lite import feedback_fix_plan
from tin_lite import website_change_patch as patch

SOURCE = "feedback"
KIND = "copy"
PLAN_WORKFLOW = feedback_fix_plan.WORKFLOW_KEY
NONE_YET = (
    "No copy fix yet: Turn what people say into a copy fix has not finished a run in this "
    "project, so there is nothing to apply."
)
RERUN = "Run Turn what people say into a copy fix again."
# How many quote links a change row and its pull request carry; the plan keeps them all.
SHOWN_QUOTES = 10

logger = logging.getLogger(__name__)


def plan_path(run_id) -> str:
    return feedback_fix_plan.PATH_TEMPLATE.format(run_id=UUID(str(run_id)))


def parse_plan(text: str) -> dict[str, Any]:
    """The plan's patch; NoPatch when the run planned no change, ValueError when it is broken."""
    parsed = feedback_fix_plan.parse(text)
    found = parsed["patch"]
    if found is None:
        reason = " ".join(str(parsed["summary"].get("reason") or "").split())[:300]
        raise patch.NoPatch(
            f"The newest feedback run planned no copy change ({parsed['summary']['outcome']}): "
            f"{reason.rstrip('.')}."
        )
    return {
        **{
            key: found[key]
            for key in (
                "repository",
                "base_ref",
                "base_sha",
                "route",
                "kind",
                "people",
                "quotes",
                "edits",
            )
        },
        "summary": " ".join(found["summary"].split()),
        "files": [
            {key: item[key] for key in ("path", "action", "content")} for item in found["files"]
        ],
    }


def _detail(plan: dict) -> dict:
    return {
        "theme_kind": plan["kind"],
        "people": plan["people"],
        "quote_count": len(plan["quotes"]),
        "quotes": plan["quotes"][:SHOWN_QUOTES],
        "edits": plan["edits"],
    }


def _edit_lines(plan: dict) -> list[str]:
    return [f"- `{edit['path']}`: “{edit['before']}” → “{edit['after']}”" for edit in plan["edits"]]


def _plan_lines(source: dict, plan: dict) -> list[str]:
    return [
        f"From qa.feedback_to_fix run `{source['plan_run_id']}`, read at `{plan['base_sha']}` "
        f"of {plan['base_ref']}, for {plan['route']}: {plan['people']} different people said "
        "it in public threads.",
        "",
        *_edit_lines(plan),
        "",
        "Tin applied the plan's files as they are.",
    ]


def _body_lines(plan: dict) -> list[str]:
    shown = plan["quotes"][:SHOWN_QUOTES]
    more = len(plan["quotes"]) - len(shown)
    return [
        plan["summary"],
        "",
        "What changes:",
        "",
        *_edit_lines(plan),
        "",
        f"Why: {plan['people']} different people said it in public threads:",
        "",
        *[f"- {url}" for url in shown],
        *([f"- and {more} more in the plan"] if more > 0 else []),
    ]


SPEC = patch.PatchSource(
    source=SOURCE,
    kind=KIND,
    prefix="fb",
    plan_workflow=PLAN_WORKFLOW,
    noun="copy fix",
    label="copy fix",
    title_prefix="Copy fix",
    rerun=RERUN,
    none_yet=NONE_YET,
    plan_path=plan_path,
    parse_plan=parse_plan,
    max_plan_bytes=feedback_fix_plan.MAX_PLAN_BYTES,
    detail=_detail,
    plan_lines=_plan_lines,
    body_lines=_body_lines,
    identity=feedback_fix_plan.quotes_digest,
)


async def plan_changes(
    *, database, storage, integrations, project_id: UUID, inputs: dict, bind: bool = True
) -> dict[str, Any]:
    """Record the newest copy fix as one row and say whether the next run applies it."""
    return await patch.plan_changes(
        SPEC,
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
        bind=bind,
    )


async def select_source(*, database, storage, integrations, project_id, inputs) -> dict:
    return await patch.select_source(
        SPEC,
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
    )


async def guard_source(conn, *, project_id, inputs, source) -> None:
    return await patch.guard_source(SPEC, conn, project_id=project_id, inputs=inputs, source=source)


async def apply(*, database, storage, integrations, run, sleep=None, clock=None) -> bool:
    return await patch.apply(
        SPEC,
        database=database,
        storage=storage,
        integrations=integrations,
        run=run,
        sleep=sleep,
        clock=clock,
    )


async def record(*, database, storage, integrations, run, revision: str) -> None:
    """Put a just-published plan in Decisions: its copy fix as a pending row, or withdraw an
    older pending one when this run planned nothing.

    The run already paid for its plan, so nothing here fails it: a row Tin couldn't record now
    is recorded by the next preview (preflight_website_change with source feedback).
    """
    try:
        await patch.plan_changes(
            SPEC,
            database=database,
            storage=storage,
            integrations=integrations,
            project_id=run.project_id,
            inputs={},
            bind=False,
            plan_run={"id": run.id, "canonical_commit_sha": revision},
        )
    except Exception:
        logger.warning("feedback_fix_row_not_recorded", extra={"run_id": str(run.id)})


async def start_after_approval(*, runtime, settings, project_id: UUID, change: dict, actor: str):
    """Start website.change for an approved copy fix: Tin opens the pull request and merges it
    once the required checks pass. Returns (run, None), or (None, why it did not start); the
    approval stands either way."""
    from tin_lite.content_repository_delivery import WEBSITE_CHANGE_ID
    from tin_lite.integrations import IntegrationError
    from tin_lite.run_service import start_workflow_run

    workflow = await runtime.database.get_workflow(WEBSITE_CHANGE_ID)
    if workflow is None:
        return None, "website.change is not installed on this Tin."
    try:
        run = await start_workflow_run(
            runtime=runtime,
            settings=settings,
            workflow=workflow,
            project_id=project_id,
            started_by_clerk_user_id=actor,
            start_idempotency_key=(
                f"website-change:{change['change_id']}:{change['content_sha256'][:16]}"
            ),
            input_payload={
                "source": SOURCE,
                "expected_repository": change["detail"]["repository"],
            },
        )
    except (IntegrationError, LookupError, RuntimeError, ValueError) as exc:
        # BillingError and an uncertain Temporal start are RuntimeErrors.
        return None, str(exc)
    return run, None
