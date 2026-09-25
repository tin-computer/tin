"""Paid ads monitor: normalising account reads, every rule with a positive and a negative case,
the quiet period, caps, the single proposal per check, rendering and the contract pin."""

from __future__ import annotations

import json
from datetime import date

import pytest

from tin_lite import paid_ads
from tin_lite import paid_ads_monitor as monitor
from tin_lite.workflow_inputs import validate_input_schema

LAUNCH_ID = "00000000-0000-4000-8000-00000000aaaa"
RUN_ID = "00000000-0000-4000-8000-00000000bbbb"
TODAY = date(2026, 10, 12)
CAMPAIGN = {
    "name": "Tin | Search | Core [tin:abc]",
    "enabled_at": "2026-09-01T10:00:00+00:00",
    "allowable_cpa_usd": 30.0,
    "daily_budget_usd": 20.0,
    "cpc_ceiling_usd": 2.0,
    "shared_set": "customers/1/sharedSets/9",
}


def campaign_row(**overrides):
    campaign = {
        "id": "24143450789",
        "name": "Tin | Search | Core [tin:abc]",
        "status": "ENABLED",
        "primaryStatus": "ELIGIBLE",
        "primaryStatusReasons": [],
        "biddingStrategyType": "TARGET_SPEND",
        "targetSpend": {"cpcBidCeilingMicros": "2000000"},
        "aiMaxSetting": {"enableAiMax": False},
    }
    campaign.update(overrides.pop("campaign", {}))
    metrics = {
        "impressions": "1000",
        "clicks": "40",
        "costMicros": "80000000",
        "conversions": 4.0,
        "searchImpressionShare": 0.2,
        "searchBudgetLostImpressionShare": 0.1,
        "searchRankLostImpressionShare": 0.5,
    }
    metrics.update(overrides.pop("metrics", {}))
    budget = {"amountMicros": "20000000", "recommendedBudgetAmountMicros": "30000000"}
    budget.update(overrides.pop("budget", {}))
    return {"campaign": campaign, "campaignBudget": budget, "metrics": metrics}


def term_row(term, clicks=3, cost=9.0, conversions=0.0, status="NONE"):
    return {
        "searchTermView": {"searchTerm": term, "status": status},
        "segments": {
            "keyword": {"info": {"text": "imessage api", "matchType": "PHRASE"}},
            "searchTermMatchSource": "KEYWORD",
        },
        "adGroup": {"id": "77"},
        "metrics": {
            "clicks": str(clicks),
            "costMicros": str(int(cost * 1_000_000)),
            "conversions": conversions,
        },
    }


def keyword_row(text, cost=0.0, conversions=0.0, status="ENABLED", quality=6):
    return {
        "adGroupCriterion": {
            "resourceName": f"customers/1/adGroupCriteria/77~{abs(hash(text)) % 10_000}",
            "criterionId": str(abs(hash(text)) % 10_000),
            "keyword": {"text": text, "matchType": "EXACT"},
            "status": status,
            "qualityInfo": {"qualityScore": quality},
        },
        "metrics": {
            "clicks": "10",
            "impressions": "200",
            "costMicros": str(int(cost * 1_000_000)),
            "conversions": conversions,
        },
    }


def ad_row(ad_id, approval="APPROVED", status="ENABLED", topics=()):
    return {
        "adGroupAd": {
            "resourceName": f"customers/1/adGroupAds/77~{ad_id}",
            "ad": {"id": ad_id},
            "status": status,
            "adStrength": "GOOD",
            "policySummary": {
                "approvalStatus": approval,
                "reviewStatus": "REVIEWED",
                "policyTopicEntries": [{"topic": t} for t in topics],
            },
            "primaryStatusReasons": [],
        }
    }


def reads(**overrides):
    base = {
        "campaign_7d": [campaign_row()],
        "campaign_14d": [
            campaign_row(metrics={"clicks": "80", "costMicros": "160000000", "conversions": 8.0})
        ],
        "campaign_30d": [
            campaign_row(metrics={"clicks": "160", "costMicros": "320000000", "conversions": 16.0})
        ],
        "search_terms": [
            term_row("imessage api pricing", clicks=5, cost=12.0, conversions=1.0),
            term_row("imessage api jobs", clicks=4, cost=10.0),
            term_row("send imessage from python free", clicks=3, cost=7.5),
            term_row("sendblue", clicks=2, cost=6.0),
            term_row("imessage weather", clicks=1, cost=2.0),
        ],
        "keywords": [
            keyword_row("imessage api", cost=100.0, conversions=3.0),
            keyword_row("imessage bot", cost=95.0, conversions=0.0),
            keyword_row("imessage for business", cost=20.0, conversions=0.0),
        ],
        "ads": [ad_row("1"), ad_row("2", approval="DISAPPROVED", topics=["TRADEMARKS"])],
        "assets": [],
        "conversion_actions": [
            {
                "conversionAction": {"name": "Signup", "category": "SIGNUP"},
                "metrics": {"allConversions": 16.0},
            }
        ],
        "subscriptions": [
            {"recommendationSubscription": {"type": "KEYWORD", "status": "PAUSED"}},
        ],
    }
    base.update(overrides)
    return monitor.normalize_reads(base)


LABELS = {
    "imessage api pricing": "relevant",
    "imessage api jobs": "job_or_free",
    "send imessage from python free": "job_or_free",
    "sendblue": "competitor",
    "imessage weather": "irrelevant",
}


def decide(reads_value=None, labels=None, today=TODAY, cap=20, **campaign_overrides):
    return monitor.decide(
        campaign={**CAMPAIGN, **campaign_overrides},
        reads=reads_value if reads_value is not None else reads(),
        labels=LABELS if labels is None else labels,
        today=today,
        auto_negatives_cap=cap,
    )


# ---------------------------------------------------------------- contract and schema


def test_contract_and_schema_are_closed_and_stable():
    assert monitor.KEY == "ads.monitor" and monitor.PREFIX == "paid_ads_monitor"
    assert monitor.contract_digest() == monitor.contract_digest()
    assert [r["model"] for r in monitor.route_definitions()] == ["gpt-6-sol", "gpt-6-luna"]
    assert monitor.route_for("brief") is paid_ads.JUDGMENT_ROUTE
    assert monitor.route_for("classify:1") is paid_ads.DRAFTING_ROUTE
    assert monitor.route_for("repair:1") is paid_ads.DRAFTING_ROUTE
    validate_input_schema(monitor.INPUT_SCHEMA)
    assert monitor.INPUT_SCHEMA["additionalProperties"] is False
    assert monitor.INPUT_SCHEMA["properties"]["max_cost_usd"]["default"] == 2
    assert monitor.schema_name("classify:1") == "paid_ads_monitor_classify_1"
    assert "## 1. Classify" not in monitor.WRITING and "irrelevant" in monitor.CLASSIFY_RULES
    assert "watch_for" in monitor.BRIEF_RULES


def test_check_inputs_requires_a_launch_run_and_sane_bounds():
    monitor.check_inputs(
        {"launch_run_id": LAUNCH_ID, "auto_negatives_per_run": 5, "max_cost_usd": 2}
    )
    with pytest.raises(ValueError):
        monitor.check_inputs({"launch_run_id": ""})
    with pytest.raises(ValueError):
        monitor.check_inputs({"launch_run_id": "not-a-uuid"})
    with pytest.raises(ValueError):
        monitor.check_inputs({"launch_run_id": LAUNCH_ID, "auto_negatives_per_run": 51})
    with pytest.raises(ValueError):
        monitor.check_inputs({"launch_run_id": LAUNCH_ID, "max_cost_usd": 0.5})


def test_paths_use_both_run_ids_and_proposal_numbers_are_padded():
    names = monitor.paths(LAUNCH_ID, RUN_ID)
    assert names == {
        "MONITOR.md": f"ads/google/{LAUNCH_ID}/monitor/{RUN_ID}.md",
        "monitor.json": f"ads/google/{LAUNCH_ID}/monitor/{RUN_ID}.json",
    }
    assert monitor.proposal_path(LAUNCH_ID, 7) == f"ads/google/{LAUNCH_ID}/proposals/007.md"
    with pytest.raises(ValueError):
        monitor.proposal_path(LAUNCH_ID, 0)
    with pytest.raises(ValueError):
        monitor.paths("nope", RUN_ID)


# ---------------------------------------------------------------- normalising


def test_normalize_reads_converts_micros_and_tolerates_missing_fields():
    picture = reads()
    campaign = picture["campaign"]
    assert campaign["id"] == "24143450789" and campaign["bidding_strategy_type"] == "TARGET_SPEND"
    assert campaign["cpc_ceiling_usd"] == 2.0 and campaign["target_cpa_usd"] is None
    assert campaign["budget_usd"] == 20.0 and campaign["recommended_budget_usd"] == 30.0
    assert campaign["ai_max_enabled"] is False and campaign["migration_dates"] == []
    week = picture["windows"]["7d"]
    assert week == {
        "impressions": 1000,
        "clicks": 40,
        "cost_usd": 80.0,
        "conversions": 4.0,
        "ctr": 0.04,
        "cpc_usd": 2.0,
        "cpa_usd": 20.0,
        "impression_share": 0.2,
        "budget_lost_share": 0.1,
        "rank_lost_share": 0.5,
    }
    assert picture["windows"]["30d"]["conversions"] == 16.0
    assert [t["term"] for t in picture["search_terms"]][:2] == [
        "imessage api pricing",
        "imessage api jobs",
    ]
    assert picture["search_terms"][0]["conversions"] == 1.0
    assert (
        picture["keywords"][0]["quality_score"] == 6 and picture["keywords"][0]["cost_usd"] == 100.0
    )
    assert picture["ads"][1]["approval_status"] == "DISAPPROVED" and picture["ads"][1][
        "topics"
    ] == ["TRADEMARKS"]
    assert picture["conversion_actions"] == [
        {"name": "Signup", "category": "SIGNUP", "conversions_30d": 16.0}
    ]
    assert picture["subscriptions"] == [{"type": "KEYWORD", "status": "PAUSED"}]

    empty = monitor.normalize_reads({})
    assert empty["campaign"]["status"] == "UNKNOWN" and empty["windows"]["7d"]["cpa_usd"] is None
    assert empty["windows"]["7d"]["impression_share"] is None and empty["search_terms"] == []
    junk = monitor.normalize_reads(
        {"search_terms": ["x", {"metrics": {"clicks": "nan"}}], "ads": [{}]}
    )
    assert junk["search_terms"] == [] and junk["ads"] == []
    shares = monitor.normalize_reads(
        {"campaign_7d": [campaign_row(metrics={"searchImpressionShare": "< 0.1"})]}
    )
    assert shares["windows"]["7d"]["impression_share"] is None


# ---------------------------------------------------------------- model steps


def test_classify_schema_and_label_validation():
    terms = ["imessage api jobs", "sendblue"]
    schema = monitor.classify_schema(terms)
    assert schema["properties"]["labels"]["items"]["properties"]["term"]["enum"] == sorted(terms)
    assert schema["additionalProperties"] is False
    with pytest.raises(ValueError):
        monitor.classify_schema([])
    labels = monitor.validate_labels(
        {
            "labels": [
                {"term": "sendblue", "label": "competitor"},
                {"term": "imessage api jobs", "label": "job_or_free"},
            ]
        },
        terms,
    )
    assert labels == {"imessage api jobs": "job_or_free", "sendblue": "competitor"}
    for bad in (
        {"labels": [{"term": "other", "label": "relevant"}]},
        {"labels": [{"term": "sendblue", "label": "spam"}]},
        {"labels": [{"term": "sendblue", "label": "competitor"}]},
        {"labels": "no"},
        {
            "labels": [
                {"term": "sendblue", "label": "competitor"},
                {"term": "sendblue", "label": "relevant"},
            ]
        },
    ):
        with pytest.raises(paid_ads.UnusableModelResult):
            monitor.validate_labels(bad, terms)
    system, user = monitor.classify_prompt(
        "iMessage API for agents", [{"term": "sendblue", "clicks": 2}]
    )
    assert "job_or_free" in system and "sendblue" in user


def test_brief_schema_and_validation():
    schema = monitor.brief_schema()
    assert set(schema["properties"]) == {"summary", "changes_explained", "watch_for"}
    good = monitor.validate_brief(
        {
            "summary": "Spend was $80 for four conversions at $20 each.",
            "changes_explained": [" one "],
            "watch_for": ["a", "b", "c", "d"],
        }
    )
    assert good["changes_explained"] == ["one"] and len(good["watch_for"]) == 3
    for bad in (
        "text",
        {"summary": "short", "changes_explained": [], "watch_for": []},
        {
            "summary": "Spend was $80 for four conversions at $20 each.",
            "changes_explained": [""],
            "watch_for": [],
        },
        {
            "summary": "Spend was $80 for four conversions at $20 each.",
            "changes_explained": "x",
            "watch_for": [],
        },
    ):
        with pytest.raises(paid_ads.UnusableModelResult):
            monitor.validate_brief(bad)
    system, user = monitor.brief_prompt({"quiet": False}, {"name": "Core"})
    assert "changes_explained" in system and '"quiet": false' in user


# ---------------------------------------------------------------- rules


def test_quiet_period_changes_nothing_and_says_so():
    decision = decide(today=date(2026, 9, 3))
    assert decision["quiet"] is True and decision["days_live"] == 2
    assert decision["auto"] == {"negatives": [], "pause_ads": [], "pause_keywords": []}
    assert decision["proposals"] == []
    assert (
        decision["findings"][0]["code"] == "quiet_period"
        and "Day 3" in decision["findings"][0]["text"]
    )
    assert decide(today=date(2026, 9, 4))["quiet"] is False
    assert decide(enabled_at=None)["quiet"] is True


def test_negatives_follow_labels_clicks_conversions_status_and_the_cap():
    decision = decide()
    chosen = [n["text"] for n in decision["auto"]["negatives"]]
    # "imessage weather" is irrelevant but had one click; "sendblue" is a competitor; pricing converted.
    assert chosen == ["imessage api jobs", "send imessage from python free"]
    assert all(n["match_type"] == "PHRASE" for n in decision["auto"]["negatives"])
    assert "no conversion" in decision["auto"]["negatives"][0]["why"]

    capped = decide(cap=1)
    assert [n["text"] for n in capped["auto"]["negatives"]] == ["imessage api jobs"]
    assert decide(cap=0)["auto"]["negatives"] == []

    converted = decide(labels={**LABELS, "imessage api pricing": "irrelevant"})
    assert "imessage api pricing" not in [n["text"] for n in converted["auto"]["negatives"]]

    already = reads(
        search_terms=[term_row("imessage api jobs", clicks=4, cost=10.0, status="EXCLUDED")]
    )
    assert decide(already)["auto"]["negatives"] == []

    unlabeled = decide(labels={})
    assert unlabeled["auto"]["negatives"] == []


def test_negatives_judge_a_term_across_every_row_it_appears_in():
    # search_term_view returns one row per term, ad group and matched keyword.
    split = reads(
        search_terms=[
            term_row("imessage api jobs", clicks=1, cost=2.0, conversions=1.0),
            term_row("imessage api jobs", clicks=4, cost=10.0),
            term_row("send imessage from python free", clicks=1, cost=2.5),
            term_row("send imessage from python free", clicks=1, cost=3.0),
            term_row("imessage weather", clicks=3, cost=4.0),
            term_row("imessage weather", clicks=1, cost=1.0, status="ADDED_EXCLUDED"),
        ]
    )
    negatives = decide(split)["auto"]["negatives"]
    assert [(n["text"], n["clicks"], n["cost_usd"]) for n in negatives] == [
        ("send imessage from python free", 2, 5.5)
    ]
    assert negatives[0]["why"].startswith("2 clicks and $5.50")


def test_disapproved_ads_are_paused_only_while_enabled():
    decision = decide()
    assert [p["ad_id"] for p in decision["auto"]["pause_ads"]] == ["2"]
    assert "TRADEMARKS" in decision["auto"]["pause_ads"][0]["why"]
    paused = reads(ads=[ad_row("2", approval="DISAPPROVED", status="PAUSED")])
    assert decide(paused)["auto"]["pause_ads"] == []
    limited = reads(ads=[ad_row("3", approval="APPROVED_LIMITED")])
    assert decide(limited)["auto"]["pause_ads"] == []


def test_wasteful_keywords_pause_after_thirty_days_at_three_times_allowable():
    decision = decide()
    assert [k["text"] for k in decision["auto"]["pause_keywords"]] == ["imessage bot"]
    assert "$95.00" in decision["auto"]["pause_keywords"][0]["why"]
    early = decide(today=date(2026, 9, 20))
    assert early["auto"]["pause_keywords"] == []
    cheap = decide(allowable_cpa_usd=40.0)  # threshold 120 > 95
    assert cheap["auto"]["pause_keywords"] == []
    assert decide(allowable_cpa_usd=0)["auto"]["pause_keywords"] == []
    paused = reads(keywords=[keyword_row("imessage bot", cost=95.0, status="PAUSED")])
    assert decide(paused)["auto"]["pause_keywords"] == []


def test_bid_strategy_proposals_follow_conversion_thresholds():
    decision = decide()  # TARGET_SPEND with 16 conversions in 30 days
    assert len(decision["proposals"]) == 1
    proposal = decision["proposals"][0]
    assert proposal["kind"] == "bid_strategy_change"
    assert proposal["proposed"] == {"strategy": "maximize_conversions", "target_cpa_usd": None}
    assert proposal["previous"]["cpc_ceiling_usd"] == 2.0

    few = reads(campaign_30d=[campaign_row(metrics={"conversions": 14.0})])
    assert decide(few)["proposals"] == []

    max_conv = reads(
        campaign_7d=[
            campaign_row(
                campaign={"biddingStrategyType": "MAXIMIZE_CONVERSIONS", "targetSpend": None}
            )
        ],
        campaign_30d=[
            campaign_row(
                campaign={"biddingStrategyType": "MAXIMIZE_CONVERSIONS", "targetSpend": None},
                metrics={"conversions": 32.0, "costMicros": "640000000"},
            )
        ],
    )
    proposal = decide(max_conv)["proposals"][0]
    assert proposal["proposed"] == {"strategy": "target_cpa", "target_cpa_usd": 22.0}  # 20 × 1.1
    assert "$22.00" in proposal["rationale"]
    capped = decide(max_conv, allowable_cpa_usd=21.0)["proposals"][0]
    assert capped["proposed"]["target_cpa_usd"] == 21.0

    already = reads(
        campaign_7d=[
            campaign_row(
                campaign={
                    "biddingStrategyType": "MAXIMIZE_CONVERSIONS",
                    "maximizeConversions": {"targetCpaMicros": "22000000"},
                }
            )
        ],
        campaign_30d=[campaign_row(metrics={"conversions": 40.0})],
    )
    assert decide(already)["proposals"] == []


def test_budget_proposals_step_twenty_percent_and_yield_to_bid_changes():
    constrained = reads(
        campaign_7d=[
            campaign_row(
                campaign={
                    "primaryStatus": "LIMITED",
                    "primaryStatusReasons": ["BUDGET_CONSTRAINED"],
                }
            )
        ],
        campaign_30d=[campaign_row(metrics={"conversions": 5.0})],
    )
    proposal = decide(constrained)["proposals"][0]
    assert proposal["kind"] == "budget_change"
    assert proposal["previous"] == {"daily_budget_usd": 20.0}
    assert proposal["proposed"] == {"daily_budget_usd": 24.0}
    assert "under the $30.00" in proposal["rationale"]

    expensive = reads(
        campaign_7d=[campaign_row(campaign={"primaryStatusReasons": ["BUDGET_CONSTRAINED"]})],
        campaign_14d=[
            campaign_row(metrics={"clicks": "80", "costMicros": "160000000", "conversions": 4.0})
        ],
        campaign_30d=[campaign_row(metrics={"conversions": 5.0})],
    )
    assert decide(expensive)["proposals"] == []  # 14 d CPA $40 > allowable $30

    wasteful = reads(
        campaign_14d=[campaign_row(metrics={"costMicros": "95000000", "conversions": 0.0})],
        campaign_30d=[campaign_row(metrics={"conversions": 5.0})],
    )
    proposal = decide(wasteful)["proposals"][0]
    assert proposal["proposed"] == {"daily_budget_usd": 16.0}
    tiny = reads(
        campaign_7d=[campaign_row(budget={"amountMicros": "1000000"})],
        campaign_14d=[campaign_row(metrics={"costMicros": "95000000", "conversions": 0.0})],
        campaign_30d=[campaign_row(metrics={"conversions": 5.0})],
    )
    assert decide(tiny)["proposals"][0]["proposed"]["daily_budget_usd"] == 1.0

    # A due bidding change wins and only one proposal is ever made per check.
    both = reads(
        campaign_7d=[campaign_row(campaign={"primaryStatusReasons": ["BUDGET_CONSTRAINED"]})],
    )
    decision = decide(both)
    assert [p["kind"] for p in decision["proposals"]] == ["bid_strategy_change"]


def test_alerts_and_findings_cover_the_watch_list():
    noisy = reads(
        campaign_7d=[
            campaign_row(
                campaign={
                    "status": "PAUSED",
                    "primaryStatus": "MISCONFIGURED",
                    "primaryStatusReasons": ["HAS_ADS_DISAPPROVED", "MISCONFIGURED"],
                    "aiMaxSetting": {"enableAiMax": True},
                    "acaMigrationDateTime": "2026-09-15 00:00:00",
                },
                metrics={
                    "searchBudgetLostImpressionShare": 0.45,
                    "clicks": "40",
                    "costMicros": "50000000",
                    "conversions": 1.0,
                },
            )
        ],
        conversion_actions=[
            {
                "conversionAction": {"name": "Signup", "category": "SIGNUP"},
                "metrics": {"allConversions": 0},
            }
        ],
        subscriptions=[
            {"recommendationSubscription": {"type": "USE_BROAD_MATCH_KEYWORD", "status": "ENABLED"}}
        ],
    )
    decision = decide(noisy)
    codes = [a["code"] for a in decision["alerts"]]
    assert codes == [
        "ai_max_on",
        "migrated",
        "has_ads_disapproved",
        "misconfigured",
        "paused_externally",
        "no_conversion_data",
    ]
    findings = {f["code"]: f["text"] for f in decision["findings"]}
    assert "45%" in findings["budget_limited"]
    assert "USE_BROAD_MATCH_KEYWORD" in findings["auto_apply_on"]
    assert "sendblue" in findings["competitor_terms"]
    assert "landing_page" in findings and "$50.00" in findings["landing_page"]
    calm = decide()
    assert calm["alerts"] == [] and "landing_page" not in {f["code"] for f in calm["findings"]}
    fresh = decide(today=date(2026, 9, 6), reads_value=noisy)
    assert "no_conversion_data" not in [a["code"] for a in fresh["alerts"]]


def test_decision_is_deterministic_and_json_serialisable():
    first, second = decide(), decide()
    assert first == second
    json.dumps(first)
    assert first["metrics"]["allowable_cpa_usd"] == 30.0 and first["metrics"]["search_terms"] == 5


# ---------------------------------------------------------------- rendering


def brief():
    return {
        "summary": "Forty clicks cost $80 and brought four conversions at $20 each, under the $30 you can pay.",
        "changes_explained": ["Two wasted searches are now blocked."],
        "watch_for": ["Whether the disapproved ad comes back after an edit."],
    }


def test_render_writes_readable_report_and_round_trips_json():
    decision = decide()
    applied = {
        "negatives": [{**n, "status": "applied"} for n in decision["auto"]["negatives"]],
        "pause_ads": [{**p, "status": "unknown"} for p in decision["auto"]["pause_ads"]],
        "pause_keywords": [{**k, "status": "applied"} for k in decision["auto"]["pause_keywords"]],
    }
    saved = [
        {
            "number": 1,
            "path": monitor.proposal_path(LAUNCH_ID, 1),
            "proposal": decision["proposals"][0],
        }
    ]
    documents = monitor.render(
        decision=decision,
        campaign=CAMPAIGN,
        reads_normalized=reads(),
        brief=brief(),
        applied=applied,
        proposals_saved=saved,
        run_id=RUN_ID,
        today=TODAY,
    )
    report = documents["MONITOR.md"]
    assert report.startswith("# Google Ads check, 2026-10-12")
    assert "Live for 41 days" in report
    assert "Last 7 days: 40 clicks for $80.00" in report and "$20.00 per conversion" in report
    assert "The most you can pay for one conversion, from the assessment: $30.00." in report
    assert 'Blocked the search "imessage api jobs" (applied)' in report
    assert (
        "Paused ad 2 (unknown)" in report
        and 'Paused the keyword "imessage bot" (applied)' in report
    )
    assert "Proposal 1:" in report and "proposals/001.md" in report
    assert "## Watch for" in report and "http" not in report
    payload = json.loads(documents["monitor.json"])
    assert payload["run_id"] == RUN_ID and payload["decision"] == decision
    assert payload["applied"] == applied and payload["proposals"] == saved
    for name, text in documents.items():
        assert len(text.encode()) <= monitor.LIMITS[name]


def test_render_without_changes_or_proposals_says_so():
    decision = decide(today=date(2026, 9, 2))
    documents = monitor.render(
        decision=decision,
        campaign=CAMPAIGN,
        reads_normalized=reads(),
        brief={
            "summary": "Day two: ads approved, spend registering, tag recording.",
            "changes_explained": [],
            "watch_for": [],
        },
        applied={"negatives": [], "pause_ads": [], "pause_keywords": []},
        proposals_saved=[],
        run_id=RUN_ID,
        today=date(2026, 9, 2),
    )
    assert "- Nothing changed today." in documents["MONITOR.md"]
    assert "- No proposals today." in documents["MONITOR.md"]
    assert "## Watch for" not in documents["MONITOR.md"]


def test_render_refuses_oversized_documents():
    decision = decide()
    with pytest.raises(ValueError):
        monitor.render(
            decision=decision,
            campaign=CAMPAIGN,
            reads_normalized=reads(),
            brief={**brief(), "summary": "x" * 130_000},
            applied={"negatives": [], "pause_ads": [], "pause_keywords": []},
            proposals_saved=[],
            run_id=RUN_ID,
            today=TODAY,
        )


def test_proposal_documents_explain_the_change_and_the_approval():
    decision = decide()
    text = monitor.proposal_document(decision["proposals"][0], CAMPAIGN, 1, TODAY)
    assert text.startswith("# Proposal 1: Change how Google bids")
    assert "as many clicks as the budget buys" in text and "as many conversions" in text
    assert "Nothing changes until you approve" in text
    budget = {
        "kind": "budget_change",
        "previous": {"daily_budget_usd": 20.0},
        "proposed": {"daily_budget_usd": 24.0},
        "rationale": "The budget ran out.",
    }
    text = monitor.proposal_document(budget, {"campaign_name": "Core"}, 2, TODAY)
    assert "From $20.00 a day to $24.00 a day." in text and "Campaign: Core." in text
    tcpa = {
        "kind": "bid_strategy_change",
        "previous": {"strategy": "maximize_conversions", "target_cpa_usd": None},
        "proposed": {"strategy": "target_cpa", "target_cpa_usd": 22.0},
        "rationale": "Enough conversions.",
    }
    assert "target $22.00 per conversion" in monitor.proposal_document(tcpa, CAMPAIGN, 3, TODAY)


def test_summary_line_counts_only_applied_changes():
    decision = decide()
    applied = {
        "negatives": [{"text": "a", "status": "applied"}, {"text": "b", "status": "unknown"}],
        "pause_ads": [{"ad_id": "2", "status": "applied"}],
        "pause_keywords": [],
    }
    assert (
        monitor.summary_line(decision, applied, [{"number": 1}])
        == "1 negative added, 1 pause applied, 1 proposal awaiting you"
    )
    quiet = decide(today=date(2026, 9, 2))
    empty = {"negatives": [], "pause_ads": [], "pause_keywords": []}
    assert monitor.summary_line(quiet, empty, []) == "Quiet period, nothing changed"
    assert (
        monitor.summary_line({**decision, "quiet": False, "alerts": []}, empty, [])
        == "Checked, nothing changed"
    )
    alerted = {**decision, "alerts": [{"code": "x", "text": "y"}]}
    assert monitor.summary_line(alerted, empty, []) == "1 alert"
