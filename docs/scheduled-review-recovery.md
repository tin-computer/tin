# Scheduled review lifetime and recovery

The 24-hour catch-up window limits how late a scheduled occurrence may start. It is
not an approval deadline. The dispatcher waits for its child to finish, including
human review, so Temporal's skip-overlap policy covers the whole review. Postgres
also rejects a new scheduled occurrence while the same saved configuration has a
pending, running, needs-input or paused run. This protects recovered children whose
original dispatcher has already closed. Retrying the same occurrence still returns
the existing run.

## Upgrade existing schedules

Older stored Temporal schedule actions have a 24-hour workflow execution timeout.
Updating the application does not update those stored actions. After deploying the
fix, use the normal operator environment to preview and apply the migration:

```bash
uv run tin-lite repair-schedule-timeouts
uv run tin-lite repair-schedule-timeouts --apply
```

This operator command touches only Tin dispatcher actions with the old 24-hour
timeout. It preserves the latest calendar, pause state, catch-up/overlap policy,
task queue, arguments and metadata, and is safe to rerun. An unexpected action or
timeout fails closed for inspection. The command does not change Postgres inputs,
approve drafts, reset workflows or alter deadlines already pinned to executions.

Inventory active legacy dispatchers separately. Their existing execution timeout
cannot be removed by updating the schedule; they can still expire after the upgrade.
If one expires while awaiting review, inspect its child before any recovery. Recovery is a
separate operator action: do not create another product run, regenerate the paid draft or mark
it approved in Postgres, and review the exact reset boundary of that workflow's history first.
