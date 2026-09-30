from uuid import uuid4

from test_procedure_publication import publication_db as publication_db

from tin_lite.x_posts import save_effect


async def test_project_purge_removes_only_its_x_approval_chain(publication_db):
    db = publication_db
    projects = [
        await db.create_project(name="X purge", state_repo_id=f"projects/{uuid4()}")
        for _ in range(2)
    ]
    owned = []
    for project in projects:
        approval = uuid4()
        keys = [
            f"x:approved:{approval}",
            f"x:preview:{uuid4()}",
            f"x:confirm:{project.id}:{uuid4()}",
            f"x:post:{approval}",
            f"x:media:{approval}:0:1:upload",
        ]
        for key in keys:
            await save_effect(db, key, {"project_id": str(project.id), "text": "Test draft"})
        owned.append(keys)
    async with db.pool.acquire() as conn:
        await db.purge_project_data(conn, project_id=projects[0].id)
        await db.purge_project_data(conn, project_id=projects[0].id)
    for key in owned[0]:
        assert await db.get_effect(key) is None
    for key in owned[1]:
        assert (await db.get_effect(key)).status == "completed"
