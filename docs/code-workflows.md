# Slice A: bounded code workflows

Implemented and deployed September 15, 2026, within the existing private-project rollout.
This implements only Slice A of `project-connections-and-private-workflow-funding-plan.md`.

This page records the original code-only foundation. Current file access is documented
separately below. The deployed executor also has later
[managed model steps](code-model-workflows.md), [API connections](project-api-connections.md),
and [eligible schedules](code-workflow-schedules.md). Slice A's limits and acceptance below
do not claim coverage of those later capabilities; see [current feature status](feature-status.md).

Code workflows can read [current project files](code-project-files.md) through `ctx.files`.
Tin pins the latest project revision at launch for stable retries; users choose no revision.

Private describes project ownership and access. `workflow.code` is a separately registered
Temporal executor for ordinary Python functions. Existing reusable `codex.procedure` packages,
interactive `project.task` runs, and their historical contracts remain supported.

This same contract also accepts [public contributions](adding-a-workflow.md). Public packages
are reviewed and explicitly registered by maintainers; they do not go through project-local
activation. The lifecycle below describes the private pilot, not a restriction on public code.

## Authoring and lifecycle

Use the existing `tin-workflow-package-v1` format. The complete Tin-owned fixture is
[`code_example/workflow.json`](../src/tin_lite/code_example/workflow.json), with
[`main.py`](../src/tin_lite/code_example/main.py) and fixture orders beside it.
`get_workflow_authoring_guide` returns a private copy in `code_example_files`, under
`workflow_packages/custom.order_report/`. The Tin-owned example is a source fixture, not a new
production Registry listing. Loader/export and execution tests prove both ownership forms use the same contract.

1. Commit these ordinary files with MCP `commit_project_changes` and the current project revision.
2. Validate the returned revision with `validate_workflow_package`.
3. Activate that exact revision with `activate_workflow_package` and a stable request ID.
4. Use `start_workflow` with the returned workflow UUID and `{ "minimum_cents": 1000 }`.
   Tin supplies project identity outside the authored inputs. No quote or additional approval is
   needed for this example. A manual saved configuration can use the existing save/start APIs.
5. Use `get_run`, `read_run_output`, and project Files to inspect the result. `stop_procedure` and
   HTTP `POST /api/workflows/runs/{id}/stop-procedure` also stop code runs before publication.

The example filters three fixture orders and reports two orders totaling 3,900 cents.
Users' coding agents can author/test the function locally; hosted execution uses its pinned
files independently and does not launch an agent.

## Contract and bounds

The definition selects `executor: workflow.code` and a `code` object with:

- `runtime: python3.12.8-stdlib-v1`;
- `entrypoint`: a declared package-relative `.py` file exporting `run(ctx, inputs)`;
- `files`: 1–32 explicitly declared UTF-8 files, at most 64,000 bytes each and 256,000 bytes total;
- `timeout_seconds`: 1–60 seconds;
- `output`: one `project.artifact` with a safe path, media type, and `max_bytes` ≤ 64,000. The file
  name may carry `{date}` and `{slug}` once each, so repeated runs keep separate, readable files.

`run` may be synchronous or asynchronous and returns exactly `{ "path": "…", "content": "…" }`.
The path must match the contract: `{date}` is the run's creation date (`ctx["created_at"][:10]`,
UTC) and `{slug}` is lowercase letters, digits and single hyphens, at most 80 characters, chosen by
the code. Tin records a named path before writing the checkpoint, so recovery after worker loss
reads the same file. Tin checks nonempty UTF-8 content, byte limits and NUL rejection;
JSON media additionally requires parseable JSON. This is a fixed text-result schema, not an
arbitrary JSON-Schema implementation or multi-artifact protocol. Inputs use the existing bounded
closed JSON-Schema subset. `ctx` contains the stable run ID and creation timestamp. Relative file
reads start in the package directory; local helper modules and fixture files must be declared.

The supported dependency surface is Python's standard library. No package installation, model
route, integration capability, arbitrary network service, recursive start,
or private schedule is exposed in Slice A. Unknown fields fail validation. Managed project
memory and workflow-package destinations are protected. No prompt or skill file is required.

## Execution and recovery

The existing isolated E2B image supplies pinned Python 3.12.8 and Tin's protected UID/cleanup
helper. The worker uploads its own protected launcher and the pinned package; it never imports
customer modules into the API or Temporal worker. No image rebuild was needed for the proof.

Customer code runs as `tin-work`, with an allowlisted environment, no privileges, a separate
network namespace, and E2B deny-all egress. There are no broker grants, OAuth caches, model keys,
run-tool grants, database/Temporal credentials, storage credentials, or project repository
checkout in this sandbox. The package workspace uses a 16 MiB scratch mount. The launcher limits
address space to 256 MiB per process, the worker UID to 16 processes, open files to 64, file size
to the bounded result envelope, and CPU/wall time to the declared execution window. Setup and
cleanup have separate short infrastructure timeouts. These are compute bounds, not pricing for
supplier services. Arbitrary code can still write ordinary scratch files in the disposable VM.

All worker descendants are killed before reading a regular-file result. Raw terminal output and
customer exception text are discarded. Every success, error, timeout and cancellation attempts
sandbox deletion. A later activity/failure/Stop retries cleanup where necessary.

The existing project execution position serializes this compute with procedures/tasks; it keeps
its historical Temporal name. New `kind=code` children use the code activity. Existing kinds and
recorded histories keep their activity sequences. Temporal carries run/control IDs and the small
review decision, never package source, fixture records, inputs, output bytes, or credentials.

Pure computation can repeat under bounded activity retry until its result is checkpointed.
Package revision, normalized inputs and timestamp remain pinned. A saved immutable checkpoint is
reused after worker loss. Only the trusted switchboard stages the result and publishes it through
existing lease/fencing validation, project-write serialization, `expectedHeadSha`, conflict
retention, and uncertain-commit reconciliation. `procedure_artifact_persist`,
`procedure_canonical_commit`, and `procedure_projection` remain the shared authoritative receipt
names; no duplicate execution database is placed in project files. The Postgres run projection,
Activity, generic output reader and optional declared human review use the existing services.

## Funding and rollout

`bounded-code-v1` is a trusted included-compute policy, checked against the closed executable
contract and pinned in the existing included-workflow receipt. It admits runs at zero Tin credits
without creating credit budgets, paid operations, or deductions. An authored cost/free flag does
not grant this policy. Existing paid procedure/model accounting is unchanged.

Private activation still requires the isolated template and either the explicit project
allowlist or the billed open setting (`TIN_LITE_PRIVATE_WORKFLOWS_OPEN`).
No credentials, production configuration, migrations, catalog sources, or rollout flags changed.
The opt-in proof creates a **new test code.storage repository** and real short-lived E2B sandboxes;
it never uses the configured product database. The report repository is retained for retrieval.

## Verification

### Automated tests with synthetic external execution

`tests/test_workflow_code.py` exercises package parity/export, safe paths and size limits, parsing
without executing customer code, input/result rejection, zero-balance MCP admission, duplicate
publication/projection, immutable source selection, review, stop-before-start, stop-during-compute,
retained output conflicts, revoked membership, checkpoint recovery after worker loss, and
reconciliation after a lost successful commit response. Postgres uses disposable schemas on a local test database.
Temporal uses a real local dev server and replays the new workflow history; compute/storage are
synthetic unless the explicit live flag is enabled. Existing procedure, billing, project execution,
publication, MCP and recorded-history suites also passed.

# Explicit external compute/storage test. Reads only the existing E2B/storage credentials.
TIN_LITE_TEST_DATABASE_DSN=postgresql://USER@127.0.0.1:5432/TEST_DB \
TIN_LITE_CODE_LIVE_PROOF=1 \
  uv run pytest tests/test_workflow_code.py -q
```

The live test emits a credential-free proof and saves its replay history in pytest's temporary
directory. Build verification confirms the wheel includes the launcher and all example files.
Live provider acceptance is separate from fixture-based verification.

## Slice B

The follow-up [Slice B implementation](code-model-workflows.md) adds optional managed model
steps to this same executor and preserves the code-only zero-credit policy. Its model accounting and recovery checks are separate from code-only execution.
Slices B, C and D are deployed within the private-project pilot. External connections and
eligible code schedules are documented separately above; direct SDK credentials and
procedure delegation remain deferred.
