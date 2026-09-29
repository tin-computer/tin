# Procedure publication recovery — first half

This document records the accepted first-half release. The later, explicitly member-directed
comparison/application backend is specified in [Saved output resolution](output-resolution.md);
it does not change the recovery or original-run semantics documented here.

## ELI5

A finished procedure keeps its work in a sealed, run-specific snapshot. If someone edits
another file while it runs, Tin can still save the result without losing that edit. If they
edit the same file, Tin leaves their version alone and offers the generated result to read
or download. No filename prefixes or duplicate files appear in canonical project Files.

If a save succeeded but its acknowledgment disappeared, retry finds that exact save instead
of generating the work again or creating another commit. A failed run can still have a
readable result. This release does not add an action to apply a conflicting result.

## Scope and invariants

- Applies to project-artifact results of `codex.procedure`, including declared Markdown,
  CSV, and Mermaid output. It does not change project-file CAS, task exact-diff approval,
  or GitHub delivery semantics. Broader native workflow publication remains a separate follow-up;
  the narrow visibility finalization correction below is now included.
- `procedure_artifact_persist` records one validated immutable checkpoint: run, project,
  generation, definition revision, original project revision, checkpoint revision, path,
  media type, byte count, and SHA-256. No output body enters Postgres or Temporal history.
- Both ordinary completion and recovered-branch completion read the resolved commit SHA,
  not a moving branch. Older persisted receipts are validated and pinned in the unfinished
  publication receipt without rewriting their completed result.
- Before any canonical write, compare the original and current destination entry, including
  file mode and bytes. Missing files differ from deletion, directories, and symlinks. Unsafe
  parent entries fail closed. An unrelated edit is retained by writing onto current HEAD.
- Persist the attempted parent and checkpoint before sending the single-file commit. Validate
  the active session lease/fencing tuple immediately before sending with `expectedHeadSha`.
  A CAS rejection or uncertain response is reconciled before another write can be attempted.
- Reconciliation follows the captured HEAD's actual single-parent chain, matching the exact
  execution marker, saved parent, declared path, diff scope, and content digest. Later edits
  do not hide an earlier successful publication. Read-only recovery needs no active lease;
  any new write still does. No-change results reuse their verified revision without a commit.
- Receipt completion is monotonic. A later failure cannot overwrite a completed effect;
  a different completion result is rejected. Procedure availability, canonical receipt, and
  commit event are one Postgres transaction. Success, projection receipt, and product event
  are another transaction, still gated by the existing review decision.
- Persist replay repairs missing availability/events and retries unfinished sandbox cleanup.
  Completed publication replay repairs availability and lease cleanup. Completed success
  projection replay repairs a missing ready event, without reviving failed runs.

## Read-only product contract

Migration `024_retained_procedure_output.sql` adds nullable `workflow_runs.retained_output`.
Old rows remain valid. Public run views expose only path, revision, media type, byte count,
and `publication_pending`, `reconciliation_pending`, `output_conflict`,
`execution_interrupted` or `not_published` reason. `not_published` marks a finished result
that its validator kept as a diagnostic and Tin will not publish, such as an analytics brief
that measured nothing.

- `GET /api/workflows/runs/{id}/artifact?source=retained` returns validated checkpoint bytes.
- The existing `/artifact/document` adapter accepts the same `source` option for Markdown.
- Omitting `source` preserves canonical reads; availability no longer depends on run success.
- MCP `get_run` exposes availability; `read_run_output(run_id, source)` reads either source.
  MCP text is bounded to 100,000 characters and explicitly marks truncation. HTTP supplies
  the full bounded artifact.
- HTTP and MCP authorize project membership before any storage read. Neither queries Temporal.
- Chat, run details, and Activity offer `View generated result`; Markdown uses the existing
  reader and other formats use an authenticated download. Polling notices availability changes.
  Retained readers have no new approval, replacement, publishing, or conflict-resolution action.

## Interrupted Codex procedures

An interrupted paid attempt is not permission to purchase another attempt. The relay records
allowlisted spending/request/token stop codes in the existing attempt receipt after admission
rolls back. Cleanup merges its outcome into that receipt without replacing the stop code.
Retries check completed output first, including the immutable revision recorded when the
controller returned. Without completed output they stop before allocating another sandbox and
report the original trusted stop reason, or an explicit failed/interrupted/unconfirmed outcome.

Before destroying an interrupted isolated sandbox, Tin revokes its model admission and makes
one bounded attempt to retain changed Markdown. The installed isolation helper first kills
the author and freezes regular files; unchanged, missing, linked, oversized, non-UTF-8, or
credential-bearing output is excluded. The switchboard revalidates the pinned output contract.
This covers default/isolated plain Markdown research, `memory-section.v1` code maps and
`analytics-brief.v1` reports, whose partial text is checked only for size; section
ownership still applies. Browser/Studio profiles, identity-enabled procedures, companion
documents and other validators remain excluded.

An incomplete result goes only to `interrupted-procedures/{run_id}/{generation}`, with a
separate metadata-only intent/completion receipt and an `execution_interrupted` retained-output
projection. A lost storage acknowledgment can be reconciled from that branch before the run
fails. It cannot complete the normal persist receipt, reach canonical Files, or enter the saved
conflict application flow. Chat, Activity and run details label it **Partial result**. A partial
result passing structural checks is still incomplete; those checks do not prove the task finished.

Retention is best effort, bounded to 45 seconds after admission revocation. If the sandbox has
already disappeared or no acceptable changed output exists, the failed run has no retained
file. This does not reconstruct historical output lost with deleted sandboxes, restart work,
refund verified usage, change a quote, or bypass review. No sandbox image rebuild or database
migration is needed; capture uses the existing protected runtime's freeze command.

## Storage contract and bounds

The pinned SDK lacks these read APIs, so a narrow switchboard-only adapter uses documented
`commits` and `files/metadata` endpoints with scoped read JWTs. Writes keep the existing SDK.
The live probe confirmed ephemeral commits are readable by immutable SHA through the shared
object store. The [ephemeral-branch contract](https://code.storage/docs/guides/ephemeral-branches)
does not specify automatic expiry. Do not delete checkpoint branches while retained output
references them. Do not add branch cleanup without a retention/migration contract.

Reads compare at most 1 MB per file, cap metadata responses at 2 MB, and use 20-second HTTP
timeouts. Publication/reconciliation is bounded to 120 seconds and 20 history pages of at most
100 commits. Pagination assembles the parent chain rather than trusting date order. Incomplete,
rewritten, merge, malformed, or over-budget history never proves absence and therefore never
authorizes a duplicate write. Retry starts another bounded scan; there is no background scanner.

## Single-run operator recovery after retries are exhausted

1. Inspect the exact run and its persist, canonical, and projection receipts. Keep payloads,
   credentials, and file contents out of logs. Do not restart Codex to recover saved output.
2. If a completed canonical receipt exists, verify its immutable artifact (and checkpoint digest
   where present). Invoke `commit_codex_procedure_artifact` for that one run using the trusted
   runtime. Its completed-receipt path repairs availability/events and lease cleanup only; it
   does not republish. The run's failed/stopped status remains unchanged.
3. If only a completed persist receipt exists, verify the checkpoint identity and bytes first.
   Its persist replay repairs retained availability and cleanup without creating a sandbox.
   Legacy persist receipts without immutable metadata require validation/pinning through the
   publication recovery path; never guess a revision or blindly backfill a moving branch.
4. For an unfinished publication with a saved intent, a terminal run must have an inactive lease
   before invoking its canonical activity. It may reconcile an existing effect, but cannot make
   a new write. If history is incomplete or beyond the budget, retain the result for inspection;
   do not remove the intent, change its parent, force a commit, or declare the effect absent.
5. Do not call the final success projection to revive terminal failure, bypass required review,
   or bulk mark historical runs successful. Active workflows continue through Temporal's
   existing activities/signals. Conflict application is explicitly deferred.

## Visibility finalization — narrow follow-up

ELI5: once the report and evidence are safely saved and checked, finishing the run should not
depend on downloading them again. Tin records a small proof of that save, then updates the run,
its ready event, and its completion receipt together. A database interruption leaves all three
unfinished so retry can finish them consistently.

- Validate the bounded report/evidence pair before the existing atomic canonical publication.
  The completed `visibility_commit` receipt binds the run, project, pinned definition, canonical
  revision, exact paths, byte counts, and SHA-256 digests. It contains no artifact bodies.
- Modern projection validates those trusted receipt facts without a code.storage read. It writes
  success, the single product-ready event, and `visibility_projection` in one Postgres transaction.
  Failed/stopped runs and unapproved review gates cannot be turned into success by this helper.
- A legacy commit without publication facts gets one bounded, immutable-revision validation of
  both files, recorded separately as `visibility_publication_validation`. Retry reuses that proof
  without overwriting the completed old commit receipt. Malformed modern facts fail closed.
- Completed generation replay returns before reading sources or repeating model calls. Existing
  Temporal activity names, payloads, and workflow sequence are unchanged.
- This does not replace the shared native commit helper, add conflict-application UI, or generalize
  the procedure checkpoint/reconciliation contract to every native workflow.
