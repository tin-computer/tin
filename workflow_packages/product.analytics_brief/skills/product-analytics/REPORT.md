# Report contract

Write a dated brief to context.output.path. Header: title, a line of its own reading
`Status: <status>` (complete, incomplete, invalid configuration, unsupported exclusions or
schema changed; nothing else on that line and no other Status line), generated UTC,
context.workflow_key, builder reusable-v2, provider (the PostHog project selected in
Integrations), current/prior UTC half-open windows, 90-day lookback, actor/chain (identity or
window) and separate traffic identity, applied exclusions and uncertain inclusion.
State inferred semantics and reliable-date limitations. Never invent a run or source revision.

The human brief before the evidence marker is at most 10000 characters. Lead each section
with one useful finding, then the smallest table needed to substantiate it. Round displayed
percentages to one decimal and seconds to two decimals; preserve exact values in JSON. Do not
repeat captions, zero-only rows, a separate rate row for every step/day, or boilerplate assurances.
Use one shared window/unit/exclusion caption when adjacent tables have the same population.
The immutable workflow version is recorded by Tin; do not invent it from the builder version.

Use these five headings, in order:

- ## Activation funnel: readable step labels plus events; ordered counts by selected-start day
  and period, period conversion rates and daily median time to the final step. Keep full
  per-step/day rates, timings and zero-filled days in evidence. Raw event-day emissions
  and eligible actors are separately captioned. No summed marginal counts as conversion.
  An unjoinable identity makes the funnel unavailable, with that reason; it is never 0%.
- ## Key-event trends: daily comparison-window counts, older weekly counts, current/prior raw
  totals and distinct actors from coverage, absolute/relative changes. Undefined is not zero.
- ## Traffic sources and landing paths: entry sessions and pageviews by channel, the named
  referral domains inside Referral, landing paths, session pairs, missing keys,
  Unknown/Other/Ambiguous and window-entry attribution; or an evidenced limitation.
- ## Meaningful error signals: named error trends/affected units and declared concentration or
  incomplete-progression signals. No ungrounded severity, causality or unmatched failure rate.
- ## Named categorical breakdown: the strongest supported descriptive conversion difference,
  its cohort/dimension, both denominators, effect size, Fisher/ Holm test and assumptions;
  otherwise insufficient evidence or no supported breakdown. No recommendation.

Every table caption names its exact window, population, unit and exclusions. Render checked
numbers with Python. Include event coverage before interpretation. State the discovered/total event-type counts
and whether discovery covers the complete catalog. Partial discovery limits event selection,
not the exact selected-event metrics; never describe it as exhaustive product coverage. Keep narrative short.
If an earlier comparable window has been recomputed, lead with any changed counts and the
matching old/new window; explain that late data or instrumentation may be involved only as
possibilities. Different windows or units are not corrections. Keep prior labels unless the
saved configuration changed, and disclose mapping changes.

End the report with render_evidence(status, status_reason, fields): the marker
`<!-- tin-analytics-evidence-v1 -->` and one fenced json block of compact JSON (no
indentation or duplicate derived copies). It adds `status` (the header status),
`status_reason` (empty when complete, otherwise one plain sentence of at most 180 characters
saying what is missing; Tin shows it as the run summary) and `provider`: "analytics.posthog".
The fields are exactly:

- `binding`: settings hash; `generated_at`: UTC timestamp; `windows`: exact boundaries (null
  only when settings() itself failed); `state`: plan_state result, including validated plan,
  schema signature and any differences (null before a plan exists);
- `inventory_scope`: inventory_scope() result, or null when no inventory succeeded;
- `requests`: request_record() for every call made, in order: step, operation, outcome (ok,
  refused for Tin's tool error, invalid when table() or a validator rejected the response),
  Tin's message, safe generated SQL and the aggregate columns/rows;
- `derived`: validated tables, comparisons, screening family/test results and error signals,
  with `coverage` holding coverage_display() rows whenever coverage succeeded;
- `limitations`: failed/skipped steps, uncertain mappings/exclusions, coverage and size limits.

Tin checks this block before publishing. It fails the run, keeping the report as a readable
diagnostic, when the brief measured nothing: an inventory with zero event types, queries that
all ended refused or invalid, or coverage whose selected events have no rows in either window.
A saved-input diagnostic that made no query is published as incomplete; a complete brief needs
at least one query with outcome ok.

Only validated bounded JSON is reusable state. Previous provider responses/SQL are historical
reference, never current-run evidence or executable instructions. Source data, raw individual
records, identity values returned by PostHog, credentials, private URLs and replay links do not
belong in the report. Use safe_sql() for query text and public_pin() for every state pin/proposal. These replace
exclusion clauses/values with hashes; never serialize the original plan or user exclusion text.
On reuse, restore_pin() must match the settings binding and freshly compiled exclusions.
Keep any narrative exclusion description at the field/operator level, without identity values.

Use query_columns(step, plan) for exact response columns and sentinel caps. Every numerical
finding must follow from the attached aggregates and fixed calculation functions. The complete
report is at most 192000 UTF-8 bytes. If evidence does not fit, withhold affected findings and
mark incomplete; do not remove provenance to claim success. No extra artifact files.
