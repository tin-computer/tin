"""Crawler warnings use each sampled path's recorded robots policy."""

import pytest
from test_organic_audit_angles import BASE, CONTENT, facts, found, html_page, ids, view

from tin_lite.organic_audit_checks import site_findings
from tin_lite.organic_audit_site import parse_robots


def access(robots, denied, *, paths=("/",), current=True):
    rows = []
    for path in paths:
        rows.append(
            {"url": BASE + path, "agent": "browser", "status": "observed", "status_code": 200}
        )
        rows.extend(
            {"url": BASE + path, "agent": agent, "status": "refused", "status_code": 403}
            for agent in denied
        )
    site = view(
        [facts(path, html_page(title="Product information", body=CONTENT)) for path in paths],
        files={"robots": robots},
        access={"status": "observed", "rows": rows},
    )
    site.crawler_access_policy = current
    return site_findings(site, home=BASE + "/", pagespeed={})


@pytest.mark.parametrize("agent", ["GPTBot", "ClaudeBot", "CCBot"])
def test_intentionally_disallowed_training_crawler_is_not_an_access_problem(agent):
    robots = {
        "status": "observed",
        **parse_robots(f"User-agent: *\nAllow: /\n\nUser-agent: {agent}\nDisallow: /\n"),
    }
    assert "access.ai_crawlers_refused" not in ids(access(robots, [agent], paths=("/", "/docs")))


def test_blocked_search_robots_rule_is_not_a_second_firewall_warning():
    robots = {
        "status": "observed",
        **parse_robots("User-agent: *\nAllow: /\n\nUser-agent: OAI-SearchBot\nDisallow: /\n"),
    }
    findings = access(robots, ["OAI-SearchBot"])
    assert "access.ai_crawlers_refused" not in ids(findings)
    assert "robots.ai_search_crawlers_blocked" in ids(findings)


def test_access_warning_matches_the_actual_allowed_path_only():
    robots = {
        "status": "observed",
        **parse_robots("User-agent: *\nAllow: /\n\nUser-agent: GPTBot\nDisallow: /private\n"),
    }
    result = found(
        access(robots, ["GPTBot"], paths=("/private", "/public")), "access.ai_crawlers_refused"
    )
    assert result["urls"] == [BASE + "/public"]
    assert "1 page" in result["evidence"][0]


@pytest.mark.parametrize("status", ["unavailable", "refused", "not_collected"])
def test_unknown_robots_does_not_claim_the_crawler_is_allowed(status):
    result = found(access({"status": status}, ["OAI-SearchBot"]), "access.ai_crawlers_refused")
    assert "robots.txt allows" not in result["issue"]
    assert any("unknown" in line.lower() for line in result["evidence"])
    assert result["confidence"] == "likely"


def test_real_allowed_search_refusal_survives_but_denied_training_is_excluded():
    robots = {
        "status": "observed",
        **parse_robots("User-agent: *\nAllow: /\n\nUser-agent: GPTBot\nDisallow: /\n"),
    }
    result = found(access(robots, ["GPTBot", "OAI-SearchBot"]), "access.ai_crawlers_refused")
    assert result["impact"] == "high"
    assert result["evidence"][0].startswith("OAI-SearchBot:")
    assert not any(line.startswith("GPTBot:") for line in result["evidence"])


def test_allowed_training_refusal_does_not_ask_to_unblock_search_crawlers():
    robots = {"status": "observed", **parse_robots("User-agent: *\nAllow: /\n")}
    result = found(access(robots, ["GPTBot"]), "access.ai_crawlers_refused")
    assert "training" in result["fix"].lower()
    assert "Allow the AI search crawlers" not in result["fix"]


def test_missing_robots_file_is_an_allow_not_an_unknown():
    result = found(access({"status": "missing"}, ["OAI-SearchBot"]), "access.ai_crawlers_refused")
    assert "robots.txt allows" in result["issue"]
    assert not any("unknown" in line.lower() for line in result["evidence"])


def test_older_pinned_policy_keeps_its_recorded_finding():
    robots = {
        "status": "observed",
        **parse_robots("User-agent: *\nAllow: /\n\nUser-agent: GPTBot\nDisallow: /\n"),
    }
    result = found(access(robots, ["GPTBot"], current=False), "access.ai_crawlers_refused")
    assert result["issue"] == "The site likely blocks AI crawlers that robots.txt allows"
