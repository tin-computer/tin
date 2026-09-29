"""Offline unit tests for the content.release_announce contributed workflow package.

Validates the manifest contract, three-step model sequence, grounding constraints,
per-channel limits and output rendering without making paid supplier API calls.
"""

from __future__ import annotations

import json
import runpy
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import CodeModelError, model_terms, request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

PACKAGE_KEY = "content.release_announce"
PROJECT_ID = "a0000000-0000-0000-0000-000000000001"

CHANGELOG = (
    "# Release 2.4.0\n"
    "---\n"
    "- Added CSV export for customer analytics\n"
    "- Fixed bug causing session timeout on Safari 17\n"
    "- Refactored database connection pool internals\n"
)

EXTRACTED = {
    "changes": [
        {
            "id": 1,
            "summary": "CSV export for customer analytics",
            "category": "feature",
            "user_facing": True,
        },
        {
            "id": 2,
            "summary": "Resolved session timeout bug on Safari 17",
            "category": "fix",
            "user_facing": True,
        },
        {
            "id": 3,
            "summary": "Internal refactor of database pool",
            "category": "internal",
            "user_facing": False,
        },
    ]
}

NEWSLETTER = {
    "covered_ids": [1, 2],
    "headline": "CSV export and a Safari fix",
    "email_subject": "New in 2.4.0: CSV export for analytics",
    "email_body": (
        "Hi there,\n\nYou can now export customer analytics to CSV.\n\n"
        "We also fixed the Safari 17 timeout.\n\nTry the export today."
    ),
}

SOCIAL = {
    "covered_ids": [1, 2],
    "x_post": "v2.4 is out: export customer analytics to CSV, and no more Safari 17 timeouts.",
    "x_thread": [
        "CSV export: pick a date range in analytics and download every customer row.",
        "Safari 17 users no longer get signed out mid-session.",
    ],
    "linkedin_opening": "Tin Computer 2.4.0 lets you export customer analytics to CSV.",
    "linkedin_body": (
        "Pick a date range and download every customer row.\n\n"
        "We also fixed a Safari 17 session timeout.\n\nTry the export and tell us what you "
        "think.\n\n#analytics #release"
    ),
    "reddit_title": "Tin Computer 2.4.0 adds CSV export for customer analytics",
    "reddit_body": (
        "I work on Tin Computer. This release adds CSV export for customer analytics and "
        "fixes a Safari 17 session timeout.\n\nThe export covers one date range at a time. "
        "Would a scheduled export help you?"
    ),
    "hn_title": "Show HN: Tin Computer now exports customer analytics to CSV",
    "hn_comment": (
        "I work on Tin Computer. This release adds CSV export for customer analytics and fixes "
        "a Safari 17 timeout. Feedback on the export format would help."
    ),
}

CREATED_AT = "2026-09-28T17:00:00+00:00"


def load_package():
    root = REPOSITORY_ROOT / "workflow_packages" / PACKAGE_KEY
    definition = json.loads((root / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(root / "main.py")))
    return module, definition


class FakeContext(dict):
    """The sandbox context: run facts by key, managed models as an attribute."""

    def __init__(self, models):
        super().__init__(run_id="run-1", created_at=CREATED_AT)
        self.models = models


def fake_models(spec, *, extracted=EXTRACTED, newsletter=NEWSLETTER, social=SOCIAL, calls=None):
    calls = [] if calls is None else calls
    outputs = {
        "extract_changes": extracted,
        "write_newsletter": newsletter,
        "write_social_posts": social,
    }

    async def generate(**payload):
        request_contract(spec, payload)
        calls.append(payload)
        output = outputs[payload["step"]]
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    return FakeContext(SimpleNamespace(generate=generate))


def created():
    from datetime import datetime

    return datetime.fromisoformat(CREATED_AT)


def inputs(**overrides):
    return {
        "project_id": PROJECT_ID,
        "changelog": CHANGELOG,
        "product_name": "Tin Computer",
        **overrides,
    }


def test_package_contract_and_estimate():
    _, definition = load_package()
    spec = validate_code_definition(definition)
    assert spec.entrypoint == "main.py"
    assert spec.output_path == "content/releases/{date}-{slug}.md"
    assert spec.media_type == "text/markdown"
    assert spec.max_bytes == 32000
    assert {r.name for r in spec.model_routes} == {"extract", "announce", "social"}
    assert definition["version"] == "1.2.0"
    assert {r.model for r in spec.model_routes} == {"gpt-6-luna"}
    assert model_terms(definition)["maximum_nanos"] > 0
    properties = definition["input_schema"]["properties"]
    for name, schema in properties.items():
        if schema.get("type") == "string" and name != "project_id" and "enum" not in schema:
            assert schema["maxLength"] <= 12000
    assert {p["producer"] for p in definition["prerequisites"]} == {
        "style.capture",
        "growth.onboarding_plan",
    }
    assert all(p["level"] == "recommended" for p in definition["prerequisites"])


@pytest.mark.parametrize("tone", ["professional", "enthusiastic"])
async def test_happy_path_three_model_steps_and_report_rendering(tone):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    calls = []
    ctx = fake_models(spec, calls=calls)

    result = await module.run(
        ctx,
        inputs(
            audience="Founders and growth engineers",
            tone=tone,
            voice_notes="Short sentences. No exclamation marks.",
            release_url="https://tin.example/changelog/2-4-0",
        ),
    )
    assert [c["step"] for c in calls] == [
        "extract_changes",
        "write_newsletter",
        "write_social_posts",
    ]
    brief = calls[1]["data"]
    # Only announceable changes reach the copywriters, and user text stays data.
    assert [c["id"] for c in brief["changes"]] == [1, 2]
    assert brief["voice_notes"] == "Short sentences. No exclamation marks."
    assert calls[2]["data"]["has_release_link"] is True
    assert all("Tin Computer" not in call["instructions"] for call in calls)

    content = result["content"]
    assert result["path"] == (
        "content/releases/2026-09-28-tin-computer-csv-export-and-a-safari-fix.md"
    )
    assert content.startswith("# Tin Computer release: CSV export and a Safari fix\n")
    for heading in ("## X", "### Thread", "## LinkedIn", "## Reddit", "## Hacker News"):
        assert f"\n{heading}\n" in content
    assert "## Newsletter email" in content
    assert "**Title:** Show HN: Tin Computer now exports customer analytics to CSV" in content
    assert "**URL:** https://tin.example/changelog/2-4-0" in content
    assert "It is not a cold outreach email, and Tin does not send it." in content
    assert "1 internal change(s) left out of the announcements." in content
    assert "Not mentioned in the drafts below" not in content
    # X, LinkedIn, Reddit, Hacker News URL line and the newsletter each carry the link.
    assert content.count("https://tin.example/changelog/2-4-0") == 5
    assert f"*{len(SOCIAL['x_post']) + 24} of 280 characters, counting the link as X does*" in (
        content
    )
    assert f"of 3000 characters. The opening line is {len(SOCIAL['linkedin_opening'])}" in content
    assert f"*{len(SOCIAL['hn_title'])} of 80 characters*" in content
    assert f"Tone: {tone}" in content
    validate_code_result(json.dumps(result).encode(), spec, created_at=created())


async def test_internal_category_is_never_announced_even_if_marked_user_facing():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    extracted = {
        "changes": [
            {**EXTRACTED["changes"][0]},
            {**EXTRACTED["changes"][2], "user_facing": True},
        ]
    }
    newsletter, social = only_csv()
    calls = []
    ctx = fake_models(spec, extracted=extracted, newsletter=newsletter, social=social, calls=calls)
    await module.run(ctx, inputs())
    assert [c["id"] for c in calls[1]["data"]["changes"]] == [1]
    assert [c["id"] for c in calls[2]["data"]["changes"]] == [1]


async def test_uncovered_change_is_flagged_for_the_reviewer():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    newsletter, social = only_csv()
    result = await module.run(fake_models(spec, newsletter=newsletter, social=social), inputs())
    assert (
        "Not mentioned in the drafts below:\n- Resolved session timeout bug on Safari 17"
        in (result["content"])
    )


@pytest.mark.parametrize(
    "bad_changes,error_match",
    [
        (
            [{"id": 99, "summary": "Hallucinated", "category": "feature", "user_facing": True}],
            "out of range",
        ),
        (
            [
                {"id": 0, "summary": "Duplicate 1", "category": "feature", "user_facing": True},
                {"id": 0, "summary": "Duplicate 2", "category": "feature", "user_facing": True},
            ],
            "duplicate",
        ),
        (
            [{"id": 0, "summary": "Refactor", "category": "internal", "user_facing": False}],
            "No user-facing changes found",
        ),
        (
            [{"id": 0, "summary": "   ", "category": "feature", "user_facing": True}],
            "blank",
        ),
        (
            [{"id": 0, "summary": "x" * 200, "category": "feature", "user_facing": True}],
            "cut off",
        ),
    ],
)
async def test_extraction_validation_rejects_unusable_results(bad_changes, error_match):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    calls = []
    ctx = fake_models(spec, extracted={"changes": bad_changes}, calls=calls)
    with pytest.raises(ValueError, match=error_match):
        await module.run(ctx, inputs(changelog="- Line zero change"))
    assert len(calls) == 1  # Never proceeds to the announce step on a bad extraction.


@pytest.mark.parametrize(
    "override,error_match",
    [
        # Plausible, well-formed copy that invents a performance claim.
        (
            {"x_post": "Version 2.4.0: CSV export, and pages now load 3x faster."},
            "numbers not in the changelog: 3",
        ),
        ({"linkedin_body": "Read more at https://evil.example"}, "must not invent links"),
        ({"covered_ids": [1, 3]}, r"not supplied: \[3\]"),
        ({"covered_ids": [1, 1]}, "must not repeat"),
        ({"reddit_body": "y" * 4000}, "cut off"),
        ({"hn_comment": "   "}, "blank"),
        ({"x_post": "CSV export is here #a #b #c"}, "x_post carries 3 hashtags"),
        ({"reddit_body": "CSV export for everyone #release"}, "reddit_body carries 1 hashtags"),
        ({"x_thread": ["Safari fix #bugfix"]}, r"x_thread\[1\] carries 1 hashtags"),
        ({"linkedin_opening": "CSV export " * 20}, "shows before 'see more'"),
        ({"hn_title": "Tin Computer now exports analytics to CSV"}, "must start with 'Show HN: '"),
        ({"hn_title": "Show HN: " + "CSV export for Tin Computer " * 3}, "under 80"),
        ({"hn_title": "Show HN: Export customer analytics to CSV"}, "name the product"),
    ],
)
async def test_social_validation_rejects_unusable_posts(override, error_match):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    ctx = fake_models(spec, social={**SOCIAL, **override})
    with pytest.raises(ValueError, match=error_match):
        await module.run(ctx, inputs())


@pytest.mark.parametrize(
    "override,error_match",
    [
        ({"email_body": "Now 10x faster."}, "numbers not in the changelog: 10"),
        ({"email_subject": "   "}, "blank"),
        ({"email_body": "y" * 4000}, "cut off"),
        ({"covered_ids": [3]}, r"not supplied: \[3\]"),
    ],
)
async def test_newsletter_validation_rejects_unusable_copy(override, error_match):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    calls = []
    ctx = fake_models(spec, newsletter={**NEWSLETTER, **override}, calls=calls)
    with pytest.raises(ValueError, match=error_match):
        await module.run(ctx, inputs())
    assert [c["step"] for c in calls] == ["extract_changes", "write_newsletter"]


async def test_x_post_must_fit_with_the_appended_link():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    ctx = fake_models(spec, social={**SOCIAL, "x_post": "CSV export " * 25})
    with pytest.raises(ValueError, match="max 280"):
        await module.run(ctx, inputs(release_url="https://tin.example/r"))


async def test_a_single_change_can_skip_the_thread():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    newsletter, social = only_csv()
    result = await module.run(
        fake_models(spec, newsletter=newsletter, social={**social, "x_thread": []}), inputs()
    )
    assert "### Thread" not in result["content"]
    assert "**URL:** add a page people can try" in result["content"]


def test_named_output_must_match_the_run_date():
    from datetime import datetime

    _, definition = load_package()
    spec = validate_code_definition(definition)
    result = {"path": "content/releases/2026-09-27-x.md", "content": "# Release\n"}
    with pytest.raises(ValueError, match="declared path"):
        validate_code_result(json.dumps(result).encode(), spec, created_at=created())
    result["path"] = "content/releases/2026-09-28-x.md"
    assert validate_code_result(
        json.dumps(result).encode(), spec, created_at=datetime.fromisoformat(CREATED_AT)
    )


def only_csv():
    newsletter = {
        **NEWSLETTER,
        "covered_ids": [1],
        "headline": "CSV export",
        "email_body": "You can now export customer analytics to CSV.",
    }
    social = {
        **SOCIAL,
        "covered_ids": [1],
        "x_post": "Export customer analytics to CSV.",
        "x_thread": [],
        "linkedin_body": "You can now export customer analytics to CSV.",
        "reddit_body": "I work on Tin Computer. You can now export customer analytics to CSV.",
        "hn_comment": "I work on Tin Computer. You can now export customer analytics to CSV.",
    }
    return newsletter, social


@pytest.mark.parametrize(
    "url", ["http://tin.example/r", 'https://tin.example/r" onclick="x', "javascript:alert(1)"]
)
async def test_release_url_must_be_a_plain_https_link(url):
    module, _ = load_package()
    with pytest.raises(ValueError, match="plain https"):
        await module.run(SimpleNamespace(), inputs(release_url=url))


async def test_oversized_changelog_fails_clearly_before_any_model_call():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    changelog = "\n".join(f"- Changed ünïcödé thing number {i}" for i in range(400))
    calls = []
    with pytest.raises(ValueError, match="too long for one run"):
        await module.run(fake_models(spec, calls=calls), inputs(changelog=changelog[:12000]))
    assert calls == []


async def test_size_precheck_admits_what_the_runtime_admits():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    # A long but admissible changelog must pass both the precheck and the runtime contract.
    changelog = "\n".join(f"- Improved export option {i} for analytics" for i in range(150))
    numbered = [{"id": i, "text": line} for i, line in enumerate(changelog.splitlines())]
    module._check_request_size(module.EXTRACT_INSTRUCTIONS, numbered, module.EXTRACTION, "x")
    request_contract(
        spec,
        {
            "route": "extract",
            "step": "extract_changes",
            "instructions": module.EXTRACT_INSTRUCTIONS,
            "data": numbered,
            "output_schema": module.EXTRACTION,
        },
    )
    too_big = numbered * 4
    with pytest.raises(ValueError):
        module._check_request_size(module.EXTRACT_INSTRUCTIONS, too_big, module.EXTRACTION, "x")
    with pytest.raises(CodeModelError):
        request_contract(
            spec,
            {
                "route": "extract",
                "step": "extract_changes",
                "instructions": module.EXTRACT_INSTRUCTIONS,
                "data": too_big,
                "output_schema": module.EXTRACTION,
            },
        )


@pytest.mark.parametrize("empty_input", ["", "   ", "\n---\n===\n***", "--- \n === \n ~~~"])
async def test_split_changelog_rejects_empty_or_decorative_content(empty_input):
    module, _ = load_package()
    with pytest.raises(ValueError, match="Changelog contains no meaningful content"):
        await module.run(SimpleNamespace(), inputs(changelog=empty_input))


async def test_a_long_headline_is_cut_so_the_heading_stays_short():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    newsletter = {
        **NEWSLETTER,
        "headline": "CSV export for customer analytics, a Safari 17 session fix, faster exports",
    }
    result = await module.run(fake_models(spec, newsletter=newsletter), inputs())
    heading = result["content"].split("\n", 1)[0]
    assert (
        heading
        == "# Tin Computer release: CSV export for customer analytics, a Safari 17 session fix"
    )
    assert len(heading) - 2 <= 80
    assert result["path"] == (
        "content/releases/2026-09-28-tin-computer-csv-export-for-customer-analytics-a-safari-17-"
        "session-fix.md"
    )
