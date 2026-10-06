"""A saved workflow's diagram over HTTP: scoped to the project, drawn from its pinned revision."""

from uuid import uuid4

import httpx
from test_private_workflows import ACTOR, activate, app, fixture
from test_procedure_publication import publication_db as publication_db


async def saved(f, workflow, *, name="Saved digest"):
    return await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        definition_commit_sha=workflow.current_commit_sha,
        name=name,
        inputs={"brief": "Summarize the public docs"},
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
        pinned_definition=workflow.definition,
    )


async def test_a_saved_workflow_is_drawn_from_its_pinned_revision(publication_db):
    f = await fixture(publication_db)
    await activate(f)
    workflow = next(
        item
        for item in await f.db.list_workflows(project_id=f.project.id)
        if item.key == "custom.research_digest"
    )
    configured = await saved(f, workflow)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(
            f"/api/projects/{f.project.id}/workflows/{configured.id}/diagram"
        )
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["flow"] == workflow.definition["presentation"]["flow"]
        assert body["version"] == body["pinned_version"] == workflow.definition["version"]
        assert body["exact"] is True

        elsewhere = await client.get(f"/api/projects/{f.project.id}/workflows/{uuid4()}/diagram")
        assert elsewhere.status_code == 404

        # The saved list says it is drawn, so a row shows the button even when the catalog
        # hides the workflow.
        listed = await client.get(f"/api/projects/{f.project.id}/workflows")
        assert listed.status_code == 200, listed.json()
        row = next(item for item in listed.json() if item["id"] == str(configured.id))
        assert row["workflow_drawn"] is True


async def test_a_diagram_needs_project_membership(publication_db):
    f = await fixture(publication_db)
    await activate(f)
    workflow = next(
        item
        for item in await f.db.list_workflows(project_id=f.project.id)
        if item.key == "custom.research_digest"
    )
    configured = await saved(f, workflow)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, actor="user_outsider")),
        base_url="https://tin.test",
    ) as client:
        response = await client.get(
            f"/api/projects/{f.project.id}/workflows/{configured.id}/diagram"
        )
        assert response.status_code in {403, 404}
