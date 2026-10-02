# Planned content generation

Turn the next roadmap item into one reviewable draft, using the project's current writing
guide. Keep the roadmap unchanged. [Review and revisions](content-review.md) and
[configured delivery](repository-aware-content-delivery.md) use separate contracts;
there is no automatic publication or batch fan-out.

## Implementation

- Register `content.generate` on the existing `codex.procedure` executor, with normal review.
- Choose a saved content program and one non-deferred item. MCP and dashboard use the same
  source selection service and ordinary run endpoint; revision fields stay behind the picker.
- Before paid work, validate ownership, initialized program, research identity, revision holds
  and the selected brief. Pin its exact plan revision, current project/style revision and
  relevant retained research evidence in one existing effect receipt. Missing factual evidence
  remains an explicit verification task, not a reason to invent facts.
- Materialize that selected context through the existing procedure context. The sandbox reads
  the pinned checkout and writes only `content/drafts/{run_id}.md`; retries reuse the context,
  checkpoint, canonical publication and review machinery. No new Temporal implementation or DB.
- Bind the output's provenance and per-requirement verification notes to that context. Mechanical
  validation proves linkage and completeness of the notes, not the truth of marketing claims.
- Reuse Tin's input controls for program/item selection, brief preview and optional direction.
  The result uses the existing Markdown reader, Activity and review controls.

## Next-article default — 1.1.0

The existing workflow now needs only the content program. It follows chronological batch order
and the plan's order within a batch, skipping intentional deferrals and articles with saved
drafts. Awaiting-review drafts count as written, not published. An unfinished article or amendment
hold cannot silently be skipped. This remains an on-demand draft action, not a schedule: a user
may work ahead of editorial dates. There is no new saved workflow/card or automatic publishing.

Before dispatch, the shared run service selects one exact article and plan revision. The run,
selection effect receipt, duplicate check and billing admission commit together under the same
program/project lock order used for amendments. A repeated request returns that run; a concurrent
new default request cannot buy another draft while one is active. The existing preparation
activity validates the selected brief again and pins its research/style context. Retries reuse
the selection; they never pick whichever item happens to be next later.

Progress comes from ordinary runs and their selection/preparation receipts, including older
manual drafts. No plan mutation, new table, backfill or Temporal command sequence is required.
Failed attempts without output remain eligible. Saved conflict output must be resolved first;
once resolved it counts as a saved draft, and an explicit rewrite may create a separate version.
A failed rewrite does not hide the earlier draft. Changed briefs are flagged, not silently redrafted.

MCP defaults to just `program_id`; the dashboard shows “Next article in plan,” its brief and
counts using existing Tin controls. Selecting an individual article is secondary. An existing
draft requires both that explicit selection and `rewrite=true`. Rewriting never overwrites the
old draft. Future configuration can still be saved while a run is active or a batch is held;
admission always rechecks readiness. Legacy saved definitions keep their explicit-item contract.

MCP `get_run` returns the same Postgres mode, step, counts, percentage, summary and
update time used by the dashboard, including the selected article before compute. This is a
read-only projection addition for every workflow, not a new progress subsystem.

## Plan item kinds — 1.9.0

A plan item may be an article, an answer page for an AI-visibility gap, or a refresh of an
existing page. 1.9.0 drafts all three under the same editorial pair, with each kind's own
checks, review wording and delivery: an answer goes to the site through `website.change` at the
founder's route, and a refresh goes through the refresh applier. Older pins draft articles only.
See [one content.generate](one-content-generate.md).

## Acceptance

Test invalid/cross-project/deferred/stale selections before compute; unchanged retry binding;
retention of later plan/style edits; bounded context; source gaps; output linkage; one separately
addressable draft per run; ordinary publication/review; and MCP/HTTP parity. Browser fixtures
cover existing control styling, loading/error/empty states and project switching.

Use authorized synthetic project context for integration tests. Confirm the roadmap and writing
guide remain unchanged and that the artifact is readable after sandbox deletion. Never approve
a customer draft as an implicit test. A provisional style guide is not approved personal voice.
