# Private code workflow schedules — Slice D

Slice D extends the existing saved project configuration and Temporal Schedule contract to
eligible `workflow.code` packages. It adds no executor, scheduler, version picker, credential
mode, or approval step. Private Codex procedures remain manual. Existing definitions with
`schedule_modes: [on_demand]` retain that contract; authors explicitly add `daily` and/or
`weekly` to a new package revision when appropriate.

## Author, connect, save, run

1. Author and test ordinary Python locally using the examples from
   `get_workflow_authoring_guide`. Declare inputs, output, model routes and service capabilities.
   Keep keys out of source and prompts. Publish project files, validate the exact revision,
   then activate it through the existing package tools.
2. Call `get_code_workflow_setup` with the project and workflow ID plus inputs. It validates
   inputs and returns connection readiness, the configured Tin cost bound, and schedule funding
   issues without making a paid call. External provider charges remain separate and unknown.
   The dashboard uses the same service at `POST /api/projects/{id}/workflow-setup`.
3. Supply connections in Integrations. Credentials remain encrypted on the switchboard;
   isolated code receives only declared managed operations. Rotation affects fresh requests.
   A completed step retains its original response under the existing recovery contract.
4. `create_project_workflow` saves inputs and an optional schedule, selecting the current active
   definition automatically. For example:

   ```json
   {
     "cadence": "weekly",
     "weekdays": ["monday", "friday"],
     "local_time": "09:00",
     "timezone": "America/Los_Angeles",
     "end_at": "2026-12-01T00:00:00Z"
   }
   ```

5. Saving a schedule over MCP also runs the configuration once right away: `create_project_workflow`
   returns `first_run` with that run's id, or why it could not start (the schedule is saved either
   way), billed like any run. The dashboard keeps its choice between "Set up" and "Set up and run
   now". `start_project_workflow` runs a saved configuration again. The calendar continues independently
   of the coding agent or browser. My System shows next run, latest result, editable inputs,
   connection/setup issues and the estimate. Code forms preserve multiple weekdays and existing
   start/end bounds; date bounds are authored through MCP/API and displayed in the editor.
6. `update_project_workflow` changes future runs using the expected settings revision. Omitting
   `schedule` keeps it. `clear_schedule=true` removes it. Existing saves and accepted runs keep
   their exact definition. `set_project_workflow_paused` and `archive_project_workflow` provide
   the same controls as the dashboard; archive preserves runs and files.

## Authority, recovery and cost

- Every occurrence rechecks the saved creator's project membership, the execution gate,
  pinned definition, required connections and current funds. Removing the creator's membership
  stops fresh dispatch. Another member can create a new configuration under their own identity.
- Transactional admission rejects a stale settings revision or an overlapping active run.
  The accepted run and next-run projection commit together. A lost activity response recovers
  the same run and its accepted inputs even if future configuration inputs changed.
- Persistent setup/funding issues pause the configuration before pausing its Temporal clock.
  One configuration Activity issue is deduplicated per settings revision. Resume rechecks setup.
  Custom API 401/403 responses mark the connection for attention only if the credential and
  configuration revisions still match; rotating the key clears that authentication issue.
- Temporal retains skip-overlap and a 24-hour catch-up window. Older/ended occurrences are
  skipped. The next-run projection follows Temporal's own calendar walk across DST changes:
  depending on the zone a repeated local time fires once or twice, and a nonexistent one is
  skipped or moved. End bounds retain the product's existing exclusive-end behavior.
- Postgres decides what runs. A configuration becomes `active` only after Temporal accepted
  its calendar; a failed sync marks it `failed` with no next run and pauses the old Temporal
  calendar when it can, and any occurrence Temporal still fires for a failed, mid-save or
  paused configuration starts nothing. A failed configuration can be paused; resuming sends
  the saved calendar to Temporal again rather than unpausing whatever Temporal held. A pause
  made while a settings save is in flight stays in force.
- New or changed schedules must use an IANA zone name (not the host's `localtime`, `Factory`,
  or a `posix/` or `right/` copy) and must still have a run before their end. Schedules saved
  earlier keep loading and dispatching, and an edit that leaves them unchanged is accepted.
- Code-only bounded compute remains included at zero credits. Model schedules require the
  existing credit account and sufficient standing schedule spending limit; each occurrence and
  paid call still passes authoritative ledger admission. A setup estimate grants no spending.
- Completed named model/API steps project progress from durable effect receipts into Postgres.
  There are no invented percentages, execution-state files, or new history-reading product APIs.

## Verification

- Disposable real Postgres and MCP/HTTP tests cover saved inputs, zero-credit admission,
  duplicate dispatch, lost response, stale settings, overlap, rotation/removal, removed
  membership, insufficient funds, standing limits, end/catch-up bounds and archive. Compute,
  supplier transport and Temporal handles in these unit/integration cases are fixtures.
- Packaged dashboard tests use real Chromium with fixture identity/HTTP in light and dark
  themes. They verify shared setup facts, pinned schedule modes and lossless schedule edits.
- The opt-in `test_code_schedule_live.py` uses a real local Temporal calendar, E2B and
  code.storage with disposable Postgres and fixture OAuth verification. The authoring MCP client
  closes before the calendar fires. Verify the report after sandbox deletion, zero model
  calls/credits for code-only work, and history replay. Paid-model calendar acceptance
  requires separate authorized provider access and a funded test ledger.

## Unchanged recurring reports

When a validated report is byte-identical to the checkpoint, staging reuses the
immutable revision instead of creating an empty commit. Publication still checks
the pinned base, current destination and lease, and records a no-change receipt.
Concurrent destination edits remain conflicts; no synthetic file or commit is added.

## Remaining scope

Direct SDK/customer-secret execution, procedure delegation, broader languages, model BYOK,
recursive starts, queue fairness activation, live payments and broader rollout are not included.
