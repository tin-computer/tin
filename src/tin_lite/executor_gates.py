"""Start-time readiness gates for provider-backed executors.

The run service raises these reasons when a start is refused; the growth onboarding plan
reads the same reasons so its appendix never sends an agent to a door that is locked.
"""

from __future__ import annotations

from typing import Any

from tin_lite.keyword_plan_v6 import POLICY as KEYWORD_POLICY
from tin_lite.organic_system import INPUT_SCHEMA as ORGANIC_SYSTEM_INPUTS

# The current keyword policy's floor: it reserves $1.65 at most, so $2 never refuses a call.
KEYWORD_MINIMUM_USD = float(KEYWORD_POLICY["minimum_ceiling_usd"])
KEYWORD_DEFAULT_USD = ORGANIC_SYSTEM_INPUTS["properties"]["keyword_max_cost_usd"]["default"]


def organic_audit_gate(settings: Any) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or getattr(settings, "organic_audit_max_cost_usd", 0) < 0.05
    ):
        return (
            "Organic audit requires a configured DataForSEO account and an explicit spending limit."
        )
    return None


def keyword_plan_gate(settings: Any) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or not getattr(settings, "luna_api_key", None)
        or getattr(settings, "keyword_plan_max_cost_usd", 0) < KEYWORD_MINIMUM_USD
    ):
        return (
            "Keyword planning requires DataForSEO, the native model, "
            f"and an enabled spending ceiling of at least ${KEYWORD_MINIMUM_USD:g}."
        )
    return None


def paid_ads_gate(settings: Any) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or not getattr(settings, "luna_api_key", None)
        or not getattr(settings, "gak_url", None)
        or not getattr(settings, "gak_token", None)
        or getattr(settings, "paid_ads_max_cost_usd", 0) < 3
    ):
        return (
            "Paid ads assessment requires DataForSEO, the Keyword Planner service, the native "
            "model and an enabled ceiling of at least $3."
        )
    return None


def organic_system_gate(
    settings: Any, *, keyword_max_cost_usd: float = KEYWORD_DEFAULT_USD
) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or not getattr(settings, "luna_api_key", None)
        or getattr(settings, "organic_audit_max_cost_usd", 0) < 0.05
        or getattr(settings, "keyword_plan_max_cost_usd", 0) < keyword_max_cost_usd
        or getattr(settings, "content_plan_max_cost_usd", 0) < 1
    ):
        return (
            "Enable audit, keyword research and the $1 content-planning allowance first. "
            "No child workflow was started."
        )
    return None


def google_ads_gate(settings: Any) -> str | None:
    from tin_lite.google_ads import manager_oauth_client

    client_id, client_secret = manager_oauth_client(settings)
    if (
        not getattr(settings, "google_ads_manager_customer_id", None)
        or not getattr(settings, "google_ads_manager_refresh_token", None)
        or not client_id
        or client_secret is None
        or not getattr(settings, "luna_api_key", None)
    ):
        return (
            "Google Ads workflows require Tin's manager account credentials, the Google OAuth "
            "client and the native model."
        )
    return None
