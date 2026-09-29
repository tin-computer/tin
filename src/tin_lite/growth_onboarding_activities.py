"""Fixed onboarding recipe: one pinned plan child, one hold, then Tin sets the picks up."""

import json
import re
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid5

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import analytics
from tin_lite.content_delivery import ContentDelivery, DeliverySettings
from tin_lite.growth_onboarding import (
    CONTENT_DRAFT_KEYS,
    KEY,
    PLAN_BLOCK,
    PLAN_PATH,
    POLICY,
    STEPS,
    apply_priority,
    chosen_systems,
    current_plan_text,
    picked_actions,
    plan_block,
    plan_connections,
    plan_control,
    plan_picks,
    plan_view,
    render_report,
    system_facts,
    systems_details,
    ui_links,
)
from tin_lite.onboarding import onboarding_tin_state
from tin_lite.organic_audit import digest
from tin_lite.product_urls import dashboard_url
from tin_lite.run_reports import publish_report_file
from tin_lite.schedules import TemporalScheduleService, WorkflowSchedule, next_run_after
from tin_lite.workflow_definitions import ensure_schedule_allowed
from tin_lite.workflow_inputs import (
    WorkflowInputError,
    coerce_schema_inputs,
    normalize_workflow_inputs,
)
from tin_lite.workflow_prerequisites import PrerequisiteError

ACTIVE_STATUSES = {"pending", "running", "needs_input"}


_URL_IN_TEXT = re.compile(r"https?://[^\s;,)]+")
_DOMAIN_IN_TEXT = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b", re.I)


def tidy_known_inputs(key: str, inputs: dict, run_input: dict) -> tuple[dict, list[str]]:
    """Repair inputs the plan writer tends to pad with prose.

    The visibility audit wants a domain or URL; a plan once passed "with.md — https://with.md;
    Markdown collaboration for developers" and the audit refused the domain three times.
    """
    fixed = dict(inputs)
    notes: list[str] = []
    if key == "visibility.audit":
        target = str(fixed.get("target") or "").strip()
        messy = " — " in target or ";" in target or len(target.split()) > 4
        if target and target != "this project" and messy:
            url = _URL_IN_TEXT.search(target)
            domain = _DOMAIN_IN_TEXT.search(target)
            replacement = (
                url.group(0)
                if url
                else domain.group(0)
                if domain
                else str(run_input.get("product_url") or "this project")
            )
            fixed["target"] = replacement
            notes.append(f"target set to {replacement!r}; the plan wrote a sentence")
    return fixed, notes


def coerce_enum_inputs(schema: dict, inputs: dict) -> tuple[dict, list[str]]:
    """Replace out-of-set enum values with the schema default (or drop them), noting each.

    The plan writer sometimes puts free text into an enum input; one bad value must not cost
    the founder the whole role.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return dict(inputs), []
    fixed = dict(inputs)
    notes: list[str] = []
    for key, value in list(inputs.items()):
        prop = properties.get(key)
        if not isinstance(prop, dict) or not isinstance(prop.get("enum"), list):
            continue
        if value in prop["enum"]:
            continue
        if "default" in prop:
            fixed[key] = prop["default"]
            notes.append(f"{key} reset to {prop['default']!r}; the plan wrote {value!r}")
        else:
            fixed.pop(key, None)
            notes.append(f"{key} dropped; the plan wrote {value!r}, not one of the choices")
    return fixed, notes


def repaired_action_inputs(action: dict, schema: dict, run_input: dict) -> tuple[dict, list[str]]:
    """The inputs setup actually starts a plan action with, and the repairs it made.

    Billing admits onboarding children by recomputing this, so both must agree.
    """
    tidied, tidy_notes = tidy_known_inputs(action["key"], action["inputs"], run_input or {})
    # Plans written before code typed their inputs carry numbers and booleans as text.
    typed, type_notes = coerce_schema_inputs(schema, tidied)
    coerced, notes = coerce_enum_inputs(schema, typed)
    return coerced, tidy_notes + type_notes + notes


def child_handles(actions):
    """The only setup facts allowed into Temporal history, including receipt replay."""
    return [
        {key: row[key] for key in ("run_id", "executor", "temporal_workflow_id")}
        for row in actions
        if row.get("run_id")
    ]


class GrowthOnboardingActivities:
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
        if run is None or run.executor != KEY or run.status.value not in ACTIVE_STATUSES:
            raise ApplicationError("Growth onboarding is no longer active.", non_retryable=True)
        if not await self.db.has_project_access(
            project_id=run.project_id,
            clerk_user_id=run.started_by_clerk_user_id,
        ):
            raise ApplicationError("Project access is no longer available.", non_retryable=True)
        return run

    async def saved(self, run_id, stage):
        receipt = await self.db.get_effect(f"onboarding:{run_id}:{stage}")
        return receipt.result if receipt and receipt.status == "completed" else None

    @activity.defn
    async def growth_onboarding_prepare(self, run_id: str):
        run = await self.active(run_id)
        await self.db.mark_run_running(run.id)
        key = f"onboarding:{run_id}:prepare"
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
            if definition.get("growth_onboarding_policy") != POLICY:
                raise ApplicationError("Unsupported onboarding policy.", non_retryable=True)
            definitions = {}
            for step, workflow_key in STEPS.items():
                child = json.loads(
                    await self.storage.read_canonical_artifact(
                        repo_id="registry/workflows",
                        commit_sha=run.definition_commit_sha,
                        path=f"workflows/{workflow_key}.json",
                    )
                )
                # The plan is a native LLM flow. A parent pinned before that change still holds the
                # Codex definition; it is read here, and starting that child then stops the parent
                # with "Onboarding child executor changed" rather than switching its executor.
                if child.get("key") != workflow_key or child.get("executor") not in {
                    workflow_key,
                    "codex.procedure",
                }:
                    raise ApplicationError(
                        "Onboarding child contract is unavailable.", non_retryable=True
                    )
                definitions[step] = child
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "definition_revision": run.definition_commit_sha,
                    "definitions": definitions,
                    "input_sha256": digest(run.input),
                },
            )
        await self.db.project_run_progress(
            run_id=run.id,
            mode="steps",
            current=0,
            total=3,
            step="plan",
            summary="Writing the plan from the site, memory and your agent's inputs.",
        )

    async def _start_child(
        self,
        run,
        prepared,
        *,
        step,
        workflow_key,
        inputs,
        project_workflow_id=None,
        template_override=None,
    ):
        from tin_lite.run_service import start_workflow_run

        definition = prepared["definitions"].get(step) if step in prepared["definitions"] else None
        template = template_override or await self.db.get_registry_workflow(workflow_key)
        if template is None:
            raise ApplicationError(f"{workflow_key} is not in the registry.", non_retryable=True)
        if definition is not None:
            if template.executor != definition["executor"]:
                raise ApplicationError("Onboarding child executor changed.", non_retryable=True)
            template = replace(template, definition=definition)
        child = await start_workflow_run(
            runtime=SimpleNamespace(
                database=self.db, storage=self.storage, integrations=self.integrations
            ),
            settings=self.settings,
            workflow=template,
            project_id=run.project_id,
            started_by_clerk_user_id=run.started_by_clerk_user_id,
            start_idempotency_key=f"onboarding:{run.id}:{step}",
            input_payload=inputs,
            project_workflow_id=project_workflow_id,
            definition_commit_sha=(
                prepared["definition_revision"]
                if definition is not None
                else template.current_commit_sha
            ),
            input_schema=(definition or template.definition)["input_schema"],
            trigger_source=run.trigger_source,
            trigger_client=run.trigger_client,
            started_by_oauth_client_id=run.started_by_oauth_client_id,
            _prepare_only=True,
            _billing_parent_run_id=(run.id if getattr(self.db, "billing", None) else None),
        )
        return {
            "run_id": str(child.id),
            "executor": child.executor,
            "temporal_workflow_id": child.temporal_workflow_id,
        }

    @activity.defn
    async def growth_onboarding_step(self, payload: dict[str, str]) -> dict[str, str]:
        run_id, step = payload["run_id"], payload["step"]
        if step not in STEPS:
            raise ApplicationError("Unknown onboarding step.", non_retryable=True)
        run = await self.active(run_id)
        prepared = await self.saved(run_id, "prepare")
        if not prepared or prepared["input_sha256"] != digest(run.input):
            raise ApplicationError("Onboarding preparation is unavailable.", non_retryable=True)
        key = f"onboarding:{run_id}:step:{step}"
        async with (
            self.db.effect_lock(f"onboarding:{run_id}:control", KEY) as (control_conn, _),
            self.db.effect_lock(key, KEY, conn=control_conn) as (conn, receipt),
        ):
            run = await self.active(run_id)
            if receipt and receipt.status == "completed":
                return receipt.result
            inputs = apply_priority({k: v for k, v in run.input.items() if k != "project_id"})
            try:
                result = await self._start_child(
                    run, prepared, step=step, workflow_key=STEPS[step], inputs=inputs
                )
            except PrerequisiteError as exc:
                raise ApplicationError(str(exc), type=exc.code, non_retryable=True) from exc
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    async def _plan(self, run):
        facts = await system_facts(database=self.db, project_id=run.project_id, run_id=run.id)
        plan = next(row for row in facts["steps"] if row["step"] == "plan")
        if plan["status"] != "succeeded" or not plan["canonical_commit_sha"]:
            raise ApplicationError("The plan did not finish.", non_retryable=True)
        return plan

    @activity.defn
    async def growth_onboarding_review(self, run_id: str) -> bool:
        run = await self.active(run_id)
        plan = await self._plan(run)
        project = await self.db.get_project(run.project_id)
        text = (
            await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=plan["canonical_commit_sha"],
                path=plan["artifact_path"],
            )
        ).decode("utf-8", "replace")
        _picked, offered = plan_picks(text)
        from tin_lite.onboarding_experience import validate_plan

        issues = await validate_plan(
            database=self.db,
            project_id=run.project_id,
            text=text,
            systems=offered,
            timezone=(run.input or {}).get("timezone") or "UTC",
            run_input=run.input or {},
        )
        view = plan_view(text)[:1200]
        await self.db.project_run_progress(
            run_id=run.id,
            mode="steps",
            current=1,
            total=3,
            step="systems",
            summary=(
                f"Waiting for your pick among {len(offered)} systems in {plan['artifact_path']}."
                + (
                    f" {len(issues)} setup issue(s) need correction before those workflows can run."
                    if issues
                    else ""
                )
            ),
        )
        analytics.capture(
            "onboarding_plan_written",
            distinct_id=run.project_id,
            project_id=run.project_id,
            properties={"run_id": str(run.id), "systems": len(offered), "view": view},
        )
        return await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=plan["canonical_commit_sha"],
            artifact_ref=plan["artifact_ref"],
            artifact_path=plan["artifact_path"],
            # Tin's own words to the founder, and nothing else: the dashboard shows this
            # explanation to them, and the agent quotes it as given. The plan's system
            # count is in the progress summary, and the agent's next steps in get_started.
            summary=view
            or (
                f"The plan is ready: {len(offered)} systems Tin can run, in "
                f"{plan['artifact_path']}."
            ),
        )

    @activity.defn
    async def growth_onboarding_approval(self, run_id: str) -> None:
        await self.db.record_human_review(
            run_id=UUID(run_id),
            decision="approved",
            summary="You picked what Tin takes on; Tin is setting it up.",
        )
        run = await self.db.get_run(UUID(run_id))
        analytics.capture(
            "onboarding_approved",
            distinct_id=run.project_id,
            project_id=run.project_id,
            properties={"run_id": str(run.id)},
        )

    async def _current_plan_text(self, project):
        return await current_plan_text(storage=self.storage, project=project)

    @activity.defn
    async def growth_onboarding_setup(self, run_id: str) -> list[dict[str, str]]:
        try:
            return await self._setup(run_id)
        except ApplicationError:
            raise
        except Exception as exc:
            # Runtime exceptions can contain SQL/provider payloads. History only needs
            # a safe retry signal; durable receipts determine which effects to reconcile.
            raise ApplicationError(
                f"Onboarding setup interrupted ({type(exc).__name__}); "
                "retry will reconcile saved effects.",
                type="onboarding_setup_interrupted",
            ) from None

    async def _setup(self, run_id: str) -> list[dict[str, str]]:
        run = await self.active(run_id)
        prepared = await self.saved(run_id, "prepare")
        if not prepared:
            raise ApplicationError("Onboarding preparation is unavailable.", non_retryable=True)
        key = f"onboarding:{run_id}:setup"
        async with (
            self.db.effect_lock(f"onboarding:{run_id}:control", KEY) as (control_conn, _),
            self.db.effect_lock(key, KEY, conn=control_conn) as (conn, receipt),
        ):
            if receipt and receipt.status == "completed":
                return child_handles(receipt.result["actions"])
            approved = await self.saved(run_id, "approved_plan")
            if not approved:
                raise ApplicationError(
                    (
                        "This run has no saved approval snapshot. Start a new onboarding "
                        "run and approve its plan before setup."
                    ),
                    type="onboarding_approval_missing",
                    non_retryable=True,
                )
            text, head = approved["text"], approved["revision"]
            project = await self.db.get_project(run.project_id)
            picked, offered = plan_picks(text)
            systems = chosen_systems(picked, offered)
            block = plan_block(text)
            if not systems or block is None:
                problem = (
                    f"No system is ticked in {PLAN_PATH}"
                    if not systems
                    else f"{PLAN_PATH} has no {PLAN_BLOCK} block"
                )
                await self.db.project_run_progress(
                    run_id=run.id,
                    mode="steps",
                    current=2,
                    total=3,
                    step="setup",
                    summary=f"{problem}; nothing was set up.",
                )
                raise ApplicationError(
                    f"{problem} (offered: {', '.join(offered) or 'none'}).",
                    type="onboarding_no_systems",
                    non_retryable=True,
                )
            actions = picked_actions(block, systems)
            connections = plan_connections(text)
            control = plan_control(text)
            state = await onboarding_tin_state(
                storage=self.storage,
                database=self.db,
                settings=self.settings,
                project_id=run.project_id,
            )
            live = {row["key"]: row for row in state["workflows"]}
            timezone = str(run.input.get("timezone") or "UTC")
            outcomes: list[dict] = []
            for index, action in enumerate(actions):
                action_key = f"onboarding:{run_id}:action:{index}"
                saved_action = await self.db.get_effect(action_key, conn=conn)
                if saved_action and saved_action.status == "completed":
                    outcomes.append(saved_action.result)
                    continue
                outcome = dict(action)
                row = live.get(action["key"])
                declined = [
                    provider
                    for provider in (row["requires_integrations"] if row else [])
                    if connections.get(provider, {}).get("state") == "declined"
                ]
                pending_action = await self.db.get_effect(f"{action_key}:enact", conn=conn)
                if pending_action:
                    outcome.update(
                        await self._enact(run, prepared, action, timezone, index, conn=conn)
                    )
                elif row is None:
                    outcome.update(status="skipped", reason="Not a workflow Tin offers.")
                elif declined:
                    outcome.update(
                        status="declined",
                        provider=declined[0],
                        note=connections[declined[0]]["note"],
                        reason=f"{declined[0]} not connected by the founder's choice.",
                    )
                elif not row["runnable"]:
                    outcome.update(
                        status="blocked",
                        reason=row["reason"],
                        unblock=(row.get("unblock") or {}).get("kind"),
                    )
                elif row.get("kind") == "task":
                    outcome.update(
                        status="skipped",
                        reason=(
                            "Project tasks are one-off requests and cannot be installed by "
                            "onboarding."
                        ),
                    )
                else:
                    try:
                        outcome.update(
                            await self._enact(run, prepared, action, timezone, index, conn=conn)
                        )
                    except (WorkflowInputError, PrerequisiteError) as exc:
                        # One action failing must not take the whole setup down: the
                        # founder gets the rest, and this one shows as left out with why.
                        outcome.update(status="skipped", reason=str(exc)[:240])
                await self.db.start_effect(conn, execution_key=action_key, operation=KEY)
                await self.db.complete_effect(conn, execution_key=action_key, result=outcome)
                outcomes.append(outcome)
            delivery = await self._configure_delivery(run, outcomes, head)
            result = {
                "setup_status": "partial"
                if any(
                    row.get("status") in {"blocked", "skipped", "declined"}
                    or row.get("first_run_status") == "blocked"
                    or row.get("delivery_error")
                    for row in outcomes
                )
                else "complete",
                "plan_revision": head,
                "systems": systems,
                "offered": offered,
                "details": systems_details(block, systems),
                "business": project.name,
                "timezone": timezone,
                "delivery": delivery,
                "connections": connections,
                "control": control,
                "actions": outcomes,
                "links": ui_links(dashboard_url(self.settings), run.project_id),
            }
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=result)
            analytics.capture(
                "onboarding_set_up",
                distinct_id=run.project_id,
                project_id=run.project_id,
                properties={
                    "run_id": str(run.id),
                    "systems": systems,
                    "control": control,
                    "connections": connections,
                    "outcomes": {
                        status: sum(1 for row in outcomes if row.get("status") == status)
                        for status in ("started", "scheduled", "declined", "blocked")
                    },
                    "workflows": [row.get("key") for row in outcomes],
                },
            )
        await self.db.project_run_progress(
            run_id=run.id,
            mode="steps",
            current=2,
            total=3,
            step="setup",
            summary=(
                f"Set up {sum(row.get('status') in {'started', 'scheduled'} for row in outcomes)} "
                f"workflows for {', '.join(systems) or 'no system'}. "
                + (
                    "Setup is partial; inspect incomplete_setup for what needs attention."
                    if result["setup_status"] == "partial"
                    else "Setup is complete."
                )
            ),
        )
        return child_handles(outcomes)

    async def _enact(self, run, prepared, action, timezone, index, *, conn):
        key = f"onboarding:{run.id}:action:{index}:enact"
        async with self.db.effect_lock(key, KEY, conn=conn) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            template = await self.db.get_registry_workflow(action["key"])
            if receipt and receipt.result:
                intent = receipt.result
                template = replace(
                    template,
                    definition=intent["definition"],
                    current_commit_sha=intent["definition_revision"],
                )
            else:
                intent = {
                    "definition": template.definition,
                    "definition_revision": template.current_commit_sha,
                }
            definition = template.definition
            coerced, notes = repaired_action_inputs(
                action, definition["input_schema"], run.input or {}
            )
            try:
                inputs = normalize_workflow_inputs(
                    schema=definition["input_schema"],
                    project_id=run.project_id,
                    inputs=coerced,
                )
                schedule = None
                if action["mode"] != "once":
                    schedule = WorkflowSchedule(
                        cadence=action["mode"],
                        weekdays=list(action["weekdays"]) if action["mode"] == "weekly" else [],
                        local_time=action["local_time"],
                        timezone=timezone,
                    )
                    ensure_schedule_allowed(definition, schedule)
                    if self.temporal is None:
                        raise ValueError("Scheduling is unavailable on this worker.")
            except ValueError as exc:
                return {"status": "blocked", "reason": str(exc)[:240]}
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.save_effect_progress(conn, execution_key=key, result=intent)
            configured_id = None
            if schedule is not None:
                configured = await self.db.create_project_workflow(
                    project_id=run.project_id,
                    workflow_id=template.id,
                    definition_commit_sha=template.current_commit_sha,
                    name=template.title,
                    inputs=inputs,
                    input_schema=definition["input_schema"],
                    schedule=schedule.model_dump(mode="json"),
                    request_id=uuid5(run.id, f"setup:{index}:{action['key']}"),
                    created_by_clerk_user_id=run.started_by_clerk_user_id,
                    pinned_definition=definition,
                )
                configured_id = configured.id
                if "schedule" not in intent:
                    schedule_id = await TemporalScheduleService(
                        client=self.temporal, settings=self.settings
                    ).create(
                        project_workflow_id=str(configured.id),
                        schedule=schedule,
                    )
                    next_run = next_run_after(schedule)
                    await self.db.project_workflow_synced(
                        project_workflow_id=configured.id,
                        temporal_schedule_id=schedule_id,
                        next_run_at=next_run,
                    )
                    intent["schedule"] = {
                        "project_workflow_id": str(configured.id),
                        "next_run_at": next_run.isoformat(),
                    }
                    intent["first_run_status"] = "pending"
                    await self.db.save_effect_progress(conn, execution_key=key, result=intent)
            try:
                child = await self._start_child(
                    run,
                    prepared,
                    step=f"setup:{index}:{action['key']}" + (":first" if schedule else ""),
                    workflow_key=action["key"],
                    inputs={k: v for k, v in inputs.items() if k != "project_id"},
                    project_workflow_id=configured_id,
                    template_override=template,
                )
            except (WorkflowInputError, PrerequisiteError) as exc:
                # These are definitive admission refusals. Transport/runtime failures remain
                # pending and retry against the same schedule and first-run identities.
                result = {"status": "blocked", "reason": str(exc)[:240]}
                if schedule:
                    result.update(
                        status="scheduled", **intent["schedule"], first_run_status="blocked"
                    )
            else:
                result = {"status": "started", **child}
                if notes:
                    result["note"] = "; ".join(notes)
                if schedule:
                    result.update(
                        status="scheduled", **intent["schedule"], first_run_status="admitted"
                    )
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    async def _configure_delivery(self, run, outcomes, head):
        """Point the scheduled content programs at GitHub when it is connected.

        Drafts otherwise stay in Tin after approval; the report says which it is.
        """
        programs = [
            row
            for row in outcomes
            if row.get("status") == "scheduled"
            and row.get("key") in CONTENT_DRAFT_KEYS
            and row.get("project_workflow_id")
        ]
        if not programs:
            return None
        connection = await self.db.get_integration_connection(
            project_id=run.project_id, provider_key="infra.github"
        )
        repository = (
            connection.configuration.get("selected_repository")
            if connection is not None and connection.status == "connected"
            else None
        )
        if not repository:
            return {"mode": "draft_only", "repository": None}
        settings = DeliverySettings(mode="github_pr", repository=repository)
        service = ContentDelivery(
            database=self.db, storage=self.storage, integrations=self.integrations
        )
        revision = head
        configured_programs = []
        for index, row in enumerate(programs):
            try:
                saved = await service.save_settings(
                    project_id=run.project_id,
                    program_id=UUID(row["project_workflow_id"]),
                    settings=settings,
                    request_id=uuid5(run.id, f"delivery:{index}:{row['key']}"),
                    expected_revision=revision,
                    actor=run.started_by_clerk_user_id,
                    client_id=None,
                )
            except Exception:  # one program failing to configure must not stop setup
                row["delivery_error"] = (
                    "Pull-request delivery could not be saved; drafts stay in Tin."
                )
                row["delivery_mode"] = "draft_only"
                continue
            revision = saved.get("revision", revision)
            row["delivery_mode"] = "github_pr"
            configured_programs.append(str(row["project_workflow_id"]))
        return {
            "mode": "github_pr" if len(configured_programs) == len(programs) else "partial",
            "repository": repository,
            "path_pattern": settings.path_pattern,
            "configured_programs": configured_programs,
        }

    async def _titles(self, setup):
        titles = {}
        for action in setup["actions"]:
            if action["key"] not in titles:
                template = await self.db.get_registry_workflow(action["key"])
                titles[action["key"]] = template.title if template else action["key"]
        return titles

    @activity.defn
    async def growth_onboarding_report(self, run_id: str) -> bool:
        run = await self.db.get_run(UUID(run_id))
        setup = await self.saved(run_id, "setup")
        if not setup:
            raise ApplicationError("Setup did not record its result.", non_retryable=True)
        content = render_report(setup, titles=await self._titles(setup))
        path = f"reports/onboarding/{run.id}/RESULT.md"
        revision = await publish_report_file(
            database=self.db,
            storage=self.storage,
            run_id=run.id,
            workflow_key=KEY,
            prefix="onboarding",
            path=path,
            content=content.encode(),
        )
        project = await self.db.get_project(run.project_id)
        # The run held for review, so it completes through the review-aware success path.
        await self.db.project_success(
            run_id=run.id,
            canonical_commit_sha=revision,
            artifact_ref=f"code.storage://{project.state_repo_id}@{revision}/{path}",
            artifact_path=path,
        )
        analytics.capture(
            "onboarding_report_written",
            distinct_id=run.project_id,
            project_id=run.project_id,
            properties={"run_id": str(run.id), "path": path},
        )
        return True

    @activity.defn
    async def growth_onboarding_failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(run_id))
        if run.status.value == "superseded":
            return  # A newer onboarding replaced it before approval; nothing failed.
        analytics.capture(
            "onboarding_failed",
            distinct_id=run.project_id,
            project_id=run.project_id,
            properties={"run_id": str(run.id)},
        )
        # Setup admits children before Temporal dispatches them. If setup exhausted its
        # retries, none of those children was dispatched by this parent. Close the pending
        # projections so an interrupted partial setup cannot strand an active run forever.
        async with self.db.effect_lock(f"onboarding:{run.id}:control", KEY) as (conn, _):
            setup = await self.saved(str(run.id), "setup")
            if setup is None:
                pending = await conn.fetch(
                    "SELECT id FROM workflow_runs WHERE project_id=$1 AND status='pending' "
                    "AND starts_with(start_idempotency_key, $2)",
                    run.project_id,
                    f"onboarding:{run.id}:setup:",
                )
                for child in pending:
                    await self.db.project_failure(
                        run_id=child["id"],
                        error_message="Onboarding setup failed before this run was dispatched.",
                    )
        await self.db.project_failure(
            run_id=UUID(run_id),
            error_message=(
                "Growth onboarding could not finish. A finished plan stays in project Files; "
                "Check My system for schedules already created; they keep their current state. "
                "Prepared first runs were not dispatched if setup did not complete."
            ),
        )
