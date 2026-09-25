# Migration Battlecard Qualification

The versioned test cases in `qualification.json` specify the input boundaries, behavioral expectations, and structural requirements for `growth.migration_battlecard`.

## Philosophy

A competitor migration battlecard must never rely on subjective marketing hyperbole or hand-waving comparisons. This qualification suite verifies:

1. **Standard Displacement (`ordinary`):** Evaluates a canonical B2B SaaS displacement (e.g. PostHog vs Mixpanel). Validates that all six required sections are generated: Executive Summary, Friction Taxonomy Analysis, Parity Matrix with honest competitor advantages, 4-Stage Zero-Downtime Runbook, Economic Payback Model, and Objection Counter-Playbook.
2. **High-Complexity Infrastructure (`high_complexity`):** Evaluates an enterprise infrastructure migration (e.g. OpenTelemetry vs Datadog) where stateful dual-write ingestion, schema mapping, and delta reconciliation must be explicitly detailed.
3. **Negative Input Rejection (`invalid_missing_competitor`):** Asserts that missing mandatory inputs (such as an empty competitor name string) trigger validation failure before procedure execution.

## Offline Verification

All economic payback formulas embedded in `skills/migration-battlecard/RUBRIC.md` are executed and verified offline in `tests/test_migration_battlecard.py`. No live model calls or paid APIs are invoked during automated CI qualification.
