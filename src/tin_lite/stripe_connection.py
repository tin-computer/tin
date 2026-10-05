"""Stripe through a founder's restricted key: validation, GET-only reads and projection.

The founder creates a restricted key (`rk_live_…`/`rk_test_…`) in their Stripe dashboard from a
link that pre-selects read permissions, and pastes it into Tin's own page. Tin validates it,
records which reads it allows, and stores it encrypted. Workflows never see the key: they call
the reviewed operations below through the service gateway and receive small projected records.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx

from tin_lite.connection_records import (
    ServiceArgumentError,
    boolean,
    bounded_metadata,
    email_domain,
    fit_records,
    integer,
    open_credential,
    read_bounded_json,
    record_call,
    seal_credential,
    text,
)
from tin_lite.integrations import (
    STRIPE_CAPABILITIES,
    STRIPE_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationInputError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
    ServiceResponseTooLarge,
)
from tin_lite.provider_errors import ProviderErrorDetail, explain
from tin_lite.provider_errors import detail as provider_error_detail

STRIPE_API = "https://api.stripe.com"
# Pinned so responses keep the shape the projections read; never the account's default version.
# Latest GA version on 2026-09-24 (docs.stripe.com/changelog). Bump deliberately, with fixtures.
STRIPE_VERSION = "2026-08-26.dahlia"
RESTRICTED_KEY = re.compile(r"rk_(test|live)_[A-Za-z0-9]{20,247}\Z")
UNRESTRICTED_KEY = re.compile(r"(sk|pk)_(test|live)_")
# Stripe's dashboard pre-selects these on its "Create restricted key" page. Products back the
# price reads; the rest match the capabilities. GET /v1/account needs a Connect permission on a
# restricted key, so Tin never asks for it and identifies the account without it.
DASHBOARD_PERMISSIONS = (
    "rak_customer_read",
    "rak_subscription_read",
    "rak_plan_read",
    "rak_product_read",
    "rak_invoice_read",
    "rak_charge_read",
)
# Each capability needs every listed resource readable: subscriptions expand their customer
# and prices expand their product, and Stripe checks permission for expanded objects too.
CAPABILITY_RESOURCES = {
    "subscriptions.read": ("/v1/subscriptions", "/v1/customers"),
    "customers.read": ("/v1/customers",),
    "invoices.read": ("/v1/invoices",),
    "prices.read": ("/v1/prices", "/v1/products"),
    "charges.read": ("/v1/charges",),
}
RESOURCE_NAMES = {
    "/v1/subscriptions": "Subscriptions",
    "/v1/customers": "Customers",
    "/v1/invoices": "Invoices",
    "/v1/prices": "Prices",
    "/v1/products": "Products",
    "/v1/charges": "Charges",
}
READ_PATHS = frozenset({"/v1/account", *RESOURCE_NAMES})
MAX_UPSTREAM_BYTES = 8_000_000
# An error body is read only for its type, code and message.
MAX_ERROR_BYTES = 64_000
RATE_REASONS = frozenset(
    {
        "global-rate",
        "endpoint-rate",
        "global-concurrency",
        "endpoint-concurrency",
        "resource-specific",
    }
)
# 2100-01-01T00:00:00Z; Stripe timestamps are Unix seconds.
MAX_TIMESTAMP = 4_102_444_800
STRIPE_ID = re.compile(r"[A-Za-z0-9_]{3,255}\Z")
# Stripe's permission errors name the account: "... for this endpoint on account 'acct_…'".
ACCOUNT_IN_MESSAGE = re.compile(r"\b(acct_[A-Za-z0-9]{6,64})\b")


def create_key_url(name: str = "Tin") -> str:
    """The dashboard page that creates a restricted key with Tin's read permissions selected."""
    params = [("name", name), *(("permissions[]", item) for item in DASHBOARD_PERMISSIONS)]
    return f"https://dashboard.stripe.com/apikeys/create?{urlencode(params)}"


# ---------------------------------------------------------------- operations and arguments


@dataclass(frozen=True)
class Operation:
    capability: str
    path: str
    arguments: frozenset[str]
    project: Callable[[dict[str, Any]], dict[str, Any]]
    expand: str | None = None


def _checked(arguments: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise ServiceArgumentError("arguments must be an object")
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ServiceArgumentError(f"unsupported argument {unknown[0]}")
    return arguments


def _timestamp(value: Any, field: str, *, end: bool) -> int:
    """Unix seconds, or a YYYY-MM-DD UTC day (start of day for *_gte, end of day for *_lte)."""
    if type(value) is int and 0 <= value <= MAX_TIMESTAMP:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            moment = datetime.combine(day, time.max if end else time.min, tzinfo=UTC)
            return int(moment.timestamp())
    raise ServiceArgumentError(f"{field} must be Unix seconds or a YYYY-MM-DD date")


def _choice(value: Any, field: str, options: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in options:
        raise ServiceArgumentError(f"{field} must be one of {', '.join(sorted(options))}")
    return value


def _stripe_id(value: Any, field: str, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not STRIPE_ID.fullmatch(value):
        raise ServiceArgumentError(f"{field} must be a Stripe object id")
    if prefix and not value.startswith(prefix):
        raise ServiceArgumentError(f"{field} must start with {prefix}")
    return value


SUBSCRIPTION_STATUSES = frozenset(
    {
        "all",
        "active",
        "past_due",
        "unpaid",
        "canceled",
        "incomplete",
        "incomplete_expired",
        "trialing",
        "paused",
        "ended",
    }
)
INVOICE_STATUSES = frozenset({"draft", "open", "paid", "uncollectible", "void"})


def request_for(operation: str, arguments: Any) -> tuple[Operation, dict[str, Any]]:
    """Validate one call's closed arguments and return Stripe's GET path and query params."""
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ServiceArgumentError("unknown Stripe operation")
    args = _checked(arguments, spec.arguments)
    limit = args.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ServiceArgumentError("limit must be an integer from 1 to 100")
    params: dict[str, Any] = {"limit": limit}
    if spec.expand:
        params["expand[]"] = spec.expand
    if "cursor" in args and args["cursor"] is not None:
        params["starting_after"] = _stripe_id(args["cursor"], "cursor")
    bounds = {}
    for field, end in (("created_gte", False), ("created_lte", True)):
        if args.get(field) is not None:
            bounds[field] = _timestamp(args[field], field, end=end)
    if len(bounds) == 2 and bounds["created_gte"] > bounds["created_lte"]:
        raise ServiceArgumentError("created_gte must not be after created_lte")
    if "created_gte" in bounds:
        params["created[gte]"] = bounds["created_gte"]
    if "created_lte" in bounds:
        params["created[lte]"] = bounds["created_lte"]
    if operation == "subscriptions.list":
        params["status"] = _choice(args.get("status", "all"), "status", SUBSCRIPTION_STATUSES)
    if operation == "invoices.list":
        if args.get("status") is not None:
            params["status"] = _choice(args["status"], "status", INVOICE_STATUSES)
        if args.get("customer") is not None:
            params["customer"] = _stripe_id(args["customer"], "customer", "cus_")
        if args.get("subscription") is not None:
            params["subscription"] = _stripe_id(args["subscription"], "subscription", "sub_")
    if operation == "customers.list" and args.get("email") is not None:
        email = args["email"]
        if (
            not isinstance(email, str)
            or not 3 <= len(email) <= 512
            or any(ord(c) < 33 or ord(c) == 127 for c in email)
        ):
            raise ServiceArgumentError("email must be one exact address, at most 512 characters")
        params["email"] = email
    if operation == "prices.list" and args.get("active") is not None:
        if type(args["active"]) is not bool:
            raise ServiceArgumentError("active must be true or false")
        params["active"] = "true" if args["active"] else "false"
    return spec, params


def check_arguments(operation: str, arguments: Any) -> None:
    """Gateway hook: reject a malformed call before any receipt or provider request exists."""
    request_for(operation, arguments)


# ---------------------------------------------------------------- projections


def _id(value: Any) -> str | None:
    if isinstance(value, str):
        return text(value, 255)
    if isinstance(value, dict):
        return text(value.get("id"), 255)
    return None


def _customer(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"id": text(value, 255)}
    if not isinstance(value, dict):
        return None
    if value.get("deleted") is True:
        return {"id": text(value.get("id"), 255), "deleted": True}
    email = text(value.get("email"), 320)
    address = value.get("address") if isinstance(value.get("address"), dict) else {}
    return {
        "id": text(value.get("id"), 255),
        "email": email,
        "name": text(value.get("name")),
        "email_domain": email_domain(email),
        "country": text(address.get("country"), 2),
        "created": integer(value.get("created")),
        "metadata": bounded_metadata(value.get("metadata")),
    }


def project_customer(value: dict[str, Any]) -> dict[str, Any]:
    customer = _customer(value) or {}
    if customer.get("deleted"):
        return customer
    return {
        **customer,
        "currency": text(value.get("currency"), 3),
        "delinquent": boolean(value.get("delinquent")),
    }


def _item(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    recurring = price.get("recurring") if isinstance(price.get("recurring"), dict) else {}
    return {
        "price_id": text(price.get("id"), 255),
        "product_id": _id(price.get("product")),
        "unit_amount": integer(price.get("unit_amount")),
        "currency": text(price.get("currency"), 3),
        "interval": text(recurring.get("interval"), 10),
        "interval_count": integer(recurring.get("interval_count")),
        "quantity": integer(item.get("quantity")),
    }


def _discounts(value: dict[str, Any]) -> tuple[list[str], str | None]:
    ids, coupon = [], None
    entries = value.get("discounts") if isinstance(value.get("discounts"), list) else []
    legacy = value.get("discount")
    for entry in [*entries, *([legacy] if isinstance(legacy, dict) else [])][:5]:
        if isinstance(entry, str):
            ids.append(text(entry, 255))
        elif isinstance(entry, dict):
            if isinstance(entry.get("id"), str) and entry["id"] not in ids:
                ids.append(text(entry["id"], 255))
            source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
            coupon = coupon or _id(entry.get("coupon")) or _id(source.get("coupon"))
    return [item for item in ids if item], coupon


def project_subscription(value: dict[str, Any]) -> dict[str, Any]:
    items = value.get("items") if isinstance(value.get("items"), dict) else {}
    rows = items.get("data") if isinstance(items.get("data"), list) else []
    # Since 2025-03-31 (basil) the billing period lives on each item.
    period_ends = [
        row["current_period_end"]
        for row in rows
        if isinstance(row, dict) and type(row.get("current_period_end")) is int
    ]
    details = value.get("cancellation_details")
    details = details if isinstance(details, dict) else {}
    discount_ids, coupon = _discounts(value)
    return {
        "id": text(value.get("id"), 255),
        "status": text(value.get("status"), 40),
        "created": integer(value.get("created")),
        "start_date": integer(value.get("start_date")),
        "current_period_end": integer(value.get("current_period_end"))
        or (max(period_ends) if period_ends else None),
        "cancel_at_period_end": boolean(value.get("cancel_at_period_end")),
        "canceled_at": integer(value.get("canceled_at")),
        "ended_at": integer(value.get("ended_at")),
        "trial_start": integer(value.get("trial_start")),
        "trial_end": integer(value.get("trial_end")),
        "cancellation_details": {
            "reason": text(details.get("reason"), 60),
            "feedback": text(details.get("feedback"), 60),
        },
        "livemode": boolean(value.get("livemode")),
        "metadata": bounded_metadata(value.get("metadata")),
        "discount_ids": discount_ids,
        "coupon_id": coupon,
        "items": [_item(row) for row in rows[:20]],
        "customer": _customer(value.get("customer")),
    }


def project_invoice(value: dict[str, Any]) -> dict[str, Any]:
    parent = value.get("parent") if isinstance(value.get("parent"), dict) else {}
    details = parent.get("subscription_details")
    details = details if isinstance(details, dict) else {}
    return {
        "id": text(value.get("id"), 255),
        "customer": _id(value.get("customer")),
        "subscription": _id(details.get("subscription")) or _id(value.get("subscription")),
        "status": text(value.get("status"), 40),
        "billing_reason": text(value.get("billing_reason"), 60),
        "amount_due": integer(value.get("amount_due")),
        "amount_paid": integer(value.get("amount_paid")),
        "currency": text(value.get("currency"), 3),
        "created": integer(value.get("created")),
        "period_start": integer(value.get("period_start")),
        "period_end": integer(value.get("period_end")),
        "attempt_count": integer(value.get("attempt_count")),
    }


def project_price(value: dict[str, Any]) -> dict[str, Any]:
    product = value.get("product")
    recurring = value.get("recurring")
    return {
        "id": text(value.get("id"), 255),
        "product": {
            "id": _id(product),
            "name": text(product.get("name")) if isinstance(product, dict) else None,
        },
        "unit_amount": integer(value.get("unit_amount")),
        "currency": text(value.get("currency"), 3),
        "type": text(value.get("type"), 20),
        "recurring": {
            "interval": text(recurring.get("interval"), 10),
            "interval_count": integer(recurring.get("interval_count")),
        }
        if isinstance(recurring, dict)
        else None,
        "active": boolean(value.get("active")),
        "nickname": text(value.get("nickname")),
    }


def project_charge(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": text(value.get("id"), 255),
        "customer": _id(value.get("customer")),
        "amount": integer(value.get("amount")),
        "amount_refunded": integer(value.get("amount_refunded")),
        "currency": text(value.get("currency"), 3),
        "status": text(value.get("status"), 40),
        "paid": boolean(value.get("paid")),
        "refunded": boolean(value.get("refunded")),
        "created": integer(value.get("created")),
        "failure_code": text(value.get("failure_code"), 80),
    }


_WINDOW = frozenset({"created_gte", "created_lte", "cursor", "limit"})
# The reviewed operation table; the gateway maps (provider, operation) through it explicitly.
OPERATIONS: dict[str, Operation] = {
    "subscriptions.list": Operation(
        "subscriptions.read",
        "/v1/subscriptions",
        _WINDOW | {"status"},
        project_subscription,
        expand="data.customer",
    ),
    "customers.list": Operation(
        "customers.read", "/v1/customers", _WINDOW | {"email"}, project_customer
    ),
    "invoices.list": Operation(
        "invoices.read",
        "/v1/invoices",
        _WINDOW | {"status", "customer", "subscription"},
        project_invoice,
    ),
    "prices.list": Operation(
        "prices.read",
        "/v1/prices",
        frozenset({"active", "cursor", "limit"}),
        project_price,
        expand="data.product",
    ),
    "charges.list": Operation("charges.read", "/v1/charges", _WINDOW, project_charge),
}
assert {op.capability for op in OPERATIONS.values()} == set(STRIPE_CAPABILITIES)


def project_page(
    operation: str,
    payload: Any,
    *,
    max_response_bytes: int | None,
    livemode: bool | None,
) -> dict[str, Any]:
    """Project one Stripe list page and fit its leading records to the byte bound."""
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "list"
        or not isinstance(payload.get("data"), list)
        or type(payload.get("has_more")) is not bool
        or any(not isinstance(item, dict) for item in payload["data"])
    ):
        raise IntegrationUpstreamError("Stripe returned an unexpected list")
    project = OPERATIONS[operation].project
    records = [project(item) for item in payload["data"]]
    return fit_records(
        records,
        max_response_bytes=max_response_bytes,
        has_more=payload["has_more"],
        envelope={"livemode": livemode},
    )


# ---------------------------------------------------------------- GET-only client


class StripeWriteRefused(RuntimeError):
    """A non-GET request was attempted; the Stripe client is read-only by construction."""


class StripeAuthenticationFailed(ServiceCallRefused):
    def __init__(self, provider_error: ProviderErrorDetail | None = None) -> None:
        super().__init__(
            "Stripe rejected the stored restricted key. Enter a new key in Integrations.",
            code="authentication_failed",
            provider_error=provider_error,
        )


class StripePermissionDenied(ServiceCallRefused):
    def __init__(self, path: str, provider_error: ProviderErrorDetail | None = None) -> None:
        resource = RESOURCE_NAMES.get(path, "that resource")
        super().__init__(
            f"Stripe's restricted key cannot read {resource}. Give the key Read access to "
            f"{resource} in Stripe, then press Check again in Integrations.",
            code="permission_denied",
            provider_error=provider_error,
        )
        self.path = path


class StripeReader:
    """GET-only reads against the fixed Stripe API host with a pinned API version."""

    def __init__(self, client: httpx.AsyncClient, key: str) -> None:
        self._client, self._key = client, key

    async def request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        if method != "GET":
            raise StripeWriteRefused(f"{method} is never sent to Stripe")
        if path not in READ_PATHS:
            raise StripeWriteRefused("Stripe path is not a reviewed read")
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Stripe-Version": STRIPE_VERSION,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "Tin-Stripe/1",
        }
        async with self._client.stream(
            "GET",
            STRIPE_API + path,
            params=params or {},
            headers=headers,
            follow_redirects=False,
            timeout=20.0,
        ) as response:
            request_id = response.headers.get("request-id")
            status = response.status_code
            if status != 200:
                try:
                    error = await read_bounded_json(
                        response, maximum=MAX_ERROR_BYTES, provider="Stripe"
                    )
                except IntegrationError:
                    error = None
                detail = provider_error_detail("Stripe", status, error, secrets=(self._key,))
            if status == 401:
                raise StripeAuthenticationFailed(detail)
            if status == 403:
                raise StripePermissionDenied(path, detail)
            if status == 429:
                reason = response.headers.get("stripe-rate-limited-reason")
                reason = reason if reason in RATE_REASONS else None
                raise IntegrationRateLimitedError(
                    "Stripe rate-limited this read"
                    + (f" ({reason})" if reason else "")
                    + "; try again later in a new step.",
                    reason=reason,
                    provider_error=detail,
                )
            if status != 200:
                raise ServiceCallRefused(
                    f"Stripe could not complete the read (HTTP {status}).",
                    code="provider_error",
                    provider_error=detail,
                )
            payload = await read_bounded_json(
                response, maximum=MAX_UPSTREAM_BYTES, provider="Stripe"
            )
        if not isinstance(payload, dict):
            raise IntegrationUpstreamError("Stripe returned an invalid response")
        return payload, request_id

    async def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        return await self.request("GET", path, params)


# ---------------------------------------------------------------- connection lifecycle


def _display_name(account: dict[str, Any]) -> str | None:
    settings = account.get("settings") if isinstance(account.get("settings"), dict) else {}
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    profile = account.get("business_profile")
    profile = profile if isinstance(profile, dict) else {}
    for value in (dashboard.get("display_name"), profile.get("name"), account.get("email")):
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


def _account_from_refusal(error: StripePermissionDenied, key: str) -> str:
    """The account a permission error names, or a stable stand-in derived from the key."""
    message = error.provider_error.message if error.provider_error else None
    found = ACCOUNT_IN_MESSAGE.search(message or "")
    if found:
        return found.group(1)
    return "stripe_key_" + hashlib.sha256(key.encode()).hexdigest()[:24]


async def inspect_key(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    """Identify the account and probe each read; raise a founder-facing error otherwise.

    Reading /v1/account needs a Connect permission on a restricted key, which Tin's link does not
    ask for. Without it the account id comes from Stripe's refusal, and the account has no name.
    """
    reader = StripeReader(client, key)
    account: dict[str, Any] = {}
    try:
        account, _ = await reader.get("/v1/account")
    except StripeAuthenticationFailed:
        raise IntegrationInputError(
            "Stripe rejected this key. Copy the whole rk_… value, and check the key was not "
            "deleted or expired in Stripe."
        ) from None
    except StripePermissionDenied as error:
        account = {"id": _account_from_refusal(error, key)}
    except IntegrationRateLimitedError:
        raise IntegrationUpstreamError(
            "Stripe is rate-limiting requests right now; try again in a minute."
        ) from None
    except ServiceCallRefused as error:
        raise IntegrationUpstreamError(
            explain("Stripe could not check the key; try again.", error.provider_error)
        ) from None
    except httpx.HTTPError:
        raise IntegrationUpstreamError("Stripe could not be reached; try again.") from None
    account_id = account.get("id")
    if not isinstance(account_id, str) or not account_id.startswith(("acct_", "stripe_key_")):
        raise IntegrationUpstreamError("Stripe did not identify the account for this key")
    readable: dict[str, bool] = {}
    for path in RESOURCE_NAMES:
        try:
            await reader.get(path, {"limit": 1})
            readable[path] = True
        except StripePermissionDenied:
            readable[path] = False
        except StripeAuthenticationFailed:
            raise IntegrationInputError("Stripe rejected this key while checking it.") from None
        except ServiceCallRefused as error:
            raise IntegrationUpstreamError(
                explain(
                    "Stripe could not finish checking the key; try again in a minute.",
                    error.provider_error,
                )
            ) from None
        except httpx.HTTPError:
            raise IntegrationUpstreamError(
                "Stripe could not finish checking the key; try again in a minute."
            ) from None
    granted = [
        capability
        for capability in STRIPE_CAPABILITIES
        if all(readable[path] for path in CAPABILITY_RESOURCES[capability])
    ]
    if not granted:
        raise IntegrationInputError(
            "This key cannot read subscriptions, customers, invoices, prices or charges. "
            "Create it from Tin's link so the read permissions are selected."
        )
    return {
        "account_id": account_id,
        "account_name": _display_name(account),
        "granted_capabilities": granted,
        "missing_permissions": [RESOURCE_NAMES[path] for path, ok in readable.items() if not ok],
    }


def _label(configuration: dict[str, Any]) -> str:
    account_id = configuration.get("account_id")
    if isinstance(account_id, str) and account_id.startswith("stripe_key_"):
        account_id = None
    name = configuration.get("account_name") or account_id or "Stripe"
    return name if configuration.get("livemode") else f"{name} (test mode)"


class StripeConnections:
    def __init__(self, integrations: Any) -> None:
        self.integrations = integrations
        self.db = integrations._database

    async def connect(
        self,
        *,
        project_id: UUID,
        clerk_user_id: str,
        restricted_key: str,
        expected_revision: str | None,
    ):
        """Validate a pasted restricted key, then store it encrypted; replacement uses CAS."""
        self.integrations._require_configured(STRIPE_PROVIDER)
        key = restricted_key.strip() if isinstance(restricted_key, str) else ""
        if UNRESTRICTED_KEY.match(key):
            raise IntegrationInputError(
                "That is a secret or publishable key. Tin accepts only a restricted key "
                "(rk_live_… or rk_test_…) with read permissions; create one from Tin's link."
            )
        match = RESTRICTED_KEY.fullmatch(key)
        if match is None:
            raise IntegrationInputError(
                "Paste a Stripe restricted key: it starts with rk_live_ or rk_test_."
            )
        inspected = await inspect_key(self.integrations._client, key)
        ciphertext, version = seal_credential(
            self.integrations, project_id=project_id, provider_key=STRIPE_PROVIDER, value=key
        )
        configuration = {
            "revision": str(uuid4()),
            "livemode": match.group(1) == "live",
            **inspected,
        }
        async with self.db.integration_refresh_lock(
            project_id=project_id, provider_key=STRIPE_PROVIDER
        ):
            current = await self.db.get_integration_connection(
                project_id=project_id, provider_key=STRIPE_PROVIDER
            )
            if (current.configuration.get("revision") if current else None) != expected_revision:
                raise IntegrationAuthorizationError(
                    "The Stripe connection changed; refresh before replacing its key."
                )
            connection = await self.db.upsert_integration_connection(
                project_id=project_id,
                provider_key=STRIPE_PROVIDER,
                external_account_id=inspected["account_id"],
                external_account_label=_label(configuration),
                configuration=configuration,
                credential_ciphertext=ciphertext,
                credential_key_version=version,
                connected_by_clerk_user_id=clerk_user_id,
            )
        await self.integrations._record_activity(
            connection,
            "integration_connected",
            f"Stripe connected: {_label(configuration)}, read only.",
            suffix=configuration["revision"],
        )
        return connection

    async def refresh(self, *, project_id: UUID):
        """Re-check the stored key's permissions, for example after editing it in Stripe."""
        connection = await self.integrations._connection(project_id, STRIPE_PROVIDER)
        key = open_credential(self.integrations, connection)
        try:
            inspected = await inspect_key(self.integrations._client, key)
        except IntegrationInputError:
            await self.db.mark_integration_attention(
                project_id=project_id,
                provider_key=STRIPE_PROVIDER,
                error_code="authentication_failed",
            )
            raise
        async with self.db.integration_refresh_lock(
            project_id=project_id, provider_key=STRIPE_PROVIDER
        ):
            current = await self.integrations._connection(project_id, STRIPE_PROVIDER)
            if current.configuration.get("revision") != connection.configuration.get("revision"):
                raise IntegrationAuthorizationError(
                    "The Stripe key was replaced while it was checked; refresh."
                )
            if inspected["account_id"] != current.external_account_id:
                raise IntegrationAuthorizationError("The stored key now names another account.")
            configuration = {**current.configuration, **inspected}
            return await self.db.update_integration_configuration(
                project_id=project_id,
                provider_key=STRIPE_PROVIDER,
                configuration=configuration,
                external_account_label=_label(configuration),
            )

    async def call(
        self,
        operation: str,
        arguments: Any,
        *,
        connection: Any,
        run_id: UUID | None,
        execution_key: str,
        max_response_bytes: int | None,
    ) -> dict[str, Any]:
        spec, params = request_for(operation, arguments)
        granted = connection.configuration.get("granted_capabilities") or []
        if spec.capability not in granted:
            raise IntegrationAuthorizationError("Stripe's key does not allow this read")
        key = open_credential(self.integrations, connection)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"account": connection.external_account_id, "path": spec.path, "params": params},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt = {
            "connection": connection,
            "capability": spec.capability,
            "fingerprint": fingerprint,
            "execution_key": execution_key,
            "run_id": run_id,
        }
        try:
            payload, request_id = await StripeReader(self.integrations._client, key).get(
                spec.path, params
            )
        except IntegrationError as exc:
            if isinstance(exc, StripeAuthenticationFailed):
                await self.db.mark_integration_attention(
                    project_id=connection.project_id,
                    provider_key=STRIPE_PROVIDER,
                    error_code="authentication_failed",
                )
            await record_call(
                self.db,
                **receipt,
                status="failed",
                error_code=getattr(exc, "code", "provider_request_failed"),
            )
            raise
        summary: dict[str, Any] = {"record_count": len(payload.get("data") or [])}
        try:
            result = project_page(
                operation,
                payload,
                max_response_bytes=max_response_bytes,
                livemode=connection.configuration.get("livemode"),
            )
        except ServiceResponseTooLarge:
            # Stripe answered; the outcome is known even though no record fits the bound.
            await record_call(
                self.db,
                **receipt,
                status="completed",
                response_summary={**summary, "returned_count": 0, "error": "record_too_large"},
                provider_request_id=request_id,
            )
            raise
        summary.update(
            returned_count=len(result["records"]),
            has_more=result["has_more"],
            truncated=result["truncated"],
        )
        await record_call(
            self.db,
            **receipt,
            status="completed",
            response_summary=summary,
            provider_request_id=request_id,
        )
        return result
