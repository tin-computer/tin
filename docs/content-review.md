# Content review and revisions

`content.generate` and `content.public_article` share one review service across the
dashboard and MCP. The same contract applies to a standalone article and a draft
inside an organic traffic system. Feedback revises that item; it does not select
the next roadmap item.

## Product behavior

- Reading opens the clean article. Request changes opens the inline text composer;
  closing it preserves unsent feedback. Users can name project files in their text.
- Submitting feedback starts a separately accounted successor run. The existing
  decision points to the newest complete copy, with Previous → Revised comparison.
- Generation notes describe changes and feedback not followed. They remain a
  separate artifact and viewer action, never text or a link embedded in public copy.
- Approval is bound to the current reviewed revision. An old approval cannot approve
  a newer draft. Only the final approved copy is eligible for configured delivery.
- A failed revision needs explicit retry. Feedback is not discarded by generic retry,
  and a stopped revision closes its waiting review ancestry.
- Discard turns down anything waiting in Decisions: the run ends as declined, nothing it
  proposed is used, and its files stay readable in Files. The dashboard's Discard and MCP
  `discard_workflow_review(run_id)` share one service (`proposal_decline.discard_review`):
  the same membership check, the same refusal for a decision with nothing to approve, a
  one-off task stopped instead, and the same result when discarded again.

Hiding a template from discovery does not remove review or delivery controls from
existing drafts. The dashboard resolves their workflow metadata by ID independently
of the discovery list; this does not make the template discoverable again.

## Contributor invariants

The `content-revision.v1` adapter pins the reviewed copy, original brief and evidence,
writing guide, feedback and additional reference snapshot. It must not rewrite the
roadmap, style guide or destination. Retried preparation reuses its context receipt.
Missing or ambiguous references belong in Generation notes.

One project-row transaction arbitrates approval versus revision, stores an idempotent
command, funds the successor and supersedes the old review. The durable command
outbox bridges dispatch; Temporal carries command/run identifiers, not feedback or
article contents. Preserve historical adapter/output contracts and exact-version
guards. Do not infer approval from a generated no-draft assessment.

The shared implementation lives in `src/tin_lite/workflow_reviews.py`,
`workflow_review_store.py`, `workflow_review_dispatch.py` and `article_review.py`.
The dashboard composer is `src/tin_lite/static/workflow-review.js`.
Run focused service/Temporal review tests and `npm run test:workflow-review-browser`
when changing this contract. Fixture checks do not imply live provider or customer
acceptance; never approve a customer's draft as an incidental test.

X uses the same review endpoints with its own [file-based revision contract](x-workflow.md#feedback-that-improves-the-next-post). X feedback can automatically update the account's writing guide; the article adapter above continues to keep feedback local to its draft.
