# Invoiced workspaces

Some workspaces belong to managed clients whom Tin bills outside the product. Their work should
not stop for lack of credits, and their admins should never be sent to Stripe. An operator marks
such a workspace's wallet as **invoiced**.

## What changes

`billing_accounts.funding` is `prepaid` (the default) or `invoiced`. For an invoiced wallet:

- A start and each paid step skip the credit check. Nothing else is skipped: runs are metered,
  reserved and settled as usual, every charge is posted to the ledger, and the project's per-run,
  monthly and schedule limits still stop runaway spending. Raise those limits on the Billing page
  for a client that needs more.
- The balance falls below zero as charges settle. Usage past the credits is still to be
  invoiced and paid.
- Checkout is refused. Instead of the top-up form, the Billing page shows **Credits left** while
  the balance is positive, then **Used** (usage past the credits, not yet paid), with this
  month's workspace usage. `get_project_spending` returns `funding`, and for the billing admin
  `credits_left_usd`, `used_usd` and `workspace_spent_this_month_usd`.
- Welcome credits and any earlier top-ups are spent first, exactly as before.

Funding is a property of the wallet, read when a run starts and at each paid step. It is not
pinned on the run: a wallet switched back to `prepaid` with a negative balance stops buying paid
steps until the balance is cleared, the same way a suspended wallet does. Rates, ceilings and
the funding policy of each run stay pinned in its terms.

## Operator commands

Only the operator CLI changes funding. No dashboard or MCP control exists, so a founder cannot
mark their own workspace. Every change is kept in the append-only `billing_funding_changes`.

```bash
# Bill a managed client's workspace by invoice
tin-lite set-billing-funding --workspace-id <uuid> --funding invoiced \
  --reason "Managed client: Cruvo, invoiced monthly"

# Record an invoice the client paid (idempotent per reference, never more than the unpaid usage)
tin-lite record-invoice-payment --workspace-id <uuid> --amount-usd 412.50 --reference INV-2026-031

# Back to credits
tin-lite set-billing-funding --workspace-id <uuid> --funding prepaid --reason "Contract ended"
```

The workspace must already have a billing account, which hosted Tin creates the first time one
of its projects starts a run or opens Billing. Mark the workspace that holds only that client's
projects: the flag covers every project in it. A recorded payment is an `adjustment` ledger row;
it settles charged usage and never grants credits.

To read a month's usage for an invoice, sum the workspace's `charge` rows in `billing_ledger`
for that month; each row names its project and run.
