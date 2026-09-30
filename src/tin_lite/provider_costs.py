"""Advisory connected-provider costs, separate from Tin's credit ledger."""

import re
from urllib.parse import urlsplit


def validate_provider_cost(value):
    if not isinstance(value, dict) or set(value) != {"estimated_usd", "basis", "pricing_url"}:
        raise ValueError("provider_cost needs estimated_usd, basis and pricing_url")
    amount, basis, url = (value[key] for key in ("estimated_usd", "basis", "pricing_url"))
    if not isinstance(amount, str) or not re.fullmatch(r"\d{1,7}(?:\.\d{1,6})?", amount):
        raise ValueError("provider_cost estimated_usd must be a nonnegative USD decimal string")
    if not isinstance(basis, str) or not 1 <= len(basis.strip()) <= 400:
        raise ValueError(
            "provider_cost basis must explain the per-run estimate in 1-400 characters"
        )
    if not isinstance(url, str) or len(url) > 1000:
        raise ValueError("provider_cost pricing_url must be an HTTPS pricing reference")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid or any(char.isspace() for char in url):
        raise ValueError("provider_cost pricing_url must be an HTTPS pricing reference")


def provider_costs(definition, bindings):
    from tin_lite.integrations import registered_integrations

    names = {item.key: item.name for item in registered_integrations()}
    services = definition.get("code", definition.get("procedure", {})).get("services", {})
    result = []
    for binding in bindings:
        item = {
            "provider_key": binding.provider_key,
            "provider_name": names.get(
                binding.provider_key, binding.name.replace("_", " ").title()
            ),
            "status": "unknown",
            "billed_by": "provider",
        }
        # Google explicitly prices every supported Search Console API call at zero.
        if binding.provider_key == "analytics.gsc":
            item.update(
                status="free",
                estimated_usd="0.00",
                basis="Search Console API requests are free of charge.",
                pricing_url="https://developers.google.com/webmaster-tools/pricing",
            )
        elif hint := services.get(binding.name, {}).get("provider_cost"):
            validate_provider_cost(hint)
            item.update(status="creator_estimate", **hint)
        result.append(item)
    return result
