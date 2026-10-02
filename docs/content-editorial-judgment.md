# Editorial judgment before planned drafting

## In plain language

A calendar entry is not an obligation to publish. Before writing, Tin compares the selected
brief with current pages. It drafts only when it can explain a worthwhile reader benefit.
Otherwise it saves a short assessment instead of another article to approve.

## Contract

New `content.generate` 1.4.0 definitions pin `content-draft.v3`. The same bounded Codex
procedure first inspects the relevant site index, exact update destination and nearby coverage
(up to 12 pages). It considers intent and actual coverage, not just matching keywords.
No second model route, activity sequence, progress table or workflow executor is introduced.

There are four outcomes:

| Outcome | Result | Next selection |
| --- | --- | --- |
| Draft | Clean article plus generation notes; normal approval and configured delivery | Existing article ordering |
| Already covered | Short assessment plus notes; no article approval or delivery; the run and the plan item record the page that covers it | The item is marked covered; the next manual or weekly start selects the next item |
| Brief needs revision | Assessment explains necessary change of target/intent/scope | Hold until amendment or explicit recheck |
| Insufficient evidence | Assessment explains missing/unavailable coverage | Hold until amendment or explicit recheck |

No outcome automatically starts another item, advances a schedule, changes the plan file,
approves an earlier draft, or publishes anything. `rewrite=true` with an explicit item is also
the existing intentional recheck control; an amended brief is eligible again without it.
Unchanged already-covered items are counted separately from drafts. A failed new attempt
does not hide an earlier saved result. A completed assessment can supersede an older draft's
relevance, but its files and pending approval remain independently accessible.

Updates must name a substantive gain and bounded change, preserving the rest of the existing
page's useful content. Delivery still consumes complete approved page copy. Section-only
snippets must not masquerade as whole-page replacements. A materially different target or
an update that cannot preserve the page within this contract requires replanning; this slice
does not introduce section-patch delivery or automatic website editing.

## Durable output and validation

The existing two paths and atomic CAS/checkpoint mechanism are unchanged:

- `content/drafts/{run_id}.md`: article copy, or exactly `# Content assessment`, a blank line,
  the rationale and a final newline for no-draft outcomes. The path is a run-owned result slot;
  it does not make an assessment into an article.
- `content/drafts/{run_id}.generation.md`: bound source provenance, structured editorial
  judgment, source context and requirement accounting (24 KB).

The judgment is one JSON block under `## Editorial judgment`, containing `outcome`,
`rationale`, `reader_gain`, `change_scope`, and `compared_pages` (URL, inspected/unavailable,
coverage). Validation rejects unsupported outcomes, duplicate fields/pages, credential URLs,
unbound notes, missing reader gain/scope for drafts, absent inspected own-site coverage,
uninspected update destinations, and article/assessment disagreement. Unavailable pages do
not establish already-covered status. Mechanical checks validate the contract and accounting,
not the truth of an editorial verdict or a promise of ranking/citation gains.

Both checkpoint files are validated before publication. The canonical commit receipt carries
the small validated `content_editorial` projection (`content-editorial-check.v1`). Only that
run/revision-bound proof lets this template complete without an article review. Finalization
clears the run's `review_required` flag for such a result, because nothing waits for a
decision (run 1e474e10 kept the flag with no decision, so it read like a draft waiting in
Decisions); no approval is manufactured and `review_decision` stays empty. Finalization still
rejects terminal failures/stops and an already-open review gate. Delivery ignores no-copy
outcomes, and its article extraction independently rejects assessment documents.

An already-covered result also names the page that covers the brief: the first inspected
compared page on the planned site. The canonical commit receipt records it as `covered_by`, the
run summary starts "Already covered by <page>", and program progress shows the item as
`covered` (`page`, `reason`, `run_id`), so the plan editor links the page and the next draft
moves on. Runs saved before `covered_by` existed get the same page from their judgment.

HTTP/MCP and My System read the same receipt-backed progress. Assessments are readable through
Activity and the generic reader; the existing roadmap topic disclosure links to the assessment.
They are not included in article delivery, draft counts, or Decisions. Related generation notes
stay in reader context, never embedded in public copy.

## Compatibility and rollout

Historical v1/v2 definitions, artifacts, runtime footer instructions and approvals are unchanged.
The Temporal command sequence and payloads are unchanged: the existing review activity returns
false only for a newly validated no-copy result. The ordinary result activity records success.
No migration is required. Native planners, Browser/Studio profiles, auth and billing remain
unchanged. Default/isolated images must pass `--check-editorial` before dispatch; old images fail
before context transfer or model execution rather than forcing article-only output.

## Verification

Local tests cover all four outcomes, provenance and malformed judgment rejection, real Postgres
publication/finalization retry, no Decisions or automatic delivery for assessments, normal article
approval, HTTP/MCP readability, plan preservation, ordering, amendment eligibility and old-image
rejection. Existing v1/v2 publication, retained-output, billing and Temporal replay tests remain.
Browser fixtures cover escaped assessment reasons, separate counts, read/recheck controls,
light/dark themes and narrow screens. These deterministic fixtures are not a live model-quality
claim; a live editorial assessment must be examined separately.
