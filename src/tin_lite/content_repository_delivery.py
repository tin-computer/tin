"""Repository adaptation of an approved page, using the ordinary procedure runner.

The page is immutable input, not an invitation to draft again. Admission, usage,
execution isolation, checkpoints and GitHub effects remain the existing contracts.
Sources are approved planned articles (content.generate), answer pages and public
articles. An approval can start this procedure itself (see ContentDelivery.adapt); when
the founder's delivery setting commits to main, Tin then merges the pull request, but
only one that adds nothing except the approved page, once GitHub reports it clean.

website.change (website_change.py) adapts pages with this same machinery: source pinning,
the page check, the saved patch, recovery and the merge loop. Its runs keep their pinned
source under the same receipt key, and their own policy decides whether Tin merges (see
publish_after_pull_request): website.change merges once the repository's required checks
pass. content.deliver's own rules are unchanged.
"""

import asyncio
import hashlib
import html
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
# website.change, which puts approved changes on a founder's website, reuses this machinery.
WEBSITE_CHANGE_ID = UUID("00000000-0000-4000-8000-000000000045")
WEBSITE_CHANGE_OPERATION = "website_change_source_v1"
ADAPTER_WORKFLOW_IDS = frozenset({WORKFLOW_ID, WEBSITE_CHANGE_ID})
SOURCE_OPERATIONS = {WORKFLOW_ID: OPERATION, WEBSITE_CHANGE_ID: WEBSITE_CHANGE_OPERATION}
CHECK_COMMAND = "git diff --check"
RECOVERY_OPERATION = "content_repository_delivery_recovery_v1"
MERGE_OPERATION = "content_repository_delivery_merge_v1"
# How long Tin waits for GitHub to call a pull request clean before leaving it open. The
# delivery activity allows five minutes; this leaves room for the merge call itself.
MERGE_WAIT_SECONDS = 210
MERGE_POLL_SECONDS = 15
# GitHub's mergeable_state values that let Tin merge. `clean`: every check passed.
# `has_hooks`: the same, with pre-receive hooks. content.deliver merges only on these.
MERGE_READY = frozenset({"clean", "has_hooks"})
# website.change merges once the checks the repository requires pass (Emre, 10/1). GitHub
# reports `unstable` for a pull request that can merge while a check the repository does not
# require fails or is still running; a failing required check reports `blocked` instead.
REQUIRED_CHECKS_READY = MERGE_READY | {"unstable"}
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


# A website.change run whose changes come from the latest audit (website_change_audit) rather
# than an approved page. Its source receipt sits under the same key; nothing else is shared.
AUDIT_SOURCE = "audit"
# Sources a website.change run repairs the site from with site-fix-v5's machinery: the audit's
# findings, and the URL changes other workflows planned.
REPAIR_SOURCES = frozenset({AUDIT_SOURCE, "planned"})
# A website.change run that applies the blog index plan's files as they are, without Codex.
BLOG_INDEX_SOURCE = "blog_index"


def _website_source(run):
    if getattr(run, "workflow_id", None) != WEBSITE_CHANGE_ID:
        return None
    return (getattr(run, "input", None) or {}).get("source")


def repairs_site(run):
    """Whether this is a website.change run that repairs the site from change rows (the
    audit's findings or planned URL changes) with site-fix-v5's preparation."""
    return _website_source(run) in REPAIR_SOURCES


def applies_plan(run):
    """Whether this is a website.change run that applies the blog index plan's files."""
    return _website_source(run) == BLOG_INDEX_SOURCE


def adapts(run):
    """Whether this run adapts an approved page to the site: content.deliver or website.change."""
    return (
        getattr(run, "workflow_id", None) in ADAPTER_WORKFLOW_IDS
        and not repairs_site(run)
        and not applies_plan(run)
    )


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
        # A website.change run also says which change it made and whether it may publish.
        **(
            {
                "change_id": source["change"]["change_id"],
                "publish": (source.get("publish") or {}).get("mode"),
            }
            if source.get("change")
            else {}
        ),
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


async def page_source(*, database, storage, project_id, source_run_id):
    """The approved page's exact copy and provenance: a planned article, answer page or
    public article. Refuses anything not approved in this project."""
    candidate = await database.get_run(UUID(str(source_run_id)))
    if approved_document.kind_for(candidate):
        return await approved_document.select(
            database=database,
            storage=storage,
            project_id=project_id,
            source_run_id=source_run_id,
        )
    return {
        "source_kind": "article",
        **await approved_article.select(
            database=database,
            storage=storage,
            project_id=project_id,
            source_run_id=source_run_id,
        ),
    }


async def select_source(*, database, storage, integrations, project_id, inputs, approval=False):
    """Pin the approved page and repository; `approval` marks a start by the page's approval.

    Only that start carries the founder's delivery (a pull request, or commit to main) into
    the run. Prepare PR and agent starts always leave their pull request open.
    """
    source = await page_source(
        database=database,
        storage=storage,
        project_id=project_id,
        source_run_id=inputs["source_run_id"],
    )
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


async def guard_page(conn, *, project_id, source):
    """Recheck the pinned page's approval under the project lock. A planned item (it carries
    its brief) is a content.generate run, whatever kind of page it drafted."""
    if source.get("source_kind") in {"answer_page", "public_article"} and "item" not in source:
        await approved_document.guard(conn, project_id=project_id, source=source)
    else:
        await approved_article.guard(conn, project_id=project_id, source=source)


async def guard_source(conn, *, project_id, inputs, source):
    """Called under create_run's project lock, in the run/budget/receipt transaction."""
    if inputs["source_run_id"] != source["source_run_id"]:
        raise ValueError("The selected article is not approved for delivery.")
    await guard_page(conn, project_id=project_id, source=source)
    await guard_attempts(conn, project_id=project_id, inputs=inputs, source=source)


async def guard_attempts(conn, *, project_id, inputs, source):
    """One adaptation per page and repository, whether content.deliver or website.change
    made it, so the two can never open two pull requests for the same page."""
    duplicate = await conn.fetchval(
        "SELECT r.id FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "AND s.operation = ANY($4::text[]) AND s.status='completed' "
        "WHERE r.project_id=$1 AND r.workflow_id = ANY($2::uuid[]) "
        "AND s.result->>'source_run_id'=$3 "
        "AND s.result->'binding'->>'repository_id'=$5 "
        "ORDER BY r.created_at DESC LIMIT 1",
        project_id,
        list(ADAPTER_WORKFLOW_IDS),
        source["source_run_id"],
        list(SOURCE_OPERATIONS.values()),
        str(source["binding"]["repository_id"]),
    )
    retry = inputs.get("retry_run_id")
    if retry and retry != str(duplicate):
        raise ValueError("Choose the latest failed adaptation for this article and repository.")
    if not duplicate:
        return
    prior = await conn.fetchrow("SELECT status FROM workflow_runs WHERE id=$1", duplicate)
    # GitHub refused the earlier PR before writing anything (for example another open PR
    # changed a shared file), or never got the request: nothing was delivered, so a fresh
    # adaptation may replace it. A PR that opened, or a request with an unknown outcome,
    # still has to be reconciled first. A saved patch alone no longer blocks; Retry delivery
    # stays the free way to send it.
    delivered = await conn.fetchval(
        "SELECT status IN ('started', 'completed') FROM integration_call_receipts "
        "WHERE execution_key=$1",
        f"{duplicate}:procedure_pull_request",
    )
    if prior["status"] == "failed" and not delivered:
        return
    if prior["status"] in {"pending", "running", "needs_input"}:
        raise ValueError(
            f"This article's delivery run {duplicate} is still working; wait for it to finish."
        )
    raise ValueError(
        f"This article already has a delivery. Open run {duplicate} for its pull request"
        + (" and reconcile it before paying for a new adaptation." if retry else ".")
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
            proof = validate_patch(manifest, source)
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
                blocking_paths=blocking_paths(proof),
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

    if intent.get("via") == "website.change":
        # A content.generate answer page goes to the site through website.change.
        workflow = await runtime.database.get_workflow(WEBSITE_CHANGE_ID)
        if workflow is None:
            raise LookupError("website.change is not installed on this Tin.")
        return await start_workflow_run(
            runtime=runtime,
            settings=settings,
            workflow=workflow,
            project_id=run.project_id,
            started_by_clerk_user_id=intent.get("chosen_by") or run.started_by_clerk_user_id,
            start_idempotency_key=adaptation_start_key(run.id),
            input_payload={
                "source": "content_draft",
                "source_run_id": str(run.id),
                "expected_repository": intent["settings"]["repository"],
            },
            trigger_source=intent.get("trigger_source") or "manual",
        )
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


# Settings that reach every page, whatever folder they sit in.
# Dependency and package-manager files. They reach every page, wherever they sit, so a patch
# that changes one always waits for the founder's review.
DEPENDENCY_FILES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "bun.lock",
        "bun.lockb",
        ".npmrc",
        ".yarnrc.yml",
        "Gemfile.lock",
        "composer.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
    }
)
SITE_WIDE_NAMES = DEPENDENCY_FILES | {
    "vercel.json",
    "netlify.toml",
    "wrangler.toml",
    "wrangler.json",
    "tsconfig.json",
    "jsconfig.json",
}
SITE_WIDE_STEMS = (
    "middleware.",
    "next.config.",
    "astro.config.",
    "nuxt.config.",
    "svelte.config.",
    "vite.config.",
    "remix.config.",
    "gatsby-config.",
)
# Framework entry files that wrap or replace every page below them, wherever they sit:
# Next.js layouts, templates, error and loading boundaries, route handlers and the pages
# router's _app/_document, SvelteKit's +layout and +error, Remix's root.
FRAMEWORK_ENTRY_STEMS = frozenset(
    {
        "layout",
        "template",
        "error",
        "global-error",
        "not-found",
        "loading",
        "default",
        "route",
        "instrumentation",
        "_app",
        "_document",
        "_error",
        "_middleware",
        "+layout",
        "+error",
        "+server",
        "root",
    }
)
# Source folders frameworks route from. A chosen route named after one (`/app/{slug}`,
# `/pages/{slug}`) can't tell the route's folder from the framework's, so it merges nothing
# but the page itself.
FRAMEWORK_ROOT_FOLDERS = frozenset(
    {"app", "pages", "src", "routes", "api", "components", "lib", "layouts", "public", "static"}
)


def outside_route(manifest, proof, route):
    """The patch's files besides the page that don't sit in the chosen route's own folder.

    For `/guides/{slug}` a file serves the route only when its directories include `guides`
    (`src/app/guides/[slug]/page.tsx`, `content/guides/...`). Root layouts, middleware, host and
    build settings, shared components and dotfiles reach other pages, so they stay a PR, as do
    framework entry files (a layout or _document) anywhere. A route named after a framework's
    own source folder (`/app/{slug}`) can't be told apart from it, so only the page merges.
    """
    folders = [part for part in route.split("{slug}", 1)[0].strip("/").split("/") if part]
    if FRAMEWORK_ROOT_FOLDERS.intersection(folders):
        folders = []
    outside = []
    for item in manifest.get("files") or []:
        path = item["path"]
        if path == proof["article_path"]:
            continue
        parts = path.split("/")
        name = parts[-1]
        directories = parts[:-1]
        inside = bool(folders) and any(
            directories[index : index + len(folders)] == folders
            for index in range(len(directories) - len(folders) + 1)
        )
        if (
            not inside
            or any(part.startswith(".") for part in parts)
            or name in SITE_WIDE_NAMES
            or name.startswith(SITE_WIDE_STEMS)
            or name.split(".", 1)[0] in FRAMEWORK_ENTRY_STEMS
        ):
            outside.append(path)
    return outside


def merge_rule(manifest, proof, route):
    """Which rule lets Tin merge this patch under a commit-to-main setting, else None.

    `page_only`: the approved page alone, the change the Markdown publisher commits today.
    `chosen_route`: the page plus the site code that serves it, when the founder chose where
    these pages live, the PR puts the page at that route and every other file sits in that
    route's own folder. Neither applies when Tin can't confirm the page keeps the approved
    wording; any other site change stays a PR.
    """
    from tin_lite.page_routes import matches

    if proof.get("copy_check") == "not_confirmed":
        return None
    if page_only(manifest, proof):
        return "page_only"
    if (
        route
        and matches(route, proof.get("public_route"))
        and not outside_route(manifest, proof, route)
    ):
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
    main, is merged: when its patch passes `merge_rule`, the page is a file it adds rather
    than one it rewrites, its branch still holds exactly that patch, and GitHub calls it
    clean (no conflicts, no failing or pending checks, no required review) within a few
    minutes. A website.change run merges once the repository's required checks pass, so a
    failing optional check does not hold it (REQUIRED_CHECKS_READY), and its receipt names
    the state that allowed the merge. Otherwise the PR stays open and the receipt says why.
    The outcome is recorded once; retries reuse it.
    """
    source = await saved_source(database, run.id)
    # A website.change run always records its outcome: merged, or open and why.
    website = bool(source.get("change"))
    if not website and (source.get("approval") or {}).get("mode") != "github_commit":
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
            proof = validate_patch(manifest, source)
            number = published["pull_request_number"]
            base = {"pull_request": published["external_url"], "number": number}
            hold = None
            if website:
                from tin_lite import website_change

                route = source.get("route")
                hold = await website_change.hold_reason(database, run, source, manifest, proof)
            else:
                route = (source.get("approval") or {}).get("route")
            rule = merge_rule(manifest, proof, route)
            from tin_lite.page_routes import matches

            outside = (
                outside_route(manifest, proof, route)
                if route and matches(route, proof.get("public_route"))
                else []
            )
            if hold:
                result = {**base, "status": "left_open", "reason": hold}
            elif proof["copy_check"] == "not_confirmed":
                result = {
                    **base,
                    "status": "left_open",
                    "reason": "Tin couldn't confirm the page keeps the approved wording word for "
                    "word, so it waits for your review.",
                }
            elif rule is None:
                result = {
                    **base,
                    "status": "left_open",
                    "reason": (
                        f"It also changes {', '.join(outside[:3])}, outside your chosen route "
                        f"{route}, so it waits for your review."
                        if outside
                        else f"It changes site files besides the page and does not put the page "
                        f"at your chosen route {route}, so it waits for your review."
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
                        new_paths=(proof["article_path"],),
                        number=number,
                        branch=published.get("pull_request_branch"),
                        sleep=sleep,
                        clock=clock,
                        ready=REQUIRED_CHECKS_READY if website else MERGE_READY,
                        record_state=website,
                    ),
                }
            prefix = "website_change" if website else "content_delivery"
            if result["status"] == "merged":
                event, summary = (
                    f"{prefix}_merged",
                    f"Tin merged PR #{number} into {manifest['default_branch']}."
                    if result.get("merged_by") == "tin"
                    else f"PR #{number} was merged into {manifest['default_branch']}.",
                )
            else:
                event, summary = (
                    f"{prefix}_left_open",
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


async def _merge_when_clean(
    *,
    integrations,
    run,
    source,
    manifest,
    new_paths,
    number,
    branch,
    sleep,
    clock,
    ready=MERGE_READY,
    record_state=False,
):
    """Merge once GitHub reports one of the `ready` states, or say why the PR stays open.

    Each of `new_paths` must be a file the pull request adds (a page), never one it rewrites.

    `dirty`, `behind` and `draft` stop at once. `blocked` and `unknown` never merge; Tin
    waits for them to change until the deadline. With `record_state`, the merge receipt
    names the state that allowed the merge.

    `unstable` counts only where the base branch requires status checks (Emre, 10/1): with
    none, or none Tin can read, nothing says which checks matter, so Tin waits for every
    check to pass (`clean`). The receipt records which rule applied (`checks_rule`).
    """
    binding = binding_from(source)
    rule = {}
    reason = "Its checks had not all passed after a few minutes, so Tin left it open."
    if "unstable" in ready:
        required = await required_checks(integrations, run, binding)
        if required:
            rule = {"checks_rule": "required_checks", "required_checks": required}
            reason = "Its required checks had not passed after a few minutes, so Tin left it open."
        else:
            ready = MERGE_READY
            rule = {"checks_rule": "all_checks", "required_checks": []}
            reason = (
                "Your repository requires no status checks, so Tin waits for every check to "
                "pass. They had not all passed after a few minutes, so Tin left it open."
            )
    result = await _merge_loop(
        integrations=integrations,
        run=run,
        binding=binding,
        manifest=manifest,
        new_paths=new_paths,
        number=number,
        branch=branch,
        sleep=sleep,
        clock=clock,
        ready=ready,
        record_state=record_state,
        reason=reason,
    )
    return {**result, **rule}


async def required_checks(integrations, run, binding):
    """The status checks the base branch requires; none when GitHub can't say."""
    try:
        found = await integrations.github_required_status_checks(
            project_id=run.project_id,
            repository=binding.repository,
            branch=binding.default_branch,
        )
    except Exception:
        return []
    return list(found.get("contexts") or []) if found.get("readable") else []


async def _merge_loop(
    *,
    integrations,
    run,
    binding,
    manifest,
    new_paths,
    number,
    branch,
    sleep,
    clock,
    ready,
    record_state,
    reason,
):
    deadline = clock().timestamp() + MERGE_WAIT_SECONDS
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
        if state.get("mergeable") is True and state.get("mergeable_state") in ready:
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
                    new_paths=tuple(new_paths),
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
                    **({"mergeable_state": state["mergeable_state"]} if record_state else {}),
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
    """Find the page in the patch and say how closely it keeps the approved wording.

    The page takes whatever form the site uses: a Markdown or MDX file, a component, a typed
    page registry or plain HTML. A pull request is the founder's to review, so neither the
    page's format nor its wording refuses one; only the pinned source and repository do.
    `copy_check` decides whether Tin may merge on its own (merge_rule): it may when the
    article is stored exactly (`exact_source_preserved`) or every paragraph reads word for
    word in the page (`wording_preserved`), never when `not_confirmed`. Rendering and build
    checks are separate and never implied.
    """
    article = source["article"]
    if hashlib.sha256(article.encode()).hexdigest() != source["article_sha256"]:
        raise ValueError("The approved article proof is inconsistent.")
    if (
        manifest["repository"] != source["binding"]["repository"]
        or manifest["head_sha"] != source["binding"]["head_sha"]
    ):
        raise ValueError("The repository differs from the pinned delivery source.")
    paragraphs = [words for block in re.split(r"\n\s*\n", article) if (words := _words(block))]
    exact, worded, closest = [], [], (0, None)
    for item in manifest["files"]:
        path, text = item["path"], item["content"]
        if _stores_exactly(path, text, article):
            exact.append(path)
            continue
        page = f" {' '.join(_words(text))} "
        found = sum(f" {' '.join(words)} " in page for words in paragraphs)
        if paragraphs and found == len(paragraphs):
            worded.append(path)
        elif found > closest[0]:
            closest = (found, path)
    if len(exact) == 1:
        path, check = exact[0], "exact_source_preserved"
    elif not exact and len(worded) == 1:
        path, check = worded[0], "wording_preserved"
    else:
        path, check = (exact or worded or [closest[1]])[0], "not_confirmed"
    from tin_lite.page_urls import public_route

    route = public_route(manifest.get("body"))
    return {
        "article_path": path,
        "article_sha256": source["article_sha256"],
        "copy_check": check,
        "build_check": "not_verified_by_tin",
        **({"public_route": route} if route else {}),
    }


def _stores_exactly(path, text, article):
    """The article byte for byte: a Markdown body, or one complete JSON string token."""
    if path.endswith((".md", ".mdx")):
        body = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.S)
        return body.lstrip("\n") == article
    if path.endswith((".tsx", ".jsx", ".js", ".ts", ".astro", ".vue", ".svelte", ".json")):
        # Complete JSON string tokens, not fragment matches in escaped prose.
        return any(
            json.loads(token) == article
            for token in re.findall(
                r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"', text
            )
        )
    return False


def _words(text):
    """The words a reader sees, in order: no markup, link targets, scripts or styles."""
    text = re.sub(r"<(script|style)\b.*?</\1\s*>|<!--.*?-->", " ", text, flags=re.S | re.I)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)", " ", text)
    text = html.unescape(re.sub(r"</?[A-Za-z!][^>]*>", " ", text))
    return re.findall(r"[^\W_]+", text.casefold())


def blocking_paths(proof):
    """The page's own path, the one file another open PR must not also add; None when the
    patch has no recognizable page, so every file blocks as for any other PR."""
    return frozenset({proof["article_path"]}) if proof.get("article_path") else None


def validate_patch(manifest, source):
    """The page check for content.deliver and website.change alike. The procedure's own file
    and byte caps bound the patch; website_change.hold_reason decides a website merge."""
    return validate_copy(manifest, source)


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
