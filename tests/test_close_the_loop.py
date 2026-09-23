"""Offline tests for outreach.close_the_loop: parsing, verification, ranking and drafts."""

import json
import runpy
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import model_terms, request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "outreach.close_the_loop"
DEFINITION = json.loads((ROOT / "workflow.json").read_text())["definition"]
SPEC = validate_code_definition(DEFINITION)
MODULE = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))

SHIPPED = "Scheduled exports: send any report to Google Sheets every morning. CSV only for now."
CSV = """contact,name,date,request,relationship
ana@acme.test,Ana Ruiz,2026-03-02,Left: can't get reports into Google Sheets automatically,churned
bo@beta.test,Bo,2026-05-10,Need scheduled exports to Sheets and to Excel,lost_deal
cy@gamma.test,,2026-06-01,Please add an automatic export to Google Sheets,customer
cy@gamma.test,,2026-04-01,Any way to push reports to sheets on a schedule?,customer
di@delta.test,Di,2026-07-15,Dark mode please,trial
ed@eps.test,Ed,2026-08-01,Would love a Slack digest of reports,lead
fa@zeta.test,Fa,2026-08-20,Sheets export on a schedule would close the deal for us,lead
"""
MATCHES = {
    0: ("asked_for_this", "reports into Google Sheets automatically", ""),
    1: ("partly", "scheduled exports to Sheets", "Excel export"),
    2: ("asked_for_this", "automatic export to Google Sheets", ""),
    3: ("asked_for_this", "push reports to sheets on a schedule", ""),
    4: ("no", "", ""),
    # Plausible but invented: this quote is not in the request text.
    5: ("asked_for_this", "Slack digest of Google Sheets exports", ""),
    6: ("asked_for_this", "Sheets export on a schedule", ""),
}
DRAFTS = {
    "win_back": (
        "{name}, the reason you left is fixed",
        "Hi {name},\n\nYou told us {their_words}. That is now built: {LINK}\n\nNo pressure; "
        "if it helps, take another look.\n\nMaya",
    ),
    "convert": (
        "Scheduled Sheets exports are live",
        "Hi {name},\n\nWhen you were trying us you asked: {their_words}. It is live: {LINK}"
        "\n\nHappy to help you set it up.\n\nMaya",
    ),
    "tell": (
        "You asked, we built it",
        "Hi {name},\n\nThanks for asking: {their_words}. You can turn it on today: {LINK}\n\nMaya",
    ),
}
LINK = "https://example.test/changelog/sheets"


def inputs(**overrides):
    return {
        "shipped": SHIPPED,
        "shipped_on": "2026-09-20",
        "requests_csv": CSV,
        "link": LINK,
        "sender_name": "Maya",
        **overrides,
    }


def model(matches=None, drafts=None):
    """A fake model client that checks every request against the declared route bounds.

    `matches(item)` returns (match, quote, gap), or None to drop that ID from the answer.
    """
    calls = []
    matches = matches or (lambda item: MATCHES[item["id"]])

    async def generate(**payload):
        request_contract(SPEC, payload)
        calls.append(payload)
        if payload["route"] == "match":
            output = {"items": []}
            for item in payload["data"]["requests"]:
                verdict = matches(item)
                if verdict is not None:
                    match, quote, gap = verdict
                    output["items"].append(
                        {"id": item["id"], "match": match, "quote": quote, "gap": gap}
                    )
        else:
            chosen = DRAFTS if drafts is None else drafts
            output = {
                "messages": [
                    {
                        "group": g["group"],
                        "subject": chosen[g["group"]][0],
                        "body": chosen[g["group"]][1].replace("{LINK}", LINK),
                    }
                    for g in payload["data"]["groups"]
                ]
            }
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


def test_manifest_fits_the_code_contract_and_estimate():
    assert DEFINITION["key"] == ROOT.name
    terms = model_terms(DEFINITION)
    assert terms["maximum_nanos"] > 0


async def test_ordinary_export_becomes_a_ranked_verified_send_list():
    context, calls = model()
    result = await MODULE.run(context, inputs())
    validate_code_result(json.dumps(result).encode(), SPEC)
    report = result["content"]
    assert [c["step"] for c in calls] == ["match_requests_0", "draft_messages"]
    # Contacts and names never reach a model.
    sent = json.dumps([c["data"] for c in calls])
    assert "acme.test" not in sent and "Ana" not in sent
    # Groups in order: win back, then convert, then tell.
    assert report.index("## Send first: Win back (2)") < report.index("## Convert (1)")
    assert report.index("## Convert (1)") < report.index("## Tell (1)")
    # Churned before lost deal; partial match carries what is still missing.
    assert report.index("ana@acme.test") < report.index("bo@beta.test")
    assert "Partly: not covered, Excel export" in report
    # The customer who asked twice appears once, dated from the first ask.
    assert report.count("| cy@gamma.test |") == 1
    assert "2026-04-01 | 172 days" in report and "(2 asks)" in report
    # An invented quote is not trusted: that person goes to a manual check, not a group.
    assert "## Check by hand (1)" in report and "| ed@eps.test | lead |" in report
    assert "ed@eps.test,Ed" not in report
    # Unrelated requests are only counted.
    assert "di@delta.test" not in report
    assert "4 people:** 2 to win back, 1 to convert, 1 to tell" in report
    # Personalized preview and merge list use the requester's own words.
    assert "You told us “reports into Google Sheets automatically”" in report
    assert (
        "fa@zeta.test,Fa,convert,Scheduled Sheets exports are live,Sheets export on a schedule"
        in report
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"requests_csv": "email,date,request\na@b.test,2026-01-01,x\n"},
            "contact, date and request",
        ),
        ({"requests_csv": "contact,date,request\n"}, "at least one request"),
        ({"requests_csv": "contact,date,request\na@b.test,01/02/2026,Sheets\n"}, "YYYY-MM-DD"),
        ({"requests_csv": "contact,date,request\na@b.test,2026-02-30,Sheets\n"}, "real calendar"),
        (
            {"requests_csv": "contact,date,request,relationship\na@b.test,2026-01-01,x,vip\n"},
            "relationship",
        ),
        ({"requests_csv": "contact,date,request\na@b.test,2026-01-01,x,extra\n"}, "does not match"),
        ({"requests_csv": "contact,date,request\n,2026-01-01,Sheets\n"}, "contact"),
        ({"requests_csv": "contact,date,request\na@b.test,2026-01-01,\n"}, "request of"),
        (
            {"requests_csv": "contact,date,request\na@b.test,2026-01-01," + "x" * 601 + "\n"},
            "request of",
        ),
        (
            {"requests_csv": "contact,date,request\n" + "a@b.test,2026-01-01,x\n" * 151},
            "at most 150",
        ),
        ({"shipped_on": "next week"}, "Ship date"),
        ({"link": "http://example.test/changelog"}, "https://"),
        ({"link": "https://example.test/a b"}, "https://"),
        ({"shipped": "   short   "}, "20 characters"),
    ],
)
async def test_invalid_inputs_fail_before_any_model_call(overrides, message):
    context, calls = model()
    with pytest.raises(ValueError, match=message):
        await MODULE.run(context, inputs(**overrides))
    assert calls == []


@pytest.mark.parametrize("problem", ["missing", "duplicate", "foreign"])
async def test_matching_that_loses_repeats_or_invents_ids_is_unusable(problem):
    context, calls = model(
        matches=(lambda item: None if item["id"] == 3 else MATCHES[item["id"]])
        if problem == "missing"
        else None
    )
    original = context.models.generate

    async def altered(**payload):
        response = await original(**payload)
        items = response["parsed"]["items"]
        if problem == "duplicate":
            items.append(dict(items[0]))
        elif problem == "foreign":
            items[0] = {**items[0], "id": 99}
        return response

    context.models.generate = altered
    with pytest.raises(ValueError, match="exactly once"):
        await MODULE.run(context, inputs())
    assert [c["step"] for c in calls] == ["match_requests_0"]


async def test_mostly_invented_quotes_make_the_matching_unusable():
    def invented(item):
        return ("asked_for_this", "wants a native Google Sheets sync", "")

    context, calls = model(matches=invented)
    with pytest.raises(ValueError, match="unusable"):
        await MODULE.run(context, inputs())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("drafts", "reason"),
    [
        ({**DRAFTS, "tell": ("Built", "Hi {name}, {their_words} {LINK}\n```\nx\n```")}, "plain"),
        (
            {
                **DRAFTS,
                "tell": (
                    "Built",
                    "Hi {name}, {their_words} is live: {LINK} Try https://evil.test now",
                ),
            },
            "link",
        ),
        ({**DRAFTS, "convert": ("Live", "Hi {name}, it is live: {LINK}")}, "exactly once"),
        (
            {**DRAFTS, "win_back": ("Hi", "Hi {first}, you said {their_words}: {LINK}")},
            "placeholders",
        ),
        ({**DRAFTS, "tell": ("{their_words}", "Hi {name}, {their_words} {LINK}")}, "subject"),
    ],
)
async def test_unusable_draft_keeps_the_list_and_withholds_the_copy(drafts, reason):
    context, calls = model(drafts=drafts)
    result = await MODULE.run(context, inputs())
    validate_code_result(json.dumps(result).encode(), SPEC)
    report = result["content"]
    assert "No draft: the drafted copy failed a check" in report and reason in report
    assert "evil.test" not in report and "**Subject:**" not in report
    assert "fa@zeta.test,Fa,convert,,Sheets export on a schedule" in report
    assert len(calls) == 2


async def test_no_matching_requests_skips_the_draft_and_says_so():
    context, calls = model(matches=lambda item: ("no", "", ""))
    result = await MODULE.run(context, inputs())
    assert [c["step"] for c in calls] == ["match_requests_0"]
    assert "Nobody in this export asked for this change." in result["content"]
    assert "## Merge list" not in result["content"]


async def test_large_exports_split_into_bounded_calls_and_fit_the_report():
    rows = "".join(
        f"person{i:03d}@customer-{i:03d}.example.test,Person {i},2026-0{1 + i % 8}-1{i % 9},"
        f"Our weekly reports in Google Sheets on a schedule please (row {i}),"
        f"{['churned', 'lost_deal', 'trial', 'lead', 'customer'][i % 5]}\n"
        for i in range(150)
    )

    def all_match(item):
        return ("asked_for_this", "reports in Google Sheets on a schedule", "")

    export = "contact,name,date,request,relationship\n" + rows
    assert len(export) <= DEFINITION["input_schema"]["properties"]["requests_csv"]["maxLength"]
    context, calls = model(matches=all_match)
    result = await MODULE.run(context, inputs(requests_csv=export))
    validate_code_result(json.dumps(result).encode(), SPEC)
    assert [c["step"] for c in calls] == ["match_requests_0", "match_requests_1", "draft_messages"]
    assert "Tell 150 people" in result["content"]


async def test_exports_needing_more_than_two_calls_are_rejected_before_calling():
    request = "é" * 590  # Non-ASCII text expands when serialized for the model.
    rows = "".join(f"p{i}@x.test,2026-01-01,{request}\n" for i in range(40))
    context, calls = model()
    with pytest.raises(ValueError, match="split the export"):
        await MODULE.run(context, inputs(requests_csv="contact,date,request\n" + rows))
    assert calls == []


async def test_founder_sees_the_exact_words_and_the_merge_list_cannot_run_formulas():
    export = (
        "contact,name,date,request,relationship\n"
        "=HYPERLINK(1),=cmd,2026-05-01,=IMPORTXML() and Scheduled Google Sheets Exports,churned\n"
    )

    def lower_case_quote(item):
        return ("asked_for_this", "scheduled google sheets exports", "")

    context, _ = model(matches=lower_case_quote)
    result = await MODULE.run(context, inputs(requests_csv=export))
    report = result["content"]
    assert "“Scheduled Google Sheets Exports”" in report
    assert "'=HYPERLINK(1),'=cmd,win_back," in report
    assert "\n=HYPERLINK" not in report.split("```csv", 1)[1]
