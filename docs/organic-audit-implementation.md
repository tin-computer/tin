# Organic audit: first implementation milestone

## ELI5

Tin checks a bounded sample of your public website, asks an AI adviser a fixed set of
buyer questions, and saves what it found. You get a readable report and a structured
list for later technical-fix and content-planning workflows. Nothing edits your site.

This reference includes the version 0.2 scoring contract and its versioned follow-ups.
Use [feature status](feature-status.md) for current availability. Changes to policy or
question preparation must not reinterpret an older run's pinned evidence.

## Implemented

- `organic.audit`: one native Temporal implementation in the Organic Traffic Registry.
  Dashboard, HTTP, and MCP share a start service. No Luna conversation, GitHub,
  sandbox, new database table, or separate engine is required.
- Exact public HTTPS origin, selected US/GB/CA/AU market, English. Credentials, paths,
  queries, private addresses, and ambiguous hostnames fail preflight. Tin checks DNS
  but does not fetch arbitrary URLs itself. DataForSEO owns its crawler's DNS and
  redirect isolation; off-host results are excluded, not silently attributed to the target.
- One DataForSEO OnPage crawl, at most 100 pages, rendering flags off, ordinary robots
  restrictions preserved. Short activity polls plus Temporal timers; one-hour elapsed
  collection deadline and 120-poll ceiling.
- Deterministic HTTP, redirect, canonical-target, title/description, duplicate-metadata,
  broken-link, and possible-orphan observations. Missing flags are unknown, not passes.
  Deliberate redirects and provider warnings require review, not automatic fixes.
- Public search-backed research creates a frozen buyer panel: one to three supported
  jobs, four question families per job, two fresh answers per question. A separate
  validator rejects identity/fit errors and brand leakage. No repair/resampling loop.
- Explicit OpenAI GPT-6 Luna route. Answering receives only the neutral buyer question
  and market, not the target or project memory. Final-answer citations are distinct
  from provider-returned sources. When a complete answer contains none of the validated
  target names, deterministic code records name absence without a paid grading call.
  A name occurrence still requires an answer-only judge to disambiguate genuine mentions
  and recommendations; positives require exact target-bearing quotes. Negative mentions
  are not recommendations; first in a list does not mean preferred first.
- Two separate branded probes about the offering and pricing/limitations. These expose
  what the API answered, not a verified accuracy score or part of the unbranded baseline.
- Content-review findings flag buyer-answer coverage worth investigating. Each candidate
  requires both answers to that question to be scored and neither to cite the website;
  an unrelated incomplete question no longer suppresses these review findings. A missing
  citation does not prove missing content, explain causality, or predict lift.
- HTTP/MCP stopping fences future paid work in Postgres, signals Temporal, and attempts
  to stop a known crawl. Accepted requests can still incur costs. An uncertain/in-flight
  canonical publication must finish reconciliation before its outcome can be reported.

## Durable handoff

One create-only, compare-and-swap commit publishes:

```text
reports/organic-audit/{run_id}/AUDIT.md       <= 150,000 bytes
reports/organic-audit/{run_id}/findings.json  <= 500,000 bytes
reports/organic-audit/{run_id}/evidence.json  <= 900,000 bytes
```

Every file fits the existing one-million-byte MCP reader. Findings include stable IDs,
status, severity, confidence, ownership, verification, dependencies, and next-action
category. Technical findings carry up to ten example URLs, a full affected count, and
a deterministic reference into the complete bounded crawl evidence. This avoids
repeating long URLs across every check.

The report names the findings digest; findings name their evidence digest. Evidence
pins the run, project, requested host, time, policy, and definition revision. Downstream
workflows should receive the immutable artifact revision, findings digest, and selected
finding IDs; recheck the issue and repository ownership before proposing changes.
Content planning can also consume the frozen questions. Nothing starts automatically.

Existing run paths are never overwritten without a saved intent proving this run's
publication. Lost responses reconcile against first-parent history, all three file
contents, and exact changed paths. Later edits/deletion never cause resurrection.
Success, Activity, and final receipt commit in one Postgres transaction; finalization
needs no new storage read.

## MCP-first journey

1. `list_workflows(project_id)` finds the workflow; `get_workflow` returns its input schema.
2. `start_workflow` takes `workflow_id="organic.audit"`, inputs `site_url` and `market`,
   and an optional stable UUID `request_id`. Reuse it after an uncertain client response.
   HTTP retains the existing `Idempotency-Key` header. Neither path requires Luna.
3. `get_run` reads Postgres state and the artifact revision. `read_run_output` reads the
   report; `read_project_file` with that revision reads findings/evidence.
4. `stop_organic_audit(run_id)` stops future work before publication. HTTP uses
   `POST /api/workflows/runs/{run_id}/stop-organic-audit` and the same service.

Project membership gates every operation. Saved configurations are manual-only; there
is no new audit dashboard, comparison page, or review gate.

## Scoring and partial results in 0.2

The immutable definition pins `organic-audit-v2`. A scope receipt records this policy;
pre-existing receipts without that field remain v1. The worker accepts the exact old and
new instruction/policy pairs and keeps v1 grading, requests, reporting, and receipts intact.
It does not use the latest catalog to reinterpret an existing run.

- A complete answer without a validated target-name occurrence is a deterministic negative
  for mentions/recommendations, not a missing observation. Website citations remain independent.
  Empty, incomplete, oversized, missing-search, or unavailable responses are **never** negatives.
- Name presence is not itself a positive grade: namesakes and negative mentions still need
  semantic grading. The stricter judge prompt and exact supporting-quote checks reject unsupported
  positives. No paid grading repair or replacement-answer loop is introduced.
- Partial evidence retains `observed_metrics`, alongside explicit `completed`, `planned`, and
  `missing` counts. The comparable full-panel `metrics` field stays null until every planned
  observation is scored. Observed counts are not presented as full-panel percentages or a score.
- The generic Markdown report includes a bounded question table, scored counts per pair, and
  individual gap explanations. Only Tin-owned reason codes are rendered; provider errors,
  credentials, and arbitrary model-generated diagnostic text are never copied into the report.
- Valid content-review findings carry `audit_coverage=partial` when appropriate and cite only
  complete question pairs. They remain hypotheses requiring inspection, not missing-page claims
  or authorization for automatic execution.
- The publication receipt carries a small coverage summary. The existing Postgres result
  summary and Activity event receive it in the same atomic completion transaction. A partial
  report is visibly described as partial rather than merely saying the audit is ready.

Offline regression against the immutable Claw Messenger pilot avoids its two erroneous grading
calls and yields 23 scored observations with one still unknown. Ten complete question pairs
support three content-review findings. This is an **offline replay**, not a new measurement:
no provider calls were made and no historical artifact, receipt, or Activity event was changed.
The legacy report was also reproduced byte-for-byte using the v1 path.

## Spending and retry

Trusted-switchboard configuration: `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`,
`TIN_LITE_ORGANIC_AUDIT_MAX_COST_USD` (default **0**), and existing
`TIN_LITE_LUNA_API_KEY`. Credentials never enter Files, MCP inputs, E2B, or Temporal.
Missing DataForSEO configuration or a zero limit fails the shared start preflight.
The definition pins the model route, policy, instructions, and output schemas.

Before dispatch, Tin persists a conservative reservation, request fingerprint, and
intent. Reservations remain allocated for uncertain calls; they are not an invoice.
Ceilings: $0.05/basic crawl, $0.20/bounded search response, $0.03/no-tools structured
response. The largest panel reserves **$6.20** including branded probes. Lower budgets
produce explicitly unavailable components, never extra attempts or invented observations.

Each model request allows 60 KB input, 6,000 output tokens, default service tier, and at
most three search calls. Normalized responses cap at 16 KB, complete scored observations
at 21 KB, and crawl evidence at 240 KB. Oversized observations become unavailable,
rather than being silently truncated and scored.

Pricing was checked against [DataForSEO OnPage](https://dataforseo.com/pricing/on-page/onpage-api),
[OpenAI model pricing](https://developers.openai.com/api/docs/models/gpt-6-luna),
[hosted-tool pricing](https://developers.openai.com/api/docs/pricing), and the
[search-context limit](https://developers.openai.com/api/docs/guides/tools-web-search).
Basic crawling is $0.00015/page; its reservation includes headroom. Model reservations
allow repeated search-context processing and output under the caps without caching
discounts. **Recheck ceilings and reported usage before a live pilot: these are Tin's
reservations, not provider-enforced dollar caps.**

A DataForSEO tag is correlation, not idempotency. Uncertain submissions recover through
bounded ID List metadata matched to the original request/tag/time window. No match
never authorizes resubmission. Uncertain model results remain missing observations;
incomplete panels withhold comparable metrics while exposing labeled observed counts, per-question
gaps, and supported complete-pair review findings. A crash between saved intent and dispatch
can therefore sacrifice availability rather than risk another charge.

## Acceptance and remaining work

Local tests cover duplicate delivery, paid-request ambiguity, metadata recovery, budget
exhaustion, frozen-panel reuse, negative mentions, exact citation hosts, missing results,
and handoffs. Disposable Postgres schemas prove HTTP/MCP membership and idempotency,
projection rollback, stopping, and no revival. Local Temporal tests exercise retry,
stop signals, identifier-only activity inputs, and replay. Existing accepted Cloud
histories remain unchanged and replay-tested.

The first paid pilot below verifies the real provider host/schema path, authenticated MCP
execution and artifact reads, receipts, Activity projection, spending reservations, and
identifier-only Cloud history. It does not establish final invoiced costs, signed-in visual
acceptance, or a fully measured AI panel. Future pilots still require an approved ceiling;
do not repeat paid requests merely to replace missing observations.

## 0.3 — grounded GEO preparation (September 9, 2026)

The repeated Tin Lite panel failures exposed a preparation mismatch: a single searched
JSON response invented questions and their citations before it could see which source URLs
the API returned. The parser also ignored completed `open_page` and `find_in_page` URLs,
and collapsed missing search, failed search and excess calls into one opaque reason.

The new pinned `organic-audit-v3` separates three steps inside the existing activity:

1. Public research of the exact requested host, with host-filtered search and a concise
   factual note. A parent company or private project context cannot substitute for that site.
2. No-tool question drafting from that saved note and its explicit observed URL list.
   Questions are natural buyer hypotheses grounded in product facts, not alleged web quotes.
3. Deterministic source/identity/neutrality checks followed by independent semantic review.
   Freeze the accepted panel before any answer is measured.

There is at most one separately receipted research recovery after a known validation failure
and one draft correction using the same saved evidence. Both consume the original run's
budget. Unknown provider acceptance stops recovery; retries reuse completed receipts.
Neither answers nor scores select a replacement question or trigger replacement sampling.

The source design borrows `../thinklikeanagent/scripts/generate_prompt_panel.py` and
`website_optimizer/prompts.py`: ground preparation in evidence, use short natural questions,
prevent target leakage and forced choices, then freeze. The 36-prompt operator-reviewed
experimental panel, keyword-universe requirement, intervention framework and QSoA estimands
are not imported into this bounded, automatic API diagnostic.

The OpenAI Docs check confirmed separate search/open/find action shapes and documented source
inclusion/domain filtering. Completed page actions now count as observed URLs. V3 requires a
completed answer and at least one completed web call; failed additional tool attempts remain
visible in diagnostics rather than erasing successful research. The processed-call cap still
applies, failed-only/missing search is unmeasured, and answer evidence is never fabricated.
See [official web-search documentation](https://developers.openai.com/api/docs/guides/tools-web-search).

Safe response IDs, overall status and search-call statuses survive validation failures.
Both preparation attempts and their precise failure reasons are published in bounded evidence.
V1 and V2 policy/instruction/parser paths remain intact for pinned historical runs. The
artifact filenames, finding IDs, source-digest handoff, Temporal activities, and UI stay the same.

Local acceptance: 737 tests passed with disposable Postgres, including the complete frozen
panel's duplicate-execution test, source normalization, both correction paths, budget refusal,
unknown-acceptance refusal, V1/V2 compatibility and identifier-only Temporal replay. Live
acceptance is pending deployment and a new paid run; these tests alone do not establish GEO
measurement quality on the target site.

### 0.3.1 — buyer intent, not interview prompts

Live v3 preparation passed for Tin Lite and Claw Messenger, including one bounded
Claw draft correction. Human inspection found that accepting preparation was insufficient:
some questions were interview prompts or generic supporting-feature markets, and Claw's
panel treated descriptive taglines as brand aliases. The v4 instruction contract makes
each question a standalone buyer-to-assistant ask about the core purchased problem,
prefers one strong job over three padded jobs, and requires genuine brand aliases.
Semantic review checks these distinctions, while explicitly accepting reasonable buyer
problem hypotheses without demanding proof that someone already asked the question.
V3 instructions and existing panels remain pinned, unchanged. No answers are resampled.
V4 live acceptance is pending; prompt-contract unit tests cannot establish question quality.

The v3 Tin branded pricing probe also exposed a completed response with three completed
searches and a fourth attempt left `searching`. V4 accepts this bounded case only once
the processed-call ceiling was reached and the final response completed. Sources from
unfinished attempts remain excluded; diagnostics retain their status. Below-ceiling
unfinished calls, unknown statuses, and incomplete responses still fail closed. This is
an interpretation of the documented ignored-over-budget behavior, not a claim that the
unfinished attempt succeeded. V3 parsing remains unchanged.
