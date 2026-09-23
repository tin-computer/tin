# Add `community.opportunity_scan` workflow package

## What it changes
Adds a contributed Codex workflow that turns a project's existing business context into a bounded, reviewable scan of public community conversations where the target audience is discussing the relevant problem.

The workflow:
- searches for concrete public discussions rather than generic community directories;
- verifies community participation rules from first-party guidance when available;
- records conversation and rules URLs plus dates when available;
- suggests a helpful participation mode and response angle;
- explicitly avoids automatic posting, unsolicited outreach, moderation evasion, or fabricated evidence.

## Why this workflow
Tin already covers search/SEO, content planning, email outreach, product research/QA, and paid acquisition. This package covers a different distribution unit: finding specific community conversations and determining whether participation is allowed and useful.

## Output
`reports/COMMUNITY_OPPORTUNITY_SCAN.md`

The artifact is review-first. It does not send messages or publish anything.

## Verification
- `uv run tin-lite validate-community`
- `uv run pytest tests/test_community_opportunity_scan.py`
- Full repository checks: `uv run ruff format --check .`, `uv run ruff check .`, `uv run lint-imports`, `uv run pytest`

## Limitations
The package is intentionally unregistered. A maintainer must select it for the public Registry. Live provider/community research is separate from static package validation and should be evaluated with authorized resources before activation.
