"""Offline stand-ins for first-party connection bindings, for workflow package tests.

A package that binds `payments.stripe` calls `await ctx.services.call(service=..., step=...,
operation=..., arguments={...})`. `FakeStripeConnection` answers those calls from synthetic
Stripe objects the way Tin's gateway does: it checks the closed arguments with Tin's own
validator, applies Stripe's list semantics (newest first, created window, filters, cursor,
limit), expands the customer or product like Tin's upstream request, and returns Tin's real
projection fitted to the binding's `max_response_bytes`. Errors are `ValueError`s carrying the
gateway's messages, which is what package code sees in the sandbox.

    from connection_fakes import FakeStripeConnection, stripe_objects

    class Context(dict):
        def __init__(self, services):
            super().__init__(run_id="00000000-0000-4000-8000-000000000099", created_at=NOW)
            self.services = services

    stripe = FakeStripeConnection(stripe_objects(), service="stripe", max_response_bytes=64000)
    result = await MODULE.run(Context(stripe), inputs)
    assert [call["operation"] for call in stripe.calls] == ["subscriptions.list"]

`stripe_objects()` loads `tests/fixtures/connections/stripe_objects.json`; pass your own dict
with the same keys (account, customers, subscriptions, invoices, prices, products, charges) to
model a specific business. Use `refuse={"step": "rate_limited"}` to rehearse a refusal.

`FakePostHogConnection` does the same for an `analytics.posthog` binding. List operations page
`posthog_objects()` with PostHog's search, limit and position cursor; `query.hogql` first runs
Tin's HogQL guard, then answers from canned results keyed by the call's `name` (or its exact
query text) and applies the query's own LIMIT, since the fake cannot execute SQL:

    posthog = FakePostHogConnection(posthog_objects(), service="posthog")
    result = await posthog.call(service="posthog", step="signups", operation="query.hogql",
        arguments={"name": "signups_by_day", "query": "SELECT ... LIMIT 30"})
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from tin_lite import posthog_connection
from tin_lite.code_services import MESSAGE_LIMIT, CodeServiceError
from tin_lite.connection_records import ServiceArgumentError as ArgumentError
from tin_lite.integrations import (
    POSTHOG_CAPABILITIES,
    STRIPE_CAPABILITIES,
    ServiceCallRefused,
    ServiceResponseTooLarge,
)
from tin_lite.provider_errors import ProviderErrorDetail, explain
from tin_lite.stripe_connection import (
    OPERATIONS,
    RESOURCE_NAMES,
    ServiceArgumentError,
    project_page,
    request_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "connections"
STEP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
REFUSALS = {
    "rate_limited": "Stripe rate-limited this read (endpoint-rate); try again later in a new step.",
    "authentication_failed": (
        "Stripe rejected the stored restricted key. Enter a new key in Integrations."
    ),
}


def stripe_objects() -> dict[str, Any]:
    return json.loads((FIXTURES / "stripe_objects.json").read_text())


class FakeStripeConnection:
    """One `payments.stripe` service binding with the gateway's limits, served offline."""

    def __init__(
        self,
        objects: dict[str, Any],
        *,
        service: str = "stripe",
        max_calls: int = 8,
        max_response_bytes: int = 64_000,
        capabilities: tuple[str, ...] = STRIPE_CAPABILITIES,
        livemode: bool = False,
        refuse: dict[str, str] | None = None,
    ) -> None:
        self.objects = copy.deepcopy(objects)
        self.service, self.max_calls = service, max_calls
        self.max_response_bytes = max_response_bytes
        self.capabilities, self.livemode = set(capabilities), livemode
        self.refuse = dict(refuse or {})
        self.calls: list[dict[str, Any]] = []

    async def request(self, **_kwargs):
        raise CodeServiceError("The service request differs from its declared contract.")

    async def call(self, *, service, step, operation, arguments=None):
        arguments = arguments or {}
        if service != self.service or not isinstance(step, str) or not STEP.fullmatch(step):
            raise CodeServiceError("The service request differs from its declared contract.")
        prior = next((c for c in self.calls if c["step"] == step), None)
        if prior is not None:
            if (prior["operation"], prior["arguments"]) != (operation, arguments):
                raise CodeServiceError("This service step already has a different request.")
            if "error" in prior:
                raise CodeServiceError(prior["error"])
            return copy.deepcopy(prior["response"])
        if len(self.calls) >= self.max_calls:
            raise CodeServiceError("The workflow reached its declared service-call limit.")
        try:
            spec, params = request_for(operation, arguments)
        except ServiceArgumentError as exc:
            raise CodeServiceError(
                f"The service request differs from its declared contract: {exc}."
            ) from None
        if spec.capability not in self.capabilities:
            raise CodeServiceError("The service request differs from its declared contract.")
        record = {"step": step, "operation": operation, "arguments": copy.deepcopy(arguments)}
        self.calls.append(record)
        if step in self.refuse:
            message = REFUSALS.get(self.refuse[step]) or (
                f"Stripe's restricted key cannot read {RESOURCE_NAMES[spec.path]}. Give the "
                f"key Read access to {RESOURCE_NAMES[spec.path]} in Stripe, then press Check "
                "again in Integrations."
            )
            record["error"] = message
            raise CodeServiceError(message)
        try:
            response = project_page(
                operation,
                self.page(spec.path, params),
                max_response_bytes=self.max_response_bytes,
                livemode=self.livemode,
            )
        except ServiceResponseTooLarge:
            raise CodeServiceError(
                "The service response exceeded this binding's max_response_bytes "
                f"({self.max_response_bytes}); request less data, for example a smaller "
                "row_limit or the next start_row page."
            ) from None
        # The gateway serializes the response; hand back plain JSON like the sandbox sees.
        record["response"] = json.loads(json.dumps(response))
        return copy.deepcopy(record["response"])

    def page(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Stripe's list semantics over the fixture objects: newest first, then filters."""
        resource = path.removeprefix("/v1/")
        rows = sorted(
            self.objects.get(resource, []), key=lambda item: (-item["created"], item["id"])
        )
        low, high = params.get("created[gte]"), params.get("created[lte]")
        rows = [
            row
            for row in rows
            if (low is None or row["created"] >= low) and (high is None or row["created"] <= high)
        ]
        status = params.get("status")
        if resource == "subscriptions":
            if status in (None, "all"):
                rows = rows if status == "all" else [r for r in rows if r["status"] != "canceled"]
            elif status == "ended":
                rows = [r for r in rows if r["status"] in {"canceled", "incomplete_expired"}]
            else:
                rows = [r for r in rows if r["status"] == status]
        for field in ("customer", "email"):
            if field in params:
                rows = [r for r in rows if r.get(field) == params[field]]
        if resource == "invoices":
            if status is not None:
                rows = [r for r in rows if r["status"] == status]
            if "subscription" in params:
                rows = [r for r in rows if _invoice_subscription(r) == params["subscription"]]
        if "active" in params:
            rows = [r for r in rows if r.get("active") is (params["active"] == "true")]
        if "starting_after" in params:
            ids = [row["id"] for row in rows]
            if params["starting_after"] not in ids:
                raise CodeServiceError(
                    "Service response unavailable or invalid; "
                    "the request will not be repeated automatically."
                )
            rows = rows[ids.index(params["starting_after"]) + 1 :]
        page = copy.deepcopy(rows[: params["limit"]])
        expand = params.get("expand[]")
        for row in page:
            if expand == "data.customer":
                row["customer"] = self._find("customers", row.get("customer")) or row["customer"]
            if expand == "data.product":
                row["product"] = self._find("products", row.get("product")) or row["product"]
        return {"object": "list", "data": page, "has_more": len(rows) > len(page), "url": path}

    def _find(self, resource: str, object_id: Any) -> dict[str, Any] | None:
        return next(
            (copy.deepcopy(o) for o in self.objects.get(resource, []) if o["id"] == object_id),
            None,
        )


def _invoice_subscription(invoice: dict[str, Any]) -> str | None:
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    return details.get("subscription") or invoice.get("subscription")


assert set(OPERATIONS) == {
    "subscriptions.list",
    "customers.list",
    "invoices.list",
    "prices.list",
    "charges.list",
}


def posthog_objects() -> dict[str, Any]:
    return json.loads((FIXTURES / "posthog_objects.json").read_text())


POSTHOG_REFUSALS = {
    "rate_limited": lambda: posthog_connection.rate_limited(budget=False, retry_after=30),
    "budget_exceeded": lambda: posthog_connection.rate_limited(budget=True, retry_after=600),
    "reauthorization_required": posthog_connection.PostHogUnauthorized,
    "permission_denied": posthog_connection.PostHogPermissionDenied,
    "query_error": lambda: posthog_connection.query_rejected(
        ProviderErrorDetail(
            "PostHog",
            400,
            "validation_error",
            "invalid_input",
            "Unable to resolve field: missing_column",
        )
    ),
}


class FakePostHogConnection:
    """One `analytics.posthog` binding with the gateway's limits, served offline."""

    def __init__(
        self,
        objects: dict[str, Any],
        *,
        service: str = "posthog",
        max_calls: int = 8,
        max_response_bytes: int = 64_000,
        capabilities: tuple[str, ...] = POSTHOG_CAPABILITIES,
        queries: dict[str, dict[str, Any]] | None = None,
        refuse: dict[str, str] | None = None,
    ) -> None:
        self.objects = copy.deepcopy(objects)
        self.queries = copy.deepcopy(queries if queries is not None else objects.get("queries", {}))
        self.service, self.max_calls = service, max_calls
        self.max_response_bytes = max_response_bytes
        self.capabilities = set(capabilities)
        self.refuse = dict(refuse or {})
        self.calls: list[dict[str, Any]] = []

    async def request(self, **_kwargs):
        raise CodeServiceError("The service request differs from its declared contract.")

    async def call(self, *, service, step, operation, arguments=None):
        arguments = arguments or {}
        if service != self.service or not isinstance(step, str) or not STEP.fullmatch(step):
            raise CodeServiceError("The service request differs from its declared contract.")
        prior = next((c for c in self.calls if c["step"] == step), None)
        if prior is not None:
            if (prior["operation"], prior["arguments"]) != (operation, arguments):
                raise CodeServiceError("This service step already has a different request.")
            if "error" in prior:
                raise CodeServiceError(prior["error"])
            return copy.deepcopy(prior["response"])
        if len(self.calls) >= self.max_calls:
            raise CodeServiceError("The workflow reached its declared service-call limit.")
        try:
            spec, request = posthog_connection.request_for(operation, arguments)
        except ArgumentError as exc:
            raise CodeServiceError(
                f"The service request differs from its declared contract: {exc}."
            ) from None
        if spec.capability not in self.capabilities:
            raise CodeServiceError("The service request differs from its declared contract.")
        record = {"step": step, "operation": operation, "arguments": copy.deepcopy(arguments)}
        self.calls.append(record)
        try:
            if step in self.refuse:
                raise POSTHOG_REFUSALS[self.refuse[step]]()
            if operation == "query.hogql":
                response = posthog_connection.project_query(
                    self.query(request["body"], arguments.get("name")),
                    max_response_bytes=self.max_response_bytes,
                )
            else:
                params = request["params"]
                response = posthog_connection.project_list(
                    operation,
                    self.page(spec.resource, params),
                    offset=params["offset"],
                    max_response_bytes=self.max_response_bytes,
                )
        except ServiceCallRefused as exc:
            # The gateway's composition: Tin's sentence, then what PostHog said.
            record["error"] = explain(str(exc), exc.provider_error)[:MESSAGE_LIMIT]
            detail = exc.provider_error.to_dict() if exc.provider_error else None
            raise CodeServiceError(record["error"], code=exc.code, provider_error=detail) from None
        except ServiceResponseTooLarge:
            raise CodeServiceError(
                "The service response exceeded this binding's max_response_bytes "
                f"({self.max_response_bytes}); request less data, for example a smaller "
                "row_limit or the next start_row page."
            ) from None
        record["response"] = json.loads(json.dumps(response))
        return copy.deepcopy(record["response"])

    def page(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        """PostHog's list semantics over the fixture objects: filters, then limit/offset."""
        rows = self.objects.get(resource, [])
        search = str(params.get("search") or "").lower()
        if search:
            rows = [
                r for r in rows if search in str(r.get("name") or r.get("derived_name")).lower()
            ]
        if "event_names" in params:
            wanted = set(json.loads(params["event_names"]))
            rows = [r for r in rows if wanted & set(r.get("_fixture_events", []))]
        start, limit = params["offset"], params["limit"]
        page = copy.deepcopy(rows[start : start + limit])
        more = start + limit < len(rows)
        return {
            "count": len(rows),
            "next": f"https://us.posthog.com/next?offset={start + limit}" if more else None,
            "previous": None,
            "results": page,
        }

    def query(self, body: dict[str, Any], name: str | None) -> dict[str, Any]:
        text = body["query"]["query"]
        canned = self.queries.get(name) if name else None
        canned = canned or self.queries.get(text)
        if canned is None:
            raise LookupError(
                f"FakePostHogConnection has no canned result for name={name!r}; pass queries="
            )
        limit = int(re.search(r"LIMIT\s+(\d+)\s*\Z", text, re.IGNORECASE).group(1))
        result = copy.deepcopy(canned)
        rows = result.get("results", [])
        result["results"] = rows[:limit]
        result["hasMore"] = bool(result.get("hasMore")) or len(rows) > limit
        return result
