# Model service and Codex procedures

This documents the model-service foundation contract. Its
“not included” and “deferred” statements below describe that slice, not current rollout.
Today native model calls and protected Codex API execution retain distinct usage contracts;
hosted defaults select the API runner for supported new Codex work, and historical OAuth
runs retain their pins. Private code/model/connection/schedule slices have since shipped
within the operator-enabled pilot. See [feature status](feature-status.md),
[billing coverage](workflow-billing-coverage.md), and [Codex API execution](codex-api-pilot.md).

## Foundation boundary at acceptance

Tin has two different ways to do model-backed work. The shared model service calls OpenAI,
Anthropic, Gemini, or OpenRouter using credentials held on the Tin server. A Codex procedure
runs the Codex coding agent in a sandbox through the protected API relay. (At acceptance it
used a pooled ChatGPT login; that mechanism has since been removed, see
[API-only Codex execution](oauth-credential-security.md).)
Adding a provider to the first does not change how the second authenticates or executes.

Hosted users do not need their own model keys. Self-host operators configure their own keys
on their server. No version picker, model picker, extra approval step, or report-only
restriction on private workflows is introduced here.

## Implemented

Unconfirmed calls retain a fixed failure label (timeout, connection, provider status or
interruption) in their existing usage receipt. They remain unconfirmed for accounting and
cannot be purchased again by an activity retry. Raw provider messages, prompts and response
bodies are not copied into diagnostics. Interactive task retries also preserve the original
API stop reason, such as a token limit, instead of replacing it with a generic unresolved error.

- Explicit `openrouter` adapter, using the official OpenAI SDK's compatible Chat Completions
  client, with optional server-only `OPENROUTER_API_KEY`. Other credential names are unchanged.
  Its endpoint is fixed; prompts cannot choose a URL, supply credentials, or override routing.
- Text, strict JSON Schema, reasoning effort, response identifiers and normalized token usage
  use the same service contract. Required parameters must be supported by the selected endpoint.
  No automatic provider fallback, model substitution or SDK retry is requested. Gemini's SDK
  retries are now explicitly disabled too.
- A credential only configures an adapter. It does not add a route or change a workflow's
  selected model. All current model routes and immutable workflow definitions are unchanged.
- Runtime service calls require a scope set by the trusted activity: run, stable step and
  existing database connection. The recorder resolves project/workflow/revision from Postgres;
  this is not a client-supplied authorization token or a new public model API.
- One `native_model_usage_v1` effect receipt per run/step records intent before dispatch.
  A completed observation contains provider/model, response identifier and reported token
  counts, including cache reads and cache writes separately. Invalid JSON or empty output still
  retains the provider's usage. No prompts, output, hidden reasoning or credentials go into
  these accounting receipts or Temporal history.
- A crash, timeout, cancellation or failure to save an observation leaves an unconfirmed
  receipt with unknown usage, not zero. Retrying that step does not make another paid call.
  The owning workflow still stores/replays its output in its existing receipt; if that output
  is lost, the workflow must report/reconcile the uncertainty, not repurchase automatically.
- Receipt locks reuse the owning activity's database connection, so a busy worker cannot
  deadlock by holding every connection while requesting another for accounting.

Anthropic's shared input total includes uncached input, cache writes and cache reads. Its
cached-input field now means cache reads alone. Optional usage fields stay null when unknown;
missing values are not substituted with zero. These are usage observations, not invoices or
a new billing ledger. They do not contain provider pricing, retail prices or a spend budget.
Existing workflow-specific spending ceilings and retry policies remain in force.

## Coverage and limits of the September 10 slice

The paid ads assessment adds two routes (`paid-ads-judgment-v1`, `paid-ads-drafting-v1`) and
the zero-cost `gak` tool provider; the Google Ads launch adds `paid-ads-launch-copy-v1` and the
zero-cost `google_ads` tool provider, which the monitor and proposal approvals share. All shared-router callers are connected: paid ads assessment, keyword planning, content planning,
native character design (draft, bounded repairs, refinement), and the retained native
site-health executor for older histories. There is no schema migration, new Temporal
implementation, change to the dashboard/MCP start contract, or sandbox image rebuild.

Older direct Responses callers such as Luna and the visibility/organic audit path are **not**
included in these shared-router receipts. Their existing workflow-specific receipts remain.
Nor are Codex's OAuth calls, E2B compute, DataForSEO or Studio media spending included.
Absence of a receipt must never be presented as a free run or complete platform-wide usage.
No public usage dashboard or billing endpoint is added by this slice.

At acceptance, private package activation was deferred until Codex no longer relied on
reusable pooled OAuth credentials. That has since happened: new Codex compute is API-only and
private packages use explicit activation (see [private activation](private-workflow-activation.md)).
This implementation was not that isolation proof. Private workflows may still legitimately prepare GitHub PRs through
the existing authorized integration gateway; there is no new prohibition on that outcome.

## Visibility response recovery

The direct Responses path used by `visibility.audit` keeps its accounting observation separate
from its recoverable output. Each panel, answer and adjudication step saves a bounded
`visibility_response_v1` receipt before parsing or semantic validation. It contains only the
returned text, public search/citation evidence and token counts, capped at 250,000 bytes.
Prompts, credentials and reasoning are excluded; no output is added to Temporal history or
to the usage receipt. An oversized response records a small rejection instead of its body.

Retry reuses this response under the original step's effect lock and connection. A valid
checkpoint can finish the owning effect after an interrupted validation or lost write
acknowledgment. Invalid output retains its original non-retryable validation error. A request
without a saved response remains uncertain and is never automatically purchased again;
this also applies to older attempted calls whose output was not retained. Completed legacy
effects remain reusable without a new response checkpoint.

The panel schema asks for a bare DNS hostname or an empty unknown domain. Deterministic code
also normalizes HTTP(S) URLs and harmless hostname formatting before checking target identity
and question blindness. Normalization does not substitute another target or guess an unknown
domain. Concurrent answer calls finish saving their receipts before a validation failure is
projected.

## Verification

Tests use real SDK request serialization with in-memory HTTP responses and real SQL in
disposable local Postgres schemas. They cover provider mapping, credential placement, disabled
retries, rejected output, lost responses, cancellation, duplicate dispatch, accounting-write
failure, full-pool nested locks, multiple character steps and inactive/unbound scopes.
No paid provider pilot, production workflow mutation or private activation is claimed.
An OpenRouter route must be explicitly registered and tested before a workflow uses it;
merely deploying this adapter does not start sending requests to OpenRouter.

## Provider references

The adapter follows OpenRouter's [structured output contract](https://openrouter.ai/docs/guides/features/structured-outputs)
and [usage accounting fields](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
Anthropic normalization follows its [prompt caching usage definition](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
The direct OpenAI adapter remains on Responses, not Chat Completions or Codex authentication.
