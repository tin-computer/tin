# Private workflow runtime groundwork

Historical record: this page documents the September 10 cancellation groundwork, before
private activation and API billing. Its remaining-work list is not the current release status.
See [private activation](private-workflow-activation.md), [Codex API execution](codex-api-pilot.md),
and [current feature status](feature-status.md) for the subsequent deployed capabilities.

## In plain English

Private workflows may use the user's authorized integrations, including preparing a PR in
their connected GitHub repository. That does not require a report-only product restriction
or another approval ceremony. Tin still owns credential handling and actual delivery.

This first runtime slice gives all Codex-backed procedures the same Stop operation. It
stops unfinished work, preserves already verified output, and never pretends to recall a
publication or PR that has started. It does not activate private packages yet.

## Implemented

- `POST /api/workflows/runs/{run_id}/stop-procedure` and MCP `stop_procedure` use one
  membership-gated service. Task controls,
  native workflows and review gates retain their existing semantics.
- The service binds the exact run/project/generation, serializes duplicate requests, then
  takes the same non-blocking publication lock as delivery. A publication receipt, canonical
  revision or GitHub delivery reservation means reconcile the effect, not cancel it.
- One Postgres transaction marks the run stopped, releases its lease, revokes its broker
  grants and records one Activity fact. Grant minting takes the same run lock and rejects a
  stopped run. Existing run-tool grants cannot authorize effects after lease release.
- Temporal cancellation and E2B deletion are both attempted, independently, with bounded
  waits. Repeating Stop is harmless and retries cleanup. A failed control/cleanup attempt
  returns explicit `cancellation_pending` / `cleanup_pending` facts without upstream details.
- An active procedure checks the durable stop/lease during its existing heartbeat loop.
  Thus an API interruption after the stop transaction does not require a successful cancel
  request to interrupt the sandbox. Retried activities reject stopped runs; failed creation
  or persistence cleans up its sandbox. No new execution engine or control table was added.
- Retained verified checkpoints remain readable. Delayed failure cannot replace stopped with
  failed. New publication cannot start with the released lease, and an uncertain earlier
  publication remains eligible for its original reconciliation.

MCP discovery advertises cancel for active, unpublished procedures; a race with publication
can still return a conflict. HTTP is ready for the existing dashboard controls, but this
slice does not add a new browser button or design. Normal saves still select the latest
definition automatically; there is no user-facing version picker.

## Verification and limits

The dedicated tests use real SQL in disposable Postgres schemas for concurrency, grant
revocation, generation/executor checks, lost publication responses, retained outputs,
stopped-run retry, membership and HTTP/MCP parity. They also cover cancellation outages,
E2B deletion races and a worker observing Stop when the API never delivered cancellation.
The opt-in live test uses a real disposable, network-fenced E2B sandbox with synthetic idle
work. It invokes MCP against local Postgres and stubs Temporal cancellation. No model call,
pooled credential, production project change or GitHub write is involved in that proof.

This is the cancellation portion of Phase 2, not completion of private isolation or billing.
Revoking a broker grant prevents another fetch/writeback; it does not erase an OAuth cache
already fetched by a running sandbox. Existing bounded sandbox deletion remains necessary.
No claim is made that already accepted provider requests stop costing money immediately.

The subsequent [model-service slice](model-service-accounting.md) adds explicit OpenRouter
support and trusted run usage receipts to the native API-key router. That service is separate
from Codex's pooled ChatGPT authentication and does not prove its isolation or usage.

Remaining before a private-execution pilot: keep reusable pooled Codex credentials outside
author-controlled commands and artifact channels; prove that execution path's run-scoped
model access and trusted usage capture; bind private capability policy to validation,
activation and admission.
The existing project-owned GitHub snapshot/PR gateway remains the delivery route. Native
OAuth refresh and the present default/browser/Studio profiles are unchanged here. Do not
enable private execution or retail billing on the strength of cancellation tests alone.
