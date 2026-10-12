"""What a founder reads when something stops, kept apart from what an operator needs.

Every failure gets one of four kinds:

- `needs_you`: the founder can fix it (reconnect, add credits, raise a limit, change an
  input). Tin's own words are kept, and `action` names where to go.
- `transient`: nothing to change; trying again later is the fix.
- `explained`: Tin wrote the message on purpose (a non-retryable ApplicationError). Kept.
- `internal`: anything else. The founder reads one plain sentence; the raw text stays in
  logs, receipts and the run's operator-only `error_detail`.

Exceptions are matched by class name along the MRO so this module imports nothing heavy
and is safe inside the workflow sandbox.
"""

from __future__ import annotations

import os
import sysconfig
from dataclasses import asdict, dataclass
from typing import Any, Literal

from temporalio.exceptions import ApplicationError, FailureError

from tin_lite.redaction import scrub_secrets

Kind = Literal["needs_you", "transient", "explained", "internal"]
KINDS = ("needs_you", "transient", "explained", "internal")
DETAIL_LIMIT = 2000
MESSAGE_LIMIT = 600

INTERNAL_MESSAGE = (
    "Tin hit a problem on its side. Nothing you set up caused it. Try again; "
    "if it happens again, tell us."
)
TRANSIENT_MESSAGE = "A service Tin depends on did not answer in time. Try again in a few minutes."
STEP_TIMEOUT_MESSAGE = "A step ran past its time limit and was stopped. Try again."

# Founder wording for the billing refusals a founder can act on. Every other BillingError
# code is Tin's own state (pricing, ledgers, pins) and reads as internal.
BILLING_FOUNDER_TEXT = {
    "project_limit": (
        "billing",
        "This project's spending limit stopped the run. Raise the limit in Billing, then retry.",
    ),
    "run_limit": (
        "billing",
        "The run reached its spending maximum. Raise the per-run limit in Billing, then retry.",
    ),
    "insufficient_funds": (
        "billing",
        "Your Tin credits ran out. Add credits in Billing, then retry.",
    ),
    "schedule_not_funded": (
        "billing",
        "This schedule has no spending limit for unattended runs. Set one in Billing.",
    ),
}
BILLING_TRANSIENT_TEXT = {
    "usage_pending": "An earlier paid step is still settling. Try again in a few minutes.",
}
BILLING_LIMIT_CODES = frozenset({"project_limit", "run_limit"})
# Tin's own pricing, ledger and pin states: the founder cannot change any of them.
BILLING_INTERNAL_CODES = frozenset(
    {
        "unmetered_profile",
        "unmetered_workflow",
        "unmetered_operation",
        "unpriced_model",
        "invalid_budget",
        "invalid_child_budget",
        "operation_already_attempted",
        "ledger_conflict",
        "spending_stopped",
        "request_limit",
        "token_limit",
    }
)
BILLING_INTERNAL_MESSAGE = (
    "Tin could not price or charge this step. This is on Tin's side; nothing more was "
    "charged. Try again, and if it happens again, tell us."
)

# Tin's own exception classes whose text is written for the person reading it.
_NEEDS_YOU = {
    "IntegrationAuthorizationError": "integrations",
    "IntegrationNotConfiguredError": "integrations",
    "IntegrationInputError": "input",
    "WorkflowInputError": "input",
    "PrerequisiteError": "prerequisite",
}
_TRANSIENT = (
    "IntegrationRateLimitedError",
    "IntegrationUpstreamError",
    "ServiceCallRefused",
    "ServiceResponseTooLarge",
)
# Tin-authored text that is neither founder-fixable nor retryable as is.
_EXPLAINED = (
    "IntegrationDeliveryUnknownError",
    "IntegrationDeliveryRefusedError",
    "IntegrationError",
    "CodexAttemptStopped",
    "OutputConflictError",
    "StudioError",
)
_NETWORK = (
    "TimeoutError",
    "ConnectionError",
    "TransportError",
    "TimeoutException",
    "APITimeoutError",
    "APIConnectionError",
    # httpx transport classes, which a receipt names without its MRO.
    "ReadError",
    "WriteError",
    "ConnectError",
    "ReadTimeout",
    "WriteTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
)


@dataclass(frozen=True)
class Failure:
    kind: Kind
    message: str
    action: str | None = None
    code: str | None = None

    def details(self) -> dict[str, Any]:
        """The small control fact an ApplicationError carries to the workflow."""
        return {"tin_failure": {k: v for k, v in asdict(self).items() if v is not None}}


# Where a needs_you failure is fixed, for MCP clients and the dashboard.
FIX_HINTS = {
    "integrations": "Connect or reconnect the integration under Integrations, then retry.",
    "billing": "Add credits or raise the project's spending limits under Billing, then retry.",
    "input": "Change the workflow's inputs, then start it again.",
    "prerequisite": "Finish the prerequisite the error names, then start it again.",
}


def run_failure_view(run: Any) -> dict[str, Any]:
    """The kind and fix for a failed run's error, for HTTP and MCP readers."""
    if not getattr(run, "error_message", None) or getattr(run, "error_kind", None) not in KINDS:
        return {"error_kind": None, "error_fix": None}
    kind = run.error_kind
    fix = FIX_HINTS.get(getattr(run, "error_action", None) or "") if kind == "needs_you" else None
    if kind == "transient":
        fix = "Nothing to change; start it again."
    return {"error_kind": kind, "error_fix": fix}


def _names(exc: BaseException) -> list[str]:
    return [cls.__name__ for cls in type(exc).__mro__]


def _text(value: object, limit: int = MESSAGE_LIMIT) -> str:
    return scrub_secrets(" ".join(str(value or "").split()))[:limit]


def detail(exc: BaseException) -> str:
    """Operator-only: the exception's class and scrubbed text."""
    text = _text(exc, DETAIL_LIMIT)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def billing_refusal(exc: BaseException) -> Failure:
    code = str(getattr(exc, "code", "") or "") or None
    if code in BILLING_FOUNDER_TEXT:
        action, message = BILLING_FOUNDER_TEXT[code]
        return Failure("needs_you", message, action=action, code=code)
    if code in BILLING_TRANSIENT_TEXT:
        return Failure("transient", BILLING_TRANSIENT_TEXT[code], code=code)
    if code in BILLING_INTERNAL_CODES or not _text(exc):
        return Failure("internal", BILLING_INTERNAL_MESSAGE, code=code)
    # Quote, enrollment and form refusals: billing.py's own sentence says what happened.
    return Failure("explained", _text(exc), code=code)


def billing_public_message(exc: BaseException) -> str:
    """An HTTP or MCP body for a billing refusal. billing.py's own sentences, which name the
    limit and amounts, are kept; internal pricing and ledger states are not shown."""
    code = getattr(exc, "code", None)
    if code in BILLING_INTERNAL_CODES or not _text(exc):
        return BILLING_INTERNAL_MESSAGE
    return _text(exc)


def is_spending_limit(exc: BaseException) -> bool:
    return getattr(exc, "code", None) in BILLING_LIMIT_CODES


def classify(exc: BaseException) -> Failure:
    """The founder-safe failure for an exception raised in Tin's own process."""
    carried = from_details(exc)
    if carried is not None:
        return carried
    names = _names(exc)
    if "BillingError" in names:
        return billing_refusal(exc)
    for name in names:
        if name in _NEEDS_YOU:
            code = getattr(exc, "code", None)
            return Failure(
                "needs_you",
                _text(exc) or INTERNAL_MESSAGE,
                action=_NEEDS_YOU[name],
                code=code if isinstance(code, str) else None,
            )
        if name in _TRANSIENT:
            code = getattr(exc, "code", None)
            return Failure(
                "transient",
                _text(exc) or TRANSIENT_MESSAGE,
                code=code if isinstance(code, str) else None,
            )
        if name in _EXPLAINED:
            return Failure("explained", _text(exc) or INTERNAL_MESSAGE)
    if isinstance(exc, ApplicationError):
        # Tin's own refusal: in Tin's process only Tin code raises ApplicationError, typed
        # (such as AnalyticsBriefMeasuredNothing) or not. When it was raised while handling
        # a founder-fixable error, keep its words and take the cause's kind and action.
        message = _text(exc.message) or INTERNAL_MESSAGE
        for cause in (exc.__cause__, exc.__context__):
            if cause is not None and not isinstance(cause, ApplicationError):
                inner = classify(cause)
                if inner.kind in ("needs_you", "transient"):
                    return Failure(inner.kind, message, action=inner.action, code=inner.code)
                break
        return Failure("explained", message)
    if any(name in _NETWORK for name in names):
        return Failure("transient", TRANSIENT_MESSAGE)
    if tin_sentence(exc):
        return Failure("explained", _text(exc))
    return Failure("internal", INTERNAL_MESSAGE)


def tin_sentence(exc: BaseException) -> bool:
    """A plain ValueError, LookupError or RuntimeError that Tin's own code raised with a
    message, such as "The project changed while the plan was being prepared." The same
    classes raised inside the standard library or an installed package (a UUID or JSON
    parser, a driver) describe internals."""
    names = _names(exc)
    if not any(name in ("ValueError", "LookupError", "RuntimeError") for name in names):
        return False
    if any(name in _LEAKY for name in names) or not _text(exc):
        return False
    tb = exc.__traceback__
    if tb is None:
        return False
    while tb.tb_next is not None:
        tb = tb.tb_next
    # Raised by Tin's package (wherever it is installed) or other code outside the standard
    # library and installed packages, such as a test double.
    if str(tb.tb_frame.f_globals.get("__name__", "")).startswith("tin_lite"):
        return True
    filename = os.path.realpath(tb.tb_frame.f_code.co_filename)
    return not filename.startswith(_LIBRARY_ROOTS)


_LIBRARY_ROOTS = tuple(
    os.path.realpath(path) + os.sep
    for name in ("stdlib", "platstdlib", "purelib", "platlib")
    if (path := sysconfig.get_paths().get(name))
)


def from_details(exc: BaseException) -> Failure | None:
    """The classification an activity attached to its ApplicationError, if any."""
    if not isinstance(exc, ApplicationError):
        return None
    for item in exc.details or ():
        carried = item.get("tin_failure") if isinstance(item, dict) else None
        if isinstance(carried, dict) and carried.get("kind") in KINDS:
            message = carried.get("message")
            if isinstance(message, str) and message.strip():
                action, code = carried.get("action"), carried.get("code")
                return Failure(
                    carried["kind"],
                    message[:MESSAGE_LIMIT],
                    action=action if isinstance(action, str) else None,
                    code=code if isinstance(code, str) else None,
                )
    return None


# Converted Python exceptions arrive at the workflow as ApplicationError(type=<class name>).
# Histories from before the activity interceptor carry no details, so only these names keep
# their text there.
_SAFE_TYPES = frozenset({"BillingError", *(_NEEDS_YOU), *_TRANSIENT, *_EXPLAINED})


def _type_kind(name: str) -> Kind:
    if name == "BillingError" or name in _NEEDS_YOU:
        return "needs_you"
    return "transient" if name in _TRANSIENT else "explained"


def classify_workflow_cause(cause: BaseException) -> tuple[Failure, str]:
    """Inside a workflow: the founder-safe failure and the operator detail for a cause."""
    carried = from_details(cause)
    if isinstance(cause, ApplicationError):
        label = cause.type or "ApplicationError"
        raw = _text(cause.message, DETAIL_LIMIT)
        if carried is not None:
            return carried, f"{label}: {raw}" if raw else label
        if cause.type is None:
            return Failure("explained", _text(cause.message) or INTERNAL_MESSAGE), label
        if cause.type in _SAFE_TYPES and raw:
            failure = Failure(
                _type_kind(cause.type), raw[:MESSAGE_LIMIT], action=_NEEDS_YOU.get(cause.type)
            )
            return failure, label
        if cause.type in _NETWORK:
            return Failure("transient", TRANSIENT_MESSAGE), f"{label}: {raw}"
        return Failure("internal", INTERNAL_MESSAGE), f"{label}: {raw}" if raw else label
    if type(cause).__name__ == "TimeoutError" and isinstance(cause, FailureError):
        # Temporal's own timeout: a step outran its start-to-close or heartbeat limit.
        return Failure("transient", STEP_TIMEOUT_MESSAGE), detail(cause)
    if isinstance(cause, FailureError):
        return Failure("internal", INTERNAL_MESSAGE), detail(cause)
    failure = classify(cause)
    return failure, detail(cause)


def receipt_failure(text: str | None) -> Failure | None:
    """A failed receipt stores "<Type>: <text>" for operators. Founder text comes from it only
    for Tin's own types; anything else reads as internal."""
    if not isinstance(text, str) or not text.strip():
        return None
    label, separator, rest = text.partition(": ")
    if not separator or not label.isidentifier():
        # Not "<Type>: <text>" (`_safe_failure`'s operator form): a sentence Tin wrote for
        # the receipt itself, such as an analytics brief that measured nothing.
        return Failure("explained", _text(text))
    rest = _text(rest)
    if label == "BillingError":
        return Failure("explained", rest) if rest else None
    if label == "ApplicationError" and rest:
        # Only Tin's own code raises ApplicationError in Tin's process.
        return Failure("explained", rest)
    if label in _SAFE_TYPES and rest:
        return Failure(_type_kind(label), rest, action=_NEEDS_YOU.get(label))
    if label in _NETWORK:
        return Failure("transient", TRANSIENT_MESSAGE)
    return Failure("internal", INTERNAL_MESSAGE)


def failure_payload(failure: Failure, raw: str) -> dict[str, Any]:
    """What a workflow hands its failure activity: founder text, kind and operator detail."""
    payload: dict[str, Any] = {
        "reason": failure.message,
        "kind": failure.kind,
        "detail": raw[:DETAIL_LIMIT],
    }
    if failure.action:
        payload["action"] = failure.action
    if failure.code:
        payload["code"] = failure.code
    return payload


def payload_failure(payload: Any) -> Failure | None:
    """The Failure a workflow passed to a failure activity, if it passed one."""
    if not isinstance(payload, dict) or payload.get("kind") not in KINDS:
        return None
    message = payload.get("reason")
    if not isinstance(message, str) or not message.strip():
        return None
    action, code = payload.get("action"), payload.get("code")
    return Failure(
        payload["kind"],
        message.strip()[:MESSAGE_LIMIT],
        action=action if isinstance(action, str) else None,
        code=code if isinstance(code, str) else None,
    )


def split_failure_input(value: Any) -> tuple[str, dict[str, Any] | None]:
    """A failure activity's argument: a run ID (older histories) or, since the workflow's
    failure-reason-v1 patch, a dict with the run ID and the classified cause."""
    if isinstance(value, dict):
        return str(value["run_id"]), value
    return str(value), None


def failure_projection(
    passed: dict[str, Any] | None, fallback: str, *, saved: str | None = None
) -> dict[str, Any]:
    """`project_failure` keyword arguments for a failure activity with its own wording.

    Order: a specific message the run saved earlier (`saved`), then the passed cause when it
    says more than the fallback (founder-fixable, temporary or Tin's own words), else the
    fallback. The passed operator detail is kept whichever message wins.
    """
    failure = payload_failure(passed)
    raw = passed.get("detail") if isinstance(passed, dict) else None
    projection: dict[str, Any] = {"error_detail": raw if isinstance(raw, str) else None}
    if saved:
        fixable = failure is not None and failure.kind in ("needs_you", "transient")
        projection.update(
            error_message=saved,
            error_kind=failure.kind if fixable else "explained",
            error_action=failure.action if fixable else None,
        )
    elif failure is not None and failure.kind != "internal":
        projection.update(
            error_message=failure.message,
            error_kind=failure.kind,
            error_action=failure.action,
        )
    else:
        projection.update(
            error_message=fallback,
            error_kind="internal" if failure is not None else None,
            error_action=None,
        )
    return projection


def public_error_text(exc: BaseException, fallback: str | None = None) -> str:
    """Text for an HTTP or MCP error body. Tin's deliberate ValueError, LookupError and
    RuntimeError messages pass; parser, driver, provider and key errors do not."""
    names = _names(exc)
    if "BillingError" in names:
        return billing_public_message(exc)
    if "ValidationError" in names and "pydantic" in type(exc).__module__:
        return fallback or _pydantic_summary(exc)
    if any(name in _LEAKY for name in names) or (
        names[0] in ("ValueError", "LookupError", "RuntimeError") and not tin_sentence(exc)
    ):
        return fallback or PUBLIC_FALLBACK
    return _text(exc) or (fallback or PUBLIC_FALLBACK)


PUBLIC_FALLBACK = "Tin could not complete this request. Nothing you sent caused it."


# Builtin and library errors whose text describes Tin's internals rather than the request.
_LEAKY = frozenset(
    {
        "KeyError",
        "IndexError",
        "AttributeError",
        "TypeError",
        "JSONDecodeError",
        "UnicodeError",
        "NotImplementedError",
        "PostgresError",
        "InterfaceError",
        "ModelProviderError",
        "CodeExecutionError",
        "TemporalStartError",
        "RPCError",
        "HTTPError",
        "TransportError",
    }
)


def _pydantic_summary(exc: BaseException) -> str:
    errors = getattr(exc, "errors", None)
    try:
        items = errors(include_url=False) if callable(errors) else []
    except Exception:  # pragma: no cover - errors() is part of pydantic's API
        items = []
    parts = []
    for item in items[:3]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "input"
        parts.append(f"{location}: {item.get('msg', 'invalid')}")
    return ("Invalid input: " + "; ".join(parts))[:MESSAGE_LIMIT] if parts else "Invalid input."
