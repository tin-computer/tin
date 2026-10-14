"""A content plan for a large site publishes instead of failing after its paid work.

A content.plan run on a site with 166 sitemap pages read 177 of them and built a 709 KB
evidence.json, which the 400 KB publication limit refused once every model call was done.
The limits are runaway guards now, and a refusal names the file and its size.
"""

import inspect

import pytest

from tin_lite import content_plan_activities, content_plan_agent
from tin_lite.organic_audit_publication import publish_artifacts


def test_limits_sit_well_above_a_large_site():
    source = inspect.getsource(content_plan_activities.ContentPlanActivities.publish)
    assert '"evidence.json": 4_000_000' in source
    assert '"PLAN.md": 1_000_000' in source
    assert content_plan_agent.BRIEF_FILES["research.json"] >= 4_000_000
    assert content_plan_agent.BRIEF_FILES["context.json"] >= 1_000_000


async def test_refusal_names_the_file_and_its_size():
    path = "reports/content-plan/r/evidence.json"
    with pytest.raises(ValueError, match=rf"{path} is 11 bytes; publication allows 1 to 10\."):
        await publish_artifacts(
            storage=None,
            repo_id="repo",
            branch="main",
            documents={path: b"x" * 11},
            paths={"evidence.json": path},
            limits={"evidence.json": 10},
            message="content.plan r",
            intent=None,
            save_intent=None,
            validate_active=None,
        )
