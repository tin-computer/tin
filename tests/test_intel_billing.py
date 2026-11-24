"""Daily intelligence's billing: parents buy nothing themselves; their judges are session-funded."""

from __future__ import annotations

from tin_lite import codex_api_pricing
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.service_pricing import NANOS_PER_DOLLAR, PARENT_EXECUTORS, service_terms


def definition(key):
    return next(b for b in BUILTIN_WORKFLOWS if b.key == key).definition


def test_the_world_parent_is_a_fifteen_dollar_spending_parent():
    terms = service_terms(definition("intel.world"))
    assert terms["kind"] == "parent" and terms["operations"] == []
    assert terms["maximum_nanos"] == 15 * NANOS_PER_DOLLAR
    assert "external_api_funding" not in terms
    assert {"intel.world", "intel.daily"} <= PARENT_EXECUTORS


def test_the_client_parent_reads_x_on_operator_credits():
    terms = service_terms({"executor": "intel.daily", "key": "intel.daily"})
    assert terms["external_api_funding"] == "operator_x_credits"
    assert terms["maximum_nanos"] == 15 * NANOS_PER_DOLLAR


def test_both_judges_have_a_fifteen_dollar_session_ceiling():
    for key in ("intel.world_judge", "intel.client_judge"):
        assert codex_api_pricing.PROCEDURE_KEY_MAXIMUMS[key] == 15 * NANOS_PER_DOLLAR
