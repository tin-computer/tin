"""Remembering a delivery at approval stays retryable after a stale head or a changed pick."""

import httpx
import pytest
from test_content_delivery import commit_configured, configured, publish_draft
from test_content_draft import fixture, start
from test_private_workflows import ACTOR, app
from test_procedure_publication import publication_db as publication_db

from tin_lite.project_files import StaleProjectRevisionError


async def remembered_mode(f):
    saved = await f.delivery.settings(project_id=f.project.id, program_id=f.configured.id)
    return saved["settings"]["mode"]


@pytest.mark.asyncio
async def test_remembering_a_pick_again_after_changing_it_saves_it(publication_db, monkeypatch):
    f = await commit_configured(
        await configured(await fixture(publication_db, monkeypatch)), mode="draft_only"
    )
    run, _, _ = await publish_draft(f, await start(f))
    await f.delivery.choose(run=run, mode="github_commit", remember=True, actor=ACTOR)
    await f.delivery.choose(run=run, mode="github_pr", remember=True, actor=ACTOR)
    assert await remembered_mode(f) == "github_pr"

    await f.delivery.choose(run=run, mode="github_commit", remember=True, actor=ACTOR)

    assert await remembered_mode(f) == "github_commit"


@pytest.mark.asyncio
async def test_remembering_after_a_stale_head_succeeds_on_the_next_attempt(
    publication_db, monkeypatch
):
    f = await commit_configured(
        await configured(await fixture(publication_db, monkeypatch)), mode="draft_only"
    )
    run, _, _ = await publish_draft(f, await start(f))
    real = f.delivery.settings
    stale = await real(project_id=f.project.id, program_id=f.configured.id)
    # An unrelated commit lands between reading the settings and saving them.
    f.storage.repo.head = f.storage.repo.edit({"unrelated.md": b"x"}, parent=f.storage.repo.head)
    reads = {"count": 0}

    async def stale_once(**values):
        reads["count"] += 1
        return stale if reads["count"] == 1 else await real(**values)

    f.delivery.settings = stale_once
    with pytest.raises(StaleProjectRevisionError):
        await f.delivery.choose(run=run, mode="github_commit", remember=True, actor=ACTOR)

    # The founder presses Approve again; the fresh head is a new save, not a conflict.
    await f.delivery.choose(run=run, mode="github_commit", remember=True, actor=ACTOR)

    f.delivery.settings = real
    assert await remembered_mode(f) == "github_commit"


@pytest.mark.asyncio
async def test_a_conflicting_remember_request_is_409_not_500(publication_db, monkeypatch):
    from tin_lite import content_delivery
    from tin_lite.domain import SideEffectConflictError

    f = await commit_configured(
        await configured(await fixture(publication_db, monkeypatch)), mode="draft_only"
    )
    run, _, _ = await publish_draft(f, await start(f))

    async def conflicting(self, **values):
        raise SideEffectConflictError("project file request ID belongs to different changes")

    monkeypatch.setattr(content_delivery.ContentDelivery, "save_settings", conflicting)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f), raise_app_exceptions=False),
        base_url="https://tin.test",
    ) as client:
        response = await client.post(
            f"/api/workflows/runs/{run.id}/approve",
            json={"delivery": "github_commit", "remember": True},
        )

    assert response.status_code == 409
    assert "different changes" in response.json()["detail"]
