"""How AI assistants answer buyer questions, measured through DataForSEO AI Optimization.

One call asks a bounded prompt panel on up to six answer engines and returns one row per
prompt and engine: the trimmed answer, the URLs it cites, whether the brand is mentioned,
cited or recommended first, and what the call cost.

Two kinds of measurement are kept apart on every row:

- ``consumer_app_answer``: what a person sees in the product. DataForSEO's LLM Scraper
  collects ChatGPT and Gemini from their consumer interfaces; its SERP API collects Google
  AI Mode and the AI Overview on a Google results page.
- ``api_model_answer``: what the vendor's API model says. DataForSEO's LLM Responses API
  asks Claude and Perplexity Sonar models, with web search on. claude.ai and
  perplexity.ai are not measured.

Contracts (read 2026-09-30):
https://docs.dataforseo.com/v3/ai_optimization/llm_scraper/overview/
https://docs.dataforseo.com/v3/ai_optimization/llm_responses/overview/
https://docs.dataforseo.com/v3/serp/google/ai_mode/task_post/
https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/

Paid requests are never retried automatically. A caller-supplied ledger makes each paid
request at most once per run; an attempt whose outcome is unknown is reported, not repeated.
Collecting a posted task (task_get) is free, so polling needs no receipt.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from tin_lite.dataforseo import DataForSEOError
from tin_lite.keyword_data import HTTP_REFUSALS
from tin_lite.organic_audit import MARKETS
from tin_lite.usage_capture import begin_observation, observe_tool

API_ORIGIN = "https://api.dataforseo.com/v3"
CONSUMER_APP_ANSWER = "consumer_app_answer"
API_MODEL_ANSWER = "api_model_answer"

LIMITS = {
    "prompts": 40,
    # LLM Responses accepts at most 500 characters in user_prompt; the other endpoints more.
    "prompt_chars": 500,
    "aliases": 10,
    "competitors": 20,
    "answer_chars": 4000,
    "cited_urls": 20,
    "url_chars": 2000,
    "max_cost_usd": Decimal(20),
    "deadline_seconds": (60, 3600),
    "response_bytes": 4_000_000,
}
# The LLM Scraper standard queue can take up to 45 minutes (pricing page, 2026-09-30).
DEFAULT_DEADLINE_SECONDS = 2700
POLL_SECONDS = 20
# Live calls hold one receipt lock (one database connection) each while they wait.
LIVE_CONCURRENCY = 4
POLL_CONCURRENCY = 8
LIVE_TIMEOUT_SECONDS = 150  # LLM Responses live calls take up to 120 s.
PRIORITIES = {"standard": 1, "high": 2}


@dataclass(frozen=True)
class Engine:
    key: str
    measurement: str
    surface: str
    mode: str  # "task": post then collect; "live": one call returns the answer
    post: str
    collect: str | None = None
    model: str | None = None
    # USD per request, by queue priority. Live LLM Responses prices are Tin's per-call bound:
    # the vendor charges $0.0006 plus the model provider's cost, which it does not publish.
    price: dict[str, Decimal] = field(default_factory=dict)


# Prices checked 2026-09-30 on https://dataforseo.com/pricing/ai-optimization/llm-scraper,
# /pricing/ai-optimization/llm-responses, /pricing/google-serp/google-ai-mode-serp-api and
# /pricing/google-serp/google-organic-serp-api.
ENGINES: dict[str, Engine] = {
    "chatgpt": Engine(
        key="chatgpt",
        measurement=CONSUMER_APP_ANSWER,
        surface="ChatGPT app (LLM Scraper)",
        mode="task",
        post="ai_optimization/chat_gpt/llm_scraper/task_post",
        collect="ai_optimization/chat_gpt/llm_scraper/task_get/advanced",
        price={"standard": Decimal("0.0012"), "high": Decimal("0.0024")},
    ),
    "gemini": Engine(
        key="gemini",
        measurement=CONSUMER_APP_ANSWER,
        surface="Gemini app (LLM Scraper)",
        mode="task",
        post="ai_optimization/gemini/llm_scraper/task_post",
        collect="ai_optimization/gemini/llm_scraper/task_get/advanced",
        price={"standard": Decimal("0.0012"), "high": Decimal("0.0024")},
    ),
    "google_ai_mode": Engine(
        key="google_ai_mode",
        measurement=CONSUMER_APP_ANSWER,
        surface="Google AI Mode (SERP API)",
        mode="task",
        post="serp/google/ai_mode/task_post",
        collect="serp/google/ai_mode/task_get/advanced",
        price={"standard": Decimal("0.0012"), "high": Decimal("0.0024")},
    ),
    # Live, so the reported cost already reflects the refund DataForSEO makes when a results
    # page has no AI Overview. There is no separate AI Overview endpoint.
    "google_ai_overview": Engine(
        key="google_ai_overview",
        measurement=CONSUMER_APP_ANSWER,
        surface="Google AI Overview (SERP API)",
        mode="live",
        post="serp/google/organic/live/advanced",
        price={"standard": Decimal("0.004"), "high": Decimal("0.004")},
    ),
    # LLM Responses reports the provider's charge only when the answer exists, so both API
    # engines run live: the receipt then carries the final cost, not the $0.01 advance.
    "claude": Engine(
        key="claude",
        measurement=API_MODEL_ANSWER,
        surface="Claude API model with web search (LLM Responses)",
        mode="live",
        post="ai_optimization/claude/llm_responses/live",
        model="claude-sonnet-5",
        price={"standard": Decimal("0.05"), "high": Decimal("0.05")},
    ),
    # Perplexity is live-only on LLM Responses; Sonar models always search the web.
    "perplexity": Engine(
        key="perplexity",
        measurement=API_MODEL_ANSWER,
        surface="Perplexity Sonar API model (LLM Responses)",
        mode="live",
        post="ai_optimization/perplexity/llm_responses/live",
        model="sonar",
        price={"standard": Decimal("0.02"), "high": Decimal("0.02")},
    ),
}
MAX_OUTPUT_TOKENS = 1024
PENDING = {40601, 40602, 20100}
NO_RESULTS = {40102}


class CostCeilingExceeded(ValueError):
    """The estimate is above the caller's ceiling; nothing was posted."""

    def __init__(self, estimate: Decimal, ceiling: Decimal) -> None:
        super().__init__(f"Estimated cost ${estimate} exceeds the ${ceiling} ceiling.")
        self.estimate, self.ceiling = estimate, ceiling


class ProviderRejected(DataForSEOError):
    """DataForSEO answered and refused the request; nothing was bought."""


@dataclass(frozen=True)
class Brand:
    name: str
    domain: str
    aliases: tuple[str, ...] = ()
    competitors: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class AIAnswersRequest:
    prompts: tuple[str, ...]
    engines: tuple[str, ...]
    brand: Brand
    max_cost_usd: Decimal
    market: str = "US"
    language_code: str = "en"
    priority: str = "standard"
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS

    @classmethod
    def from_inputs(cls, inputs: dict[str, Any]) -> AIAnswersRequest:
        """Validate plain JSON inputs. Raises ValueError with a safe message."""
        if not isinstance(inputs, dict):
            raise ValueError("AI answer inputs must be an object.")
        prompts = inputs.get("prompts")
        if not isinstance(prompts, list) or not 1 <= len(prompts) <= LIMITS["prompts"]:
            raise ValueError(f"Give between 1 and {LIMITS['prompts']} prompts.")
        cleaned = tuple(_text(p, "prompt", LIMITS["prompt_chars"]) for p in prompts)
        if len({p.casefold() for p in cleaned}) != len(cleaned):
            raise ValueError("Prompts must not repeat.")
        engines = inputs.get("engines", list(ENGINES))
        if (
            not isinstance(engines, list)
            or not engines
            or len(set(engines)) != len(engines)
            or any(engine not in ENGINES for engine in engines)
        ):
            raise ValueError("Engines must be distinct and among: " + ", ".join(ENGINES) + ".")
        market = inputs.get("market", "US")
        if market not in MARKETS:
            raise ValueError("Market must be one of: " + ", ".join(MARKETS) + ".")
        language = inputs.get("language_code", "en")
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
            raise ValueError("language_code must be a two-letter code.")
        priority = inputs.get("priority", "standard")
        if priority not in PRIORITIES:
            raise ValueError("priority must be standard or high.")
        deadline = inputs.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)
        low, high = LIMITS["deadline_seconds"]
        if type(deadline) is not int or not low <= deadline <= high:
            raise ValueError(f"deadline_seconds must be between {low} and {high}.")
        ceiling = _money(inputs.get("max_cost_usd"))
        if ceiling is None or not 0 < ceiling <= LIMITS["max_cost_usd"]:
            raise ValueError(f"max_cost_usd must be above 0 and at most {LIMITS['max_cost_usd']}.")
        brand = inputs.get("brand")
        if not isinstance(brand, dict):
            raise ValueError("brand must be an object with name and domain.")
        aliases = brand.get("aliases", [])
        competitors = brand.get("competitors", [])
        if not isinstance(aliases, list) or len(aliases) > LIMITS["aliases"]:
            raise ValueError(f"Give at most {LIMITS['aliases']} brand aliases.")
        if not isinstance(competitors, list) or len(competitors) > LIMITS["competitors"]:
            raise ValueError(f"Give at most {LIMITS['competitors']} competitors.")
        return cls(
            prompts=cleaned,
            engines=tuple(engines),
            brand=Brand(
                name=_text(brand.get("name"), "brand name", 100, minimum=2),
                domain=domain_of(brand.get("domain")),
                aliases=tuple(_text(a, "alias", 100, minimum=2) for a in aliases),
                competitors=tuple(_text(c, "competitor", 100, minimum=2) for c in competitors),
            ),
            max_cost_usd=ceiling,
            market=market,
            language_code=language,
            priority=priority,
            deadline_seconds=deadline,
        )

    def as_inputs(self) -> dict[str, Any]:
        """The JSON form ``from_inputs`` accepts; saved in a receipt, never in Temporal."""
        return {
            "prompts": list(self.prompts),
            "engines": list(self.engines),
            "brand": {
                "name": self.brand.name,
                "domain": self.brand.domain,
                "aliases": list(self.brand.aliases),
                "competitors": list(self.brand.competitors),
            },
            "max_cost_usd": str(self.max_cost_usd),
            "market": self.market,
            "language_code": self.language_code,
            "priority": self.priority,
            "deadline_seconds": self.deadline_seconds,
        }


def _text(value: Any, label: str, maximum: int, *, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Each {label} must be text.")
    cleaned = " ".join(value.split())
    if not minimum <= len(cleaned) <= maximum or any(ord(c) < 32 for c in cleaned):
        raise ValueError(f"Each {label} must be {minimum}-{maximum} printable characters.")
    return cleaned


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def domain_of(value: Any) -> str:
    """A bare hostname from a domain or URL, without www. Raises ValueError otherwise."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("brand domain must be a hostname such as example.com.")
    candidate = value.strip()
    try:
        host = urlsplit(candidate if "://" in candidate else f"//{candidate}").hostname or ""
    except ValueError:
        host = ""
    host = host.casefold().removesuffix(".").removeprefix("www.")
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
        raise ValueError("brand domain must be a hostname such as example.com.")
    return host


def estimate_cost(request: AIAnswersRequest) -> Decimal:
    """Upper-bound estimate for the whole panel, from the pinned per-request prices."""
    per_prompt = sum(ENGINES[e].price[request.priority] for e in request.engines)
    return per_prompt * len(request.prompts)


# --------------------------------------------------------------------------- parsing


def _cited(values: list[Any]) -> list[str]:
    urls: list[str] = []
    for value in values:
        url = value.get("url") if isinstance(value, dict) else None
        if (
            isinstance(url, str)
            and len(url) <= LIMITS["url_chars"]
            and url.startswith(("https://", "http://"))
            and not any(c.isspace() for c in url)
            and url not in urls
        ):
            urls.append(url)
    return urls


def _result(task: dict) -> dict:
    results = task.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise DataForSEOError("Answer result envelope is invalid.")
    return results[0]


def parse_answer(engine: str, task: dict) -> dict:
    """Answer text and cited URLs from one completed task. No answer gives empty text."""
    if task.get("status_code") in NO_RESULTS or (
        task.get("result") is None and task.get("result_count") == 0
    ):
        return {"text": "", "cited_urls": [], "model": None}
    result = _result(task)
    items = [i for i in result.get("items") or [] if isinstance(i, dict)]
    if engine in {"chatgpt", "gemini"}:
        sources = list(result.get("sources") or [])
        for item in items:
            sources.extend(item.get("sources") or [])
        text = result.get("markdown") or ""
        model = result.get("model")
    elif engine in {"google_ai_mode", "google_ai_overview"}:
        overview = next((i for i in items if i.get("type") == "ai_overview"), None)
        if overview is None:
            return {"text": "", "cited_urls": [], "model": None}
        sources = list(overview.get("references") or [])
        for element in overview.get("items") or []:
            if isinstance(element, dict):
                sources.extend(element.get("references") or [])
        text = overview.get("markdown") or ""
        if not text:
            text = "\n\n".join(
                str(e.get("markdown") or e.get("text") or "")
                for e in overview.get("items") or []
                if isinstance(e, dict)
            ).strip()
        model = None
    else:
        sources, parts = [], []
        for item in items:
            if item.get("type") != "message":
                continue
            for section in item.get("sections") or []:
                if isinstance(section, dict) and section.get("type") == "text":
                    parts.append(str(section.get("text") or ""))
                    sources.extend(section.get("annotations") or [])
        text = "\n\n".join(p for p in parts if p)
        model = result.get("model_name")
    if not isinstance(text, str):
        raise DataForSEOError("Answer text is not text.")
    return {
        "text": text,
        "cited_urls": _cited(sources),
        "model": str(model)[:100] if model else None,
    }


def trim(text: str, limit: int = LIMITS["answer_chars"]) -> tuple[str, bool]:
    """Keep at most ``limit`` characters, cut on a word boundary when one is near."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.8:
        cut = cut[:space]
    return cut.rstrip() + " …", True


# --------------------------------------------------------------------- brand detection

# A numbered or bulleted line, also as a heading ("### 1. Acme") or in bold ("**1. Acme**").
_LIST_ITEM = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*)?(?:\d{1,2}[.)]|[-*•+])[ \t]+\S", re.M
)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _first_offset(text: str, names: tuple[str, ...]) -> int | None:
    offsets = [
        m.start()
        for name in names
        if len(name) >= 2
        for m in [re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)]
        if m
    ]
    return min(offsets) if offsets else None


def _cites_domain(url: str, domain: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold().removesuffix(".")
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


def _first_unit(text: str) -> tuple[int, int]:
    """Where the answer's first recommendation sits: its first list item, else sentence."""
    match = _LIST_ITEM.search(text)
    if match:
        end = text.find("\n", match.start())
        return match.start(), len(text) if end < 0 else end
    stripped = len(text) - len(text.lstrip())
    end = _SENTENCE_END.search(text, stripped)
    return stripped, len(text) if end is None else end.end()


def detect_brand(text: str, cited_urls: list[str], brand: Brand) -> dict[str, bool]:
    """Mentioned: a name, alias or the domain appears in the answer.

    Cited: a cited URL is on the brand's domain or a subdomain. Recommended first: the brand
    is named inside the answer's first list item (or its first sentence when there is no
    list), and no named competitor appears before it.
    """
    names = (*brand.names, brand.domain)
    first = _first_offset(text, names)
    mentioned = first is not None
    cited = any(_cites_domain(url, brand.domain) for url in cited_urls)
    recommended_first = False
    if mentioned:
        start, end = _first_unit(text)
        in_unit = _first_offset(text[start:end], names) is not None
        rival = _first_offset(text, brand.competitors) if brand.competitors else None
        recommended_first = in_unit and (rival is None or first <= rival)
    return {"mentioned": mentioned, "cited": cited, "recommended_first": recommended_first}


# ------------------------------------------------------------------------- transport

_SESSION: ContextVar[tuple[AIAnswersClient, httpx.AsyncClient] | None] = ContextVar(
    "ai_answers_session", default=None
)


class AIAnswersClient:
    """Bounded DataForSEO AI Optimization and SERP answer calls. No retries, no caller URLs."""

    def __init__(self, login: str, password: str, *, transport=None) -> None:
        self._auth = httpx.BasicAuth(login, password)
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=self._auth,
            timeout=LIVE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    @asynccontextmanager
    async def session(self):
        """Let the calls inside share one client and its connection pool."""
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

    async def _call(
        self, method: str, path: str, payload: list | None = None, *, observation=None
    ) -> dict:
        """One request. A refusal of the whole request is a known outcome, raised as
        ProviderRejected after `observation` is receipted at the cost DataForSEO reported."""
        shared = _SESSION.get()
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(asyncio.timeout(LIVE_TIMEOUT_SECONDS + 10))
            client = (
                shared[1]
                if shared is not None and shared[0] is self
                else await stack.enter_async_context(self._client())
            )
            response = await stack.enter_async_context(
                client.stream(method, f"{API_ORIGIN}/{path}", json=payload)
            )
            if response.status_code in HTTP_REFUSALS:
                # Refused at the door (credentials, balance, rate): nothing was bought.
                code = HTTP_REFUSALS[response.status_code]
                await observe_tool(observation, {"cost": 0})
                raise ProviderRejected(f"DataForSEO refused the request (status {code}).")
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > LIMITS["response_bytes"]:
                    raise DataForSEOError("Answer response exceeded its size bound.")
        data = json.loads(body)
        status = data.get("status_code") if isinstance(data, dict) else None
        if type(status) is int and status != 20000 and not data.get("tasks"):
            # DataForSEO refused the whole request and ran no task, at the cost it reports.
            cost = _money(data.get("cost", 0))
            if cost is None:
                raise DataForSEOError("Answer refusal has invalid cost metadata.")
            await observe_tool(observation, {"cost": str(cost)})
            raise ProviderRejected(f"DataForSEO refused the request (status {status}).")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise DataForSEOError("Answer response has an invalid envelope.")
        return data

    async def post_tasks(self, engine: str, requests: list[dict]) -> dict:
        """Post one engine's tasks in one request. Returns task ids and costs by tag."""
        spec = ENGINES[engine]
        if spec.mode != "task" or not 1 <= len(requests) <= 100:
            raise ValueError("Only task engines post, at most 100 tasks at a time.")
        tags = [r["tag"] for r in requests]
        observation = await begin_observation("dataforseo", "tool", spec.post)
        try:
            data = await self._call("POST", spec.post, requests, observation=observation)
            tasks = data["tasks"]
            costs = [_money(t.get("cost")) if isinstance(t, dict) else None for t in tasks]
            if any(c is None for c in costs):
                raise DataForSEOError("Answer tasks have invalid cost metadata.")
            await observe_tool(observation, {"cost": str(sum(costs, Decimal(0)))})
            if data.get("status_code") != 20000:
                raise ProviderRejected("DataForSEO refused the answer tasks.")
            posted: dict[str, dict] = {}
            for task, cost in zip(tasks, costs, strict=True):
                tag = (task.get("data") or {}).get("tag")
                if tag not in tags or tag in posted:
                    raise DataForSEOError("Answer task tags do not match the request.")
                accepted = task.get("status_code") == 20100
                posted[tag] = {
                    "task_id": str(UUID(task["id"])) if accepted else None,
                    "status_code": task.get("status_code"),
                    "cost_usd": str(cost),
                }
            return {"tasks": posted}
        except DataForSEOError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError) as exc:
            raise DataForSEOError("Answer task outcome could not be confirmed.") from exc

    async def collect(self, engine: str, task_id: str) -> dict | None:
        """One posted task's result, or None while it is still queued. Free to call."""
        spec = ENGINES[engine]
        try:
            data = await self._call("GET", f"{spec.collect}/{UUID(task_id)}")
            if data.get("status_code") != 20000 or len(data["tasks"]) != 1:
                raise DataForSEOError("Answer collection was not successful.")
            task = data["tasks"][0]
            if not isinstance(task, dict) or str(task.get("id")) != str(UUID(task_id)):
                raise DataForSEOError("Answer collection returned another task.")
            return None if task.get("status_code") in PENDING else task
        except DataForSEOError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError) as exc:
            raise DataForSEOError("Answer collection is unavailable.") from exc

    async def live(self, engine: str, request: dict) -> dict:
        """One live answer. The task's reported cost is receipted before it is checked."""
        spec = ENGINES[engine]
        if spec.mode != "live":
            raise ValueError("This engine posts tasks; it has no live call here.")
        observation = await begin_observation("dataforseo", "tool", spec.post)
        try:
            data = await self._call("POST", spec.post, [request], observation=observation)
            if len(data["tasks"]) != 1 or not isinstance(data["tasks"][0], dict):
                raise DataForSEOError("Live answer has an invalid task envelope.")
            task = data["tasks"][0]
            cost = _money(task.get("cost"))
            if cost is None:
                raise DataForSEOError("Live answer has invalid cost metadata.")
            await observe_tool(observation, {"cost": str(cost)})
            if data.get("status_code") != 20000 or (
                task.get("status_code") != 20000 and task.get("status_code") not in NO_RESULTS
            ):
                raise ProviderRejected("DataForSEO did not return the live answer.")
            return task
        except DataForSEOError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError) as exc:
            raise DataForSEOError("Live answer outcome could not be confirmed.") from exc


def task_request(request: AIAnswersRequest, engine: str, index: int, tag: str) -> dict:
    """The exact provider request for one prompt on one engine."""
    prompt = request.prompts[index]
    location = MARKETS[request.market]
    if engine in {"chatgpt", "gemini"}:
        return {
            "keyword": prompt,
            "location_code": location,
            "language_code": request.language_code,
            "priority": PRIORITIES[request.priority],
            "tag": tag,
        }
    if engine == "google_ai_mode":
        return {
            "keyword": prompt,
            "location_code": location,
            "language_code": request.language_code,
            "priority": PRIORITIES[request.priority],
            "device": "desktop",
            "os": "windows",
            "tag": tag,
        }
    if engine == "google_ai_overview":
        return {
            "keyword": prompt,
            "location_code": location,
            "language_code": request.language_code,
            "device": "desktop",
            "os": "windows",
            "depth": 10,
            "load_async_ai_overview": True,
            "tag": tag,
        }
    if engine == "claude":
        return {
            "user_prompt": prompt,
            "model_name": ENGINES[engine].model,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "web_search": True,
            "web_search_country_iso_code": request.market,
            "tag": tag,
        }
    if engine == "perplexity":
        return {
            "user_prompt": prompt,
            "model_name": ENGINES[engine].model,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "web_search_country_iso_code": request.market,
            "tag": tag,
        }
    raise ValueError("Unknown answer engine.")


# ------------------------------------------------------------------------ measurement


class Ledger(Protocol):
    async def once(
        self, name: str, *, reserve_usd: Decimal, call: Callable[[], Awaitable[dict]]
    ) -> dict:
        """Run a paid call at most once and return its saved outcome.

        Returns ``{"status": "completed", "value": ...}``, ``{"status": "failed", "reason": ...}``
        or ``{"status": "unknown", "reason": ...}``. A completed outcome is returned again
        without calling; an attempt that left no outcome is never repeated.
        """


class MemoryLedger:
    """Process-local ledger for tests and callers without receipts. Not durable."""

    def __init__(self) -> None:
        self.outcomes: dict[str, dict] = {}
        self.attempted: set[str] = set()
        self.reserved: dict[str, Decimal] = {}

    async def once(self, name, *, reserve_usd, call):
        if name in self.outcomes:
            return self.outcomes[name]
        if name in self.attempted:
            return {"status": "unknown", "reason": "unconfirmed_previous_request"}
        self.attempted.add(name)
        self.reserved[name] = reserve_usd
        self.outcomes[name] = await call_outcome(call)
        return self.outcomes[name]


async def call_outcome(call: Callable[[], Awaitable[dict]]) -> dict:
    """Map a paid call's exception to a saved outcome. BillingError is left to the caller."""
    try:
        return {"status": "completed", "value": await call()}
    except ProviderRejected:
        return {"status": "failed", "reason": "provider_rejected"}
    except DataForSEOError:
        return {"status": "unknown", "reason": "provider_result_unavailable"}


def _row(request: AIAnswersRequest, engine: str, index: int) -> dict:
    spec = ENGINES[engine]
    return {
        "prompt_index": index,
        "prompt": request.prompts[index],
        "engine": engine,
        "measurement": spec.measurement,
        "surface": spec.surface,
        "status": "pending",
        "reason": None,
        "model": spec.model,
        "answer": "",
        "answer_truncated": False,
        "cited_urls": [],
        "brand": {"mentioned": False, "cited": False, "recommended_first": False},
        "cost_usd": None,
        "provider_task_id": None,
    }


def _fill(row: dict, engine: str, task: dict, brand: Brand) -> None:
    try:
        answer = parse_answer(engine, task)
    except (DataForSEOError, TypeError, AttributeError, ValueError):
        row.update(status="failed", reason="unreadable_result")
        return
    urls = answer["cited_urls"]
    text, truncated = trim(answer["text"])
    row.update(
        status="answered" if text else "no_answer",
        reason=None if text else "engine_gave_no_answer",
        answer=text,
        answer_truncated=truncated,
        cited_urls=urls[: LIMITS["cited_urls"]],
        # Detection reads the full answer and every citation, not the trimmed copy.
        brand=detect_brand(answer["text"], urls, brand),
        model=answer["model"] or row["model"],
    )


def summarize(request: AIAnswersRequest, rows: list[dict], estimate: Decimal) -> dict:
    statuses = ("answered", "no_answer", "failed", "timeout", "unknown")
    by_engine: dict[str, dict] = {}
    total = Decimal(0)
    unconfirmed = 0
    for engine in request.engines:
        mine = [r for r in rows if r["engine"] == engine]
        costs = [_money(r["cost_usd"]) for r in mine]
        spent = sum((c for c in costs if c is not None), Decimal(0))
        missing = sum(
            c is None and r["status"] == "unknown" for c, r in zip(costs, mine, strict=True)
        )
        total += spent
        unconfirmed += missing
        by_engine[engine] = {
            "measurement": ENGINES[engine].measurement,
            **{s: sum(r["status"] == s for r in mine) for s in statuses},
            **{k: sum(r["brand"][k] for r in mine) for k in ("mentioned", "cited")},
            "recommended_first": sum(r["brand"]["recommended_first"] for r in mine),
            "cost_usd": str(spent),
            "cost_unconfirmed_rows": missing,
        }
    return {
        "prompts": len(request.prompts),
        "engines": list(request.engines),
        "rows": len(rows),
        **{s: sum(r["status"] == s for r in rows) for s in statuses},
        "mentioned": sum(r["brand"]["mentioned"] for r in rows),
        "cited": sum(r["brand"]["cited"] for r in rows),
        "recommended_first": sum(r["brand"]["recommended_first"] for r in rows),
        "complete": all(r["status"] in {"answered", "no_answer"} for r in rows),
        "cost_usd": str(total),
        "cost_unconfirmed_rows": unconfirmed,
        "estimate_usd": str(estimate),
        "max_cost_usd": str(request.max_cost_usd),
        "by_engine": by_engine,
    }


async def measure(
    client: AIAnswersClient,
    request: AIAnswersRequest,
    *,
    ledger: Ledger,
    tag: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    poll_seconds: float = POLL_SECONDS,
) -> dict:
    """Ask every prompt on every engine and return ``{"rows": [...], "summary": {...}}``.

    Raises CostCeilingExceeded before any request when the estimate is above the ceiling.
    One engine failing never fails the others; its rows say why. Tasks still queued at the
    deadline are reported as ``timeout`` with their provider task id.
    """
    estimate = estimate_cost(request)
    if estimate > request.max_cost_usd:
        raise CostCeilingExceeded(estimate, request.max_cost_usd)
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", tag):
        raise ValueError("The request tag must be a short identifier.")
    rows = {
        (engine, index): _row(request, engine, index)
        for index in range(len(request.prompts))
        for engine in request.engines
    }
    tags = {key: f"{tag}:{key[0]}:{key[1]}" for key in rows}
    posted_at: list[datetime] = []
    waiting: dict[tuple[str, int], str] = {}
    async with client.session():
        # 1. Post each task engine's panel in one request.
        for engine in (e for e in request.engines if ENGINES[e].mode == "task"):
            indexes = range(len(request.prompts))
            payload = [task_request(request, engine, i, tags[(engine, i)]) for i in indexes]

            async def post(engine=engine, payload=payload) -> dict:
                # The deadline counts from the first post, so a retried activity keeps it.
                return {
                    **await client.post_tasks(engine, payload),
                    "posted_at": clock().isoformat(),
                }

            outcome = await ledger.once(
                f"post:{engine}",
                reserve_usd=ENGINES[engine].price[request.priority] * len(payload),
                call=post,
            )
            for index in indexes:
                row = rows[(engine, index)]
                if outcome["status"] != "completed":
                    row.update(status=outcome["status"], reason=outcome.get("reason"))
                    continue
                task = outcome["value"]["tasks"].get(tags[(engine, index)])
                if task is None:
                    row.update(status="failed", reason="task_missing_from_response")
                    continue
                row["cost_usd"] = task["cost_usd"]
                if task["task_id"] is None:
                    row.update(status="failed", reason=f"provider_status_{task['status_code']}")
                    continue
                row["provider_task_id"] = task["task_id"]
                waiting[(engine, index)] = task["task_id"]
            if outcome["status"] == "completed":
                posted_at.append(datetime.fromisoformat(outcome["value"]["posted_at"]))

        # 2. Ask the live engines, a few calls at a time.
        gate = asyncio.Semaphore(LIVE_CONCURRENCY)

        async def ask(engine: str, index: int) -> None:
            row = rows[(engine, index)]
            payload = task_request(request, engine, index, tags[(engine, index)])
            async with gate:
                outcome = await ledger.once(
                    f"live:{engine}:{index}",
                    reserve_usd=ENGINES[engine].price[request.priority],
                    call=lambda: client.live(engine, payload),
                )
            if outcome["status"] != "completed":
                row.update(status=outcome["status"], reason=outcome.get("reason"))
                return
            task = outcome["value"]
            row["cost_usd"] = str(_money(task.get("cost")))
            row["provider_task_id"] = str(task.get("id") or "")[:64] or None
            _fill(row, engine, task, request.brand)

        # 3. Collect posted tasks while the live answers arrive, until the deadline.
        reader = asyncio.Semaphore(POLL_CONCURRENCY)

        async def read(key: tuple[str, int], task_id: str) -> None:
            async with reader:
                try:
                    task = await client.collect(key[0], task_id)
                except DataForSEOError:
                    return  # Collection is free; try again next round.
            if task is None:
                return
            waiting.pop(key, None)
            code = task.get("status_code")
            if code == 20000 or code in NO_RESULTS:
                _fill(rows[key], key[0], task, request.brand)
            else:
                rows[key].update(status="failed", reason=f"provider_status_{code}")

        async def collect_all() -> None:
            if not waiting:
                return
            deadline = min(posted_at) + timedelta(seconds=request.deadline_seconds)
            while waiting:
                await asyncio.gather(*(read(k, t) for k, t in list(waiting.items())))
                if not waiting:
                    return
                if clock() >= deadline:
                    for key in waiting:
                        rows[key].update(status="timeout", reason="not_ready_by_deadline")
                    return
                await sleep(poll_seconds)

        await asyncio.gather(
            collect_all(),
            *(ask(engine, index) for (engine, index) in rows if ENGINES[engine].mode == "live"),
        )

    ordered = [rows[(engine, i)] for i in range(len(request.prompts)) for engine in request.engines]
    return {"rows": ordered, "summary": summarize(request, ordered, estimate)}
