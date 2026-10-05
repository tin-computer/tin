# Failed-payment recovery

`revenue.payment_recovery` is the first workflow in the **Revenue system**. It finds customers
whose automatic Stripe payment failed and is still unpaid, writes each one a short personal email
in the founder's voice, and after one approval sends the emails from the founder's own Gmail.

It needs two connections: Stripe (read invoices, subscriptions, charges and prices) and Google
Workspace (Gmail read and send). Tin's Stripe key stays read-only. Stripe keeps retrying the card
on its own schedule; this workflow adds the personal note that Stripe's automatic emails lack.

The code is `src/tin_lite/payment_recovery.py` (selection, drafting checks, documents) and
`src/tin_lite/payment_recovery_activities.py` (receipts, provider calls, approval). Tests are in
`tests/test_payment_recovery.py`.

## What it reads

From Stripe, through the connection's reviewed operations:

- open invoices created in the window (`lookback_days`, default 30): only automatically
  charged invoices with at least one failed attempt and an amount still owed;
- each candidate customer's subscriptions in every status, for the plan, start date and
  status, so a canceled or paused subscription is seen and left out;
- failed charges in the window, for the decline code, the bank's reason and the card's brand
  and expiry (never its digits). Charges no longer name their invoice, so only a failed charge
  for the same customer and amount, made after the invoice, explains it; otherwise the email
  just says the payment failed;
- prices with their products, for plan names;
- each chosen customer's paid invoices, for how long and how much they have paid.

From the project: the brand guide, project memory, the Start here plan, up to five context
notes and the writing guide (`.agents/skills/writing-style/SKILL.md`). From the founder's
mailbox: how many messages they exchanged with each customer in the last 90 days and the latest
one's subject and opening lines. Only those facts are kept, never the thread. An unreadable
mailbox leaves them out; it never fails the run. A customer already emailed about any of their
open invoices, or whose earlier email is unconfirmed, is listed and skipped before the limit is
applied.

## How the emails are made

Code builds one case per customer: the newest failed invoice, the amount, what went wrong
(first payment, expired card, insufficient funds, a 3-D Secure request or a plain decline), the
plan, tenure, payment history and latest mail. Customers without an email, subscriptions that
already ended and invoices the founder sends by hand are left out and listed. The largest
amounts and longest-standing customers come first, up to `max_customers` (default 10, at most
25).

One model step (`gpt-6-sol`) writes every email. Its input holds no customer email address or card
details. Each draft must have a one-line subject, a 80–1,500 character body, the payment link
placeholder exactly once, no other link, address or placeholder, and no refund, discount,
free period, suspension or deletion: only the founder decides those. Code then puts Stripe's own
`hosted_invoice_url` in place of the placeholder. A draft that fails a check, or a model answer
that is missing or unusable, is replaced by Tin's plain email for that situation, so every
customer still gets one and the run never fails on model output.

## Approval and sending

The plan file (`revenue/payment-recovery/<run>/PLAN.md`) shows every exact email, who it goes
to, the facts behind it and why it is written that way. The Decision approves all of them at once.

After approval, Tin goes through the emails one by one, about 15 seconds apart:

1. If this invoice already has a send receipt for the project, it is skipped.
2. Stripe is asked again for the customer's open invoices; if this invoice is no longer open
   (paid, voided or closed), it is skipped.
3. The email goes out from the connected Gmail address, plain text, with a Message-ID derived
   from the receipt so a retried attempt is looked up in Gmail before it is ever re-sent.

Each invoice's receipt is keyed by project and invoice, so Tin never emails about the same
invoice twice, even from a later run, and a run never sends an invoice another run has
attempted. Each run sends under its own Gmail key.

- Gmail refuses a message outright: nothing was sent, the receipt is removed, and a later run
  may try again. A 401 or 403 (access revoked) or 429 (sending limit) also stops the run, which
  says how many emails went out.
- Gmail's answer is lost: the receipt is kept as unconfirmed, and the result says to check the
  Sent folder. It is never sent again automatically.
- A timeout or other transient error: the activity is retried, and the retry looks for the
  delivered message by its Message-ID before posting.
- Stripe stops accepting the key at the re-check: the run stops with the count sent; a rate
  limit or timeout is retried instead.

When Stripe has more open invoices or failed charges than one run reads, the plan says so.

With a Stripe test-mode key, the run drafts and asks for approval as usual but sends nothing.
When no invoice needs an email, the run finishes without a Decision and says so in
`RESULT.md`.

## Cost

One drafting step under a $2 ceiling; Stripe and Gmail calls are receipted at $0. See
[billing coverage](workflow-billing-coverage.md).
