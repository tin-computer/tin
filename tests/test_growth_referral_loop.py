"""Offline checks for growth.referral_loop; Stripe and model responses are synthetic."""

import json
import runpy
from decimal import Decimal
from types import SimpleNamespace

import jsonschema
import pytest

from connection_fakes import FakeStripeConnection, stripe_objects
from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = REPOSITORY_ROOT / "workflow_packages" / "growth.referral_loop"
PROJECT_ID = "a0000000-0000-0000-0000-000000000001"
CREATED_AT = "2026-09-01T12:00:00+00:00"
GOOD_COPY = {
    "page_headline": "Share a tool that helps your team",
    "page_intro": (
        "Invite a colleague who could use the product in their work. Both people can benefit "
        "from the proposed referral credit."
    ),
    "email_subject": "Could someone in your team use this?",
    "email_opening": "I hope the product has been useful in your day-to-day work.",
    "email_ask": (
        "If a colleague comes to mind who would find it useful, would you be open to "
        "introducing them?"
    ),
    "email_closing": "Thanks for being a customer",
}


def package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    return SimpleNamespace(**runpy.run_path(str(ROOT / "main.py"))), definition


class Files:
    def __init__(self, values=None):
        self.values = values or {}
        self.reads = []

    def read_text(self, path):
        self.reads.append(path)
        if path not in self.values:
            raise FileNotFoundError(path)
        return self.values[path]


class Context:
    def __init__(self, definition, *, services, model_output=GOOD_COPY, files=None):
        spec = validate_code_definition(definition)
        self.values = {"run_id": "00000000-0000-4000-8000-000000000099", "created_at": CREATED_AT}
        self.files = Files(files)
        self.services = services
        self.model_calls = []

        async def generate(**payload):
            request_contract(spec, payload)
            self.model_calls.append(payload)
            if isinstance(model_output, dict) and set(model_output) == set(GOOD_COPY):
                jsonschema.validate(model_output, payload["output_schema"])
            return {"parsed": model_output, "text": json.dumps(model_output)}

        self.models = SimpleNamespace(generate=generate)

    def __getitem__(self, key):
        return self.values[key]


def live_stripe(**overrides):
    objects = stripe_objects()
    for customer in objects["customers"]:
        customer["livemode"] = True
    for subscription in objects["subscriptions"]:
        subscription["livemode"] = True
    return FakeStripeConnection(
        objects,
        service="stripe",
        max_response_bytes=64_000,
        livemode=True,
        **overrides,
    )


def fallback_inputs(**overrides):
    return {
        "project_id": PROJECT_ID,
        "arpu": 100,
        "currency": "USD",
        "gross_margin_percent": 80,
        "monthly_churn_percent": 5,
        "current_cac": 30,
        "allowed_ltv_percent": 30,
        **overrides,
    }


async def run_with(services, inputs=None, *, model_output=GOOD_COPY, files=None):
    module, definition = package()
    ctx = Context(definition, services=services, model_output=model_output, files=files)
    result = await module.run(ctx, {"project_id": PROJECT_ID, **(inputs or {})})
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition))
    return result["content"], ctx


def test_manifest_has_optional_read_only_stripe_and_one_bounded_copy_call():
    _, definition = package()
    spec = validate_code_definition(definition)
    assert definition["key"] == "growth.referral_loop"
    assert definition["integration_requirements"] == [
        {
            "provider_key": "payments.stripe",
            "capabilities": ["subscriptions.read"],
            "required": False,
        }
    ]
    assert definition["human_review"]["eligible"] is True
    assert definition["code"]["services"]["stripe"]["max_calls"] == 8
    assert [(route.model, route.max_calls) for route in spec.model_routes] == [("gpt-6-luna", 1)]
    assert spec.output_path == "reports/growth/REFERRAL_LOOP.md"


def test_stripe_zero_decimal_currencies_keep_their_minor_unit():
    module, _ = package()
    assert module.currency_decimals("JPY") == 0
    assert module.currency_decimals("USD") == 2
    assert module.money(Decimal("1250"), "JPY") == "JPY 1,250"


async def test_live_stripe_selects_only_mature_active_customers_and_caps_credit():
    stripe = live_stripe()
    content, ctx = await run_with(
        stripe,
        {
            "gross_margin_percent": 80,
            "monthly_churn_percent": 5,
            "current_cac": 40,
            "currency": "USD",
            "allowed_ltv_percent": 30,
            "minimum_paid_days": 90,
        },
        files={"reports/GROWTH_ONBOARDING_PLAN.md": "The product serves small engineering teams."},
    )
    assert "Estimated gross-profit LTV: USD 812.00" in content
    assert "Current CAC comparison: USD 40.00" in content
    assert "Maximum combined two-sided credit face value: USD 50.00" in content
    assert "USD 12.50 credit for each person" in content
    assert "ada@lovelace.example" in content and "linus@kernel.example" in content
    assert "grace@hopper.example" not in content and "margaret@apollo.example" not in content
    assert len(stripe.calls) == 1
    assert stripe.calls[0]["operation"] == "subscriptions.list"
    assert ctx.model_calls[0]["step"] == "draft_referral_copy"
    assert "ada@lovelace.example" not in json.dumps(ctx.model_calls[0]["data"])
    assert "nothing is sent, published or changed" in content


async def test_stripe_refusal_uses_supplied_economics_without_contacts():
    stripe = live_stripe(refuse={"stripe_subscriptions_1": "permission_denied"})
    content, _ = await run_with(stripe, fallback_inputs())
    assert "Stripe: unavailable; using fallback inputs where supplied" in content
    assert "Monthly ARPU estimate: USD 100.00 (fallback input)" in content
    assert "Estimated gross-profit LTV: USD 1,600.00" in content
    assert "Current CAC comparison: USD 30.00" in content
    assert "USD 9.37 credit for each person" in content
    assert "No Stripe contacts are available" in content
    assert "ada@lovelace.example" not in content


async def test_supplied_net_arpu_overrides_stripe_list_price_estimate():
    content, _ = await run_with(
        live_stripe(),
        {
            "arpu": 100,
            "currency": "USD",
            "gross_margin_percent": 80,
            "monthly_churn_percent": 5,
        },
    )
    assert "Monthly ARPU estimate: USD 100.00 (supplied net ARPU)" in content
    assert "USD 50.75 (live Stripe listed recurring prices)" not in content


async def test_missing_economics_keeps_default_shape_and_does_not_invent_a_price():
    class MissingConnection:
        async def call(self, **_):
            raise ValueError(
                "The declared project connection is unavailable or permission was removed."
            )

    content, _ = await run_with(MissingConnection())
    assert "Two-sided account credit" in content
    assert "No defensible dollar reward is priced in this run" in content
    assert "{{credit_amount}}" in content
    assert "monthly ARPU and its currency, gross margin, monthly churn" in content
    assert "Maximum combined two-sided credit face value:" not in content


async def test_plausible_but_unusable_stripe_page_uses_fallback_instead_of_claiming_a_sample():
    class UnusablePage:
        async def call(self, **_):
            return {
                "livemode": True,
                "records": [{"id": "sub_1"}],
                "has_more": True,
                "truncated": False,
                "next_cursor": None,
            }

    content, _ = await run_with(UnusablePage(), fallback_inputs())
    assert "Stripe returned an unusable subscription page" in content
    assert "Monthly ARPU estimate: USD 100.00 (fallback input)" in content
    assert "No Stripe contacts are available" in content
    assert "sub_1" not in content


async def test_test_mode_data_never_lists_contacts_or_sets_stripe_arpu():
    stripe = FakeStripeConnection(stripe_objects(), service="stripe", max_response_bytes=64_000)
    content, _ = await run_with(stripe, fallback_inputs())
    assert "Stripe test-mode records are excluded" in content
    assert "No contacts listed: this connection is in Stripe test mode." in content
    assert "ada@lovelace.example" not in content
    assert "Monthly ARPU estimate: USD 100.00 (fallback input)" in content


async def test_plausible_but_unusable_model_copy_is_rejected():
    stripe = live_stripe()
    bad_copy = {**GOOD_COPY, "email_ask": "https://example.invalid"}
    with pytest.raises(ValueError, match="unusable email_ask"):
        await run_with(stripe, fallback_inputs(), model_output=bad_copy)
    invented_reward = {
        **GOOD_COPY,
        "page_intro": "Invite a friend and get USD 50 in credit for each signup.",
    }
    with pytest.raises(ValueError, match="unusable page_intro"):
        await run_with(live_stripe(), fallback_inputs(), model_output=invented_reward)


async def test_qualification_cases_are_concrete_and_synthetic_runs_satisfy_them():
    qualification_path = REPOSITORY_ROOT / "workflow_evals/growth.referral_loop/qualification.json"
    qualification = Qualification.model_validate_json(qualification_path.read_bytes())
    fixtures = {
        "live_subscription_economics": live_stripe(),
        "stripe_unavailable_fallback": live_stripe(
            refuse={"stripe_subscriptions_1": "permission_denied"}
        ),
        "economics_missing": SimpleNamespace(
            call=_async_error(
                "The declared project connection is unavailable or permission was removed."
            )
        ),
    }
    assert {case.id for case in qualification.cases} == set(fixtures)
    for case in qualification.cases:
        content, _ = await run_with(fixtures[case.id], case.inputs)
        report = assess_output(case, status="succeeded", content=content.encode())
        assert report["status"] == "passed", (case.id, report["checks"])


def _async_error(message):
    async def call(**_):
        raise ValueError(message)

    return call


@pytest.mark.parametrize(
    "inputs",
    [
        {"minimum_paid_days": True},
        {"gross_margin_percent": 0},
        {"monthly_churn_percent": 0},
        {"currency": "US"},
    ],
)
async def test_invalid_business_inputs_are_rejected_before_stripe_or_model(inputs):
    stripe = live_stripe()
    module, definition = package()
    ctx = Context(definition, services=stripe)
    with pytest.raises(ValueError):
        await module.run(ctx, {"project_id": PROJECT_ID, **inputs})
    assert stripe.calls == []
    assert ctx.model_calls == []
