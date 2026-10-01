# Workflow credits: cost previews and internal funding

## Outcome

Run means run. A member does not accept another quote or manage a hold. Before
starting, Tin checks a reusable, deterministic cost estimate against the current
balance and project limits. It charges verified usage, not the estimate.

## Small implementation

1. **Reusable cost policy.** Derive an estimate from the pinned workflow definition,
   configured scope and existing price card; cache by those values. No model call,
   web research, historical-user-data scan or new database is an estimator. Initially
   reuse the supported workflows' conservative configured spending bounds where we
   lack a defensible lower estimate. Label that basis honestly: it is not an average
   or an exact price. Input/definition/rate changes select a new estimate. Expose the
   estimate through a read-only HTTP/MCP preview and the configuration surface.
2. **One admission path.** Dashboard, MCP, direct/saved starts, revisions and scheduled
   occurrences share the existing admission service. A normal start needs no quote
   ID. Check the estimate, available credits, project limits and concurrency before
   dispatch. Preserve explicit scheduled spending authority. Free onboarding and
   approved setup children remain free. No enrollment, limit or Stripe-mode changes.
3. **Managed-operation funding.** Reuse `billing_operations` and `committed_nanos`. Managed model and service budgets
   pin `funding=per_operation_v1`; admission reserves zero. Under the existing wallet
   lock, a paid call checks its own bounded cost and adds only that liability. On
   observation replace it with actual cost and immediately free the difference.
   Unsettled actual usage stays unavailable so another run cannot spend it. Round
   the internal root liability upward to cents, then round the final charge once.
   Admission of a new run counts every unsettled per-call run that can buy work now
   (a pending or running run in its tree) at the larger of its estimate and its
   committed liability, against both the monthly limit and the credits, so parallel
   starts cannot all pass before their first paid call. A run that has ended, or that
   waits on its founder, counts at its committed liability alone. Nothing extra is
   reserved and no hold line is shown; the Billing page's available credits are the
   start check's own figure. Paid calls of runs already admitted are checked against
   actual commitments only.
4. **Limits and recovery.** Check available funds and current run/month/schedule limits
   at every call, including parent children and parallel projects sharing a wallet.
   Decline before buying a call if funding is insufficient; retain existing receipts
   and durable files. Do not invent automatic resume support or blindly repurchase
   uncertain calls. Existing bounded unknown-cost reconciliation still applies.
   A terminal run's unresolved bill retains monetary liability but no execution slot;
   active children and leases still count toward the project's concurrency limit.
   Parent steps aggregate once, and terminal settlement creates one ledger charge.
5. **Quiet UI.** Remove the per-run approval dialog and the reserved-balance line.
   Preserve stable request IDs through ambiguous responses. Show configured estimated
   cost and actual usage, with a clear add-credits error when a start cannot be funded.
   MCP preview is optional; agent instructions must not demand a quote/approval loop.

## Ordinary Codex sessions

New customer-funded root procedures with a default, isolated or browser profile use
`funding=procedure_session_v1`. Admission holds the configured session maximum once;
parallel starts cannot spend the same credits. Model responses record actual usage
against that session, without a new wallet reservation or repricing previous calls.
An outstanding/uncertain request blocks the next. Current project limits, membership,
account suspension and the run's active lease still govern continuation. Final
settlement releases unused credits and creates one charge; no per-call Stripe payment
or new UI approval is involved.

The model's context/output capacity replaces arbitrary lifetime token limits. The
session threshold stops further requests after observed usage reaches it. It does not
promise to stop supplier spending in the middle of an accepted response: Tin absorbs
excess while the customer maximum remains binding. See [execution limits and supplier
exposure](codex-api-pilot.md#transport-and-usage). Other executors and specialized
profiles retain their pinned funding and runtime policy.

## Compatibility

Old quotes and budgets retain their original funding semantics and pinned prices. Each funding marker is stored in existing JSON terms; no schema migration,
Temporal command change, new state machine, credential change or executor migration.
Explicit legacy quote IDs continue to validate their original binding. No existing
run, invoice, welcome grant, payment or ledger history is rewritten.

The first policy intentionally uses existing configured upper bounds for all
currently supported paid executors. It does **not** claim a $0.25 expected price
from one partial audit. More precise estimates can be added to the same policy
once representative executions support them. This is separate from releasing
unused per-call liability, which does not depend on estimate accuracy.

`POST /api/projects/{id}/billing/estimate` and MCP `estimate_workflow_run` return
the same preview without creating quotes, budgets or runs. Existing `/quotes`
and `quote_workflow_run` remain optional compatibility interfaces. Configuration
panels label an unmeasured configured bound as **Maximum charge**, using existing UI
styles. The estimate basis remains available through HTTP/MCP; a configured maximum
is not presented as a measured expected cost. No supplier credentials or model-provider routes change.

## Verification and rollout

- Deterministic estimate/cache identity; scope and rate invalidation; no supplier I/O.
- HTTP/MCP direct and saved starts without a quote, estimates and insufficient funds.
- Zero upfront reservation; two concurrent paid calls cannot overspend a shared wallet.
- Immediate unused-call release; accurate cent rounding; one final charge under retry.
- Parent/child funding, schedule authority, changed limits, unknown usage and legacy runs.
- Free onboarding at zero balance, including trusted initial children.
- Browser regression for no quote dialog, request recovery and uncluttered billing.
- Deploy after focused/full checks. Verify health and authenticated estimate reads;
  do not buy another audit merely to test a bookkeeping change.
