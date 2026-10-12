import hashlib
from uuid import uuid4

import asyncpg
import pytest
from test_private_workflows import fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.technical_repair_plan import REPAIRS
from tin_lite.website_change import ChangeRow, propose


def change(kind):
    return ChangeRow(
        change_id="oa_" + hashlib.sha256(kind.encode()).hexdigest()[:20],
        source="audit",
        kind=kind,
        title="Audit repair",
        paths=("/",),
        content_sha256="a" * 64,
        detail={},
    )


async def test_database_accepts_every_supported_audit_repair_kind(publication_db):
    f = await fixture(publication_db)
    kinds = {repair.kind for repair in REPAIRS.values()}
    rows = await propose(f.db, project_id=f.project.id, rows=[change(k) for k in sorted(kinds)])
    assert {row["kind"] for row in rows} == kinds


@pytest.mark.parametrize("kind", ["", "x", "404_error", "Soft_404", "soft-404", "a" * 41])
async def test_database_still_refuses_malformed_change_kinds(publication_db, kind):
    f = await fixture(publication_db)
    with pytest.raises(asyncpg.CheckViolationError):
        await f.db.pool.execute(
            "INSERT INTO website_changes (id, project_id, change_id, source, kind, title, "
            "paths, content_sha256) VALUES ($1,$2,$3,'audit',$4,'Audit repair','[\"/\"]',$5)",
            uuid4(),
            f.project.id,
            "oa_" + hashlib.sha256(kind.encode()).hexdigest()[:20],
            kind,
            "a" * 64,
        )
