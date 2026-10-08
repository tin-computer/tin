# Failed-payment recovery

`revenue.payment_recovery` is the first workflow in the **Revenue system**. It finds people
whose automatic Stripe payment failed and is still unpaid, writes each one a short personal email
in the founder's voice, and after one approval sends the emails from the founder's own Gmail.

It needs two connections: Stripe (read invoices, subscriptions, charges and prices) and Google
Workspace (Gmail read and send). Tin's Stripe key stays read-only. Stripe keeps retrying the card
on its own schedule; this workflow adds the personal note that Stripe's automatic emails lack.

The code is `src/tin_lite/payment_recovery.py` (selection, drafting checks, documents) and
`src/tin_lite/payment_recovery_activities.py` (receipts, provider calls, approval). Tests are in
`tests/test_payment_recovery.py`.

## Inputs and schedule

| Input | Default | Meaning |
| --- | --- | --- |
| `lookback_days` | 30 | Open invoices created in this window (1–90 days). |
| `max_customers` | 10 | People emailed per run (1–25), largest amounts first. |
| `products` | all | Stripe product names or ids. Use it when one Stripe account bills for several products or businesses. |

A new setup runs **weekly** (Mondays 09:00 in the project's timezone); monthly and on demand are
also offered. Stripe retries a failed card for two to four weeks, so a weekly run reaches people
while the card is still being retried; a monthly one often finds the subscription already
canceled.

## What it reads

From Stripe, through the connection's reviewed operations:

- open invoices created in the window: only automatically charged invoices with at least one
  failed attempt and an amount still owed, and, with `products`, only invoices billing one of
  them;
- prices with their products, for plan names and the product list;
- for each candidate: their subscriptions in every status (a canceled or paused one is left
  out), their failed charges since the invoice (the bank's decline reason and the card's brand
  and expiry, never its digits), and, for the people emailed, their paid invoices.

Charges no longer name their invoice, so only a failed charge for the same customer and amount,
made after the invoice, explains it; otherwise the email just says the payment failed. The
decline is turned into a plain reason the customer can act on; Stripe's internal risk reasons
are never shown.

From the project: the brand guide, project memory, the Start here plan, up to five context
notes and the writing guide (`.agents/skills/writing-style/SKILL.md`). The plan says when there
is no writing guide. From the founder's mailbox: the latest exchange with each person in the
last 90 days (only its subject, date and opening lines are kept, never the thread), and whether
the founder wrote to them in the last 14 days. An unreadable mailbox leaves those facts out; it
never fails the run.

## Who gets an email

Invoices are grouped by **email address**, so several Stripe customer records with one address
are one person, emailed once about their newest failed invoice. A person is left out when:

- their address is a throwaway inbox or a reserved name (yopmail, mailinator, `example.com`,
  `.test` and similar); these are only counted;
- Tin emailed them in the last 30 days, or emailed twice about the same subscription, or an
  earlier email to them is unconfirmed;
- the founder wrote to them in the last 14 days (that conversation is theirs);
- their subscription is canceled, paused or unreadable, or Stripe has no address for them.

Each reason is listed, except throwaway addresses and people beyond the run's limit, which are
one count line each with the first few names. When the account bills products the run left
out (or, with no choice, several products), the plan names them and suggests the products whose
names match the project.

## How the emails are made

One model step (`gpt-6-sol`) writes every email. Its input holds no customer email address,
card detail or date. Each draft must have a one-line subject, a 80–1,500 character body, the
payment link placeholder exactly once; no other link or bare domain, address, phone or account
number, or placeholder; no date, weekday or deadline (an email may be sent days after it is
written); no other way to pay or to send card details; and no refund, discount, free period,
suspension or deletion. These checks hold whatever a customer's mail asked the model to write.
Code then puts Stripe's own `hosted_invoice_url` in place of the placeholder. A draft that fails
a check, or a model answer that is missing or unusable, is replaced by Tin's plain email for
that situation, so every person still gets one and the run never fails on model output. In the
plan, each email is shown as exact plain text and customer-supplied text is escaped.

## Approval and sending

The plan file (`revenue/payment-recovery/<run>/PLAN.md`) shows every exact email, who it goes
to, the facts behind it and why it is written that way. The Decision approves all of them at
once. **If nobody decides within 6 days, the Decision closes unsent** (shorter than a week, so it never overlaps the next weekly run): the run stops, the plan
stays in Files, and the next scheduled run can start (a waiting run would otherwise block it).

After approval, Tin goes through the emails one by one, about 15 seconds apart:

1. If this invoice already has a send receipt for the project, it is skipped.
2. Stripe is asked again for the customer's open invoices; if this invoice is no longer open
   (paid, voided or closed), it is skipped.
3. The email goes out from the connected Gmail address, plain text, with a Message-ID derived
   from the receipt so a retried attempt is looked up in Gmail before it is ever re-sent.

Each invoice's receipt is keyed by project and invoice and records the address and
subscription, so later runs apply the per-person limits above. A run never sends an invoice
another run has attempted, and each run sends under its own Gmail key.

- Gmail refuses a message outright: nothing was sent, the receipt is removed, and a later run
  may try again. A 401 or 403 (access revoked) or 429 (sending limit) also stops the run, which
  says how many emails went out.
- Gmail's answer is lost: the receipt is kept as unconfirmed, and the result says to check the
  Sent folder. It is never sent again automatically.
- A timeout or other transient error: the activity is retried, and the retry looks for the
  delivered message by its Message-ID before posting.
- Stripe stops accepting the key at the re-check: the run stops with the count sent; a rate
  limit or timeout is retried instead.

With a Stripe test-mode key, the run drafts and asks for approval as usual but sends nothing.
When no one needs an email, the run finishes without a Decision and says why in `RESULT.md`.

## Cost

One drafting step under a $2 ceiling; Stripe and Gmail calls are receipted at $0. A run with
no one to email makes no model call. See [billing coverage](workflow-billing-coverage.md).
