"""Real SQL receipts and projection with a synthetic X provider and code.storage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationUpstreamError,
)
from tin_lite.project_media import validate_media
from tin_lite.x_posts import KEY, save_effect
from tin_lite.x_publish_activities import XPublishActivities

ACTOR = "user_XPublisher123"


class SyntheticX:
    def __init__(self):
        self.id = uuid4()
        self.calls = 0
        self.uncertain = False
        self.connected = True
        self.media_calls = []
        self.fail_processing = False
        self.status_error_once = False
        self.uncertain_append = False
        self.media_ids = None

    async def connection(self, project_id, capability):
        assert project_id and capability in {"x.posts.publish", "x.media.upload"}
        if not self.connected:
            raise IntegrationAuthorizationError("X disconnected")
        return SimpleNamespace(id=self.id, external_account_id="12345")

    async def create_post(self, connection, *, text, media_ids):
        assert connection.id == self.id and text == "Exact approved text"
        assert media_ids == self.media_ids
        self.calls += 1
        if self.uncertain:
            raise IntegrationDeliveryUnknownError("Synthetic lost response")
        return {
            "post_id": "9001",
            "text": text,
            "url": "https://x.com/i/web/status/9001",
        }

    async def upload_image(self, connection, *, content, media_type):
        assert connection.id == self.id and content and media_type == "image/png"
        self.media_calls.append("image")
        return {"media_id": "300", "expires_after_secs": 3600}

    async def set_alt_text(self, connection, *, media_id, text):
        assert connection.id == self.id and media_id == "300" and text == "Product screenshot"
        self.media_calls.append("alt")
        return {"media_id": media_id}

    async def initialize_video(self, connection, *, total_bytes):
        assert connection.id == self.id and total_bytes > 0
        self.media_calls.append("initialize")
        return {"media_id": "301", "expires_after_secs": 3600}

    async def append_video(self, connection, *, media_id, segment_index, content):
        assert connection.id == self.id and media_id == "301" and segment_index == 0 and content
        self.media_calls.append("append")
        if self.uncertain_append:
            raise IntegrationDeliveryUnknownError("Synthetic lost append response")
        return {"data": {"expires_at": 999999}}

    async def finalize_video(self, connection, *, media_id):
        assert connection.id == self.id and media_id == "301"
        self.media_calls.append("finalize")
        return {"data": {"id": media_id, "processing_info": {"state": "pending"}}}

    async def media_status(self, connection, *, media_id):
        assert connection.id == self.id and media_id == "301"
        self.media_calls.append("status")
        if self.status_error_once:
            self.status_error_once = False
            raise IntegrationUpstreamError("Synthetic temporary read failure")
        return {
            "data": {
                "id": media_id,
                "processing_info": {"state": "failed" if self.fail_processing else "succeeded"},
            }
        }


async def setup(publication_db, storage, x, *, attachments=None):
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
        "attachments": attachments or [],
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


def attach_fixture(storage, *, kind: str):
    filename = "image.png" if kind == "image" else "demo.mp4"
    path = f"media/{filename}"
    raw = (Path(__file__).parent / "fixtures" / "x_media" / filename).read_bytes()
    storage.repo.edit({path: raw}, message="Synthetic approved attachment")
    return {
        "type": kind,
        "path": path,
        "alt_text": "Product screenshot" if kind == "image" else "",
        **validate_media(path, raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


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


@pytest.mark.asyncio
async def test_image_upload_alt_text_and_post_use_approved_media(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    attachment = attach_fixture(storage, kind="image")
    x.media_ids = ["300"]
    run_id, approval_id = await setup(publication_db, storage, x, attachments=[attachment])
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    await activities.execute(str(run_id))
    assert x.media_calls == ["image", "alt"] and x.calls == 1
    for stage in ("upload", "alt"):
        receipt = await publication_db.get_effect(f"x:media:{approval_id}:0:1:{stage}")
        assert receipt.status == "completed"


@pytest.mark.asyncio
async def test_video_upload_resumes_after_status_read_failure(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    attachment = attach_fixture(storage, kind="video")
    x.media_ids = ["301"]
    x.status_error_once = True
    run_id, approval_id = await setup(publication_db, storage, x, attachments=[attachment])
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    with pytest.raises(IntegrationUpstreamError, match="temporary read failure"):
        await activities.execute(str(run_id))
    assert x.calls == 0
    assert x.media_calls == ["initialize", "append", "finalize", "status"]
    await activities.execute(str(run_id))
    assert x.media_calls == ["initialize", "append", "finalize", "status", "status"]
    assert x.calls == 1
    for stage in ("initialize", "append:0", "finalize"):
        assert (
            await publication_db.get_effect(f"x:media:{approval_id}:0:1:{stage}")
        ).status == "completed"


@pytest.mark.asyncio
async def test_failed_video_processing_never_posts(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    attachment = attach_fixture(storage, kind="video")
    x.fail_processing = True
    run_id, approval_id = await setup(publication_db, storage, x, attachments=[attachment])
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    with pytest.raises(ApplicationError, match="could not process"):
        await activities.execute(str(run_id))
    assert x.calls == 0
    assert await publication_db.get_effect(f"x:post:{approval_id}") is None


@pytest.mark.asyncio
async def test_uncertain_video_append_is_never_repeated(publication_db):
    storage, x = HistoryStorage(), SyntheticX()
    attachment = attach_fixture(storage, kind="video")
    x.uncertain_append = True
    run_id, approval_id = await setup(publication_db, storage, x, attachments=[attachment])
    activities = XPublishActivities(
        database=publication_db, storage=storage, integrations=SimpleNamespace(x=x)
    )
    with pytest.raises(IntegrationDeliveryUnknownError):
        await activities.execute(str(run_id))
    assert (
        await publication_db.get_effect(f"x:media:{approval_id}:0:1:append:0")
    ).status == "started"
    with pytest.raises(ApplicationError, match="uncertain outcome"):
        await activities.execute(str(run_id))
    assert x.media_calls.count("append") == 1 and x.calls == 0
