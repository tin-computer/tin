"""Project-file recipes activated into the existing workflow catalog; no new engine."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from tin_lite.domain import WorkflowStatus
from tin_lite.integrations import parse_integration_requirements
from tin_lite.procedures import (
    MAX_SANDBOX_TIMEOUT_SECONDS,
    load_pinned_codex_procedure,
    validate_codex_procedure_definition,
)
from tin_lite.project_files import safe_project_file_path
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.workflow_packages import PACKAGE_FORMAT, decode_workflow_source, package_digest
from tin_lite.workflow_prerequisites import parse_workflow_prerequisites

PRIVATE_KEY = re.compile(r"custom\.[a-z][a-z0-9_]{0,47}")
REVISION = r"^[0-9a-f]{40}$"
POLICY = "private-procedure-v1"


class PrivateWorkflowError(WorkflowInputError):
    def __init__(self, code: str, message: str, *, status: int = 422, path: str | None = None):
        super().__init__(message)
        self.code, self.status, self.path = code, status, path

    def diagnostic(self):
        return {"code": self.code, "message": str(self), "path": self.path}


def package_manifest_path(path: str) -> str:
    """Accept the package directory an agent points at, as well as its manifest."""
    value = str(path).strip().removeprefix("./").rstrip("/")
    if value.startswith("workflow_packages/") and value.count("/") == 1:
        value += "/workflow.json"
    return value


class PackageSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(pattern=r"^workflow_packages/custom\.[a-z][a-z0-9_]{0,47}/workflow\.json$")
    revision: str = Field(pattern=REVISION)


class PackageActivation(PackageSelection):
    request_id: UUID
    # Required explicit null means create; clients get the update value from discovery.
    expected_revision: str | None = Field(pattern=REVISION)


class PrivateWorkflowArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: str = Field(pattern=REVISION)


def _closed(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"unsupported {label} fields")


def validate_private_definition(definition):
    """Current private policy, shared by activation, admission and the trusted worker."""
    _closed(
        definition,
        {
            "key",
            "version",
            "title",
            "description",
            "executor",
            "kind",
            "input_schema",
            "procedure",
            "code",
            "human_review",
            "schedule_modes",
            "integration_requirements",
            "system",
            "prerequisites",
        },
        "private definition",
    )
    if not isinstance(definition.get("key"), str) or not PRIVATE_KEY.fullmatch(definition["key"]):
        raise ValueError(
            "private keys must be custom.<lowercase_name> (letters, digits, underscores)"
        )
    for key, maximum in (("title", 120), ("description", 2000), ("version", 40)):
        value = definition.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"private {key} must contain 1-{maximum} characters")
    if definition.get("kind", "workflow") != "workflow":
        raise ValueError("private packages define workflows, not tasks or uploaded executors")
    if definition.get("executor") != "workflow.code" and definition.get("schedule_modes") != [
        "on_demand"
    ]:
        raise ValueError("private pilot workflows must declare schedule_modes: [on_demand]")
    if definition.get("system") not in {
        None,
        "start-here",
        "organic-traffic",
        "cold-outreach",
        "product-qa",
        "creative-studio",
        "revenue",
    }:
        raise ValueError("choose an existing system or omit system")
    from tin_lite.workflow_packages import validate_package_input_schema

    schema = definition.get("input_schema", {})
    validate_package_input_schema(schema)
    if schema["properties"].get("project_id") != {"type": "string", "format": "uuid"}:
        raise ValueError("project_id is a Tin-bound UUID, not an authored input")
    review = definition.get("human_review")
    if review is not None:
        _closed(
            review,
            {"eligible", "reason", "summary", "defer_label", "queue_clause", "review_label"},
            "review",
        )
        if type(review.get("eligible")) is not bool or any(
            not isinstance(value, str) or len(value) > 1000
            for key, value in review.items()
            if key != "eligible"
        ):
            raise ValueError("review eligibility must be boolean and review copy bounded text")
    for prerequisite in parse_workflow_prerequisites(
        definition.get("prerequisites"), input_schema=schema
    ):
        if prerequisite.kind == "identity" or prerequisite.producer is not None:
            raise ValueError(
                "private prerequisites may only name earlier runs or ordinary project files"
            )
        if prerequisite.workflow == definition["key"]:
            raise ValueError("a private workflow cannot require itself")
    if definition.get("executor") == "workflow.code":
        from tin_lite.workflow_code import validate_code_definition

        return validate_code_definition(definition)
    if definition.get("executor") != "codex.procedure":
        raise ValueError("unsupported private executor")
    procedure = definition.get("procedure")
    _closed(
        procedure,
        {
            "prompt_path",
            "skills_path",
            "skill_files",
            "entry_skill",
            "workspace",
            "output",
            "verification",
            "project_skills",
            "sandbox",
            "services",
        },
        "private procedure",
    )
    _closed(procedure.get("sandbox"), {"profile", "egress", "timeout_seconds"}, "sandbox")
    _closed(
        procedure.get("workspace", {"kind": "project.state"}),
        {"kind", "provider_key", "capabilities", "limits"},
        "workspace",
    )
    _closed(procedure.get("verification", {}), {"commands"}, "verification")
    dependencies = procedure.get("project_skills", [])
    if not isinstance(dependencies, list):
        raise ValueError("project_skills must be a list")
    for dependency in dependencies:
        _closed(dependency, {"name", "path", "required"}, "project skill")
    output = procedure.get("output")
    _closed(
        output,
        {
            "kind",
            "path",
            "media_type",
            "max_bytes",
            "provider_key",
            "max_files",
            "receipt_path_template",
            "allow_no_change",
        },
        "private output",
    )
    spec = validate_codex_procedure_definition(definition)
    if not spec.sandbox.isolated or spec.sandbox.open_egress:
        raise ValueError("private procedures require the isolated, fenced sandbox profile")
    destination = spec.output_path or (spec.receipt_path_template or "").replace("{run_id}", "run")
    if (
        not safe_project_file_path(destination)
        or destination == "wiki/INDEX.md"
        or destination.split("/")[0] in {".tin-lite", "procedures", "registry"}
        or spec.output_media_type in {"video/mp4", "image/svg+xml"}
    ):
        raise ValueError(
            "private output must be an ordinary project text file or GitHub PR receipt"
        )
    requirements = parse_integration_requirements(definition.get("integration_requirements"))
    allowed = {
        "infra.github": {
            "contents.read",
            "contents.write",
            "pull_requests.read",
            "pull_requests.write",
        },
        "workspace.google": {"gmail.messages.read", "calendar.events.read"},
    }
    bound_providers = {service.provider_key for service in spec.services}
    for requirement in requirements:
        if requirement.provider_key in bound_providers:
            continue  # The shared binding validator checked these capabilities.
        if not requirement.required or not set(requirement.capabilities) <= allowed.get(
            requirement.provider_key, set()
        ):
            raise ValueError(
                "private integrations require supported GitHub or Workspace read capabilities"
            )
    github = next((r for r in requirements if r.provider_key == "infra.github"), None)
    needed = set(spec.workspace_capabilities)
    if spec.result_kind == "github.pull_request":
        needed |= allowed["infra.github"]
    if bool(github and github.provider_key not in bound_providers) != spec.repository_workspace or (
        needed and (not github or set(github.capabilities) != needed)
    ):
        raise ValueError(
            "GitHub requirements must match the repository workspace and result contract"
        )
    return spec


def private_execution_blocker(settings, project_id: UUID, action: str = "execution") -> str | None:
    """Say which private execution gate is closed, or None when both are open."""
    projects = getattr(settings, "private_workflow_projects", ())
    if not (getattr(settings, "private_workflows_open", False) or project_id in projects):
        return (
            f"Private {action} is not enabled for this project: it is not admitted to private "
            "workflow execution on this deployment. Ask the Tin operator to admit it."
        )
    if not getattr(settings, "e2b_isolated_template", None):
        return (
            f"Private {action} is unavailable: this deployment has no isolated runtime "
            "configured for private workflows."
        )
    return None


def private_execution_ready(settings, project_id: UUID) -> bool:
    return private_execution_blocker(settings, project_id) is None


def package_policy(definition):
    if definition.get("executor") == "workflow.code":
        from tin_lite.workflow_code import validate_code_definition

        return validate_code_definition(definition).policy
    return POLICY


def model_execution_ready(settings, definition):
    if definition.get("executor") != "workflow.code":
        return True
    from tin_lite.workflow_code import validate_code_definition

    # Paid managed reads share the metered policy but need no model key.
    return not validate_code_definition(definition).model_routes or bool(
        getattr(settings, "luna_api_key", None)
    )


def require_private_execution(settings, workflow, project_id):
    if workflow.project_id is None:
        return
    if workflow.project_id != project_id:
        raise LookupError("workflow not found")
    if blocker := private_execution_blocker(settings, project_id):
        raise PrivateWorkflowError("private_execution_unavailable", blocker, status=409)
    validate_private_definition(workflow.definition)


class PrivateWorkflows:
    def __init__(self, *, database, storage, settings):
        self.db, self.storage, self.settings = database, storage, settings

    async def project(self, project_id, actor):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=actor):
            raise LookupError("project not found")
        project = await self.db.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
        return project

    async def load(self, project, selection):
        files = {}

        async def read(*, repo_id, commit_sha, path):
            if path not in files:
                files[path] = await self.storage.read_workflow_resource(
                    repo_id=repo_id, commit_sha=commit_sha, path=path
                )
            return files[path]

        try:
            raw = await read(
                repo_id=project.state_repo_id, commit_sha=selection.revision, path=selection.path
            )
            source = decode_workflow_source(raw, definition_path=selection.path)
            if source.package_format != PACKAGE_FORMAT:
                raise ValueError("use tin-workflow-package-v1")
            validate_private_definition(source.definition)
            await self.check_prerequisite_keys(project, source.definition)
            from tin_lite.workflow_code import load_code_package

            loader = (
                load_code_package
                if source.definition["executor"] == "workflow.code"
                else load_pinned_codex_procedure
            )
            await loader(
                storage=SimpleNamespace(read_workflow_resource=read),
                repo_id=project.state_repo_id,
                commit_sha=selection.revision,
                definition_path=selection.path,
            )
            digest = package_digest(files, definition_path=selection.path)
            return source.definition, digest, sorted(files)
        except (ValueError, KeyError, TypeError) as exc:
            raise PrivateWorkflowError(
                "invalid_package", str(exc)[:400], path=selection.path
            ) from exc

    async def check_prerequisite_keys(self, project, definition):
        """Run prerequisites may name built-in or this project's own active workflows."""
        wanted = {
            item.workflow
            for item in parse_workflow_prerequisites(
                definition.get("prerequisites"), input_schema=definition.get("input_schema")
            )
            if item.kind == "run" and item.workflow
        }
        if not wanted:
            return
        known = {
            workflow.key
            for workflow in await self.db.list_workflows(project_id=project.id)
            if workflow.status == WorkflowStatus.ACTIVE
            and workflow.project_id in (None, project.id)
        }
        missing = sorted(wanted - known)
        if missing:
            raise ValueError(f"prerequisite names an unknown workflow: {missing[0]}")

    async def validate(self, *, project_id, actor, selection):
        project = await self.project(project_id, actor)
        try:
            definition, digest, paths = await self.load(project, selection)
        except PrivateWorkflowError as exc:
            return {"valid": False, "diagnostics": [exc.diagnostic()]}
        return {
            "valid": True,
            "diagnostics": [],
            "key": definition["key"],
            "title": definition["title"],
            "revision": selection.revision,
            "path": selection.path,
            "package_digest": digest,
            "files": paths,
            "policy": package_policy(definition),
            "runtime_available": private_execution_ready(self.settings, project_id)
            and model_execution_ready(self.settings, definition),
            "integration_requirements": definition.get("integration_requirements", []),
            "prerequisites": definition.get("prerequisites", []),
            "output": definition.get("code", definition.get("procedure"))["output"],
        }

    async def activate(self, *, project_id, actor, client_id, selection):
        project = await self.project(project_id, actor)
        if blocker := private_execution_blocker(self.settings, project_id, "activation"):
            raise PrivateWorkflowError("private_execution_unavailable", blocker, status=409)
        request = {**selection.model_dump(mode="json"), "actor": actor, "client_id": client_id}
        key = f"private-workflow:{project_id}:{selection.request_id}"
        async with self.db.effect_lock(key, "private_workflow_activate") as (conn, existing):
            if existing is not None:
                return self.replay(existing, request)
            definition, digest, paths = await self.load(project, selection)
            if not model_execution_ready(self.settings, definition):
                raise PrivateWorkflowError(
                    "model_unavailable", "The declared model service is unavailable.", status=409
                )
            async with conn.transaction():
                await self.lock(conn, project_id, actor)
                previous = await conn.fetchrow(
                    "SELECT * FROM workflows WHERE project_id=$1 AND key=$2 FOR UPDATE",
                    project_id,
                    definition["key"],
                )
                if (
                    previous["current_commit_sha"] if previous else None
                ) != selection.expected_revision:
                    raise PrivateWorkflowError(
                        "activation_conflict",
                        "The active recipe changed. Inspect it and retry with that revision.",
                        status=409,
                    )
                if previous and (
                    previous["definition_repo_id"] != project.state_repo_id
                    or previous["definition_path"] != selection.path
                    or previous["executor"] != definition["executor"]
                ):
                    raise PrivateWorkflowError(
                        "source_conflict",
                        "An activated workflow cannot change its source location or executor.",
                        status=409,
                    )
                from tin_lite.luna import _tool_name

                candidates = await conn.fetch(
                    "SELECT id,key FROM workflows WHERE project_id IS NULL OR project_id=$1",
                    project_id,
                )
                if any(
                    _tool_name(row["key"]) == _tool_name(definition["key"])
                    and (not previous or row["id"] != previous["id"])
                    for row in candidates
                ):
                    raise PrivateWorkflowError(
                        "key_conflict",
                        "This key collides with an available workflow. Choose another custom name.",
                        status=409,
                    )
                workflow_id = previous["id"] if previous else uuid4()
                await conn.execute(
                    """
                    INSERT INTO workflows (id,project_id,key,title,description,executor,
                        definition_repo_id,
                        definition_path,current_commit_sha,version_label,definition,status)
                    VALUES ($1,$2,$3,$4,$5,$11,$6,$7,$8,$9,$10::jsonb,'active')
                    ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title,
                        description=EXCLUDED.description,
                        current_commit_sha=EXCLUDED.current_commit_sha,
                        version_label=EXCLUDED.version_label,
                        definition=EXCLUDED.definition,status='active',updated_at=now()
                    WHERE workflows.project_id=EXCLUDED.project_id
                    """,
                    workflow_id,
                    project_id,
                    definition["key"],
                    definition["title"],
                    definition["description"],
                    project.state_repo_id,
                    selection.path,
                    selection.revision,
                    definition["version"],
                    json.dumps(definition),
                    definition["executor"],
                )
                result = {
                    "workflow_id": str(workflow_id),
                    "key": definition["key"],
                    "status": "active",
                    "revision": selection.revision,
                    "path": selection.path,
                    "package_digest": digest,
                    "files": paths,
                    "policy": package_policy(definition),
                }
                await self.finish(
                    conn,
                    key=key,
                    operation="private_workflow_activate",
                    request=request,
                    result=result,
                    project_id=project_id,
                    actor=actor,
                    client_id=client_id,
                    title=definition["title"],
                    verb="activated",
                )
            return {**result, "replayed": False}

    async def archive(self, *, project_id, workflow_id, actor, client_id, selection):
        await self.project(project_id, actor)
        request = {
            **selection.model_dump(mode="json"),
            "workflow_id": str(workflow_id),
            "actor": actor,
            "client_id": client_id,
        }
        key = f"private-workflow:{project_id}:{selection.request_id}"
        async with self.db.effect_lock(key, "private_workflow_archive") as (conn, existing):
            if existing is not None:
                return self.replay(existing, request)
            async with conn.transaction():
                await self.lock(conn, project_id, actor)
                row = await conn.fetchrow(
                    "SELECT * FROM workflows WHERE id=$1 AND project_id=$2 FOR UPDATE",
                    workflow_id,
                    project_id,
                )
                if row is None:
                    raise LookupError("private workflow not found")
                if row["current_commit_sha"] != selection.expected_revision:
                    raise PrivateWorkflowError(
                        "activation_conflict",
                        "The active recipe changed. Inspect it before archiving.",
                        status=409,
                    )
                await conn.execute(
                    "UPDATE workflows SET status='archived',updated_at=now() "
                    "WHERE id=$1 AND project_id=$2",
                    workflow_id,
                    project_id,
                )
                result = {
                    "workflow_id": str(workflow_id),
                    "key": row["key"],
                    "status": "archived",
                    "revision": row["current_commit_sha"],
                    "path": row["definition_path"],
                }
                await self.finish(
                    conn,
                    key=key,
                    operation="private_workflow_archive",
                    request=request,
                    result=result,
                    project_id=project_id,
                    actor=actor,
                    client_id=client_id,
                    title=row["title"],
                    verb="archived",
                )
            return {**result, "replayed": False}

    async def lock(self, conn, project_id, actor):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"private-workflows:{project_id}",
        )
        if not await conn.fetchval(
            "SELECT true FROM project_memberships "
            "WHERE project_id=$1 AND clerk_user_id=$2 FOR SHARE",
            project_id,
            actor,
        ):
            raise LookupError("project not found")

    @staticmethod
    def replay(existing, request):
        if existing.status != "completed" or (existing.result or {}).get("request") != request:
            raise PrivateWorkflowError(
                "request_conflict",
                "This request ID belongs to a different operation or caller.",
                status=409,
            )
        return {**existing.result["response"], "replayed": True}

    async def finish(
        self, conn, *, key, operation, request, result, project_id, actor, client_id, title, verb
    ):
        await self.db.start_effect(conn, execution_key=key, operation=operation)
        await conn.execute(
            """
            INSERT INTO activity_events
                (project_id,run_id,event_type,details,summary,audience,dedupe_key)
            VALUES ($1,NULL,$2,$3::jsonb,$4,'product',$5)
            """,
            project_id,
            f"private_workflow_{verb}",
            json.dumps(
                {
                    "kind": "your_edits",
                    "actor_clerk_user_id": actor,
                    "oauth_client_id": client_id,
                    "workflow_id": result["workflow_id"],
                    "workflow_key": result["key"],
                    "workflow_title": title,
                    "definition_revision": result["revision"],
                }
            ),
            f"{title} {verb} for this project.",
            key,
        )
        await self.db.complete_effect(
            conn, execution_key=key, result={"request": request, "response": result}
        )


def workflow_source_view(workflow, settings):
    private = workflow.project_id is not None
    active = workflow.status == WorkflowStatus.ACTIVE
    available = not private or private_execution_ready(settings, workflow.project_id)
    return {
        "scope": "project" if private else "builtin",
        "definition_revision": workflow.current_commit_sha,
        "source": {"path": workflow.definition_path, "revision": workflow.current_commit_sha},
        "runtime_available": available,
        "allowed_actions": (["start", "save"] if active and available else [])
        + (["activate"] if private and available else [])
        + (["archive"] if private and active else []),
    }


SERVICE_ERRORS_GUIDE = (
    "A failed call carries Tin's message. When the provider answered and refused, the "
    "message ends with what it said (for example a HogQL error), and provider_error holds "
    "{provider, status, type, code, message}: the message redacted (credentials, tokens, "
    "the person part of email addresses) and cut to 1500 characters. Other failures keep "
    "Tin's generic message."
)


def authoring_guide(*, settings, project_id):
    from tin_lite.code_project_files import MAX_FILE_BYTES as MAX_READ_BYTES
    from tin_lite.workflow_code import (
        MAX_MODEL_CALLS,
        MAX_OUTPUT_BYTES,
        MAX_ROUTE_CALLS,
        MAX_ROUTE_INPUT_BYTES,
        MAX_ROUTE_OUTPUT_TOKENS,
        MAX_TIMEOUT_SECONDS,
        MODEL_TARGETS,
        RUN_MODEL_INPUT_WARNING_BYTES,
        example_files,
    )
    from tin_lite.workflow_creator import creator_files
    from tin_lite.workflow_services import MAX_SERVICE_CALLS, MAX_SERVICE_RESPONSE_BYTES

    key = "custom.research_digest"
    root = f"workflow_packages/{key}"
    manifest = {
        "package_format": PACKAGE_FORMAT,
        "definition": {
            "key": key,
            "title": "Prepare a research digest",
            "version": "1.0.0",
            "description": "Read project evidence and write one concise, source-labelled digest.",
            "executor": "codex.procedure",
            "schedule_modes": ["on_demand"],
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_id": {"type": "string", "format": "uuid"},
                    "brief": {
                        "type": "string",
                        "title": "What to summarize",
                        "minLength": 1,
                        "maxLength": 2000,
                    },
                },
                "required": ["project_id", "brief"],
            },
            "procedure": {
                "prompt_path": "PROMPT.md",
                "skills_path": "skills",
                "entry_skill": "research-digest",
                "skill_files": ["skills/research-digest/SKILL.md"],
                "sandbox": {"profile": "isolated", "egress": "fenced", "timeout_seconds": 900},
                "workspace": {"kind": "project.state"},
                "output": {
                    "kind": "project.artifact",
                    "path": "reports/custom/RESEARCH_DIGEST.md",
                    "media_type": "text/markdown",
                    "max_bytes": 64000,
                },
            },
        },
    }
    return {
        "package_format": PACKAGE_FORMAT,
        "policy": POLICY,
        "runtime_available": private_execution_ready(settings, project_id),
        "steps": [
            "Edit the example for the user's intended workflow. "
            "Use an unused custom key; its folder must match.",
            "Commit via commit_project_changes with current project HEAD and a stable request_id. "
            "File writes do not activate or start anything.",
            "Call validate_workflow_package with the manifest path and returned commit revision. "
            "Fix diagnostics before activation.",
            "Call activate_workflow_package with that exact revision and a new request_id. "
            "expected_revision=null means create; use the active revision for update.",
            "Refresh list_workflows. Call start_workflow with its UUID, project_id and inputs; "
            "do not put project_id inside inputs.",
            "Call get_code_workflow_setup for code input, connection and cost readiness. "
            "Optionally create_project_workflow to save inputs and an eligible code schedule; "
            "a saved schedule also runs once right away. "
            "Saves choose the active definition automatically; existing saves/runs do not move.",
            "Code definitions may opt into daily/weekly schedule_modes alongside on_demand. "
            "Schedules use local time/timezone, optional start_at/end_at and selected weekdays. "
            "Update future inputs with update_project_workflow; use set_project_workflow_paused "
            "or archive_project_workflow to stop future occurrences without deleting history. "
            "Code-only compute is included at zero credits; managed model schedules require "
            "credits and an existing standing schedule spending limit. No quote approval step.",
            "Use get_workflow (UUID or key) to inspect, or archive_private_workflow to retire. "
            "Reactivation is explicit; running work/results are not deleted.",
            "Declare prerequisites so agents are told what to run first; required ones block "
            "start_workflow with a prerequisite_missing diagnostic, recommended ones return "
            "advisories.",
        ],
        "public_contribution": (
            "To offer a workflow for Tin's public catalog, the pull request author must be the "
            "Tin user who ran it here: GitHub connected on this business project (not the "
            "personal one), Start here completed, and a succeeded run of the custom.* copy of "
            "the exact package. Put 'Tin run ID: <uuid>' in the pull request; the repository "
            "gate closes workflow pull requests it cannot verify. See "
            "docs/contributing-workflows.md in the tin repository."
        ),
        "limits": {
            "files": 64,
            "manifest_bytes": 512000,
            "prompt_bytes": 32000,
            "skill_file_bytes": 64000,
            "prompt_and_skill_bytes": 128000,
            "max_timeout_seconds": MAX_SANDBOX_TIMEOUT_SECONDS,
            "schedule_modes": {
                "codex.procedure": ["on_demand"],
                "workflow.code": ["on_demand", "daily", "weekly", "monthly"],
            },
        },
        "capabilities": {
            "results": [
                "project.artifact (one bounded UTF-8 file)",
                "github.pull_request (unmerged, verified through the connected-project gateway)",
            ],
            "integrations": {
                "custom.api.<name>": ["http.read", "http.write"],
                "analytics.gsc": ["sites.list", "search_analytics.read"],
                "infra.github": [
                    "contents.read",
                    "contents.write",
                    "pull_requests.read",
                    "pull_requests.write",
                ],
                "workspace.google": ["gmail.messages.read", "calendar.events.read"],
                "payments.stripe": [
                    "subscriptions.read",
                    "customers.read",
                    "invoices.read",
                    "prices.read",
                    "charges.read",
                ],
                "analytics.posthog": ["query.read", "definitions.read", "insights.read"],
            },
            "prerequisites": {
                "levels": ["required", "recommended"],
                "kinds": {
                    "run": "A succeeded, published run of a built-in or your own custom.* "
                    "workflow in this project; optional match scopes (product_host, "
                    "site_origin, market) or via_input naming the input that holds its run id.",
                    "artifact": "An ordinary project file at HEAD; {input} placeholders are "
                    "filled from inputs, section is only for wiki/INDEX.md headings.",
                },
                "example": [
                    {
                        "kind": "run",
                        "workflow": "organic.audit",
                        "match": ["site_origin"],
                        "level": "required",
                        "reason": "The digest cites the newest audit of the same site.",
                    }
                ],
            },
            "run_id": "A run knows its own Tin run ID. workflow.code reads ctx['run_id']; a "
            "codex.procedure's brief ends with a RUN CONTEXT line naming it, and its commands "
            "read the same value from TIN_RUN_ID. Use it where the output names its run.",
            "notes": "Declare required capabilities; connections alone grant a procedure nothing. "
            "Repository verification runs as the credential-free worker. Project context selection "
            "is not a narrower read permission. Existing PR overlap and result bounds apply.",
            "not_in_pilot": [
                "uploaded trusted executors (use workflow.code for isolated Python)",
                "recursive workflow starts",
                "browser/Studio",
                "test identities",
                "managed memory sections",
                "private Codex procedure schedules",
            ],
        },
        "creator_files": creator_files(),
        "qualification": {
            "cases_path": "workflow_evals/<workflow_key>/qualification.json",
            "creator": (
                "Activate creator_files as custom.workflow_create, then start it "
                "with a brief. It returns reports/WORKFLOW_CANDIDATE.json."
            ),
            "inspect": (
                "inspect_workflow_candidate returns independently checked proposed "
                "file changes. Commit them through commit_project_changes after "
                "review."
            ),
            "check": (
                "qualify_workflow_package validates a pinned package and cases "
                "without execution. Supply {case_id, run_id} references to evaluate "
                "finished runs and measure model usage."
            ),
            "live": (
                "Use normal explicit activation and budgeted starts for authorized "
                "live cases. Do not automatically start candidate code, call "
                "providers or publish the result."
            ),
        },
        "code_example_files": example_files(),
        "model_example_files": example_files("custom.order_classification", model_steps=True),
        "connection_example_files": example_files("custom.connected_accounts", connections=True),
        "procedure_services": {
            "bindings": "procedure.services uses the same bindings and limits as code.services. "
            "Declare matching required integration_requirements. Provider secrets stay in Tin.",
            "tools": [
                "request_service(service, step, path, method, params, body)",
                "call_service(service, step, operation, arguments)",
            ],
            "limits": f"At most four aliases and {MAX_SERVICE_CALLS} requests total; 16 KB "
            f"requests and 1024-{MAX_SERVICE_RESPONSE_BYTES} byte responses. The procedure "
            "keeps its own bounded runtime.",
            "recovery": "Reuse a step only for the identical request. Completed responses replay; "
            "uncertain requests cannot be retried under a new step.",
            "compatibility": "Fenced default/isolated profiles; private procedures stay isolated "
            "and on demand. Bind every non-workspace integration. Bound Google reads use "
            "call_service; no browser, Studio or test-identity combinations.",
            "costs": "Codex uses existing model pricing. Connected-provider costs are separate "
            "and unknown unless independently verified; call limits are not dollar ceilings.",
            "managed": "managed.pagespeed and managed.podscan (free) work through call_service. "
            "Paid managed "
            "services such as managed.dataforseo are for workflow.code packages only.",
            "errors": SERVICE_ERRORS_GUIDE
            + " A call_service or request_service tool error is JSON with code, message "
            "and provider_error.",
        },
        "code_contract": {
            "executor": "workflow.code",
            "code_only_policy": "bounded-code-v1",
            "model_policy": "managed-code-model-v1",
            "timeout_seconds": f"1-{MAX_TIMEOUT_SECONDS}; the package's wall-clock window, "
            "which also bounds each model call's wait",
            "network": "none",
            "credits": "Code-only compute is included. Declared model calls and paid managed "
            "reads (managed.dataforseo) use metered credits.",
            "result": "Return exactly {path, content}; one declared UTF-8 artifact of at most "
            f"{MAX_OUTPUT_BYTES} bytes (output.max_bytes).",
            "authoring": "Export run(ctx, inputs); ctx has run_id and created_at. "
            "Only declared package files and the Python standard library are available.",
            "services": {
                "setup": "prepare_project_connection opens secure setup. "
                "Never put secrets in MCP or project files.",
                "bindings": "code.services maps a name to provider_key, max_calls "
                f"(1-{MAX_SERVICE_CALLS}) and max_response_bytes "
                f"(1024-{MAX_SERVICE_RESPONSE_BYTES}). Declare matching required "
                f"integration_requirements; at most {MAX_SERVICE_CALLS} calls total. A "
                "founder-connected provider may be required: false; ctx['connections'] then "
                "gives its state for the run (connected, not_connected or needs_attention), and "
                "calls only work when connected.",
                "provider_cost": "An optional provider_cost on a service binding has "
                "estimated_usd (nonnegative decimal string per run), basis (assumptions, "
                "up to 400 characters), and pricing_url (HTTPS). Use verified provider "
                "pricing and the workflow's usage bounds; omit when unknown. This is an "
                "advisory creator estimate, not a Tin credit charge or a provider spending cap.",
                "custom_api": "await ctx.services.request(service=..., step=..., path=..., "
                "method='GET', params={}, body=None). Custom API bindings use custom.api.<name> "
                "and http.read/http.write capabilities. Connection methods must also permit it.",
                "registered_adapters": "await ctx.services.call(service=..., step=..., "
                "operation=..., arguments={}). Supports GSC sites.list/search_analytics.read, "
                "GitHub repositories.list, Google Workspace gmail.messages.search/"
                "gmail.thread.read/calendar.events.list. Existing connections are reused. "
                "GSC search_analytics.read accepts start_row and dimension_filters and returns "
                "the leading rows that fit max_response_bytes, adding truncated and "
                "next_start_row when more may exist. Stripe (payments.stripe) "
                "subscriptions.list/customers.list/invoices.list/prices.list/charges.list "
                "return projected {records, has_more, truncated, next_cursor}; pass "
                "next_cursor as cursor in a new step to continue. PostHog (analytics.posthog) "
                "reads the founder's selected project: event_definitions.list/"
                "property_definitions.list/insights.list page the same way, and query.hogql "
                "takes {query, name} where query is one SELECT ending in LIMIT <= 1000 with "
                "no OFFSET (page with a WHERE on timestamp) and returns {columns, types, rows, "
                "has_more, truncated}. Stripe customer records include full email and name. "
                "Arguments, fields and errors: docs/stripe-and-posthog-connections.md in "
                "Tin's source.",
                "managed": "Tin holds the key; there is nothing to connect. Declare the provider "
                "in integration_requirements with its capabilities and bind it in code.services. "
                "managed.pagespeed (pagespeed.read, crux.read; $0): pagespeed.run {url, "
                "strategy: mobile|desktop, categories} returns scores, lab lcp_ms/cls/tbt_ms and "
                "field data or field_status no_field_data; a run over 55 s returns status "
                "timed_out. crux.query {origin|url, form_factor} returns p75 and good/poor "
                "shares, or status no_field_data. managed.dataforseo (serp.read, keywords.read, "
                "backlinks.read; charged per call at DataForSEO's reported cost, $0.05 "
                "reserved per call): serp.organic {keyword, location_code, language_code, "
                "device, depth}, keywords.ideas {keywords, location_code, language_code, limit, "
                "offset}, keywords.overview {keywords, location_code, language_code}, "
                "backlinks.summary {target, include_subdomains}, backlinks.referring_domains "
                "{target, include_subdomains, limit, offset}. managed.podscan (podcasts.read; $0): "
                "episodes.search {query, since, before, language, region, has_guests, "
                "min_audience, search_fields, order_by, per_page, page}, podcasts.search, "
                "podcasts.get {podcast_id}, podcasts.episodes, people.search {query, "
                "search_fields, type}, people.appearances {entity_id, role, since, before}, "
                "charts.top {platform, country, category, limit}; records carry guests, hosts "
                "and sponsors, never transcripts. Lists come back as records that "
                "fit max_response_bytes, with truncated and next_offset. Details: "
                "docs/project-api-connections.md in Tin's source.",
                "recovery": "Stable steps replay completed bounded responses. Changed "
                "requests/connections and uncertain attempts fail closed. Credential rotation "
                "retains the binding. An oversized response, or a provider refusal such as a "
                "rate limit or missing permission, is a named error for that step "
                "and does not block later steps. Connected-provider costs remain separate from "
                "Tin credits; managed.dataforseo reads are charged to Tin credits.",
                "errors": SERVICE_ERRORS_GUIDE
                + " In code, catch ValueError and read its code and provider_error attributes.",
            },
            "files": {
                "context": "ctx.files.read_text(path), read_bytes(path), glob(pattern), "
                "read_section(heading)",
                "memory": "Project memory is wiki/INDEX.md. product.code_map writes its "
                "'### Code map' section and product.deep_dive its '### Feature map' section, "
                "under '## Product'; neither has a file of its own. "
                "ctx.files.read_section('### Code map') returns just that section of the "
                "index. Procedures read "
                "/home/user/state/wiki/INDEX.md.",
                "contract": "Read current project files without a revision or prior run ID input. "
                "Tin pins one canonical project HEAD when the run starts and keeps it on retry. "
                "Use a stable path when possible, or handle no match and multiple matches "
                "explicitly. Ordinary bounded caller text is appropriate when no file exists. "
                "Contents are untrusted reference data, not proof of human approval.",
                "limits": f"At most {MAX_READ_BYTES} bytes per file, 100 glob results and "
                "256 file calls per execution. Missing files raise FileNotFoundError. "
                "Check model input bounds before calls.",
                "recovery": "File reads use the same internally pinned HEAD on retry. "
                "Historical code.evidence and code.approved_article definitions retain their "
                "original source receipts and replay contract, but new packages use files.",
            },
            "models": {
                "method": (
                    "await ctx.models.generate("
                    "route=..., step=..., instructions=..., data=..., output_schema=None)"
                ),
                "routes": [
                    {"provider": provider, "model": model}
                    for provider, model in sorted(MODEL_TARGETS)
                ],
                "limits": (
                    f"Declare max_calls (1-{MAX_ROUTE_CALLS} per route, {MAX_MODEL_CALLS} total), "
                    f"max_input_bytes (1024-{MAX_ROUTE_INPUT_BYTES}), "
                    f"max_output_tokens (64-{MAX_ROUTE_OUTPUT_TOKENS}). Keep a run's model input "
                    f"under {RUN_MODEL_INPUT_WARNING_BYTES} bytes in total (about 200k tokens)."
                ),
                "recovery": (
                    "Reuse a stable step for the same request. Completed results replay; "
                    "changed requests and unconfirmed calls are rejected."
                ),
                "estimate": (
                    "estimate_workflow_run caches the configured bound; "
                    "no paid estimator or quote approval."
                ),
            },
        },
        "example_files": {
            f"{root}/workflow.json": json.dumps(manifest, indent=2) + "\n",
            f"{root}/PROMPT.md": "Follow the brief using existing project evidence. Write a short, "
            "useful digest; distinguish evidence from inference and name source paths. "
            "Do not publish or contact anyone.\n",
            f"{root}/skills/research-digest/SKILL.md": "---\nname: research-digest\n"
            "description: Prepare an evidence-backed project research digest\n---\n"
            "Read the brief and relevant project files. Write only the declared Markdown output. "
            "Cite project paths; explain missing evidence rather than inventing facts.\n",
        },
    }
