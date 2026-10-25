# Run usage: observed facts, not billing

## In plain language

A run can now answer “what resources did this use?” through HTTP and MCP. It separates API-model
tokens, Codex ChatGPT usage, sandbox time, and DataForSEO's reported request costs. A system also
includes the child runs it actually started, not older audit/keyword runs it merely read.

Missing measurements mean **unknown**, not free. The readout does not charge users, manage balances,
quote a maximum price, or turn a ChatGPT subscription's token counters into API charges.

## Shared read contract

- HTTP: `GET /api/workflows/runs/{run_id}/usage`.
- MCP: `get_run_usage(run_id)` (refresh the MCP connection to discover newly added tools).
- Both authorize project membership first and call the same Postgres-only projection. No provider,
  Temporal, or code.storage read is needed. HTTP marks the response `no-store`.
- `own` contains observations and known subtotals. `children` breaks down the fixed organic-system
  child recipe. `inclusive_totals` includes both exactly once. Actual ownership requires the parent
  receipt, same project, and matching child start idempotency key. Failed children still count.
- Reads use one repeatable-read snapshot, a five-second SQL statement timeout, and at most 2,048
  observations per run. Any cap is marked `truncated`. Parent traversal is bounded to the four
  existing organic-system steps; this is not a graph registry.

The readout selects metadata, not full report/prompt/error bodies. It returns normalized bounded
token counts, provider/model, outcome, opaque observation ID, and explicitly named cost bases.
It does not expose sandbox identifiers, credentials, raw provider errors or hidden model reasoning.
Tool observations keep known provider names, including `fal` and `podscan`; a missing or
unrecognized name stays `unknown` instead of being attributed to DataForSEO.

## What is measured

- Existing `native_model_usage_v1` receipts: run-scoped OpenAI, Anthropic, Gemini and OpenRouter
  adapter observations, including provider usage preserved when output validation fails.
- Existing `isolated_codex_attempt_v1` receipts: one trusted cumulative usage observation per
  isolated attempt, including failed/cancelled attempts. Never sum the cumulative and last-turn
  counters or price OAuth counters using API rates. Default/browser/Studio Codex token capture
  is not added by this slice.
- New `external_usage_v1` receipts: organic audit's Responses calls and audit/keyword DataForSEO
  request submissions. Persist intent before dispatch and available provider metadata before
  downstream output validation. An ambiguous attempt is not silently dispatched again.
- New `sandbox_usage_v1` receipts: provider start time, CPU and memory, plus Tin-observed deletion.
  The key is per physical sandbox; duplicate cleanup cannot extend the recorded interval.
  Never use E2B's expiration deadline as an actual shutdown time or Codex execution time as the
  sandbox's full lifetime. Missing start/deletion facts remain unknown.
- Historical audit/keyword metadata can contribute partial observations. Modern receipts supersede
  the corresponding legacy paid step; reservations are never treated as consumption. Older adapters'
  fallback-zero costs are not upgraded into evidence of a free request.

Observation writes reuse the activity's existing connection; they do not acquire another pool
connection while all workers are holding theirs. Sandbox observation failures cannot strand paid
compute or prevent deletion. They leave a coverage gap, not a fabricated measurement.

## Cost meaning and limits

`provider_reported_cost_usd` is the trusted DataForSEO task-envelope cost field, not a supplier
invoice reconciliation. [DataForSEO documents the task cost in USD](https://docs.dataforseo.com/v3/on_page/task_post/).
Google Ads Keyword Planner (`gak`) tool receipts report `0` because the operator-run service is
not metered per request; the observation still records that a bounded request was made. Google Ads API (`google_ads`) tool receipts report `0` for the same reason: Google charges
for clicks in the founder's own account, never per request.

`reference_estimate_usd` currently covers E2B only: observed wall seconds multiplied by the CPU and
memory list-price card pinned as `e2b-public-2026-09-10`. The source is
[E2B's public pricing](https://e2b.dev/pricing): $0.000014/vCPU-second and $0.0000045/GiB-second.
This is explicitly a **wall-time reference estimate**, not measured billed-active time. It does not
reconcile pauses, storage, base plans, negotiated discounts or invoices.

API-model tokens remain unpriced in this readout; adding a tested model/tier/context/cache-aware
price card is separate from recording usage. Do not guess a price from a model prefix. Codex OAuth
must remain a distinct execution category even after native model price cards are added.

`known_provider_reported_cost_usd` and `known_reference_estimate_usd` are separate subtotals.
`total_cost_usd` remains null: adding incomplete observations with different bases would falsely
look like a complete bill. `coverage` is explicitly partial. Legacy/uninstrumented calls, failed
attempts without returned metadata, audit poll/recovery reads, and non-run chat are not fully
accounted for. No receipt backfill, old-receipt mutation, new migration, pricing UI, retail ledger,
balance, checkout, workflow schedule or user charge is introduced.

## Verification

Disposable-Postgres tests cover HTTP/MCP parity and authorization, parent ownership and failed
children, legacy/modern deduplication, malformed/missing/zero metadata, provider-response capture
before invalid output, no repurchase, cumulative Codex counts, bounded reads, full-pool connection
reuse, cleanup idempotency, and deletion despite observation failure or cancellation.
