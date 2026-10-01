# Project-private workflow activation pilot

## In plain language

A coding agent can put a small recipe in project Files, check it, and explicitly activate it.
It then appears alongside built-in workflows and runs on Tin's existing compute. Editing the
recipe does not activate or run it. Saving a configuration chooses the latest activated recipe
automatically; existing configurations and runs keep the recipe they selected.

Execution is limited to allowlisted projects unless the operator opens it to every project on
a billed deployment.
Private procedures are manual; eligible code workflows also support schedules. There is no
new engine, version database, filesystem copy, or workflow-specific reader/UI. Current
capabilities and billing boundaries are summarized in [feature status](feature-status.md).

## Authoring contract

`get_workflow_authoring_guide(project_id)` returns the executable three-file example and current
capabilities. Use the same `tin-workflow-package-v1` decoder as built-ins. The procedure example
below stays in the project's code.storage repository; the guide also supplies code examples:

```
workflow_packages/custom.research_digest/
  workflow.json
  PROMPT.md
  skills/research-digest/SKILL.md
```

1. Use `list_project_files` and `commit_project_changes` to commit the package against the current
   project revision. Ordinary file concurrency/conflict rules apply.
2. `validate_workflow_package(project_id, path, revision)` reads only declared regular files at
   that exact commit. `path` is the manifest, `workflow_packages/custom.<key>/workflow.json`;
   the package directory is accepted too. It returns diagnostics, digest, source paths, output
   and required integrations. `runtime_available` means the pilot runtime is enabled, **not** that integrations
   are connected or that a future model run is guaranteed to succeed.
3. `activate_workflow_package(project_id, path, revision, request_id, expected_revision)` validates
   again and projects the recipe into the existing catalog. Explicit null creates; the current
   activated revision updates. Workflow UUID, owning project, executor and source location remain
   stable. A conflicting activation returns `activation_conflict`; reread before retrying.
4. Refresh `list_workflows`, inspect using `get_workflow` (UUID or unambiguous key), then use normal
   `start_workflow`. Supply `project_id` outside inputs. Optionally save a configuration
   with `create_project_workflow` for My system, using only the definition's supported schedule
   modes; a saved schedule also runs once right away. No user-facing version selection is needed.
5. `archive_private_workflow(project_id, workflow_id, request_id, expected_revision)` removes the
   recipe from discovery and prevents new starts. It preserves source files, saved configurations,
   running work and historical results. Explicit activation restores the same UUID.

The authenticated HTTP equivalents are under
`/api/projects/{project_id}/workflow-packages`: `GET /guide`, `POST /validate`, `POST /activate`,
and `POST /{workflow_id}/archive`. Both transports use the same service. Catalog detail/list
include scope, exact source revision, supported client inputs and lifecycle actions. Bound
`project_id` is absent from client input schemas. Activation/archive use a caller-bound request
receipt and one transaction for catalog state, product Activity and completion. A lost response
replays its original receipt, not today's moving catalog state.

## Code workflow extensions (deployed pilot)

The same lifecycle now accepts bounded `workflow.code` packages in addition to the procedure
contract below. `get_workflow_authoring_guide` supplies `code_example_files`; the existing
procedure example is preserved. Code-only execution runs with zero Tin credits under a validated
included-compute policy. Later slices add [managed model steps](code-model-workflows.md),
[project API connections](project-api-connections.md), and [eligible code schedules](code-workflow-schedules.md)
without giving author code raw credentials or direct networking. The project rollout gate remains.
See [code workflows](code-workflows.md) and the linked extension contracts.

## Supported procedure execution

- Explicit `codex.procedure`, `custom.<lowercase_name>` key and `on_demand` scheduling only.
- Required `isolated` / `fenced` profile; existing controller/worker separation, trusted usage
  observations and retry checkpoint rules apply. Timeout is bounded at 3,600 seconds.
- One bounded UTF-8 project artifact, or a bounded unmerged GitHub PR with repository verification.
  GitHub capabilities must match the declared workspace/result and use the connected-project
  gateway. Existing PR overlap checks and result validation remain in force.
- Repository workspaces are snapshots of up to 20,000 eligible files / 100 MB, each file at
  most 2 MB (larger media and built files are left out; see
  [repository-aware delivery](repository-aware-content-delivery.md)). The bound belongs to
  the gateway; a `limits` key in older definitions is ignored.
- Optional connected Workspace read capabilities: Gmail messages and calendar events. No email
  sending, test identities, browser/Studio, uploaded native executors or recursive starts.
- Optional existing project skills and normal review eligibility. Managed `wiki/INDEX.md`
  section writers remain Tin-owned. Context selection is not a narrower project read permission.
- No mandatory report-only restriction; ordinary output paths use the existing Files policy.

New starts verify project/source ownership and the current membership before source resolution.
The worker validates the pinned recipe and rechecks the initiating member/runtime before a new
paid attempt. Archive does not cancel already-started work; existing Stop remains separate.
Current delivery fencing, conflict preservation, integration authorization and retained-output
recovery continue to apply.

## Deployment and verification

Configure `TIN_LITE_E2B_ISOLATED_TEMPLATE` with the accepted isolated image and explicitly allow
pilot project UUIDs in comma-separated `TIN_LITE_PRIVATE_WORKFLOW_PROJECTS`. Default is empty.
`TIN_LITE_PRIVATE_WORKFLOWS_OPEN=true` admits every project instead. Startup refuses it unless
`TIN_LITE_BILLING_ENABLED=true`, so private runs are funded through the credit ledger; the
isolated template remains required either way.
Do not enable unisolated/browser/Studio execution for private recipes. No migration, new native
model route, API-key exposure, default image rebuild or credential replacement is required.

Automated tests exercise real disposable Postgres, HTTP and MCP lifecycle parity, concurrent
activation/replay, failed transaction rollback, invalid drafts, cross-project access and revoked
membership, archive/reactivate with preserved run/configuration, worker admission, GitHub PR
contracts, and saved A after B with latest-version new saves. Existing runtime isolation tests
remain separate; see [isolated runtime](isolated-codex-runtime.md).

Usage observations alone are not supplier invoices or a retail spending guarantee. Current
funding is described in [billing coverage](workflow-billing-coverage.md); code-only execution
remains included, and eligible code schedules have their own standing-authority checks.
Private procedure schedules, upgrade UI and scaffold/copy ancestry remain deferred.
