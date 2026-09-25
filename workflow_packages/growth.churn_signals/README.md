# growth.churn_signals

## Find churn risks and what to tell each customer

Most growth workflows focus on acquiring new customers. This workflow focuses on keeping the customers you already have.

Founders often discover churn risks only after customers cancel or stop engaging. By the time support tickets, reviews, and feature requests reveal a pattern, revenue may already be lost.

This workflow analyzes customer feedback, finds complaints that are increasing over time, estimates the revenue affected, checks whether recent releases already fixed them, and gives each affected customer one action with a draft note. It produces a ranked retention plan that founders and customer-success teams can use before customers leave.

## Why this workflow exists

Customer acquisition is expensive. Retaining existing customers is often a faster and more cost-effective way to grow recurring revenue.

Most small teams collect feedback in support systems, review sites, and feature-request boards, but they rarely have a systematic process for answering questions such as:

- Which complaints are increasing?
- Which issues affect the highest-value customers?
- Which cancellations share a common cause?
- Which customers should we contact after a fix is released?
- Which problems represent the greatest revenue risk?

This workflow provides a repeatable process for turning customer feedback into retention actions.

## Who runs it, and when

### Primary Trigger: a weekly retention review, run on demand

A founder, product manager, or customer-success lead runs the workflow once per week after exporting recent:
The founder runs it by hand each week after exporting new feedback. It isn't scheduled, because the inputs are pasted exports.

### Secondary Trigger: After a Product Release

After shipping a release, the workflow checks whether previously reported problems have been fixed and identifies customers who should be informed.

This helps teams close the feedback loop and recover at-risk customers.

---

## Inputs

| Input | Required | What it is |
|---|---|---|
| `feedback_csv` | yes | `date, customer_id, source, text`: tickets, reviews and feature requests in one export |
| `customers_csv` | no | `customer_id, plan, mrr, status` from billing; without it, ranking uses customer counts |
| `changelog` | no | Recent release notes, used to tell customers their problem is fixed |
| `winback_offer` | no | An offer in your exact words; drafts never invent one |
| `recent_days` / `baseline_days` | no | Trend windows, default 14 and the 42 days before |
| `top_risks` | no | How many risks get message plans, 1–6, default 5 |
| `as_of` | no | End date for a reproducible run |

## How it works

| Step | Who | What happens |
|---|---|---|
| 1. Parse | code | Read both CSVs, drop bad dates, empty text and duplicates |
| 2. Sample | code | Keep rows inside the two windows. Above 80 rows, thin them evenly across time so the baseline is not starved |
| 3. Tag | model `tag` | Give each row a specific issue name, category, severity and whether it talks about cancelling |
| 4. Check | code | Every ID exactly once, known categories, usable issue names, at most 15 issues |
| 5. Measure | code | Per issue: recent count against what the baseline rate predicts (a c-chart test at 2σ), active and cancelled customers, MRR at risk and lost, plan concentration |
| 6. Rank | code | active MRR (or customers) × severity × trend × (1 + share of cancel talk) |
| 7. Plan | model `plan` | For the top risks: a title, whether the changelog fixes it (with verbatim evidence), and draft notes |
| 8. Check | code | Every risk ID exactly once; claimed fixes must quote the changelog or are treated as open; no placeholders; no offers unless one was supplied |
| 9. Moves | code | Each known customer gets one move, from the highest-ranked risk that warrants a message |

Moves: **tell them it's fixed** (active, issue fixed), **acknowledge before they cancel**
(active, open, and rising, new, severe or talking about cancelling), **win back** (cancelled,
issue fixed), **hold until fixed** (cancelled, still open) and **no message yet**.

## Output

`reports/CHURN_SIGNALS.md`: the ranked risks with the numbers behind each, recent customer
words, the changelog evidence, draft notes per move, a who-to-contact table and the ranking
method.
Nothing is sent. The drafts are for the founder to review and send.

## Tests

`tests/test_churn_signals.py` runs offline with synthetic data. It covers the full path, fake
IDs and too many issues from the tagging step, an invented risk ID, a claimed fix the changelog
does not contain, an offer that was never supplied, the no-complaint path and even sampling.

## Success Metrics


The workflow aims to improve:

- Retention of at-risk customers
- Revenue retention (MRR at risk that is saved)
- Reactivation of cancelled customers
- How quickly customers hear about fixes after a release

Teams can measure impact from the report and their billing export:

- **Save rate:** share of customers marked *Acknowledge* still active 30 days later
- **Win-back rate:** share of customers marked *Win back* who reactivate within 30 days
- **MRR at risk:** total across ranked risks, falling run over run
- **Rising issues:** fewer issues marked *rising* or *new* each run
- **Fix-to-notice time:** days from a changelog entry to telling affected customers

---
