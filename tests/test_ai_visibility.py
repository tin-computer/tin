"""AI Visibility: a week's report is validated, scored and stored per project and week, and the
page reads one week with every week's totals."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from tin_lite.ai_visibility import (
    AIVisibilityStore,
    answers_for,
    page,
    parse_report,
    summarize,
)
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.db import Database, apply_migrations

USER = "user_founder"
OTHER = "user_stranger"


def report(week_start: str = "2026-11-02", **changes) -> dict:
    """Two questions (one branded, out of the score), a web engine and a memory engine, two
    answers each."""
    base = {
        "version": 1,
        "week_start": week_start,
        "collected_at": "2026-11-04T19:51:18+00:00",
        "domain": "tin.computer",
        "takes": 2,
        "engines": [
            {"key": "chatgpt_app", "name": "ChatGPT", "detail": "app", "kind": "web"},
            {"key": "sol_noweb", "name": "GPT-6.1 Sol", "detail": "OpenAI", "kind": "memory"},
        ],
        "sections": [{"key": "ICP2", "title": "Technical founders"}],
        "groups": [
            {"key": "C7", "section": "ICP2", "title": "Run marketing from my coding agent"},
            {"key": "C13", "section": "ICP2", "title": "Branded", "note": "names you"},
        ],
        "questions": [
            {"id": "7d", "group": "C7", "text": "Which marketing tools have an MCP server?"},
            {"id": "13a", "group": "C13", "text": "Tin vs Okara", "in_score": False},
        ],
        "answers": [
            answer("7d", "chatgpt_app", 1, "mentioned", rivals=["HubSpot", "Ahrefs"]),
            answer("7d", "chatgpt_app", 2, "none", rivals=["HubSpot"]),
            answer("7d", "sol_noweb", 1, "none", rivals=["Zapier"]),
            answer("7d", "sol_noweb", 2, "no_answer"),
            answer("13a", "chatgpt_app", 1, "recommended", rivals=["Okara"]),
            answer("13a", "chatgpt_app", 2, "recommended"),
        ],
    }
    return {**base, **changes}


def answer(question, engine, take, outcome, rivals=()) -> dict:
    return {
        "question": question,
        "engine": engine,
        "take": take,
        "outcome": outcome,
        "quote": "Tin is an open-source option." if outcome in ("recommended", "mentioned") else "",
        "queries": ["marketing MCP servers Claude Code"] if engine == "chatgpt_app" else [],
        "cited": [{"url": "https://tin.computer/", "domain": "tin.computer", "title": "Tin"}],
        "recommended_instead": list(rivals),
    }


def raw(data: dict) -> str:
    return json.dumps(data)


def test_a_report_must_be_consistent() -> None:
    parse_report(raw(report()))
    cases = {
        "Monday": report(week_start="2026-11-04"),
        "unknown question or engine": report(answers=[answer("7d", "nobody", 1, "none")]),
        "two answers": report(answers=[answer("7d", "chatgpt_app", 1, "none")] * 2),
        "above takes": report(answers=[answer("7d", "chatgpt_app", 3, "none")]),
        "unknown group": report(questions=[{"id": "x", "group": "C99", "text": "?"}]),
        "unique": report(engines=report()["engines"] * 2),
        "Extra": report(colour="green"),
        "icon": report(
            engines=[{**report()["engines"][0], "icon": "somebody"}, report()["engines"][1]]
        ),
    }
    for expected, data in cases.items():
        with pytest.raises(ValueError, match=expected):
            parse_report(raw(data))
    with pytest.raises(ValueError, match="bytes"):
        parse_report(" " * 20_000_001)


def test_the_score_leaves_out_unscored_questions_and_missing_answers() -> None:
    summary = summarize(parse_report(raw(report())))
    # 7d only: two web answers (one mentioned), one memory answer (no_answer is not counted).
    assert summary["web"] == {
        "answers": 2,
        "recommended": 0,
        "mentioned": 1,
        "questions": 1,
        "questions_named": 1,
        "rivals": [{"name": "HubSpot", "answers": 2}, {"name": "Ahrefs", "answers": 1}],
    }
    assert summary["memory"]["answers"] == 1 and summary["memory"]["mentioned"] == 0
    assert summary["week_start"] == "2026-11-02"


def test_the_page_shows_each_cells_best_answer_and_the_trend() -> None:
    first = parse_report(
        raw(report("2026-10-26", answers=[answer("7d", "chatgpt_app", 1, "none")]))
    )
    second = parse_report(raw(report()))
    weeks = [summarize(first), summarize(second)]
    shown = page(second, weeks)
    assert shown["cells"]["7d"]["chatgpt_app"] == {"outcome": "mentioned", "count": 1, "of": 2}
    assert shown["cells"]["7d"]["sol_noweb"] == {"outcome": "none", "count": 1, "of": 2}
    assert shown["cells"]["13a"]["chatgpt_app"]["outcome"] == "recommended"
    assert "sol_noweb" not in shown["cells"]["13a"]
    assert shown["rivals"]["7d"] == {"web": ["HubSpot", "Ahrefs"], "memory": ["Zapier"]}
    assert shown["score"]["web"]["mentioned_rate"] == 0.5
    assert shown["previous"]["web"]["mentioned_rate"] == 0.0
    assert [w["week_start"] for w in shown["weeks"]] == ["2026-10-26", "2026-11-02"]
    assert page(first, weeks)["previous"] is None
    assert shown["domain"] == "tin.computer" and shown["groups"][1]["note"] == "names you"


def test_answers_for_one_cell() -> None:
    parsed = parse_report(raw(report()))
    with_text = report(
        engines=[{**report()["engines"][0], "icon": "openai"}, report()["engines"][1]],
        answers=[{**answer("7d", "chatgpt_app", 1, "none"), "text": "## Options\n- HubSpot"}],
    )
    shown = answers_for(parse_report(raw(with_text)), "7d", "chatgpt_app")
    assert shown["engine"]["icon"] == "openai" and shown["answers"][0]["text"].startswith(
        "## Options"
    )
    cell = answers_for(parsed, "7d", "chatgpt_app")
    assert [a["take"] for a in cell["answers"]] == [1, 2]
    assert cell["answers"][0]["queries"] == ["marketing MCP servers Claude Code"]
    with pytest.raises(LookupError):
        answers_for(parsed, "7d", "nobody")


# ------------------------------------------------------------------------------- HTTP


class FakePool:
    def __init__(self, rows: dict[tuple[UUID, date], dict]) -> None:
        self.rows = rows

    async def fetch(self, _sql, project_id):
        return [
            {"summary": json.dumps(row["summary"])}
            for (pid, _week), row in sorted(self.rows.items(), key=lambda item: item[0][1])
            if pid == project_id
        ]

    async def fetchval(self, _sql, project_id, week_start):
        row = self.rows.get((project_id, week_start))
        return None if row is None else json.dumps(row["report"])


class FakeDatabase:
    def __init__(self, project_id: UUID, rows: dict) -> None:
        self.project_id = project_id
        self.pool = FakePool(rows)

    async def has_project_access(self, *, project_id: UUID, clerk_user_id: str) -> bool:
        return clerk_user_id == USER and project_id == self.project_id

    async def get_project(self, project_id: UUID):
        return SimpleNamespace(id=project_id)


def app_for(database, clerk_user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=database)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=clerk_user_id,
        token_type="session_token",  # noqa: S106
    )
    return app


@pytest.mark.asyncio
async def test_the_page_is_for_project_members_and_reads_any_day_of_a_week() -> None:
    project_id = uuid4()
    parsed = parse_report(raw(report()))
    rows = {
        (project_id, parsed.week_start): {
            "report": json.loads(parsed.model_dump_json()),
            "summary": summarize(parsed),
        }
    }
    database = FakeDatabase(project_id, rows)
    base = f"/api/projects/{project_id}/ai-visibility"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(database, USER)), base_url="http://test"
    ) as client:
        latest = await client.get(base)
        midweek = await client.get(base, params={"week": "2026-11-05"})
        missing = await client.get(base, params={"week": "2026-10-20"})
        cell = await client.get(
            f"{base}/answers",
            params={"week": "2026-11-02", "question": "7d", "engine": "sol_noweb"},
        )
        empty = await client.get(f"/api/projects/{uuid4()}/ai-visibility")
    assert latest.status_code == 200 and latest.json()["week_start"] == "2026-11-02"
    assert latest.headers["x-tin-read-source"] == "postgres"
    assert midweek.json()["week_start"] == "2026-11-02"
    assert missing.status_code == 404
    assert [a["outcome"] for a in cell.json()["answers"]] == ["none", "no_answer"]
    assert empty.status_code == 404  # not a member of that project
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(database, OTHER)), base_url="http://test"
    ) as client:
        assert (await client.get(base)).status_code == 404


@pytest.mark.asyncio
async def test_a_member_project_without_weeks_says_so() -> None:
    project_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(FakeDatabase(project_id, {}), USER)),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/projects/{project_id}/ai-visibility")
    assert response.status_code == 200 and response.json() == {"week_start": None, "weeks": []}


# --------------------------------------------------------------------------- Postgres


@pytest.fixture
async def visibility_db():
    """Real migrations in a disposable schema, never the configured product DB."""
    dsn = os.environ.get("TIN_LITE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set TIN_LITE_TEST_DATABASE_DSN for isolated Postgres contract tests")
    schema = "ai_visibility_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    database = Database(dsn)
    try:
        database._pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        scoped = dsn + ("&" if "?" in dsn else "?") + f"search_path={schema}"
        await apply_migrations(scoped, Path(__file__).parents[1] / "migrations")
        yield database
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_weeks_are_stored_per_project_and_replaced_on_import(visibility_db) -> None:
    project = await visibility_db.create_project(name="Visibility", state_repo_id="repo-vis")
    store = AIVisibilityStore(visibility_db)
    summary = await visibility_db.get_project_system_summary(project_id=project.id)
    assert summary["ai_visibility_weeks"] == 0
    assert await store.page(project.id) == {"week_start": None, "weeks": []}

    assert await store.save(project.id, parse_report(raw(report("2026-10-26")))) is False
    assert await store.save(project.id, parse_report(raw(report()))) is False
    replaced = report(answers=[answer("7d", "chatgpt_app", 1, "recommended")])
    assert await store.save(project.id, parse_report(raw(replaced))) is True

    shown = await store.page(project.id)
    assert shown["week_start"] == "2026-11-02"
    assert shown["cells"]["7d"]["chatgpt_app"]["outcome"] == "recommended"
    assert [w["week_start"] for w in shown["weeks"]] == ["2026-10-26", "2026-11-02"]
    assert (await store.page(project.id, date(2026, 10, 30)))["week_start"] == "2026-10-26"
    with pytest.raises(LookupError):
        await store.page(project.id, date(2026, 10, 12))
    cell = await store.answers(project.id, date(2026, 11, 3), "7d", "chatgpt_app")
    assert [a["outcome"] for a in cell["answers"]] == ["recommended"]
    summary = await visibility_db.get_project_system_summary(project_id=project.id)
    assert summary["ai_visibility_weeks"] == 2
    with pytest.raises(asyncpg.CheckViolationError):
        await visibility_db.pool.execute(
            """INSERT INTO ai_visibility_weeks (project_id, week_start, report, summary)
               VALUES ($1, '2026-11-04', '{}', '{}')""",
            project.id,
        )
