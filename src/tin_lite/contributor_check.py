"""Whether a pull-request author is a Tin user who ran their workflow on a real project.

The repository's contributor gate asks this for every outside workflow pull request. The
answer carries reason codes only: no project names, run contents or account details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from tin_lite.community import private_key
from tin_lite.domain import Project, RunStatus, Workflow, WorkflowRun
from tin_lite.integrations import GITHUB_PROVIDER
from tin_lite.projects import is_personal_project, personal_project_id

# Stable codes; the repository gate maps each one to the step that fixes it.
REASONS = (
    "no_tin_account",
    "run_not_found",
    "run_not_owned",
    "personal_project",
    "github_not_connected",
    "onboarding_incomplete",
    "run_not_finished",
    "package_mismatch",
)


class ContributorDatabase(Protocol):
    async def clerk_user_for_github_id(self, github_user_id: int) -> str | None: ...

    async def get_run(self, run_id: UUID) -> WorkflowRun | None: ...

    async def get_project(self, project_id: UUID) -> Project | None: ...

    async def get_workflow(self, workflow_id: UUID) -> Workflow | None: ...

    async def get_integration_connection(self, *, project_id: UUID, provider_key: str) -> Any: ...

    async def project_setup_completed(self, project_id: UUID) -> bool: ...


@dataclass(frozen=True)
class ContributorCheck:
    verified: bool
    reasons: list[str] = field(default_factory=list)


async def check_contributor(
    database: ContributorDatabase, *, github_user_id: int, run_id: UUID, package_key: str
) -> ContributorCheck:
    clerk_user_id = await database.clerk_user_for_github_id(github_user_id)
    if clerk_user_id is None:
        return ContributorCheck(False, ["no_tin_account"])
    run = await database.get_run(run_id)
    if run is None:
        return ContributorCheck(False, ["run_not_found"])
    if run.started_by_clerk_user_id != clerk_user_id:
        # Someone else's run: say nothing more about it.
        return ContributorCheck(False, ["run_not_owned"])

    reasons: list[str] = []
    project = await database.get_project(run.project_id)
    if (
        project is None
        or is_personal_project(project.name)
        or project.id == personal_project_id(clerk_user_id)
    ):
        reasons.append("personal_project")
    github = await database.get_integration_connection(
        project_id=run.project_id, provider_key=GITHUB_PROVIDER
    )
    if github is None or github.status != "connected":
        reasons.append("github_not_connected")
    if not await database.project_setup_completed(run.project_id):
        reasons.append("onboarding_incomplete")
    if run.status != RunStatus.SUCCEEDED:
        reasons.append("run_not_finished")
    workflow = await database.get_workflow(run.workflow_id)
    if workflow is None or workflow.key != private_key(package_key):
        reasons.append("package_mismatch")
    return ContributorCheck(not reasons, reasons)
