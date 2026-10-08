from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_billing import ACTOR, fund, quote
from test_billing import billed as billed
from test_gak import settings_values
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing import BillingService
from tin_lite.billing_contracts import BillingError
from tin_lite.billing_payments import PRODUCTS, StripePayments
from tin_lite.settings import Settings


def test_stripe_key_must_match_the_configured_mode(monkeypatch):
    def settings(key, **overrides):
        return Settings(
            _env_file=None,
            **settings_values(
                monkeypatch,
                TIN_LITE_BILLING_ENABLED="true",
                TIN_LITE_BILLING_TEST_ENABLED="true",
                STRIPE_SECRET_KEY=key,
                **overrides,
            ),
        )

    assert settings("rk_test_x").stripe_mode == "test"
    assert settings("rk_live_x", TIN_LITE_STRIPE_MODE="live").stripe_mode == "live"
    with pytest.raises(ValueError, match="not a test-mode key"):
        settings("sk_live_x")
    with pytest.raises(ValueError, match="not a live-mode key"):
        settings("rk_test_x", TIN_LITE_STRIPE_MODE="live")


def go_live(f):
    """Switch the fixture to live mode after its test-mode setup, as a deployment would."""
    f.settings.stripe_mode = "live"
    f.settings.stripe_secret_key = SecretStr("rk_live_fixture")
    f.billing = BillingService(database=f.db, settings=f.settings)
    f.db.billing = f.billing
    f.live_calls = []

    def wire(request):
        f.live_calls.append(request)
        if "/products" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": PRODUCTS["live"][0],
                    "livemode": True,
                    "metadata": {"tin_product": "tin-lite"},
                },
            )
        if request.url.path.endswith("/checkout/sessions"):
            pid = parse_qs(request.content.decode())["metadata[tin_payment_id]"][0]
            return httpx.Response(
                200,
                json={
                    "id": f"cs_live_{pid}",
                    "livemode": True,
                    "url": f"https://checkout.stripe.com/c/pay/{pid}",
                },
            )
        raise AssertionError(request.url.path)

    f.payments = StripePayments(
        billing=f.billing, settings=f.settings, transport=httpx.MockTransport(wire)
    )


def completed(payment_id, cents, *, livemode):
    return {
        "id": f"evt_live_{payment_id}",
        "type": "checkout.session.completed",
        "livemode": livemode,
        "data": {
            "object": {
                "id": f"cs_live_{payment_id}",
                "mode": "payment",
                "livemode": livemode,
                "currency": "usd",
                "amount_total": cents,
                "payment_status": "paid",
                "payment_intent": f"pi_live_{payment_id}",
                "metadata": {"tin_product": "tin-lite", "tin_payment_id": payment_id},
            }
        },
    }


async def test_live_top_up_uses_the_live_product_and_credits_the_same_balance(billed):
    f = billed
    await fund(f, 2500)
    go_live(f)
    payment = await f.payments.checkout(
        workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=1000, request_id=uuid4()
    )
    assert payment["mode"] == "live"
    body = parse_qs(f.live_calls[-1].content.decode())
    assert body["line_items[0][price_data][product]"] == [PRODUCTS["live"][0]]
    assert all(call.headers["Authorization"] == "Bearer rk_live_fixture" for call in f.live_calls)

    ignored = await f.payments.handle_event(completed(payment["id"], 1000, livemode=False))
    assert ignored == {"received": True, "ignored": True}
    await f.payments.handle_event(completed(payment["id"], 1000, livemode=True))
    # The earlier test-mode top-up stays in the balance; the live one adds to it.
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "35.00"


async def test_test_mode_top_up_is_kept_but_not_refundable_through_live_stripe(billed):
    f = billed
    earlier, _ = await fund(f, 2500)
    go_live(f)
    [view] = await f.payments.list_payments(f.project.workspace_id, ACTOR)
    assert view["mode"] == "test" and view["refundable_usd"] == "0.00"
    with pytest.raises(BillingError, match="test-mode payment") as refused:
        await f.payments.request_refund(UUID(earlier["id"]), ACTOR, uuid4(), 1000)
    assert refused.value.code == "refund_unavailable"
    assert f.live_calls == []


async def test_live_mode_rejects_a_test_mode_checkout_response(billed):
    f = billed
    go_live(f)
    f.payments.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "id": PRODUCTS["live"][0],
                "livemode": False,
                "metadata": {"tin_product": "tin-lite"},
            },
        )
    )
    with pytest.raises(BillingError) as refused:
        await f.payments.checkout(
            workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=1000, request_id=uuid4()
        )
    assert refused.value.code == "product_mismatch"


async def test_live_quotes_say_live_and_never_use_the_illustrative_card(billed):
    f = billed
    go_live(f)
    # The fixture's legacy workflow prices on the illustrative fallback card.
    with pytest.raises(BillingError) as refused:
        await quote(f)
    assert refused.value.code == "unmetered_profile"
    f.settings.codex_api_projects = {f.project.id}
    q = await quote(f)
    assert q["mode"] == "live" and q["terms"]["mode"] == "live"
    assert not q["notice"].startswith("Test funds")
