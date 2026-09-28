# Prepaid billing: test-mode vertical slice

Historical implementation and acceptance record for the initial test pilot. Mandatory quotes,
whole-run holds and its narrow executor/enrollment list have been superseded for new runs by
[configured per-call funding](workflow-credit-simplification.md),
[expanded billing coverage](workflow-billing-coverage.md), and
[hosted defaults](studio-api-and-hosted-credits.md). Historical budgets retain their semantics.
Self-service refund actions were removed; support handles refunds through Stripe. Use
[self-hosted billing](self-hosted-billing.md) for current deployment behavior.

## In plain language

One workspace funds its projects. Before paid work starts, Tin reserves the maximum the
member accepted. Completed work receives one charge; unused funds return to the available
balance. Native models and Codex procedures share this customer contract, while retaining
different usage and supplier-cost records. Existing results stay readable without funding.

This implementation starts in **test mode only**. No illustrative design amount or test rate
is an approved live price. Existing, non-enrolled workspaces retain their current behavior.

## Implementation order

1. Add immutable test rates, quotes, a transactional account ledger, run reservations and
   per-operation spending admissions. Keep money in integer sub-cent units and round only
   final customer charges. Bind quotes to the complete selected workflow and normalized input.
2. Enforce admission inside the shared run-creation transaction. Duplicate requests reuse the
   reservation. Native model calls and eligible isolated Codex attempts use the same spending
   service. Unsupported execution profiles cannot silently run free in an enrolled workspace.
3. Settle from trusted observations, including failed/cancelled work. Unknown outcomes remain
   pending reconciliation; supplier uncertainty is not zero usage or permission to repurchase.
   Composite workflow funding is deferred until all of that parent's paid effects are covered;
   the organic parent is deliberately not eligible for this test pilot.
4. Add separate test Stripe Checkout and signing configuration. Only verified payment outcomes
   credit funds; redirects do not. Use dedicated Tin Lite product metadata, idempotency keys,
   paid invoices, and compensating refund/dispute records. Never modify Strangeloop products,
   subscriptions or its webhook endpoint.
5. Expose matching HTTP/MCP services and the existing Paper billing composition and copy.
   Omit auto top-up, promotions and other controls without an implemented service.
6. Test concurrent spending, duplicate starts/payments/settlements, stale quotes, revoked access,
   cancellation, ambiguous dispatch, unknown usage, and cross-project visibility. Verify the
   complete test-payment journey when a Stripe test key is available.

## Rollout boundaries

- Test enrollment is explicit; it does not automatically convert existing runs or debit history.
- Workspace billing authority never grants access to project files or private workflow content.
- Supplier observations, Tin's test tariff and actual supplier invoices remain distinct.
- A quote is not a promise of SEO results or a guarantee that a generated artifact is useful.
- Hosted self-service is restricted to profiles with integrated metering. Additional native
  workflows, parent branches and Codex profiles need their own paid-effect coverage before
  eligibility, not a second billing engine.
- Live charging requires approved rates, failure/refund terms, tax treatment, configured live
  payments and explicit enrollment. Test payment objects never fund a live account.

## Payment configuration

`STRIPE_SECRET_KEY` must be a test key for this slice. Tin Lite requires its own
`STRIPE_WEBHOOK_SECRET`; Strangeloop's signing secret is endpoint-specific and is not reused.
The local Strangeloop billing runbook mentions `STRIPE_KEY` and `STRIPE_TEST_KEY`, but the
current local env files do not contain them. No source credentials have been copied.

On September 10, the operator supplied a Tin Lite `STRIPE_SECRET_KEY` locally. A real Stripe
balance request confirmed test mode, and the separate product `tin_lite_prepaid_test_v1`
was created and read back successfully with Tin Lite metadata. No other application's product,
subscription, key, or webhook was changed.

### Manual setup and deployment order

1. In the **same Stripe account**, obtain a test secret key and place it in the ignored Tin Lite
   `.env` as `STRIPE_SECRET_KEY`. Do not paste it into chat. A restricted key needs permissions
   for Checkout and its payment/customer/invoice operations, product creation, and refunds.
   No publishable frontend key is necessary for this hosted Checkout flow.
2. Validate migration `029_prepaid_billing.sql` on a PlanetScale development branch, then apply
   it to the runtime database before deploying this switchboard version. The migration has so
   also passed PlanetScale development validation as recorded below.
3. Deploy the server and packaged UI. Store the Stripe key in the switchboard's secret env,
   never a workflow package, sandbox, browser bundle or Temporal payload.
   Verify runtime table permissions and `USAGE` on `billing_ledger_id_seq`: table grants alone
   do not permit the ledger's generated IDs. Grant that one sequence to the existing runtime
   role through the migration credential if needed; never give the runtime DDL authority.
4. Create a **new test-mode webhook endpoint**, distinct from Strangeloop's endpoint:
   `https://lite.tin.computer/webhooks/stripe/tin-lite`. Use API version `2024-06-20` and subscribe
   to `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.expired`, `invoice.paid`, `refund.created`, `refund.updated`, `refund.failed`,
   `charge.dispute.created`, and `charge.dispute.closed`. Set this endpoint's own signing secret
   as `STRIPE_WEBHOOK_SECRET`.
5. Set `TIN_LITE_BILLING_ENABLED=true` and `TIN_LITE_BILLING_TEST_ENABLED=true`, then restart. Explicitly enroll a test workspace
   through the creator-authenticated test-enrollment endpoint or `enroll_billing_test` MCP
   tool, then set project spending limits. Existing workspaces are never auto-enrolled.
6. Start a test top-up. The first Checkout automatically creates the separate deterministic
   product `tin_lite_prepaid_test_v1`, named **Tin Lite usage — test**. Do not reuse the
   Strangeloop product, subscription prices or webhook signing secret. A key with product
   write permission avoids a manual product-creation step.
7. Complete an actual Stripe test checkout and verify the signed webhook, balance, invoice,
   quoted workflow start, settlement and refund. That account-connected acceptance has **not**
   happened yet. Do not claim the simulated tests below prove it.

### Current implementation boundaries

- Eligible profiles: `creative.character`, `content.plan`, and procedures whose immutable
  sandbox profile is `isolated`. Native provider service and Codex ChatGPT OAuth usage remain
  separate trusted measurements. Rates are fictional `tin-test-only-v1` retail rates, not
  supplier invoice estimates or an approved live offer.
- The accepted maximum binds the full selected definition and normalized inputs. It is a
  bounded authorization, not an estimate of a changing filesystem snapshot. Files can still
  change before an activity pins its context; excess supplier expense is absorbed by Tin.
- Quote, reserve and run creation share the normal HTTP/MCP start contract. Each paid unit
  rechecks project membership and current spending limits. A scheduled occurrence also needs
  a current saved-workflow creator and explicit `schedule_max_nanos` standing authority.
  The Billing limit editor sets it as "Per scheduled run"; blank keeps paid schedules off.
  Hosted default policies start with $10.
- A start blocked by project limits keeps the `project_limit` code (HTTP 402) and names the one
  limit that applies: no spending policy, the per-run limit against the estimate, this month's
  limit with the amount already committed, or the concurrent-run limit with the active count.
- Direct plan-file edits remain ordinary project-file operations. Paid **AI amendments** through
  the content-program revision API are not enrolled yet: that alternate start cannot supply an
  accepted quote, so it is rejected in billed workspaces. Audit, keyword planning, the organic
  parent, browser/Studio/default Codex profiles, project tasks and the remaining native workflows
  are also not eligible. Unenrolled workspaces retain all existing behavior.
- Settlement uses trusted usage even if artifact validation fails. A no-operation run has no
  execution fee. Eligible workflows have no paid work after their final review gate; their
  known costs settle while approval is waiting, without altering approval or the file lease.
  Unconfirmed usage stays pending, is reconciled from durable receipts, and
  unresolved expense is absorbed after the recorded 24-hour deadline. Charges round once to
  cents. Monthly limits count charges posted in that UTC month plus all unsettled reservations,
  including reservations carried from a prior month.
- A lost Temporal start acknowledgment retains the original run and reservation. Recovery uses
  the same workflow ID with duplicate reuse rejected. The browser retains opaque quote/request
  IDs across reloads after ambiguous starts; it stores no workflow inputs with them.
- Signed payment events and verified Stripe reads are the only funding authority. Checkout
  redirects do not credit funds or start work. Refunds reserve only unused funds; charges consume
  the oldest top-up first. Ledger rows are append-only, including compensating dispute entries.
- External/dashboard refunds and disputes suspend further spending pending operator review.
  There is no automatic dispute-resolution or account-resume control in this pilot. An inquiry
  that closes without a chargeback (`warning_closed`) restores its credits and is stored with
  status `won`, avoiding a migration; like `won` and `lost`, it is terminal, so later events on
  that dispute ID are ignored. An ambiguous
  payment/refund older than Stripe's protected retry window is not repurchased automatically;
  it needs operator reconciliation. Replay a missing `invoice.paid` event if its invoice link
  has not arrived.
- Reconciliation runs in the existing switchboard process; it is not another workflow engine.
  A standalone worker without that process does not supply the billing reconciliation loop.
- Checkout sessions expire 45 minutes after the payment request (Stripe's minimum is 30), so an
  abandoned top-up stops being pending within the hour. Pending checkouts are reread from Stripe
  on a per-payment backoff: every 30 seconds for the first 2 minutes, every 2 minutes to 10,
  every 10 minutes to an hour, then every 30 minutes. Webhooks remain the primary path.
- No automatic top-up, subscriptions, promotional credit, live-mode account, retroactive charge,
  or changed Strangeloop resource is part of this slice.

### Local verification

Final local checks on September 10, 2026: **980 backend tests passed**, with 5 skipped and
3 existing collection warnings; all 4 billing browser checks and 3 content-plan browser
regressions passed. Ruff, import-boundary checks, JavaScript syntax, diff whitespace and the
wheel build passed. The wheel contains all billing modules and UI assets. These counts do
not include a real Stripe checkout or a production workflow run.

`tests/test_billing.py` and `tests/test_billing_recovery.py` use the complete migration chain in
isolated PostgreSQL schemas with mocked provider/Stripe transports. They cover admission races,
duplicate starts/settlement, HTTP/MCP starts and authorization, native/Codex trusted usage,
interrupted usage projections, lowered limits, revoked membership, unknown dispatch, unknown
usage, immutable ledger records, refund eligibility, payment recovery and dispute ordering.

`npm run test:billing-browser` exercises the packaged application in automated Chromium: light
and dark themes, desktop/mobile, member-only Spending, editable limits, pending payments,
Checkout-return behavior, quote consent and lost-start recovery. Screenshots were inspected.
The in-app browser was unavailable, so no signed-in production browser test has occurred.
The composition follows the existing Paper billing boards and application tokens/components;
unimplemented auto-top-up/promotional controls are omitted.

### Checkout return correction

The operator completed the second Checkout: the real callback credited exactly $10 of test
funds, and the paid invoice was read back through authenticated MCP. Returning to the browser
exposed a navigation bug: `billing_payment` selected the Billing view but left the previously
selected project/workspace in place. The single-workspace browser fixture had missed this.

The corrected bootstrap reads `GET /api/billing/payments/{id}/return`, which resolves only the
stored payment's workspace after billing-admin authorization. It performs no Stripe request,
funding, enrollment or workflow start. The shell then selects only an accessible project in
that workspace, retaining the preferred project when it belongs there. It persists the
selection and consumes the payment parameter once, so reloads stay correct and later explicit
project switches work normally. Missing/forbidden returns or lost project access show a retryable
navigation error, not an unrelated workspace or a claim that payment failed. The existing
Postgres payment/ledger projection remains the only source of payment status.

Regression tests now cover two workspaces with both stale URL and localStorage selections,
paid/pending/expired returns, reload, explicit switching, lookup failure and lost project access.
Backend tests cover read-only resolution, missing/invalid IDs, non-admin project members,
outsiders and revoked workspace membership. No schema or sandbox change is needed.
