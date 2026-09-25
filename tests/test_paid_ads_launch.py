"""Paid ads launch: contract pin, input checks, the gate matrix, the deterministic plan, copy
validation, the orchestrated sequence with a fake model, tag-install bounds and renders.
No providers, no database."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from pathlib import Path

import pytest

from tin_lite import paid_ads_launch as launch
from tin_lite.workflow_inputs import validate_input_schema

FIXTURES = Path(__file__).parent / "fixtures" / "paid_ads"
RUN_ID = "00000000-0000-4000-8000-00000000abcd"
PROJECT_ID = "00000000-0000-4000-8000-00000000dcba"
TODAY = date(2026, 9, 22)
MARKER = launch.marker(RUN_ID)


def assessment():
    return json.loads((FIXTURES / "assessment_sample.json").read_text())


def keywords_csv():
    return (FIXTURES / "keywords_sample.csv").read_text()


def account(**overrides):
    base = {
        "customer": {
            "id": "1234567890",
            "currency_code": "USD",
            "time_zone": "America/New_York",
            "status": "ENABLED",
            "auto_tagging_enabled": True,
            "conversion_tracking_status": "CONVERSION_TRACKING_MANAGED_BY_SELF",
            "accepted_customer_data_terms": True,
            "conversion_tracking_id": "17707549309",
        },
        "billing": {"approved": True, "statuses": ["APPROVED"]},
        "conversion_actions": [
            {
                "resource_name": "customers/1234567890/conversionActions/1",
                "id": "1",
                "name": "Trial started",
                "category": "SIGNUP",
                "status": "ENABLED",
                "primary_for_goal": True,
                "type": "WEBPAGE",
                "conversions_30d": 12.0,
            }
        ],
        "link_status": "active",
    }
    base.update(overrides)
    return base


def site(*, tagged=True, verdict="ok"):
    signals = {"gtag": tagged, "aw_conversion": tagged, "gtm": False, "ga4": False}
    return {
        "verdict": verdict,
        "pages": [
            {"url": "https://www.clawmessenger.com/", "kind": "home", "signals": signals},
            {"url": "https://www.clawmessenger.com/pricing", "kind": "pricing", "signals": signals},
        ],
        "readable": ["https://www.clawmessenger.com/", "https://www.clawmessenger.com/pricing"],
    }


def inputs(**overrides):
    return {"project_id": PROJECT_ID, "assessment_run_id": RUN_ID, "max_cost_usd": 3, **overrides}


def skeleton(**overrides):
    return launch.plan_skeleton(
        assessment=assessment(),
        keywords_csv=keywords_csv(),
        inputs=inputs(**overrides),
        account=account(),
        marker=MARKER,
        today=TODAY,
    )


# ---------------------------------------------------------------- contract and inputs


def test_contract_and_schema_are_stable_and_closed():
    validate_input_schema(launch.INPUT_SCHEMA)
    assert launch.INPUT_SCHEMA["additionalProperties"] is False
    assert launch.contract_digest() == launch.contract_digest()
    assert [(r["provider"], r["model"]) for r in launch.route_definitions()] == [
        ("openai", "gpt-6-sol"),
        ("openai", "gpt-6-sol"),
        ("openai", "gpt-6-luna"),
    ]
    assert launch.route_for("copy").key == "paid-ads-launch-copy-v1"
    assert launch.route_for("brief").key == "paid-ads-judgment-v1"
    assert launch.route_for("negatives").key == launch.route_for("repair:1").key
    assert launch.route_for("tag_install").key == "paid-ads-drafting-v1"
    for section in (
        launch.WRITING,
        launch.COPY_RULES,
        launch.NEGATIVE_RULES,
        launch.BRIEF_RULES,
        launch.TAG_RULES,
        launch.OUTPUT_RULES,
    ):
        assert len(section) > 150
    assert launch.schema_name("repair:1") == "paid_ads_launch_repair_1"
    assert launch.paths(RUN_ID, launch.PLAN_DOCS)["PLAN.md"] == f"ads/google/{RUN_ID}/PLAN.md"
    assert MARKER.startswith("[tin:") and len(MARKER) == 18


@pytest.mark.parametrize(
    "bad",
    [
        {"assessment_run_id": ""},
        {"assessment_run_id": "not-a-uuid"},
        {"landing_page_url": "http://example.com"},
        {"daily_budget_usd": -1},
        {"cpc_ceiling_usd": True},
        {"max_cost_usd": 1},
        {"max_cost_usd": 11},
    ],
)
def test_check_inputs_rejects_what_the_schema_cannot_express(bad):
    with pytest.raises(ValueError):
        launch.check_inputs(inputs(**bad))
    launch.check_inputs(inputs())


# ---------------------------------------------------------------- gate


def test_gate_matrix_covers_every_outcome():
    ready = launch.gate(
        assessment=assessment(),
        account=account(),
        site=site(),
        inputs=inputs(),
        existing_campaign=None,
    )
    assert ready["outcome"] == "ready" and ready["conversion_action"]["id"] == "1"
    cases = [
        (
            "not_recommended",
            {**assessment(), "decision": "not_now", "campaign": None},
            account(),
            site(),
            None,
        ),
        ("needs_link", assessment(), account(link_status="pending"), site(), None),
        (
            "account_disabled",
            assessment(),
            account(customer={**account()["customer"], "status": "SUSPENDED"}),
            site(),
            None,
        ),
        (
            "needs_billing",
            assessment(),
            account(billing={"approved": False, "statuses": []}),
            site(),
            None,
        ),
        ("campaign_exists", assessment(), account(), site(), {"name": "Tin Search · Claw"}),
        ("landing_page_unreachable", assessment(), account(), site(verdict="blocked"), None),
        ("needs_tracking", assessment(), account(conversion_actions=[]), site(), None),
        ("needs_tracking", assessment(), account(), site(tagged=False), None),
    ]
    for expected, source, acct, pages, existing in cases:
        result = launch.gate(
            assessment=source, account=acct, site=pages, inputs=inputs(), existing_campaign=existing
        )
        assert result["outcome"] == expected, expected
        assert result["outcome"] in launch.OUTCOMES and result["text"]
    untagged = launch.gate(
        assessment=assessment(),
        account=account(),
        site=site(tagged=False),
        inputs=inputs(),
        existing_campaign=None,
    )
    assert untagged["conversion_action"]["id"] == "1"
    stale = account(
        conversion_actions=[{**account()["conversion_actions"][0], "conversions_30d": 0}]
    )
    assert (
        launch.gate(
            assessment=assessment(),
            account=stale,
            site=site(),
            inputs=inputs(),
            existing_campaign=None,
        )["outcome"]
        == "needs_tracking"
    )


def test_conversion_category_maps_events_and_names_actions():
    assert launch.conversion_category("free_signup") == ("SIGNUP", "ONE_PER_CLICK")
    assert launch.conversion_category("purchase_no_trial") == ("PURCHASE", "MANY_PER_CLICK")
    assert launch.conversion_category("lead_form")[0] == "SUBMIT_LEAD_FORM"
    assert launch.conversion_category("booking")[0] == "BOOK_APPOINTMENT"
    assert launch.conversion_category("something_else") == ("SIGNUP", "ONE_PER_CLICK")
    name = launch.conversion_action_name("Claw Messenger", MARKER)
    assert name.startswith("Claw Messenger conversion ") and MARKER in name


# ---------------------------------------------------------------- deterministic plan


def test_plan_skeleton_joins_keywords_and_computes_the_numbers():
    plan = skeleton()
    assert plan["campaign_name"].endswith(MARKER) and len(plan["campaign_name"]) <= 80
    assert plan["daily_budget_usd"] == 10 and plan["markets"] == ["US", "GB"]
    # ceiling = min(0.1 × budget, 1.2 × median high bid of the chosen keywords)
    assert plan["cpc_ceiling_usd"] == 1.0
    assert plan["landing_page"] == "https://www.clawmessenger.com/pricing"
    assert plan["start_date"] == "2026-09-23" and plan["currency_code"] == "USD"
    assert [g["name"] for g in plan["ad_groups"]] == [
        "iMessage API",
        "OpenClaw integration",
        "Alternatives",
    ]
    assert plan["ad_groups"][0]["keywords"] == [
        {"id": "kw_api", "text": "imessage api", "match_type": "EXACT"},
        {"id": "kw_business", "text": "imessage for business", "match_type": "EXACT"},
    ]
    assert plan["ad_groups"][2]["match_type"] == "PHRASE"
    texts = [n["text"] for n in plan["negatives"]]
    assert "jobs" in texts and "imessage on android" in texts and "free" in texts
    assert len(texts) == len(set(texts)) and "imessage api" not in texts
    assert all(n["match_type"] in {"PHRASE", "EXACT"} for n in plan["negatives"])
    assert plan["competitors"] == ["sendblue"]
    assert plan["allowable_cpa_usd"] == 30.0 and plan["assessment_run_id"] == RUN_ID


def test_plan_skeleton_honours_founder_overrides_and_refuses_broad():
    plan = skeleton(
        daily_budget_usd=40,
        cpc_ceiling_usd=2.5,
        landing_page_url="https://www.clawmessenger.com/",
        campaign_name="Claw search test",
    )
    assert plan["daily_budget_usd"] == 40 and plan["cpc_ceiling_usd"] == 2.5
    assert plan["landing_page"] == "https://www.clawmessenger.com/"
    assert plan["campaign_name"] == f"Claw search test {MARKER}"
    broad = assessment()
    broad["campaign"]["ad_groups"][0]["match_type"] = "broad"
    with pytest.raises(ValueError):
        launch.plan_skeleton(
            assessment=broad,
            keywords_csv=keywords_csv(),
            inputs=inputs(),
            account=account(),
            marker=MARKER,
            today=TODAY,
        )
    empty = assessment()
    empty["campaign"]["ad_groups"] = [
        {"name": "x", "cluster_id": "c", "keyword_ids": ["missing"], "match_type": "exact"}
    ]
    with pytest.raises(ValueError):
        launch.plan_skeleton(
            assessment=empty,
            keywords_csv=keywords_csv(),
            inputs=inputs(),
            account=account(),
            marker=MARKER,
            today=TODAY,
        )


def test_negatives_never_block_a_keyword_the_campaign_buys(monkeypatch):
    # A phrase negative blocks every query holding its words in order, so a starter word
    # inside a bought keyword would switch that keyword off.
    rows = keywords_csv() + "kw_course,Online Course Platform,bofu,3,500,5.0,4.0,9.0,40,c1,,\n"
    data = assessment()
    data["campaign"]["ad_groups"][0]["keyword_ids"].append("kw_course")
    data["campaign"]["negatives"] += ["Course Platform", "platform online"]
    monkeypatch.setitem(
        launch.STARTER_NEGATIVES, "exact", ["online course platform", "course platform"]
    )
    plan = launch.plan_skeleton(
        assessment=data,
        keywords_csv=rows,
        inputs=inputs(),
        account=account(),
        marker=MARKER,
        today=TODAY,
    )
    assert "Online Course Platform" in [k["text"] for k in plan["ad_groups"][0]["keywords"]]
    kept = {(n["text"], n["match_type"]) for n in plan["negatives"]}
    assert ("course", "PHRASE") not in kept and ("course platform", "PHRASE") not in kept
    assert ("online course platform", "EXACT") not in kept
    # An exact negative blocks only its own query; words out of order block nothing bought.
    assert ("course platform", "EXACT") in kept and ("platform online", "PHRASE") in kept
    assert ("courses", "PHRASE") in kept and ("free", "PHRASE") in kept
    merged = launch.merge_negatives(plan, {"negatives": ["Platform", "imessage", "platforms"]})
    added = {n["text"] for n in merged["negatives"]} - {n["text"] for n in plan["negatives"]}
    assert added == {"platforms"}


# ---------------------------------------------------------------- copy validation


def good_copy(plan, sitelink_pages=None):
    pages = sitelink_pages or [plan["landing_page"]]
    groups = []
    for index, group in enumerate(plan["ad_groups"]):
        stem = "iMessage API"  # never a competitor name, whatever the keyword says
        groups.append(
            {
                "name": group["name"],
                "headlines": [f"{stem} {n}"[:30] for n in range(1, 13)],
                "descriptions": [
                    f"Send and receive iMessage from your app with one API call {n}."
                    for n in range(4)
                ],
                "path1": "pricing",
                "path2": "" if index else "api",
            }
        )
    return {
        "ad_groups": groups,
        "sitelinks": [
            {
                "text": "Pricing",
                "description1": "Plans from $5 a month",
                "description2": "Cancel any time",
                "url": pages[0],
            }
        ],
        "callouts": ["Plans from $5 a month", "Setup in minutes"],
    }


def test_validate_copy_catches_each_problem_class():
    plan = skeleton()
    copy = good_copy(plan)
    assert launch.validate_copy(copy, plan=plan, competitors=plan["competitors"]) == []

    def broken(mutate):
        bad = json.loads(json.dumps(copy))
        mutate(bad)
        return launch.validate_copy(bad, plan=plan, competitors=plan["competitors"])

    def set_headline(index, value):
        def mutate(bad):
            bad["ad_groups"][0]["headlines"][index] = value

        return mutate

    assert any("exceeds 30" in p for p in broken(set_headline(0, "x" * 31)))
    assert any("exclamation" in p for p in broken(set_headline(0, "Try it now!")))
    assert any("capitals" in p for p in broken(set_headline(0, "SIGN UP TODAY")))
    assert not any("capitals" in p for p in broken(set_headline(0, "iMessage API for AI")))
    assert any("forbidden claim" in p for p in broken(set_headline(0, "The best iMessage API")))
    assert any("competitor" in p for p in broken(set_headline(0, "Beats Sendblue")))
    assert any(
        "repeats" in p
        for p in broken(set_headline(1, copy["ad_groups"][0]["headlines"][0].lower()))
    )
    assert any("empty" in p for p in broken(set_headline(0, "  ")))
    assert any(
        "needs 12 headlines" in p
        for p in broken(lambda b: b["ad_groups"][0]["headlines"].__delitem__(slice(7, None)))
    )
    assert not broken(lambda b: b["ad_groups"][0]["headlines"].pop())
    assert any(
        "exceeds 90" in p
        for p in broken(lambda b: b["ad_groups"][0]["descriptions"].__setitem__(0, "d" * 91))
    )
    assert any(
        "path" in p for p in broken(lambda b: b["ad_groups"][0].__setitem__("path1", "two words"))
    )
    assert any("ad groups must be exactly" in p for p in broken(lambda b: b["ad_groups"].pop()))
    assert any(
        "pairs" in p for p in broken(lambda b: b["sitelinks"][0].__setitem__("description2", ""))
    )
    assert any(
        "https" in p for p in broken(lambda b: b["sitelinks"][0].__setitem__("url", "http://x"))
    )
    assert any("exceeds 25" in p for p in broken(lambda b: b["callouts"].__setitem__(0, "c" * 26)))
    merged = launch.merge_copy(plan, copy)
    assert merged["ad_groups"][0]["headlines"][0] == copy["ad_groups"][0]["headlines"][0]
    assert merged["sitelinks"][0]["text"] == "Pricing" and merged["callouts"] == copy["callouts"]


# ---------------------------------------------------------------- orchestration


class FakeModel:
    """Answers each step from its schema and prompt, the way a well-behaved model would."""

    def __init__(self, *, unusable=(), overrides=None):
        self.calls, self.unusable, self.overrides = [], list(unusable), overrides or {}

    async def __call__(self, step, system, user, schema, max_out, effort):
        self.calls.append(step)
        if step in self.unusable:
            self.unusable.remove(step)
            raise launch.UnusableModelResult("response was truncated")
        base = step.removesuffix(":retry").split(":")[0]
        result = getattr(self, "_" + base)(user, schema)
        return self.overrides.get(base, lambda value, user: value)(result, user)

    def _copy(self, user, schema):
        plan = json.loads(
            user.split("PLAN (fixed by code):\n", 1)[1].split("\n\nBUSINESS PROFILE:", 1)[0]
        )
        pages = schema["properties"]["sitelinks"]["items"]["properties"]["url"]["enum"]
        fake = {
            "ad_groups": [
                {"name": g["name"], "keywords": [{"text": k} for k in g["keywords"]]}
                for g in plan["ad_groups"]
            ],
            "landing_page": plan["landing_page"],
        }
        return good_copy(fake, pages)

    def _negatives(self, user, schema):
        return {"negatives": ["hiring", "salary", "imessage api", "free trial hack"]}

    def _brief(self, user, schema):
        facts = json.loads(user.split("PLAN FACTS (the only numbers you may use):\n", 1)[1])
        return {
            "summary": f"One search campaign at ${facts['daily_budget_usd']} a day.",
            "what_tin_will_do": ["Create the campaign paused, then turn it on."],
            "what_tin_will_not_do": ["No broad match.", "No spend before approval."],
            "watch_for": ["Search terms in the first week."],
        }

    def _repair(self, user, schema):
        payload = json.loads(user)
        fixed = payload["copy"]
        for group in fixed["ad_groups"]:
            group["headlines"] = [h.replace("!", "") for h in group["headlines"]]
        return {"fixes": [{"slot": "copy", "value": json.dumps(fixed)}]}


def evidence():
    return {
        "plan": skeleton(),
        "assessment": assessment(),
        "readable": site()["readable"],
        "site_text": "Claw Messenger. iMessage API for AI agents. Plans from $5 a month.",
        "negative_themes": ["jobs", "free"],
    }


async def test_build_plan_retries_once_repairs_once_and_renders():
    model = FakeModel(unusable=["copy"])
    result = await launch.build_plan({"inputs": inputs(), "run_id": RUN_ID}, evidence(), model)
    assert model.calls == ["copy", "copy:retry", "negatives", "brief"]
    assert result["retried_steps"] and result["retried_steps"][0].startswith("copy:")
    plan = result["plan"]
    assert len(plan["ad_groups"][0]["headlines"]) == 12
    texts = [n["text"] for n in plan["negatives"]]
    assert "hiring" in texts and "imessage api" not in texts
    documents = result["documents"]
    assert set(documents) == set(launch.PLAN_DOCS)
    for name, content in documents.items():
        assert 0 < len(content.encode()) <= launch.PLAN_DOCS[name]
    text = documents["PLAN.md"]
    assert "$10.00 on an average day" in text and "Nothing has been created yet" in text
    assert "http" not in text.replace(plan["landing_page"], "").replace(
        "https://www.clawmessenger.com", ""
    )
    assert not re.search(r"\(E-|\[E-", text)
    parsed = json.loads(documents["plan.json"])
    assert parsed["marker"] == MARKER and parsed["operations"] > 20
    rows = list(csv.DictReader(io.StringIO(documents["negatives.csv"])))
    assert rows and set(rows[0]) == {"text", "match_type"}
    try:
        from tin_lite import google_ads_requests
    except ImportError:
        return
    operations = google_ads_requests.campaign_bundle(parsed["plan"], customer_id="1234567890")
    assert isinstance(operations, list) and operations


def test_prune_copy_drops_rule_breaking_extras_instead_of_failing():
    plan = launch.plan_skeleton(
        assessment=assessment(),
        keywords_csv=keywords_csv(),
        inputs=inputs(),
        account={"customer": {}},
        marker=MARKER,
        today=TODAY,
    )
    fake = {
        "ad_groups": [
            {"name": g["name"], "keywords": [{"text": k["text"]} for k in g["keywords"]]}
            for g in plan["ad_groups"]
        ],
        "landing_page": plan["landing_page"],
    }
    copy = good_copy(fake, [plan["landing_page"]])
    copy["sitelinks"].append({**copy["sitelinks"][0], "text": "Docs"})
    assert len(copy["ad_groups"][0]["headlines"]) == 12
    copy["sitelinks"][0]["text"] = "Frequently asked questions"
    copy["sitelinks"][1]["description1"] = "only one description"
    copy["sitelinks"][1]["description2"] = ""
    copy["callouts"].append("This callout is far too long to serve")
    copy["ad_groups"][0]["headlines"][0] = "Try it now!"
    pruned = launch.prune_copy(copy, plan=plan, competitors=plan["competitors"])
    assert all(len(s["text"]) <= 25 for s in pruned["sitelinks"])
    assert (
        pruned["sitelinks"][0]["description1"] == ""
        and pruned["sitelinks"][0]["description2"] == ""
    )
    assert all(len(c) <= 25 for c in pruned["callouts"])
    assert len(pruned["ad_groups"][0]["headlines"]) == 11
    assert not launch.validate_copy(pruned, plan=plan, competitors=plan["competitors"])


async def test_build_plan_repairs_bad_copy_then_gives_up():
    def shout(value, user):
        for index in range(5):
            value["ad_groups"][0]["headlines"][index] = f"Try it now {index}!"
        return value

    model = FakeModel(overrides={"copy": shout})
    result = await launch.build_plan({"inputs": inputs(), "run_id": RUN_ID}, evidence(), model)
    assert "repair:1" in model.calls
    assert all("!" not in h for h in result["plan"]["ad_groups"][0]["headlines"])

    def no_fix(value, user):
        return {"fixes": []}

    stubborn = FakeModel(overrides={"copy": shout, "repair": no_fix})
    with pytest.raises(launch.UnusableModelResult):
        await launch.build_plan({"inputs": inputs(), "run_id": RUN_ID}, evidence(), stubborn)


# ---------------------------------------------------------------- tag install


def test_validate_tag_install_accepts_insert_only_changes():
    original = (
        '<!doctype html>\n<html>\n<head>\n<meta charset="utf-8">\n<title>x</title>\n'
        "</head>\n<body></body>\n</html>\n"
    )
    files = {"index.html": original, "app.js": "console.log(1)\n"}
    good = original.replace(
        '<meta charset="utf-8">\n', '<meta charset="utf-8">\n{{GLOBAL_SITE_TAG}}\n'
    )
    accepted = launch.validate_tag_install(
        {"path": "index.html", "content": good, "reason": "head"}, files
    )
    assert accepted["path"] == "index.html" and accepted["content"] == good
    for bad in (
        {"path": "index.html", "content": original},
        {"path": "index.html", "content": good.replace("<title>x</title>", "<title>y</title>")},
        {"path": "index.html", "content": good + "{{GLOBAL_SITE_TAG}}\n"},
        {"path": "missing.html", "content": good},
        {"path": "index.html", "content": good.replace("{{GLOBAL_SITE_TAG}}", "<script></script>")},
    ):
        with pytest.raises(launch.UnusableModelResult):
            launch.validate_tag_install(bad, files)


# ---------------------------------------------------------------- renders


def test_renders_stay_readable_and_within_limits():
    action = {"name": f"Claw Messenger conversion {MARKER}", "category": "SIGNUP", "id": "9"}
    snippets = {
        "global_site_tag": "<script>gtag</script>",
        "event_snippet": "<script>event</script>",
    }
    gate = {"outcome": "needs_tracking", "text": "Install the tag first."}
    tracking = launch.render_tracking(
        action, snippets, {"number": 4, "repository": "o/r"}, gate, "Claw"
    )
    assert set(tracking) == set(launch.TRACKING_DOCS)
    assert (
        "pull request #4" in tracking["TRACKING.md"] and "Tag Assistant" in tracking["TRACKING.md"]
    )
    assert json.loads(tracking["tracking.json"])["conversion_action"]["id"] == "9"
    setup = launch.render_setup({"outcome": "needs_billing", "text": "Add billing."}, assessment())
    assert "Nothing was created" in setup["SETUP.md"]
    plan = skeleton()
    created = {
        "launch_run_id": RUN_ID,
        "customer_id": "1234567890",
        "campaign_id": "555",
        "budget_id": "556",
        "shared_set_id": "557",
        "resources": {"campaign": "customers/1234567890/campaigns/555"},
        "enabled": True,
        "enabled_at": "2026-09-23T08:00:00+00:00",
        "created_at": "2026-09-23T07:59:00+00:00",
        "paused_subscriptions": ["KEYWORD"],
        "conversion_action": action,
    }
    result = launch.render_result(
        plan, created, {"summary": "Two ads under review."}, "Tracking is in place."
    )
    assert "The campaign is on." in result["RESULT.md"]
    campaign = json.loads(result["campaign.json"])
    assert campaign["campaign_id"] == "555" and campaign["allowable_cpa_usd"] == 30.0
    assert campaign["launch_run_id"] == RUN_ID and campaign["marker"] == MARKER
    for name, content in {**tracking, **setup, **result}.items():
        limits = {**launch.TRACKING_DOCS, **launch.SETUP_DOCS, **launch.RESULT_DOCS}
        assert 0 < len(content.encode()) <= limits[name]
    assert launch.summary_line("ready", plan).startswith("Google Ads campaign live")
    assert launch.summary_line("needs_tracking").startswith("Install tracking")


def test_plan_skeleton_creates_held_back_groups_paused():
    data = assessment()
    data["campaign"]["ad_groups"][0]["name"] = "Buyer terms — hold pending purchase evidence"
    plan = launch.plan_skeleton(
        assessment=data,
        keywords_csv=keywords_csv(),
        inputs=inputs(),
        account={"customer": {}},
        marker=MARKER,
        today=TODAY,
    )
    statuses = {g["name"]: g["status"] for g in plan["ad_groups"]}
    assert statuses["Buyer terms — hold pending purchase evidence"] == "PAUSED"
    assert "ENABLED" in statuses.values()
    text = launch.render_plan(
        launch.merge_copy(
            plan,
            good_copy(
                {
                    **plan,
                    "ad_groups": [
                        {
                            "name": g["name"],
                            "keywords": [{"text": k["text"]} for k in g["keywords"]],
                        }
                        for g in plan["ad_groups"]
                    ],
                    "landing_page": plan["landing_page"],
                }
            ),
        ),
        {
            "summary": "s",
            "what_tin_will_do": ["a"],
            "what_tin_will_not_do": ["b"],
            "watch_for": ["c"],
        },
        data,
        inputs(),
    )["PLAN.md"]
    assert "created paused)" in text
