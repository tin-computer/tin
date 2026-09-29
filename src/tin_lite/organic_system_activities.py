"""Fixed organic recipe: normal pinned child runs and existing file handoff contracts."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import technical_fix
from tin_lite.organic_audit import digest
from tin_lite.organic_system import (
    KEY,
    STEPS,
    check_inputs,
    drafts_articles,
    policy_steps,
    schedules_articles,
    system_facts,
)
from tin_lite.run_reports import publish_run_report
from tin_lite.schedules import WorkflowSchedule
from tin_lite.technical_fix_sources import TechnicalFixSources
from tin_lite.workflow_definitions import ensure_schedule_allowed
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs
from tin_lite.workflow_prerequisites import PrerequisiteError

# The first scheduled draft comes a week after the system's own first article at the earliest.
WEEKLY_START_DELAY = timedelta(days=7)


async def founder_timezone(database, project_id):
    """The timezone Start here recorded for the founder, then the newest saved schedule's."""
    candidates = [
        await database.pool.fetchval(
            "SELECT input->>'timezone' FROM workflow_runs WHERE project_id=$1 "
            "AND executor='growth.onboarding' AND input ? 'timezone' "
            "ORDER BY created_at DESC LIMIT 1",
            project_id,
        ),
        await database.pool.fetchval(
            "SELECT schedule->>'timezone' FROM project_workflows WHERE project_id=$1 "
            "AND schedule IS NOT NULL AND status IN ('active','paused') "
            "ORDER BY created_at DESC LIMIT 1",
            project_id,
        ),
        getattr(await database.get_project(project_id), "timezone", None),
    ]
    for value in candidates:
        try:
            if value:
                ZoneInfo(value)
                return value
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


def weekly_section(weekly):
    lines = ["## Weekly articles", "", f"Status: {weekly['status']}", ""]
    if weekly["status"] != "succeeded":
        return lines + [f"Reason: {weekly['reason'].replace('_', ' ')}.", ""]
    days = [day.capitalize() for day in weekly["weekdays"]]
    named = days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]
    first = (weekly.get("next_run_at") or "")[:10]
    return lines + [
        f"Tin drafts the next planned article every {named} at {weekly['local_time']} "
        f"({weekly['timezone']})" + (f", starting {first}." if first else ".") + " "
        "Each draft waits for your review in Decisions, and no new draft starts while one "
        "is waiting. Pause it or change its days in My system.",
        "",
        f"Saved workflow: `{weekly['project_workflow_id']}`",
        "",
    ]


class OrganicSystemActivities:
    def __init__(self, *, database, storage, settings, integrations, temporal=None):
        self.db, self.storage, self.settings, self.integrations = (
            database,
            storage,
            settings,
            integrations,
        )
        self.temporal = temporal

    async def active(self, run_id):
        run = await self.db.get_run(UUID(str(run_id)))
        if run is None or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError(
                "Organic traffic system is no longer active.", non_retryable=True
            )
        if not await self.db.has_project_access(
            project_id=run.project_id,
            clerk_user_id=run.started_by_clerk_user_id,
        ):
            raise ApplicationError("Project access is no longer available.", non_retryable=True)
        return run

    async def saved(self, run_id, stage):
        receipt = await self.db.get_effect(f"traffic:{run_id}:{stage}")
        return receipt.result if receipt and receipt.status == "completed" else None

    @activity.defn
    async def organic_system_prepare(self, run_id: str):
        run = await self.active(run_id)
        check_inputs(run.input)
        await self.db.mark_run_running(run.id)
        key = f"traffic:{run_id}:prepare"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            definition = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=f"workflows/{KEY}.json",
                )
            )
            policy = definition.get("organic_system_policy")
            try:
                steps = policy_steps(policy)
            except ValueError as exc:
                raise ApplicationError(
                    "Unsupported organic system policy.", non_retryable=True
                ) from exc
            definitions = {}
            for step, workflow_key in steps.items():
                if step == "technical" and not run.input["technical_fix"]:
                    continue
                child = json.loads(
                    await self.storage.read_canonical_artifact(
                        repo_id="registry/workflows",
                        commit_sha=run.definition_commit_sha,
                        path=f"workflows/{workflow_key}.json",
                    )
                )
                if child.get("key") != workflow_key or child.get("executor") != (
                    "codex.procedure"
                    if step in {"technical", "draft", "delivery"}
                    else workflow_key
                ):
                    raise ApplicationError(
                        "System child contract is unavailable.", non_retryable=True
                    )
                definitions[step] = child
            content_delivery = None
            if drafts_articles(policy):
                from tin_lite.organic_content import pin_destination

                content_delivery = await pin_destination(self.db, self.integrations, run)
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "definition_revision": run.definition_commit_sha,
                    "definitions": definitions,
                    "input_sha256": digest(run.input),
                    "policy": policy,
                    "content_delivery": content_delivery,
                },
            )
        await self.db.project_run_progress(
            run_id=run.id,
            mode="steps",
            current=0,
            total=len(steps),
            step="research",
            summary="Auditing the website and researching buyer searches.",
        )

    async def _child_inputs(self, run, step):
        inputs = run.input
        facts = await system_facts(database=self.db, project_id=run.project_id, run_id=run.id)
        children = {row["step"]: row for row in facts["steps"]}
        if step == "audit":
            return {key: inputs[key] for key in ("site_url", "market")}, None
        if step == "keywords":
            return {
                "site_url": inputs["site_url"],
                "market": inputs["market"],
                "buyer_context": inputs["buyer_context"],
                "max_cost_usd": inputs["keyword_max_cost_usd"],
            }, None
        if step == "technical":
            if not inputs["technical_fix"]:
                return None, "not_requested"
            if children["audit"]["status"] != "succeeded":
                return None, "audit_unavailable"
            prepared = await self.saved(run.id, "prepare")
            policy = technical_fix.definition_policy(prepared["definitions"]["technical"])
            source = await TechnicalFixSources(
                database=self.db,
                storage=self.storage,
                supported_checks=technical_fix.supported_checks(policy),
            ).inspect(
                project_id=run.project_id,
                audit_run_id=UUID(children["audit"]["run_id"]),
            )
            candidates = [row for row in source["findings"] if row["source_eligible"]]
            if not candidates:
                return None, "no_eligible_findings"
            return {
                "audit_run_id": children["audit"]["run_id"],
                "audit_revision": source["source"]["audit_revision"],
                "finding_id": candidates[0]["finding"]["id"],
                "expected_repository": inputs["expected_repository"],
                "repository_serves_site": inputs["repository_serves_site"],
                "context": "One finding selected from this system run's audit. No unrelated edits.",
            }, None
        if step == "content":
            if any(children[key]["status"] != "succeeded" for key in ("audit", "keywords")):
                return None, "research_unavailable"
            return {
                "audit_run_id": children["audit"]["run_id"],
                "keyword_run_id": children["keywords"]["run_id"],
                "start_date": inputs["start_date"],
                "duration": inputs["duration"],
                "pieces_per_batch": 2,
                "context_files": [],
                "amendment_id": "",
            }, None
        if step == "draft":
            if children["content"]["status"] != "succeeded":
                return None, "content_plan_unavailable"
            from tin_lite.content_draft_sources import ContentDraftSources

            program_id = children["content"]["project_workflow_id"]
            discovery = await ContentDraftSources(database=self.db, storage=self.storage).discover(
                project_id=run.project_id, program_id=UUID(program_id)
            )
            if not discovery["next"]["available"]:
                return None, (
                    "nothing_to_draft"
                    if discovery["next"]["item_id"] is None
                    else "next_article_needs_attention"
                )
            return {"program_id": program_id, "delivery": "draft_only"}, None
        if step == "delivery":
            prepared = await self.saved(run.id, "prepare")
            intent = prepared["content_delivery"]
            if intent["mode"] == "draft_only":
                return None, intent["reason"]
            if children["draft"]["status"] == "skipped":
                return None, "nothing_to_deliver"
            if children["draft"]["status"] != "succeeded":
                return None, "draft_unavailable"
            draft = await self.db.get_run(UUID(children["draft"]["run_id"]))
            from tin_lite.content_delivery import choice_key
            from tin_lite.content_editorial_judgment import NO_DRAFT, saved

            choice = await self.db.get_effect(choice_key(draft.id))
            if choice and choice.status == "completed" and choice.result.get("mode") == "none":
                return None, "draft_only_selected"
            judgment = await saved(self.db, draft)
            if judgment and judgment["outcome"] in NO_DRAFT:
                return None, (
                    "already_covered"
                    if judgment["outcome"] == "already_covered"
                    else "editorial_attention_required"
                )
            if draft.review_decision != "approved":
                return None, "draft_not_approved"
            return {
                "source_run_id": str(draft.id),
                "expected_repository": intent["binding"]["repository"],
            }, None
        raise ValueError("Unknown organic system step.")

    @activity.defn
    async def organic_system_step(self, payload: dict[str, str]) -> dict[str, str]:
        from tin_lite.run_service import start_workflow_run

        run_id, step = payload["run_id"], payload["step"]
        if step not in STEPS:
            raise ApplicationError("Unknown organic system step.", non_retryable=True)
        run = await self.active(run_id)
        prepared = await self.saved(run_id, "prepare")
        if not prepared or prepared["input_sha256"] != digest(run.input):
            raise ApplicationError("System preparation is unavailable.", non_retryable=True)
        if step not in prepared["definitions"] and step != "technical":
            return {"status": "skipped", "reason": "not_in_pinned_recipe"}
        key = f"traffic:{run_id}:step:{step}"
        async with (
            self.db.effect_lock(f"traffic:{run_id}:control", KEY),
            self.db.effect_lock(key, KEY) as (conn, receipt),
        ):
            run = await self.active(run_id)
            if receipt and receipt.status == "completed":
                return receipt.result
            inputs, reason = await self._child_inputs(run, step)
            if inputs is None:
                result = {
                    "status": "skipped"
                    if reason
                    in {
                        "not_requested",
                        "no_eligible_findings",
                        "nothing_to_draft",
                        "nothing_to_deliver",
                        "draft_only_selected",
                        "github_not_connected",
                        "github_repository_not_selected",
                        "already_covered",
                    }
                    else "blocked",
                    "reason": reason,
                }
            else:
                definition = prepared["definitions"][step]
                template = await self.db.get_registry_workflow(STEPS[step])
                if template is None or template.executor != definition["executor"]:
                    raise ApplicationError("System child executor changed.", non_retryable=True)
                template = replace(template, definition=definition)
                configured_id = None
                if step == "content":
                    inputs = normalize_workflow_inputs(
                        schema=definition["input_schema"], project_id=run.project_id, inputs=inputs
                    )
                    configured = await self.db.create_project_workflow(
                        project_id=run.project_id,
                        workflow_id=template.id,
                        definition_commit_sha=prepared["definition_revision"],
                        name=f"Organic traffic — {run.input['site_url']}",
                        inputs=inputs,
                        input_schema=definition["input_schema"],
                        schedule=None,
                        request_id=uuid5(run.id, "content-program"),
                        created_by_clerk_user_id=run.started_by_clerk_user_id,
                        pinned_definition=definition,
                    )
                    await self.db.project_workflow_synced(
                        project_workflow_id=configured.id,
                        temporal_schedule_id=None,
                        next_run_at=None,
                    )
                    configured_id = configured.id
                try:
                    child = await start_workflow_run(
                        runtime=SimpleNamespace(
                            database=self.db, storage=self.storage, integrations=self.integrations
                        ),
                        settings=self.settings,
                        workflow=template,
                        project_id=run.project_id,
                        started_by_clerk_user_id=run.started_by_clerk_user_id,
                        start_idempotency_key=f"system:{run.id}:{step}",
                        input_payload=inputs,
                        project_workflow_id=configured_id,
                        definition_commit_sha=prepared["definition_revision"],
                        input_schema=definition["input_schema"],
                        trigger_source=run.trigger_source,
                        trigger_client=run.trigger_client,
                        started_by_oauth_client_id=run.started_by_oauth_client_id,
                        _prepare_only=True,
                        _organic_parent_run_id=run.id if step == "draft" else None,
                        _billing_parent_run_id=(
                            run.id if getattr(self.db, "billing", None) else None
                        ),
                    )
                except PrerequisiteError as exc:
                    raise ApplicationError(str(exc), type=exc.code, non_retryable=True) from exc
                result = {
                    "run_id": str(child.id),
                    "executor": child.executor,
                    "temporal_workflow_id": child.temporal_workflow_id,
                }
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    @activity.defn
    async def organic_system_step_failure(self, payload: dict[str, str]):
        run_id, step = payload["run_id"], payload["step"]
        if step not in STEPS:
            raise ValueError("Unknown system step")
        key = f"traffic:{run_id}:step:{step}"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "status": "failed",
                    "reason": "step_preparation_failed",
                },
            )

    @activity.defn
    async def organic_system_weekly_articles(self, run_id: str) -> dict:
        """Save one weekly content.generate configuration for this system's content program.

        Each occurrence drafts the next planned article and waits for review; the dispatcher
        holds an occurrence while an earlier draft from the program still waits. Not a child
        run: the parent's spending bound is unchanged and each occurrence is billed on its own.
        """
        key = f"traffic:{run_id}:weekly"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            run = await self.active(run_id)
            prepared = await self.saved(run_id, "prepare")
            if not prepared or prepared["input_sha256"] != digest(run.input):
                raise ApplicationError("System preparation is unavailable.", non_retryable=True)
            result = await self._weekly_articles(run, prepared)
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    async def _weekly_articles(self, run, prepared):
        if not schedules_articles(prepared["policy"]):
            return {"status": "skipped", "reason": "not_in_pinned_recipe"}
        weekdays = list(run.input.get("article_weekdays") or [])
        if not weekdays:
            return {"status": "skipped", "reason": "weekly_articles_off"}
        facts = await system_facts(database=self.db, project_id=run.project_id, run_id=run.id)
        content = next(row for row in facts["steps"] if row["step"] == "content")
        if content["status"] != "succeeded" or not content["project_workflow_id"]:
            return {"status": "skipped", "reason": "content_plan_unavailable"}
        definition = prepared["definitions"]["draft"]
        schedule = WorkflowSchedule(
            cadence="weekly",
            weekdays=weekdays,
            local_time=run.input.get("article_local_time") or "10:00",
            timezone=await founder_timezone(self.db, run.project_id),
            start_at=datetime.now(UTC) + WEEKLY_START_DELAY,
        )
        try:
            ensure_schedule_allowed(definition, schedule)
        except WorkflowInputError:
            return {"status": "blocked", "reason": "weekly_schedule_unsupported"}
        if self.temporal is None:
            return {"status": "blocked", "reason": "scheduling_unavailable"}
        template = await self.db.get_registry_workflow(STEPS["draft"])
        if template is None or template.executor != definition["executor"]:
            raise ApplicationError("System child executor changed.", non_retryable=True)
        from tin_lite.project_workflow_operations import sync_project_workflow

        configured = await self.db.create_project_workflow(
            project_id=run.project_id,
            workflow_id=template.id,
            definition_commit_sha=prepared["definition_revision"],
            name=f"Weekly article — {run.input['site_url']}",
            inputs=normalize_workflow_inputs(
                schema=definition["input_schema"],
                project_id=run.project_id,
                inputs={"program_id": content["project_workflow_id"]},
            ),
            input_schema=definition["input_schema"],
            schedule=schedule.model_dump(mode="json"),
            request_id=uuid5(run.id, "weekly-articles"),
            created_by_clerk_user_id=run.started_by_clerk_user_id,
            pinned_definition=definition,
        )
        configured = await sync_project_workflow(
            runtime=SimpleNamespace(database=self.db, temporal=self.temporal),
            settings=self.settings,
            configured=configured,
        )
        saved = WorkflowSchedule.model_validate(configured.schedule)
        return {
            "status": "succeeded",
            "project_workflow_id": str(configured.id),
            "program_id": content["project_workflow_id"],
            "weekdays": saved.weekdays,
            "local_time": saved.local_time,
            "timezone": saved.timezone,
            "next_run_at": configured.next_run_at.isoformat() if configured.next_run_at else None,
        }

    @activity.defn
    async def organic_system_weekly_articles_failure(self, run_id: str):
        key = f"traffic:{run_id}:weekly"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            run = await self.db.get_run(UUID(run_id))
            if run is not None:
                # A configuration saved before its Temporal schedule failed would never run.
                await self.db.pool.execute(
                    "UPDATE project_workflows SET status='archived', next_run_at=NULL, "
                    "updated_at=now() WHERE project_id=$1 AND request_id=$2 "
                    "AND temporal_schedule_id IS NULL AND status<>'archived'",
                    run.project_id,
                    uuid5(run.id, "weekly-articles"),
                )
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={"status": "failed", "reason": "weekly_schedule_not_saved"},
            )

    @activity.defn
    async def organic_system_progress(self, run_id: str):
        run = await self.active(run_id)
        facts = await system_facts(database=self.db, project_id=run.project_id, run_id=run.id)
        count = sum(
            row["status"] in {"succeeded", "failed", "stopped", "skipped", "blocked"}
            for row in facts["steps"]
        )
        await self.db.project_run_progress(
            run_id=run.id,
            mode="steps",
            current=count,
            total=len(facts["steps"]),
            step="system",
            summary=f"{count} of {len(facts['steps'])} organic traffic steps accounted for.",
        )

    @activity.defn
    async def organic_system_finish(self, run_id: str) -> bool:
        run = await self.db.get_run(UUID(run_id))
        facts = await system_facts(database=self.db, project_id=run.project_id, run_id=run.id)
        failed = any(row["status"] not in {"succeeded", "skipped"} for row in facts["steps"])
        lines = [
            "# Organic traffic system",
            "",
            "## Result",
            "",
            "Some steps could not finish. Completed outputs are preserved."
            if failed
            else "The requested organic traffic steps are complete. "
            "Nothing was merged or published to the website.",
            "",
            f"Website: {run.input['site_url']}",
            "",
        ]
        for row in facts["steps"]:
            lines.extend([f"## {row['workflow_key']}", "", f"Status: {row['status']}", ""])
            if row["reason"]:
                lines.extend([f"Reason: {row['reason'].replace('_', ' ')}.", ""])
            if row["run_id"]:
                lines.extend([f"Run: `{row['run_id']}`", ""])
            if row["artifact_ref"]:
                lines.extend([f"Source: `{row['artifact_ref']}`", ""])
            if row["project_workflow_id"]:
                lines.extend(
                    [
                        "The content program is saved in My system, manual-only. "
                        "You can edit upcoming batches and choose its schedule there.",
                        "",
                    ]
                )
        weekly = facts.get("weekly_articles")
        if weekly and weekly.get("reason") != "not_in_pinned_recipe":
            lines.extend(weekly_section(weekly))
        scheduled = bool(weekly and weekly.get("status") == "succeeded")
        lines.extend(
            [
                "## Boundaries",
                "",
                "One technical finding at most; others remain for review. "
                "No eligible finding means no technical-fix compute or PR. "
                "The next planned article is drafted when this recipe includes generation. "
                "Its final approved copy can become an unmerged PR; without GitHub, "
                "the Markdown stays readable in Tin for manual export or a later delivery. "
                "The existing writing guide is used when available; no style samples are invented. "
                "Backlinks and outreach are not part of this version. "
                + (
                    "Plan dates alone draft nothing; the weekly schedule drafts one planned "
                    "article at a time and follows the program's delivery settings. "
                    if scheduled
                    else "Plan dates do not automatically generate the rest of the roadmap. "
                )
                + "An open PR is not a published website.",
                "",
            ]
        )
        await publish_run_report(
            database=self.db,
            storage=self.storage,
            run_id=run.id,
            workflow_key=KEY,
            prefix="traffic",
            path=f"reports/organic-system/{run.id}/RESULT.md",
            content="\n".join(lines).encode(),
            summary="Organic traffic system needs attention."
            if failed
            else "Organic traffic system complete.",
            failed=failed,
        )
        return not failed

    @activity.defn
    async def organic_system_failure(self, run_id: str):
        await self.db.project_failure(
            run_id=UUID(run_id),
            error_message="Organic traffic system could not finish. "
            "Completed child outputs remain available; "
            "paid requests were not automatically repeated.",
        )
