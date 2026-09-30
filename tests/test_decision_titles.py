"""Decisions name what they ask about, and list only what someone can decide."""

from __future__ import annotations

from uuid import uuid4

import pytest
from test_procedure_publication import PATH, activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.content_delivery import summary_line

SHA = "b" * 40


async def review_run(db, *, artifact_title=None, explanation=None):
    _, _, run, _ = await activity_fixture(db, review=True)
    await db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=SHA,
        artifact_ref=f"code.storage://repo@{SHA}/{PATH}",
        artifact_path=PATH,
        summary="Research is ready for your review.",
        artifact_title=artifact_title,
        explanation=explanation,
    )
    return run


async def titles(db, project_id):
    return [item["title"] for item in await db.list_pending_decisions(project_id=project_id)]


@pytest.mark.asyncio
async def test_review_title_names_the_output(publication_db):
    run = await review_run(publication_db, artifact_title="Which tools work with agents?")
    assert await titles(publication_db, run.project_id) == ["Review: Which tools work with agents?"]


async def saved_day(db, run_id):
    """The day a run's output was saved, in its project's time zone, as a title shows it."""
    saved = await db.pool.fetchval(
        "SELECT (review_requested_at AT TIME ZONE project.timezone)::date "
        "FROM workflow_runs JOIN projects AS project ON project.id = project_id "
        "WHERE workflow_runs.id=$1",
        run_id,
    )
    return f"{saved:%b} {saved.day}"


@pytest.mark.asyncio
async def test_an_output_without_a_heading_is_named_by_workflow_and_day(publication_db):
    # Repeat runs of one workflow would otherwise share a title.
    run = await review_run(publication_db)
    day = await saved_day(publication_db, run.id)
    assert await titles(publication_db, run.project_id) == [f"Review: Research · {day}"]
    [decision] = await publication_db.list_pending_decisions(project_id=run.project_id)
    assert decision["output_title"] == f"Research · {day}"


@pytest.mark.asyncio
async def test_generic_titles_saved_earlier_are_named_when_listed(publication_db):
    run = await review_run(publication_db)
    await publication_db.pool.execute(
        "UPDATE run_decisions SET title='Review workflow output' WHERE run_id=$1", run.id
    )
    day = await saved_day(publication_db, run.id)
    assert await titles(publication_db, run.project_id) == [f"Review: Research · {day}"]
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET artifact_title='A readable heading' WHERE id=$1", run.id
    )
    assert await titles(publication_db, run.project_id) == ["Review: A readable heading"]
    await publication_db.pool.execute(
        "UPDATE run_decisions SET title='Choose a hero' WHERE run_id=$1", run.id
    )
    assert await titles(publication_db, run.project_id) == ["Choose a hero"]


async def task_run(db, workflow_id, *, phase, has_changes, title):
    # A project holds one active task at a time, so each task gets its own project.
    project = await db.create_project(name=title, state_repo_id=f"projects/{uuid4()}")
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            task_phase, task_has_changes, task_title)
           VALUES ($1,$2,$3,'project.task',$4,$5,$6,1,1,'needs_input',false,$7,$8,$9)""",
        run_id,
        project.id,
        workflow_id,
        "d" * 40,
        f"project.task:{run_id}",
        str(run_id),
        phase,
        has_changes,
        title,
    )
    return project.id, run_id


@pytest.mark.asyncio
async def test_only_tasks_with_changes_to_review_are_decisions(publication_db):
    db = publication_db
    workflow_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'project.task', 'One-off project task', 'project.task',
                   'registry/workflows', 'task.json', $2, '1', '{}')""",
        workflow_id,
        "d" * 40,
    )
    project_id, reviewing = await task_run(
        db, workflow_id, phase="review", has_changes=True, title="Update the FAQ"
    )
    decisions = await db.list_pending_decisions(project_id=project_id)
    assert [(item["run_id"], item["title"]) for item in decisions] == [
        (reviewing, "Review: Update the FAQ")
    ]
    assert (await db.get_project_system_summary(project_id=project_id))["waiting_count"] == 1
    # A task asking a question, and a reviewed task that changed nothing, leave both counts.
    for phase, has_changes in [("needs_input", False), ("needs_input", None), ("review", False)]:
        project_id, _ = await task_run(
            db, workflow_id, phase=phase, has_changes=has_changes, title="Pick a tone"
        )
        assert not await db.list_pending_decisions(project_id=project_id)
        assert (await db.get_project_system_summary(project_id=project_id))["waiting_count"] == 0


@pytest.mark.asyncio
async def test_the_card_line_says_what_the_output_contains(publication_db):
    run = await review_run(
        publication_db,
        artifact_title="Release announcements for Tin",
        explanation="What shipped: Public workflow packages anyone can contribute.",
    )
    [decision] = await publication_db.list_pending_decisions(project_id=run.project_id)
    assert decision["output_title"] == "Release announcements for Tin"
    assert (
        decision["explanation"] == "What shipped: Public workflow packages anyone can contribute."
    )
    # Activity keeps the review notice; only the card carries the output's own line.
    summary = await publication_db.pool.fetchval(
        "SELECT summary FROM activity_events WHERE run_id=$1 "
        "AND event_type='human_review_requested'",
        run.id,
    )
    assert summary == "Research is ready for your review."


RELEASE = b"""# Release announcements for Tin

## What shipped

### Features (1)
- Public workflow packages anyone can [contribute](https://example.com/contribute). More soon.

*2 internal change(s) left out of the announcements.*

---

## X post

> Tin 0.9 is out.
"""


@pytest.mark.parametrize(
    ("document", "line"),
    [
        (RELEASE, "What shipped: Public workflow packages anyone can contribute."),
        (
            b"# Which tools work with coding agents?\n\nMost of them expose an MCP server. "
            b"Some ship a CLI.\n",
            "Most of them expose an MCP server.",
        ),
        (
            b"---\ntitle: Notes\n---\n# Notes\n\n```\ncode line\n```\n> quoted\n\n"
            b"| a | b |\n\nThe *real* first line\n",
            "The real first line",
        ),
        (b"# Only a title\n\n## And a section\n", None),
        (b"\xff\xfe not text", None),
    ],
)
def test_summary_line_reads_the_first_thing_a_reader_sees(document, line):
    assert summary_line(document) == line


GENERIC = "Research is ready for your review."
DELIVERY = "Approval opens an unmerged GitHub PR in example/site at docs/answer.md."


class PinnedFiles:
    """Saved outputs by (revision, path); records every read."""

    def __init__(self, files):
        self.files, self.reads = files, []

    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        self.reads.append((commit_sha, path))
        return self.files[(commit_sha, path)]


async def older_reviews(db, count, *, summary=GENERIC):
    """`count` reviews in one project, saved the way decisions were before outputs carried
    their heading: no title on the run, and a card line that only restates the workflow."""
    _, _, first, _ = await activity_fixture(db, review=True)
    runs = [first.id] + [uuid4() for _ in range(count - 1)]
    for run_id in runs[1:]:
        await db.pool.execute(
            """INSERT INTO workflow_runs (id, project_id, workflow_id, executor,
                definition_commit_sha, temporal_workflow_id, thread_id, generation,
                fencing_token, status, lease_active, review_required)
               VALUES ($1,$2,$3,'codex.procedure',$4,$5,$6,1,1,'running',false,true)""",
            run_id,
            first.project_id,
            first.workflow_id,
            "d" * 40,
            f"procedure:{run_id}",
            str(run_id),
        )
    for index, run_id in enumerate(runs):
        await db.request_human_review(
            run_id=run_id,
            canonical_commit_sha=SHA,
            artifact_ref=f"code.storage://repo@{SHA}/reports/{index}.md",
            artifact_path=f"reports/{index}.md",
            summary=summary,
        )
    return await db.get_project(first.project_id), runs


def document(heading, line):
    return f"# {heading}\n\n{line} A second sentence the card leaves out.\n".encode()


@pytest.mark.asyncio
async def test_older_decisions_get_their_heading_and_sentence_a_few_at_a_time(publication_db):
    from tin_lite.decision_backfill import PER_LISTING, backfill_decision_text

    db = publication_db
    project, runs = await older_reviews(db, PER_LISTING + 2)
    files = PinnedFiles(
        {
            (SHA, f"reports/{index}.md"): document(f"Answer {index}", f"Line {index}.")
            for index in range(len(runs))
        }
    )
    before = await db.list_pending_decisions(project_id=project.id)
    assert [item["explanation"] for item in before] == [GENERIC] * len(runs)

    assert await backfill_decision_text(database=db, storage=files, project=project) == 3
    assert len(files.reads) == PER_LISTING
    assert await backfill_decision_text(database=db, storage=files, project=project) == 2
    assert len(files.reads) == len(runs)
    # Stored once: later listings read Postgres only.
    assert await backfill_decision_text(database=db, storage=files, project=project) == 0
    assert len(files.reads) == len(runs)

    after = await db.list_pending_decisions(project_id=project.id)
    assert [item["output_title"] for item in after] == [f"Answer {i}" for i in range(len(runs))]
    assert [item["title"] for item in after] == [f"Review: Answer {i}" for i in range(len(runs))]
    assert [item["explanation"] for item in after] == [f"Line {i}." for i in range(len(runs))]


@pytest.mark.asyncio
async def test_backfill_reads_the_revision_the_decision_pins(publication_db):
    from tin_lite.decision_backfill import backfill_decision_text

    db = publication_db
    project, [run_id] = await older_reviews(db, 1, summary=f"{GENERIC} {DELIVERY}")
    later = "c" * 40
    files = PinnedFiles(
        {
            (SHA, "reports/0.md"): document("Which tools work with agents?", "Most use MCP."),
            (later, "reports/0.md"): document("A later edit", "Not what was reviewed."),
        }
    )
    await backfill_decision_text(database=db, storage=files, project=project)
    assert files.reads == [(SHA, "reports/0.md")]
    [decision] = await db.list_pending_decisions(project_id=project.id)
    assert decision["output_title"] == "Which tools work with agents?"
    # Where approval delivers the draft still follows the new first sentence.
    assert decision["explanation"] == f"Most use MCP. {DELIVERY}"
    run = await db.get_run(run_id)
    assert run.artifact_title == "Which tools work with agents?"


@pytest.mark.asyncio
async def test_backfill_is_idempotent_for_documents_with_nothing_to_say(publication_db):
    from tin_lite.decision_backfill import backfill_decision_text

    db = publication_db
    project, [run_id] = await older_reviews(db, 1)
    files = PinnedFiles({(SHA, "reports/0.md"): b"| a | b |\n| - | - |\n"})
    assert await backfill_decision_text(database=db, storage=files, project=project) == 1
    assert await backfill_decision_text(database=db, storage=files, project=project) == 0
    assert files.reads == [(SHA, "reports/0.md")]
    [decision] = await db.list_pending_decisions(project_id=project.id)
    day = await saved_day(db, run_id)
    assert decision["output_title"] == f"Research · {day}"
    # No filler: the card's own default line, which the card leaves out.
    assert decision["explanation"] == "Review the complete output before this workflow continues."


@pytest.mark.asyncio
async def test_backfill_skips_new_decisions_and_retries_an_unreadable_one(publication_db):
    from tin_lite.decision_backfill import backfill_decision_text

    db = publication_db
    project, [older, newer] = await older_reviews(db, 2)
    # Saved after outputs carried their heading: nothing to read.
    await db.pool.execute("UPDATE run_decisions SET explanation='New line.' WHERE run_id=$1", newer)
    files = PinnedFiles({})
    # Storage unavailable: nothing is stored and the next listing tries again.
    assert await backfill_decision_text(database=db, storage=files, project=project) == 0
    assert await backfill_decision_text(database=db, storage=files, project=project) == 0
    assert files.reads == [(SHA, "reports/0.md")] * 2
    decisions = await db.list_pending_decisions(project_id=project.id)
    assert [item["run_id"] for item in decisions] == [older, newer]
    assert decisions[1]["explanation"] == "New line."


ANNOUNCEMENT = b"""# Tin release: brand guides, a public Registry

## What shipped

### Feature (6)
- Brand guide proposals.
- A public Registry.

### Improvement (1)
- Faster Files.

*2 internal change(s) left out of the announcements.*

---

## X

### Post

> Tin ships brand guides.

## LinkedIn

Tin ships brand guides.

## Newsletter email

**Subject:** Brand guides
"""


@pytest.mark.parametrize(
    ("document", "line"),
    [
        (
            ANNOUNCEMENT,
            "Announces six features and one improvement, with drafts for X, LinkedIn "
            "and your newsletter.",
        ),
        (RELEASE, "Announces one feature, with drafts for X post."),
        (
            b"# Release\n\n## What shipped\n\n### Fix (12)\n- A fix.\n",
            "Announces 12 fixes.",
        ),
        # Anything else keeps the first sentence a reader sees.
        (b"# Notes\n\n## What shipped\n\nA paragraph, not counted groups.\n", None),
        (b"# Notes\n\n## Summary\n\n### Feature (2)\n- Two.\n", None),
    ],
)
def test_a_release_announcement_says_what_it_announces(document, line):
    from tin_lite.content_delivery import release_line, review_line

    assert release_line(document) == line
    assert review_line(document) == (line or summary_line(document))


@pytest.mark.asyncio
async def test_listing_decisions_names_older_ones_from_their_files(publication_db):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from tin_lite.api import router
    from tin_lite.auth import AuthContext, require_user

    db = publication_db
    project, _ = await older_reviews(db, 1)
    await db.pool.execute(
        "INSERT INTO project_memberships (project_id, clerk_user_id) VALUES ($1, 'user_member')",
        project.id,
    )
    files = PinnedFiles({(SHA, "reports/0.md"): ANNOUNCEMENT})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_member",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.runtime = SimpleNamespace(database=db, storage=files)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(f"/api/projects/{project.id}/decisions")
        second = await client.get(f"/api/projects/{project.id}/decisions")
    assert first.status_code == 200 and first.headers["X-Tin-Read-Source"] == "postgres"
    [decision] = first.json()
    assert decision["title"] == "Review: Tin release: brand guides, a public Registry"
    assert decision["explanation"] == (
        "Announces six features and one improvement, with drafts for X, LinkedIn "
        "and your newsletter."
    )
    assert second.json() == first.json()
    assert len(files.reads) == 1
