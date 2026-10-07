"""Allowlisted, bounded DataForSEO research calls. No SDK/HTTP retries or caller URLs."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal

import httpx

from tin_lite.dataforseo import DataForSEO, DataForSEOError
from tin_lite.keyword_plan import POLICY, host, phrase
from tin_lite.organic_audit import MARKETS
from tin_lite.usage_capture import begin_observation, observe_tool

ENDPOINTS = {
    "ranked": "dataforseo_labs/google/ranked_keywords/live",
    "ranked_relevant": "dataforseo_labs/google/ranked_keywords/live",
    "competitors": "dataforseo_labs/google/competitors_domain/live",
    # v9 asks for more domains so filtering out platforms and namesakes still leaves three.
    "competitors_wide": "dataforseo_labs/google/competitors_domain/live",
    "ideas": "dataforseo_labs/google/keyword_ideas/live",
    "suggestions": "dataforseo_labs/google/keyword_suggestions/live",
    "related": "dataforseo_labs/google/related_keywords/live",
    "overview": "dataforseo_labs/google/keyword_overview/live",
    "serp": "serp/google/organic/live/advanced",
    # Paid-ads assessment kinds: batch metrics, a Google Ads traffic forecast, Ads Transparency
    # Center lookups and a competitor's paid keyword footprint.
    "overview_batch": "dataforseo_labs/google/keyword_overview/live",
    "ad_traffic": "keywords_data/google_ads/ad_traffic_by_keywords/live",
    "ads_search": "serp/google/ads_search/live/advanced",
    "ads_advertisers": "serp/google/ads_advertisers/live/advanced",
    "ranked_paid": "dataforseo_labs/google/ranked_keywords/live",
}
PAID_BOUNDS = {"batch": 40, "ads_depth": 20, "ranked_paid_rows": 50}
# HTTP refusals as the DataForSEO status codes they stand for (unauthorized, payment required,
# access denied, rate limit), for callers that settle a refusal as a known outcome.
HTTP_REFUSALS = {401: 40100, 402: 40200, 403: 40300, 429: 40202}
# The traffic forecast answers with one aggregate row per request rather than an items list.
ROW_RESULT_KINDS = frozenset({"ad_traffic"})
# The client a batch of lookups shares, with the adapter that opened it.
_SESSION: ContextVar[tuple[KeywordData, httpx.AsyncClient] | None] = ContextVar(
    "keyword_data_session", default=None
)


def request_for(kind: str, *, market: str, value, tag: str) -> dict:
    request = {"location_code": MARKETS[market], "language_code": "en", "tag": tag}
    if kind == "ranked":
        request.update(target=host(value).removeprefix("www."), limit=POLICY["footprint_rows"])
    elif kind == "ranked_relevant":
        seeds = value["seeds"]
        if not isinstance(seeds, list) or not 1 <= len(seeds) <= POLICY["max_seeds"]:
            raise ValueError("Competitor research requires bounded seed phrases.")
        filters = []
        for seed in seeds:
            if filters:
                filters.append("or")
            filters.append(["keyword_data.keyword", "regex", "(?i)" + re.escape(phrase(seed))])
        request.update(
            target=host(value["host"]).removeprefix("www."),
            limit=POLICY["footprint_rows"],
            filters=filters,
        )
    elif kind == "competitors":
        request.update(target=host(value).removeprefix("www."), limit=5)
    elif kind == "competitors_wide":
        request.update(target=host(value).removeprefix("www."), limit=20)
    elif kind == "ideas":
        request.update(keywords=[phrase(value)], limit=POLICY["idea_rows"])
    elif kind == "related":
        request.update(keyword=phrase(value), depth=1, limit=POLICY["related_rows"])
    elif kind == "suggestions":
        request.update(keyword=phrase(value), limit=POLICY["idea_rows"])
    elif kind == "overview":
        if not isinstance(value, list) or not 1 <= len(value) <= POLICY["max_seeds"]:
            raise ValueError("Seed metric lookup exceeds its bound.")
        request.update(keywords=[phrase(item) for item in value])
    elif kind == "serp":
        request.update(keyword=phrase(value), depth=10, device="desktop", os="windows")
    elif kind == "overview_batch":
        if not isinstance(value, list) or not 1 <= len(value) <= PAID_BOUNDS["batch"]:
            raise ValueError("Batch metric lookup exceeds its bound.")
        request.update(keywords=[phrase(item) for item in value])
    elif kind == "ad_traffic":
        keywords, bid = value["keywords"], value["bid"]
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= PAID_BOUNDS["batch"]:
            raise ValueError("Traffic forecast exceeds its keyword bound.")
        if isinstance(bid, bool) or not isinstance(bid, (int, float)) or not 0.01 <= bid <= 500:
            raise ValueError("Traffic forecast requires a bid between $0.01 and $500.")
        request.update(
            keywords=[phrase(item) for item in keywords],
            bid=float(bid),
            match="phrase",
            date_interval="next_month",
        )
    elif kind == "ads_search":
        request.pop("language_code")
        request.update(target=host(value).removeprefix("www."), depth=PAID_BOUNDS["ads_depth"])
    elif kind == "ads_advertisers":
        request.pop("language_code")
        request.update(keyword=phrase(value), depth=PAID_BOUNDS["ads_depth"])
    elif kind == "ranked_paid":
        request.update(
            target=host(value).removeprefix("www."),
            item_types=["paid"],
            limit=PAID_BOUNDS["ranked_paid_rows"],
        )
    else:
        raise ValueError("Unknown keyword research endpoint.")
    return request


class DataForSEOTaskError(DataForSEOError):
    """DataForSEO answered and refused the request or its task: a known outcome."""

    def __init__(self, status_code: int, status_message: str | None = None) -> None:
        super().__init__(f"DataForSEO returned status {status_code}.")
        self.status_code = status_code
        # DataForSEO's own explanation ("Invalid Field: 'location_code'."); the service
        # gateway redacts and shows it to the run, never to logs or founders.
        self.status_message = status_message if isinstance(status_message, str) else None


class KeywordData:
    validate_target = staticmethod(DataForSEO.validate_target)

    def __init__(self, login: str, password: str, *, transport=None):
        self._auth = httpx.BasicAuth(login, password)
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=self._auth,
            timeout=45,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    @asynccontextmanager
    async def session(self):
        """Let the lookups inside share one client and its open connections.

        Each lookup keeps its own timeout, size bound and receipt; only the connection
        pool is shared. Nested sessions reuse the outer client.
        """
        current = _SESSION.get()
        if current is not None and current[0] is self:
            yield
            return
        async with self._client() as client:
            token = _SESSION.set((self, client))
            try:
                yield
            finally:
                _SESSION.reset(token)

    async def task(
        self,
        endpoint: str,
        request: dict,
        *,
        scope_keys: tuple[str, ...] | None = None,
        settle_errors: bool = False,
    ) -> dict:
        """POST one live request and return its checked task, with cost read back.

        The observation reserves and settles the supplier cost inside the caller's usage
        scope. `scope_keys` names the request fields the provider must echo back (all of them
        by default). With `settle_errors`, a refused request or task is recorded with its
        reported cost and raised as `DataForSEOTaskError`, a known outcome, instead of
        leaving the observation unconfirmed.
        """
        observation = await begin_observation("dataforseo", "tool", endpoint)
        shared = _SESSION.get()
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(asyncio.timeout(50))
                client = (
                    shared[1]
                    if shared is not None and shared[0] is self
                    else await stack.enter_async_context(self._client())
                )
                response = await stack.enter_async_context(
                    client.stream(
                        "POST", f"https://api.dataforseo.com/v3/{endpoint}", json=[request]
                    )
                )
                if settle_errors and response.status_code in HTTP_REFUSALS:
                    # Refused at the door (credentials, balance, rate): nothing was bought.
                    await observe_tool(observation, {"cost": 0})
                    raise DataForSEOTaskError(HTTP_REFUSALS[response.status_code])
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4_000_000:
                        raise DataForSEOError("Keyword response exceeded the transport bound.")
                payload = json.loads(body)
            if not isinstance(payload, dict):
                raise DataForSEOError("Keyword provider response was not successful.")
            if payload.get("status_code") != 20000:
                if settle_errors and type(payload.get("status_code")) is int:
                    await observe_tool(observation, {"cost": payload.get("cost", 0)})
                    raise DataForSEOTaskError(payload["status_code"], payload.get("status_message"))
                raise DataForSEOError("Keyword provider response was not successful.")
            tasks = payload.get("tasks")
            if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
                raise DataForSEOError("Keyword response has an invalid task envelope.")
            task = tasks[0]
            await observe_tool(observation, task)
            # 20100 is DataForSEO's "No Search Results": a completed, charged, empty lookup.
            if task.get("status_code") not in {20000, 20100}:
                if settle_errors and type(task.get("status_code")) is int:
                    raise DataForSEOTaskError(task["status_code"], task.get("status_message"))
                raise DataForSEOError("Keyword task did not return a completed result.")
            data = task.get("data")
            if not isinstance(data, dict) or any(
                data.get(key) != request[key]
                for key in (request if scope_keys is None else scope_keys)
            ):
                raise DataForSEOError("Keyword response belongs to a different request scope.")
            cost = Decimal(str(task.get("cost")))
            if not cost.is_finite() or cost < 0:
                raise DataForSEOError("Keyword response has invalid cost metadata.")
            return task
        except DataForSEOError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError, TimeoutError):
            raise DataForSEOError("Keyword provider outcome could not be confirmed.") from None

    async def query(self, kind: str, *, market: str, value, tag: str) -> dict:
        request = request_for(kind, market=market, value=value, tag=tag)
        try:
            task = await self.task(ENDPOINTS[kind], request)
            cost = Decimal(str(task.get("cost")))
            results = task.get("result")
            if results is None and (
                task.get("result_count") == 0 or task.get("status_code") == 20100
            ):
                results = []
            if kind in ROW_RESULT_KINDS:
                if not isinstance(results, list) or len(results) > len(request["keywords"]):
                    raise DataForSEOError("Keyword response has an invalid result envelope.")
                if any(not isinstance(row, dict) for row in results):
                    raise DataForSEOError("Keyword result contains invalid rows.")
                return {
                    "items": results,
                    "items_count": len(results),
                    "total_count": None,
                    "reported_cost_usd": str(cost),
                    "provider_task_id": str(task.get("id") or "")[:100],
                }
            if not isinstance(results, list) or len(results) > 1:
                raise DataForSEOError("Keyword response has an invalid result envelope.")
            items, total = [], 0
            if results:
                result = results[0]
                if not isinstance(result, dict) or any(
                    result.get(key, request[key]) != request[key]
                    for key in ("location_code", "language_code")
                    if key in request
                ):
                    raise DataForSEOError("Keyword result has a different market or language.")
                items = result.get("items")
                if items is None and result.get("items_count") == 0:
                    items = []
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise DataForSEOError("Keyword result contains invalid rows.")
                total = result.get("total_count")
            # A SERP includes non-organic features; only the consumer's bounded organic
            # projection is retained. Other endpoints may not exceed requested row counts.
            if kind == "serp":
                bound = 100
            elif kind == "overview_batch":
                bound = len(request["keywords"])
            elif kind in {"ads_search", "ads_advertisers"}:
                bound = request["depth"]
            else:
                bound = request.get("limit", POLICY["max_seeds"])
            if len(items) > bound:
                raise DataForSEOError("Keyword provider exceeded the requested row bound.")
            return {
                "items": items,
                "items_count": len(items),
                "total_count": total if isinstance(total, int) and total >= 0 else None,
                "reported_cost_usd": str(cost),
                "provider_task_id": str(task.get("id") or "")[:100],
            }
        except DataForSEOError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError, TimeoutError):
            raise DataForSEOError("Keyword provider outcome could not be confirmed.") from None
