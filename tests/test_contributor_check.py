"""The contributor gate: a PR author must be a Tin user who ran the package on a real project."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from test_procedure_publication import publication_db as publication_db
from test_procedure_publication import run_fixture

from tin_lite.api import router
from tin_lite.contributor_check import REASONS, check_contributor
from tin_lite.domain import RunStatus
from tin_lite.projects import personal_project_id

ACTOR = "user_contributor"
GITHUB_ID = 4242
PACKAGE = "outreach.speaking_shortlist"


class FakeDatabase:
    def __init__(self) -> None:
        self.identities = {GITHUB_ID: ACTOR}
        self.project = SimpleNamespace(id=uuid4(), name="Acme")
        self.workflow = SimpleNamespace(id=uuid4(), key="custom.speaking_shortlist")
        self.run = replace(
            run_fixture(),
            project_id=self.project.id,
            workflow_id=self.workflow.id,
            status=RunStatus.SUCCEEDED,
            started_by_clerk_user_id=ACTOR,
        )
        self.github = SimpleNamespace(status="connected")
        self.set_up = True

    async def clerk_user_for_github_id(self, github_user_id):
        return self.identities.get(github_user_id)

    async def get_run(self, run_id):
        return self.run if run_id == self.run.id else None

    async def get_project(self, project_id):
        return self.project if project_id == self.project.id else None

    async def get_workflow(self, workflow_id):
        return self.workflow if workflow_id == self.workflow.id else None

    async def get_integration_connection(self, *, project_id, provider_key):
        assert provider_key == "infra.github"
        return self.github if project_id == self.project.id else None

    async def project_setup_completed(self, project_id):
        return self.set_up and project_id == self.project.id


async def check(db, **overrides):
    values = {"github_user_id": GITHUB_ID, "run_id": db.run.id, "package_key": PACKAGE}
    result = await check_contributor(db, **{**values, **overrides})
    assert set(result.reasons) <= set(REASONS)
    return result


async def test_an_onboarded_author_with_a_finished_private_run_is_verified():
    result = await check(FakeDatabase())
    assert result.verified and result.reasons == []


async def test_unknown_github_account_and_other_peoples_runs_say_nothing_more():
    db = FakeDatabase()
    assert (await check(db, github_user_id=1)).reasons == ["no_tin_account"]
    assert (await check(db, run_id=uuid4())).reasons == ["run_not_found"]
    db.run = replace(db.run, started_by_clerk_user_id="user_someoneelse", status=RunStatus.FAILED)
    assert (await check(db)).reasons == ["run_not_owned"]


async def test_every_unmet_requirement_is_reported_together():
    db = FakeDatabase()
    db.project = SimpleNamespace(id=db.project.id, name="Ada's project")
    db.github = SimpleNamespace(status="needs_attention")
    db.set_up = False
    db.run = replace(db.run, status=RunStatus.RUNNING)
    result = await check(db, package_key="growth.something_else")
    assert not result.verified
    assert result.reasons == [
        "personal_project",
        "github_not_connected",
        "onboarding_incomplete",
        "run_not_finished",
        "package_mismatch",
    ]


async def test_the_bootstrap_project_counts_as_personal_even_after_a_rename():
    db = FakeDatabase()
    db.project = SimpleNamespace(id=personal_project_id(ACTOR), name="Renamed")
    db.run = replace(db.run, project_id=db.project.id)
    assert (await check(db)).reasons == ["personal_project"]


async def test_a_public_run_of_the_same_package_is_not_the_private_copy():
    db = FakeDatabase()
    db.workflow = SimpleNamespace(id=db.workflow.id, key=PACKAGE)
    assert (await check(db)).reasons == ["package_mismatch"]


def gate_app(db, token):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(contributor_check_token=token)
    app.state.runtime = SimpleNamespace(database=db)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_endpoint_is_absent_without_a_token_and_rejects_the_wrong_one():
    db = FakeDatabase()
    body = {"github_user_id": GITHUB_ID, "run_id": str(db.run.id), "package_key": PACKAGE}
    async with gate_app(db, None) as http:
        assert (await http.post("/api/contributor-checks", json=body)).status_code == 404
    async with gate_app(db, SecretStr("gate-token")) as http:
        missing = await http.post("/api/contributor-checks", json=body)
        wrong = await http.post(
            "/api/contributor-checks", json=body, headers={"Authorization": "Bearer nope"}
        )
        assert (missing.status_code, wrong.status_code) == (401, 401)
        ok = await http.post(
            "/api/contributor-checks", json=body, headers={"Authorization": "Bearer gate-token"}
        )
        bad = await http.post(
            "/api/contributor-checks",
            json={**body, "package_key": "../etc"},
            headers={"Authorization": "Bearer gate-token"},
        )
    assert ok.status_code == 200 and ok.json() == {"verified": True, "reasons": []}
    assert bad.status_code == 422


async def test_github_identity_rebinds_and_setup_completion_reads_the_receipt(publication_db):
    db = publication_db
    await db.link_github_identity(clerk_user_id="user_first", github_user_id=7, github_login="ada")
    await db.link_github_identity(clerk_user_id="user_second", github_user_id=7, github_login="ada")
    assert await db.clerk_user_for_github_id(7) == "user_second"
    await db.link_github_identity(
        clerk_user_id="user_second", github_user_id=8, github_login="ada-renamed"
    )
    assert await db.clerk_user_for_github_id(7) is None
    assert await db.clerk_user_for_github_id(8) == "user_second"

    project = await db.create_project(name="Acme", state_repo_id="repo-acme")
    run = replace(run_fixture(), project_id=project.id, executor="growth.onboarding")
    await db.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'growth.onboarding', 'Start here', 'growth.onboarding',
                   'registry/workflows', 'onboarding.json', $2, '1', '{}')
           ON CONFLICT DO NOTHING""",
        run.workflow_id,
        run.definition_commit_sha,
    )
    workflow_id = await db.pool.fetchval(
        "SELECT id FROM workflows WHERE key = 'growth.onboarding' AND project_id IS NULL"
    )
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status)
           VALUES ($1, $2, $3, 'growth.onboarding', $4, $5, $6, 1, 1, 'succeeded')""",
        run.id,
        project.id,
        workflow_id,
        run.definition_commit_sha,
        run.temporal_workflow_id,
        run.thread_id,
    )
    assert await db.project_setup_completed(project.id) is False
    await db.pool.execute(
        """INSERT INTO effect_receipts (execution_key, operation, status)
           VALUES ($1, 'onboarding_setup', 'started')""",
        f"onboarding:{run.id}:setup",
    )
    assert await db.project_setup_completed(project.id) is False
    await db.pool.execute(
        "UPDATE effect_receipts SET status = 'completed', result = '{}' WHERE execution_key = $1",
        f"onboarding:{run.id}:setup",
    )
    assert await db.project_setup_completed(project.id) is True
