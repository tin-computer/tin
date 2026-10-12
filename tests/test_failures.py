"""A founder reads what they can fix; Tin's internals stay in the operator detail."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from tin_lite.billing_contracts import BillingError, usd_nanos
from tin_lite.failure_interceptor import _ClassifyingActivity
from tin_lite.failures import (
    BILLING_FOUNDER_TEXT,
    BILLING_INTERNAL_MESSAGE,
    INTERNAL_MESSAGE,
    PUBLIC_FALLBACK,
    TRANSIENT_MESSAGE,
    Failure,
    billing_public_message,
    billing_refusal,
    classify,
    classify_workflow_cause,
    failure_payload,
    failure_projection,
    from_details,
    public_error_text,
    receipt_failure,
    run_failure_view,
    split_failure_input,
)
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationInputError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
)
from tin_lite.mcp_errors import tool_error
from tin_lite.model_providers import ModelProviderError
from tin_lite.workflow_inputs import WorkflowInputError


def tin_value_error() -> ValueError:
    with pytest.raises(ValueError) as raised:
        usd_nanos("ten")
    return raised.value


def library_value_error() -> ValueError:
    with pytest.raises(ValueError) as raised:
        uuid.UUID("not-a-uuid")
    return raised.value


# --- classify -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind", "action"),
    [
        (IntegrationAuthorizationError("Reconnect X in this project"), "needs_you", "integrations"),
        (IntegrationNotConfiguredError("Connect Stripe first"), "needs_you", "integrations"),
        (IntegrationInputError("X timeline cursor is invalid"), "needs_you", "input"),
        (WorkflowInputError("Choose a start date"), "needs_you", "input"),
        (IntegrationUpstreamError("X returned an incomplete result"), "transient", None),
        (IntegrationRateLimitedError("X rate-limited this read"), "transient", None),
        (IntegrationDeliveryUnknownError("X may have posted it"), "explained", None),
    ],
)
def test_tin_integration_and_input_errors_keep_their_words(exc, kind, action) -> None:
    failure = classify(exc)
    assert (failure.kind, failure.action, failure.message) == (kind, action, str(exc))


def test_library_and_programming_errors_are_internal() -> None:
    for exc in (
        KeyError("rows"),
        AttributeError("'NoneType' object has no attribute 'x'"),
        json.JSONDecodeError("Expecting value", "", 0),
        library_value_error(),
        ModelProviderError("OpenAI model request failed"),
    ):
        failure = classify(exc)
        assert failure.kind == "internal", exc
        assert failure.message == INTERNAL_MESSAGE


def test_a_sentence_raised_in_tin_code_is_kept() -> None:
    failure = classify(tin_value_error())
    assert failure.kind == "explained"
    assert failure.message.startswith("Use a USD amount")


def test_network_errors_read_as_temporary() -> None:
    assert classify(TimeoutError()) == Failure("transient", TRANSIENT_MESSAGE)
    assert classify(ConnectionResetError("reset by 10.0.0.4")).message == TRANSIENT_MESSAGE


def test_tin_refusal_takes_the_kind_of_the_error_it_handled() -> None:
    try:
        try:
            raise IntegrationAuthorizationError("Reconnect X in this project")
        except IntegrationAuthorizationError:
            raise ApplicationError(
                "X voice extraction could not be confirmed. No replacement was purchased.",
                non_retryable=True,
            ) from None
    except ApplicationError as exc:
        failure = classify(exc)
    assert failure.kind == "needs_you"
    assert failure.action == "integrations"
    assert failure.message.startswith("X voice extraction could not be confirmed")


# --- billing ------------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(BILLING_FOUNDER_TEXT))
def test_founder_billing_codes_say_what_to_change(code) -> None:
    failure = billing_refusal(BillingError(code, "raw"))
    assert failure.kind == "needs_you"
    assert failure.action == "billing"
    assert failure.message == BILLING_FOUNDER_TEXT[code][1]
    assert "Billing" in failure.message


def test_billing_refusals_are_not_all_spending_limits() -> None:
    assert billing_refusal(BillingError("usage_pending", "x")).kind == "transient"
    internal = billing_refusal(BillingError("unpriced_model", "This model has no pinned price."))
    assert internal == Failure("internal", BILLING_INTERNAL_MESSAGE, code="unpriced_model")
    assert "limit" not in internal.message
    quote = billing_refusal(BillingError("stale_quote", "This quote changed or expired."))
    assert (quote.kind, quote.message) == ("explained", "This quote changed or expired.")


def test_billing_diagnostics_hide_internal_states_but_keep_amounts() -> None:
    limit = BillingError("project_limit", "Estimated at up to $4.00; the per-run limit is $2.00.")
    assert limit.diagnostic() == {"code": "project_limit", "message": str(limit)}
    internal = BillingError("unmetered_profile", "This execution profile is not billing-ready.")
    assert internal.diagnostic()["message"] == BILLING_INTERNAL_MESSAGE
    assert billing_public_message(internal) == BILLING_INTERNAL_MESSAGE


# --- activity interceptor -----------------------------------------------------------------


class Raising:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def init(self, outbound) -> None:  # pragma: no cover - not used by these tests
        pass

    async def execute_activity(self, input):
        raise self.exc


async def intercepted(exc: BaseException) -> ApplicationError:
    with pytest.raises(ApplicationError) as raised:
        await _ClassifyingActivity(Raising(exc)).execute_activity(SimpleNamespace())
    return raised.value


async def test_interceptor_keeps_type_and_text_and_adds_the_classification() -> None:
    error = await intercepted(KeyError("rows"))
    # Same as Temporal's own conversion, so retry policies match on the same type.
    assert (error.type, error.message, error.non_retryable) == ("KeyError", "'rows'", False)
    assert from_details(error) == Failure("internal", INTERNAL_MESSAGE)
    error = await intercepted(BillingError("insufficient_funds", "raw"))
    assert error.type == "BillingError"
    assert from_details(error).action == "billing"


async def test_interceptor_annotates_tin_refusals_and_leaves_detailed_ones() -> None:
    refusal = ApplicationError("Tin stopped this.", non_retryable=True)
    error = await intercepted(refusal)
    assert error.non_retryable is True
    assert from_details(error) == Failure("explained", "Tin stopped this.")
    typed = ApplicationError("The brief measured nothing.", type="BriefMeasuredNothing")
    error = await intercepted(typed)
    assert (error.type, error.message) == ("BriefMeasuredNothing", "The brief measured nothing.")
    assert from_details(error) == Failure("explained", "The brief measured nothing.")
    detailed = ApplicationError("x", {"attempt": 1}, type="OutputConflictError")
    assert await intercepted(detailed) is detailed


# --- workflow side ------------------------------------------------------------------------


def test_workflow_cause_uses_attached_details_then_safe_types() -> None:
    attached = ApplicationError(
        "raw text", Failure("needs_you", "Reconnect X").details(), type="KeyError"
    )
    failure, raw = classify_workflow_cause(attached)
    assert failure.message == "Reconnect X"
    assert raw == "KeyError: raw text"
    # Older histories carry no details: only Tin's own types keep their text.
    old_safe = ApplicationError("Reconnect X", type="IntegrationAuthorizationError")
    assert classify_workflow_cause(old_safe)[0].kind == "needs_you"
    old_leak = ApplicationError("duplicate key value", type="UniqueViolationError")
    failure, raw = classify_workflow_cause(old_leak)
    assert failure.message == INTERNAL_MESSAGE
    assert raw == "UniqueViolationError: duplicate key value"


# --- failure activities -------------------------------------------------------------------


def test_failure_projection_prefers_saved_then_specific_then_fallback() -> None:
    needs = {
        "run_id": "r",
        **failure_payload(Failure("needs_you", "Reconnect X", "integrations"), "d"),
    }
    internal = {"run_id": "r", **failure_payload(Failure("internal", INTERNAL_MESSAGE), "KeyError")}
    assert split_failure_input("r") == ("r", None)
    assert split_failure_input(needs)[0] == "r"
    assert failure_projection(None, "Fixed text.")["error_message"] == "Fixed text."
    shown = failure_projection(needs, "Fixed text.")
    assert (shown["error_message"], shown["error_kind"], shown["error_action"]) == (
        "Reconnect X",
        "needs_you",
        "integrations",
    )
    hidden = failure_projection(internal, "Fixed text.")
    assert (hidden["error_message"], hidden["error_detail"]) == ("Fixed text.", "KeyError")
    saved = failure_projection(needs, "Fixed text.", saved="X needs reconnecting first.")
    assert saved["error_message"] == "X needs reconnecting first."
    assert saved["error_action"] == "integrations"


def test_receipt_text_is_operator_text() -> None:
    assert receipt_failure("CodexAttemptStopped: Codex stopped at the run maximum.").kind == (
        "explained"
    )
    assert receipt_failure("ReadError: operation failed").message == TRANSIENT_MESSAGE
    assert receipt_failure("SandboxException: 502 from 10.1.2.3").message == INTERNAL_MESSAGE
    # A receipt sentence Tin wrote whole, without the "<Type>: " operator form.
    measured = "The analytics brief measured nothing: PostHog returned no events."
    assert receipt_failure(measured) == Failure("explained", measured)


def test_run_view_kind_needs_a_message() -> None:
    run = SimpleNamespace(error_message="Your Tin credits ran out.", error_kind="needs_you")
    run.error_action = "billing"
    assert run_failure_view(run)["error_fix"].startswith("Add credits")
    run.error_message = None
    assert run_failure_view(run) == {"error_kind": None, "error_fix": None}


# --- HTTP and MCP bodies ------------------------------------------------------------------


def test_public_error_text_passes_tin_sentences_only() -> None:
    assert public_error_text(tin_value_error()).startswith("Use a USD amount")
    assert public_error_text(library_value_error()) == PUBLIC_FALLBACK
    assert public_error_text(KeyError("schedule")) == PUBLIC_FALLBACK
    assert public_error_text(ModelProviderError("OpenAI model request failed")) == PUBLIC_FALLBACK
    assert public_error_text(LookupError("run not found")) == PUBLIC_FALLBACK
    assert public_error_text(WorkflowInputError("Choose a start date")) == "Choose a start date"


def test_mcp_masks_internal_runtime_errors_but_keeps_input_errors() -> None:
    assert str(tool_error(ModelProviderError("OpenAI model request failed"))) == (
        f"conflict: {PUBLIC_FALLBACK}"
    )
    assert str(tool_error(library_value_error())).startswith("invalid: badly formed")
    assert tool_error(KeyError("x")) is None
