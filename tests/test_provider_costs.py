from copy import deepcopy
from dataclasses import asdict

import pytest

from tin_lite.provider_costs import provider_costs
from tin_lite.workflow_services import service_bindings

HINT = {
    "estimated_usd": "0.03",
    "basis": "Three requests at $0.01 each on the standard plan.",
    "pricing_url": "https://provider.example/pricing",
}


def contract(provider="custom.api.crm", hint=None):
    binding = {"provider_key": provider, "max_calls": 3, "max_response_bytes": 8000}
    if hint is not None:
        binding["provider_cost"] = hint
    requirements = [
        {
            "provider_key": provider,
            "capabilities": [
                "search_analytics.read" if provider == "analytics.gsc" else "http.read"
            ],
            "required": True,
        }
    ]
    services = {"crm": binding}
    return {"code": {"services": services}}, service_bindings(services, requirements)


def test_creator_cost_is_advisory_and_does_not_change_runtime_binding_receipts():
    definition, bindings = contract(hint=HINT)
    _, original = contract()
    assert asdict(bindings[0]) == asdict(original[0])
    assert provider_costs(definition, bindings)[0] == {
        "provider_key": "custom.api.crm",
        "provider_name": "Crm",
        "status": "creator_estimate",
        "billed_by": "provider",
        **HINT,
    }


def test_known_free_provider_is_not_labeled_unknown_or_overridden_by_creator():
    definition, bindings = contract("analytics.gsc", HINT)
    result = provider_costs(definition, bindings)[0]
    assert result["status"] == "free" and result["estimated_usd"] == "0.00"
    assert result["pricing_url"] == "https://developers.google.com/webmaster-tools/pricing"


def test_unknown_cost_is_not_zero():
    definition, bindings = contract()
    result = provider_costs(definition, bindings)[0]
    assert result["status"] == "unknown"
    assert "estimated_usd" not in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_usd", "-1"),
        ("estimated_usd", "NaN"),
        ("estimated_usd", 0.03),
        ("basis", " "),
        ("basis", "x" * 401),
        ("pricing_url", "javascript:alert(1)"),
        ("pricing_url", "https://user:secret@example.com"),
    ],
)
def test_invalid_author_cost_metadata_is_rejected_at_validation(field, value):
    hint = deepcopy(HINT)
    hint[field] = value
    with pytest.raises(ValueError, match="provider_cost"):
        contract(hint=hint)
