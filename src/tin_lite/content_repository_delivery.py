"""Repository adaptation of an approved page, using the ordinary procedure runner.

The page is immutable input, not an invitation to draft again. Admission, usage,
execution isolation, checkpoints and GitHub effects remain the existing contracts.
Sources are approved planned articles (content.generate), answer pages and public
articles. An approval can start this procedure itself (see ContentDelivery.adapt); when
the founder's delivery setting commits to main, Tin then merges the pull request, but
only one that adds nothing except the approved page, once GitHub reports it clean.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from tin_lite import approved_article, approved_document, content_draft
from tin_lite.content_delivery import (
    ADAPTED_PATH,
    ADAPTER,
    ContentDelivery,
    adaptation_start_key,
    adapted,
    chosen_mode,
)
from tin_lite.domain import RunStatus
from tin_lite.integrations import GitHubFileChange, GitHubRepositoryBinding

KEY = "content.deliver"
WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000036")
OPERATION = "content_repository_delivery_source_v1"
CHECK_COMMAND = "git diff --check"
RECOVERY_OPERATION = "content_repository_delivery_recovery_v1"
MERGE_OPERATION = "content_repository_delivery_merge_v1"
# How long Tin waits for GitHub to call a pull request clean before leaving it open. The
# delivery activity allows five minutes; this leaves room for the merge call itself.
MERGE_WAIT_SECONDS = 210
MERGE_POLL_SECONDS = 15
# GitHub's mergeable_state values that no amount of waiting fixes, in the founder's words.
MERGE_STOPS = {
    "dirty": "It conflicts with the default branch.",
    "behind": "Your repository requires it to be up to date with the default branch first.",
    "draft": "It is a draft pull request.",
}


def recovery_key(run_id):
    return f"content-delivery:{UUID(str(run_id))}:recovery"


def merge_key(run_id):
    return f"content-delivery:{UUID(str(run_id))}:merge"


def status_projection(run, source, publication, recovery, merge=None):
    pr = (
        recovery
        if recovery.get("url")
        else (
            {
                "url": publication["external_url"],
                "number": publication.get("pull_request_number"),
                "repository": source["binding"]["repository"],
            }
            if publication.get("external_url")
            else None
        )
    )
    merge = merge or None
    return {
        "status": "completed"
        if pr
        else "failed"
        if run.status == RunStatus.FAILED
        else "started"
        if run.status == RunStatus.RUNNING
        else "pending",
        "repository": source["binding"]["repository"],
        "path": "Repository-adapted article",
        "source_run_id": source["source_run_id"],
        "run_id": str(run.id),
        "error": run.error_message if not pr else None,
        "pull_request": pr,
        "approval_label": None,
        # The address the adaptation says the page will have once merged; a claim, not proof.
        "public_route": recovery.get("public_route") or publication.get("public_route"),
        # Set only when the founder's setting commits to main: Tin merged it, or left it open.
        "merge": merge,
        "merged": bool(merge and merge.get("status") == "merged"),
        "merged_at": merge.get("merged_at") if merge else None,
    }


def source_key(run_id):
    return f"content-delivery:{UUID(str(run_id))}:source"


def binding_from(source):
    return GitHubRepositoryBinding(
        **{**source["binding"], "connection_id": UUID(source["binding"]["connection_id"])}
    )


async def adaptation_facts(database, run_ids):
    """Saved receipts of content.deliver runs, keyed by run; one Postgres read."""
    if not run_ids:
        return {}
    from tin_lite.content_programs import decoded

    keys = {
        key: (run_id, part)
        for run_id in run_ids
        for key, part in (
            (source_key(run_id), "source"),
            (f"{run_id}:procedure_canonical_commit", "publication"),
            (recovery_key(run_id), "recovery"),
            (merge_key(run_id), "merge"),
        )
    }
    rows = await database.pool.fetch(
        "SELECT execution_key, result FROM effect_receipts "
        "WHERE execution_key = ANY($1::text[]) AND status = 'completed'",
        list(keys),
    )
    facts = {}
    for row in rows:
        run_id, part = keys[row["execution_key"]]
        facts.setdefault(run_id, {})[part] = decoded(row["result"] or {})
    return {run_id: value for run_id, value in facts.items() if value.get("source")}


def child_projection(run, facts):
    facts = facts or {}
    if not facts.get("source"):
        raise ValueError("The delivery's approved source binding is unavailable.")
    return status_projection(
        run,
        facts["source"],
        facts.get("publication") or {},
        facts.get("recovery") or {},
        facts.get("merge"),
    )


async def adapted_status(database, run, intent, start):
    """The approved page's own delivery view: its adaptation's pull request, projected back.

    `start` is the approval's start receipt ({status, result, error_message}) or None.
    """
    view = {
        "adapter": ADAPTER,
        "mode": chosen_mode(intent),
        "repository": intent["settings"]["repository"],
        "path": ADAPTED_PATH,
        "status": "pending" if run.review_decision == "approved" else "awaiting_review",
        "error": None,
        "run_id": None,
        "pull_request": None,
        "commit": None,
        "public_route": None,
        "merge": None,
        "merged": False,
        "merged_at": None,
        "approval_label": "Publish",
    }
    start = start or {}
    if start.get("status") == "failed":
        return {**view, "status": "failed", "error": start.get("error_message")}
    child_id = (
        (start.get("result") or {}).get("run_id") if start.get("status") == "completed" else None
    )
    if not child_id:
        return view
    child = await database.get_run(UUID(child_id))
    facts = (await adaptation_facts(database, [child.id])).get(child.id) if child else None
    if child is None or not facts:
        return {**view, "status": "started", "run_id": child_id}
    projected = child_projection(child, facts)
    return {
        **view,
        **{
            key: projected[key]
            for key in (
                "status",
                "error",
                "run_id",
                "pull_request",
                "public_route",
                "merge",
                "merged",
                "merged_at",
            )
        },
    }


async def discover(database, project_id):
    articles = await approved_article.discover(database, project_id)
    rows = await database.pool.fetch(
        "SELECT id, COALESCE(artifact_title, 'Approved page') AS title FROM workflow_runs "
        "WHERE project_id=$1 AND workflow_id = ANY($2::uuid[]) "
        "AND status='succeeded' AND review_decision='approved' "
        "ORDER BY created_at DESC, id DESC LIMIT 100",
        project_id,
        list(approved_document.KINDS),
    )
    connection = await database.get_integration_connection(
        project_id=project_id, provider_key="infra.github"
    )
    return {
        "articles": articles + [{"run_id": str(row["id"]), "title": row["title"]} for row in rows],
        "repository": connection.configuration.get("selected_repository")
        if connection and connection.status == "connected"
        else None,
    }


async def select_source(*, database, storage, integrations, project_id, inputs, approval=False):
    """Pin the approved page and repository; `approval` marks a start by the page's approval.

    Only that start carries the founder's delivery (a pull request, or commit to main) into
    the run. Prepare PR and agent starts always leave their pull request open.
    """
    candidate = await database.get_run(UUID(str(inputs["source_run_id"])))
    if approved_document.kind_for(candidate):
        source = await approved_document.select(
            database=database,
            storage=storage,
            project_id=project_id,
            source_run_id=inputs["source_run_id"],
        )
    else:
        source = {
            "source_kind": "article",
            **await approved_article.select(
                database=database,
                storage=storage,
                project_id=project_id,
                source_run_id=inputs["source_run_id"],
            ),
        }
    run = await database.get_run(UUID(source["source_run_id"]))
    selected = await database.get_effect(content_draft.selection_key(run.id))
    # The approval-time choice, else the pinned intent: the one the exact publisher uses.
    # A choice to adapt the page is this workflow's own and does not block it.
    intent = await ContentDelivery(database=database).intent(run)
    if intent and not adapted(intent):
        raise ValueError(
            "This article already has automatic delivery. Use its existing delivery action."
        )
    binding = await integrations.github_repository_binding(
        project_id=project_id, expected_repository=inputs["expected_repository"]
    )
    system_delivery = (selected.result or {}).get("system_delivery") if selected else None
    if system_delivery and system_delivery["mode"] == "github_pr":
        from tin_lite.organic_content import check_destination

        check_destination(system_delivery, binding)
    pinned = {
        **source,
        "binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
    }
    if approval:
        if not adapted(intent):
            raise ValueError("This page's approval did not ask Tin to publish it.")
        if str(intent["repository_id"]) != str(binding.repository_id) or str(
            intent["connection_id"]
        ) != str(binding.connection_id):
            raise ValueError("The GitHub connection changed after this page was approved.")
        pinned["approval"] = {
            "mode": chosen_mode(intent),
            "requested_by": intent.get("chosen_by"),
            **({"route": intent["route"]} if intent.get("route") else {}),
        }
    return pinned


async def guard_source(conn, *, project_id, inputs, source):
    """Called under create_run's project lock, in the run/budget/receipt transaction."""
    if inputs["source_run_id"] != source["source_run_id"]:
        raise ValueError("The selected article is not approved for delivery.")
    if source.get("source_kind") in {"answer_page", "public_article"}:
        await approved_document.guard(conn, project_id=project_id, source=source)
    else:
        await approved_article.guard(conn, project_id=project_id, source=source)
    duplicate = await conn.fetchval(
        "SELECT r.id FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "AND s.operation=$4 AND s.status='completed' "
        "WHERE r.project_id=$1 AND r.workflow_id=$2 "
        "AND s.result->>'source_run_id'=$3 "
        "AND s.result->'binding'->>'repository_id'=$5 "
        "ORDER BY r.created_at DESC LIMIT 1",
        project_id,
        WORKFLOW_ID,
        source["source_run_id"],
        OPERATION,
        str(source["binding"]["repository_id"]),
    )
    if duplicate and inputs.get("retry_run_id") == str(duplicate):
        prior = await conn.fetchrow("SELECT status FROM workflow_runs WHERE id=$1", duplicate)
        external = await conn.fetchrow(
            "SELECT status FROM integration_call_receipts WHERE execution_key=$1",
            f"{duplicate}:procedure_pull_request",
        )
        checkpoint = await conn.fetchval(
            "SELECT true FROM effect_receipts WHERE execution_key=$1 AND status='completed'",
            f"{duplicate}:procedure_artifact_persist",
        )
        if prior["status"] != "failed" or external or checkpoint:
            raise ValueError("Reconcile the previous delivery before purchasing a new adaptation.")
        return
    if inputs.get("retry_run_id"):
        raise ValueError("Choose the latest failed adaptation for this article and repository.")
    if duplicate:
        raise ValueError(
            f"This article already has a delivery attempt. Open run {duplicate}; "
            "retry its saved delivery rather than drafting or paying again."
        )


async def saved_source(database, run_id):
    receipt = await database.get_effect(source_key(run_id))
    if not receipt or receipt.status != "completed" or not receipt.result:
        raise ValueError("The delivery's approved source binding is unavailable.")
    return receipt.result


async def retry_status(database, run):
    source = await saved_source(database, run.id)
    recovery = await database.get_effect(recovery_key(run.id))
    publication = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    if (
        publication
        and publication.status == "completed"
        and (publication.result or {}).get("external_url")
    ):
        status = status_projection(run, source, publication.result, {})
        return status
    checkpoint = await database.get_effect(f"{run.id}:procedure_artifact_persist")
    if (
        not checkpoint
        or checkpoint.status != "completed"
        or not (checkpoint.result or {}).get("ephemeral_commit_sha")
    ):
        raise ValueError(
            "This attempt has no saved repository patch to deliver. "
            "No model call will be purchased by Retry delivery."
        )
    if run.status not in {RunStatus.FAILED, RunStatus.SUCCEEDED}:
        raise ValueError("Wait for this delivery attempt to finish before retrying delivery.")
    return {
        "status": recovery.status if recovery else "pending",
        "repository": source["binding"]["repository"],
        "run_id": str(run.id),
        "pull_request": recovery.result if recovery else None,
    }


async def recover_delivery(*, database, storage, integrations, run):
    """Member-requested retry of a frozen patch, without a new model run or approval."""
    await retry_status(database, run)
    key = recovery_key(run.id)
    async with database.effect_lock(key, RECOVERY_OPERATION) as (conn, existing):
        if existing and existing.status == "completed":
            return existing.result
        await database.start_effect(conn, execution_key=key, operation=RECOVERY_OPERATION)
        try:
            from tin_lite.integrations import GitHubFileChange
            from tin_lite.procedures import (
                load_pinned_codex_procedure,
                validate_procedure_pull_request,
            )

            workflow = await database.get_workflow(run.workflow_id)
            spec = await load_pinned_codex_procedure(
                storage=storage,
                repo_id=workflow.definition_repo_id,
                commit_sha=run.definition_commit_sha,
                definition_path=workflow.definition_path,
            )
            project = await database.get_project(run.project_id)
            persisted = await database.get_effect(f"{run.id}:procedure_artifact_persist")
            from tin_lite.procedures import procedure_checkpoint_path

            checkpoint = await storage.read_procedure_checkpoint(
                repo_id=project.state_repo_id,
                revision=persisted.result["ephemeral_commit_sha"],
                path=procedure_checkpoint_path(run.id),
            )
            source = await saved_source(database, run.id)
            approved = await database.get_run(UUID(source["source_run_id"]))
            if (
                not approved
                or approved.project_id != run.project_id
                or approved.status != RunStatus.SUCCEEDED
                or approved.review_decision != "approved"
                or approved.canonical_commit_sha != source["source_revision"]
            ):
                raise ValueError("The saved delivery no longer has an approved source.")
            manifest = validate_procedure_pull_request(checkpoint, spec=spec)
            proof = validate_copy(manifest, source)
            binding = binding_from(source)
            result = await integrations.github_create_pull_request(
                project_id=run.project_id,
                run_id=run.id,
                execution_key=f"{run.id}:procedure_pull_request",
                title=manifest["title"],
                body=manifest["body"],
                files=tuple(GitHubFileChange(**item) for item in manifest["files"]),
                base_branch=binding.default_branch,
                expected_base_sha=binding.head_sha,
                expected_binding=binding,
                allow_unrelated_base_advance=True,
            )
            result = {**asdict(result), **proof}
            async with conn.transaction():
                await database.complete_effect(conn, execution_key=key, result=result)
                await database.add_activity(
                    conn=conn,
                    run_id=run.id,
                    event_type="content_delivery_recovered",
                    audience="product",
                    summary=f"Article delivery confirmed as PR #{result['number']}.",
                    details={
                        "kind": "runs",
                        "external_url": result["url"],
                        "external_label": f"Review PR #{result['number']}",
                        "source_run_id": source["source_run_id"],
                    },
                    dedupe_key=f"{key}:ready",
                )
            return result
        except Exception:
            await database.fail_effect(
                conn,
                execution_key=key,
                error_message="Delivery could not be confirmed. "
                "The saved patch and article are unchanged.",
            )
            raise


async def start_approved_adaptation(*, runtime, settings, run, intent):
    """Admit and dispatch the adaptation an approval asked for, as the approver.

    The ordinary run service does the rest: source pinning, one metered procedure session
    charged on its actual usage, and an idempotent Temporal start under one start key.
    """
    from tin_lite.page_routes import direction
    from tin_lite.run_service import start_workflow_run

    workflow = await runtime.database.get_workflow(WORKFLOW_ID)
    if workflow is None:
        raise LookupError("Page adaptation is not installed on this Tin.")
    route = intent.get("route")
    return await start_workflow_run(
        runtime=runtime,
        settings=settings,
        workflow=workflow,
        project_id=run.project_id,
        started_by_clerk_user_id=intent.get("chosen_by") or run.started_by_clerk_user_id,
        start_idempotency_key=adaptation_start_key(run.id),
        input_payload={
            "source_run_id": str(run.id),
            "expected_repository": intent["settings"]["repository"],
            "direction": direction(route) if route else "",
        },
        trigger_source=intent.get("trigger_source") or "manual",
        _approval_delivery=True,
    )


async def saved_manifest(database, storage, run):
    """The run's frozen pull-request patch, revalidated against its pinned contract."""
    from tin_lite.procedures import (
        load_pinned_codex_procedure,
        procedure_checkpoint_path,
        validate_procedure_pull_request,
    )

    persisted = await database.get_effect(f"{run.id}:procedure_artifact_persist")
    revision = (persisted.result or {}).get("ephemeral_commit_sha") if persisted else None
    if not persisted or persisted.status != "completed" or not revision:
        raise ValueError("This adaptation has no saved repository patch.")
    workflow = await database.get_workflow(run.workflow_id)
    spec = await load_pinned_codex_procedure(
        storage=storage,
        repo_id=workflow.definition_repo_id,
        commit_sha=run.definition_commit_sha,
        definition_path=workflow.definition_path,
    )
    project = await database.get_project(run.project_id)
    checkpoint = await storage.read_procedure_checkpoint(
        repo_id=project.state_repo_id,
        revision=revision,
        path=procedure_checkpoint_path(run.id),
    )
    return validate_procedure_pull_request(checkpoint, spec=spec)


def merge_rule(manifest, proof, route):
    """Which rule lets Tin merge this patch under a commit-to-main setting, else None.

    `page_only`: the approved page alone, the change the Markdown publisher commits today.
    `chosen_route`: the page plus the site code that serves it, when the founder chose where
    these pages live and the PR puts the page at that route. The copy proof, the five-file
    limit and the dependency ban still hold; any other site change stays a PR.
    """
    from tin_lite.page_routes import matches

    if page_only(manifest, proof):
        return "page_only"
    if route and matches(route, proof.get("public_route")):
        return "chosen_route"
    return None


def page_only(manifest, proof):
    """True when the patch adds nothing but the approved page as one Markdown file.

    That is the same change the Markdown publisher commits to main today.
    """
    files = manifest.get("files") or []
    return (
        len(files) == 1
        and files[0]["path"] == proof["article_path"]
        and proof["article_path"].endswith(".md")
    )


async def publish_after_pull_request(
    *, database, storage, integrations, run, sleep=None, clock=None
):
    """Honor a commit-to-main setting after the adaptation's PR opens, or leave it open.

    Only a run started by the page's approval, with the founder's setting to commit to
    main, is merged: when its patch is the approved page alone, its branch still holds
    exactly that patch, and GitHub calls it clean (no conflicts, no failing or pending
    checks, no required review) within a few minutes. Otherwise the PR stays open and
    the receipt says why. The outcome is recorded once; retries reuse it.
    """
    source = await saved_source(database, run.id)
    if (source.get("approval") or {}).get("mode") != "github_commit":
        return None
    publication = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    published = (publication.result or {}) if publication else {}
    if (
        not publication
        or publication.status != "completed"
        or not published.get("external_url")
        or type(published.get("pull_request_number")) is not int
    ):
        return None
    key = merge_key(run.id)
    sleep = sleep or asyncio.sleep
    clock = clock or (lambda: datetime.now(UTC))
    async with database.effect_lock(key, MERGE_OPERATION) as (conn, receipt):
        if receipt and receipt.status == "completed":
            return receipt.result
        await database.start_effect(conn, execution_key=key, operation=MERGE_OPERATION)
        try:
            manifest = await saved_manifest(database, storage, run)
            proof = validate_copy(manifest, source)
            number = published["pull_request_number"]
            base = {"pull_request": published["external_url"], "number": number}
            route = (source.get("approval") or {}).get("route")
            rule = merge_rule(manifest, proof, route)
            if rule is None:
                result = {
                    **base,
                    "status": "left_open",
                    "reason": (
                        f"It changes site files besides the page and does not put the page at "
                        f"your chosen route {route}, so it waits for your review."
                        if route
                        else "It changes site files besides the page, so it waits for your review."
                    ),
                }
            else:
                result = {
                    **base,
                    "merge_rule": rule,
                    **await _merge_when_clean(
                        integrations=integrations,
                        run=run,
                        source=source,
                        manifest=manifest,
                        number=number,
                        branch=published.get("pull_request_branch"),
                        sleep=sleep,
                        clock=clock,
                    ),
                }
            if result["status"] == "merged":
                event, summary = (
                    "content_delivery_merged",
                    f"Tin merged PR #{number} into {manifest['default_branch']}."
                    if result.get("merged_by") == "tin"
                    else f"PR #{number} was merged into {manifest['default_branch']}.",
                )
            else:
                event, summary = (
                    "content_delivery_left_open",
                    f"PR #{number} is open. {result['reason']}",
                )
            async with conn.transaction():
                await database.complete_effect(conn, execution_key=key, result=result)
                for target in (run.id, UUID(source["source_run_id"])):
                    await database.add_activity(
                        conn=conn,
                        run_id=target,
                        event_type=event,
                        audience="product",
                        summary=summary,
                        details={
                            "kind": "runs",
                            "external_url": result.get("url") or published["external_url"],
                            "external_label": f"View PR #{number}",
                            "source_run_id": source["source_run_id"],
                        },
                        dedupe_key=f"{key}:{event}:{target}",
                    )
            return result
        except Exception:
            await database.fail_effect(
                conn,
                execution_key=key,
                error_message="Tin could not confirm the merge. The pull request is unchanged.",
            )
            raise


async def _merge_when_clean(*, integrations, run, source, manifest, number, branch, sleep, clock):
    binding = binding_from(source)
    deadline = clock().timestamp() + MERGE_WAIT_SECONDS
    reason = "Its checks had not all passed after a few minutes, so Tin left it open."
    while True:
        state = await integrations.github_pull_request_merge_state(
            project_id=run.project_id, repository=binding.repository, number=number
        )
        if state["merged"]:
            return {
                "status": "merged",
                "commit": state.get("merge_commit_sha"),
                "url": state.get("url"),
                "merged_at": state.get("merged_at") or clock().isoformat(),
                "merged_by": "github",
            }
        if state["state"] != "open":
            return {"status": "left_open", "reason": "It was closed on GitHub."}
        if state.get("base_ref") != binding.default_branch:
            return {
                "status": "left_open",
                "reason": "Its destination branch changed after Tin opened it.",
            }
        if not state.get("same_repository") or (branch and state.get("head_ref") != branch):
            return {"status": "left_open", "reason": "Its branch changed after Tin opened it."}
        stop = MERGE_STOPS.get(state.get("mergeable_state"))
        if stop:
            return {"status": "left_open", "reason": stop}
        if state.get("mergeable") is True and state.get("mergeable_state") in {
            "clean",
            "has_hooks",
        }:
            from tin_lite.integrations import IntegrationAuthorizationError

            try:
                merged = await integrations.github_merge_pull_request(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=f"{run.id}:procedure_pull_request_merge",
                    number=number,
                    expected_head_sha=state["head_sha"],
                    branch=state["head_ref"],
                    files=tuple(GitHubFileChange(**item) for item in manifest["files"]),
                    expected_binding=binding,
                    commit_title=f"{manifest['title']} (#{number})"[:200],
                )
            except IntegrationAuthorizationError as exc:
                # A changed branch or connection is final; the PR stays for the founder.
                return {
                    "status": "left_open",
                    "reason": f"{str(exc).rstrip('.')}, so Tin left it open.",
                }
            if merged.get("merged"):
                return {
                    "status": "merged",
                    "commit": merged["commit"],
                    "url": merged.get("url"),
                    "merged_at": clock().isoformat(),
                    "merged_by": "tin",
                }
            return {"status": "left_open", "reason": merged.get("reason") or reason}
        if state.get("mergeable_state") == "blocked":
            reason = (
                "GitHub needs a review or a required check before it can merge, "
                "so Tin left it open."
            )
        if clock().timestamp() >= deadline:
            return {"status": "left_open", "reason": reason}
        await sleep(MERGE_POLL_SECONDS)


def validate_copy(manifest, source):
    """Prove lossless article storage, not that a site's renderer displays every byte.

    A Markdown site stores the reviewed body directly. Component-based sites retain
    it as a JSON string literal consumed by their existing Markdown renderer. The
    adapter may not introduce a renderer dependency or rewrite the prose as JSX.
    Actual rendering/build checks are separate and must never be implied by this proof.
    """
    article = source["article"]
    if hashlib.sha256(article.encode()).hexdigest() != source["article_sha256"]:
        raise ValueError("The approved article proof is inconsistent.")
    if (
        manifest["repository"] != source["binding"]["repository"]
        or manifest["head_sha"] != source["binding"]["head_sha"]
    ):
        raise ValueError("The repository differs from the pinned delivery source.")
    matches = []
    for item in manifest["files"]:
        path, text = item["path"], item["content"]
        if path.rsplit("/", 1)[-1] in {
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
        }:
            raise ValueError("Article delivery cannot change dependencies.")
        if path.endswith((".md", ".mdx")):
            body = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.S)
            if body.lstrip("\n") == article:
                matches.append(path)
        elif path.endswith((".tsx", ".jsx", ".js", ".ts", ".astro", ".vue", ".svelte", ".json")):
            # Decode complete JSON string tokens, not fragment matches in escaped
            # prose. This proves source storage, not runtime consumption/rendering.
            for token in re.findall(
                r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"', text
            ):
                if json.loads(token) == article:
                    matches.append(path)
                    break
    if len(matches) != 1:
        raise ValueError(
            "Keep the approved article unchanged in exactly one Markdown file "
            "or JSON string consumed by the site's Markdown renderer."
        )
    from tin_lite.page_urls import public_route

    route = public_route(manifest.get("body"))
    return {
        "article_path": matches[0],
        "article_sha256": source["article_sha256"],
        "copy_check": "exact_source_preserved",
        "build_check": "not_verified_by_tin",
        **({"public_route": route} if route else {}),
    }


async def delivery_history(executor, *, project_id, source_ids):
    if not source_ids:
        return {}
    rows = await executor.fetch(
        "SELECT DISTINCT ON (s.result->>'source_run_id') r.id, r.status, r.error_message, "
        "r.progress_summary, s.result->>'source_run_id' AS source_id, "
        "s.result->'binding'->>'repository' AS repository, p.result AS publication, "
        "recovery.result AS recovery, checkpoint.result AS checkpoint "
        "FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "AND s.operation=$3 AND s.status='completed' "
        "LEFT JOIN effect_receipts p "
        "ON p.execution_key=r.id::text || ':procedure_canonical_commit' "
        "AND p.status='completed' "
        "LEFT JOIN effect_receipts recovery "
        "ON recovery.execution_key='content-delivery:' || r.id::text || ':recovery' "
        "AND recovery.status='completed' "
        "LEFT JOIN effect_receipts checkpoint "
        "ON checkpoint.execution_key=r.id::text || ':procedure_artifact_persist' "
        "AND checkpoint.status='completed' WHERE r.project_id=$1 AND r.workflow_id=$2 "
        "AND s.result->>'source_run_id'=ANY($4::text[]) "
        "ORDER BY s.result->>'source_run_id', r.created_at DESC, r.id DESC",
        project_id,
        WORKFLOW_ID,
        OPERATION,
        list(source_ids),
    )
    from tin_lite.content_programs import decoded

    return {
        row["source_id"]: {
            "run_id": str(row["id"]),
            "status": row["status"],
            "repository": row["repository"],
            "summary": row["progress_summary"],
            "error": row["error_message"],
            "pull_request_url": decoded(row["publication"] or {}).get("external_url")
            or decoded(row["recovery"] or {}).get("url"),
            "has_checkpoint": bool(decoded(row["checkpoint"] or {}).get("ephemeral_commit_sha")),
        }
        for row in rows
    }
