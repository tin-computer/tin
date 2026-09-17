"""Server-side product analytics: every MCP call, run state change and onboarding milestone.

Events go to PostHog through its batch endpoint from a background task, so nothing on the
request path waits on the network. The person is the project (its id as `distinct_id`), since
a project is what a founder and their agent share; calls that have no project use the Clerk
user id. Without an API key everything here is a no-op.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)

PAYLOAD_CAP = 64 * 1024
"""Bytes of JSON kept per argument or result field; longer values are cut and flagged."""

_SECRET_KEY = re.compile(r"(token|secret|password|api_key|apikey|authorization|credential)", re.I)
_FLUSH_EVERY = 2.0
_FLUSH_AT = 50
_QUEUE_MAX = 5000


def redact(value: Any) -> Any:
    """Replace values under secret-looking keys, recursively."""
    if isinstance(value, dict):
        return {
            k: "[redacted]" if _SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def clip(value: Any, *, cap: int = PAYLOAD_CAP) -> tuple[Any, dict[str, Any]]:
    """Return the value JSON-safe and under `cap` bytes, plus size facts for the event."""
    text = json.dumps(redact(value), default=str, ensure_ascii=False)
    size = len(text.encode("utf-8"))
    if size <= cap:
        return json.loads(text), {"bytes": size, "truncated": False}
    return text.encode("utf-8")[:cap].decode("utf-8", errors="ignore"), {
        "bytes": size,
        "truncated": True,
    }


METADATA_FIELDS = frozenset(
    {
        "tool",
        "duration_ms",
        "is_error",
        "error_type",
        "arguments_bytes",
        "result_bytes",
        "clerk_user_id",
        "oauth_client_id",
        "client_name",
        "client_version",
        "protocol_version",
        "run_id",
        "workflow",
        "status",
        "executor",
        "trigger_source",
        "review_required",
        "decision",
        "audience",
        "option",
        "control",
        "stopped_runs",
        "removed_schedules",
        # Who connected, for the internal Slack alerts. Identity only: an email, a
        # display name, and which door they came through. Never project content.
        "email",
        "name",
        "via",
        "surface",
        "agent",
        "first_session",
    }
)

PERSON_FIELDS = frozenset({"email", "name"})
"""Of those, the ones worth pinning to the PostHog person so later events can read them."""


class Analytics:
    def __init__(
        self,
        api_key: str | None,
        host: str = "https://us.i.posthog.com",
        *,
        source: str = "tin-lite",
    ) -> None:
        self._api_key = api_key or None
        self._host = host.rstrip("/")
        self._source = source
        self._queue: deque[dict[str, Any]] = deque(maxlen=_QUEUE_MAX)
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._client: httpx.AsyncClient | None = None
        self.sent = 0
        self.dropped = 0

    @property
    def enabled(self) -> bool:
        return self._api_key is not None

    def capture(
        self,
        event: str,
        *,
        distinct_id: str | UUID | None,
        properties: dict[str, Any] | None = None,
        project_id: str | UUID | None = None,
        set_person: bool = False,
    ) -> None:
        """Queue one event. Safe to call from anywhere; drops silently when disabled.

        `set_person` pins the identity fields to the PostHog person as well, so a later
        event from the same person can be rendered with their email without another
        lookup. Pass it where identity is freshly fetched, not on every call.
        """
        if not self.enabled or distinct_id is None:
            return
        props: dict[str, Any] = {"$lib": "tin-lite", "source": self._source}
        if project_id is not None:
            props["project_id"] = str(project_id)
            props["$groups"] = {"project": str(project_id)}
        # Analytics has no project-content contract. Fail closed for new properties,
        # including nested payloads, free-form errors and activity summaries.
        props.update(
            {
                key: value
                for key, value in (properties or {}).items()
                if key in METADATA_FIELDS
                and isinstance(value, str | int | float | bool | type(None))
            }
        )
        if set_person:
            person = {k: props[k] for k in PERSON_FIELDS if props.get(k) is not None}
            if person:
                props["$set"] = person
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(
            {
                "event": event,
                "distinct_id": str(distinct_id),
                "properties": props,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        self._ensure_flusher()

    def _ensure_flusher(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="analytics-flush")
        if len(self._queue) >= _FLUSH_AT:
            self._wake.set()

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_FLUSH_EVERY)
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._queue:
                continue
            await self.flush()

    async def flush(self) -> None:
        """Send everything queued now. Failures are logged and the batch is dropped."""
        if not self._queue or not self.enabled:
            return
        batch: list[dict[str, Any]] = []
        while self._queue and len(batch) < 500:
            batch.append(self._queue.popleft())
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        try:
            response = await self._client.post(
                f"{self._host}/batch/", json={"api_key": self._api_key, "batch": batch}
            )
            response.raise_for_status()
            self.sent += len(batch)
        except Exception:
            self.dropped += len(batch)
            logger.warning("analytics: dropped %d events", len(batch), exc_info=True)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.flush()
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_current = Analytics(None)


def configure(api_key: str | None, host: str, *, source: str = "tin-lite") -> Analytics:
    """Install the process-wide client; called once from the app lifespan."""
    global _current
    _current = Analytics(api_key, host, source=source)
    return _current


def current() -> Analytics:
    return _current


def capture(
    event: str,
    *,
    distinct_id: str | UUID | None,
    properties: dict[str, Any] | None = None,
    project_id: str | UUID | None = None,
    set_person: bool = False,
) -> None:
    _current.capture(
        event,
        distinct_id=distinct_id,
        properties=properties,
        project_id=project_id,
        set_person=set_person,
    )


def capture_new_user(*, auth: Any, clerk_user_id: str, via: str) -> None:
    """Announce a person's first arrival in Tin, with their identity attached.

    The Clerk lookup runs in the background, so the tool call or request that created the
    user never waits on it. When Clerk cannot be reached the event still fires, carrying
    the Clerk id alone.
    """
    if not _current.enabled:
        return

    async def run() -> None:
        try:
            identity = await auth.identity(clerk_user_id)
        except Exception:  # noqa: BLE001 - an alert must never break a signup
            logger.warning("analytics: identity lookup failed for %s", clerk_user_id)
            identity = {"email": None, "name": None}
        capture(
            "tin_user_created",
            distinct_id=clerk_user_id,
            properties={
                "clerk_user_id": clerk_user_id,
                "via": via,
                "email": identity.get("email"),
                "name": identity.get("name"),
            },
            set_person=True,
        )

    try:
        asyncio.get_running_loop().create_task(run(), name="analytics-new-user")
    except RuntimeError:
        return


async def aclose() -> None:
    await _current.aclose()
