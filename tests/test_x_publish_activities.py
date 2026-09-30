"""Real SQL receipts and projection with a synthetic X provider and code.storage."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite.integrations import IntegrationAuthorizationError, IntegrationDeliveryUnknownError
from tin_lite.x_posts import KEY, save_effect
from tin_lite.x_publish_activities import XPublishActivities

ACTOR = "user_XPublisher123"


class SyntheticX:
    def __init__(self):
        self.id = uuid4()
        self.calls = 0
        self.uncertain = False
        self.connected = True

    async def connection(self, project_id, capability):
        assert project_id and capability == "x.posts.publish"
        if not self.connected:
            raise IntegrationAuthorizationError("X disconnected")
        return SimpleNamespace(id=self.id, external_account_id="12345")

    async def create_post(self, connection, *, text, media_ids):
        assert connection.id == self.id and text == "Exact approved text" and media_ids is None
        self.calls += 1
        if self.uncertain:
            raise IntegrationDeliveryUnknownError("Synthetic lost response")
        return {
            "post_id": "9001",
            "text": text,
            "url": "https://x.com/i/web/status/9001",
        }


async def setup(publication_db, storage, x):
    db = publication_db
    project = await db.create_project(name="X publisher", state_repo_id=storage.repo.id)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    workflow_id = await db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,'X publish',$2,'registry/workflows','x.json',$3,'1','{}')""",
            workflow_id,
            KEY,
            "d" * 40,
        )
    approval_id, run_id = uuid4(), uuid4()
    payload = {
        "project_id": str(project.id),
        "approval_id": str(approval_id),
        "actor": ACTOR,
        "revision": storage.repo.head,
        "text": "Exact approved text",
        "attachments": [],
        "connection_id": str(x.id),
        "account_id": "12345",
        "fingerprint": "f" * 64,
    }
    await save_effect(db, f"x:approved:{approval_id}", payload)
    await db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,
            temporal_workflow_id,thread_id,generation,fencing_token,status,lease_active,
            started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        project.id,
        workflow_id,
        KEY,
        "d" * 40,
        f"{KEY}:{run_id}",
        str(run_id),
        ACTOR,
        json.dumps({"project_id": str(project.id), "approval_id": str(approval_id)}),
    )
    return run_id, approval_id


@pytest.mark.asyncio
async def test_post_receipt_survives_replay_and_sql_projection(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    run_id, approval_id = await setup(publication_db, storage, x)
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    await activities.execute(str(run_id))
    await activities.execute(str(run_id))
    assert x.calls == 1
    row = await publication_db.pool.fetchrow(
        "SELECT status,artifact_path,result_summary FROM workflow_runs WHERE id=$1", run_id
    )
    assert row["status"] == "succeeded"
    assert row["artifact_path"] == f"reports/x/{run_id}.md"
    assert "https://x.com/i/web/status/9001" in row["result_summary"]
    receipt = await publication_db.get_effect(f"x:post:{approval_id}")
    assert receipt.status == "completed" and receipt.result["value"]["post_id"] == "9001"


@pytest.mark.asyncio
async def test_uncertain_post_is_never_resent_after_activity_retry(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    x.uncertain = True
    run_id, approval_id = await setup(publication_db, storage, x)
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    with pytest.raises(IntegrationDeliveryUnknownError):
        await activities.execute(str(run_id))
    assert (await publication_db.get_effect(f"x:post:{approval_id}")).status == "started"
    with pytest.raises(ApplicationError, match="Check the account"):
        await activities.execute(str(run_id))
    assert x.calls == 1
    await activities.failure(str(run_id))
    row = await publication_db.pool.fetchrow(
        "SELECT status,error_message FROM workflow_runs WHERE id=$1", run_id
    )
    assert row["status"] == "failed" and "may have published" in row["error_message"]


@pytest.mark.asyncio
async def test_confirmed_post_finishes_after_report_failure_and_disconnect(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    run_id, _ = await setup(publication_db, storage, x)
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    publish_report = activities._publish_report

    async def lost_report(*_args):
        raise RuntimeError("synthetic report outage")

    activities._publish_report = lost_report
    with pytest.raises(RuntimeError, match="report outage"):
        await activities.execute(str(run_id))
    await publication_db.pool.execute(
        "UPDATE effect_receipts SET status='started', result=NULL WHERE execution_key=$1",
        f"{run_id}:x_post",
    )
    activities._publish_report = publish_report
    x.connected = False
    await activities.execute(str(run_id))
    assert x.calls == 1
    assert (await publication_db.get_effect(f"{run_id}:x_post")).status == "completed"
    assert (await publication_db.get_run(run_id)).status.value == "succeeded"


@pytest.mark.asyncio
async def test_revoked_project_membership_stops_post_before_dispatch(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    run_id, _ = await setup(publication_db, storage, x)
    run = await publication_db.get_run(run_id)
    await publication_db.pool.execute(
        "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
        run.project_id,
        ACTOR,
    )
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    with pytest.raises(ApplicationError, match="access was removed"):
        await activities.execute(str(run_id))
    assert x.calls == 0
