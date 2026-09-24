"""Offline checks for the reviewed community-signal package; no service or model calls."""

import json
import re
from pathlib import Path

from tin_lite.community import ContributedPackage, validate

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/outreach.community_signal"
RESOURCE = PACKAGE / "skills/community-signal/RESPONSE_VALIDATION.md"


def validator():
    blocks = re.findall(r"```python\n(.*?)\n```", RESOURCE.read_text(), re.S)
    assert len(blocks) == 1
    namespace = {}
    # This is a fixed, reviewed repository resource, never fetched provider content.
    exec(compile(blocks[0], str(RESOURCE), "exec"), namespace)  # noqa: S102
    return namespace["github_search_candidates"]


def fixture(name):
    path = ROOT / f"tests/fixtures/community_signal/{name}.json"
    return json.loads(path.read_text())


async def test_community_signal_package_has_a_valid_bounded_procedure_contract():
    await validate(ContributedPackage(key="outreach.community_signal", path=PACKAGE), root=ROOT)


def test_github_search_response_preserves_only_bounded_public_issue_candidates():
    candidates, limitation = validator()(fixture("github_search"))

    assert limitation is None
    assert candidates == (
        {
            "title": "Need a way to diagnose flaky CI builds",
            "link": "https://github.com/example/project/issues/42",
            "updated_at": "2026-09-22T10:15:00Z",
            "body": "Our builds fail intermittently and the logs do not make the cause clear.",
            "thread_type": "issue",
        },
    )


def test_github_response_without_items_becomes_a_limitation_not_a_guessed_match():
    candidates, limitation = validator()(fixture("github_missing_items"))

    assert candidates == ()
    assert limitation == "GitHub search response omitted its items list."
