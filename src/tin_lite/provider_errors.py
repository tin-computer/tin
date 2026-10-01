"""What a provider itself said when it refused a run's service call, cut and redacted.

Tin's gateway used to hand authors only its own sentence ("PostHog could not complete the
read (HTTP 400)."). That kept bodies and credentials out of runs, and it also hid the one
line an author needs to fix a call: PostHog's HogQL diagnostic, Google's INVALID_ARGUMENT
reason, Stripe's invalid_request_error. A ProviderErrorDetail keeps the HTTP status, the
provider's error type and code, and its message. Nothing else from the body is kept.

Every text field is made printable, redacted with `redaction.redact_message` (the call's own
credential, token and key shapes, credential assignments, the local part of email
addresses) and cut: the message to MESSAGE_LIMIT characters, type and code to FIELD_LIMIT.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tin_lite.redaction import redact_message

MESSAGE_LIMIT = 1500
FIELD_LIMIT = 100
_FIELDS = ("provider", "status", "type", "code", "message")


def _clean(value: Any, limit: int, secrets: Sequence[str]) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    text = redact_message(text, secrets=secrets)
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class ProviderErrorDetail:
    provider: str
    status: int | None = None
    type: str | None = None
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _FIELDS if getattr(self, name) is not None}

    @classmethod
    def from_dict(cls, value: Any) -> ProviderErrorDetail | None:
        """Read a detail back from a receipt; anything malformed reads as no detail."""
        if not isinstance(value, dict) or not isinstance(value.get("provider"), str):
            return None
        status = value.get("status")
        return cls(
            provider=value["provider"][:FIELD_LIMIT],
            status=status if type(status) is int else None,
            type=_clean(value.get("type"), FIELD_LIMIT, ()),
            code=_clean(value.get("code"), FIELD_LIMIT, ()),
            message=_clean(value.get("message"), MESSAGE_LIMIT, ()),
        )

    @property
    def said(self) -> str | None:
        """`type/code: message`, or None when the provider sent nothing beyond a status."""
        label = "/".join(part for part in (self.type, self.code) if part)
        if label and self.message:
            return f"{label}: {self.message}"
        return label or self.message


def detail(
    provider: str, status: int | None, body: Any, *, secrets: Sequence[str] = ()
) -> ProviderErrorDetail:
    """Pick the error type, code and message out of the JSON shapes providers send.

    - PostHog and DRF APIs: `{"type", "code", "detail", "attr"}`.
    - Stripe: `{"error": {"type", "code", "message", "param"}}`.
    - Google: `{"error": {"code": 400, "status": "INVALID_ARGUMENT", "message",
      "errors": [{"reason"}]}}`.
    - OAuth: `{"error": "invalid_grant", "error_description"}`.
    """
    kind = code = message = None
    if isinstance(body, dict):
        error = body.get("error")
        source = error if isinstance(error, dict) else body
        kind = source.get("type") if isinstance(source.get("type"), str) else source.get("status")
        code = source.get("code")
        if not isinstance(code, str):
            reasons = source.get("errors")
            first = reasons[0] if isinstance(reasons, list) and reasons else None
            code = first.get("reason") if isinstance(first, dict) else None
        if not isinstance(code, str) and isinstance(error, str):
            code = error
        for key in ("message", "detail", "error_description", "status_message"):
            if isinstance(source.get(key), str) and source[key].strip():
                message = source[key]
                break
    return ProviderErrorDetail(
        provider=provider,
        status=status,
        type=_clean(kind, FIELD_LIMIT, secrets),
        code=_clean(code, FIELD_LIMIT, secrets),
        message=_clean(message, MESSAGE_LIMIT, secrets),
    )


def explain(message: str, provider_error: ProviderErrorDetail | None) -> str:
    """Tin's own sentence, then what the provider said, when it said anything."""
    said = provider_error.said if provider_error is not None else None
    if not said:
        return message
    return f"{message.rstrip()} {provider_error.provider} said: {said}"
