"""Services Tin holds the key for: PageSpeed Insights with CrUX, and DataForSEO live reads.

A package binds `managed.pagespeed` or `managed.dataforseo` the way it binds any provider: a
required `integration_requirements` entry plus one `code.services` alias. There is no founder
connection. Tin's own settings hold the credentials; they stay on the switchboard and never
reach the sandbox. Each call goes through the same service gateway, receipts and replay rules
as connected providers. DataForSEO reads are paid: each call reserves a per-call ceiling
before dispatch and settles the cost DataForSEO reports, through `service_pricing`, exactly
as the native keyword workflows do. PageSpeed Insights and CrUX are free.

Only live (synchronous) endpoints are exposed. DataForSEO task_post endpoints, such as the
OnPage crawl, stay native.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from tin_lite.connection_records import ServiceArgumentError, fit_records, text
from tin_lite.integrations import (
    IntegrationDefinition,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
)

PAGESPEED_PROVIDER = "managed.pagespeed"
DATAFORSEO_PROVIDER = "managed.dataforseo"
MANAGED_KEY = re.compile(r"managed\.[a-z][a-z0-9_]{0,47}\Z")
CAPABILITIES = {
    PAGESPEED_PROVIDER: ("pagespeed.read", "crux.read"),
    DATAFORSEO_PROVIDER: ("serp.read", "keywords.read", "backlinks.read"),
}
# The settings each provider needs, named by their environment variables in errors.
SETTINGS = {
    PAGESPEED_PROVIDER: (("pagespeed_api_key",), "TIN_LITE_PAGESPEED_API_KEY"),
    DATAFORSEO_PROVIDER: (
        ("dataforseo_login", "dataforseo_password"),
        "DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD",
    ),
}
DEFINITIONS = {
    PAGESPEED_PROVIDER: IntegrationDefinition(
        PAGESPEED_PROVIDER,
        "PageSpeed Insights and CrUX",
        "Tin",
        "Google's lab Lighthouse scores and Chrome field data for public pages, on Tin's key.",
        "Public page measurements; no project data",
        CAPABILITIES[PAGESPEED_PROVIDER],
        ("Code workflows", "Codex procedures"),
    ),
    DATAFORSEO_PROVIDER: IntegrationDefinition(
        DATAFORSEO_PROVIDER,
        "DataForSEO",
        "Tin",
        "Live SERP, keyword and backlink reads on Tin's DataForSEO account, charged per call.",
        "Public search data; no project data",
        CAPABILITIES[DATAFORSEO_PROVIDER],
        ("Code workflows",),
    ),
}
# The provider name each managed service's usage observations carry.
USAGE_PROVIDER = {PAGESPEED_PROVIDER: "pagespeed", DATAFORSEO_PROVIDER: "dataforseo"}
# What one paid call reserves before dispatch. The reported cost settles it; a supplier
# overrun is Tin's loss, not a larger customer charge. At the argument bounds below every
# live read costs well under this ($0.03 at most at DataForSEO's September 2026 prices).
CALL_CEILING_USD = {DATAFORSEO_PROVIDER: "0.05"}
# Tin's own wait for Google, under the gateway's 25-second per-call limit, so a slow
# Lighthouse run comes back as a "timed_out" result rather than an unresolved request.
GOOGLE_SECONDS = 22
MAX_GOOGLE_BYTES = 12_000_000
PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
PAGESPEED_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
CRUX_METRICS = {
    "largest_contentful_paint": "lcp_ms",
    "interaction_to_next_paint": "inp_ms",
    "cumulative_layout_shift": "cls",
    "first_contentful_paint": "fcp_ms",
    "experimental_time_to_first_byte": "ttfb_ms",
}
LIGHTHOUSE_CODE = re.compile(r"\b([A-Z][A-Z0-9_]{2,39})\b")


def is_managed(provider_key: Any) -> bool:
    return isinstance(provider_key, str) and provider_key in DEFINITIONS


def configured(settings: Any, provider_key: str) -> bool:
    names, _ = SETTINGS[provider_key]
    return all(getattr(settings, name, None) for name in names)


def not_configured(provider_key: str) -> str:
    name, (_, env) = DEFINITIONS[provider_key].name, SETTINGS[provider_key]
    return f"{name} is not configured on this Tin deployment; an operator sets {env}."


def paid(provider_key: str) -> bool:
    return provider_key in CALL_CEILING_USD


# ---------------------------------------------------------------- operations and arguments


@dataclass(frozen=True)
class Operation:
    capability: str
    arguments: frozenset[str]
    endpoint: str | None = None


OPERATIONS = {
    (PAGESPEED_PROVIDER, "pagespeed.run"): Operation(
        "pagespeed.read", frozenset({"url", "strategy", "categories"})
    ),
    (PAGESPEED_PROVIDER, "crux.query"): Operation(
        "crux.read", frozenset({"origin", "url", "form_factor"})
    ),
    (DATAFORSEO_PROVIDER, "serp.organic"): Operation(
        "serp.read",
        frozenset({"keyword", "location_code", "language_code", "device", "depth"}),
        "serp/google/organic/live/advanced",
    ),
    (DATAFORSEO_PROVIDER, "keywords.ideas"): Operation(
        "keywords.read",
        frozenset({"keywords", "location_code", "language_code", "limit", "offset"}),
        "dataforseo_labs/google/keyword_ideas/live",
    ),
    (DATAFORSEO_PROVIDER, "keywords.overview"): Operation(
        "keywords.read",
        frozenset({"keywords", "location_code", "language_code"}),
        "dataforseo_labs/google/keyword_overview/live",
    ),
    (DATAFORSEO_PROVIDER, "backlinks.summary"): Operation(
        "backlinks.read", frozenset({"target", "include_subdomains"}), "backlinks/summary/live"
    ),
    (DATAFORSEO_PROVIDER, "backlinks.referring_domains"): Operation(
        "backlinks.read",
        frozenset({"target", "include_subdomains", "limit", "offset"}),
        "backlinks/referring_domains/live",
    ),
}
# Bounds on list arguments and pages; each keeps one call inside its reservation.
BOUNDS = {
    "keywords.ideas": {"keywords": 20, "limit": 100},
    "keywords.overview": {"keywords": 50},
    "backlinks.referring_domains": {"limit": 100},
    "serp.organic": {"depth": 100},
    "offset": 10_000,
}


def _integer(args, name, default, low, high):
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise ServiceArgumentError(f"{name} must be an integer from {low} to {high}")
    return value


def _choice(args, name, default, allowed):
    value = args.get(name, default)
    if value not in allowed:
        raise ServiceArgumentError(f"{name} must be one of {', '.join(allowed)}")
    return value


def _phrase(value, name="keyword"):
    from tin_lite.keyword_plan import phrase

    try:
        value = phrase(value)
    except ValueError:
        raise ServiceArgumentError(f"{name} must be 1-80 printable characters") from None
    if len(value) > 80:
        raise ServiceArgumentError(f"{name} must be 1-80 printable characters")
    return value


def _phrases(args, maximum):
    values = args.get("keywords")
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ServiceArgumentError(f"keywords must be a list of 1-{maximum} phrases")
    found = {}
    for value in values:
        clean = _phrase(value, "each keyword")
        found.setdefault(clean.casefold(), clean)
    return list(found.values())


def _market(args):
    location = _integer(args, "location_code", 2840, 1, 99_999_999)
    language = args.get("language_code", "en")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2,4})?", language):
        raise ServiceArgumentError("language_code must be a language code such as en or pt-BR")
    return {"location_code": location, "language_code": language}


def _public_url(value, name="url"):
    from tin_lite.keyword_plan import safe_url

    if not isinstance(value, str) or safe_url(value) is None or urlsplit(value).fragment:
        raise ServiceArgumentError(
            f"{name} must be a public http(s) URL without credentials or a fragment"
        )
    return value


def _target(value):
    """A bare domain (no scheme, leading www. dropped) or an absolute page URL."""
    from tin_lite.keyword_plan import host

    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return _public_url(value, "target")
    try:
        domain = host(value if isinstance(value, str) else "")
    except (ValueError, UnicodeError):
        raise ServiceArgumentError(
            "target must be a domain such as example.com or an absolute page URL"
        ) from None
    return domain.removeprefix("www.")


def request_for(operation: str, args: Any) -> dict[str, Any]:
    """The normalized provider request for one call; raises ServiceArgumentError."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ServiceArgumentError("arguments must be an object")
    provider = next((p for p, name in OPERATIONS if name == operation), None)
    spec = OPERATIONS.get((provider, operation))
    if spec is None:
        raise ServiceArgumentError("unknown operation")
    extra = set(args) - spec.arguments
    if extra:
        raise ServiceArgumentError(f"unsupported argument {sorted(extra)[0]}")
    if operation == "pagespeed.run":
        if "url" not in args:
            raise ServiceArgumentError("url is required")
        categories = args.get("categories", ["performance"])
        if (
            not isinstance(categories, list)
            or not categories
            or len(set(categories)) != len(categories)
            or set(categories) - set(PAGESPEED_CATEGORIES)
        ):
            raise ServiceArgumentError(
                "categories must be a list drawn from " + ", ".join(PAGESPEED_CATEGORIES)
            )
        return {
            "url": _public_url(args["url"]),
            "strategy": _choice(args, "strategy", "mobile", ("mobile", "desktop")),
            "categories": [c for c in PAGESPEED_CATEGORIES if c in categories],
        }
    if operation == "crux.query":
        if ("origin" in args) == ("url" in args):
            raise ServiceArgumentError("give exactly one of origin or url")
        request: dict[str, Any] = {}
        if "origin" in args:
            parts = urlsplit(_public_url(args["origin"], "origin"))
            if parts.path not in {"", "/"} or parts.query:
                raise ServiceArgumentError("origin must be scheme and host only")
            request["origin"] = f"{parts.scheme}://{parts.netloc}"
        else:
            request["url"] = _public_url(args["url"])
        if "form_factor" in args:
            request["formFactor"] = _choice(
                args, "form_factor", None, ("phone", "desktop", "tablet")
            ).upper()
        return request
    if operation == "serp.organic":
        if "keyword" not in args:
            raise ServiceArgumentError("keyword is required")
        return {
            "keyword": _phrase(args["keyword"]),
            **_market(args),
            "device": _choice(args, "device", "desktop", ("desktop", "mobile")),
            "depth": _integer(args, "depth", 10, 1, BOUNDS["serp.organic"]["depth"]),
        }
    if operation.startswith("keywords."):
        bounds = BOUNDS[operation]
        request = {"keywords": _phrases(args, bounds["keywords"]), **_market(args)}
        if operation == "keywords.ideas":
            request["limit"] = _integer(args, "limit", 20, 1, bounds["limit"])
            request["offset"] = _integer(args, "offset", 0, 0, BOUNDS["offset"])
        return request
    if "target" not in args:
        raise ServiceArgumentError("target is required")
    include = args.get("include_subdomains", True)
    if type(include) is not bool:
        raise ServiceArgumentError("include_subdomains must be true or false")
    request = {"target": _target(args["target"]), "include_subdomains": include}
    if operation == "backlinks.referring_domains":
        request["limit"] = _integer(args, "limit", 20, 1, BOUNDS[operation]["limit"])
        request["offset"] = _integer(args, "offset", 0, 0, BOUNDS["offset"])
        request["order_by"] = ["rank,desc"]
    return request


def check_arguments(operation: str, args: Any) -> None:
    """The gateway checks values before a receipt exists, so a bad call is fixable."""
    request_for(operation, args)


# ---------------------------------------------------------------- projections


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _fraction(value):
    """Lighthouse and CrUX report scores and densities as 0-1 numbers or numeric strings."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    value = _number(value)
    return None if value is None else round(float(value), 4)


def _milliseconds(value):
    value = _number(value)
    return None if value is None else round(float(value), 1)


def pagespeed_result(request: dict, payload: dict) -> dict:
    """Scores and the key metrics, never the Lighthouse report itself."""
    lighthouse = payload.get("lighthouseResult") or {}
    base = {"url": request["url"], "strategy": request["strategy"]}
    error = lighthouse.get("runtimeError") or {}
    if isinstance(error, dict) and error.get("code"):
        return {**base, "status": "lighthouse_error", "code": text(error["code"], 60)}
    audits = lighthouse.get("audits") or {}
    categories = lighthouse.get("categories") or {}

    def lab(name):
        return _number((audits.get(name) or {}).get("numericValue"))

    scores = {}
    for name in request["categories"]:
        score = _fraction((categories.get(name) or {}).get("score"))
        scores[name] = None if score is None else round(score * 100)
    cls = lab("cumulative-layout-shift")
    return {
        **base,
        "status": "observed",
        "final_url": text(lighthouse.get("finalDisplayedUrl") or lighthouse.get("finalUrl"), 2000),
        "fetch_time": text(lighthouse.get("fetchTime"), 40),
        "lighthouse_version": text(lighthouse.get("lighthouseVersion"), 20),
        "scores": scores,
        "lab": {
            "lcp_ms": _milliseconds(lab("largest-contentful-paint")),
            "cls": None if cls is None else round(float(cls), 4),
            "tbt_ms": _milliseconds(lab("total-blocking-time")),
            "fcp_ms": _milliseconds(lab("first-contentful-paint")),
            "speed_index_ms": _milliseconds(lab("speed-index")),
        },
        **_pagespeed_field(payload.get("loadingExperience")),
    }


def _pagespeed_field(experience) -> dict:
    """Field data PSI attaches from CrUX, or an explicit "no field data" (never zeros)."""
    metrics = (experience or {}).get("metrics") if isinstance(experience, dict) else None
    if not isinstance(metrics, dict) or not metrics:
        return {"field_status": "no_field_data", "field": None}

    def p75(name, scale=1.0):
        value = _number((metrics.get(name) or {}).get("percentile"))
        return None if value is None else round(float(value) * scale, 4)

    field = {
        "scope": "origin" if experience.get("origin_fallback") else "url",
        "overall": text(experience.get("overall_category"), 30),
        "lcp_ms": p75("LARGEST_CONTENTFUL_PAINT_MS"),
        "inp_ms": p75("INTERACTION_TO_NEXT_PAINT"),
        "cls": p75("CUMULATIVE_LAYOUT_SHIFT_SCORE", 0.01),
        "fcp_ms": p75("FIRST_CONTENTFUL_PAINT_MS"),
        "ttfb_ms": p75("EXPERIMENTAL_TIME_TO_FIRST_BYTE"),
    }
    if all(field[k] is None for k in ("lcp_ms", "inp_ms", "cls", "fcp_ms", "ttfb_ms")):
        return {"field_status": "no_field_data", "field": None}
    return {"field_status": "observed", "field": field}


def crux_result(request: dict, payload: dict) -> dict:
    record = payload.get("record") if isinstance(payload, dict) else None
    key = (record or {}).get("key") or {}
    base = {
        "origin" if "origin" in request else "url": request.get("origin") or request.get("url"),
        "form_factor": (request.get("formFactor") or "ALL").lower(),
    }
    metrics = (record or {}).get("metrics") or {}
    values = {}
    for source, name in CRUX_METRICS.items():
        metric = metrics.get(source)
        if not isinstance(metric, dict):
            values[name] = None
            continue
        p75 = _fraction((metric.get("percentiles") or {}).get("p75"))
        bins = metric.get("histogram") or []
        densities = [_fraction(b.get("density")) if isinstance(b, dict) else None for b in bins]
        densities = (densities + [None, None, None])[:3]
        values[name] = {
            "p75": p75 if name == "cls" or p75 is None else round(p75, 1),
            "good": densities[0],
            "needs_improvement": densities[1],
            "poor": densities[2],
        }
    if all(value is None or value["p75"] is None for value in values.values()):
        return {**base, "status": "no_field_data"}
    period = (record or {}).get("collectionPeriod") or {}

    def day(value):
        if not isinstance(value, dict):
            return None
        parts = [value.get(k) for k in ("year", "month", "day")]
        return "-".join(f"{p:02d}" for p in parts) if all(type(p) is int for p in parts) else None

    return {
        **base,
        "status": "observed",
        "normalized_url": text(key.get("url") or key.get("origin"), 2000),
        "collection_period": {
            "first_date": day(period.get("firstDate")),
            "last_date": day(period.get("lastDate")),
        },
        "metrics": values,
    }


def _intent(item):
    info = item.get("search_intent_info") or {}
    return text(info.get("main_intent"), 30)


def keyword_record(item: dict, *, monthly: bool = False) -> dict:
    info = item.get("keyword_info") or {}
    properties = item.get("keyword_properties") or {}
    record = {
        "keyword": text(item.get("keyword"), 120),
        "search_volume": _number(info.get("search_volume")),
        "keyword_difficulty": _number(properties.get("keyword_difficulty")),
        "cpc": _number(info.get("cpc")),
        "competition": _number(info.get("competition")),
        "intent": _intent(item),
    }
    if monthly:
        months = (
            info.get("monthly_searches") if isinstance(info.get("monthly_searches"), list) else []
        )
        record["monthly"] = [
            [f"{m['year']}-{m['month']:02d}", _number(m.get("search_volume"))]
            for m in months[:12]
            if isinstance(m, dict) and type(m.get("year")) is int and type(m.get("month")) is int
        ]
    return record


def serp_record(item: dict) -> dict:
    return {
        "rank_group": _number(item.get("rank_group")),
        "rank_absolute": _number(item.get("rank_absolute")),
        "domain": text(item.get("domain"), 253),
        "url": text(item.get("url"), 2000),
        "title": text(item.get("title"), 200),
        "description": text(item.get("description"), 300),
    }


def referring_domain_record(item: dict) -> dict:
    return {
        "domain": text(item.get("domain"), 253),
        "rank": _number(item.get("rank")),
        "backlinks": _number(item.get("backlinks")),
        "backlinks_spam_score": _number(item.get("backlinks_spam_score")),
        "first_seen": text(item.get("first_seen"), 40),
        "lost_date": text(item.get("lost_date"), 40),
    }


SUMMARY_FIELDS = (
    "rank",
    "backlinks",
    "backlinks_spam_score",
    "referring_domains",
    "referring_domains_nofollow",
    "referring_main_domains",
    "referring_ips",
    "referring_pages",
    "broken_backlinks",
    "broken_pages",
    "crawled_pages",
)


def dataforseo_result(
    operation: str, request: dict, task: dict, *, max_response_bytes: int
) -> dict:
    """Project one DataForSEO task and keep the leading records that fit the binding."""
    cost = str(Decimal(str(task.get("cost"))))
    results = task.get("result")
    result = results[0] if isinstance(results, list) and results else None
    if result is not None and not isinstance(result, dict):
        raise IntegrationUpstreamError("DataForSEO returned an invalid result")
    result = result or {}
    items = result.get("items") if isinstance(result.get("items"), list) else []
    items = [item for item in items if isinstance(item, dict)]
    if operation == "backlinks.summary":
        return {
            "target": request["target"],
            **{name: _number(result.get(name)) for name in SUMMARY_FIELDS},
            "first_seen": text(result.get("first_seen"), 40),
            "cost_usd": cost,
        }
    total = _number(result.get("total_count"))
    envelope: dict[str, Any] = {"cost_usd": cost}
    if operation == "serp.organic":
        envelope.update(
            keyword=request["keyword"],
            location_code=request["location_code"],
            language_code=request["language_code"],
            device=request["device"],
            se_results_count=_number(result.get("se_results_count")),
            serp_features=sorted(
                {
                    text(item.get("type"), 40)
                    for item in items
                    if item.get("type") != "organic" and isinstance(item.get("type"), str)
                }
            )[:20],
        )
        records = [serp_record(item) for item in items if item.get("type") == "organic"]
        return _fit(records, envelope, max_response_bytes)
    if operation.startswith("keywords."):
        envelope.update(
            location_code=request["location_code"], language_code=request["language_code"]
        )
        records = [keyword_record(item, monthly=operation == "keywords.overview") for item in items]
        if operation == "keywords.overview":
            return _fit(records, envelope, max_response_bytes)
        envelope["total_count"] = total
        return _fit(records, envelope, max_response_bytes, offset=request["offset"], total=total)
    envelope.update(target=request["target"], total_count=total)
    records = [referring_domain_record(item) for item in items]
    return _fit(records, envelope, max_response_bytes, offset=request["offset"], total=total)


def _fit(records, envelope, maximum, *, offset=None, total=None):
    """Leading records under the byte bound. Paged reads say where the next page starts."""
    more = offset is not None and total is not None and offset + len(records) < total
    page = fit_records(
        records,
        max_response_bytes=maximum,
        has_more=more,
        envelope=envelope,
        offset=offset if offset is not None else 0,
    )
    cursor = page.pop("next_cursor")
    if offset is not None:
        # Pass it back as `offset` in a new step to read the next page.
        page["next_offset"] = None if cursor is None else int(cursor)
    # Not paged upstream: fewer records (a smaller depth or fewer keywords) is the only way
    # to see the ones left out, so there is no offset to pass back.
    return page


# ---------------------------------------------------------------- calls


class ManagedServices:
    """Trusted adapter; `transport` lets tests answer with recorded provider responses."""

    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.transport = settings, transport

    async def call(
        self,
        provider: str,
        operation: str,
        arguments: dict,
        *,
        execution_key: str,
        max_response_bytes: int,
    ) -> dict:
        request = request_for(operation, arguments)
        if provider == PAGESPEED_PROVIDER:
            if operation == "pagespeed.run":
                return await self._pagespeed(request)
            return await self._crux(request)
        return await self._dataforseo(
            operation, request, execution_key=execution_key, max_response_bytes=max_response_bytes
        )

    def _google_client(self):
        return httpx.AsyncClient(
            trust_env=False,
            timeout=GOOGLE_SECONDS,
            follow_redirects=False,
            transport=self.transport,
        )

    def _google_key(self) -> str:
        return self.settings.pagespeed_api_key.get_secret_value()

    async def _google(self, method, url, **kwargs) -> tuple[int, Any]:
        """One Google read: (status, parsed body or None). Raises TimeoutError past the limit."""
        async with (
            asyncio.timeout(GOOGLE_SECONDS),
            self._google_client() as client,
            client.stream(
                method, url, headers={"X-Goog-Api-Key": self._google_key()}, **kwargs
            ) as response,
        ):
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_GOOGLE_BYTES:
                    return response.status_code, None
            try:
                return response.status_code, json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                return response.status_code, None

    @staticmethod
    def _refuse(status: int, product: str) -> None:
        if status == 429:
            raise IntegrationRateLimitedError(
                f"Google rate-limited Tin's {product} reads; try again later in a new step."
            )
        if status in {401, 403}:
            raise ServiceCallRefused(
                f"Google refused Tin's {product} key; an operator needs to check "
                "TIN_LITE_PAGESPEED_API_KEY and that the API is enabled for it.",
                code="provider_refused",
            )

    async def _pagespeed(self, request: dict) -> dict:
        base = {"url": request["url"], "strategy": request["strategy"]}
        params = [("url", request["url"]), ("strategy", request["strategy"])]
        params += [("category", name) for name in request["categories"]]
        try:
            status, payload = await self._google("GET", PAGESPEED_ENDPOINT, params=params)
        except (TimeoutError, httpx.TimeoutException):
            # Free and read-only, so a slow run is a known outcome: say so and let the
            # workflow try again under a new step, or report the page without lab data.
            return {**base, "status": "timed_out", "seconds": GOOGLE_SECONDS}
        except (httpx.HTTPError, OSError):
            return {**base, "status": "unavailable", "reason": "request_failed"}
        self._refuse(status, "PageSpeed Insights")
        if status == 200 and isinstance(payload, dict):
            return pagespeed_result(request, payload)
        if status in {400, 500} and isinstance(payload, dict):
            # Lighthouse could not load or measure the page (NO_FCP, FAILED_DOCUMENT_REQUEST).
            message = str((payload.get("error") or {}).get("message") or "")
            match = LIGHTHOUSE_CODE.search(message.split("error:", 1)[-1])
            if "Lighthouse" in message:
                return {
                    **base,
                    "status": "lighthouse_error",
                    "code": match.group(1) if match else "UNKNOWN",
                }
        return {**base, "status": "unavailable", "reason": "provider_error", "http_status": status}

    async def _crux(self, request: dict) -> dict:
        base = {
            "origin" if "origin" in request else "url": request.get("origin") or request.get("url"),
            "form_factor": (request.get("formFactor") or "ALL").lower(),
        }
        body = {**request, "metrics": list(CRUX_METRICS)}
        try:
            status, payload = await self._google("POST", CRUX_ENDPOINT, json=body)
        except (TimeoutError, httpx.TimeoutException):
            return {**base, "status": "timed_out", "seconds": GOOGLE_SECONDS}
        except (httpx.HTTPError, OSError):
            return {**base, "status": "unavailable", "reason": "request_failed"}
        self._refuse(status, "CrUX")
        if status == 404:
            # CrUX has no record for most small sites. That is an answer, not zeros.
            return {**base, "status": "no_field_data"}
        if status == 400:
            raise ServiceCallRefused(
                "CrUX did not accept this origin or URL; check that it is public and canonical.",
                code="invalid_request",
            )
        if status == 200 and isinstance(payload, dict):
            return crux_result(request, payload)
        return {**base, "status": "unavailable", "reason": "provider_error", "http_status": status}

    async def _dataforseo(self, operation, request, *, execution_key, max_response_bytes):
        from tin_lite.keyword_data import DataForSEOTaskError, KeywordData

        spec = OPERATIONS[(DATAFORSEO_PROVIDER, operation)]
        # The tag echoes back, proving the task answers this call and no other.
        tag = "tin-svc-" + hashlib.sha256(execution_key.encode()).hexdigest()[:40]
        client = KeywordData(
            self.settings.dataforseo_login.get_secret_value(),
            self.settings.dataforseo_password.get_secret_value(),
            transport=self.transport,
        )
        try:
            task = await client.task(
                spec.endpoint, {**request, "tag": tag}, scope_keys=("tag",), settle_errors=True
            )
        except DataForSEOTaskError as exc:
            raise _refusal(exc.status_code) from None
        return dataforseo_result(operation, request, task, max_response_bytes=max_response_bytes)


def _refusal(code: int) -> ServiceCallRefused:
    """DataForSEO status codes as Tin's own messages; never provider bodies."""
    if code == 40202:
        return IntegrationRateLimitedError(
            "DataForSEO rate-limited Tin's reads; try again later in a new step."
        )
    if 40100 <= code < 40400:
        return ServiceCallRefused(
            f"DataForSEO refused Tin's account (status {code}); this read is unavailable "
            "until an operator checks Tin's DataForSEO access and balance.",
            code="provider_refused",
        )
    if 40400 <= code < 50000:
        return ServiceCallRefused(
            f"DataForSEO rejected the request (status {code}); check the target, keywords, "
            "location_code and language_code.",
            code="invalid_request",
        )
    return ServiceCallRefused(
        f"DataForSEO could not complete the read (status {code}); try again in a new step.",
        code="provider_error",
    )
