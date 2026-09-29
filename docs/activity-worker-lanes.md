# Activity worker lanes

## Why this change

Long sandbox activities must not block independent saves, review updates or projections.
Tin keeps their worker capacity separate.

## Small runtime boundary

- The existing queue remains the workflow queue and the **four-slot Codex lane**.
  Both sandbox-create activities, procedure persistence/execution, design execution,
  and task turns stay there. Projects can execute concurrently through the protected API route;
  four is worker capacity, not an account-level serialization rule.
- One activity-only worker polls `<existing queue>-trusted`, with four activity slots.
  An explicit allowlist covers trusted preparation, native work, publication,
  approvals and projections. New activities must be classified at runtime startup;
  unknown activity names never receive trusted routing.
- Both workers live in the existing switchboard process and share its lifecycle.
  Workers shut down before their provider clients/database are closed.
- There are no waiting semaphores consuming the trusted slots, no new service,
  database table, version picker or execution engine.

## Independent project workflows

Independent runs in the same project can execute at the same time. Each run keeps its
own sandbox and checkpoint; the existing four activity slots bound compute per worker.
When those slots are busy, Temporal holds pending activities until capacity is available.
There is no additional per-project execution queue. Prerequisites, task-turn ordering,
and billing admission limits still apply.

The existing internal `tin.project_codex_execution` child uses
`tin.run-codex:<run_id>:<turn_number>` for new execution. It carries identifiers only and
preserves the existing retries and cancellation cleanup. It is not a catalog entry or
another product run. Historical children keep their project-scoped IDs during replay.

Canonical saves still take the existing project lock and use `expectedHeadSha`. Procedure
publication preserves unrelated edits and retains conflicting output for inspection;
task approval applies only its exact reviewed proposal. Parallel compute does not grant
permission to overwrite another run's work.

The outbound interceptor changes only activity routing options, not command order,
activity names, payloads, retries, definitions or effect keys. Routing is a pure
function of the recorded workflow queue and code-defined allowlist; it reads no
environment, database or mutable configuration during replay. Eager trusted
execution on the workflow worker is disabled.

## Upgrade and operations

The original worker retains **all** activity registrations. Activities already
scheduled onto the original queue stay there and drain normally. Newly scheduled
trusted activities use the new lane, including later steps of existing workflows.
Keeping the old queue is essential: moving all registrations would strand pending
activities. Codex remains on the original queue; trusted work retains its own capacity.

New Codex execution uses protected API controllers. The pooled-login broker and local
auth cache are removed. Compute capacity belongs to the worker, not a login account.

Workflow patches preserve old histories and activity routing. Already-running children
finish with their recorded IDs; new runs and later task turns use independent IDs. Keep
legacy child workflow and activity registrations so those histories can drain and replay.

Four trusted slots are bounded too: native work can still wait for capacity.

## Honest progress and retry behavior

Planned drafts say “Ready to draft” while waiting for the writing worker, then
“Writing” only when execution is about to begin. Persistence atomically records
“The result is saved. Finishing delivery.” with its existing receipt. Canonical
publication, review, approval and completion advance the same Postgres projection.
HTTP, MCP and dashboard continue reading Postgres, never Temporal history.

Late persistence/publication retries cannot replace review or terminal progress
with an earlier phase. Repeating a review request after approval cannot move the
run back into `needs_input`. Review remains required; 3/3 describes finished output
work, not approval or website publication.

## Verification

- Real local Temporal test: eight queued Codex tasks, one held open; independent
  save and review complete before release, and maximum concurrent Codex work is one.
- Full mocked procedure travels through preparation, execution, save, review and
  completion on the intended queues.
- An activity scheduled by the old worker drains from the old queue after upgrade;
  its next step uses the trusted queue. Both old and new histories replay.
- Accepted production-history replay includes the new routing interceptor.
- Mixed procedures, code workflows, design runs and task turns in one project overlap
  across independent workers; worker capacity still bounds active compute.
- Review uses no compute slot; cancellation waits for active compute cleanup.
- Worker restart, failed compute, cancelled queue entries and a pre-upgrade paused
  task resuming alongside another run are covered by real local Temporal tests.
- Database tests exercise save/review progress, late duplicate deliveries,
  approved-review retries, atomic projection failure and terminal monotonicity.

These queue tests use no paid provider calls. Production verification must separately
check both pollers, replay actual draft histories, and run idempotent review
projection checks through the deployed trusted worker. Never regenerate, approve,
or publish a user's draft merely to test routing.
