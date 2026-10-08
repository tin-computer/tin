"""website.change: a patch another workflow planned, applied as it is after approval.

Some workflows read the founder's repository, plan the exact files a change needs and open no
pull request: content.blog_index (`source: blog_index`, retired for new work) and
qa.feedback_to_fix (`source: feedback`). Each saves its plan in project files with the full
text of every file, the repository, branch and commit it read, and the site route it changes.

A website.change run with such a source reads the newest succeeded planning run's plan
server-side and records one `website_changes` row: change ID from the source's prefix, the
plan's route as its path, and the SHA-256 of the files as the content an approval covers.
Nothing here needs judgment, so no Codex session runs: Tin opens the pull request with exactly
those files, and under the same mode rules as the other sources merges it once the required
checks pass when the row was approved and touches no protected page. Otherwise the founder
merges it.

A plan read from an older commit is applied only when none of its files changed upstream
since; otherwise the row stays and the start says which file moved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from tin_lite import content_repository_delivery as delivery
from tin_lite import website_change
from tin_lite import website_change_audit as shared
from tin_lite.organic_audit import canonical_json


class NoPatch(ValueError):
    """The newest plan is valid but planned no change; the message says why."""


@dataclass(frozen=True)
class PatchSource:
    """How one source names, reads and reports its plan. The rules are shared."""

    source: str
    kind: str
    prefix: str
    plan_workflow: str
    # "blog index plan": what the founder reads in Decisions and on the run.
    noun: str
    # "blog index": what a run applying it is busy with.
    label: str
    # "Blog index": the pull request title and the report heading.
    title_prefix: str
    rerun: str
    none_yet: str
    plan_path: Callable[[Any], str]
    # The plan from PLAN.md's text; raises NoPatch for a plan without a change.
    parse_plan: Callable[[str], dict]
    max_plan_bytes: int
    # Extra row detail the Decisions card shows, beyond the files.
    detail: Callable[[dict], dict]
    # The lines under "## The plan" in the run's report, and the pull request's opening.
    plan_lines: Callable[[dict, dict], list[str]]
    body_lines: Callable[[dict], list[str]]
    # A row ID that survives a reworded plan; None uses the files' digest.
    identity: Callable[[dict], str] | None = None


def files_sha256(files: list[dict]) -> str:
    return hashlib.sha256(canonical_json(files)).hexdigest()


def change_id(spec: PatchSource, plan: dict) -> str:
    """The prefix and the first 20 hex digits of the plan's identity: the SHA-256 of the
    canonical JSON of `files` unless the source names its own."""
    digest = spec.identity(plan) if spec.identity else files_sha256(plan["files"])
    return f"{spec.prefix}_{digest[:20]}"


def title(spec: PatchSource, plan: dict) -> str:
    return f"{spec.title_prefix}: {plan['summary'] or plan['route']}"


def change_row(spec: PatchSource, plan: dict, run_id) -> website_change.ChangeRow:
    return website_change.ChangeRow(
        change_id=change_id(spec, plan),
        source=spec.source,
        kind=spec.kind,
        title=title(spec, plan)[:200],
        paths=(plan["route"],),
        content_sha256=files_sha256(plan["files"]),
        detail={
            "plan_run_id": str(run_id),
            "repository": plan["repository"],
            "base_ref": plan["base_ref"],
            "base_sha": plan["base_sha"],
            "route": plan["route"],
            "summary": plan["summary"],
            **spec.detail(plan),
            "files": [
                {
                    "path": item["path"],
                    "action": item["action"],
                    "bytes": len(item["content"].encode()),
                }
                for item in plan["files"]
            ],
        },
    )


async def latest_plan_run(spec: PatchSource, database, project_id):
    return await database.pool.fetchrow(
        "SELECT r.id, r.canonical_commit_sha FROM workflow_runs r "
        "JOIN workflows w ON w.id = r.workflow_id "
        "WHERE r.project_id=$1 AND w.key=$2 AND r.status='succeeded' "
        "AND r.canonical_commit_sha IS NOT NULL "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1",
        project_id,
        spec.plan_workflow,
    )


async def read_plan(spec: PatchSource, database, storage, project_id, run_id, revision) -> dict:
    project = await database.get_project(project_id)
    raw = await storage.read_canonical_artifact_if_exists(
        repo_id=project.state_repo_id, commit_sha=revision, path=spec.plan_path(run_id)
    )
    if raw is None:
        raise ValueError(f"The {spec.label} run {run_id} saved no PLAN.md.")
    if len(raw) > spec.max_plan_bytes + 100_000:
        raise ValueError(f"The {spec.noun} is too large.")
    return spec.parse_plan(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)


async def stale_reason(spec: PatchSource, integrations, project_id, plan, binding) -> str | None:
    """Why the plan can't apply to the branch as it is now, or None."""
    if plan["base_ref"] != binding.default_branch:
        return (
            f"The plan was made for {plan['base_ref']}, but the site deploys from "
            f"{binding.default_branch}. {spec.rerun}"
        )
    if plan["base_sha"] == binding.head_sha:
        return None
    try:
        changed = await integrations.github_changed_paths(
            project_id=project_id,
            repository=plan["repository"],
            base=plan["base_sha"],
            head=binding.head_sha,
        )
    except Exception:
        changed = {"paths": [], "complete": False}
    moved = [item["path"] for item in plan["files"] if item["path"] in changed["paths"]]
    if not changed.get("complete"):
        return (
            f"Tin can't tell what changed on {plan['base_ref']} since the plan read it, so it "
            f"leaves the plan as it is. {spec.rerun}"
        )
    if moved:
        return (
            f"{', '.join(moved)} changed on {plan['base_ref']} since the plan read it, so "
            f"Tin leaves the plan as it is. {spec.rerun}"
        )
    return None


async def plan_changes(
    spec: PatchSource,
    *,
    database,
    storage,
    integrations,
    project_id: UUID,
    inputs: dict,
    bind: bool = True,
    plan_run: dict | None = None,
) -> dict[str, Any]:
    """Record the newest plan as one row and say whether the next run applies it.

    `plan_run` (`id`, `canonical_commit_sha`) records a run that just published its plan,
    before it is marked succeeded.
    """
    from tin_lite.website_change import (
        WebsiteChangeConflict,
        approval_for,
        file_protected,
        project_protected_paths,
        protected,
        protected_paths,
    )

    empty = {
        "change_source": spec.source,
        "plan_run_id": None,
        "changes": [],
        "decisions_needed": [],
        "ask": None,
        "next_run": {"mode": None, "reason": None, "change_ids": []},
        "execution_available": False,
    }
    run = plan_run or await latest_plan_run(spec, database, project_id)
    if run is None:
        return {**empty, "note": spec.none_yet}
    flight = await shared.rows_in_flight(
        database, integrations, project_id=project_id, source=spec.source
    )
    try:
        plan = await read_plan(
            spec, database, storage, project_id, run["id"], run["canonical_commit_sha"]
        )
    except NoPatch as exc:
        # The newest plan changes nothing: an older pending change no longer stands.
        await website_change.retire(
            database, project_id=project_id, source=spec.source, keep=list(flight)
        )
        return {**empty, "plan_run_id": str(run["id"]), "note": str(exc)}
    row = change_row(spec, plan, run["id"])
    if inputs.get("expected_repository") and inputs["expected_repository"] != plan["repository"]:
        raise ValueError(
            f"The {spec.noun} is for {plan['repository']}, not {inputs['expected_repository']}."
        )
    binding = None
    if bind and row.change_id not in flight:
        binding = await integrations.github_repository_binding(
            project_id=project_id, expected_repository=plan["repository"]
        )
    [stored] = await website_change.propose(
        database, project_id=project_id, rows=[row], run_id=run["id"]
    )
    await website_change.retire(
        database, project_id=project_id, source=spec.source, keep=[row.change_id, *flight]
    )
    setting = await project_protected_paths(database.pool, project_id=project_id)
    roots = protected_paths(setting["paths"], inputs.get("protected_paths"))
    try:
        approval = await approval_for(database.pool, project_id=project_id, change=row.as_dict())
    except WebsiteChangeConflict:
        approval = None
    hit = protected(plan["route"], roots) or next(
        (root for item in plan["files"] if (root := file_protected(item["path"], roots))), None
    )
    view = {
        **row.as_dict(),
        "status": stored["status"],
        "decided_by": stored["decided_by"],
        "decided_at": stored["decided_at"],
        "approved": approval is not None,
        "approval": approval,
        "protected": hit,
        "suggestion": "ask" if hit else "apply",
    }
    reason, mode = None, None
    if stored["status"] == "declined":
        reason = f"You declined this {spec.noun} in Tin; Tin won't apply it."
    elif row.change_id in flight:
        carried = flight[row.change_id]
        reason = (
            f"This {spec.noun} already sits in PR #{carried['number']} ({carried['state']}): "
            f"{carried['url']}."
        )
    elif binding is not None:
        reason = await stale_reason(spec, integrations, project_id, plan, binding)
    if reason is None and (binding is not None or not bind):
        if approval is not None and not hit:
            mode, reason = "direct", shared.DIRECT_REASON
        elif approval is None:
            mode, reason = "pull_request", shared.UNAPPROVED_REASON
        else:
            mode, reason = (
                "pull_request",
                f"It touches {hit}, a protected page, so the pull request waits for your "
                "review even though you approved it.",
            )
    result = {
        **empty,
        "plan_run_id": str(run["id"]),
        "plan_revision": run["canonical_commit_sha"],
        "plan": {key: plan[key] for key in ("repository", "base_ref", "base_sha", "route")}
        | {"summary": plan["summary"], "files": view["detail"]["files"]},
        "changes": [view],
        "next_run": {
            "mode": mode,
            "reason": reason,
            "change_ids": [row.change_id] if mode else [],
        },
        "protected_paths": roots,
        "protected_paths_revision": setting["revision"],
    }
    if binding is None or mode is None:
        return result
    return {
        **result,
        "repository_binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
        "execution_available": True,
    }


async def select_source(
    spec: PatchSource, *, database, storage, integrations, project_id, inputs
) -> dict:
    """Pin the plan run, its row with its approval, the repository and the publish mode. The
    files are re-read from the pinned plan when the run applies them."""
    preview = await plan_changes(
        spec,
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
    )
    if not preview["next_run"]["change_ids"]:
        raise ValueError(preview["next_run"]["reason"] or preview.get("note") or spec.none_yet)
    [change] = preview["changes"]
    return {
        "source": spec.source,
        "plan_run_id": preview["plan_run_id"],
        "plan_revision": preview["plan_revision"],
        "binding": preview["repository_binding"],
        "changes": [
            {
                key: change[key]
                for key in (
                    "change_id",
                    "source",
                    "kind",
                    "title",
                    "paths",
                    "content_revision",
                    "content_sha256",
                    "detail",
                    "approval",
                    "protected",
                )
            }
        ],
        "protected_paths": preview["protected_paths"],
        "protected_paths_revision": preview["protected_paths_revision"],
        "publish": {
            "mode": preview["next_run"]["mode"],
            "reason": preview["next_run"]["reason"],
        },
    }


async def guard_source(spec: PatchSource, conn, *, project_id, inputs, source) -> None:
    """Under create_run's project lock: the pinned approval still stands, and no other run of
    this project is still applying a plan of the same source."""
    from tin_lite.website_change import WebsiteChangeConflict, approval_for

    if inputs.get("source") != spec.source or source.get("source") != spec.source:
        raise ValueError(f"The {spec.noun} is not the source this run was admitted for.")
    [change] = source["changes"]
    try:
        current = await approval_for(conn, project_id=project_id, change=change)
    except WebsiteChangeConflict:
        current = None
    if current != change.get("approval"):
        raise ValueError(f"The {spec.noun}'s approval changed while starting. Start again.")
    busy = await conn.fetchval(
        "SELECT id FROM workflow_runs WHERE project_id=$1 AND workflow_id=$2 "
        "AND input->>'source'=$3 AND status = ANY($4::text[]) ORDER BY created_at DESC LIMIT 1",
        project_id,
        website_change.WORKFLOW_ID,
        spec.source,
        list(shared.ACTIVE),
    )
    if busy:
        raise ValueError(
            f"Website changes run {busy} is still applying the {spec.label}; wait for it to finish."
        )


def report(spec: PatchSource, source: dict, plan: dict, *, pull_request=None, merge=None) -> bytes:
    lines = [f"# {spec.title_prefix}", "", "## Result", ""]
    if pull_request is not None:
        lines += [f"Pull request: {pull_request.url}", ""]
    if merge:
        lines += [
            f"Tin merged it into {plan['base_ref']}."
            if merge.get("status") == "merged" and merge.get("merged_by") == "tin"
            else "It was merged."
            if merge.get("status") == "merged"
            else f"It is open. {merge.get('reason', '')}".strip(),
            "",
        ]
    [change] = source["changes"]
    approval = change.get("approval")
    lines += [
        "## Website changes",
        "",
        source["publish"]["reason"],
        "",
        f"- `{change['change_id']}` ({spec.kind}): "
        + (
            f"approved by {approval['by']} on {approval['at'][:10]}."
            if approval
            else "waiting for your approval."
        ),
        "",
        "## The plan",
        "",
        *spec.plan_lines(source, plan),
        "",
    ]
    return "\n".join(lines).encode()


def failed_report(spec: PatchSource, plan: dict, reason: str) -> bytes:
    """The report of a run that couldn't apply the plan to the repository as it is now."""
    return "\n".join(
        [
            f"# {spec.title_prefix}",
            "",
            "## Result",
            "",
            f"Tin couldn't apply the {spec.noun}: {reason.rstrip('.')}. No pull request "
            f"was opened, and the change stays waiting in Tin. {spec.rerun}",
            "",
            *[f"- {item['action']} `{item['path']}`" for item in plan["files"]],
            "",
        ]
    ).encode()


def pull_request_body(spec: PatchSource, source: dict, plan: dict) -> str:
    lines = [
        *spec.body_lines(plan),
        "",
        f"Change `{source['changes'][0]['change_id']}` from Tin's website.change.",
        "",
        "Tin merges this pull request once the site's build and the repository's required checks "
        "pass, because you approved it in Tin."
        if source["publish"]["mode"] == "direct"
        else "Review and merge it when it looks right; Tin doesn't merge it.",
    ]
    return "\n".join(lines)


async def apply(spec: PatchSource, *, database, storage, integrations, run, sleep, clock) -> bool:
    """Open the pull request with the plan's files, merge it under the mode rules, and report.
    Each step is receipted, so a retried activity repeats none of them."""
    import asyncio
    from datetime import UTC, datetime

    from tin_lite.integrations import GitHubFileChange, IntegrationAuthorizationError
    from tin_lite.run_reports import publish_run_report
    from tin_lite.website_change import (
        WebsiteChangeConflict,
        approval_for,
        file_protected,
        project_protected_paths,
        protected,
        protected_paths,
    )

    source = await delivery.saved_source(database, run.id)
    plan = await read_plan(
        spec, database, storage, run.project_id, source["plan_run_id"], source["plan_revision"]
    )
    [change] = source["changes"]
    if files_sha256(plan["files"]) != change["content_sha256"]:
        raise ValueError(f"The {spec.noun} changed after this run started.")
    binding = delivery.binding_from(source)
    try:
        pull_request = await integrations.github_create_pull_request(
            project_id=run.project_id,
            execution_key=f"{run.id}:procedure_pull_request",
            title=title(spec, plan)[:200],
            body=pull_request_body(spec, source, plan),
            files=tuple(
                GitHubFileChange(path=item["path"], content=item["content"])
                for item in plan["files"]
            ),
            base_branch=binding.default_branch,
            expected_base_sha=binding.head_sha,
            run_id=run.id,
            expected_binding=binding,
            # Commits that touch none of the plan's files since admission don't block it;
            # one that changes a plan file, or an open PR that does, still does.
            allow_unrelated_base_advance=True,
        )
    except IntegrationAuthorizationError as exc:
        # Tin couldn't apply the plan to the repository as it is now: the run failed; it did
        # not find nothing to change. Same rule as website_change_audit.preparation_failed.
        await publish_run_report(
            database=database,
            storage=storage,
            run_id=run.id,
            workflow_key=website_change.KEY,
            prefix="website-change",
            path=f"website/changes/{run.id}.md",
            content=failed_report(spec, plan, str(exc)),
            summary=f"Tin couldn't apply the {spec.noun}: {exc}"[:900],
            failed=True,
        )
        return True
    key = delivery.merge_key(run.id)
    sleep = sleep or asyncio.sleep
    clock = clock or (lambda: datetime.now(UTC))
    async with database.effect_lock(key, delivery.MERGE_OPERATION) as (conn, receipt):
        if receipt and receipt.status == "completed":
            merge = receipt.result
        else:
            await database.start_effect(conn, execution_key=key, operation=delivery.MERGE_OPERATION)
            base = {"pull_request": pull_request.url, "number": pull_request.number}
            hold = None
            if source["publish"]["mode"] != "direct":
                hold = source["publish"]["reason"]
            else:
                try:
                    current = await approval_for(
                        database.pool, project_id=run.project_id, change=change
                    )
                except WebsiteChangeConflict:
                    current = None
                setting = await project_protected_paths(database.pool, project_id=run.project_id)
                roots = protected_paths(source.get("protected_paths"), setting["paths"])
                hit = protected(plan["route"], roots) or next(
                    (r for item in plan["files"] if (r := file_protected(item["path"], roots))),
                    None,
                )
                if current != change.get("approval"):
                    hold = "Its approval changed after Tin opened it, so it waits for your review."
                elif hit:
                    hold = f"It touches {hit}, a protected page, so it waits for your review."
            try:
                if hold:
                    merge = {**base, "status": "left_open", "reason": hold}
                else:
                    merge = {
                        **base,
                        "merge_rule": "approved_changes",
                        **await delivery._merge_when_clean(
                            integrations=integrations,
                            run=run,
                            source=source,
                            manifest={
                                "title": title(spec, plan),
                                "files": [
                                    {"path": item["path"], "content": item["content"]}
                                    for item in plan["files"]
                                ],
                                "default_branch": binding.default_branch,
                            },
                            new_paths=tuple(
                                item["path"] for item in plan["files"] if item["action"] == "create"
                            ),
                            number=pull_request.number,
                            branch=pull_request.branch,
                            sleep=sleep,
                            clock=clock,
                            ready=delivery.REQUIRED_CHECKS_READY,
                            record_state=True,
                        ),
                    }
                event = (
                    "website_change_merged"
                    if merge["status"] == "merged"
                    else "website_change_left_open"
                )
                async with conn.transaction():
                    await database.complete_effect(conn, execution_key=key, result=merge)
                    await database.add_activity(
                        conn=conn,
                        run_id=run.id,
                        event_type=event,
                        audience="product",
                        summary=(
                            f"Tin merged PR #{pull_request.number} into {binding.default_branch}."
                            if merge["status"] == "merged"
                            else f"PR #{pull_request.number} is open. {merge['reason']}"
                        )[:240],
                        details={
                            "kind": "runs",
                            "external_url": pull_request.url,
                            "external_label": f"View PR #{pull_request.number}",
                            "change_ids": [change["change_id"]],
                        },
                        dedupe_key=f"{key}:{event}:{run.id}",
                    )
            except Exception:
                await database.fail_effect(
                    conn,
                    execution_key=key,
                    error_message="Tin could not confirm the merge. The pull request is unchanged.",
                )
                raise
    await publish_run_report(
        database=database,
        storage=storage,
        run_id=run.id,
        workflow_key=website_change.KEY,
        prefix="website-change",
        path=f"website/changes/{run.id}.md",
        content=report(spec, source, plan, pull_request=pull_request, merge=merge),
        summary=(
            f"{spec.title_prefix} merged in PR #{pull_request.number}."
            if merge["status"] == "merged"
            else f"{spec.title_prefix} PR #{pull_request.number} is open."
        ),
    )
    return True
