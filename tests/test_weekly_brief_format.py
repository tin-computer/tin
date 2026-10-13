from datetime import UTC, datetime, timedelta

import pytest

from tin_lite.weekly_brief import (
    WeeklyBriefProtocolError,
    WeeklyBriefReporter,
    WeeklyBriefSource,
    validate_weekly_brief_artifacts,
)


class Responses:
    model = "fixture-model"

    def __init__(self, text):
        self.text, self.calls = text, 0

    async def create(self, payload):
        self.calls += 1
        return {
            "id": "resp_fixture",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": self.text}]}
            ],
        }


@pytest.mark.parametrize(
    "body",
    [
        "The scan finished. The demo is not reviewed.",
        "## This week\n\nThe scan finished.",
        "# Weekly summary\n\nThe scan finished.",
    ],
)
async def test_heading_format_does_not_discard_a_usable_summary(body):
    response = Responses(body)
    reporter = WeeklyBriefReporter(responses=response, skill_suite="Use only recorded results.")
    end = datetime(2026, 10, 12, tzinfo=UTC)
    source = WeeklyBriefSource("run summary", "fixture://report", "The scan finished.")
    result = await reporter.report(
        project_name="Test product",
        period_start=end - timedelta(days=7),
        period_end=end,
        inputs={},
        sources=[source],
    )
    expected = body if body.startswith("# ") else f"# Weekly brief\n\n{body}"
    assert result["markdown"].split("\n\n## Sources", 1)[0] == expected
    assert body in result["markdown"] and result["source_refs"] == [source.artifact_ref]
    docs = reporter.build_artifacts(
        run_id="fixture-run",
        artifact_path="reports/weekly.md",
        evidence_path="reports/weekly/evidence.json",
        result=result,
    )
    validate_weekly_brief_artifacts(
        *docs, artifact_path="reports/weekly.md", evidence_path="reports/weekly/evidence.json"
    )
    assert response.calls == 1


async def test_sources_only_is_not_a_summary():
    reporter = WeeklyBriefReporter(
        responses=Responses("\n## Sources\n- invented"), skill_suite="Use recorded results."
    )
    end = datetime(2026, 10, 12, tzinfo=UTC)
    with pytest.raises(WeeklyBriefProtocolError, match="no summary"):
        await reporter.report(
            project_name="Test product",
            period_start=end - timedelta(days=7),
            period_end=end,
            inputs={},
            sources=[],
        )
