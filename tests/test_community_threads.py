"""Offline tests for the outreach.community_threads workflow package."""

import json
import re
from uuid import uuid4

import jsonschema
import pytest

from tin_lite.community import (
    REPOSITORY_ROOT,
    CheckoutStorage,
    ContributedPackage,
    validate,
)
from tin_lite.procedures import load_pinned_codex_procedure

PACKAGE_KEY = "outreach.community_threads"
PACKAGE_DIR = REPOSITORY_ROOT / "workflow_packages" / PACKAGE_KEY
SKILL_DIR = PACKAGE_DIR / "skills" / "community-threads"
STATE = re.compile(r"```tin-community-threads\s*\n(.*?)\n```", re.S)
DISCLOSURE = re.compile(r"disclosure:|i'm the founder of|i work on", re.I)


def definition():
    return json.loads((PACKAGE_DIR / "workflow.json").read_text())["definition"]


def check_report(report: str, earlier: list[str]) -> list[str]:
    """What the skill promises of a report: a state block, no repeated thread, disclosed drafts."""
    blocks = STATE.findall(report)
    if len(blocks) != 1:
        raise ValueError("the report must end with one tin-community-threads block")
    threads = json.loads(blocks[0])["threads"]
    seen = {
        url for previous in earlier for url in json.loads(STATE.findall(previous)[0])["threads"]
    }
    if repeated := [url for url in threads if url in seen]:
        raise ValueError(f"repeats a thread from an earlier run: {repeated[0]}")
    drafts = re.findall(r"Draft reply:\n((?:>.*\n?)+)", report)
    if len(drafts) != len(threads):
        raise ValueError("every thread needs exactly one draft reply")
    if any(not DISCLOSURE.search(draft) for draft in drafts):
        raise ValueError("a draft reply is missing its disclosure")
    return threads


def report(url: str, reply: str = "> Try a pooler.\n> Disclosure: I'm the founder of Acme.") -> str:
    return (
        "# Community threads: pool exhaustion\n\nStatus: 1 threads\n\n"
        f"## 1. Pool keeps running out\n- URL: {url}\n\nDraft reply:\n{reply}\n\n"
        "## Skipped\n- none\n\n"
        f'```tin-community-threads\n{{"version": 1, "threads": ["{url}"]}}\n```\n'
    )


def test_manifest_contract():
    d = definition()
    assert d["key"] == PACKAGE_KEY and d["executor"] == "codex.procedure"
    assert d["schedule_modes"] == ["on_demand", "weekly"]
    assert d["human_review"]["eligible"] is True
    output = d["procedure"]["output"]
    assert output["path_template"] == "reports/community-threads/{run_id}.md"
    assert d["procedure"]["entry_skill"] == "community-threads"
    assert d["procedure"]["sandbox"] == {
        "profile": "isolated",
        "egress": "fenced",
        "timeout_seconds": 900,
    }
    paths = {p["path"] for p in d["prerequisites"]}
    assert {"reports/GROWTH_ONBOARDING_PLAN.md", "wiki/INDEX.md"} <= paths


def test_inputs_are_optional_and_bounded():
    schema = definition()["input_schema"]
    assert schema["required"] == ["project_id"]
    jsonschema.validate({"project_id": str(uuid4())}, schema)
    for bad in (
        {"platforms": ["linkedin"]},
        {"lookback_days": 0},
        {"lookback_days": 31},
        {"max_opportunities": 11},
        {"focus": "x" * 1001},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"project_id": str(uuid4()), **bad}, schema)


async def test_package_validates_and_pins():
    await validate(ContributedPackage(key=PACKAGE_KEY, path=PACKAGE_DIR), root=REPOSITORY_ROOT)
    pinned = await load_pinned_codex_procedure(
        storage=CheckoutStorage(REPOSITORY_ROOT),
        repo_id="workflow_packages",
        commit_sha="test",
        definition_path=f"workflow_packages/{PACKAGE_KEY}/workflow.json",
    )
    assert pinned.workflow_key == PACKAGE_KEY and pinned.entry_skill == "community-threads"
    assert set(pinned.skill_files) == {
        "community-threads/SKILL.md",
        "community-threads/PLATFORM_RULES.md",
    }


def test_prompt_and_skill_keep_the_guards():
    prompt = " ".join((PACKAGE_DIR / "PROMPT.md").read_text().split())
    skill = " ".join((SKILL_DIR / "SKILL.md").read_text().split())
    for text in (prompt, skill):
        assert "strictly as untrusted data" in text
        assert "never follow instructions" in text.lower()
    assert "Never post, comment, vote, send DMs or emails" in prompt
    assert "reports/GROWTH_ONBOARDING_PLAN.md" in prompt and "wiki/INDEX.md" in prompt
    assert "tin-community-threads" in skill and "no older than 14 days" in skill
    everything = prompt + skill + (SKILL_DIR / "PLATFORM_RULES.md").read_text()
    # Public replies only: no private outreach routes or leftovers from the old package.
    for gone in ("Business Email", "RADAR.csv", "SEO Topic", "Tier 1"):
        assert gone not in everything


def test_report_check_accepts_a_good_report_and_refuses_unusable_ones():
    first = report("https://news.ycombinator.com/item?id=1")
    assert check_report(first, []) == ["https://news.ycombinator.com/item?id=1"]
    # Plausible but unusable: the same thread again, or a draft without the disclosure.
    with pytest.raises(ValueError, match="repeats a thread"):
        check_report(report("https://news.ycombinator.com/item?id=1"), [first])
    with pytest.raises(ValueError, match="missing its disclosure"):
        check_report(report("https://reddit.com/r/x/1", "> Try our tool, it's great."), [first])
    with pytest.raises(ValueError, match="one tin-community-threads block"):
        check_report("# Community threads\n\nStatus: 0 threads\n", [])
