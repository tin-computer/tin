# Keyword planning

## ELI5

Research what relevant buyers search for, check a bounded sample of search results, and
save an evidence-backed inventory of content opportunities. This is input to a later content
planner, not an editorial calendar or permission to write, publish, or contact anyone.

Implementation scope approved September 8, 2026. Deployment and paid acceptance are recorded
below; the initial pilot failed and is preserved as history.

## Reliability correction — version 0.2

The corrective scope is limited to `organic.keyword_plan`; the audit, Studio, shared model
adapter, Temporal engine, and output-publication boundaries are unchanged. The v1 policy,
instructions and schemas remain executable for old pinned runs. New definitions pin
`keyword-plan-v2`; no failed run is relabelled, edited or silently retried.

- Use concise lookup seeds and DataForSEO Keyword Suggestions, which matches the seed words,
  instead of broad category-based Keyword Ideas. Keep Related Keywords as bounded expansion.
- Restrict competitor footprints to the seeds using escaped literal matching, including a
  deterministic post-filter. Large search-overlap domains are not presumed commercial rivals.
- Preserve proposed seeds separately from measured provider observations, with unknown metrics
  and explicit user/model provenance; a database miss is not a zero-volume measurement.
- Screen every retained candidate for the stated buyer before choosing SERP samples. Only
  direct and meaningfully adjacent intents are sampled, direct first. Ranking on the target
  site, sharing a platform, and generic industry relevance are not sufficient buyer fit.
- Use one required, short slot per candidate in model schemas. Shared enum definitions keep
  the 300-slot schema within provider limits. Tin expands slot assignments into unchanged stable
  keyword IDs, group/exclusion records, and full exact-assignment validation. Missing slots,
  unknown/unused/duplicate groups, wrong primaries and ungrounded high-priority adjacent groups
  still fail closed. No guess-based repair or default inclusion fills a missing assessment.
- Pass actual bounded existing-page URLs to the reviewer, not just an existence boolean.
  They remain pages to inspect, never a claim that their content was read.
- At most three receipted model calls: optional seeds, buyer screening, final review. Reserve
  screening and review funding before research. A saved or uncertain call is never repurchased.
  A safe workflow-specific message distinguishes invalid model assessment from unavailable
  model response, without leaking provider payloads or credentials.

Source contracts: [Keyword Suggestions](https://docs.dataforseo.com/v3/dataforseo_labs/google/keyword_suggestions/live/),
[Ranked Keywords filters](https://docs.dataforseo.com/v3/dataforseo_labs/google/ranked_keywords/live/),
and [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The OpenAI Docs skill informed required-property and shared-enum schema design; semantic buyer
fit and cross-references are still checked separately. The file handoff keeps the same three
paths and group/keyword IDs, adds per-candidate `buyer_fit` and coverage counts, and marks the
schema version explicitly. No new table, executor, model route, UI, or consumer workflow.

## Contract

- Native, on-demand `organic.keyword_plan`; independent from `organic.audit`, MCP-first.
- Required: exact HTTPS website origin, English-language buyer context, and one supported
  market (US, GB, CA, AU). Optional seeds, competitor hosts, exact audit run, and Search Console.
- An explicit per-run dollar limit is bounded by the switchboard ceiling, which defaults to
  zero. The existing DataForSEO and native model credentials stay on the switchboard.
  Enable `TIN_LITE_KEYWORD_PLAN_MAX_COST_USD` with a value from `2` to `25`; the effective
  limit is the lower of this setting and the run's limit (default `2` for policy v6; runs
  pinned to v5 or earlier need at least `5`). No environment or production spending
  configuration is changed by this implementation.
- Up to eight seeds, three competitor footprints, 300 selected keyword candidates, and 40
  live top-ten SERP samples. No universal difficulty/volume rejection rules, cadence promises,
  category-name viability verdict, backlink API, calendar, rescore mode, or new table.
- At most three model calls: propose missing seeds, screen buyers, then group/review observations.
  Founder seeds can avoid the first call. Models cannot create provider observations, modify
  metrics, or assign a candidate more than once. All candidates receive a group or exclusion.
- Search results are sampled, not a complete survey. Unsampled terms remain explicitly so.
  Grouping is a reviewable model judgment, supported where possible by measured URL overlap;
  overlap is not a hard same-page rule. Existing URLs are candidates for inspection, not proof
  that a page adequately answers the query or needs editing.
- Keep keyword spelling, punctuation, and word order. Normalize case/whitespace for deduplication
  only. Missing volume remains unknown; paid competition and organic difficulty stay distinct.

## Research

1. Pin scope, optional audit evidence, optional matching Search Console property, instructions,
   schemas, and spending limit before external calls.
2. Read bounded target/search-competitor footprints and expand buyer-context seeds through
   Keyword Suggestions and Related Keywords. Retain seed metrics through Keyword Overview;
   retain unmeasured seed proposals with explicit provenance and unknown metrics.
3. Deduplicate and select candidates across source groups so one large competitor or seed cannot
   monopolize the inventory. Record collection/selection limits and omissions explicitly.
4. Screen buyer fit, then inspect a bounded, diverse sample with live organic SERPs. Preserve result URLs for later
   research; do not infer page content, link requirements, or missing website pages from snippets.
5. Produce validated intent groups, buyer-fit explanations, relative priorities, suggested page
   approaches, evidence requirements, and excluded candidates. No page-count quota.
6. Publish the readable report and structured evidence in one create-only canonical commit.

Source contracts: [Ranked Keywords](https://docs.dataforseo.com/v3/dataforseo_labs/google/ranked_keywords/live/),
[Competitors Domain](https://docs.dataforseo.com/v3/dataforseo_labs/google/competitors_domain/live/),
[Keyword Ideas](https://docs.dataforseo.com/v3/dataforseo_labs/google/keyword_ideas/live/),
[Related Keywords](https://docs.dataforseo.com/v3/dataforseo_labs/google/related_keywords/live/),
[Keyword Overview](https://docs.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live/),
[Live organic SERPs](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/).

Search Console reuses the existing project-owned integration, with explicit opt-in, property
matching, target-page/country filtering, a pinned 90-day window, and bounded query/page rows.
Its API returns top rows, not guaranteed full coverage. A missing/mismatched optional property
is disclosed and not read; it does not prevent public keyword research.

## Durability and handoff

Each paid operation saves a request fingerprint and conservative reservation before dispatch.
Completed calls are reused. Unknown outcomes are not blindly retried; unavailable evidence is
reported. No claim of provider exactly-once billing or guaranteed metadata reconciliation.
Essential model failure fails the run rather than inventing a valid strategic assessment.

Publish only:

- `reports/keyword-plan/{run_id}/PLAN.md` (90 KB; the primary artifact).
- `reports/keyword-plan/{run_id}/keywords.json` (900 KB; the consumer contract).
- `reports/keyword-plan/{run_id}/evidence.json` (900 KB; bounded observations and receipts).

The structured inventory binds run/project/definition/scope, stable candidate/group IDs,
source references, sampled search results, and coverage. The Markdown is a derived readable
view. Later consumers select this run's exact publication revision. Ordinary project files
remain editable; edits never silently change a running consumer's input.

Retention also has byte bounds: 240 KB of candidate observations (up to six independent
observations per keyword), 12 KB per SERP sample, and 300 KB of model input. The report may
summarize a subset of groups; full validated groups stay in the inventory. Sources retain
request fingerprints, counts, and normalized-result hashes; rows outside the selected inventory
are not duplicated into public evidence. All omissions are explicit, not negative findings.

Reuse canonical CAS publication and lost-response reconciliation, generic run progress,
Postgres-backed Activity, and identifier-only Temporal execution. Stopping fences later paid
work but cannot refund accepted calls. Once publication starts, reconcile it before completion
instead of declaring the run stopped while its files may already have been committed.

## Policy v6 reservations — September 29, 2026

New definitions pin `keyword-plan-v6`: the v5 research, instructions, schemas and caps, with
reservations sized from list prices and a $2 floor instead of $5. Lookups reserve $0.05
(largest list price $0.036), search-result samples $0.005 ($0.002), and the seed, screening and
review calls $0.10, $0.10 and $0.15 (worst cases $0.083, $0.085 and $0.099 at long-context
rates). A full run reserves at most $1.65. Arithmetic and sources are in
[workflow billing coverage](workflow-billing-coverage.md#ceilings-sized-to-measured-cost--september-29-2026).
A provider charge above its reservation still stops research rather than overspending.

## Policy v7: screening in batches — October 1, 2026

Production run a59cded8 (v6) screened about 300 candidates in one call capped at 5,000 output
tokens. GPT-6 Luna counts reasoning against that cap: the call reasoned for 4,221 tokens,
stopped mid-JSON, and the run failed after buying $0.42 of research. New definitions pin
`keyword-plan-v7` (catalog `organic.keyword_plan` 0.7.0). It keeps v6's research, seeds,
review, instructions and schemas and changes only screening:

- At most 50 candidates per call (`triage:0`, `triage:1`, ...), three batches at a time. Each
  batch labels its own `k1..k50` slots; Tin concatenates the labelled rows back in candidate
  order, which is exactly what one call over all candidates returns.
- A batch is cut off when the provider reports `incomplete` for `max_output_tokens`, or when its
  JSON does not parse and it used its whole allowance. It is asked once more
  (`triage:N:retry`) with 16,000 tokens instead of 8,000, under its own reservation. GPT-6 Luna
  allows 128,000 output tokens per response.
- If the retry is cut off too, or would pass the run's spending limit, the run fails with
  `output_truncated` and names the batch. No further batch starts; completed batches keep
  their receipts.
- The research (sources, seeds, competitors, candidates and coverage) is saved as its own
  record before screening. Every lookup already had a completed receipt, so a repeated
  activity never bought one twice, but a retry of a failed run used to buy them all again
  because only a finished collection was reused. A retry of the same job now reuses the saved
  research and screens again.
- Runs pinned to v2-v6 keep one screening call with their 5,000-token cap and fail as before;
  their failure now says the call stopped at its output limit when that is what happened.

## Concurrent lookups — September 29, 2026

Research buys the same calls as before, but no longer one at a time. Policies, seed counts,
sample caps, model inputs, request fingerprints and receipts are unchanged.

- Collection runs the target footprint, competitor discovery and the Search Console read while
  the seed proposal is written, then the competitor footprints, seed metrics, suggestions and
  related keywords four at a time. `sources` is rebuilt in the old fixed order, because
  candidate selection takes one row from each source in turn.
- New runs inspect the search-result samples in one `keyword_inspect_batch` activity, four at a
  time, behind the `keyword-inspect-batch-v1` patch. Each sample keeps its `serp:{index}`
  receipt. Histories started earlier still schedule one `keyword_inspect` per sample.
- Reservations are taken in the old sequential order before each group starts, so a binding
  ceiling refuses the same calls. After a failure no further call starts; calls in flight
  finish so their receipts settle. A paid call reserves on the database connection that
  already holds its receipt lock, so each lookup in flight uses one pooled connection.
- One DataForSEO client and its connections are shared across a batch; each lookup keeps its
  own timeout, size bound and receipt.
- If the seed proposal fails after the target and competitor lookups started, those two lookups
  were still bought. A refused seed reservation buys no lookup.

## Acceptance

Fixture-only provider/model tests first; no account credentials or paid calls in tests.
Verify exact request bounds, unchanged provider metrics, complete candidate assignment,
unknown-versus-empty results, safe URLs/Markdown, budget exhaustion, ambiguous requests,
duplicate activity execution, cancellation, wrong-project/target audit selection, wrong GSC
property, atomic publication recovery, one completion event, HTTP/MCP parity, and Temporal replay.
Then run the existing regression suite. A real provider pilot requires separate spending approval.

Implemented and locally verified September 8, 2026: 437 tests pass, including 43 keyword-plan
tests with real isolated Postgres and local Temporal replay. Ruff, import boundaries, existing
diagram/comparison asset checks, and the packaged comparison browser test also pass. No
production deployment, environment change, or paid provider pilot was performed in this slice.
