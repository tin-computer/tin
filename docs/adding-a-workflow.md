# Adding a workflow

A workflow is a job you want to run again. You give it inputs, decide how the work happens,
and define what should come out. Tin handles running it, saving the result, and making it
available through the dashboard and MCP.

Start with code. If a step needs judgment, call a model from that code. If the job needs an
agent to explore and decide what to do along the way, use a Codex procedure. All three can
be contributed to the public Registry; they aren't three different products.

## Choose how the work happens

### Ordinary Python

Use `workflow.code` when you know the steps: parse a file, filter records, calculate something,
validate it, write a report. There's no reason to ask a model to do arithmetic your code can do.

A package is a small directory:

```text
workflow_packages/reports.your_summary/
├── workflow.json
└── main.py
```

`workflow.json` declares the inputs, entrypoint, files, runtime limit and output.
`main.py` exports `run(ctx, inputs)`, which returns `{"path": "...", "content": "..."}`.
It can be synchronous or asynchronous. Every helper or data file must be declared too.

Start from [the CSV summary](../workflow_packages/example.csv_summary/workflow.json) and
its [Python implementation](../workflow_packages/example.csv_summary/main.py). It validates
the supplied rows and totals an integer column. No model, prompt or skill file is involved.

### Python with model steps

Use the same executor when some steps need a model. Your code still owns the sequence,
branches, loops, validation and rendering. A workflow can have several model steps; it
doesn't have to hand the whole job to an agent.

Declare each route under `code.model_routes`, including its provider, model and call limits.
Then call it where you need it:

```python
classified = await ctx.models.generate(
    route="classify",
    step="classify_feedback",
    instructions="Classify each supplied feedback item. Preserve its ID.",
    data=items,
    output_schema=CLASSIFICATION_SCHEMA,
)
# Validate classified["parsed"], then pass the checked result to another step.
```

The [feedback digest](../workflow_packages/example.feedback_digest/main.py) shows the complete
sequence: assign IDs in code, classify with a model, check that no IDs changed, summarize
with a second model call, then render the report. Its
[manifest](../workflow_packages/example.feedback_digest/workflow.json) declares both calls.

Keep `step` stable for each logical call. Tin records completed calls and reuses their
results if execution retries. Changing the request under an already-used step ID is an
error, not permission to buy another response. Code itself may run again, so don't treat
a local variable or scratch file as durable state.

Tin supplies the model service. Hosted calls use Tin-managed credentials and credits;
self-hosted calls use the operator's configured credentials. Don't put provider keys in
the package. Declared limits feed the existing estimate, and verified usage is charged.
Code-only bounded execution uses no Tin credits.

Read current project files with `ctx.files.read_text(path)`, `read_bytes(path)` or
`glob(pattern)`. The run uses the latest project revision at launch and keeps it for retries;
users never choose an input revision. Prefer opinionated paths and ordinary optional inputs
for missing information. See [project files in code workflows](code-project-files.md).

### Codex procedures

Use `codex.procedure` when choosing the steps is part of the job: investigating a question,
working through an unfamiliar repository, or drafting from project evidence. The package
contains `workflow.json`, `PROMPT.md` and `skills/<name>/SKILL.md`, rather than a Python
entrypoint. The contract still bounds the inputs, workspace, integrations and output.

The [package guide](../workflow_packages/README.md#codex-procedure-example) has a copyable
example. Procedure packages can produce a project artifact or an unmerged GitHub PR.
They don't acquire the interactive conversation and controls of a one-off `project.task`.

A procedure knows its own run, as code does through `ctx["run_id"]`. Tin adds a `RUN CONTEXT`
line naming the run ID to the end of the brief, and sets `TIN_RUN_ID` for the procedure's
commands. Ask for it wherever the output names its run, such as a report heading, a receipt
or a file name; don't let the model invent one.

Public procedures can also [propose and adopt a reviewed document pair](reviewed-project-documents.md)
and optionally inspect a read-only connected repository. These are explicit output/workspace
contracts; ordinary reviewable artifacts keep their existing behavior.

## Draw how it runs

Every workflow shows how it runs in a panel beside My system and the Add workflows list. The
drawing lives in the definition as `presentation.flow`; it describes the run and never changes
it. A package puts it in `workflow.json`; a built-in passes `presentation=WorkflowDiagram(...)`
in `catalog.py`.

```json
"presentation": {
  "flow": {
    "direction": "TD",
    "nodes": [
      {"id": "read", "kind": "step", "label": "Read the CSV", "fact": "unique columns, including amount_cents"},
      {"id": "check", "kind": "step", "label": "Check every row", "fact": "whole cents, at most 10 digits"},
      {"id": "report", "kind": "receipt", "label": "Summary in Files", "fact": "reports/CSV_SUMMARY.md, rows and total"}
    ],
    "edges": [
      {"from": "read", "to": "check", "kind": "call"},
      {"from": "check", "to": "report", "kind": "call"}
    ]
  }
}
```

Draw what the code or prompt does, in the order it does it, from top to bottom:

- 2–8 nodes and 1–12 edges in one connected graph. A label has at most 32 characters, a fact
  48 and an edge label 40.
- Each node has a kind:
  - `step`: work the run does.
  - `surface`: an outside service it reads or writes.
  - `store`: something Tin keeps, such as a file in project Files.
  - `wait`: a timer or poll.
  - `gate`: where it waits for someone's approval.
  - `receipt`: the record it leaves, usually last.
  - `ghost`: an ending where nothing happens.
- An edge is `call` when the run moves on by itself, and `signal` when a schedule, an approval
  or a timer moves it on. Edges leaving a gate are signals.
- Two nodes in one row run side by side, or are the two ways a run can go. An edge back to an
  earlier node is drawn as a loop up the side; label it with what sends the run back.
- Steps start with a verb. A fact is one plain line a reader can check against the code,
  such as a limit, a provider or where the result lands.

`validate-community` rejects a package without a valid drawing. When you change the steps,
change the drawing in the same pull request.

## Test it as a private workflow

Fixture tests show that the package fits the contract; a private run shows that it does the
job. Outside contributors must run the package on hosted Tin before opening a pull request,
as a `custom.*` copy in a real project of their own; see
[contributing a workflow](contributing-workflows.md) for the full requirements.

1. Sign up at [Tin](https://app.tin.computer) and connect your coding agent over MCP. New
   accounts get a one-time credit that covers test runs; code-only runs use no credits.
   Use a project for a product of yours, not the personal one: connect GitHub to it and
   complete Start here.
2. Check that the copy would activate:

   ```bash
   uv run tin-lite validate-community --private --package <your key>
   ```

   A private failure means the public package is valid but its copy needs a change for the
   test run. Make that change in the copy only and keep the public package as it is.
3. Ask your agent to commit the package to the project as
   `workflow_packages/custom.<name>/`, where `<name>` is the part of your key after the first
   dot. The copy's manifest `key` must be the same `custom.<name>`. Then call
   `validate_workflow_package`, `activate_workflow_package` and `start_workflow` with real
   inputs, and read the result with `read_run_output`.
4. Put `Tin run ID: <uuid>` for a succeeded run in the PR. Optionally invite the maintainer
   reviewing your PR to the project (Invite someone in the dashboard) so they can open the
   run themselves.

Private copies have narrower rules than public packages. For the test copy:

- Declare `schedule_modes: ["on_demand"]` for a Codex procedure. Private procedures don't run on
  schedules; your public package can keep `daily` or `weekly`.
- Use a fixed output `path` instead of `path_template`.
- Name a prerequisite by its workflow or file, without `producer`. Run the producing built-in
  workflow first if your package reads its output.

Some packages can't be tested privately yet: procedures that need the browser profile, and
integrations other than GitHub, Google Workspace read access,
[Stripe, PostHog](stripe-and-posthog-connections.md) and
[project API connections](project-api-connections.md). Open an issue before building one; a
maintainer can exempt the PR from the run requirement and run it themselves.

## Submit it

For a new or revised package, follow [creation and qualification](workflow-qualification.md):
keep a small versioned case file outside the package, test behavior offline, and distinguish
author claims from measured live quality and cost. Tin's creator uses the same checks as
hand-authored contributions. Existing packages can adopt the case file incrementally.

Put the package under `workflow_packages/<key>/`, with the same key in the manifest.
Use a descriptive key such as `reports.customer_digest`; reserve `example.*` for examples
and `custom.*` for project-local copies. Include offline tests under `tests/`: a useful input,
the expected result, invalid inputs, and any model or integration responses as fixtures.
Show what happens when a model returns a plausible but unusable result. A Codex procedure
with no model routes has no model result to fixture; show the same for a plausible but unusable
provider response or input instead, such as a truncated read, an empty result or a missing
selection, in its [qualification cases](workflow-qualification.md) or tests.

Run the static package check and your tests:

```bash
uv sync --frozen
uv run tin-lite validate-community
uv run pytest tests/test_your_workflow.py
```

Validation reads the manifest and declared resources. It parses Python but doesn't execute
it. Your tests execute code separately, without production credentials or paid API calls.
A passing validator proves the package fits the contract, not that its output is useful.

Fill in the workflow part of the PR template: the private run ID, who would run this and
what they get, the closest existing workflow or PR and how yours differs, what it reads from
Tin instead of asking the founder, and how you tested it. Name the integrations, model costs
and any external effects.
External contributions need passing CI and maintainer review; see [CONTRIBUTING](../CONTRIBUTING.md).

## From a package to the public Registry

A maintainer explicitly adds the reviewed package's key and a permanent UUID to
`PUBLIC_WORKFLOWS` in [public_workflows.py](../src/tin_lite/public_workflows.py):

```python
PublicWorkflow(UUID("bced29da-7c99-45a2-8f59-f5c0964d3b51"), "reports.customer_digest")
```

Generate a new UUID for your entry; don't reuse this example. This registration can be
reviewed in the same PR or follow a source-only contribution. Don't copy the implementation
into the native catalog or write another executor for it.

The next deployed catalog sync validates the selection, publishes the manifest and all its
resources together, and updates the public Registry projection. The dashboard and MCP read
that same catalog. Existing runs and saved configurations keep their pinned version.
Keep published IDs, keys, executor and source paths stable.

Merging source alone doesn't activate a package. The shipped `example.*` packages are
deliberately unregistered, so they won't appear as customer workflows. Removing a registration
also isn't an archival command for an already-published workflow.

## Know the current boundary

The code package runtime is Python 3.12.8 with the standard library, up to 900 seconds and
one declared text artifact of up to 1,000,000 bytes. It supports up to 32 managed model calls
across four routes; keep a run's model input under 700,000 bytes (about 200k tokens);
only the registered OpenAI Luna and Astra routes are currently admitted. There is no `pip`
installation, raw credential injection or direct network access. Use declared
[project service bindings](project-api-connections.md) for supported external requests; for
Stripe or PostHog data, bind the first-party connections described in
[Stripe and PostHog connections](stripe-and-posthog-connections.md), which also has offline
test fakes.
See [code execution](code-workflows.md) and [model steps](code-model-workflows.md) for exact bounds.

If the workflow needs longer durable orchestration, multiple distinct activities, or a
capability outside this contract, contribute a native implementation. The existing catalog
already contains code-defined and model-backed workflows. Native changes explicitly register
the definition in `catalog.py`, the Temporal implementation in `workflows.py`, and activities
in the worker. Keep I/O in activities, payloads out of Temporal history, and test retries,
billing and artifact publication. Discuss that larger change in an issue first.

Public and private describe who can use a workflow, not how it runs. You can try a package
as a `custom.*` copy in your own project using the existing
[validate/activate flow](private-workflow-activation.md); see
[Test it as a private workflow](#test-it-as-a-private-workflow). Public Registry registration
doesn't need private execution. Schedules and review remain properties of the definition;
private Codex procedures are still on demand. Project-owned writing guides and other skills
remain in `.agents/skills/<name>/SKILL.md` in project Files.
