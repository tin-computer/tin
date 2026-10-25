"""Offline checks for qa.buyer_trust: package contract, trust gate rubric and cases.

No web, model or provider calls. Only the reviewed GATE.md resource executes, never page text.
"""

import json
import re
from pathlib import Path

import jsonschema
import pytest

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.workflow_prerequisites import parse_workflow_prerequisites

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/qa.buyer_trust"
SKILL = PACKAGE / "skills/buyer-trust"
CASES = ROOT / "workflow_evals/qa.buyer_trust/qualification.json"
AS_OF = "2026-09-24"
ORIGIN = "https://shop.example.com"


def manifest():
    return json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]


@pytest.fixture(scope="module")
def trust():
    path = SKILL / "GATE.md"
    blocks = re.findall(r"```python\n(.*?)\n```\n", path.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


def evidence(**overrides):
    base = {
        "origin": ORIGIN,
        "status": {
            "home": 200,
            "http_redirects_to_https": True,
            "privacy": 200,
            "security_txt": 200,
            "robots": 200,
            "git_head": 404,
            "env": 404,
        },
        "headers": {
            "hsts": "max-age=31536000; includeSubDomains",
            "csp": "default-src 'self'; frame-ancestors 'none'",
            "x_frame_options": "DENY",
            "referrer_policy": "strict-origin-when-cross-origin",
            "x_content_type_options": "nosniff",
        },
        "security_txt": {
            "contact": "mailto:security@example.com",
            "expires": "2027-06-01T00:00:00Z",
        },
        "mixed_content": [],
        "public_contact": "mailto:hello@example.com",
        "checkout": {
            "guest_option": True,
            "total_before_pay": True,
            "contact_url": f"{ORIGIN}/contact",
            "refund_url": f"{ORIGIN}/refunds",
            "refund_checked": True,
        },
    }
    for dotted, value in overrides.items():
        target = base
        *parents, leaf = dotted.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return base


def fixes_by_id(result):
    return {item["id"]: item for item in result["fixes"]}


def site_health_schema():
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == "site.health_improve")
    return builtin.input_schema


# Package contract


def test_manifest_matches_its_folder_and_declares_every_skill_file():
    definition = manifest()
    assert definition["key"] == PACKAGE.name
    assert definition["system"] == "product-qa"
    assert definition["executor"] == "codex.procedure"
    procedure = definition["procedure"]
    on_disk = sorted(str(path.relative_to(PACKAGE)) for path in SKILL.iterdir())
    assert sorted(procedure["skill_files"]) == on_disk
    assert "skills/buyer-trust/GATE.md" in procedure["skill_files"]
    front = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert "name: buyer-trust" in front
    assert procedure["entry_skill"] == "buyer-trust"
    assert procedure["sandbox"] == {
        "profile": "isolated",
        "egress": "fenced",
        "timeout_seconds": 900,
    }
    assert procedure["output"]["path"] == "reports/BUYER_TRUST.md"
    assert "integration_requirements" not in definition


def test_every_text_input_is_bounded():
    properties = manifest()["input_schema"]["properties"]
    for name, field in properties.items():
        if field["type"] == "string" and name != "project_id" and "enum" not in field:
            assert 0 < field["maxLength"] <= 2000, name
    # Packages cannot declare a pattern; the skill rejects a non-HTTPS URL before fetching.
    assert properties["product_url"]["format"] == "uri"
    assert "must be an `https://` URL" in (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_prerequisites_read_the_signup_walkthrough_and_project_memory():
    definition = manifest()
    parsed = parse_workflow_prerequisites(
        definition["prerequisites"], input_schema=definition["input_schema"]
    )
    assert all(item.level == "recommended" for item in parsed)
    signup = next(item for item in parsed if item.kind == "run")
    assert signup.workflow == "qa.signup_walkthrough"
    # product_url lets Tin match the walkthrough for the same host, as deep dive and audit do.
    assert signup.match == ("product_host",)
    artifacts = {
        (item.path, item.section): item.producer for item in parsed if item.kind == "artifact"
    }
    assert artifacts == {
        ("wiki/INDEX.md", "### Feature map"): "product.deep_dive",
        ("wiki/INDEX.md", "### Code map"): "product.code_map",
        ("reports/GROWTH_ONBOARDING_PLAN.md", None): "growth.onboarding_plan",
    }


def test_the_skill_never_rewalks_signup_or_leaves_public_get():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    prompt = (PACKAGE / "PROMPT.md").read_text(encoding="utf-8")
    assert "reports/qa/signup/<host>/" in skill
    assert "Do not walk the signup again" in skill
    assert "walk the signup again" in prompt
    assert "GET or HEAD only" in skill
    for heading in ("## Billing", "## Steps", "## Walls", "### Feature map", "### Code map"):
        assert heading in skill


def test_the_retired_trust_packages_are_gone_and_unreferenced():
    for key in ("growth.trust_fix", "reports.trust_gate", "growth.buyer_trust"):
        assert not (ROOT / "workflow_packages" / key).exists()
        assert not (ROOT / "workflow_evals" / key).exists()
    shipped = [*PACKAGE.rglob("*"), CASES]
    for path in (p for p in shipped if p.is_file()):
        text = path.read_text(encoding="utf-8")
        for retired in ("trust_fix", "trust-fix", "trust_gate", "TRUST_GATE", "growth.buyer_trust"):
            assert retired not in text, (path, retired)


def test_the_report_contract_has_the_sections_the_cases_assert():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        for needle in case["expect"]["contains"]:
            if needle.startswith("## ") or needle == "tin-buyer-trust":
                assert needle in skill or needle in (SKILL / "GATE.md").read_text(), needle
    assert "## Hand-off to site.health_improve" in skill


# Trust gate rules (ported from the retired reports.trust_gate)


def test_clean_evidence_passes_with_no_fixes(trust):
    result = trust["gate"](evidence(), AS_OF)
    assert result["verdict"] == "PASS"
    assert result["fixes"] == []
    assert {row["result"] for row in result["rows"]} == {"pass"}


def test_short_or_missing_hsts_fails_and_hands_off_a_valid_site_health_run(trust):
    for value in ("", "max-age=300", "includeSubDomains"):
        result = trust["gate"](evidence(headers__hsts=value), AS_OF)
        assert result["verdict"] == "FAIL"
        hsts = fixes_by_id(result)["hsts"]
        assert (hsts["rank"], hsts["owner"]) == ("fix_now", "site.health_improve")
        inputs = hsts["site_health_improve"]
        assert inputs["site_url"] == f"{ORIGIN}/"
        assert inputs["focus"] == "reliability"
        assert "max-age=31536000; includeSubDomains" in inputs["context"]
        assert "return no change" in inputs["context"]
        jsonschema.validate(
            {"project_id": "00000000-0000-4000-8000-000000000001", **inputs}, site_health_schema()
        )


def test_exposed_repository_or_env_path_fails_and_goes_to_the_founder(trust):
    result = trust["gate"](evidence(status__env=200), AS_OF)
    assert result["verdict"] == "FAIL"
    exposed = fixes_by_id(result)["exposed_paths"]
    assert exposed["owner"] == "founder"
    assert "/.env" in exposed["fix"] and "rotate" in exposed["fix"]
    assert "site_health_improve" not in exposed


def test_missing_privacy_page_is_fix_now_for_the_founder(trust):
    result = trust["gate"](evidence(status__privacy=404), AS_OF)
    assert result["verdict"] == "FAIL"
    assert fixes_by_id(result)["privacy"]["owner"] == "founder"


def test_plain_http_without_redirect_is_a_host_setting(trust):
    result = trust["gate"](evidence(status__http_redirects_to_https=False), AS_OF)
    assert result["verdict"] == "FAIL"
    assert fixes_by_id(result)["https_redirect"]["owner"] == "founder"


def test_mixed_content_names_the_urls_in_the_hand_off(trust):
    url = "http://cdn.example.net/app.js"
    result = trust["gate"](evidence(mixed_content=[url, url]), AS_OF)
    fix = fixes_by_id(result)["mixed_content"]
    assert fix["rank"] == "fix_now"
    assert url in fix["site_health_improve"]["context"]
    assert result["evidence"]["mixed_content"] == [url]


def test_missing_security_headers_are_one_fix_week_hand_off(trust):
    result = trust["gate"](
        evidence(headers__csp="default-src 'self'", headers__referrer_policy=""), AS_OF
    )
    assert result["verdict"] == "PASS"
    fix = fixes_by_id(result)["security_headers"]
    assert fix["rank"] == "fix_week"
    context = fix["site_health_improve"]["context"]
    assert "`Content-Security-Policy: frame-ancestors 'none'`" in context
    assert "`Referrer-Policy: strict-origin-when-cross-origin`" in context
    assert "X-Frame-Options" not in context
    assert "keep its sources" in context


def test_security_txt_uses_the_published_contact_or_asks_the_founder(trust):
    result = trust["gate"](
        evidence(status__security_txt=404, security_txt={"contact": None, "expires": None}), AS_OF
    )
    fix = fixes_by_id(result)["security_txt"]
    assert fix["owner"] == "site.health_improve"
    assert "Contact: mailto:hello@example.com" in fix["site_health_improve"]["context"]
    assert "Expires: 2027-09-24T00:00:00Z" in fix["site_health_improve"]["context"]
    unowned = trust["gate"](
        evidence(
            status__security_txt=404,
            security_txt={"contact": None, "expires": None},
            public_contact=None,
        ),
        AS_OF,
    )
    assert fixes_by_id(unowned)["security_txt"]["owner"] == "founder"


def test_expired_security_txt_is_fix_week(trust):
    result = trust["gate"](evidence(security_txt__expires="2026-01-01T00:00:00Z"), AS_OF)
    assert result["verdict"] == "PASS"
    assert fixes_by_id(result)["security_txt"]["rank"] == "fix_week"


def test_robots_server_error_goes_to_technical_seo(trust):
    result = trust["gate"](evidence(status__robots=503), AS_OF)
    assert fixes_by_id(result)["robots"]["site_health_improve"]["focus"] == "technical_seo"
    assert "robots" not in fixes_by_id(trust["gate"](evidence(status__robots=404), AS_OF))


def test_checkout_gaps_are_founder_decisions(trust):
    result = trust["gate"](
        evidence(
            checkout={
                "guest_option": False,
                "total_before_pay": False,
                "contact_url": None,
                "refund_url": None,
                "refund_checked": True,
            }
        ),
        AS_OF,
    )
    found = fixes_by_id(result)
    assert found["total_before_pay"]["rank"] == "fix_now"
    assert found["guest_option"]["rank"] == "roadmap"
    assert {
        found[key]["owner"] for key in ("total_before_pay", "contact", "refund", "guest_option")
    } == {"founder"}
    ranks = [item["rank"] for item in result["fixes"]]
    assert ranks == sorted(ranks, key=("fix_now", "fix_week", "roadmap").index)


def test_unchecked_checkout_is_unknown_not_a_fix(trust):
    result = trust["gate"](evidence(checkout=None), AS_OF)
    assert result["verdict"] == "PASS"
    assert result["fixes"] == []
    assert {"signal": "Checkout", "observed": "not checked", "result": "unknown"} in result["rows"]


@pytest.mark.parametrize("refund_url", [None, f"{ORIGIN}/terms"])
def test_unread_refund_policy_is_unknown_not_a_publish_fix(trust, refund_url):
    observed = evidence(checkout__refund_url=refund_url)
    observed["checkout"]["refund_checked"] = False
    result = trust["gate"](observed, AS_OF)
    refund = next(row for row in result["rows"] if row["signal"] == "Refund terms within one click")
    assert refund["result"] == "unknown"
    assert "refund" not in fixes_by_id(result)
    assert result["evidence"]["checkout"]["refund_checked"] is False


def test_legacy_refund_policy_without_a_checked_flag_stays_unknown(trust):
    observed = evidence(checkout__refund_url=None)
    observed["checkout"].pop("refund_checked", None)
    result = trust["gate"](observed, AS_OF)
    assert "refund" not in fixes_by_id(result)
    assert (
        next(row for row in result["rows"] if row["signal"] == "Refund terms within one click")[
            "result"
        ]
        == "unknown"
    )


def test_checked_refund_section_in_terms_satisfies_presence_check(trust):
    observed = evidence(checkout__refund_url=f"{ORIGIN}/terms")
    observed["checkout"]["refund_checked"] = True
    result = trust["gate"](observed, AS_OF)
    assert result["verdict"] == "PASS"
    assert next(
        row for row in result["rows"] if row["signal"] == "Refund terms within one click"
    ) == {
        "signal": "Refund terms within one click",
        "observed": f"{ORIGIN}/terms",
        "result": "pass",
    }
    assert "refund" not in fixes_by_id(result)


def test_unread_refund_policy_keeps_independent_hsts_failure(trust):
    observed = evidence(headers__hsts="max-age=0", checkout__refund_url=None)
    observed["checkout"]["refund_checked"] = False
    result = trust["gate"](observed, AS_OF)
    assert result["verdict"] == "FAIL"
    assert set(fixes_by_id(result)) == {"hsts"}


def test_checked_missing_refund_policy_has_a_scoped_founder_action(trust):
    observed = evidence(checkout__refund_url=None)
    observed["checkout"]["refund_checked"] = True
    result = trust["gate"](observed, AS_OF)
    refund = fixes_by_id(result)["refund"]
    assert refund["owner"] == "founder"
    assert refund["rank"] == "fix_week"
    assert "existing" in refund["fix"].lower()
    assert "only if" in refund["fix"].lower()


def test_refund_policy_checked_state_round_trips(trust):
    observed = evidence(checkout__refund_url=f"{ORIGIN}/terms")
    observed["checkout"]["refund_checked"] = False
    result = trust["gate"](observed, AS_OF)
    payload = json.loads(trust["evidence_block"](result).split("\n", 1)[1].rsplit("\n", 1)[0])
    repeated = trust["gate"](payload["evidence"], AS_OF)
    assert repeated["rows"] == result["rows"]
    assert "refund" not in fixes_by_id(repeated)


def test_refused_refund_fetch_keeps_other_checkout_facts_and_row_count(trust):
    observed = evidence(checkout__refund_url=f"{ORIGIN}/terms")
    observed["checkout"]["refund_checked"] = None
    result = trust["gate"](observed, AS_OF)
    checked = trust["gate"](evidence(), AS_OF)
    assert len(result["rows"]) == len(checked["rows"])
    assert [row for row in result["rows"] if row["signal"] != "Refund terms within one click"] == [
        row for row in checked["rows"] if row["signal"] != "Refund terms within one click"
    ]
    assert "refund" not in fixes_by_id(result)
    assert result["evidence"]["checkout"]["refund_checked"] is None


@pytest.mark.parametrize("key", ["privacy", "git_head", "env"])
def test_an_unknown_gating_signal_cannot_pass(trust, key):
    result = trust["gate"](evidence(**{f"status__{key}": None}), AS_OF)
    assert result["verdict"] == "UNVERIFIED"


def test_an_unreachable_origin_is_unverified_with_nothing_handed_off(trust):
    unreachable = evidence(
        status={key: None for key in evidence()["status"]},
        headers={key: None for key in evidence()["headers"]},
        security_txt={"contact": None, "expires": None},
        mixed_content=None,
        public_contact=None,
        checkout=None,
    )
    result = trust["gate"](unreachable, AS_OF)
    assert result["verdict"] == "UNVERIFIED"
    assert result["fixes"] == []
    assert {row["result"] for row in result["rows"]} == {"unknown"}


def test_hand_off_context_stays_within_site_health_bounds(trust):
    urls = [f"http://cdn{n}.example.net/" + "a" * 400 for n in range(10)]
    result = trust["gate"](
        evidence(headers__hsts="x" * 2000, mixed_content=urls, status__robots=500), AS_OF
    )
    schema = site_health_schema()
    for item in result["fixes"]:
        if "site_health_improve" in item:
            jsonschema.validate(
                {
                    "project_id": "00000000-0000-4000-8000-000000000001",
                    **item["site_health_improve"],
                },
                schema,
            )


def test_the_evidence_block_round_trips_for_the_next_run(trust):
    result = trust["gate"](evidence(status__privacy=404), AS_OF)
    block = trust["evidence_block"](result)
    assert block.startswith("```tin-buyer-trust\n") and block.endswith("\n```")
    payload = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert payload["verdict"] == "FAIL"
    assert payload["fixes"] == [{"id": "privacy", "owner": "founder", "rank": "fix_now"}]
    assert trust["gate"](payload["evidence"], AS_OF)["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"status__home": "200"}, "status.home"),
        ({"status__env": True}, "status.env"),
        ({"status__robots": 42}, "status.robots"),
        ({"status__http_redirects_to_https": "yes"}, "http_redirects_to_https"),
        ({"headers__hsts": None}, "headers.hsts"),
        ({"headers__csp": ["default-src"]}, "headers.csp"),
        ({"origin": "http://shop.example.com"}, "origin"),
        ({"origin": "https://shop.example.com/pricing"}, "origin"),
        ({"origin": "https://user:pw@shop.example.com"}, "origin"),
        ({"checkout__refund_url": "http://shop.example.com/refunds"}, "refund_url"),
        ({"checkout__guest_option": "true"}, "guest_option"),
        ({"checkout__refund_checked": "true"}, "refund_checked"),
        ({"checkout__refund_checked": 1}, "refund_checked"),
        ({"public_contact": "security@example.com"}, "public_contact"),
        ({"mixed_content": True}, "mixed_content"),
        ({"mixed_content": ["https://cdn.example.net/app.js"]}, "mixed_content"),
        ({"security_txt__expires": "next year"}, "ISO-8601"),
    ],
)
def test_plausible_but_unusable_evidence_is_rejected(trust, overrides, message):
    with pytest.raises(ValueError, match=message):
        trust["gate"](evidence(**overrides), AS_OF)


def test_unknown_and_missing_keys_are_rejected(trust):
    extra = evidence()
    extra["status"]["admin"] = 200
    with pytest.raises(ValueError, match="unsupported keys"):
        trust["gate"](extra, AS_OF)
    missing = evidence()
    del missing["headers"]["csp"]
    with pytest.raises(ValueError, match="missing keys"):
        trust["gate"](missing, AS_OF)


# Qualification cases


def test_cases_validate_against_the_manifest_and_include_an_unusable_target():
    schema = manifest()["input_schema"]
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        jsonschema.validate(
            {"project_id": "00000000-0000-4000-8000-000000000001", **case["inputs"]}, schema
        )
        assert case["expected_status"] == "succeeded"
        assert "CVE-" in case["expect"]["excludes"]
    unusable = next(case for case in cases if case["id"] == "unreachable_origin")
    assert unusable["inputs"]["product_url"].endswith(".invalid")
    assert "Verdict: UNVERIFIED" in unusable["expect"]["contains"]
    assert "Verdict: PASS" in unusable["expect"]["excludes"]
    secret = next(case for case in cases if case["id"] == "credentials_in_notes")
    assert "hunter2-trust" in secret["inputs"]["notes"]
    assert "hunter2-trust" in secret["expect"]["excludes"]
