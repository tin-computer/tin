"""payments.stripe: key validation, GET-only client, projection and fit, all offline."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from connection_fakes import FIXTURES, FakeStripeConnection, stripe_objects
from test_billing import billed as billed
from test_integrations import PROJECT_ID, RUN_ID, USER_ID, FakeIntegrationDatabase, settings
from test_procedure_publication import publication_db as publication_db

from tin_lite.code_services import OPERATIONS as GATEWAY_OPERATIONS
from tin_lite.code_services import CodeServiceError
from tin_lite.connection_records import fit_records, json_size
from tin_lite.integrations import (
    STRIPE_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationInputError,
    IntegrationRateLimitedError,
    IntegrationRequirement,
    IntegrationService,
    IntegrationUpstreamError,
    ServiceResponseTooLarge,
    parse_integration_requirements,
    registered_integrations,
)
from tin_lite.stripe_connection import (
    DASHBOARD_PERMISSIONS,
    OPERATIONS,
    STRIPE_VERSION,
    ServiceArgumentError,
    StripeAuthenticationFailed,
    StripePermissionDenied,
    StripeReader,
    StripeWriteRefused,
    create_key_url,
    project_page,
    request_for,
)
from tin_lite.workflow_services import service_bindings

LIVE_KEY = "rk_live_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
TEST_KEY = "rk_test_" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
OBJECTS = stripe_objects()
RESOURCE_PATHS = [
    f"/v1/{name}"
    for name in ("subscriptions", "customers", "invoices", "prices", "products", "charges")
]


def stripe_api(
    status: dict[str, int] | None = None,
    headers: dict[str, dict] | None = None,
    messages: dict[str, str] | None = None,
):
    """A Stripe stand-in: fixture lists per path, with per-path status and message overrides."""
    seen: list[httpx.Request] = []

    def respond(code, body, extra):
        raw = json.dumps(body).encode()
        return httpx.Response(code, stream=httpx.ByteStream(raw), headers=extra)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        code = (status or {}).get(path, 200)
        extra = (headers or {}).get(path, {})
        if code != 200:
            message = (messages or {}).get(path, f"key {LIVE_KEY}")
            body = {"error": {"type": "invalid_request_error", "message": message}}
            return respond(code, body, extra)
        if path == "/v1/account":
            return respond(200, OBJECTS["account"], {"request-id": "req_1"})
        fake = FakeStripeConnection(OBJECTS)
        params = dict(request.url.params)
        params["limit"] = int(params.get("limit", 10))
        for key in ("created[gte]", "created[lte]"):
            if key in params:
                params[key] = int(params[key])
        return respond(200, fake.page(path, params), {"request-id": "req_page", **extra})

    return handler, seen


async def service_for(handler, database=None):
    database = database or FakeIntegrationDatabase()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = IntegrationService(database=database, settings=settings(), client=client)
    return service, database, client


def test_registry_and_contracts_are_read_only_and_explicit() -> None:
    definition = next(d for d in registered_integrations() if d.key == STRIPE_PROVIDER)
    assert definition.capabilities == (
        "subscriptions.read",
        "customers.read",
        "invoices.read",
        "prices.read",
        "charges.read",
    )
    assert definition.setup_url == create_key_url()
    assert definition.setup_url.startswith("https://dashboard.stripe.com/apikeys/create?name=Tin")
    assert all(
        f"permissions%5B%5D={item}" in definition.setup_url for item in DASHBOARD_PERMISSIONS
    )
    assert all(item.endswith("_read") for item in DASHBOARD_PERMISSIONS)
    for name, op in OPERATIONS.items():
        assert GATEWAY_OPERATIONS[(STRIPE_PROVIDER, name)] == (op.capability, op.arguments)
    requirement = [
        {"provider_key": STRIPE_PROVIDER, "capabilities": ["invoices.read"], "required": True}
    ]
    assert parse_integration_requirements(requirement)[0].capabilities == ("invoices.read",)
    bindings = service_bindings(
        {"stripe": {"provider_key": STRIPE_PROVIDER, "max_calls": 8, "max_response_bytes": 64000}},
        requirement,
    )
    assert bindings[0].capabilities == ("invoices.read",)
    with pytest.raises(ValueError, match="unsupported capability"):
        parse_integration_requirements(
            [{"provider_key": STRIPE_PROVIDER, "capabilities": ["refunds.write"], "required": True}]
        )


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("sk_live_" + "x" * 24, "secret or publishable"),
        ("pk_test_" + "x" * 24, "secret or publishable"),
        ("rk_live_short", "restricted key"),
        ("rk_prod_" + "x" * 24, "restricted key"),
        ("", "restricted key"),
    ],
)
async def test_only_restricted_keys_are_accepted_before_any_request(key, message) -> None:
    handler, seen = stripe_api()
    service, database, client = await service_for(handler)
    async with client:
        with pytest.raises(IntegrationInputError, match=message):
            await service.stripe.connect(
                project_id=PROJECT_ID,
                clerk_user_id=USER_ID,
                restricted_key=key,
                expected_revision=None,
            )
    assert seen == [] and database.connections == {}


async def test_connect_validates_encrypts_and_labels_test_mode() -> None:
    handler, seen = stripe_api()
    service, database, client = await service_for(handler)
    async with client:
        connection = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=f"  {TEST_KEY}\n",
            expected_revision=None,
        )
    assert connection.external_account_id == "acct_FixtureAccount01"
    assert connection.external_account_label == "Fixture Co (test mode)"
    config = connection.configuration
    assert config["livemode"] is False and config["missing_permissions"] == []
    assert config["granted_capabilities"] == list(OPERATIONS_CAPABILITIES)
    # Encrypted under the project/provider context; never stored or logged in clear.
    assert TEST_KEY.encode() not in connection.credential_ciphertext
    assert (
        service._cipher.decrypt(
            connection.credential_ciphertext, context=f"credential:{PROJECT_ID}:{STRIPE_PROVIDER}"
        )
        == TEST_KEY
    )
    with pytest.raises(IntegrationAuthorizationError):
        service._cipher.decrypt(
            connection.credential_ciphertext, context=f"credential:{uuid4()}:{STRIPE_PROVIDER}"
        )
    assert TEST_KEY not in json.dumps([config, database.activities], default=str)
    # Every request is a GET to the fixed host with the pinned version and bearer auth.
    assert {(r.method, r.url.host) for r in seen} == {("GET", "api.stripe.com")}
    assert all(r.headers["stripe-version"] == STRIPE_VERSION for r in seen)
    assert all(r.headers["authorization"] == f"Bearer {TEST_KEY}" for r in seen)
    assert [r.url.path for r in seen][0] == "/v1/account"
    assert all(r.url.params.get("limit") == "1" for r in seen[1:])


OPERATIONS_CAPABILITIES = (
    "subscriptions.read",
    "customers.read",
    "invoices.read",
    "prices.read",
    "charges.read",
)


async def test_partial_permissions_grant_only_complete_reads() -> None:
    # No customer read: subscriptions (which expand customers) and customers are not granted.
    handler, _ = stripe_api(status={"/v1/customers": 403, "/v1/charges": 403})
    service, _, client = await service_for(handler)
    async with client:
        connection = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=LIVE_KEY,
            expected_revision=None,
        )
    assert connection.configuration["livemode"] is True
    assert connection.external_account_label == "Fixture Co"
    assert connection.configuration["granted_capabilities"] == ["invoices.read", "prices.read"]
    assert connection.configuration["missing_permissions"] == ["Customers", "Charges"]
    with pytest.raises(IntegrationAuthorizationError, match="subscriptions.read"):
        await service.ensure_requirements(
            project_id=PROJECT_ID,
            requirements=(IntegrationRequirement(STRIPE_PROVIDER, ("subscriptions.read",)),),
        )
    await service.ensure_requirements(
        project_id=PROJECT_ID,
        requirements=(IntegrationRequirement(STRIPE_PROVIDER, ("invoices.read",)),),
    )


# Stripe's wording for a restricted key that lacks the permission an endpoint needs.
ACCOUNT_REFUSAL = (
    f"The provided key '{LIVE_KEY[:8]}****{LIVE_KEY[-4:]}' does not have the required "
    "permissions for this endpoint on account 'acct_RefusalAccount7'. Having the "
    "'rak_connected_account_read' permission would allow this request to continue."
)


@pytest.mark.parametrize(
    ("message", "account_id", "label"),
    [
        (ACCOUNT_REFUSAL, "acct_RefusalAccount7", "acct_RefusalAccount7"),
        ("Forbidden", None, "Stripe"),
    ],
)
async def test_a_key_without_account_read_still_connects(message, account_id, label) -> None:
    # A restricted key needs a Connect permission for GET /v1/account; Tin's link never asks
    # for it, so the account id comes from Stripe's refusal, or from the key when that is silent.
    handler, _ = stripe_api(status={"/v1/account": 403}, messages={"/v1/account": message})
    service, _, client = await service_for(handler)
    async with client:
        connection = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=LIVE_KEY,
            expected_revision=None,
        )
        refreshed = await service.stripe.refresh(project_id=PROJECT_ID)
    if account_id:
        assert connection.external_account_id == account_id
    else:
        assert connection.external_account_id.startswith("stripe_key_")
        assert LIVE_KEY not in connection.external_account_id
    assert connection.external_account_label == label
    assert connection.configuration["account_name"] is None
    assert len(connection.configuration["granted_capabilities"]) == 5
    assert refreshed.external_account_id == connection.external_account_id


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        ({"/v1/account": 401}, IntegrationInputError, "rejected this key"),
        ({"/v1/account": 429}, IntegrationUpstreamError, "rate-limiting"),
        (dict.fromkeys(RESOURCE_PATHS, 403), IntegrationInputError, "cannot read"),
        ({"/v1/invoices": 500}, IntegrationUpstreamError, "finish checking"),
    ],
)
async def test_connect_rejections_store_nothing(status, error, message) -> None:
    handler, _ = stripe_api(status=status)
    service, database, client = await service_for(handler)
    async with client:
        with pytest.raises(error, match=message) as caught:
            await service.stripe.connect(
                project_id=PROJECT_ID,
                clerk_user_id=USER_ID,
                restricted_key=LIVE_KEY,
                expected_revision=None,
            )
    assert LIVE_KEY not in str(caught.value)
    assert database.connections == {}


async def test_replacing_a_key_needs_the_current_revision() -> None:
    handler, _ = stripe_api()
    service, database, client = await service_for(handler)
    async with client:
        first = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=TEST_KEY,
            expected_revision=None,
        )
        with pytest.raises(IntegrationAuthorizationError, match="changed"):
            await service.stripe.connect(
                project_id=PROJECT_ID,
                clerk_user_id=USER_ID,
                restricted_key=LIVE_KEY,
                expected_revision=None,
            )
        second = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=LIVE_KEY,
            expected_revision=first.configuration["revision"],
        )
    assert second.configuration["revision"] != first.configuration["revision"]
    assert second.configuration["livemode"] is True
    stored = database.connections[(PROJECT_ID, STRIPE_PROVIDER)]
    assert (
        service._cipher.decrypt(
            stored.credential_ciphertext, context=f"credential:{PROJECT_ID}:{STRIPE_PROVIDER}"
        )
        == LIVE_KEY
    )
    assert await service.disconnect(project_id=PROJECT_ID, provider_key=STRIPE_PROVIDER)
    assert database.connections == {}


async def test_refresh_rechecks_permissions_and_marks_a_revoked_key() -> None:
    status: dict[str, int] = {"/v1/charges": 403}
    handler, _ = stripe_api(status=status)
    service, database, client = await service_for(handler)
    async with client:
        await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=LIVE_KEY,
            expected_revision=None,
        )
        status.clear()
        refreshed = await service.stripe.refresh(project_id=PROJECT_ID)
        assert "charges.read" in refreshed.configuration["granted_capabilities"]
        assert refreshed.configuration["missing_permissions"] == []
        status["/v1/account"] = 401
        with pytest.raises(IntegrationInputError):
            await service.stripe.refresh(project_id=PROJECT_ID)
    stored = database.connections[(PROJECT_ID, STRIPE_PROVIDER)]
    assert stored.status == "needs_attention"
    assert stored.last_error_code == "authentication_failed"


async def test_start_connect_returns_tins_setup_page_not_a_key_prompt() -> None:
    service, _, client = await service_for(stripe_api()[0])
    async with client:
        started = await service.start_connect(
            project_id=PROJECT_ID, provider_key=STRIPE_PROVIDER, clerk_user_id=USER_ID
        )
    assert started.authorization_url == (
        f"https://lite.tin.test/connect?project={PROJECT_ID}&providers=payments.stripe"
    )


async def test_reader_is_get_only_and_maps_refusals() -> None:
    handler, seen = stripe_api(
        status={"/v1/charges": 429, "/v1/invoices": 403, "/v1/customers": 401, "/v1/prices": 302},
        headers={"/v1/charges": {"Stripe-Rate-Limited-Reason": "endpoint-rate"}},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = StripeReader(client, LIVE_KEY)
        for method in ("POST", "DELETE", "PATCH", "PUT"):
            with pytest.raises(StripeWriteRefused):
                await reader.request(method, "/v1/customers")
        with pytest.raises(StripeWriteRefused):
            await reader.get("/v1/refunds")
        assert seen == []
        with pytest.raises(IntegrationRateLimitedError, match="endpoint-rate") as limited:
            await reader.get("/v1/charges")
        assert limited.value.code == "rate_limited" and limited.value.reason == "endpoint-rate"
        with pytest.raises(StripePermissionDenied, match="Invoices") as denied:
            await reader.get("/v1/invoices")
        assert denied.value.code == "permission_denied"
        with pytest.raises(StripeAuthenticationFailed):
            await reader.get("/v1/customers")
        with pytest.raises(Exception, match="HTTP 302"):
            await reader.get("/v1/prices")
    for error in (limited.value, denied.value):
        assert LIVE_KEY not in str(error)


async def test_unknown_rate_limit_reasons_are_not_echoed() -> None:
    handler, _ = stripe_api(
        status={"/v1/charges": 429},
        headers={"/v1/charges": {"Stripe-Rate-Limited-Reason": "<script>"}},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationRateLimitedError) as caught:
            await StripeReader(client, LIVE_KEY).get("/v1/charges")
    assert caught.value.reason is None and "<script>" not in str(caught.value)


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("subscriptions.list", {"expand": "data.default_payment_method"}, "unsupported argument"),
        ("subscriptions.list", {"status": "deleted"}, "status must be one of"),
        ("subscriptions.list", {"created_gte": "2026-02-30"}, "Unix seconds or a YYYY-MM-DD"),
        ("subscriptions.list", {"created_gte": 1.5}, "Unix seconds"),
        ("subscriptions.list", {"created_gte": True}, "Unix seconds"),
        (
            "subscriptions.list",
            {"created_gte": "2026-09-02", "created_lte": "2026-09-01"},
            "not be after",
        ),
        ("customers.list", {"limit": 101}, "limit must be"),
        ("customers.list", {"limit": 0}, "limit must be"),
        ("customers.list", {"cursor": "cus_1&limit=100"}, "Stripe object id"),
        ("customers.list", {"email": "a b@example.com"}, "exact address"),
        ("invoices.list", {"customer": "sub_Abc"}, "start with cus_"),
        ("invoices.list", {"status": "paid,open"}, "status must be one of"),
        ("prices.list", {"active": "true"}, "true or false"),
        ("prices.list", {"created_gte": 0}, "unsupported argument"),
        ("charges.list", {"customer": "cus_Abc"}, "unsupported argument"),
        ("refunds.list", {}, "unknown Stripe operation"),
    ],
)
def test_arguments_are_closed_and_validated(operation, arguments, message) -> None:
    with pytest.raises(ServiceArgumentError, match=message):
        request_for(operation, arguments)


def test_arguments_map_to_stripe_list_parameters() -> None:
    spec, params = request_for(
        "subscriptions.list",
        {"created_gte": "2026-09-01", "created_lte": "2026-09-01", "cursor": "sub_Fixture0002"},
    )
    assert spec.path == "/v1/subscriptions" and spec.capability == "subscriptions.read"
    assert params == {
        "limit": 100,
        "expand[]": "data.customer",
        "starting_after": "sub_Fixture0002",
        "created[gte]": 1788220800,
        "created[lte]": 1788307199,
        "status": "all",
    }
    _, params = request_for("prices.list", {"active": False, "limit": 5})
    assert params == {"limit": 5, "expand[]": "data.product", "active": "false"}
    _, params = request_for(
        "invoices.list", {"status": "open", "subscription": "sub_X1", "created_gte": 5}
    )
    assert params == {"limit": 100, "created[gte]": 5, "status": "open", "subscription": "sub_X1"}


async def test_recorded_projections_match_the_fixture_objects() -> None:
    recorded = json.loads((FIXTURES / "stripe_projected.json").read_text())
    fake = FakeStripeConnection(OBJECTS)
    for operation in OPERATIONS:
        result = await fake.call(
            service="stripe", step=operation, operation=operation, arguments={}
        )
        assert result == recorded[operation], operation
    subscriptions = recorded["subscriptions.list"]["records"]
    canceled = next(s for s in subscriptions if s["id"] == "sub_Fixture0002")
    assert canceled["cancellation_details"] == {
        "reason": "cancellation_requested",
        "feedback": "too_expensive",
    }
    assert canceled["discount_ids"] == ["di_FixtureLaunch"]
    assert canceled["customer"]["email_domain"] == "hopper.example"
    # The period end comes from the items since basil; the price's product stays an id.
    assert all(s["current_period_end"] for s in subscriptions)
    assert subscriptions[-1]["items"][0]["quantity"] == 3
    invoices = recorded["invoices.list"]["records"]
    assert [i["subscription"] for i in invoices] == [
        None,
        "sub_Fixture0002",
        "sub_Fixture0002",
        "sub_Fixture0001",
    ]
    prices = recorded["prices.list"]["records"]
    assert prices[-1]["product"] == {"id": "prod_FixturePro", "name": "Pro"}
    assert prices[0]["recurring"] is None
    # Full customer emails and names are returned by design; card details never are.
    text = json.dumps(recorded)
    assert "ada@lovelace.example" in text and "4242" not in text and "pm_Fixture" not in text


def test_projection_bounds_provider_strings_and_metadata() -> None:
    big = {
        "id": "cus_Big",
        "email": "x" * 400 + "@example.com",
        "name": "n" * 500,
        "metadata": {f"k{i:02d}": "v" * 600 for i in range(30)},
        "address": {"country": "USA-long"},
        "created": 1,
        "currency": "usd",
        "delinquent": "no",
    }
    record = project_page(
        "customers.list",
        {"object": "list", "data": [big], "has_more": False},
        max_response_bytes=None,
        livemode=True,
    )["records"][0]
    assert len(record["email"]) == 320 and len(record["name"]) == 200
    assert len(record["metadata"]) == 20 and all(len(v) == 200 for v in record["metadata"].values())
    assert record["country"] == "US" and record["delinquent"] is None
    with pytest.raises(IntegrationUpstreamError):
        project_page(
            "customers.list", {"object": "list", "data": ["x"], "has_more": False}, **_bound()
        )
    with pytest.raises(IntegrationUpstreamError):
        project_page("customers.list", {"data": [], "has_more": False}, **_bound())


def _bound():
    return {"max_response_bytes": 64000, "livemode": False}


def test_fit_keeps_leading_records_and_resumes_after_the_last_one() -> None:
    records = [{"id": f"cus_{i:03d}", "pad": "x" * 200} for i in range(40)]
    whole = fit_records(records, max_response_bytes=64_000, has_more=False)
    assert whole["records"] == records and whole["next_cursor"] is None
    assert whole["truncated"] is False and whole["has_more"] is False
    upstream_more = fit_records(records, max_response_bytes=64_000, has_more=True)
    assert upstream_more["truncated"] is False and upstream_more["next_cursor"] == "cus_039"
    for bound in (1024, 2000, 4096, 5003):
        fitted = fit_records(records, max_response_bytes=bound, has_more=False, envelope={"a": 1})
        assert json_size(fitted) <= bound
        kept = len(fitted["records"])
        assert 0 < kept < 40 and fitted["records"] == records[:kept]
        assert fitted["truncated"] and fitted["has_more"]
        assert fitted["next_cursor"] == records[kept - 1]["id"] and fitted["a"] == 1
        # One more record would not have fit.
        assert json_size({**fitted, "records": records[: kept + 1]}) > bound
    with pytest.raises(ServiceResponseTooLarge):
        fit_records([{"id": "cus_1", "pad": "x" * 5000}], max_response_bytes=1024, has_more=False)
    assert fit_records([], max_response_bytes=1024, has_more=False)["records"] == []


async def test_fake_pages_through_everything_with_the_cursor() -> None:
    many = {
        **OBJECTS,
        "customers": [
            {**OBJECTS["customers"][0], "id": f"cus_Many{i:04d}", "created": 1_700_000_000 + i}
            for i in range(250)
        ],
    }
    fake = FakeStripeConnection(many, max_response_bytes=8000)
    seen, cursor, step = [], None, 0
    while True:
        step += 1
        arguments = {"cursor": cursor} if cursor else {}
        page = await fake.call(
            service="stripe", step=f"page_{step}", operation="customers.list", arguments=arguments
        )
        assert json_size(page) <= 8000
        seen += [record["id"] for record in page["records"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        if step == 8:
            break
    assert seen == [f"cus_Many{i:04d}" for i in range(249, 249 - len(seen), -1)]
    assert len(seen) == len(set(seen))
    with pytest.raises(CodeServiceError, match="service-call limit"):
        await fake.call(service="stripe", step="ninth", operation="customers.list")
    # A repeated step replays; a changed request under the same step conflicts.
    assert (
        await fake.call(service="stripe", step="page_1", operation="customers.list")
        == (fake.calls[0]["response"])
    )
    with pytest.raises(CodeServiceError, match="different request"):
        await fake.call(
            service="stripe", step="page_1", operation="customers.list", arguments={"limit": 1}
        )


async def test_fake_rehearses_refusals_and_capability_gating() -> None:
    fake = FakeStripeConnection(
        OBJECTS, capabilities=("invoices.read",), refuse={"busy": "rate_limited"}
    )
    with pytest.raises(CodeServiceError, match="declared contract"):
        await fake.call(service="stripe", step="subs", operation="subscriptions.list")
    with pytest.raises(CodeServiceError, match="rate-limited"):
        await fake.call(service="stripe", step="busy", operation="invoices.list")
    with pytest.raises(CodeServiceError, match="rate-limited"):
        await fake.call(service="stripe", step="busy", operation="invoices.list")
    result = await fake.call(service="stripe", step="retry", operation="invoices.list")
    assert len(result["records"]) == 4
    with pytest.raises(CodeServiceError, match="contract: status"):
        await fake.call(
            service="stripe", step="bad", operation="invoices.list", arguments={"status": "x"}
        )


async def test_service_call_projects_fits_and_receipts_without_the_key() -> None:
    handler, seen = stripe_api()
    service, database, client = await service_for(handler)
    async with client:
        connection = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=LIVE_KEY,
            expected_revision=None,
        )
        seen.clear()
        result = await service.stripe.call(
            "subscriptions.list",
            {"limit": 3},
            connection=connection,
            run_id=RUN_ID,
            execution_key="run:code-service:abc",
            max_response_bytes=2200,
        )
    assert seen[0].url.params["expand[]"] == "data.customer"
    assert seen[0].url.params["limit"] == "3" and seen[0].url.params["status"] == "all"
    assert json_size(result) <= 2200
    assert result["livemode"] is True and result["truncated"] is True
    assert result["next_cursor"] == result["records"][-1]["id"]
    call = database.calls[-1]
    assert call["status"] == "completed" and call["capability"] == "subscriptions.read"
    assert call["provider_request_id"] == "req_page" and call["run_id"] == RUN_ID
    assert call["response_summary"] == {
        "record_count": 3,
        "returned_count": len(result["records"]),
        "has_more": True,
        "truncated": True,
    }
    assert LIVE_KEY not in json.dumps(database.calls, default=str)


async def test_service_call_revoked_key_marks_the_connection() -> None:
    status: dict[str, int] = {}
    handler, _ = stripe_api(status=status)
    service, database, client = await service_for(handler)
    async with client:
        connection = await service.stripe.connect(
            project_id=PROJECT_ID,
            clerk_user_id=USER_ID,
            restricted_key=LIVE_KEY,
            expected_revision=None,
        )
        status["/v1/charges"] = 401
        with pytest.raises(StripeAuthenticationFailed):
            await service.stripe.call(
                "charges.list",
                {},
                connection=connection,
                run_id=RUN_ID,
                execution_key="run:code-service:revoked",
                max_response_bytes=8000,
            )
        status["/v1/charges"] = 403
        with pytest.raises(StripePermissionDenied):
            await service.stripe.call(
                "charges.list",
                {},
                connection=connection,
                run_id=RUN_ID,
                execution_key="run:code-service:denied",
                max_response_bytes=8000,
            )
    stored = database.connections[(PROJECT_ID, STRIPE_PROVIDER)]
    assert stored.status == "needs_attention"
    assert [c["error_code"] for c in database.calls[-2:]] == [
        "authentication_failed",
        "permission_denied",
    ]


async def test_api_route_never_echoes_a_key_and_maps_input_errors() -> None:
    from fastapi import FastAPI

    from tin_lite.api import router
    from tin_lite.auth import AuthContext, require_user

    class AccessDatabase(FakeIntegrationDatabase):
        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == PROJECT_ID and clerk_user_id == USER_ID

        async def get_project(self, project_id):
            return SimpleNamespace(id=project_id, name="Stripe proof")

    handler, _ = stripe_api()
    service, _, client = await service_for(handler, AccessDatabase())
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(integrations=service, database=service._database)
    app.state.settings = settings()
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=USER_ID,
        token_type="session_token",  # noqa: S106 — token category, not a credential
    )
    url = f"/api/projects/{PROJECT_ID}/integrations/payments.stripe/key"
    async with (
        client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as web,
    ):
        secret = "sk_live_" + "s" * 30
        refused = await web.post(url, json={"restricted_key": secret, "expected_revision": None})
        assert refused.status_code == 422 and secret not in refused.text
        malformed = await web.post(url, json={"restricted_key": LIVE_KEY, "extra": 1})
        assert malformed.status_code == 422 and LIVE_KEY not in malformed.text
        saved = await web.post(url, json={"restricted_key": LIVE_KEY, "expected_revision": None})
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["status"] == "connected" and LIVE_KEY not in saved.text
        assert body["setup_url"] == create_key_url()
        listed = await web.get(f"/api/projects/{PROJECT_ID}/integrations")
        stripe = next(item for item in listed.json() if item["key"] == STRIPE_PROVIDER)
        assert stripe["configured"] is True and LIVE_KEY not in listed.text
        refreshed = await web.post(
            f"/api/projects/{PROJECT_ID}/integrations/payments.stripe/refresh"
        )
        assert refreshed.status_code == 200
        foreign = await web.post(
            f"/api/projects/{uuid4()}/integrations/payments.stripe/key",
            json={"restricted_key": LIVE_KEY, "expected_revision": None},
        )
        assert foreign.status_code == 404


async def test_gateway_replays_stripe_steps_and_settles_refusals(billed, monkeypatch):
    """Real Postgres receipts: replay per step, closed contracts, a 429 that does not wedge."""
    from dataclasses import replace

    from temporalio.testing import ActivityEnvironment
    from test_private_workflows import ACTOR
    from test_project_connections import definition, prepared, public_dns

    from tin_lite.run_usage import read_run_usage
    from tin_lite.workflow_code import validate_code_definition

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    # Start the run the way the GSC gateway test does: the fixture compute stops mid-run.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))
        )
    ) as crm:
        code.services.client, code.services.resolver = crm, public_dns
        with pytest.raises(RuntimeError, match="worker loss"):
            await ActivityEnvironment().run(code.execute, run_id)
    status: dict[str, int] = {}
    handler, seen = stripe_api(
        status=status, headers={"/v1/charges": {"Stripe-Rate-Limited-Reason": "global-rate"}}
    )
    original = service._client
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await service.stripe.connect(
            project_id=f.project.id,
            clerk_user_id=ACTOR,
            restricted_key=LIVE_KEY,
            expected_revision=None,
        )
        run, workflow, _, _, _ = await code.selected(run_id)
        body = definition()
        body["integration_requirements"] = [
            {
                "provider_key": STRIPE_PROVIDER,
                "capabilities": ["subscriptions.read", "charges.read"],
                "required": True,
            }
        ]
        body["code"]["services"] = {
            "stripe": {"provider_key": STRIPE_PROVIDER, "max_calls": 3, "max_response_bytes": 4000}
        }
        changed, spec = replace(workflow, definition=body), validate_code_definition(body)

        def call(conn, step, operation, arguments):
            return code.services.call(
                conn=conn,
                run=run,
                workflow=changed,
                spec=spec,
                payload={
                    "service": "stripe",
                    "step": step,
                    "operation": operation,
                    "arguments": arguments,
                },
            )

        async with f.db.pool.acquire() as conn:
            seen.clear()
            first = await call(conn, "subs", "subscriptions.list", {"limit": 2})
            assert len(first["records"]) == 2 and first["next_cursor"] == "sub_Fixture0003"
            assert json_size(first) <= 4000 and len(seen) == 1
            assert await call(conn, "subs", "subscriptions.list", {"limit": 2}) == first
            assert len(seen) == 1
            with pytest.raises(CodeServiceError, match="different request"):
                await call(conn, "subs", "subscriptions.list", {"limit": 3})
            with pytest.raises(CodeServiceError, match="contract: status must be one of"):
                await call(conn, "bad", "subscriptions.list", {"status": "gone"})
            with pytest.raises(CodeServiceError, match="declared contract"):
                await call(conn, "bad", "invoices.list", {})  # not a declared capability
            status["/v1/charges"] = 429
            with pytest.raises(CodeServiceError, match=r"rate-limited this read \(global-rate\)"):
                await call(conn, "charges", "charges.list", {})
            with pytest.raises(CodeServiceError, match="rate-limited"):
                await call(conn, "charges", "charges.list", {})
            assert len(seen) == 2
            status.clear()
            retried = await call(conn, "charges_retry", "charges.list", {})
            assert [r["id"] for r in retried["records"]] == [
                "ch_Fixture0003",
                "ch_Fixture0002",
                "ch_Fixture0001",
            ]
            with pytest.raises(CodeServiceError, match="service-call limit"):
                await call(conn, "fourth", "charges.list", {"limit": 1})
        rows = await f.db.pool.fetch(
            """SELECT operation, status, result FROM effect_receipts
               WHERE operation IN ('code_service_call_v1', 'external_usage_v1')"""
        )
        assert rows and all(row["status"] == "completed" for row in rows)
        assert LIVE_KEY not in json.dumps([dict(r) for r in rows], default=str)
        refused = [
            json.loads(r["result"])
            for r in rows
            if r["operation"] == "code_service_call_v1"
            and json.loads(r["result"]).get("error") == "rate_limited"
        ]
        assert len(refused) == 1 and "response" not in refused[0]
        receipts = await f.db.pool.fetch(
            "SELECT status, error_code FROM integration_call_receipts WHERE provider_key=$1",
            STRIPE_PROVIDER,
        )
        assert sorted((r["status"], r["error_code"]) for r in receipts) == [
            ("completed", None),
            ("completed", None),
            ("failed", "rate_limited"),
        ]
        usage = await read_run_usage(database=f.db, run=await f.db.get_run(run.id))
        external = [o for o in usage["own"]["observations"] if o["kind"] == "connected_api"]
        assert len(external) == 4  # the three Stripe steps plus the CRM call that started the run
    finally:
        await service._client.aclose()
        service._client = original
        await service.close()
