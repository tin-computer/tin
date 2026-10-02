"""Offline checks for failed-payment recovery using the Stripe connection fake."""

import json
import runpy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from connection_fakes import FakeStripeConnection

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "retention.payment_recovery"
FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "payment_recovery" / "stripe_objects.json"
NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


MODULE, DEFINITION = package()


class Context(dict):
    def __init__(self, stripe, generate):
        super().__init__(
            run_id="00000000-0000-4000-8000-000000000099",
            created_at=NOW.isoformat(),
        )
        self.services = stripe
        self.models = SimpleNamespace(generate=generate)


def fixture_stripe():
    objects = json.loads(FIXTURE.read_text())
    return FakeStripeConnection(
        objects,
        service="stripe",
        max_calls=8,
        max_response_bytes=64000,
        livemode=False,
    )


async def test_failed_charges_are_batched_into_validated_review_drafts():
    stripe = fixture_stripe()
    requests = []

    async def generate(**payload):
        requests.append(payload)
        assert payload["route"] == "draft_email"
        assert payload["step"] == "draft_recovery_emails"
        records = payload["data"]
        assert all("email" not in row for row in records)
        return {
            "parsed": {
                "emails": [
                    {
                        "customer_id": row["customer_id"],
                        "subject": "A quick payment update",
                        "body": (
                            f"Hi {row['name'].split()[0]}, would you mind updating your payment "
                            "method?"
                        ),
                    }
                    for row in records
                ]
            },
            "text": "fixture response",
        }

    result = await MODULE.run(
        Context(stripe, generate),
        {"project_id": "00000000-0000-4000-8000-000000000001", "lookback_days": 7},
    )
    validate_code_result(json.dumps(result).encode(), validate_code_definition(DEFINITION))
    assert result["path"] == "outreach/payment_recovery/RECOVERY_QUEUE.md"
    assert "Alex Example" in result["content"] and "Sam Example" in result["content"]
    assert "card_declined" in result["content"] and "expired_card" in result["content"]
    assert "test-mode data: do not send" in result["content"]
    assert len(requests) == 1
    assert [call["operation"] for call in stripe.calls] == [
        "charges.list",
        "customers.list",
    ]


async def test_plausible_model_result_with_missing_customer_id_is_rejected():
    async def generate(**_payload):
        return {
            "parsed": {
                "emails": [
                    {"customer_id": "cus_missing", "subject": "Hello", "body": "Please update."}
                ]
            },
            "text": "fixture response",
        }

    with pytest.raises(ValueError, match="every selected customer exactly once"):
        await MODULE.run(
            Context(fixture_stripe(), generate),
            {"project_id": "00000000-0000-4000-8000-000000000001"},
        )
