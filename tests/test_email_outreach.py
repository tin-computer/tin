from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from tin_lite.campaign_revisions import request_email_campaign_revision
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.db import Database, _email_campaign_progress_text
from tin_lite.domain import EMAIL_CAMPAIGN_WORKFLOW_NAME, EMAIL_SHORTLIST_WORKFLOW_NAME
from tin_lite.email_outreach import (
    build_campaign_plan,
    build_campaign_revision_plan,
    build_email_message,
    campaign_message_id,
    campaign_revision_path,
    parse_email_send_policy,
    parse_selected_shortlist,
)
from tin_lite.workflows import registered_workflow_implementations, registered_workflows

RUN_ID = UUID("00000000-0000-4000-8000-0000000000cc")
HEADERS = (
    "candidate_id",
    "email",
    "name",
    "organization",
    "relationship_signal",
    "last_interaction_at",
    "why_selected",
    "status",
    "notes",
)


def shortlist_csv(*rows: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def test_email_campaign_progress_uses_the_fixed_approved_delivery_plan() -> None:
    next_delivery = datetime(2026, 9, 5, 16, 0, tzinfo=UTC)

    assert _email_campaign_progress_text(
        campaign_status="running",
        total=4,
        resolved=3,
        ready=0,
        next_delivery_at=next_delivery,
        send_timezone="America/Los_Angeles",
    ) == ("waiting", "Waiting until sat 09:00 for the next approved email")
    assert _email_campaign_progress_text(
        campaign_status="running",
        total=4,
        resolved=3,
        ready=1,
        next_delivery_at=None,
        send_timezone="America/Los_Angeles",
    ) == ("sending", "Sending 1 approved email")
    assert _email_campaign_progress_text(
        campaign_status="running",
        total=4,
        resolved=4,
        ready=0,
        next_delivery_at=None,
        send_timezone="America/Los_Angeles",
    ) == ("finishing", "Finishing the approved email campaign")


def test_campaign_snapshots_only_selected_rows_and_exact_rendered_copy() -> None:
    content = shortlist_csv(
        (
            "person-1",
            "ADA@example.com",
            "Ada Lovelace",
            "Analytical Engines",
            "former collaborator",
            "2026-08-01",
            "Relevant launch",
            "selected",
            "",
        ),
        (
            "person-2",
            "ignored@example.com",
            "Ignored Person",
            "Elsewhere",
            "unknown",
            "",
            "Outside focus",
            "excluded",
            "",
        ),
    )

    recipients = parse_selected_shortlist(
        content,
        run_id=RUN_ID,
        subject="Hello {{name}}",
        body="Hi {{name}},\n\nWould Tuesday work?",
        follow_up_body="Just checking back, {{name}}.",
    )

    assert len(recipients) == 1
    recipient = recipients[0]
    assert recipient.email == "ada@example.com"
    assert recipient.subject == "Hello Ada Lovelace"
    assert recipient.body == "Hi Ada Lovelace,\n\nWould Tuesday work?"
    assert recipient.follow_up_body == "Just checking back, Ada Lovelace."

    plan = build_campaign_plan(
        run_id=RUN_ID,
        shortlist_path="outreach/email/SHORTLIST.csv",
        shortlist_commit_sha="a" * 40,
        recipients=recipients,
        subject_template="Hello {{name}}",
        body_template="Hi {{name}},\n\nWould Tuesday work?",
        follow_up_template="Just checking back, {{name}}.",
        follow_up_delay_days=4,
        send_interval_seconds=60,
        daily_send_cap=25,
        send_window_start="09:00",
        send_window_end="17:00",
        send_timezone="Europe/Berlin",
        sender_account="emre@example.com",
    ).decode()
    assert "Nothing has been sent" in plan
    assert "Ada Lovelace <ada@example.com>" in plan
    assert "ignored@example.com" not in plan
    assert "The only supported personalization" in plan
    assert "after 4 day(s) when no reply is found" in plan
    assert "at least 60 second(s) between messages" in plan
    assert "Sender: emre@example.com" in plan
    assert "Daily cap: 25" in plan
    assert "09:00–17:00 Europe/Berlin" in plan


def test_email_send_policy_validates_window_and_timezone() -> None:
    policy = parse_email_send_policy(
        daily_send_cap=25,
        send_interval_seconds=60,
        send_window_start="09:00",
        send_window_end="17:00",
        send_timezone="Europe/Berlin",
    )
    assert policy.timezone == "Europe/Berlin"

    with pytest.raises(ValueError, match="start before"):
        parse_email_send_policy(
            daily_send_cap=25,
            send_interval_seconds=60,
            send_window_start="17:00",
            send_window_end="09:00",
            send_timezone="Europe/Berlin",
        )
    with pytest.raises(ValueError, match="IANA"):
        parse_email_send_policy(
            daily_send_cap=25,
            send_interval_seconds=60,
            send_window_start="09:00",
            send_window_end="17:00",
            send_timezone="Mars/Olympus",
        )


async def _reserve_in_new_york(
    monkeypatch, *, now, used_today=0, window=(time(9), time(17))
) -> tuple[int, list[datetime]]:
    deferred: list[datetime] = []

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchrow(self, query, *args):
            return {
                "execution_key": "delivery-1",
                "status": "pending",
                "campaign_run_id": RUN_ID,
                "external_account_id": "account-1",
                "daily_send_cap": 25,
                "send_interval_seconds": 60,
                "send_window_start": window[0],
                "send_window_end": window[1],
                "send_timezone": "America/New_York",
                "revision_pending": False,
            }

        async def fetchval(self, query, *args):
            # The day's send count, then the account's last start (none yet).
            return used_today if "count(*)" in query else None

        async def execute(self, query, *args):
            if "SET scheduled_for" in query:
                deferred.append(args[1])
            return "UPDATE 1"

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    database = Database("postgresql://unused")
    database._pool = Pool()  # noqa: SLF001
    monkeypatch.setattr(database, "_refresh_email_campaign_progress", AsyncMock())

    wait = await database.reserve_outreach_delivery(
        recipient_id=UUID(int=1), stage="initial", now=now
    )
    return wait, deferred


@pytest.mark.parametrize(
    ("now", "used_today"),
    [
        (datetime(2026, 3, 7, 22, 30, tzinfo=UTC), 0),  # Sat 17:30 EST, after the window
        (datetime(2026, 3, 8, 5, 0, tzinfo=UTC), 0),  # Sun 00:00 EST, before the window
        (datetime(2026, 3, 7, 17, 0, tzinfo=UTC), 25),  # Sat 12:00 EST, daily cap reached
    ],
)
@pytest.mark.asyncio
async def test_send_window_wait_ends_at_window_open_across_spring_forward(
    monkeypatch, now, used_today
) -> None:
    # New York springs forward at 02:00 on Sunday 2026-03-08; its 09:00 EDT is 13:00 UTC.
    opens = datetime(2026, 3, 8, 13, tzinfo=UTC)

    wait, deferred = await _reserve_in_new_york(monkeypatch, now=now, used_today=used_today)

    assert wait == (opens - now).total_seconds()
    assert deferred == [opens]


@pytest.mark.parametrize(
    ("now", "window", "opens"),
    [
        # 01:00 EST, the repeated hour: the 01:30 EDT opening (05:30 UTC) has already passed.
        (datetime(2026, 11, 1, 6, 0, tzinfo=UTC), (time(1, 30), time(3)), None),
        # 01:10 EST: the 01:00-01:30 EDT window closed at 05:30 UTC; Monday 01:00 EST is next.
        (
            datetime(2026, 11, 1, 6, 10, tzinfo=UTC),
            (time(1), time(1, 30)),
            datetime(2026, 11, 2, 6, tzinfo=UTC),
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_window_compares_instants_across_fall_back(
    monkeypatch, now, window, opens
) -> None:
    # New York falls back at 02:00 EDT on Sunday 2026-11-01, so 01:00-02:00 happens twice.
    wait, deferred = await _reserve_in_new_york(monkeypatch, now=now, window=window)

    if opens is None:
        assert (wait, deferred) == (0, [])
    else:
        assert wait == (opens - now).total_seconds()
        assert deferred == [opens]


def test_campaign_rejects_unsafe_or_ambiguous_shortlist_content() -> None:
    valid_columns = (
        "person-1",
        "ada@example.com",
        "Ada\nBcc: target@example.com",
        "Analytical Engines",
        "former collaborator",
        "",
        "Relevant launch",
        "selected",
        "",
    )
    with pytest.raises(ValueError, match="safe line"):
        parse_selected_shortlist(
            shortlist_csv(valid_columns),
            run_id=RUN_ID,
            subject="Hello",
            body="Hello there",
            follow_up_body=None,
        )

    duplicate = shortlist_csv(*(valid_columns[:2] + ("Ada",) + valid_columns[3:] for _ in range(2)))
    with pytest.raises(ValueError, match="duplicate"):
        parse_selected_shortlist(
            duplicate,
            run_id=RUN_ID,
            subject="Hello",
            body="Hello there",
            follow_up_body=None,
        )

    with pytest.raises(ValueError, match="unsupported template"):
        parse_selected_shortlist(
            shortlist_csv(valid_columns[:2] + ("Ada",) + valid_columns[3:]),
            run_id=RUN_ID,
            subject="Hello {{company}}",
            body="Hello there",
            follow_up_body=None,
        )


def test_campaign_message_is_stable_rfc_email_and_blocks_header_injection() -> None:
    message_id = campaign_message_id("run:recipient:initial")
    raw = build_email_message(
        sender_email="emre@example.com",
        recipient_email="ada@example.com",
        recipient_name="Ada Lovelace",
        subject="A precise hello",
        body="Hello Ada.\n",
        message_id=message_id,
    ).as_bytes()
    parsed = BytesParser(policy=policy.default).parsebytes(raw)

    assert parsed["From"] == "emre@example.com"
    assert parsed["To"] == "Ada Lovelace <ada@example.com>"
    assert parsed["Message-ID"] == message_id
    assert parsed.get_content() == "Hello Ada.\n"
    assert message_id == campaign_message_id("run:recipient:initial")

    with pytest.raises(ValueError, match="safe line"):
        build_email_message(
            sender_email="emre@example.com",
            recipient_email="ada@example.com",
            recipient_name="Ada",
            subject="Hello\nBcc: target@example.com",
            body="Unsafe",
            message_id=message_id,
        )


def test_campaign_revision_is_a_separate_exact_review_of_unsent_follow_up() -> None:
    revision_id = UUID("00000000-0000-4000-8000-0000000000dd")
    path = campaign_revision_path(RUN_ID, revision_id)
    plan = build_campaign_revision_plan(
        run_id=RUN_ID,
        revision_id=revision_id,
        revision_number=2,
        previous_follow_up_body="Old follow-up.",
        follow_up_body="New follow-up for {{name}}.",
        pending_recipient_count=3,
    ).decode()

    assert path == f"outreach/email/campaigns/{RUN_ID}/revisions/{revision_id}.md"
    assert "Remaining deliveries are paused" in plan
    assert "Pending follow-ups affected: 3" in plan
    assert "Initial emails affected: none" in plan
    assert "Old follow-up." in plan
    assert "New follow-up for {{name}}." in plan
    assert "does not send another initial email" in plan


@pytest.mark.asyncio
async def test_revision_request_publishes_review_without_exposing_copy_to_receipt() -> None:
    revision_id = UUID("00000000-0000-4000-8000-0000000000dd")
    project_id = UUID("00000000-0000-4000-8000-0000000000ee")
    request_id = UUID("00000000-0000-4000-8000-0000000000ff")

    class FakeDatabase:
        def __init__(self) -> None:
            self.receipt_result = None
            self.finalized = None

        async def begin_email_campaign_revision(self, **values):
            assert values["follow_up_body"] == "New follow-up."
            return {
                "id": revision_id,
                "campaign_run_id": RUN_ID,
                "project_id": project_id,
                "request_id": request_id,
                "revision_number": 1,
                "status": "pending",
                "previous_follow_up_body": "Old follow-up.",
                "follow_up_body": values["follow_up_body"],
                "review_path": values["review_path"].replace(
                    str(values["revision_id"]), str(revision_id)
                ),
                "review_commit_sha": None,
                "pending_recipient_count": 1,
                "requested_at": SimpleNamespace(isoformat=lambda: "requested"),
                "reviewed_at": None,
            }

        async def get_project(self, value):
            assert value == project_id
            return SimpleNamespace(
                id=project_id,
                state_repo_id="projects/test",
                canonical_branch="main",
            )

        @asynccontextmanager
        async def effect_lock(self, execution_key, operation):
            assert str(request_id) in execution_key
            assert operation == "email_campaign_revision_review"
            yield object(), None

        @asynccontextmanager
        async def project_state_lock(self, conn, value):
            assert value == project_id
            yield

        async def start_effect(self, conn, **values):
            return None

        async def complete_effect(self, conn, **values):
            self.receipt_result = values["result"]

        async def fail_effect(self, conn, **values):
            raise AssertionError("revision publication should not fail")

        async def finalize_email_campaign_revision(self, **values):
            self.finalized = values
            return {
                "id": revision_id,
                "campaign_run_id": RUN_ID,
                "project_id": project_id,
                "request_id": request_id,
                "revision_number": 1,
                "status": "pending",
                "previous_follow_up_body": "Old follow-up.",
                "follow_up_body": "New follow-up.",
                "review_path": values["artifact_ref"].split("/", 3)[-1],
                "review_commit_sha": values["review_commit_sha"],
                "requested_at": SimpleNamespace(isoformat=lambda: "requested"),
                "reviewed_at": None,
            }

    class FakeStorage:
        def __init__(self) -> None:
            self.documents = None

        async def publish_state_documents(self, **values):
            self.documents = values["documents"]
            return "a" * 40, True

    database = FakeDatabase()
    storage = FakeStorage()
    await request_email_campaign_revision(
        database=database,
        storage=storage,
        run_id=RUN_ID,
        request_id=request_id,
        follow_up_body="  New follow-up.  ",
        clerk_user_id="user_test",
    )

    assert storage.documents is not None
    plan = next(iter(storage.documents.values())).decode()
    assert "New follow-up." in plan
    assert database.receipt_result == {
        "revision_id": str(revision_id),
        "review_commit_sha": "a" * 40,
        "review_path": campaign_revision_path(RUN_ID, revision_id),
    }
    assert "follow_up_body" not in database.receipt_result
    assert database.finalized["revision_id"] == revision_id


def test_email_workflows_are_explicit_and_campaign_is_approval_gated() -> None:
    definitions = {item.key: item for item in BUILTIN_WORKFLOWS}
    shortlist = definitions[EMAIL_SHORTLIST_WORKFLOW_NAME].definition
    campaign = definitions[EMAIL_CAMPAIGN_WORKFLOW_NAME].definition

    assert shortlist["executor"] == "codex.procedure"
    assert shortlist["procedure"]["output"]["path"] == "outreach/email/SHORTLIST.csv"
    assert campaign["executor"] == EMAIL_CAMPAIGN_WORKFLOW_NAME
    assert campaign["schedule_modes"] == ["on_demand"]
    assert campaign["human_review"]["eligible"] is True
    assert campaign["integration_requirements"] == [
        {
            "provider_key": "workspace.google",
            "capabilities": [
                "gmail.messages.send",
                "gmail.messages.read",
                "gmail.history.read",
            ],
            "required": True,
        }
    ]

    registered_names = {item.__temporal_workflow_definition.name for item in registered_workflows()}
    assert EMAIL_CAMPAIGN_WORKFLOW_NAME in registered_names
    assert "outreach.email_recipient" in registered_names
    assert EMAIL_CAMPAIGN_WORKFLOW_NAME in registered_workflow_implementations()

    migration = (Path(__file__).parents[1] / "migrations" / "014_email_outreach.sql").read_text()
    assert "CREATE TABLE outreach_campaigns" in migration
    assert "CREATE TABLE outreach_recipients" in migration
    assert "CREATE TABLE outreach_deliveries" in migration
    assert "subject text" not in migration
    assert "body text" not in migration
    assert "daily_send_cap" in migration
    assert "send_window_start" in migration
    assert "CREATE TABLE workspaces" not in migration
    assert "CREATE TABLE workflow_versions" not in migration


def test_campaign_delivery_projection_is_bounded_and_omits_provider_identifiers() -> None:
    root = Path(__file__).parents[1]
    database_source = (root / "src" / "tin_lite" / "db.py").read_text()
    api_source = (root / "src" / "tin_lite" / "api.py").read_text()

    projection = database_source.split("async def list_outreach_campaign_deliveries", maxsplit=1)[
        1
    ].split("async def get_pending_email_campaign_revision", maxsplit=1)[0]
    assert "delivery.campaign_run_id = $1" in projection
    assert "recipient.address AS recipient_address" in projection
    assert "delivery.step_key AS step" in projection
    assert "provider_message_id" not in projection
    assert "provider_request_id" not in projection
    assert '"/api/outreach/campaigns/{run_id}/deliveries"' in api_source
    assert 'response.headers["X-Tin-Read-Source"] = "postgres"' in api_source


def test_existing_campaigns_receive_their_missing_follow_up_schedule() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations" / "023_backfill_email_follow_up_schedule.sql"
    ).read_text()

    assert "delivery.step_key = 'follow_up'" in migration
    assert "delivery.status = 'pending'" in migration
    assert "delivery.scheduled_for IS NULL" in migration
    assert "recipient.initial_sent_at" in migration
    assert "campaign.follow_up_delay_days" in migration
    assert "UPDATE workflow_runs AS run" in migration
    assert "Waiting until " in migration


def test_campaign_stop_is_a_product_state_safety_boundary() -> None:
    database_source = (Path(__file__).parents[1] / "src" / "tin_lite" / "db.py").read_text()
    api_source = (Path(__file__).parents[1] / "src" / "tin_lite" / "api.py").read_text()

    assert "status NOT IN ('succeeded', 'stopped', 'superseded')" in database_source
    assert (
        'campaign_status"] not in {"approved", "running", "completed"}'
        in (Path(__file__).parents[1] / "src" / "tin_lite" / "activities.py").read_text()
    )
    assert "/api/workflows/runs/{run_id}/stop-email-campaign" in api_source


def test_campaign_revision_gates_delivery_and_keeps_an_immutable_approval_record() -> None:
    root = Path(__file__).parents[1]
    migration = (root / "migrations" / "016_email_campaign_revisions.sql").read_text()
    database_source = (root / "src" / "tin_lite" / "db.py").read_text()
    activity_source = (root / "src" / "tin_lite" / "activities.py").read_text()
    api_source = (root / "src" / "tin_lite" / "api.py").read_text()

    assert "CREATE TABLE outreach_campaign_revisions" in migration
    assert "outreach_campaign_one_pending_revision_idx" in migration
    assert "WHERE status = 'pending'" in migration
    assert "FOR UPDATE OF delivery, campaign" in database_source
    assert 'if row["revision_pending"]' in database_source
    assert "effective_follow_up_body" in activity_source
    assert "/api/outreach/campaigns/{run_id}/revisions" in api_source
    assert "/revisions/{revision_id}/approve" in api_source
    assert "/revisions/{revision_id}/discard" in api_source


def test_generic_artifact_download_does_not_force_markdown_media_type() -> None:
    api_source = (Path(__file__).parents[1] / "src" / "tin_lite" / "api.py").read_text()

    get_artifact_source = api_source.split("async def get_artifact(", maxsplit=1)[1].split(
        "async def get_artifact_document(", maxsplit=1
    )[0]
    assert "_file_media_type(filename)" in get_artifact_source
    assert 'media_type="text/markdown' not in get_artifact_source
