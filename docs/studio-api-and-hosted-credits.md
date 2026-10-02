# Studio API execution and hosted credit defaults

Contributor and operator reference for protected Studio execution and optional hosted
credit defaults. Live Stripe payments remain off.

## Studio

The public workflow stays `creative.product_demo`, a `codex.procedure` with its
pinned `studio` profile. API-enabled runs select the separate
`TIN_LITE_E2B_STUDIO_API_TEMPLATE` (`tin-lite-codex-studio-api`). Historical OAuth
receipts and budgets retain their original meaning. No Temporal commands change.

The image combines the existing protected Codex controller with the pinned Studio
toolkit. Capture, rendering and verification execute as `tin-work`, whose own
browser cache/work directory is separate from controller credentials. The worker
receives only the existing bounded, run-scoped Studio voice capability. It does
not receive a provider key, pooled login, model relay grant or storage credential.
Image inspection uses the existing bounded vision API contract.

New budgets pin both OpenAI token pricing and `fal-studio-reported-cost-2026-09-16-v1`.
The existing $5 conservative run ceiling includes both; there is no orchestration
or sandbox markup. Voice synthesis and transcription each reserve at most $0.50
internally before dispatch. That is a customer liability ceiling, **not** a fal price.
Actual cost comes from an exact request/endpoint match in
[fal's billing-events API](https://fal.ai/docs/platform-apis/v1/models/billing-events),
including provider discounts. This API needs an admin-scoped `FAL_KEY`, held only
by the switchboard. Missing or inconsistent supplier costs remain unknown, never
zero or a guessed per-second price. Late costs are looked up without regenerating
audio, then settled through the existing root ledger. Supplier overruns and costs
unresolved after the existing reconciliation window are absorbed by Tin.

Each line has a stable content-bound ID and a quota reservation; synthesis and
transcription have separate output/usage receipts. A completed synthesis survives
a transcription failure. An ambiguous paid attempt cannot be automatically bought
again. The sandbox CLI preserves IDs across reruns. Changed instructions need a new
ID and consume quota. Missing voice configuration rejects a new video start before
buying Codex work.

## Hosted defaults

`TIN_LITE_BILLING_HOSTED_DEFAULTS_ENABLED` defaults **false**. When an operator
enables it with billing, welcome credits, the protected images and the server
OpenAI key configured:

- Authenticated project discovery enables credit funding for that user's workspaces
  with an established owner. It does not change billing administrators or balances.
- The existing once-per-person $10 welcome grant remains the only automatic credit.
- Projects without a policy receive $25 per-run/$100 monthly limits and $50 standing
  authority per scheduled run (projects created before October 2, 2026 keep the $10/$10/$10
  they were given), so schedules Start here sets up
  can run. Credits and the monthly limit still bound every paid step; an admin can clear the
  scheduled-run limit in Billing to keep paid schedules off. Existing policies stay intact.
- New supported Codex executions use the API without per-project enrollment.
  Insufficient funds/limits stop a start; unpriced routes remain unavailable.
- Start here workflows stay free. Code-only workflows and connected-mailbox sends
  retain their existing included policy. No historical run is retroactively charged.
- Self-hosts retain optional billing and can keep the opt-in project allowlist.

Before activating globally, drain pre-cutover compute, verify the selected images,
and perform the paid Studio acceptance. Do not silently reinterpret queued or
paused OAuth work. Pooled OAuth execution is removed; preserve historical artifacts and billing pins
without starting new OAuth compute.

## Verification

Test duplicate and changed voice requests, separate synthesis/transcription charges, unknown
usage, late billing events, quota exhaustion and one root settlement. Include bigint amounts,
concurrent welcome grants and preservation of existing project limits. Voice admission waits
at most 60 seconds for pending usage to release budget before dispatch, never repurchasing an
ambiguous attempt.

Real-image checks separately verify the protected controller, isolated worker, capture,
rasterization, scoped voice capability and video validator, then delete the sandbox.
A paid end-to-end test requires explicitly authorized resources; do not use customer drafts
or production accounts as implicit fixtures. Keep deployment evidence outside source control.
