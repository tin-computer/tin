"""Google Ads money in the account's own currency: one ECB rate pinned at launch, amounts on
Google's minimum unit, a named refusal for currencies Tin cannot price, and a monitor that
compares costs only against figures in the currency they were planned in."""

from __future__ import annotations

import json
from datetime import date

import pytest
from temporalio.exceptions import ApplicationError
from test_google_ads import CID
from test_google_ads import plan as bundle_plan
from test_paid_ads_launch import MARKER, TODAY, assessment, inputs, keywords_csv, site
from test_paid_ads_launch import account as launch_account
from test_paid_ads_launch_activities import Script, mutates, run_through_approval
from test_paid_ads_launch_activities import fixture as launch_fixture
from test_paid_ads_monitor import CAMPAIGN, campaign_row, reads

from tin_lite import ads_currency, paid_ads_launch, paid_ads_monitor
from tin_lite import google_ads_requests as requests
from tin_lite.paid_ads_proposals import _bodies

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01">
 <Cube>
  <Cube time='2026-10-12'>
   <Cube currency='USD' rate='1.25'/>
   <Cube currency='JPY' rate='200'/>
   <Cube currency='TRY' rate='50'/>
   <Cube currency='IDR' rate='20000'/>
   <Cube currency='ISK' rate='136.40'/>
  </Cube>
 </Cube>
</gesmes:Envelope>"""
FEED_DAY = ads_currency.parse_ecb(FEED)
EUR = ads_currency.fx_for("EUR", FEED_DAY)
JPY = ads_currency.fx_for("JPY", FEED_DAY)
IDR = ads_currency.fx_for("IDR", FEED_DAY)


# ---------------------------------------------------------------- rates and rounding


def test_cross_rates_come_from_one_feed_and_unknown_currencies_are_named():
    assert EUR == {
        "currency_code": "EUR",
        "usd_rate": "0.800000",
        "source": ads_currency.ECB_SOURCE,
        "date": "2026-10-12",
    }
    assert JPY["usd_rate"] == "160.000000"
    assert ads_currency.fx_for("TRY", FEED_DAY)["usd_rate"] == "40.000000"
    assert ads_currency.fx_for("USD", FEED_DAY) == ads_currency.USD_FX
    # ISK has an ECB rate but Google Ads does not bill in it; GBP is not in this feed.
    for code in ("ISK", "GBP", "XYZ"):
        with pytest.raises(ads_currency.RateUnavailable) as caught:
            ads_currency.fx_for(code, FEED_DAY)
        assert caught.value.code == "currency_not_published"
    for broken in ("", "<Cube time='2026-10-12'></Cube>", FEED.replace("'1.25'", "'0'")):
        with pytest.raises(ads_currency.RateUnavailable) as caught:
            ads_currency.parse_ecb(broken)
        assert caught.value.code == "ecb_feed_unreadable"


def test_conversion_rounds_to_googles_unit_and_respects_dollar_floors():
    assert ads_currency.convert(10, EUR) == 8.0
    assert ads_currency.convert(1.234, EUR) == 0.99
    assert ads_currency.convert(10.004, JPY) == 1601.0  # yen has no fraction
    assert ads_currency.convert(0.01, JPY, floor_usd=0.05) == 8.0  # 0.05 × 160 rounded up
    assert ads_currency.convert(0.001, EUR, floor_usd=0) == 0.01  # never below one unit
    assert ads_currency.convert(5, None) == 5.0
    assert ads_currency.money(8, "EUR") == "€8.00"
    assert ads_currency.money(1600, "JPY") == "¥1,600"
    assert ads_currency.money(650, "TRY", cents=True) == "TRY 650.00"
    assert ads_currency.money(5, None) == "5.00 (account currency)"
    assert ads_currency.with_usd(8, EUR) == "€8.00 (about $10.00)"
    assert ads_currency.with_usd(8, None) == "$8.00"
    assert "0.800000 EUR per dollar" in ads_currency.rate_line(EUR)


# ---------------------------------------------------------------- request builders


def test_bundle_sends_account_currency_micros_on_its_unit():
    ops = requests.campaign_bundle(
        bundle_plan(daily_budget=1600, cpc_ceiling=160, fx=JPY), customer_id=CID
    )
    budget = ops[0]["campaignBudgetOperation"]["create"]["amountMicros"]
    ceiling = ops[1]["campaignOperation"]["create"]["targetSpend"]["cpcBidCeilingMicros"]
    assert (budget, ceiling) == ("1600000000", "160000000")
    # Ten dollars a day in rupiah is above the old one-million cap; it is still a legal plan.
    ops = requests.campaign_bundle(
        bundle_plan(daily_budget=160_000, cpc_ceiling=16_000, fx=IDR), customer_id=CID
    )
    assert ops[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "160000000000"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"daily_budget": 1600.5, "fx": JPY}, "whole multiple of ¥1"),
        ({"daily_budget": 100, "fx": JPY}, "at least ¥160"),
        ({"daily_budget": 0.5, "fx": EUR}, "at least €0.80"),
        ({"daily_budget": 2_000_000, "fx": EUR}, "below their ceiling"),
        ({"fx": {"currency_code": "ISK", "usd_rate": "120"}}, "supported account currency"),
        ({"fx": {**EUR, "usd_rate": "0"}}, "must be positive"),
    ],
)
def test_bundle_refuses_amounts_off_unit_below_floor_or_in_unknown_currencies(override, message):
    with pytest.raises(ValueError, match=message):
        requests.campaign_bundle(bundle_plan(**override), customer_id=CID)


def test_proposal_bodies_use_the_currency_pinned_on_the_proposal():
    campaign = {"customer_id": CID, "external_budget_id": "11", "external_campaign_id": "22"}
    _, body = _bodies(
        {
            "kind": "budget_change",
            "proposed": {"daily_budget": 1600.0, "currency_code": "JPY", "usd_rate": "160"},
        },
        campaign,
    )
    assert body["operations"][0]["update"]["amountMicros"] == "1600000000"
    _, body = _bodies(
        {
            "kind": "bid_strategy_change",
            "proposed": {
                "strategy": "target_cpa",
                "target_cpa": 24.0,
                "currency_code": "EUR",
                "usd_rate": "0.8",
            },
        },
        campaign,
    )
    assert body["operations"][0]["update"]["maximizeConversions"] == {"targetCpaMicros": "24000000"}
    with pytest.raises(ValueError, match="whole multiple"):
        _bodies(
            {
                "kind": "budget_change",
                "proposed": {"daily_budget": 1600.5, "currency_code": "JPY", "usd_rate": "160"},
            },
            campaign,
        )


# ---------------------------------------------------------------- launch plan


def test_plan_converts_budget_bids_and_targets_once_at_the_pinned_rate():
    plan = paid_ads_launch.plan_skeleton(
        assessment=assessment(),
        keywords_csv=keywords_csv(),
        inputs=inputs(),
        marker=MARKER,
        today=TODAY,
        fx=JPY,
    )
    assert plan["currency_code"] == "JPY" and plan["fx"] == JPY
    # The same plan in dollars is $10 a day, a $1.00 ceiling and $30 per customer.
    assert (plan["daily_budget"], plan["cpc_ceiling"], plan["allowable_cpa"]) == (
        1600.0,
        160.0,
        4800.0,
    )


def test_gate_names_an_account_currency_tin_cannot_price():
    for currency, words in (("ISK", "bills in ISK"), (None, "did not report")):
        acct = launch_account()
        acct["customer"]["currency_code"] = currency
        gate = paid_ads_launch.gate(
            assessment=assessment(),
            account=acct,
            site=site(),
            inputs=inputs(),
            existing_campaign=None,
        )
        assert gate["outcome"] == "unsupported_currency" in paid_ads_launch.OUTCOMES
        assert words in gate["text"] and "Nothing will be created" in gate["text"]


def test_a_plan_drafted_before_currencies_reads_as_dollars():
    legacy = {
        "campaign_name": "x",
        "daily_budget_usd": 10.0,
        "cpc_ceiling_usd": 1.0,
        "allowable_cpa_usd": 30.0,
        "target_cpa_usd": None,
        "currency_code": "USD",
    }
    upgraded = paid_ads_launch.upgrade_plan(legacy)
    assert upgraded["fx"] == ads_currency.USD_FX and upgraded["daily_budget"] == 10.0
    assert upgraded["allowable_cpa"] == 30.0 and "daily_budget_usd" not in upgraded
    assert paid_ads_launch.upgrade_plan(upgraded) is upgraded


# ---------------------------------------------------------------- launch activities


def rates(feed=FEED):
    calls = []

    async def read():
        calls.append(1)
        return feed

    read.calls = calls
    return read


@pytest.mark.asyncio
async def test_a_euro_account_is_planned_approved_and_created_in_euros():
    script, reader = Script(), rates()
    script.currency = "EUR"
    activities, db, storage, script, _, _, _ = await launch_fixture(
        script=script, rate_reader=reader
    )
    run_id, mode = await run_through_approval(activities, db)
    assert mode == "launch" and len(reader.calls) == 1
    assert (await activities._result(run_id, "gather:fx"))["value"] == EUR
    assert db.campaigns[db.run.id]["daily_budget_micros"] == 8_000_000
    assert "€8.00 (about $10.00) a day" in db.reviews[0]["summary"]
    names = paid_ads_launch.paths(run_id, paid_ads_launch.PLAN_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    plan_md = tree[names["PLAN.md"]][1].decode()
    assert "€8.00 (about $10.00) on an average day" in plan_md
    assert "0.800000 EUR per dollar" in plan_md

    await activities.apply(run_id)
    (create,) = mutates(script, validate_only=False)
    operations = create[1]["operations"]
    assert operations[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "8000000"
    ceiling = operations[1]["campaignOperation"]["create"]["targetSpend"]["cpcBidCeilingMicros"]
    assert ceiling == "800000"

    await activities.publish(run_id)
    results = paid_ads_launch.paths(run_id, paid_ads_launch.RESULT_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    saved = json.loads(tree[results["campaign.json"]][1])
    assert saved["currency_code"] == "EUR" and saved["fx"] == EUR
    assert saved["daily_budget"] == 8.0 and saved["allowable_cpa"] == 24.0
    assert len(reader.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["ISK", None])
async def test_an_unpriceable_currency_stops_before_any_rate_model_or_write(currency):
    script, reader = Script(), rates()
    script.currency = currency
    activities, db, storage, script, _, _, model = await launch_fixture(
        script=script, rate_reader=reader
    )
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    assert await activities.draft(run_id) == "setup"
    gate = (await activities._result(run_id, "gathered"))["gate"]
    assert gate["outcome"] == "unsupported_currency"
    assert reader.calls == [] and model.calls == []
    assert all(c[0] == "search" for c in script.calls)
    names = paid_ads_launch.paths(run_id, paid_ads_launch.SETUP_DOCS)
    assert names["SETUP.md"] in storage.repo.trees[storage.repo.head]


@pytest.mark.asyncio
async def test_an_unreadable_rate_retries_and_changes_nothing():
    script = Script()
    script.currency = "EUR"

    async def down():
        raise ads_currency.RateUnavailable("ecb_unreachable")

    activities, db, _, script, _, _, model = await launch_fixture(script=script, rate_reader=down)
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    with pytest.raises(ApplicationError, match="ecb_unreachable") as caught:
        await activities.gather(run_id)
    assert caught.value.non_retryable is False
    assert await activities._result(run_id, "gathered") is None and model.calls == []
    activities.read_rates = rates()
    await activities.gather(run_id)
    assert (await activities._result(run_id, "gathered"))["skeleton"]["fx"] == EUR


@pytest.mark.asyncio
async def test_apply_refuses_a_plan_in_another_currency_than_the_account():
    activities, db, _, script, _, _, _ = await launch_fixture()
    run_id, _ = await run_through_approval(activities, db)
    db.effects[activities.key(run_id, "gathered:account")].result["value"]["customer"][
        "currency_code"
    ] = "EUR"
    with pytest.raises(ApplicationError, match="in USD but the account bills in EUR"):
        await activities.apply(run_id)
    assert mutates(script, validate_only=True) == [] and mutates(script, validate_only=False) == []


@pytest.mark.asyncio
async def test_an_approval_waiting_from_before_currencies_still_applies_in_dollars():
    activities, db, _, script, _, _, _ = await launch_fixture()
    run_id, _ = await run_through_approval(activities, db)
    draft = db.effects[activities.key(run_id, "draft")].result
    plan = draft["plan"]
    for key in ("daily_budget", "cpc_ceiling", "allowable_cpa", "target_cpa"):
        plan[f"{key}_usd"] = plan.pop(key)
    del plan["fx"]
    await activities.apply(run_id)
    (create,) = mutates(script, validate_only=False)
    budget = create[1]["operations"][0]["campaignBudgetOperation"]["create"]["amountMicros"]
    assert budget == "10000000"


# ---------------------------------------------------------------- monitor


EURO_CAMPAIGN = {
    "name": "Tin | Search | Core [tin:abc]",
    "enabled_at": "2026-09-01T10:00:00+00:00",
    "currency_code": "EUR",
    "fx": EUR,
    "allowable_cpa": 30.0,
    "daily_budget": 20.0,
    "cpc_ceiling": 2.0,
}
ACCOUNT = lambda code: [{"customer": {"id": "1", "currencyCode": code}}]  # noqa: E731
CONSTRAINED = {
    "campaign_7d": [
        campaign_row(
            campaign={"primaryStatus": "LIMITED", "primaryStatusReasons": ["BUDGET_CONSTRAINED"]}
        )
    ],
    "campaign_30d": [campaign_row(metrics={"conversions": 5.0})],
}


def monitor_decide(campaign, reads_value):
    return paid_ads_monitor.decide(
        campaign=campaign,
        reads=reads_value,
        labels={},
        today=date(2026, 10, 12),
        auto_negatives_cap=20,
    )


def test_monitor_proposes_and_reports_in_the_currency_the_campaign_was_planned_in():
    decision = monitor_decide(EURO_CAMPAIGN, reads(account=ACCOUNT("EUR"), **CONSTRAINED))
    assert decision["currency_code"] == "EUR"
    (proposal,) = decision["proposals"]
    assert proposal["proposed"] == {
        "daily_budget": 24.0,
        "currency_code": "EUR",
        "usd_rate": "0.800000",
    }
    assert "under the €30.00 you can pay" in proposal["rationale"]
    document = paid_ads_monitor.proposal_document(proposal, EURO_CAMPAIGN, 1, date(2026, 10, 12))
    assert "From €20.00 a day to €24.00 a day." in document
    # A lost account read falls back to the launch's currency instead of failing the check.
    assert monitor_decide(EURO_CAMPAIGN, reads(**CONSTRAINED))["currency_code"] == "EUR"


def test_monitor_steps_yen_budgets_in_whole_yen_and_never_below_a_dollars_worth():
    yen = {**EURO_CAMPAIGN, "currency_code": "JPY", "fx": JPY, "allowable_cpa": 30.0}
    wasteful = reads(
        account=ACCOUNT("JPY"),
        campaign_7d=[campaign_row(budget={"amountMicros": "2000000000"})],
        campaign_14d=[campaign_row(metrics={"costMicros": "95000000", "conversions": 0.0})],
        campaign_30d=[campaign_row(metrics={"conversions": 5.0})],
    )
    (proposal,) = monitor_decide(yen, wasteful)["proposals"]
    assert proposal["proposed"]["daily_budget"] == 1600.0
    assert "from ¥2,000 to ¥1,600 a day" in proposal["rationale"]
    tiny = reads(
        account=ACCOUNT("JPY"),
        campaign_7d=[campaign_row(budget={"amountMicros": "170000000"})],
        campaign_14d=[campaign_row(metrics={"costMicros": "95000000", "conversions": 0.0})],
        campaign_30d=[campaign_row(metrics={"conversions": 5.0})],
    )
    (proposal,) = monitor_decide(yen, tiny)["proposals"]
    assert proposal["proposed"]["daily_budget"] == 160.0


def test_a_dollar_plan_in_a_non_dollar_account_reports_plainly_and_changes_no_spend():
    decision = monitor_decide(CAMPAIGN, reads(account=ACCOUNT("TRY"), **CONSTRAINED))
    assert decision["currency_code"] == "TRY" and decision["proposals"] == []
    assert decision["auto"]["pause_keywords"] == []
    assert decision["metrics"]["allowable_cpa"] is None
    (alert,) = [a for a in decision["alerts"] if a["code"] == "currency_mismatch"]
    assert "bills in TRY" in alert["text"] and "planned in USD" in alert["text"]
    report = paid_ads_monitor.render(
        decision=decision,
        campaign=CAMPAIGN,
        reads_normalized=reads(account=ACCOUNT("TRY"), **CONSTRAINED),
        brief={"summary": "", "changes_explained": [], "watch_for": []},
        applied={},
        proposals_saved=[],
        run_id="00000000-0000-4000-8000-00000000bbbb",
        today=date(2026, 10, 12),
    )["MONITOR.md"]
    assert "daily budget TRY 20.00" in report and "$" not in report
