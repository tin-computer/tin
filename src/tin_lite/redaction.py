"""Text redaction Tin applies before text leaves the trusted side.

One home for the patterns that rollout capture, run failure messages and provider errors
share: exact secrets the caller knows, token and key shapes, credential-looking query and
header values, and (for provider text) email addresses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

REDACTED = "[redacted]"

TOKEN_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    # JSON Web Tokens (ChatGPT access/id tokens, Clerk, code.storage).
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # HTTP authorization values; keep the scheme so the transcript stays readable.
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), rf"\1 {REDACTED}"),
    (re.compile(r"(?i)\b(Basic)\s+[A-Za-z0-9+/=]{16,}"), rf"\1 {REDACTED}"),
    # Provider key shapes.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),
    # Key-named JSON values, including the escaped form seen inside tool output strings
    # (for example an echoed ``auth.json``).
    (
        re.compile(
            r'(\\?")(access_token|refresh_token|id_token|api_key|password|'
            r'OPENAI_API_KEY|CODEX_API_KEY)(\\?"\s*:\s*\\?")[^"\\]{8,}'
        ),
        rf"\1\2\3{REDACTED}",
    ),
    # Environment dumps.
    (
        re.compile(
            r"\b(TIN_BROKER_GRANT|TIN_RUN_TOOLS_GRANT|TIN_CANONICAL_AUTH_HEADER|"
            r"TIN_EPHEMERAL_AUTH_HEADER|HTTPS?_PROXY|https?_proxy)=\S+"
        ),
        rf"\1={REDACTED}",
    ),
)

_SECRET_IN_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password|authorization)=)[^\s&\"']+"
    r"|\b(?:sk|rk|re|ghp|gho|ghu|pat|xoxb|xoxp)_[A-Za-z0-9_-]{8,}\b"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)

# A whole address only: `'%@example.com'` (a LIKE pattern) has no local part and stays.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._+-])[A-Za-z0-9._+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def redact_text(value: str, secrets: Sequence[str]) -> tuple[str, int]:
    """Replace every exact secret (and its JSON-escaped form) with ``[redacted]``."""
    count = 0
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        for needle in {secret, json.dumps(secret, ensure_ascii=False)[1:-1]}:
            if needle and needle in value:
                count += value.count(needle)
                value = value.replace(needle, REDACTED)
    return value, count


def redact_token_shapes(value: str) -> tuple[str, int]:
    """Replace token, key and credential-assignment shapes; returns the text and a count."""
    count = 0
    for pattern, replacement in TOKEN_SHAPES:
        value, replaced = pattern.subn(replacement, value)
        count += replaced
    return value, count


def scrub_secrets(text: str) -> str:
    """Mask credentials an exception text may carry (headers, query strings, key prefixes, JWTs)."""

    def mask(match: re.Match[str]) -> str:
        lead = match.group(1) or ""
        return f"{lead}{REDACTED}"

    return _SECRET_IN_TEXT.sub(mask, text)


def redact_emails(text: str) -> str:
    """Hide the person in an address and keep its domain: ``[redacted]@example.com``.

    Company domains are what Tin's own workflows keep (outreach.paying_segment writes
    domains only); the local part names a person.
    """
    return _EMAIL.sub(lambda match: f"{REDACTED}@{match.group(1)}", text)


def redact_message(text: str, *, secrets: Sequence[str] = ()) -> str:
    """Everything above, in order, for one line of provider or exception text."""
    text, _ = redact_text(text, secrets)
    text, _ = redact_token_shapes(text)
    return redact_emails(scrub_secrets(text))
