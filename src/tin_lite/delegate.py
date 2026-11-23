"""Delegate (dlgt.io): a marketplace of data services Tin hires by API, with Tin's own key.

An RB2B company-only visit uses three of them: a contact finder by company domain and job
titles, a people search when that finds nobody, and a LinkedIn profile read that checks who
still works there. Each hire is quote → order → result:

- the quote (`pricing-preview`) prices this exact input; nothing is charged;
- the order buys it, under a price cap and an idempotency key, so an order repeated after a
  crash is the same order, never a second purchase;
- the result is polled with `?wait=` until the job is delivered or failed. A failed job is
  refunded by Delegate.

Prices are USD micros. A 429 is honoured once (Retry-After up to 30 s); Delegate bans clients
that keep hitting its limiter. Errors carry a code and status, never a response body.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://app.dlgt.io/api/v1"
# A runaway guard well above any answer these services give.
MAX_RESPONSE_BYTES = 4_000_000
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class DelegateError(RuntimeError):
    """A request Delegate refused or that never answered. `code` is Delegate's own when it
    named one; nothing from the response body is kept beyond it."""

    def __init__(self, message: str, *, code: str = "delegate_failed", status: int = 0):
        super().__init__(message)
        self.code, self.status = code, status


def _micros(usd: float) -> int:
    return round(usd * 1_000_000)


class Delegate:
    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise DelegateError("No Delegate key", code="missing_api_key")
        self._key, self._transport = api_key, transport

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:
            for attempt in range(2):
                try:
                    response = await client.request(
                        method,
                        path,
                        headers={
                            "Authorization": f"Bearer {self._key}",
                            "Accept": "application/json",
                            "User-Agent": "tin-lite",
                        },
                        json=(
                            {k: v for k, v in body.items() if v is not None}
                            if body is not None
                            else None
                        ),
                    )
                except httpx.HTTPError as error:
                    raise DelegateError("Delegate didn't answer", code="unavailable") from error
                if response.status_code == 429 and attempt == 0:
                    try:
                        wait = float(response.headers.get("Retry-After") or 0)
                    except ValueError:
                        wait = 0
                    if 0 < wait <= 30:
                        await asyncio.sleep(wait)
                        continue
                break
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise DelegateError("Delegate's answer was too large", code="too_large")
        if response.status_code >= 400:
            code = "delegate_refused"
            try:
                detail = response.json().get("detail")
                if isinstance(detail, dict):
                    code = str(detail.get("code") or detail.get("error_type") or code)[:80]
            except (ValueError, AttributeError):
                pass
            raise DelegateError(
                f"Delegate answered {response.status_code}",
                code=code,
                status=response.status_code,
            )
        try:
            return response.json() if response.content else None
        except ValueError as error:
            raise DelegateError("Delegate's answer wasn't JSON", code="invalid") from error

    async def order(
        self,
        service: str,
        task_input: dict,
        *,
        max_usd: float,
        idempotency_key: str,
        scope: dict | None = None,
    ) -> dict:
        """Price and buy one job. Returns its conversation ID, order ID and price; raises
        `price_above_max` before ordering when the quote is above `max_usd`."""

        svc = await self._call("GET", f"/services/by-key/{quote(service, safe='')}")
        offer = _offer(svc)
        definition = offer["definition"]
        task_input = dict(task_input)
        items = None
        if definition["fulfillment"].get("batch"):
            # A batch-only service takes one input as a batch of one.
            items = [{"custom_id": "1", "input": task_input}]
        task: dict[str, Any] = {
            "title": str(svc.get("title") or service)[:500],
            "summary": json.dumps(items or task_input)[:5000],
            "input": {} if items else task_input,
        }
        if items:
            task["items"] = items
        scope = _scope(definition, task_input, len(items) if items else None, scope or {})
        preview = await self._call(
            "POST",
            f"/offers/{quote(offer['offer_version_id'], safe='')}/pricing-preview",
            {"scope": scope, "task": task},
        )
        total = int(preview["resolution"]["total_micros"])
        cap = _micros(max_usd)
        if total > cap:
            raise DelegateError("The quote is above Tin's cap", code="price_above_max")
        body: dict[str, Any] = {
            "schema_version": 2,
            "offer_version_id": offer["offer_version_id"],
            "scope": preview["canonical_scope"],
            "task": task,
            "pricing_resolution_id": preview["pricing_resolution_id"],
            "idempotency_key": idempotency_key[:200],
            "execution_context": (
                {"file_contract_hash": svc.get("file_contract_hash")}
                if svc.get("filesystem_runtime_modes") == ["snapshot_v1"]
                else {}
            ),
        }
        funding = preview.get("funding") or {}
        if funding.get("kind") == "buyer_cap":
            body["buyer_cap_micros"] = min(cap, int(funding["maximum_cap_micros"]))
            if body["buyer_cap_micros"] < max(total, int(funding["minimum_cap_micros"])):
                raise DelegateError(
                    "The offer's minimum is above Tin's cap", code="price_above_max"
                )
        placed = await self._call("POST", "/orders", body)
        if placed.get("status") in ("rejected", "failed"):
            raise DelegateError("Delegate didn't take the order", code=f"order_{placed['status']}")
        conversation = (placed.get("result") or {}).get("conversation_id")
        if not isinstance(conversation, str) or not UUID_TEXT.fullmatch(conversation):
            raise DelegateError("Delegate's order named no job", code="invalid")
        return {
            "conversation_id": conversation,
            "order_id": str(placed.get("order_id") or "")[:80],
            "price_usd": total / 1e6,
        }

    async def result(self, conversation_id: str, *, wait: int = 30) -> dict:
        """The job's state (running, delivered or failed) and, once delivered, its payload."""

        if not UUID_TEXT.fullmatch(conversation_id):
            raise DelegateError("Not a Delegate job", code="invalid")
        detail = await self._call(
            "GET", f"/conversations/{conversation_id}" + (f"?wait={wait}" if wait else "")
        )
        return job_view(detail)


def _offer(svc: dict) -> dict:
    """The service's one active single-task offer (or its batch offer when it has only that)."""

    active = [o for o in svc.get("offer_versions") or [] if o.get("state") == "active"]
    tasks = [o for o in active if o["definition"]["fulfillment"]["kind"] == "task"]
    single = [o for o in tasks if not o["definition"]["fulfillment"].get("batch")]
    candidates = single or tasks
    if len(candidates) != 1:
        raise DelegateError("The service has no one offer to buy", code="offer_selection")
    return candidates[0]


def _scope(definition: dict, task_input: dict, count: int | None, given: dict) -> dict:
    """Scope keys bound to input keys take the input's value; a batch counts its items; keys
    the service binds to no input (a profile read's `requested_items`) come from `given`."""

    bindings = [
        b for b in definition["fulfillment"].get("bindings") or [] if b.get("task_input_key")
    ]
    scope = {
        b["scope_key"]: task_input[b["task_input_key"]]
        for b in bindings
        if b["task_input_key"] in task_input
    }
    count_key = (definition["fulfillment"].get("batch") or {}).get("count_scope_key")
    if count_key and count is not None:
        scope[count_key] = count
    scope.update(given)
    missing = [k for k in (definition.get("scope") or {}).get("required") or [] if k not in scope]
    if missing:
        raise DelegateError("The service needs input Tin didn't give", code="missing_scope")
    return scope


def job_view(detail: dict) -> dict:
    """running, delivered (with the provider's payload) or failed (with a short reason)."""

    conversation = (detail or {}).get("conversation") or {}
    jobs = (detail or {}).get("jobs") or []
    job = jobs[-1] if jobs else {}
    payload = job.get("result_payload") if isinstance(job.get("result_payload"), dict) else None
    if payload and (
        job.get("status") == "delivered" or conversation.get("status") in ("delivered", "completed")
    ):
        if payload.get("outcome") == "failed_provider":
            return {"state": "failed", "result": None, "error": "The provider failed"}
        return {"state": "delivered", "result": payload, "error": None}
    if job.get("status") in ("rejected", "dispatch_failed"):
        return {"state": "failed", "result": None, "error": f"The job was {job['status']}"}
    if conversation.get("status") == "disputed":
        return {"state": "failed", "result": None, "error": "The job is disputed"}
    return {"state": "running", "result": None, "error": None}
