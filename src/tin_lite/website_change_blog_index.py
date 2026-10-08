"""website.change, phase 3: the blog index plan, applied as it is.

`content.blog_index` (#239) plans and opens no pull request. Its run writes
`reports/blog-index/{run_id}/PLAN.md` with one fenced JSON block between
`<!-- blog-index-patch.json:start -->` and `<!-- blog-index-patch.json:end -->`:

    {"schema": "blog-index-patch/1", "repository": "owner/repo", "base_ref": "<branch>",
     "base_sha": "<sha the plan read>", "route": "/blog", "summary": "…",
     "files": [{"path": "…", "action": "create|update", "content": "<full file text>"}],
     "caps": {"max_files": 5}}

A website.change run with `source: blog_index` reads the newest succeeded content.blog_index
run's plan server-side and records one `website_changes` row: change ID `bi_` plus the first
20 hex digits of the SHA-256 of the canonical JSON of `files`, kind `index`, the route as its
path, and the full SHA-256 as the content an approval covers. Nothing here needs judgment, so
no Codex session runs: Tin opens the pull request with exactly those files, and under the same
mode rules as the other sources merges it once the required checks pass when the row was
approved and touches no protected page. Otherwise the founder merges it.

A plan read from an older commit is applied only when none of its files changed upstream
since; otherwise the row stays and the start says which file moved. Never more than five
files, and never dependencies, lockfiles, CI, deploy settings or secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any
from uuid import UUID

from tin_lite import content_repository_delivery as delivery
from tin_lite import technical_batch as batch_rules
from tin_lite import website_change
from tin_lite import website_change_audit as shared
from tin_lite.organic_audit import canonical_json

SOURCE = delivery.BLOG_INDEX_SOURCE
PLAN_WORKFLOW = "content.blog_index"
SCHEMA = "blog-index-patch/1"
MAX_FILES = 5
MAX_BYTES = 400_000
BLOCK = re.compile(
    r"<!-- blog-index-patch\.json:start -->\s*```json\s*(\{.*\})\s*```\s*"
    r"<!-- blog-index-patch\.json:end -->",
    re.S,
)
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
FILE_PATH = re.compile(r"[A-Za-z0-9_.\-()\[\]+@ ][A-Za-z0-9_.\-()\[\]+@ /]{0,250}")
NONE_YET = (
    "No blog index plan yet: content.blog_index has not finished a run in this project, so "
    "there is nothing to apply."
)


def plan_path(run_id) -> str:
    return f"reports/blog-index/{UUID(str(run_id))}/PLAN.md"


def blocked(path: str) -> str | None:
    """Why a blog index may not write this file, or None."""
    if path.rsplit("/", 1)[-1] in delivery.DEPENDENCY_FILES:
        return "dependencies"
    return batch_rules.blocked(path)


def parse_plan(text: str) -> dict[str, Any]:
    """The plan's patch, validated against the contract; raises ValueError naming the fault."""
    found = BLOCK.search(text)
    if not found:
        raise ValueError("The blog index plan has no blog-index-patch.json block.")
    try:
        plan = json.loads(found.group(1))
    except ValueError as exc:
        raise ValueError("The blog index plan's JSON block does not parse.") from exc
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError(f"The blog index plan is not {SCHEMA}.")
    if not isinstance(plan.get("repository"), str) or not REPOSITORY.fullmatch(plan["repository"]):
        raise ValueError("The blog index plan names no owner/repository.")
    if not isinstance(plan.get("base_ref"), str) or not plan["base_ref"].strip():
        raise ValueError("The blog index plan names no base branch.")
    if not isinstance(plan.get("base_sha"), str) or not SHA.fullmatch(plan["base_sha"]):
        raise ValueError("The blog index plan names no commit it read.")
    route = website_change.site_path(plan.get("route"))
    if route is None or "{" in route:
        raise ValueError("The blog index plan's route is not a site path such as /blog.")
    caps = plan.get("caps") if isinstance(plan.get("caps"), dict) else {}
    limit = min(MAX_FILES, int(caps.get("max_files") or MAX_FILES))
    files = plan.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= limit:
        raise ValueError(f"A blog index plan changes one to {limit} files.")
    seen, total = set(), 0
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "action", "content"}
            or item["action"] not in {"create", "update"}
            or not isinstance(item["path"], str)
            or not isinstance(item["content"], str)
        ):
            raise ValueError("Each blog index file is a path, a create or update action, and text.")
        path = item["path"]
        if not FILE_PATH.fullmatch(path) or ".." in path.split("/") or path in seen:
            raise ValueError(f"{path!r} is not a repository file path.")
        reason = blocked(path)
        if reason:
            raise ValueError(f"A blog index may not change {path} ({reason}).")
        if "\x00" in item["content"]:
            raise ValueError("A blog index changes text files only.")
        seen.add(path)
        total += len(item["content"].encode())
    if total > MAX_BYTES:
        raise ValueError(f"A blog index plan stays under {MAX_BYTES // 1000} KB.")
    return {
        "schema": SCHEMA,
        "repository": plan["repository"],
        "base_ref": plan["base_ref"],
        "base_sha": plan["base_sha"],
        "route": route,
        "summary": " ".join(str(plan.get("summary") or "").split())[:500],
        "files": [{key: item[key] for key in ("path", "action", "content")} for item in files],
    }


def files_sha256(files: list[dict]) -> str:
    return hashlib.sha256(canonical_json(files)).hexdigest()


def change_id(files: list[dict]) -> str:
    """`bi_` and the first 20 hex digits of the SHA-256 of the canonical JSON of `files`."""
    return f"bi_{files_sha256(files)[:20]}"


def change_row(plan: dict, run_id) -> website_change.ChangeRow:
    return website_change.ChangeRow(
        change_id=change_id(plan["files"]),
        source=SOURCE,
        kind="index",
        title=f"Blog index: {plan['summary'] or plan['route']}",
        paths=(plan["route"],),
        content_sha256=files_sha256(plan["files"]),
        detail={
            "plan_run_id": str(run_id),
            "repository": plan["repository"],
            "base_ref": plan["base_ref"],
            "base_sha": plan["base_sha"],
            "route": plan["route"],
            "summary": plan["summary"],
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


async def latest_plan_run(database, project_id):
    return await database.pool.fetchrow(
        "SELECT r.id, r.canonical_commit_sha FROM workflow_runs r "
        "JOIN workflows w ON w.id = r.workflow_id "
        "WHERE r.project_id=$1 AND w.key=$2 AND r.status='succeeded' "
        "AND r.canonical_commit_sha IS NOT NULL "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1",
        project_id,
        PLAN_WORKFLOW,
    )


async def read_plan(database, storage, project_id, run_id, revision) -> dict:
    project = await database.get_project(project_id)
    raw = await storage.read_canonical_artifact_if_exists(
        repo_id=project.state_repo_id, commit_sha=revision, path=plan_path(run_id)
    )
    if raw is None:
        raise ValueError(f"The blog index run {run_id} saved no PLAN.md.")
    if len(raw) > MAX_BYTES + 100_000:
        raise ValueError("The blog index plan is too large.")
    return parse_plan(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)


async def stale_reason(integrations, project_id, plan, binding) -> str | None:
    """Why the plan can't apply to the branch as it is now, or None."""
    if plan["base_ref"] != binding.default_branch:
        return (
            f"The plan was made for {plan['base_ref']}, but the site deploys from "
            f"{binding.default_branch}. Run content.blog_index again."
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
            "leaves the plan as it is. Run content.blog_index again."
        )
    if moved:
        return (
            f"{', '.join(moved)} changed on {plan['base_ref']} since the plan read it, so "
            "Tin leaves the plan as it is. Run content.blog_index again."
        )
    return None


async def plan_changes(
    *, database, storage, integrations, project_id: UUID, inputs: dict, bind: bool = True
) -> dict[str, Any]:
    """Record the newest blog index plan as one row and say whether the next run applies it."""
    from tin_lite.website_change import (
        WebsiteChangeConflict,
        approval_for,
        file_protected,
        project_protected_paths,
        protected,
        protected_paths,
    )

    empty = {
        "change_source": SOURCE,
        "plan_run_id": None,
        "changes": [],
        "decisions_needed": [],
        "ask": None,
        "next_run": {"mode": None, "reason": None, "change_ids": []},
        "execution_available": False,
    }
    run = await latest_plan_run(database, project_id)
    if run is None:
        return {**empty, "note": NONE_YET}
    plan = await read_plan(database, storage, project_id, run["id"], run["canonical_commit_sha"])
    row = change_row(plan, run["id"])
    if inputs.get("expected_repository") and inputs["expected_repository"] != plan["repository"]:
        raise ValueError(
            f"The blog index plan is for {plan['repository']}, not {inputs['expected_repository']}."
        )
    flight = await shared.rows_in_flight(
        database, integrations, project_id=project_id, source=SOURCE
    )
    binding = None
    if bind and row.change_id not in flight:
        binding = await integrations.github_repository_binding(
            project_id=project_id, expected_repository=plan["repository"]
        )
    [stored] = await website_change.propose(database, project_id=project_id, rows=[row])
    await website_change.retire(
        database, project_id=project_id, source=SOURCE, keep=[row.change_id, *flight]
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
        reason = "You declined this blog index plan in Tin; Tin won't apply it."
    elif row.change_id in flight:
        carried = flight[row.change_id]
        reason = (
            f"This blog index plan already sits in PR #{carried['number']} ({carried['state']}): "
            f"{carried['url']}."
        )
    elif binding is not None:
        reason = await stale_reason(integrations, project_id, plan, binding)
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


async def select_source(*, database, storage, integrations, project_id, inputs) -> dict:
    """Pin the plan run, its row with its approval, the repository and the publish mode. The
    files are re-read from the pinned plan when the run applies them."""
    preview = await plan_changes(
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
    )
    if not preview["next_run"]["change_ids"]:
        raise ValueError(preview["next_run"]["reason"] or preview.get("note") or NONE_YET)
    [change] = preview["changes"]
    return {
        "source": SOURCE,
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


async def guard_source(conn, *, project_id, inputs, source) -> None:
    """Under create_run's project lock: the pinned approval still stands, and no other blog
    index run of this project is still working."""
    from tin_lite.website_change import WebsiteChangeConflict, approval_for

    if inputs.get("source") != SOURCE or source.get("source") != SOURCE:
        raise ValueError("The blog index plan is not the source this run was admitted for.")
    [change] = source["changes"]
    try:
        current = await approval_for(conn, project_id=project_id, change=change)
    except WebsiteChangeConflict:
        current = None
    if current != change.get("approval"):
        raise ValueError("The blog index plan's approval changed while starting. Start again.")
    busy = await conn.fetchval(
        "SELECT id FROM workflow_runs WHERE project_id=$1 AND workflow_id=$2 "
        "AND input->>'source'=$3 AND status = ANY($4::text[]) ORDER BY created_at DESC LIMIT 1",
        project_id,
        website_change.WORKFLOW_ID,
        SOURCE,
        list(shared.ACTIVE),
    )
    if busy:
        raise ValueError(
            f"Website changes run {busy} is still applying the blog index; wait for it to finish."
        )


def report(source: dict, plan: dict, *, pull_request=None, merge=None) -> bytes:
    lines = ["# Blog index", "", "## Result", ""]
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
        f"- `{change['change_id']}` (index): "
        + (
            f"approved by {approval['by']} on {approval['at'][:10]}."
            if approval
            else "waiting for your approval."
        ),
        "",
        "## The plan",
        "",
        f"From content.blog_index run `{source['plan_run_id']}`, read at `{plan['base_sha']}` "
        f"of {plan['base_ref']}, for {plan['route']}.",
        "",
        *[f"- {item['action']} `{item['path']}`" for item in plan["files"]],
        "",
        "Tin applied the plan's files as they are and wrote no copy of its own.",
        "",
    ]
    return "\n".join(lines).encode()


def failed_report(source: dict, plan: dict, reason: str) -> bytes:
    """The report of a run that couldn't apply the plan to the repository as it is now."""
    return "\n".join(
        [
            "# Blog index",
            "",
            "## Result",
            "",
            f"Tin couldn't apply the blog index plan: {reason.rstrip('.')}. No pull request "
            "was opened, and the change stays waiting in Tin. Run content.blog_index again.",
            "",
            *[f"- {item['action']} `{item['path']}`" for item in plan["files"]],
            "",
        ]
    ).encode()


def pull_request_body(source: dict, plan: dict) -> str:
    lines = [
        plan["summary"] or f"A blog index for {plan['route']}.",
        "",
        "Files, as content.blog_index planned them:",
        "",
        *[f"- {item['action']} `{item['path']}`" for item in plan["files"]],
        "",
        f"Change `{source['changes'][0]['change_id']}` from Tin's website.change.",
        "",
        "Tin merges this pull request once the site's build and the repository's required checks "
        "pass, because you approved it in Tin."
        if source["publish"]["mode"] == "direct"
        else "Review and merge it when it looks right; Tin doesn't merge it.",
    ]
    return "\n".join(lines)


async def apply(*, database, storage, integrations, run, sleep=None, clock=None) -> bool:
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
        database, storage, run.project_id, source["plan_run_id"], source["plan_revision"]
    )
    [change] = source["changes"]
    if files_sha256(plan["files"]) != change["content_sha256"]:
        raise ValueError("The blog index plan changed after this run started.")
    binding = delivery.binding_from(source)
    try:
        pull_request = await integrations.github_create_pull_request(
            project_id=run.project_id,
            execution_key=f"{run.id}:procedure_pull_request",
            title=f"Blog index: {plan['summary'] or plan['route']}"[:200],
            body=pull_request_body(source, plan),
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
            content=failed_report(source, plan, str(exc)),
            summary=f"Tin couldn't apply the blog index plan: {exc}"[:900],
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
                                "title": f"Blog index: {plan['summary'] or plan['route']}",
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
        content=report(source, plan, pull_request=pull_request, merge=merge),
        summary=(
            f"Blog index merged in PR #{pull_request.number}."
            if merge["status"] == "merged"
            else f"Blog index PR #{pull_request.number} is open."
        ),
    )
    return True
