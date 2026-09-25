"""Paid ads assessment: contract pin, scorer calibration, pure validators and the orchestrated
sequence with a fake model. No providers, no database."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tin_lite import paid_ads
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.keyword_plan import keyword_id
from tin_lite.paid_ads_assets import score as scorer
from tin_lite.workflow_inputs import validate_input_schema

FIXTURES = Path(__file__).parent / "fixtures" / "paid_ads"
RUN_ID = "00000000-0000-4000-8000-00000000abcd"
PROJECT_ID = "00000000-0000-4000-8000-00000000dcba"


def test_definition_pins_the_contract_and_the_assets_stay_consistent():
    registry = {item.key: item for item in BUILTIN_WORKFLOWS}
    definition, files = registry[paid_ads.KEY].definition_and_resource_files()
    assert definition["executor"] == paid_ads.KEY and not files
    assert definition["system"] == "paid-ads" and "agent_only" not in definition
    assert definition["schedule_modes"] == ["on_demand"] and "human_review" not in definition
    assert definition["paid_ads_policy"] == paid_ads.POLICY
    assert definition["paid_ads_contract_sha256"] == paid_ads.contract_digest()
    assert [(r["provider"], r["model"]) for r in definition["paid_ads_routes"]] == [
        ("openai", "gpt-6-sol"),
        ("openai", "gpt-6-luna"),
    ]
    properties = definition["input_schema"]["properties"]
    for item in definition["prerequisites"]:
        assert item["level"] == "recommended" and item["via_input"] in properties
    assert all(not r["required"] for r in definition["integration_requirements"])
    validate_input_schema(definition["input_schema"])
    for section in (
        paid_ads.WRITING,
        paid_ads.PROFILE_RULES,
        paid_ads.SEEDS_RULES,
        paid_ads.CLASSIFY_RULES,
        paid_ads.ECONOMICS_RULES,
        paid_ads.DIAGNOSE_RULES,
        paid_ads.VERDICT_RULES,
        paid_ads.CAMPAIGN_RULES,
        paid_ads.OUTPUT_RULES,
    ):
        assert len(section) > 200
    rule_ids = {rule["id"] for rule in paid_ads.RUBRIC["diagnose_rules"]}
    assert rule_ids and all(rule in paid_ads.DIAGNOSE_RULES for rule in rule_ids)
    assert set(paid_ads.BENCHMARKS["industries"]) == set(paid_ads.RUBRIC["industries"])


@pytest.mark.parametrize(
    "bad",
    [
        {"market": "DE"},
        {"product_url": "http://example.com"},
        {"product_url": "https://example.com", "competitor_domains": ["example.com"]},
        {"price_point": -1},
        {"ads_purchases": 5, "ads_clicks": 2},
        {"max_cost_usd": 2},
        {"keyword_run_id": "not-a-uuid"},
        {"competitor_domains": [f"c{i}.example" for i in range(7)]},
    ],
)
def test_check_inputs_rejects_what_the_schema_cannot_express(bad):
    with pytest.raises(ValueError):
        paid_ads.check_inputs({"project_id": PROJECT_ID, "product_url": "https://a.example", **bad})
    paid_ads.check_inputs({"project_id": PROJECT_ID, "product_url": "https://a.example"})


def test_gate_names_the_reason_or_lets_the_assessment_run():
    base = {"product_url": "https://a.example", "conversion_event": "free_signup"}
    assert paid_ads.gate({**base, "hard_nos": ["no_paid_ads"]}, "ok")["reason"] == "no_paid_ads"
    assert paid_ads.gate({**base, "product_url": ""}, None)["reason"] == "no_site"
    assert paid_ads.gate(base, "none")["reason"] == "no_site"
    assert paid_ads.gate(base, "blocked")["reason"] == "no_site"
    assert paid_ads.gate(base, "thin") is None
    assert (
        paid_ads.gate({**base, "conversion_event": "none"}, "ok")["reason"] == "no_conversion_event"
    )
    assert paid_ads.gate(base, "ok") is None


def test_tracking_signals_and_level():
    full = paid_ads.tracking_signals(
        '<script src="https://www.googletagmanager.com/gtag/js?id=AW-1234567"></script>'
    )
    assert full["gtag"] and full["aw_conversion"]
    assert paid_ads.tracking_level([{"signals": full}])[0] == "full"
    partial = paid_ads.tracking_signals("<script>posthog.init('x')</script>")
    assert paid_ads.tracking_level([{"signals": partial}])[0] == "partial"
    assert (
        paid_ads.tracking_level([{"signals": paid_ads.tracking_signals("<p>hi</p>")}])[0] == "none"
    )


def test_scorer_reproduces_the_calibration_scorecards():
    cases = json.loads((FIXTURES / "calibration.json").read_text())["cases"]
    assert {case["id"] for case in cases} == {"tin_geo", "tin_auto", "claw", "ite"}
    for case in cases:
        result = scorer.assess(
            case["profile"], case["volumes"], case["labels"], case["curve"], case["paid_slots"]
        )
        expected = case["expected"]
        assert result["verdict"] == expected["verdict"], case["id"]
        assert round(result["allowable_cpa_customer"]) == expected["allowable_cpa_customer"]
        assert result["headroom"] == expected["headroom"]
        assert result["scores"]["total"] == expected["scores_total"]
        assert (result["estimate"] or {}).get("cpa_customer") == expected["estimate_cpa_customer"]
        low, high = result["ranges"]["daily_budget_usd"].values()
        assert 0 < low <= high
    claw = next(case for case in cases if case["id"] == "claw")
    without_history = {**claw["profile"], "ads_history": "never", "observed": {}}
    result = scorer.assess(without_history, claw["volumes"], claw["labels"], claw["curve"], [])
    assert result["verdict"] == "test" and "captive" in result["binding_constraint"]


def test_verdict_order_is_history_then_economics_then_budget_then_score():
    common = dict(
        profile={"ads_history": "running"},
        cheap_test=False,
        est={"cpa_customer": 100},
        allow_cpa=50,
        min_test=500,
    )
    observed_good = {"headroom": 1.2, "cpa_customer": 40}
    assert (
        scorer.verdict(**common, observed=observed_good, headroom=0.1, budget=10, total=5)[0]
        == "continue"
    )
    stopped = {**common, "profile": {"ads_history": "stopped"}}
    assert (
        scorer.verdict(**stopped, observed=observed_good, headroom=0.1, budget=10, total=5)[0]
        == "restart"
    )
    assert (
        scorer.verdict(
            **common,
            observed={"headroom": 0.5, "cpa_customer": 100},
            headroom=2,
            budget=1000,
            total=90,
        )[0]
        == "restructure"
    )
    assert (
        scorer.verdict(
            **stopped,
            observed={"headroom": 0.5, "cpa_customer": 100},
            headroom=2,
            budget=1000,
            total=90,
        )[0]
        == "do_not_restart"
    )
    assert (
        scorer.verdict(
            **{**common, "cheap_test": True}, observed=None, headroom=0.2, budget=150, total=40
        )[0]
        == "test"
    )
    assert (
        scorer.verdict(**common, observed=None, headroom=0.5, budget=5000, total=90)[0] == "not_now"
    )
    assert scorer.verdict(**common, observed=None, headroom=2, budget=100, total=90)[0] == "not_now"
    assert (
        scorer.verdict(**common, observed=None, headroom=2, budget=1000, total=40)[0] == "not_now"
    )
    assert scorer.verdict(**common, observed=None, headroom=2, budget=1000, total=80)[0] == "go"
    assert scorer.verdict(**common, observed=None, headroom=1.2, budget=1000, total=80)[0] == "test"


def test_allowable_follows_billing_and_falls_back_when_price_is_unknown():
    monthly, note = scorer.allowable(
        {"industry": "b2b_saas_smb", "price": 49, "billing": "monthly", "gross_margin": 0.6}
    )
    assert round(monthly, 1) == 78.4 and note["rule"].startswith("min(")
    annual, _ = scorer.allowable(
        {"industry": "b2b_saas_smb", "price": 588, "billing": "annual", "gross_margin": 0.6}
    )
    assert round(annual, 1) == 78.4
    once, note = scorer.allowable(
        {
            "industry": "consumer_software",
            "price": 8,
            "billing": "one_time",
            "gross_margin": 0.7,
            "repeat_factor": 1.3,
        }
    )
    assert round(once, 2) == 7.28 and "first-purchase" in note["rule"]
    fallback, note = scorer.allowable(
        {"industry": "dev_tools", "price": None, "billing": "monthly", "gross_margin": 0.6}
    )
    assert fallback == 60 and note["confidence"] == "low"


def volume_items():
    rows = [
        ("imessage api", 720, 61, 10.0, 30.5),
        ("openclaw imessage", 590, 2, 0.0, 0.0),
        ("imessage for business", 320, 58, 8.4, 36.0),
        ("imessage bot", 90, 25, 4.0, 10.1),
        ("sendblue alternative", 50, 68, 10.1, 58.2),
        ("imessage on android", 15000, 5, 0.5, 2.0),
    ]
    return [
        {
            "keyword": k,
            "avg_monthly_searches": v,
            "competition_index": c,
            "low_top_of_page_bid_micros": int(lo * 1e6),
            "high_top_of_page_bid_micros": int(hi * 1e6),
            "monthly_search_volumes": [
                {"year": 2026, "month": m, "searches": v} for m in range(1, 13)
            ],
        }
        for k, v, c, lo, hi in rows
    ]


def test_keyword_table_merges_sources_with_stable_ids():
    overview = [
        {
            "keyword": "imessage api",
            "keyword_info": {"cpc": 27.8},
            "search_intent_info": {"main_intent": "informational"},
        }
    ]
    gsc = [
        {"id": keyword_id("imessage api", "US"), "clicks": 68, "impressions": 2689, "position": 7.0}
    ]
    table = paid_ads.keyword_table(
        market="US",
        volume_items=volume_items(),
        overview_items=overview,
        gsc_rows=gsc,
        cluster_items=None,
        keyword_source={},
    )
    assert [row["keyword"] for row in table][:2] == ["imessage on android", "imessage api"]
    api = next(row for row in table if row["keyword"] == "imessage api")
    assert api["id"] == keyword_id("imessage api", "US") and api["cpc"] == 27.8
    assert api["gsc_clicks"] == 68 and api["provider_intent"] == "informational"
    assert api["high_bid"] == 30.5 and len(api["trend"]) == 12
    assert paid_ads.bid_levels(table) == [6.1, 15.25, 30.5]


def test_classification_must_cover_every_keyword_once():
    ids = ["a", "b"]
    with pytest.raises(paid_ads.UnusableModelResult):
        paid_ads.validate_classification(
            {"labels": [{"id": "a", "intent": "bofu", "relevance": 3}]}, ids
        )
    with pytest.raises(paid_ads.UnusableModelResult):
        paid_ads.validate_classification(
            {
                "labels": [
                    {"id": "a", "intent": "bofu", "relevance": 3},
                    {"id": "a", "intent": "tofu", "relevance": 1},
                    {"id": "b", "intent": "mofu", "relevance": 2},
                ]
            },
            ids,
        )
    labels = paid_ads.validate_classification(
        {
            "labels": [
                {"id": "a", "intent": "bofu", "relevance": 3},
                {"id": "b", "intent": "tofu", "relevance": 0},
            ]
        },
        ids,
    )
    assert labels == {"a": ("bofu", 3), "b": ("tofu", 0)}


def checks():
    return dict(
        ranges={
            "daily_budget_usd": {"min": 10, "max": 30},
            "target_cpa_usd": {"min": 0, "max": 30},
            "monthly_budget_usd": {"min": 300, "max": 900},
        },
        decision="test",
        cluster_ids={"c1"},
        keyword_ids={"k1", "k2"},
        evidence_ids={"E-form"},
        landing_pages={"https://a.example/pricing"},
    )


def good_verdict():
    return {
        "decision": "test",
        "platform": "google_search",
        "binding_constraint": "x",
        "reasons": [{"text": "a", "evidence": ["E-form"]}, {"text": "b", "evidence": []}],
        "campaign": {
            "ad_groups": [
                {"name": "Buyer", "cluster_id": "c1", "keyword_ids": ["k1"], "match_type": "exact"}
            ],
            "negatives": ["free"],
            "geo": "US",
            "daily_budget_usd": {"min": 10, "max": 20},
            "target_cpa_usd": 25,
            "landing_page": "https://a.example/pricing",
        },
        "fix_before_spend": [],
        "confidence": "medium",
        "founder_words": "Run a small test.",
    }


def test_verdict_validation_rejects_out_of_range_values_and_clamp_repairs_them():
    assert paid_ads.validate_verdict(good_verdict(), **checks()) == []
    broken = good_verdict()
    broken["campaign"]["daily_budget_usd"] = {"min": 5, "max": 90}
    broken["campaign"]["target_cpa_usd"] = 999
    broken["campaign"]["landing_page"] = "https://elsewhere.example/"
    broken["reasons"][0]["evidence"] = ["E-made-up"]
    broken["platform"] = "neither"
    problems = paid_ads.validate_verdict(broken, **checks())
    assert {
        "daily budget is outside the allowed range",
        "target CPA exceeds the allowable cost per customer",
        "landing page is not a readable page",
        "a reason cites an unknown evidence id",
    } <= set(problems)
    fixed, notes = paid_ads.clamp_verdict(
        broken,
        ranges=checks()["ranges"],
        decision="test",
        landing_pages=["https://a.example/pricing"],
        evidence_ids={"E-form"},
    )
    assert fixed["platform"] == "neither" and fixed["campaign"] is None and notes
    broken["platform"] = "google_search"
    fixed, notes = paid_ads.clamp_verdict(
        broken,
        ranges=checks()["ranges"],
        decision="test",
        landing_pages=["https://a.example/pricing"],
        evidence_ids={"E-form"},
    )
    assert paid_ads.validate_verdict(fixed, **checks()) == []
    assert fixed["campaign"]["daily_budget_usd"] == {"min": 10, "max": 30}
    assert fixed["campaign"]["target_cpa_usd"] == 30 and fixed["reasons"][0]["evidence"] == []


class FakeModel:
    """Answers each step from its schema and prompt, the way a well-behaved model would."""

    INTENT = {
        "imessage api": ("bofu", 3),
        "openclaw imessage": ("captive", 3),
        "imessage for business": ("mofu", 2),
        "imessage bot": ("mofu", 2),
        "sendblue alternative": ("brand_competitor", 2),
        "imessage on android": ("irrelevant", 0),
    }

    def __init__(self, *, unusable=(), overrides=None):
        self.calls, self.unusable, self.overrides = [], list(unusable), overrides or {}

    async def __call__(self, step, system, user, schema, max_out, effort):
        self.calls.append(step)
        if step in self.unusable:
            self.unusable.remove(step)
            raise paid_ads.UnusableModelResult("response was truncated")
        base = step.removesuffix(":retry").split(":")[0]
        result = getattr(self, "_" + base)(user, schema)
        return self.overrides.get(base, lambda value, user: value)(result, user)

    def _classify(self, user, schema):
        rows = json.loads(user.split("KEYWORDS:\n", 1)[1])
        return {
            "labels": [
                {
                    "id": row["id"],
                    "intent": self.INTENT[row["keyword"]][0],
                    "relevance": self.INTENT[row["keyword"]][1],
                }
                for row in rows
            ]
        }

    def _diagnose(self, user, schema):
        evidence = schema["properties"]["likely_reasons"]["items"]["properties"]["evidence"][
            "items"
        ]["enum"]
        return {
            "summary": "Exact match on ecosystem terms converted; broad did not.",
            "likely_reasons": [
                {"rule": "R-BROAD-MATCH", "reason": "broad clicks", "evidence": evidence[:1]}
            ],
        }

    def _verdict(self, user, schema):
        props = schema["properties"]
        decision = props["decision"]["enum"][0]
        platforms = props["platform"]["enum"]
        ranges = json.loads(
            user.split("RANGES YOU MUST STAY INSIDE:\n", 1)[1].split("\n\nPROFILE:", 1)[0]
        )
        evidence = props["reasons"]["items"]["properties"]["evidence"]["items"]["enum"]
        campaign_schema = props["campaign"]["anyOf"][0]["properties"]
        campaign = None
        if "google_search" in platforms:
            clusters = campaign_schema["ad_groups"]["items"]["properties"]["cluster_id"]["enum"]
            keywords = campaign_schema["ad_groups"]["items"]["properties"]["keyword_ids"]["items"][
                "enum"
            ]
            campaign = {
                "ad_groups": [
                    {
                        "name": "Buyer terms",
                        "cluster_id": clusters[0],
                        "keyword_ids": keywords[:2],
                        "match_type": "exact",
                    }
                ],
                "negatives": ["free", "android"],
                "geo": "United States",
                "daily_budget_usd": {
                    "min": ranges["daily_budget_usd"]["min"],
                    "max": ranges["daily_budget_usd"]["max"],
                },
                "target_cpa_usd": ranges["target_cpa_usd"]["max"],
                "landing_page": campaign_schema["landing_page"]["enum"][0],
            }
        return {
            "decision": decision,
            "platform": "google_search" if campaign else "neither",
            "binding_constraint": "Demand is small; test exact match only.",
            "reasons": [
                {"text": "Ecosystem terms convert.", "evidence": evidence[:1]},
                {"text": "Allowable is $30.", "evidence": ["E-scorecard"]},
            ],
            "campaign": campaign,
            "fix_before_spend": ["Import the purchase conversion."],
            "confidence": "medium",
            "founder_words": "Run a $150 exact-match test and read it at thirty events.",
        }

    def _repair(self, user, schema):
        payload = json.loads(user)
        verdict = payload["verdict"]
        verdict["campaign"]["daily_budget_usd"] = {
            "min": payload["ranges"]["daily_budget_usd"]["min"],
            "max": payload["ranges"]["daily_budget_usd"]["max"],
        }
        verdict["campaign"]["target_cpa_usd"] = payload["ranges"]["target_cpa_usd"]["max"]
        return {"fixes": [{"slot": "verdict", "value": json.dumps(verdict)}]}


def scope(**overrides):
    inputs = {
        "product_url": "https://www.clawmessenger.com",
        "market": "US",
        "budget": "under_500",
        "hard_nos": [],
        "price_point": 15,
        "billing": "monthly",
        "gross_margin": "software_high",
        "conversion_event": "trial_card_required",
        "ads_history": "running",
        "ads_platforms": ["google_search"],
        "ads_spend_usd": 51,
        "ads_clicks": 26,
        "ads_purchases": 5,
        "ads_soft_conversions": 0,
        "ads_notes": "exact match tests worked",
        "competitor_domains": ["sendblue.co"],
        "notes": "",
        "max_cost_usd": 6,
        **overrides,
    }
    return {
        "run_id": RUN_ID,
        "project_id": PROJECT_ID,
        "definition_sha": "e" * 40,
        "today": "2026-09-22",
        "market": "US",
        "inputs": inputs,
        "url": inputs["product_url"],
        "host": "www.clawmessenger.com",
    }


def gathered():
    return {
        "site": {
            "status": "completed",
            "value": {
                "verdict": "ok",
                "pages": [{"url": "https://www.clawmessenger.com/", "kind": "home", "status": 200}],
                "readable": [
                    "https://www.clawmessenger.com/",
                    "https://www.clawmessenger.com/pricing",
                ],
            },
        },
        "tracking": {
            "status": "completed",
            "value": {"level": "full", "signals": {"aw_conversion": True}},
        },
        "gsc": {"status": "completed", "value": {"rows": [], "returned_rows": 0}},
        "ads_search": {
            "status": "completed",
            "value": {
                "domain": "clawmessenger.com",
                "count": 1,
                "first_shown": "2026-04-14 01:41:32",
                "last_shown": "2026-05-19 12:20:39",
            },
        },
        "upstream": {"status": "completed", "value": {}},
    }


def research():
    table = paid_ads.keyword_table(
        market="US",
        volume_items=volume_items(),
        overview_items=[],
        gsc_rows=[],
        cluster_items=None,
        keyword_source={},
    )
    receipts = {
        "research:dfs:ad_traffic:4.2": {
            "status": "completed",
            "value": {"items": [{"bid": 4.2, "clicks": 5.0, "cost": 13.4, "average_cpc": 2.67}]},
        },
        "research:dfs:ad_traffic:16.8": {
            "status": "completed",
            "value": {"items": [{"bid": 16.8, "clicks": 30.1, "cost": 247.6, "average_cpc": 8.22}]},
        },
        "research:dfs:serp:0": {
            "status": "completed",
            "value": {"keyword": "imessage api", "paid_slots": 0},
        },
        "research:gak:volume": {"status": "completed", "value": {"items_count": 6}},
    }
    profile = {
        "business_name": "Claw Messenger",
        "industry": "dev_tools",
        "buyer_type": "developer",
        "price_band": "low",
        "billing": "monthly",
        "motion": "self_serve",
        "conversion_event": "trial_card_required",
        "visual_fit": 1,
        "offer_clarity": 3,
        "pricing_shown": True,
        "mobile_ok": True,
        "free_step": False,
        "geography": "US",
        "stage": "early_revenue",
        "summary": "iMessage API for agents",
        "unknowns": [],
        "basis": [],
    }
    labels = {row["id"]: list(FakeModel.INTENT[row["keyword"]]) for row in table}
    return {"profile": profile, "keywords": table, "receipts": receipts, "labels": labels}


async def test_build_assessment_renders_four_documents_around_the_computed_verdict():
    model = FakeModel()
    result = await paid_ads.build_assessment(scope(), gathered(), research(), model)
    docs = result["documents"]
    assert set(docs) == set(paid_ads.LIMITS)
    text = docs["ASSESSMENT.md"]
    assert text.count("```") == 2 and "```tin-ads" in text
    block = json.loads(re.search(r"```tin-ads\n(.*?)\n```", text, re.S).group(1))
    assert block["decision"] == "continue" == result["report"]["decision"]
    assert block["platform"] == "google_search" and block["campaign"]["ad_groups"]
    assessment = json.loads(docs["assessment.json"])
    assert assessment["scorecard"]["observed"]["cpa_customer"] == pytest.approx(10.2)
    assert assessment["keywords"][1]["intent"] == "bofu"
    assert "## What your earlier ads say" in text and "broad did not" in text
    assert "E-" not in text.split("```tin-ads")[0].split("## What Tin looked at")[0]
    assert docs["keywords.csv"].count("\n") == 7
    assert model.calls == ["diagnose", "verdict"]
    assert not result["report"]["retried_steps"] and not result["report"]["code_corrections"]


async def test_an_unusable_result_gets_one_replacement_and_two_fail_the_run():
    model = FakeModel(unusable=["diagnose"])
    result = await paid_ads.build_assessment(scope(), gathered(), research(), model)
    assert model.calls[:2] == ["diagnose", "diagnose:retry"]
    assert result["report"]["retried_steps"] == ["diagnose: response was truncated"]
    with pytest.raises(paid_ads.UnusableModelResult):
        await paid_ads.build_assessment(
            scope(), gathered(), research(), FakeModel(unusable=["verdict", "verdict:retry"])
        )


async def test_seed_negative_themes_reach_assessment_json_for_the_launch():
    themes = {**research(), "negative_themes": ["jobs", "free", "android emulator"]}
    result = await paid_ads.build_assessment(scope(), gathered(), themes, FakeModel())
    assessment = json.loads(result["documents"]["assessment.json"])
    assert assessment["negative_themes"] == ["jobs", "free", "android emulator"]
    text = result["documents"]["ASSESSMENT.md"]
    block = json.loads(re.search(r"```tin-ads\n(.*?)\n```", text, re.S).group(1))
    assert "negative_themes" not in block
    result = await paid_ads.build_assessment(scope(), gathered(), research(), FakeModel())
    assert json.loads(result["documents"]["assessment.json"])["negative_themes"] == []


async def test_an_off_contract_verdict_is_repaired_then_clamped_by_code():
    def overspend(value, user):
        value["campaign"]["daily_budget_usd"] = {"min": 1, "max": 9999}
        value["campaign"]["target_cpa_usd"] = 5000
        return value

    model = FakeModel(overrides={"verdict": overspend})
    result = await paid_ads.build_assessment(scope(), gathered(), research(), model)
    assert "repair:1" in model.calls and not result["report"]["code_corrections"]
    block = json.loads(
        re.search(r"```tin-ads\n(.*?)\n```", result["documents"]["ASSESSMENT.md"], re.S).group(1)
    )
    assert block["campaign"]["target_cpa_usd"] <= block["allowable_cpa_usd"]

    def broken_repair(value, user):
        return {"fixes": []}

    model = FakeModel(overrides={"verdict": overspend, "repair": broken_repair})
    result = await paid_ads.build_assessment(scope(), gathered(), research(), model)
    assert "target CPA clamped to the allowable" in result["report"]["code_corrections"]


async def test_no_history_yields_a_cheap_test_and_neither_platform_when_economics_fail():
    model = FakeModel()
    no_history = scope(ads_history="never", ads_spend_usd=0, ads_clicks=0, ads_purchases=0)
    result = await paid_ads.build_assessment(no_history, gathered(), research(), model)
    assert result["report"]["decision"] == "test" and "diagnose" not in model.calls
    expensive = scope(
        ads_history="never",
        ads_spend_usd=0,
        ads_clicks=0,
        ads_purchases=0,
        price_point=1,
        budget="more",
    )
    research_no_captive = research()
    for row in research_no_captive["keywords"]:
        if row["keyword"] == "openclaw imessage":
            row["keyword"] = "imessage bot app"
            research_no_captive["labels"][row["id"]] = ["mofu", 1]
    result = await paid_ads.build_assessment(
        expensive, gathered(), research_no_captive, FakeModel()
    )
    assert result["report"]["decision"] == "not_now"
    block = json.loads(
        re.search(r"```tin-ads\n(.*?)\n```", result["documents"]["ASSESSMENT.md"], re.S).group(1)
    )
    assert block["platform"] == "neither" and block["campaign"] is None


def test_not_now_documents_hold_the_gate_and_nothing_else():
    docs = paid_ads.not_now_documents(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        definition_sha="e" * 40,
        today="2026-09-22",
        inputs={"hard_nos": ["no_paid_ads"]},
        reason={"reason": "no_paid_ads", "text": "The founder said no paid ads."},
    )
    assert set(docs) == set(paid_ads.LIMITS)
    block = json.loads(
        re.search(r"```tin-ads\n(.*?)\n```", docs["ASSESSMENT.md"].decode(), re.S).group(1)
    )
    assert block["decision"] == "not_now" and block["gate"] == "no_paid_ads"
    assert json.loads(docs["evidence.json"])["evidence"] == {}


async def test_upstream_sources_refuse_a_run_from_another_project_or_a_bad_digest():
    from tin_lite.paid_ads_sources import upstream_sources

    project = SimpleNamespace(id="p1", state_repo_id="repo")
    run = SimpleNamespace(
        id="r1",
        project_id="p2",
        executor="organic.keyword_plan",
        status=SimpleNamespace(value="succeeded"),
        canonical_commit_sha="a" * 40,
    )
    db = SimpleNamespace(get_run=lambda *_: _coro(run), get_effect=lambda *_: _coro(None))
    with pytest.raises(ValueError):
        await upstream_sources(
            database=db,
            storage=None,
            project=project,
            inputs={"keyword_run_id": "00000000-0000-4000-8000-000000000001"},
            market="US",
        )
    run.project_id = "p1"
    receipt = SimpleNamespace(
        status="completed", result={"canonical_commit_sha": "a" * 40, "documents_sha256": "wrong"}
    )
    db = SimpleNamespace(get_run=lambda *_: _coro(run), get_effect=lambda *_: _coro(receipt))
    storage = SimpleNamespace(read_canonical_artifact=lambda **_: _coro(b"{}"))
    with pytest.raises(ValueError):
        await upstream_sources(
            database=db,
            storage=storage,
            project=project,
            inputs={"keyword_run_id": "00000000-0000-4000-8000-000000000001"},
            market="US",
        )
    assert (
        await upstream_sources(
            database=db, storage=storage, project=project, inputs={}, market="US"
        )
        == {}
    )


async def _coro(value):
    return value
