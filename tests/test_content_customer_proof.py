"""Offline tests for content.customer_proof using Tin's Stripe gateway fake."""

import copy
import json
import runpy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from connection_fakes import FakeStripeConnection, stripe_objects

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.code_models import CodeModelError, request_contract
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = REPOSITORY_ROOT / "workflow_packages" / "content.customer_proof"
NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
DAY = 86_400
PATH = "reports/content/CUSTOMER_PROOF.md"
MODEL_PII = "PRIVATE_METADATA_SHOULD_NOT_REACH_MODEL"

MODULE = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
DEFINITION = json.loads((ROOT / "workflow.json").read_text())["definition"]
SPEC = validate_code_definition(DEFINITION)


class Context(dict):
    def __init__(self, stripe, model_output=None, *, model_error=None):
        super().__init__(run_id="00000000-0000-4000-8000-000000000099", created_at=NOW.isoformat())
        self.services = stripe
        self.model_requests = []

        async def generate(**payload):
            request_contract(SPEC, payload)
            self.model_requests.append(payload)
            if model_error:
                raise model_error
            output = (
                model_output
                if model_output is not None
                else valid_model_output(payload["data"]["candidates"])
            )
            return {"parsed": output, "text": json.dumps(output)}

        self.models = SimpleNamespace(generate=generate)


def valid_model_output(candidates):
    return {
        "briefs": [
            {
                "candidate_index": candidate["candidate_index"],
                "question_ids": ["before_state", "measurable_result", "permission"],
                "angle_ids": ["before_after"],
            }
            for candidate in candidates
        ]
    }


def dataset(entries, *, livemode=False):
    """Create Stripe-shaped objects by adapting the committed synthetic gateway fixture."""
    objects = stripe_objects()
    base_sub = objects["subscriptions"][0]
    base_customer = objects["customers"][0]
    subscriptions, customers = [], []
    for index, entry in enumerate(entries, start=1):
        sub = copy.deepcopy(base_sub)
        customer = copy.deepcopy(base_customer)
        customer_id = entry.get("customer_id", f"cus_private_{index:05d}")
        sub_id = entry.get("subscription_id", f"sub_private_{index:05d}")
        created = int(NOW.timestamp()) - entry.get("created_days", 300) * DAY
        start = int(NOW.timestamp()) - entry.get("start_days", 300) * DAY
        sub.update(
            id=sub_id,
            customer=customer_id,
            created=created,
            start_date=start,
            status=entry.get("status", "active"),
            ended_at=(
                entry.get("ended_days_ago")
                and int(NOW.timestamp()) - entry["ended_days_ago"] * DAY
            ),
            trial_end=(int(NOW.timestamp()) - entry["trial_paid_days"] * DAY)
            if entry.get("trial_paid_days") is not None
            else None,
            livemode=livemode,
            metadata={"secret": MODEL_PII, "campaign": "synthetic"},
        )
        customer.update(
            id=customer_id,
            email=entry.get("email", f"owner@customer{index}.example.test"),
            name=entry.get("name", f"Private Customer {index}"),
            metadata={"private": MODEL_PII, "customer_ref": f"id_{index:08d}"},
        )
        # The fake expands by ID from this object collection.
        subscriptions.append(sub)
        customers.append(customer)
    return {**objects, "subscriptions": subscriptions, "customers": customers}


def stripe(entries, **options):
    return FakeStripeConnection(
        dataset(entries), service="stripe", max_response_bytes=64_000, **options
    )


async def run(
    entries,
    *,
    model_output=None,
    model_error=None,
    inputs=None,
    binding=None,
    livemode=False,
):
    binding = binding or FakeStripeConnection(
        dataset(entries, livemode=livemode),
        service="stripe",
        max_response_bytes=64_000,
        livemode=livemode,
    )
    context = Context(binding, model_output, model_error=model_error)
    result = await MODULE.run(
        context,
        {
            "retention_days": 180,
            "lookback_days": 730,
            **(inputs or {}),
        },
    )
    validate_code_result(json.dumps(result).encode(), validate_code_definition(DEFINITION))
    return result, binding, context


def ordinary():
    return [{"email": "owner@customer1.io", "start_days": 300, "created_days": 300}]


def test_manifest_declares_one_stripe_binding_one_model_route_and_artifact():
    spec = validate_code_definition(DEFINITION)
    assert DEFINITION["integration_requirements"] == [
        {
            "provider_key": "payments.stripe",
            "capabilities": ["subscriptions.read"],
            "required": True,
        }
    ]
    assert DEFINITION["code"]["services"]["stripe"] == {
        "provider_key": "payments.stripe",
        "max_calls": MODULE.MAX_CALLS,
        "max_response_bytes": MODULE.MAX_RESPONSE_BYTES,
    }
    assert set(DEFINITION["code"]["model_routes"]) == {"brief"}
    assert DEFINITION["code"]["output"]["path"] == MODULE.OUTPUT
    assert spec is not None


async def test_ordinary_qualifying_customer_gets_candidate_indexed_brief():
    result, binding, context = await run(ordinary())
    report = result["content"]
    assert result["path"] == PATH
    assert "Candidate 1" in report and "customer1.io" in report
    assert "Story hypotheses to investigate (not findings)" in report
    assert "Before using the product" in report and "Permission to request" in report
    assert "Paid tenure is only a deterministic candidate-selection signal" in report
    assert binding.calls[0]["operation"] == "subscriptions.list"
    assert len(context.model_requests) == 1


async def test_customer_below_paid_tenure_threshold_is_not_selected():
    result, _, context = await run(
        [{"email": "owner@short.io", "start_days": 120, "created_days": 300}]
    )
    assert "No candidates met the selection rule" in result["content"]
    assert not context.model_requests


async def test_trial_period_does_not_count_as_paid_tenure():
    result, _, _ = await run(
        [
            {
                "email": "owner@trial.io",
                "start_days": 300,
                "created_days": 300,
                "trial_paid_days": 150,
            }
        ]
    )
    assert "No candidates met the selection rule" in result["content"]


async def test_duplicate_subscription_ids_are_rejected():
    entries = ordinary() * 2
    entries[1] = {**entries[1], "email": "second@other.io", "subscription_id": "sub_shared"}
    entries[0] = {**entries[0], "subscription_id": "sub_shared"}
    with pytest.raises(ValueError, match="same subscription twice"):
        await run(entries)


async def test_close_plan_continuation_is_one_customer_chain():
    first = {
        "customer_id": "cus_continuity_shared",
        "email": "owner@continuity.io",
        "start_days": 300,
        "created_days": 300,
        "status": "canceled",
        "ended_days_ago": 100,
    }
    second = {
        "customer_id": "cus_continuity_shared",
        "email": "owner@continuity.io",
        "start_days": 97,
        "created_days": 97,
        "status": "active",
    }
    result, _, context = await run([first, second])
    candidate = context.model_requests[0]["data"]["candidates"][0]
    assert candidate["subscription_count"] == 2
    assert candidate["paid_tenure_days"] >= 180
    assert len(context.model_requests[0]["data"]["candidates"]) == 1
    assert "continuity.io" in result["content"]


async def test_personal_domain_is_filtered():
    result, _, context = await run(
        [{"email": "someone@gmail.com", "start_days": 300, "created_days": 300}]
    )
    assert "No eligible candidates found" in result["content"]
    assert not context.model_requests


async def test_excluded_domain_is_filtered_before_candidate_creation():
    result, _, context = await run(
        ordinary(), inputs={"exclude_domains": ["customer1.io"]}
    )
    assert "No eligible candidates found" in result["content"]
    assert not context.model_requests


async def test_names_emails_ids_domains_and_metadata_never_reach_model():
    result, _, context = await run(
        [
            {
                "email": "private.person@customer1.io",
                "name": "Private Full Name",
                "start_days": 300,
                "created_days": 300,
            }
        ]
    )
    payload = json.dumps(context.model_requests[0]["data"])
    for forbidden in (
        "private.person@customer1.io",
        "Private Full Name",
        "cus_private_00001",
        "sub_private_00001",
        MODEL_PII,
        "customer1.io",
    ):
        assert forbidden not in payload
    assert "Private Full Name" not in result["content"]
    assert "private.person@customer1.io" not in result["content"]
    assert "cus_private_00001" not in result["content"]
    assert "sub_private_00001" not in result["content"]
    assert MODEL_PII not in result["content"]


async def test_model_accepts_valid_candidate_question_and_angle_ids():
    valid = {
        "briefs": [
            {
                "candidate_index": 1,
                "question_ids": ["evidence_source"],
                "angle_ids": ["measurable_change"],
            }
        ]
    }
    result, _, _ = await run(ordinary(), model_output=valid)
    assert "What records or other evidence could verify" in result["content"]
    assert "Test whether the customer can verify a measurable change" in result["content"]
    assert "unusable model result" not in result["content"]


@pytest.mark.parametrize(
    "bad",
    [
        {
            "briefs": [
                {
                    "candidate_index": 99,
                    "question_ids": ["before_state"],
                    "angle_ids": ["before_after"],
                }
            ]
        },
        {
            "briefs": [
                {
                    "candidate_index": 1,
                    "question_ids": ["invented"],
                    "angle_ids": ["before_after"],
                }
            ]
        },
        {
            "briefs": [
                {
                    "candidate_index": 1,
                    "question_ids": ["before_state", "before_state"],
                    "angle_ids": ["before_after"],
                }
            ]
        },
        {
            "briefs": [
                {
                    "candidate_index": 1,
                    "question_ids": ["before_state"],
                    "angle_ids": ["before_after", "before_after"],
                }
            ]
        },
        {
            "briefs": [
                {
                    "candidate_index": 1,
                    "question_ids": ["before_state"],
                    "angle_ids": ["invented"],
                }
            ]
        },
        {
            "briefs": [
                {
                    "candidate_index": 1,
                    "question_ids": ["before_state"],
                    "angle_ids": ["before_after"],
                    "quote": "invented",
                }
            ]
        },
        {"not_briefs": []},
        None,
    ],
    ids=[
        "unknown-candidate",
        "unknown-question",
        "duplicate-question",
        "duplicate-angle",
        "unknown-angle",
        "extra-field",
        "wrong-root",
        "malformed",
    ],
)
async def test_unusable_model_result_falls_back_only_to_fixed_templates(bad):
    result, _, _ = await run(ordinary(), model_output=bad)
    assert "unusable model result; deterministic question templates used" in result["content"]
    assert "Before using the product" in result["content"]
    assert "Permission to request" in result["content"]
    assert "invented" not in result["content"]


async def test_model_provider_failure_propagates():
    with pytest.raises(ValueError, match="synthetic model refusal"):
        await run(ordinary(), model_error=ValueError("synthetic model refusal"))


@pytest.mark.parametrize(
    "code",
    [
        "model_access_revoked",
        "model_spending_stopped",
        "model_unavailable",
        "model_result_unconfirmed",
        "model_call_limit",
    ],
)
async def test_model_authorization_spending_and_service_errors_propagate(code):
    with pytest.raises(CodeModelError) as error:
        await run(ordinary(), model_error=CodeModelError(code))
    assert error.value.code == code


async def test_invalid_model_output_error_uses_safe_template_fallback():
    result, _, _ = await run(
        ordinary(), model_error=CodeModelError("model_output_invalid")
    )
    assert "unusable model result; deterministic question templates used" in result["content"]


async def test_pagination_continues_using_stable_steps_and_cursors():
    entries = [
        {"email": f"owner@customer{n}.io", "start_days": 300, "created_days": 300}
        for n in range(160)
    ]
    result, binding, _ = await run(entries)
    assert len(binding.calls) > 1
    assert binding.calls[0]["step"] == "page_1"
    for current, following in zip(binding.calls, binding.calls[1:]):
        assert following["step"] == f"page_{int(current['step'].split('_')[1]) + 1}"
        assert following["arguments"]["cursor"] == current["response"]["next_cursor"]
    assert "fitted=" in result["content"]


async def test_eight_call_limit_is_explicitly_reported():
    entries = [
        {"email": f"owner@customer{n}.io", "start_days": 300, "created_days": 300}
        for n in range(1200)
    ]
    result, binding, _ = await run(entries)
    assert len(binding.calls) == MODULE.MAX_CALLS
    assert "Coverage: INCOMPLETE" in result["content"]
    assert "eight-call limit was reached" in result["content"]


async def test_malformed_stripe_page_fails_instead_of_becoming_diagnostic():
    class BadPage(FakeStripeConnection):
        async def call(self, **kwargs):
            response = await super().call(**kwargs)
            response.pop("records")
            return response

    binding = BadPage(dataset(ordinary()), service="stripe")
    with pytest.raises(ValueError, match="did not return a subscription page"):
        await run(ordinary(), binding=binding)


@pytest.mark.parametrize("refusal", ["permission_denied", "authentication_failed", "rate_limited"])
async def test_known_stripe_refusals_produce_clear_diagnostics(refusal):
    binding = stripe(ordinary(), refuse={"page_1": refusal})
    result, _, context = await run(ordinary(), binding=binding)
    assert "Stripe connection needs attention" in result["content"]
    assert "No customer evidence was computed" in result["content"]
    assert not context.model_requests


async def test_test_mode_is_never_presented_as_real_customer_evidence():
    result, _, _ = await run(ordinary(), livemode=False)
    assert "TEST MODE — these records are not real customer evidence" in result["content"]


async def test_live_mode_is_labeled_without_exposing_customer_identity():
    result, binding, _ = await run(ordinary(), livemode=True)
    assert binding.calls[0]["response"]["livemode"] is True
    assert "Stripe mode: live mode" in result["content"]
    assert "Private Customer 1" not in result["content"]
    assert "owner@customer1.io" not in result["content"]


async def test_qualification_cases_pass_on_their_synthetic_runs():
    raw = REPOSITORY_ROOT / "workflow_evals" / "content.customer_proof" / "qualification.json"
    qualification = Qualification.model_validate_json(raw.read_text())
    # The qualification CLI evaluates live model calls and cannot inject fake model responses;
    # this maps its cases onto the offline fake and validates the declared output assertions.
    ordinary_entries = ordinary()
    trial_entries = [
        {
            "email": "owner@trial.io",
            "start_days": 300,
            "created_days": 300,
            "trial_paid_days": 150,
        }
    ]
    filtered_entries = [
        {"email": "owner@gmail.com", "start_days": 300, "created_days": 300},
        {"email": "owner@internal.example", "start_days": 300, "created_days": 300},
    ]
    fixture_map = {
        "ordinary": (ordinary_entries, {}, False),
        "trial_boundary": (trial_entries, {}, False),
        "filtered_candidates": (filtered_entries, {"exclude_domains": ["internal.example"]}, False),
        "incomplete_read": (
            [
                {"email": f"owner@customer{n}.io", "start_days": 300, "created_days": 300}
                for n in range(1200)
            ],
            {},
            False,
        ),
        "unusable_model_result": (ordinary_entries, {}, False),
    }
    assert {case.id for case in qualification.cases} == set(fixture_map)
    for case in qualification.cases:
        entries, extra, _ = fixture_map[case.id]
        output, _, _ = await run(entries, inputs={**case.inputs, **extra})
        assessed = assess_output(case, status="succeeded", content=output["content"].encode())
        assert assessed["status"] == "passed", (case.id, assessed["checks"])


def test_output_schema_and_route_have_bounded_closed_contracts():
    route = DEFINITION["code"]["model_routes"]["brief"]
    assert route["max_calls"] == 1
    assert MODULE.MODEL_SCHEMA["additionalProperties"] is False
    item_schema = MODULE.MODEL_SCHEMA["properties"]["briefs"]["items"]
    assert item_schema["additionalProperties"] is False
    assert item_schema["properties"]["question_ids"]["items"]["enum"] == list(MODULE.QUESTIONS)
    assert item_schema["properties"]["angle_ids"]["items"]["enum"] == list(MODULE.ANGLES)
    assert "uniqueItems" not in item_schema["properties"]["question_ids"]
    assert "uniqueItems" not in item_schema["properties"]["angle_ids"]
