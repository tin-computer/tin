# Email campaign retro

Compared 3 recipients across 2 cohorts (4 recorded touches). A cohort is one segment and the subject its recipients were sent.

## Results by cohort

| Segment | Subject | Recipients | Replied | Reply rate | Bounced | Touches to reply |
|---|---|---|---|---|---|---|
| Designers | Quick question about your Figma handoff | 2 | 2 | 100% | 0 | 1.5 |
| Marketers | Grow your pipeline without tools | 1 | 0 | 0% | 1 | - |

## What the numbers say

- **Designers / Quick question about your Figma handoff: segment performs better.** Designers replied at 100% (2 of 2) versus 0% (0 of 1) for Marketers.
  - Next action: Weight the next shortlist toward designers; rewrite the marketer opener.
- **Marketers / Grow your pipeline without tools: bounce rate too high.** Bounce rate 100% for this cohort (1 of 1).
  - Next action: Verify marketer addresses before the next send.

## Campaign health

- Reply rate overall: 67% (2 of 3 recipients).
- Bounce rate overall: 33% (1 of 3 recipients).
- Shortlist cross-check: 3 of 3 shortlist emails appear in the ledger; 0 ledger recipients are not on the shortlist.

## Method and limits

- Computed only from the supplied ledger and shortlist (4 recorded touches: initial sends plus follow-ups). No external data, no inferred sends.
- Reply and bounce rates are per recipient. Touches to reply uses the touch number recorded with the reply; '-' means no reply carried a touch number.
- Statistics are pure Python. Interpretation comes from one managed model call, checked against the computed rates before it is shown.
