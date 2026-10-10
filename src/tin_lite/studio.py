"""Creative-studio provider calls: fal-hosted text-to-speech and word-level transcription.

The switchboard owns the fal credential. A studio sandbox asks for one narrated line at a time
through the run-bound `/internal/run-tools/studio/voice` route and receives the audio bytes
plus the words the transcriber heard with their timestamps; alignment happens in the sandbox.
Each line reserves its quota before dispatch. Synthesis and transcription have independent
result and usage receipts: replay never repurchases a completed or ambiguous provider attempt.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from tin_lite.billing_contracts import BillingError, digest
from tin_lite.db import Database
from tin_lite.domain import STUDIO_PROVIDER, STUDIO_VOICE_CAPABILITY, StudioUsage
from tin_lite.settings import Settings
from tin_lite.studio_billing import CARD, billing_event

logger = logging.getLogger(__name__)

STUDIO_VOICE_OPERATION = "studio_voice"
# Gemini prebuilt voices exposed to the workflow input select; the first is the default.
STUDIO_VOICES = ("Kore", "Puck", "Zephyr", "Charon", "Leda", "Fenrir", "Aoede")
# fal's Gemini TTS content checker refuses ordinary product lines when the style asks for a
# "short-form voiceover" or "creator talking to camera"; this plainer style passes them.
DEFAULT_VOICE_STYLE = "Warm, upbeat, and quick. Friendly product narration."
MAX_VOICE_LINE_CHARACTERS = 400
MAX_VOICE_STYLE_CHARACTERS = 300
TTS_URL = "https://fal.run/fal-ai/gemini-3.1-flash-tts"
STT_URL = "https://fal.run/fal-ai/whisper"
_TIMEOUT = httpx.Timeout(240.0, connect=20.0)
_MAX_AUDIO_BYTES = 4_000_000


class StudioError(RuntimeError):
    """A bounded, user-safe studio failure (quota, validation, or provider refusal)."""


@dataclass(frozen=True)
class StudioVoice:
    audio: bytes
    media_type: str
    words: list[dict[str, Any]]
    characters: int
    replayed: bool


def voice_execution_key(run_id: UUID, request_id: str) -> str:
    return f"{run_id}:studio-voice:{request_id}"


class StudioService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._db = database
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)

    @property
    def enabled(self) -> bool:
        return self._settings.fal_key is not None

    async def close(self) -> None:
        await self._client.aclose()

    async def voice(
        self,
        *,
        run_id: UUID,
        request_id: str,
        text: str,
        voice: str,
        style: str,
        language_code: str,
        transcription_language: str,
    ) -> StudioVoice:
        if not self.enabled:
            raise StudioError("the studio voice provider is not configured")
        line = " ".join(text.split())
        if not line or len(line) > MAX_VOICE_LINE_CHARACTERS:
            raise StudioError(f"a voice line must contain 1-{MAX_VOICE_LINE_CHARACTERS} characters")
        if voice not in STUDIO_VOICES:
            raise StudioError(f"voice must be one of {', '.join(STUDIO_VOICES)}")
        style = " ".join(style.split())
        if len(style) > MAX_VOICE_STYLE_CHARACTERS:
            raise StudioError(f"style must contain at most {MAX_VOICE_STYLE_CHARACTERS} characters")
        language_code = language_code.strip()[:40] or "English (US)"
        transcription_language = transcription_language.strip().lower()[:8] or "en"
        if not request_id.isalnum() or len(request_id) > 64:
            raise StudioError("request_id must be alphanumeric and at most 64 characters")
        execution_key = voice_execution_key(run_id, request_id)
        # One run lock also serializes quota reservation; concurrent distinct request
        # IDs cannot each spend the last available line/characters.
        async with self._db.effect_lock(f"{run_id}:studio-quota", "studio_quota") as (conn, _):
            existing = await self._db.get_effect(execution_key, conn=conn)
            fingerprint = digest([line, voice, style, language_code, transcription_language])
            if existing and (existing.result or {}).get("request_digest") not in {
                None,
                fingerprint,
            }:
                raise StudioError("request_id was already used for different voice instructions")
            if existing is not None and existing.status == "completed" and existing.result:
                audio, media_type = await self._download(str(existing.result["audio_url"]))
                return StudioVoice(
                    audio=audio,
                    media_type=media_type,
                    words=list(existing.result.get("words", [])),
                    characters=int(existing.result.get("characters", len(line))),
                    replayed=True,
                )
            if existing is None:
                usage: StudioUsage = await self._db.studio_voice_usage(run_id, conn=conn)
                if usage.lines >= self._settings.studio_max_voice_lines_per_run:
                    raise StudioError("this run has used all of its voice lines")
                if (
                    usage.characters + len(line)
                    > self._settings.studio_max_voice_characters_per_run
                ):
                    raise StudioError("this run has used its voice character budget")
                async with conn.transaction():
                    await self._db.start_effect(
                        conn, execution_key=execution_key, operation=STUDIO_VOICE_OPERATION
                    )
                    await self._db.save_effect_progress(
                        conn,
                        execution_key=execution_key,
                        result={"request_digest": fingerprint, "characters": len(line)},
                    )
            elif not (existing.result or {}).get("request_digest"):
                raise StudioError(
                    "an earlier voice attempt is unresolved; it cannot be repurchased"
                )
            try:
                audio_url = await self._synthesize(
                    text=line,
                    voice=voice,
                    style=style,
                    language_code=language_code,
                    conn=conn,
                    run_id=run_id,
                    execution_key=execution_key,
                )
                audio, media_type = await self._download(audio_url)
                words = await self._transcribe(
                    audio_url,
                    language=transcription_language,
                    conn=conn,
                    run_id=run_id,
                    execution_key=execution_key,
                )
            except (StudioError, BillingError) as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=str(exc)
                )
                if isinstance(exc, BillingError):
                    raise StudioError(str(exc)) from exc
                raise
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message="studio provider call failed"
                )
                raise StudioError("the voice provider did not complete the request") from exc
            await self._db.complete_effect(
                conn,
                execution_key=execution_key,
                result={
                    "request_digest": fingerprint,
                    "audio_url": audio_url,
                    "media_type": media_type,
                    "characters": len(line),
                    "voice": voice,
                    "words": words,
                },
            )
        return StudioVoice(
            audio=audio, media_type=media_type, words=words, characters=len(line), replayed=False
        )

    def _headers(self) -> dict[str, str]:
        assert self._settings.fal_key is not None
        return {"Authorization": f"Key {self._settings.fal_key.get_secret_value()}"}

    async def _paid_call(self, *, conn, run_id, execution_key, url, body, step):
        key = f"{execution_key}:{step}"
        existing = await self._db.get_effect(key, conn=conn)
        if existing:
            if existing.status == "completed" and existing.result:
                if "error" in existing.result:
                    raise StudioError(existing.result["error"])
                return existing.result["payload"]
            raise StudioError(
                f"{step} has an unresolved provider attempt; no repeat purchase was made"
            )
        usage_key = f"{key}:usage"
        endpoint = url.removeprefix("https://fal.run/")
        record = {
            "version": 1,
            "run_id": str(run_id),
            "provider": "fal",
            "category": "tool",
            "endpoint": endpoint,
            "step": step,
            "attempted_at": datetime.now(UTC).isoformat(),
            "outcome": "unconfirmed",
            "usage": None,
            "reported_cost_usd": None,
        }
        billing = getattr(self._db, "billing", None)
        if billing:
            # An in-flight model call or late fal cost can temporarily occupy the
            # remaining ceiling. Wait before dispatch, not after a paid attempt:
            # never turn an unchanged line into a second supplier purchase.
            for attempt in range(13):
                try:
                    await billing.begin_operation(
                        conn,
                        run_id=run_id,
                        operation_id=usage_key,
                        kind="tool",
                        maximum=CARD["request_maximum_nanos"],
                    )
                    break
                except BillingError as exc:
                    pending = exc.code == "run_limit" and await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM billing_operations "
                        "WHERE run_id=$1 AND status='pending')",
                        run_id,
                    )
                    if not pending or attempt == 12:
                        raise
                    await asyncio.sleep(5)
        await self._db.start_effect(conn, execution_key=usage_key, operation="external_usage_v1")
        await self._db.save_effect_progress(conn, execution_key=usage_key, result=record)
        await self._db.start_effect(conn, execution_key=key, operation="studio_provider_result_v1")
        response = await self._client.post(url, headers=self._headers(), json=body)
        record["provider_request_id"] = response.headers.get("x-fal-request-id")
        record["status_code"] = response.status_code
        await self._db.save_effect_progress(conn, execution_key=usage_key, result=record)
        try:
            if len(response.content) > 256_000:
                raise StudioError(f"{step} returned an oversized response")
            payload = _provider_json(response, step=step)
            # Keep only bounded output needed for recovery, not an echoed request.
            payload = (
                {"audio": payload.get("audio")}
                if step == "synthesis"
                else {
                    "chunks": (payload.get("chunks") or [])[:400],
                }
            )
        except StudioError as exc:
            await self._db.complete_effect(conn, execution_key=key, result={"error": str(exc)})
            await self._reconcile_observation(conn, usage_key, record)
            raise
        await self._db.complete_effect(conn, execution_key=key, result={"payload": payload})
        await self._reconcile_observation(conn, usage_key, record)
        return payload

    async def _reconcile_observation(self, conn, key, record):
        async with self._db.effect_lock(key, "external_usage_v1", conn=conn) as (locked, existing):
            if existing and existing.status != "completed":
                await self._lookup_observation(locked, key, existing.result or record)

    async def _lookup_observation(self, conn, key, record):
        request_id = record.get("provider_request_id")
        if not request_id or len(request_id) > 200:
            return
        record = {**record, "last_cost_check_at": datetime.now(UTC).isoformat()}
        await self._db.save_effect_progress(conn, execution_key=key, result=record)
        # A read-only lookup, never another generation. Missing/late provider cost
        # remains unknown and the normal recovery loop tries the lookup again.
        try:
            response = await self._client.get(
                "https://api.fal.ai/v1/models/billing-events",
                headers=self._headers(),
                params={
                    "request_id": request_id,
                    "endpoint_id": record["endpoint"],
                    "start": record["attempted_at"],
                    "limit": 2,
                },
                timeout=5,
            )
            if not response.is_success or len(response.content) > 32_000:
                return
            nanos = billing_event(
                response.json(), request_id=request_id, endpoint=record["endpoint"]
            )
        except (httpx.HTTPError, ValueError):
            return
        if nanos is None:
            return
        record = {
            **record,
            "outcome": "cost_received",
            "reported_cost_nanos": nanos,
            "reported_cost_usd": str(Decimal(nanos) / 1_000_000_000),
        }
        await self._db.complete_effect(conn, execution_key=key, result=record)
        billing = getattr(self._db, "billing", None)
        if billing:
            from tin_lite.studio_billing import price

            priced = price(CARD, record)
            await billing.observe_operation(
                conn, operation_id=key, nanos=priced[0], observation=priced[1]
            )

    async def reconcile_usage(self):
        if not self.enabled:
            return
        rows = await self._db.pool.fetch("""SELECT execution_key FROM effect_receipts
            WHERE operation='external_usage_v1' AND status='started'
              AND result->>'provider'='fal' AND result->>'provider_request_id' IS NOT NULL
              AND created_at > now()-interval '24 hours'
            ORDER BY updated_at LIMIT 8""")
        for row in rows:
            async with self._db.effect_lock(row["execution_key"], "external_usage_v1") as (
                conn,
                receipt,
            ):
                if receipt and receipt.status != "completed":
                    await self._lookup_observation(conn, row["execution_key"], receipt.result)

    async def _synthesize(
        self, *, text: str, voice: str, style: str, language_code: str, conn, run_id, execution_key
    ) -> str:
        payload = await self._paid_call(
            conn=conn,
            run_id=run_id,
            execution_key=execution_key,
            url=TTS_URL,
            step="synthesis",
            body={
                "voice": voice,
                "prompt": text,
                "temperature": 1,
                "language_code": language_code,
                "output_format": "mp3",
                "style_instructions": style,
            },
        )
        audio = payload.get("audio") if isinstance(payload, dict) else None
        url = audio.get("url") if isinstance(audio, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise StudioError("speech synthesis returned no audio")
        return url

    async def _download(self, url: str) -> tuple[bytes, str]:
        if not url.startswith("https://"):
            raise StudioError("audio URL is not https")
        response = await self._client.get(url)
        if response.status_code != 200:
            raise StudioError("the synthesized audio could not be downloaded")
        data = response.content
        if not data or len(data) > _MAX_AUDIO_BYTES:
            raise StudioError("the synthesized audio has an invalid size")
        media_type = response.headers.get("content-type", "audio/mpeg").split(";")[0].strip()
        return data, media_type or "audio/mpeg"

    async def _transcribe(
        self, audio_url: str, *, language: str, conn, run_id, execution_key
    ) -> list[dict[str, Any]]:
        payload = await self._paid_call(
            conn=conn,
            run_id=run_id,
            execution_key=execution_key,
            url=STT_URL,
            step="transcription",
            body={
                "audio_url": audio_url,
                "task": "transcribe",
                "language": language,
                "chunk_level": "word",
            },
        )
        chunks = payload.get("chunks") if isinstance(payload, dict) else None
        words: list[dict[str, Any]] = []
        for chunk in chunks if isinstance(chunks, list) else []:
            if not isinstance(chunk, dict):
                continue
            stamp = chunk.get("timestamp")
            word = str(chunk.get("text", "")).strip()
            if (
                not word
                or not isinstance(stamp, list)
                or len(stamp) != 2
                or not all(isinstance(value, int | float) for value in stamp)
            ):
                continue
            words.append({"word": word, "start": float(stamp[0]), "end": float(stamp[1])})
        return words[:400]


def _provider_detail(response: httpx.Response) -> str:
    """A short excerpt of the provider's error so the sandbox can fix its input. Only the
    message text is kept: fal echoes the request body, which is not repeated back."""
    try:
        payload = response.json()
    except ValueError:
        return "no detail"
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    messages: list[str] = []
    for item in detail if isinstance(detail, list) else [detail]:
        if isinstance(item, dict):
            text = item.get("msg") or item.get("message") or item.get("type") or ""
            if item.get("type"):
                text = f"{item['type']}: {text}"
        else:
            text = str(item or "")
        if text:
            messages.append(" ".join(text.split()))
    return "; ".join(messages)[:240] or "no detail"


def _provider_json(response: httpx.Response, *, step: str) -> Any:
    if response.status_code == 403 and b"locked" in response.content.lower():
        raise StudioError(f"{step} is unavailable: the fal account is locked")
    if response.status_code == 429:
        raise StudioError(f"{step} is rate limited; try again shortly")
    if response.status_code != 200:
        detail = _provider_detail(response)
        logger.warning("studio %s failed with status %s: %s", step, response.status_code, detail)
        raise StudioError(f"{step} failed with status {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise StudioError(f"{step} returned an invalid response") from exc


__all__ = [
    "STUDIO_PROVIDER",
    "STUDIO_VOICE_CAPABILITY",
    "StudioError",
    "StudioService",
    "StudioVoice",
]
