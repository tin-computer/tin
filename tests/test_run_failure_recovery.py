"""Failures carry their reason; unattended runs get one silent retry after a passing failure."""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from tin_lite import activities as activities_module
from tin_lite import run_service
from tin_lite.activities import (
    TinActivities,
    failure_message,
    failure_record,
    transient_failure,
)
from tin_lite.billing_contracts import usd_nanos
from tin_lite.domain import RunStatus, WorkflowRun
from tin_lite.failures import INTERNAL_MESSAGE, Failure
from tin_lite.workflows import failure_fields, failure_reason

ROOT = Path(__file__).resolve().parents[1]


# --- failure_message -----------------------------------------------------------------------


def test_failure_message_prefers_the_reason_then_other_keys() -> None:
    assert failure_message({"reason": "Boom", "error": "x"}, restarted=False) == "Boom"
    assert failure_message({"error": "  E2B envd stream reset  "}, restarted=False) == (
        "E2B envd stream reset"
    )
    assert failure_message({"message": "m"}, restarted=False) == "m"
    assert failure_message({"detail": "d"}, restarted=False) == "d"


def test_failure_message_falls_back_to_the_exception_then_the_default() -> None:
    # Tin's own sentence: a ValueError raised inside a tin_lite module.
    with pytest.raises(ValueError) as raised:
        usd_nanos("ten")
    assert failure_message({}, raised.value, restarted=False) == (
        "Use a USD amount with at most two decimal places, such as 10.00."
    )
    # A library's own text (a bare KeyError, a parser error) is internal, never the founder's.
    internal = failure_message({}, KeyError("rows"), restarted=False)
    assert internal == INTERNAL_MESSAGE
    assert failure_message({"reason": ""}, ValueError(), restarted=False) == INTERNAL_MESSAGE
    assert failure_message(None, restarted=False) == "workflow failed"
    assert failure_message({}, default="project task failed", restarted=False) == (
        "project task failed"
    )


def test_failure_record_keeps_the_raw_cause_for_operators_only() -> None:
    record = failure_record({}, KeyError("rows"), restarted=False)
    assert record == {
        "error_message": INTERNAL_MESSAGE,
        "error_kind": "internal",
        "error_action": None,
        "error_detail": "KeyError: 'rows'",
    }
    carried = failure_record(
        {
            "run_id": "r",
            "reason": "Reconnect X in this project",
            "kind": "needs_you",
            "action": "integrations",
            "detail": "IntegrationAuthorizationError: Reconnect X in this project",
        },
        restarted=False,
    )
    assert carried["error_message"] == "Reconnect X in this project"
    assert carried["error_kind"] == "needs_you"
    assert carried["error_action"] == "integrations"


def test_failure_message_truncates_and_marks_a_fresh_restart() -> None:
    long = failure_message({"reason": "x" * 2000}, restarted=False)
    assert len(long) == 600
    marked = failure_message({"reason": "ActivityError: Bad Gateway"}, restarted=True)
    assert marked == "ActivityError: Bad Gateway (Tin had just restarted)"
    assert failure_message({"reason": marked}, restarted=True) == marked
    assert len(failure_message({"reason": "x" * 2000}, restarted=True)) == 600


def test_failure_message_reads_the_process_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activities_module, "process_uptime_seconds", lambda: 15.0)
    assert failure_message({"reason": "r"}).endswith("(Tin had just restarted)")
    monkeypatch.setattr(activities_module, "process_uptime_seconds", lambda: 900.0)
    assert failure_message({"reason": "r"}) == "r"


# --- failure_reason (workflow side) -------------------------------------------------------


def activity_error(cause: BaseException) -> ActivityError:
    outer = ActivityError(
        "Activity task failed",
        scheduled_event_id=5,
        started_event_id=6,
        identity="worker",
        activity_type="generate_visibility_audit",
        activity_id="1",
        retry_state=None,
    )
    outer.__cause__ = cause
    return outer


def test_failure_reason_keeps_tin_words_and_hides_internal_text() -> None:
    # Tin's own refusal (no type) is kept as written.
    refusal = ApplicationError("The audit target domain is invalid.", non_retryable=True)
    assert failure_reason(activity_error(refusal)) == "The audit target domain is invalid."
    # A converted library error is internal; its text is the operator detail only.
    leaked = ApplicationError("'NoneType' object has no attribute 'x'", type="AttributeError")
    fields = failure_fields(activity_error(leaked))
    assert fields["reason"] == INTERNAL_MESSAGE
    assert fields["kind"] == "internal"
    assert fields["detail"] == "AttributeError: 'NoneType' object has no attribute 'x'"
    # An activity's interceptor classification wins over its type.
    carried = ApplicationError(
        "Reconnect X in this project",
        Failure("needs_you", "Reconnect X in this project", action="integrations").details(),
        type="IntegrationAuthorizationError",
    )
    fields = failure_fields(activity_error(carried))
    assert (fields["reason"], fields["kind"], fields["action"]) == (
        "Reconnect X in this project",
        "needs_you",
        "integrations",
    )
    timeout = TimeoutError(
        "activity Heartbeat timeout", type=TimeoutType.HEARTBEAT, last_heartbeat_details=[]
    )
    fields = failure_fields(activity_error(timeout))
    assert fields["kind"] == "transient"
    assert fields["detail"] == "TimeoutError: activity Heartbeat timeout"
    assert failure_reason(RuntimeError("")) == INTERNAL_MESSAGE
    assert len(failure_reason(ApplicationError("z" * 1000))) == 600


# --- transient classifier -----------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "HTTPStatusError: 502 Bad Gateway from https://lite.tin.computer/runs/tools",
        "ConnectError: connection refused",
        "ConnectionResetError: Connection reset by peer",
        "Connection error while calling the run tools",
        "ReadTimeout: request timed out",
        "TimeoutException: envd stream timed out",
        "Service Unavailable (503)",
        "gateway timeout",
    ],
)
def test_transient_failures_match(message: str) -> None:
    assert transient_failure(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "VisibilityProtocolError: visibility target domain is invalid",
        "ActivityError: workflow failed",
        "Prerequisite missing: run the audit first",
        "error 5020 in the model reply",
        "",
        None,
    ],
)
def test_lasting_failures_do_not_match(message: str | None) -> None:
    assert transient_failure(message) is False


def test_a_fresh_restart_is_transient_whatever_the_text() -> None:
    assert transient_failure("VisibilityProtocolError: whatever", restarted=True) is True


# --- one automatic retry ------------------------------------------------------------------


def make_run(**overrides) -> WorkflowRun:
    values = dict(
        id=uuid4(),
        project_id=uuid4(),
        workflow_id=uuid4(),
        executor="visibility.audit",
        definition_commit_sha="abc123",
        temporal_workflow_id="visibility.audit:x",
        thread_id="thread",
        generation=1,
        fencing_token=1,
        status=RunStatus.FAILED,
        project_workflow_id=uuid4(),
        trigger_source="schedule",
        started_by_clerk_user_id=None,
        input={"target": "this project", "project_id": "p"},
    )
    values.update(overrides)
    return WorkflowRun(**values)


class RetryDatabase:
    def __init__(self, run: WorkflowRun, *, start_key: str | None = None) -> None:
        self.run = run
        self.start_key = start_key
        self.failures: list[str] = []
        self.retries: list[dict] = []
        self.runs_by_start_key: dict[str, WorkflowRun] = {}
        self.configured = SimpleNamespace(
            id=run.project_workflow_id,
            project_id=run.project_id,
            inputs={"target": "this project"},
            definition_commit_sha="abc123",
            input_schema={"type": "object"},
            created_by_clerk_user_id="user_owner",
        )

    async def project_failure(self, *, run_id: UUID, error_message: str, **_) -> None:
        assert run_id == self.run.id
        self.failures.append(error_message)

    async def get_run(self, run_id: UUID) -> WorkflowRun | None:
        return self.run if run_id == self.run.id else None

    async def run_start_idempotency_key(self, run_id: UUID) -> str | None:
        return self.start_key

    async def get_run_by_start_key(self, *, project_id: UUID, start_idempotency_key: str):
        return self.runs_by_start_key.get(start_idempotency_key)

    async def get_workflow(self, workflow_id: UUID):
        return SimpleNamespace(id=workflow_id, project_id=None, executor="visibility.audit")

    async def get_project_workflow(self, project_workflow_id: UUID):
        return self.configured if project_workflow_id == self.configured.id else None

    async def record_run_auto_retry(self, **values) -> None:
        self.retries.append(values)


def build(database: RetryDatabase, *, temporal: object | None = object()) -> TinActivities:
    return TinActivities(
        database=database,
        storage=object(),
        sandboxes=object(),
        settings=SimpleNamespace(),
        temporal=temporal,
    )


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_start(**kwargs):
        calls.append(kwargs)
        retry = make_run(
            id=uuid4(),
            project_id=kwargs["project_id"],
            status=RunStatus.PENDING,
            retry_of_run_id=kwargs["retry_of_run_id"],
        )
        kwargs["runtime"].database.runs_by_start_key[kwargs["start_idempotency_key"]] = retry
        return retry

    monkeypatch.setattr(run_service, "start_workflow_run", fake_start)
    return calls


async def test_scheduled_run_gets_exactly_one_retry_after_a_transient_failure(
    started: list[dict],
) -> None:
    run = make_run()
    database = RetryDatabase(run)
    payload = {"run_id": str(run.id), "reason": "HTTPStatusError: 502 Bad Gateway"}

    await build(database).project_visibility_failure(payload)
    # Temporal re-runs the failure activity on a flaky ack; the second pass must not add a run.
    await build(database).project_visibility_failure(payload)

    assert database.failures == ["HTTPStatusError: 502 Bad Gateway"] * 2
    assert len(started) == 1
    call = started[0]
    assert call["retry_of_run_id"] == run.id
    assert call["start_idempotency_key"] == f"auto-retry:{run.id}"
    assert call["trigger_source"] == "schedule"
    assert call["project_workflow_id"] == run.project_workflow_id
    assert call["input_payload"] == {"target": "this project"}
    assert call["started_by_clerk_user_id"] == "user_owner"
    assert call["runtime"].temporal is not None
    assert len(database.retries) == 1
    assert database.retries[0]["run_id"] == run.id
    assert database.retries[0]["restarted_recently"] is False


async def test_a_retry_run_is_never_retried_again(started: list[dict]) -> None:
    run = make_run(retry_of_run_id=uuid4())
    database = RetryDatabase(run)
    await build(database).project_visibility_failure(
        {"run_id": str(run.id), "reason": "ConnectError: connection refused"}
    )
    assert database.failures == ["ConnectError: connection refused"]
    assert started == []
    assert database.retries == []


async def test_lasting_failures_and_manual_runs_are_not_retried(started: list[dict]) -> None:
    lasting = make_run()
    database = RetryDatabase(lasting)
    await build(database).project_visibility_failure(
        {"run_id": str(lasting.id), "reason": "VisibilityProtocolError: target domain is invalid"}
    )
    manual = make_run(trigger_source="manual", started_by_clerk_user_id="user_1")
    manual_database = RetryDatabase(manual)
    await build(manual_database).project_visibility_failure(
        {"run_id": str(manual.id), "reason": "HTTPStatusError: 503 Service Unavailable"}
    )
    assert started == []
    assert database.retries == [] and manual_database.retries == []


async def test_onboarding_setup_runs_retry_after_a_restart(
    started: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activities_module, "process_uptime_seconds", lambda: 40.0)
    run = make_run(
        trigger_source="mcp",
        trigger_client="claude_code",
        started_by_clerk_user_id="user_1",
        project_workflow_id=None,
    )
    database = RetryDatabase(run, start_key=f"onboarding:{uuid4()}:audit")
    await build(database).project_visibility_failure(
        {"run_id": str(run.id), "reason": "VisibilityProtocolError: panel is incomplete"}
    )
    assert database.failures == [
        "VisibilityProtocolError: panel is incomplete (Tin had just restarted)"
    ]
    assert len(started) == 1
    assert started[0]["input_payload"] == {"target": "this project"}
    assert started[0]["project_workflow_id"] is None
    assert started[0]["started_by_clerk_user_id"] == "user_1"
    assert started[0]["trigger_client"] == "claude_code"
    assert database.retries[0]["restarted_recently"] is True


async def test_no_temporal_client_means_no_retry(started: list[dict]) -> None:
    run = make_run()
    database = RetryDatabase(run)
    await build(database, temporal=None).project_visibility_failure(
        {"run_id": str(run.id), "reason": "502 Bad Gateway"}
    )
    assert database.failures == ["502 Bad Gateway"]
    assert started == []


async def test_a_retry_that_cannot_start_leaves_the_failure_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(**kwargs):
        raise RuntimeError("the selected failed run cannot be retried")

    monkeypatch.setattr(run_service, "start_workflow_run", refuse)
    run = make_run()
    database = RetryDatabase(run)
    await build(database).project_visibility_failure(
        {"run_id": str(run.id), "reason": "504 Gateway Timeout"}
    )
    assert database.failures == ["504 Gateway Timeout"]
    assert database.retries == []


# --- sandbox: studio voice POST backs off through a switchboard restart ---------------------


def load_voice():
    spec = importlib.util.spec_from_file_location(
        "tin_sandbox_voice", ROOT / "sandbox" / "studio" / "voice.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_backoff_delays_double_and_stay_inside_two_minutes() -> None:
    voice = load_voice()
    delays = voice.backoff_delays()
    assert delays[:5] == [1, 2, 4, 8, 16]
    assert max(delays) <= 30
    assert 90 <= sum(delays) <= 120


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_voice_post_retries_gateway_errors_then_succeeds() -> None:
    voice = load_voice()
    attempts: list[int] = []
    slept: list[int] = []

    def opener(req, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"down"))
        if len(attempts) == 2:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if len(attempts) == 3:
            raise ConnectionResetError("Connection reset by peer")
        return Response(b'{"audio": "ok"}')

    req = urllib.request.Request("https://switchboard.test/studio/voice", data=b"{}")
    result = voice.post_with_backoff(req, opener=opener, sleep=slept.append)
    assert result == {"audio": "ok"}
    assert len(attempts) == 4
    assert slept == [1, 2, 4]


def test_voice_post_gives_up_after_the_window_and_refuses_lasting_errors() -> None:
    voice = load_voice()
    slept: list[int] = []
    req = urllib.request.Request("https://switchboard.test/studio/voice", data=b"{}")

    def always_down(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))

    with pytest.raises(voice.VoiceRefused, match="503"):
        voice.post_with_backoff(req, opener=always_down, sleep=slept.append, delays=[1, 2])
    assert slept == [1, 2]

    def refused(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 422, "Unprocessable", {}, io.BytesIO(b"bad text")
        )

    slept.clear()
    with pytest.raises(voice.VoiceRefused, match="422"):
        voice.post_with_backoff(req, opener=refused, sleep=slept.append)
    assert slept == []


def test_failure_text_masks_credentials() -> None:
    from tin_lite.activities import failure_message, scrub_secrets

    leaky = (
        "HTTP 401 for https://api.example.com/v1?api_key=sk_live_abc123456789 with header "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop and token "
        "re_abcdefghijklmnop"
    )
    scrubbed = scrub_secrets(leaky)
    assert "sk_live_abc123456789" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "re_abcdefghijklmnop" not in scrubbed
    assert "HTTP 401 for https://api.example.com/v1?api_key=[redacted]" in scrubbed
    assert "[redacted]" in failure_message({"reason": leaky}, restarted=False)
