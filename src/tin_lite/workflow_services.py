"""Shared bounded service bindings for code workflows and Codex procedures."""

import re
from dataclasses import dataclass

from tin_lite.provider_costs import validate_provider_cost

# Capabilities a service binding may declare per first-party provider. A provider added here
# also needs its reviewed operations in code_services.OPERATIONS.
SERVICE_CAPABILITIES = {
    "analytics.gsc": frozenset({"sites.list", "search_analytics.read"}),
    "workspace.google": frozenset({"gmail.messages.read", "calendar.events.read"}),
    "infra.github": frozenset({"repositories.list"}),
    "payments.stripe": frozenset(
        {"subscriptions.read", "customers.read", "invoices.read", "prices.read", "charges.read"}
    ),
    "analytics.posthog": frozenset({"query.read", "definitions.read", "insights.read"}),
    # Services Tin holds the key for (managed_services.py); no founder connection.
    "managed.pagespeed": frozenset({"pagespeed.read", "crux.read"}),
    "managed.dataforseo": frozenset({"serp.read", "keywords.read", "backlinks.read"}),
}


# Runaway guards, well above real use. Validation only: a binding accepted under the older,
# smaller maximums keeps validating unchanged.
MAX_SERVICE_CALLS = 32
MAX_SERVICE_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class ServiceBinding:
    name: str
    provider_key: str
    capabilities: tuple[str, ...]
    max_calls: int
    max_response_bytes: int


def service_bindings(value, requirements, *, allow_optional=False):
    """Validate service bindings against their integration requirements.

    With `allow_optional` (code workflows), a first-party connection may be optional: the run
    starts without it, and the code reads its state from ctx["connections"] (code_services).
    Managed services and custom API connections stay required.
    """
    from tin_lite import managed_services
    from tin_lite.integrations import parse_integration_requirements
    from tin_lite.project_connections import CUSTOM_KEY

    allowed = SERVICE_CAPABILITIES
    parsed = {r.provider_key: r for r in parse_integration_requirements(requirements)}
    if not isinstance(value, dict) or len(value) > 4:
        raise ValueError("declare at most four services")
    result = []
    for name, entry in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name)
            or not isinstance(entry, dict)
            or set(entry) - {"provider_cost"} != {"provider_key", "max_calls", "max_response_bytes"}
        ):
            raise ValueError("invalid service binding")
        if "provider_cost" in entry:
            validate_provider_cost(entry["provider_cost"])
        provider = entry["provider_key"]
        requirement = parsed.get(provider) if isinstance(provider, str) else None
        optional = (
            requirement is not None
            and not requirement.required
            and allow_optional
            and not CUSTOM_KEY.fullmatch(provider)
            and not managed_services.is_managed(provider)
        )
        if (
            requirement is None
            or not (requirement.required or optional)
            or not set(requirement.capabilities)
            <= (
                {"http.read", "http.write"}
                if CUSTOM_KEY.fullmatch(provider)
                else allowed.get(provider, set())
            )
        ):
            raise ValueError("service needs explicit supported required integration capabilities")
        for field, low, high in (
            ("max_calls", 1, MAX_SERVICE_CALLS),
            ("max_response_bytes", 1024, MAX_SERVICE_RESPONSE_BYTES),
        ):
            if type(entry[field]) is not int or not low <= entry[field] <= high:
                raise ValueError(f"service {field} must be {low}-{high}")
        result.append(
            ServiceBinding(
                name,
                provider,
                requirement.capabilities,
                entry["max_calls"],
                entry["max_response_bytes"],
            )
        )
    if (
        {s.provider_key for s in result} != set(parsed)
        or len({s.provider_key for s in result}) != len(result)
        or sum(s.max_calls for s in result) > MAX_SERVICE_CALLS
    ):
        raise ValueError(
            "each dependency needs one binding; "
            f"at most {MAX_SERVICE_CALLS} service calls are allowed"
        )
    return tuple(result)
