"""A fresh, fully migrated Postgres database per test, never the configured product DB.

Applying every migration takes about two seconds, and some two thousand tests need a
database. Each test process migrates one template database the first time a test asks for
one; every test then gets its own copy of it (CREATE DATABASE ... TEMPLATE, a fraction of a
second) and drops that copy afterwards. A test still starts from exactly what the migrations
make, and nothing it writes reaches another test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from tin_lite.db import Database, apply_migrations

MIGRATIONS = Path(__file__).parents[1] / "migrations"
DSN_VARIABLE = "TIN_LITE_TEST_DATABASE_DSN"

_template: str | None = None


def _with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path="/" + name))


async def _migrated_template(dsn: str) -> str:
    """This process's template database, migrated from nothing the first time it is asked."""
    global _template
    if _template is None:
        name = f"tin_test_template_{os.getpid()}_{uuid4().hex[:8]}"
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()
        template_dsn = _with_database(dsn, name)
        assert "024_retained_procedure_output.sql" in await apply_migrations(
            template_dsn, MIGRATIONS
        )
        assert await apply_migrations(template_dsn, MIGRATIONS) == []
        _template = name
    return _template


@asynccontextmanager
async def migrated_database(*, max_size: int = 10) -> AsyncIterator[Database]:
    """A Database on its own copy of the migrated template; skips without a test DSN."""
    dsn = os.environ.get(DSN_VARIABLE)
    if not dsn:
        pytest.skip(f"set {DSN_VARIABLE} for isolated Postgres contract tests")
    template = await _migrated_template(dsn)
    name = "tin_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    database = Database(_with_database(dsn, name))
    try:
        database._pool = await asyncpg.create_pool(database._dsn, min_size=1, max_size=max_size)
        yield database
    finally:
        await database.close()
        # FORCE ends connections a test left open, which would otherwise block the drop.
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def drop_template() -> None:
    """Drop this process's template at the end of the session, if a test made one."""
    global _template
    dsn = os.environ.get(DSN_VARIABLE)
    if _template is None or not dsn:
        return
    name, _template = _template, None

    async def drop() -> None:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(drop())
