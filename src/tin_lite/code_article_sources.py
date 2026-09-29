"""Legacy approved-article inputs for pinned code definitions and saved configurations."""

import hashlib
from uuid import UUID

from tin_lite import approved_article
from tin_lite.workflow_code import approved_article_input

OPERATION = "code_approved_article_v1"


def source_key(run_id):
    return f"code-article:{UUID(str(run_id))}:source"


async def select(*, database, storage, project_id, definition, inputs):
    name = approved_article_input(definition)
    if name is None:
        raise ValueError("This workflow does not declare an approved article source.")
    return await approved_article.select(
        database=database,
        storage=storage,
        project_id=project_id,
        source_run_id=inputs[name],
        include_style=True,
    )


async def guard(conn, *, project_id, definition, inputs, source):
    name = approved_article_input(definition)
    if name is None:
        if source is not None:
            raise ValueError("This workflow does not declare an approved article source.")
        return
    if source is None or str(UUID(inputs[name])) != source.get("source_run_id"):
        raise ValueError("Select the approved article before creating this workflow run.")
    await approved_article.guard(conn, project_id=project_id, source=source)


async def saved_source(database, run, spec):
    receipt = await database.get_effect(source_key(run.id))
    if (
        not receipt
        or receipt.status != "completed"
        or receipt.operation != OPERATION
        or not receipt.result
    ):
        raise ValueError("The run's approved article snapshot is unavailable.")
    source = receipt.result
    if (
        str(UUID(run.input[spec.approved_article_input])) != source["source_run_id"]
        or hashlib.sha256(source["article"].encode()).hexdigest() != source["article_sha256"]
    ):
        raise ValueError("The approved article differs from the run's pinned source.")
    return source
