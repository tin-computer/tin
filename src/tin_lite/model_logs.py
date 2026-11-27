"""Each Neon call's full prompt and answer, kept in Postgres for explaining and improving.

A verdict on a company or a rating of a person is one model call; its receipt keeps only the
parsed answer and its usage record only token counts. This keeps the rest: the instructions
and messages sent, the schema and settings, the answer's text, the searches it ran and the
pages it rested on (under their own addresses, grounding_links), or the kind of error when
nothing usable came back. Operators read it to see why the model decided what it did.

Prompts hold people's names, headlines and the team's notes, so a row is kept KEEP_DAYS and is
deleted with its project. Writing a log never fails the call it describes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields
from typing import Any
from uuid import UUID, uuid4

from tin_lite.model_providers import ModelRequest, ModelResult

logger = logging.getLogger(__name__)
KEEP_DAYS = 90
# Runaway guards on what one row holds, far above a Neon call's prompt and answer.
TEXT_CHARS = 400_000
ERROR_CHARS = 300
# Old rows go a batch at a time, on the next write.
PURGE_BATCH = 200


def _cut(text: Any, limit: int = TEXT_CHARS) -> str:
    text = str(text or "").replace("\x00", "")
    return text if len(text) <= limit else text[:limit] + "…"


def request_document(request: ModelRequest) -> dict[str, Any]:
    """What was sent: every field of the request, its messages as role and words."""

    sent: dict[str, Any] = {}
    for field in fields(request):
        value = getattr(request, field.name)
        if field.name == "messages":
            value = [{"role": m.role.value, "content": _cut(m.content)} for m in value]
        elif field.name == "system":
            value = _cut(value) if value is not None else None
        elif hasattr(value, "value"):
            value = value.value
        sent[field.name] = value
    sent["kind"] = type(request).__name__
    return sent


def response_document(result: ModelResult) -> dict[str, Any]:
    return {
        "provider": result.provider.value,
        "model": result.model,
        "request_id": result.request_id,
        "text": _cut(result.text),
        "parsed": result.parsed,
        "grounding": result.grounding,
        "stop_reason": result.stop_reason,
        "usage": {key: value for key, value in asdict(result.usage).items() if value is not None},
    }


async def record(
    db,
    *,
    project_id: UUID,
    run_id: UUID | str,
    step: str,
    route: str,
    request: ModelRequest,
    result: ModelResult | None = None,
    error: BaseException | None = None,
) -> None:
    """Keep one call. Never raises: a log that can't be written leaves the call as it was."""

    try:
        response = response_document(result) if result is not None else None
        async with db.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_call_logs
                       (id, project_id, run_id, step, route, request, response, error)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)""",
                uuid4(),
                project_id,
                UUID(str(run_id)),
                step[:200],
                route,
                json.dumps(request_document(request), ensure_ascii=False, default=str),
                (
                    json.dumps(response, ensure_ascii=False, default=str)
                    if response is not None
                    else None
                ),
                _cut(type(error).__name__ if error is not None else None, ERROR_CHARS) or None,
            )
            await conn.execute(
                """DELETE FROM model_call_logs WHERE id IN (
                       SELECT id FROM model_call_logs
                       WHERE created_at < now() - make_interval(days => $1)
                       ORDER BY created_at LIMIT $2)""",
                KEEP_DAYS,
                PURGE_BATCH,
            )
    except Exception:  # noqa: BLE001 - the log is for later; the call it describes stands
        logger.warning("A model call log for run %s could not be written.", run_id)


async def calls(conn, run_id: UUID | str) -> list[dict[str, Any]]:
    """A run's model calls, oldest first, as kept."""

    rows = await conn.fetch(
        """SELECT step, route, request, response, error, created_at FROM model_call_logs
           WHERE run_id = $1 ORDER BY created_at""",
        UUID(str(run_id)),
    )
    out = []
    for row in rows:
        item = dict(row)
        for key in ("request", "response"):
            if isinstance(item[key], str):
                item[key] = json.loads(item[key])
        out.append(item)
    return out
