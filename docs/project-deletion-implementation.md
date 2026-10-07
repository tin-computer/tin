# Project deletion

## In plain language

The person who created a business project can delete it from the dashboard or through their
coding agent. Tin stops the project's running work, removes its schedules, disconnects its
integrations, removes its files, and the project no longer lists or opens for any member.
Billing history stays. The personal "<name>'s project" cannot be deleted. There is no undo.

## What deletion means

Deletion is a tombstone. `projects.deleted_at` and `deleted_by_clerk_user_id` are set and the
row stays, because the billing ledger is append-only and references the project and its runs.
`has_project_access` and `list_projects_for_user` filter on `deleted_at IS NULL`, so every
HTTP route and MCP tool answers not found afterwards. The unique name index is partial
(`WHERE deleted_at IS NULL`), so a deleted project's name may be reused in its workspace.

Kept behind the tombstone: `workflow_runs` (status `stopped`), `project_workflows` and
project-owned `workflows` (status `archived`), and every `billing_*` row. Removed: memberships,
invitations, activity, chat, file changes, run decisions, rollouts, broker grants, tool grants,
integration connections and their receipts, secrets, test identities, content programs and
plans, review commands, MCP usage, the Temporal schedules and executions, sandboxes, and the
`projects/{id}` code.storage repository.

## Authorization

`projects.can_delete_project(project, actor)` is the one predicate: not the personal project
(by name, or by the deterministic personal id for this actor) and the actor is
`created_by_clerk_user_id`, or the row has no creator. `ProjectView.can_delete`, MCP
`list_projects.can_delete` and the service's own refusal all use it. Access is checked before the
name comparison so a wrong `confirm_name` never confirms that a project exists. After deletion
the members are gone, so a repeat delete authorizes through the creator or the deleter.

## Phases (`src/tin_lite/project_deletion.py`)

Under `effect_lock("project-delete:{project_id}:{request_id}", "project_delete")` and
`project_state_lock`:

1. One transaction: lock the row, authorize, `start_effect`, tombstone, stop every non-terminal
   run (release leases, drop broker grants), archive saved and project-owned workflows, remove
   invitations and memberships, read unsettled billing roots. After this commit nothing new can
   start: the gates fail, `dispatch_scheduled_workflow` refuses archived workflows, dispatch
   recovery only restarts `pending` runs.
2. External, each call bounded to 15 seconds: terminate each stopped run's execution and each
   schedule's `tin-scheduled-dispatch:` execution (terminate, not cancel, so no failure activity
   writes into a project being purged; NOT_FOUND is tolerated), delete the schedules, kill
   sandboxes, `IntegrationService.disconnect` per provider (deletes stored credentials; Google
   is not revoked upstream because one revocation ends the grant every project shares),
   `BillingService.settle` per root so reservations return (errors are logged; the
   reconciliation loop retries), `CodeStorage.delete_repo`. A failure marks the receipt failed
   and raises `ProjectDeletionPending`; the same `request_id` retries every later phase.
3. One transaction: `purge_project_data`, clear `temporal_schedule_id`, `complete_effect`.

A completed receipt replays its result. A fresh `request_id` on a deleted project runs every
phase as a no-op and returns the original `deleted_at` with zero counts. One `project_deleted`
analytics event carries the actor and the counts.

## Surfaces

| Surface | Call | Refusals |
|---|---|---|
| HTTP | `DELETE /api/projects/{project_id}?request_id=<uuid>&confirm_name=<name>` → 200 with `{project_id, name, deleted_at, stopped_runs, removed_schedules, disconnected, repo_deleted}` | 404 not a creator or unknown; 409 wrong name or `request_id` owned by another operation; 400 personal project; 503 cleanup pending |
| MCP | `delete_project(project_id, confirm_name, request_id)` → the same fields plus `tell_the_founder` | `not_found:`, `conflict:`, `request_conflict:`, `invalid:` |
| Dashboard | "Delete project →" in the project menu when `can_delete`; a dialog that requires the typed name; then the next project loads or the personal project is bootstrapped | the toast says `Could not delete <name>: <reason>` and the dialog stays open |

The agent instructions tell it to delete only when the founder asked in their own words, to say
what goes and what stays, to confirm once, and to pass the exact name.

## Verification

```
TIN_LITE_TEST_DATABASE_DSN=postgresql://tin_test:tin_test@127.0.0.1:5432/tin_test \
  uv run pytest tests/test_project_deletion.py tests/test_project_deletion_api.py \
  tests/test_mcp_auth.py tests/test_product_ui.py -q
npm run test:project-delete-browser
```

Migration `037_project_deletion.sql` adds the columns and rewrites the name index; apply it
before deploying the code.
