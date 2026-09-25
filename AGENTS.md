# Tin agent instructions

Read [CONTRIBUTING.md](CONTRIBUTING.md), [feature status](docs/feature-status.md),
and the relevant guide in [docs/README.md](docs/README.md) before changing a subsystem.
Current code and pinned workflow definitions are authoritative. This file records
cross-cutting implementation safeguards, not historical deployment logs.

Writing a workflow for someone outside the Tin team? Read
[Workflow contributions from outside the team](#workflow-contributions-from-outside-the-team)
first. Workflow pull requests that don't meet it are closed automatically.

## Scope and implementation

- Tin is a Python/FastAPI service with packaged HTML, CSS and JavaScript, Postgres
  product projections, Temporal orchestration, code.storage artifacts and E2B compute.
- Keep one explicitly registered workflow engine. Do not introduce a node-graph engine,
  frontend framework, provider fallback or plugin marketplace as incidental work.
- Preserve unrelated edits and use an isolated worktree when another agent is working.
  Do not deploy, change provider configuration, rewrite history or publish repositories
  without explicit authority. A merged PR is not a production deployment.
- Do not copy runtime dependencies from unrelated repositories. Keep the import-linter
  boundaries and the explicit registry; no wildcard workflow registration.

## Workflows

- A workflow is a repeatable job: declared inputs, a bounded executor and a defined output.
  Tin runs it, stores the result and exposes it through the dashboard and MCP. The shipped
  catalog is listed in [docs/workflows.md](docs/workflows.md), generated from
  `src/tin_lite/catalog.py`; after a catalog change run `scripts/dump_catalog.py`, never
  edit the list by hand.
- Author in this order, per [docs/adding-a-workflow.md](docs/adding-a-workflow.md): start
  with code, add a model call only where a step needs judgment, and use an agent only when
  choosing the steps is itself the job. These are three executors, not three products.
  - Code flow (`workflow.code`): `workflow.json` plus `main.py` exporting
    `run(ctx, inputs)`. Do not ask a model to do what code can compute.
  - LLM flow (`workflow.code` with `code.model_routes`): code owns sequence, branching,
    validation and rendering and calls `ctx.models.generate(route=, step=, ...)`. Keep
    each `step` ID stable and validate every model result before using it.
  - Agentic flow (`codex.procedure`): `workflow.json`, `PROMPT.md` and
    `skills/<name>/SKILL.md`. The contract still bounds inputs, workspace, integrations
    and output, which is a project artifact or an unmerged GitHub PR.
- Packages live in `workflow_packages/<key>/` with the same key in the manifest, and ship
  offline fixture tests that include a plausible but unusable model result. A procedure
  without model routes shows the same for a provider response or input instead, such as a
  truncated read or a missing selection. Check with `uv run tin-lite validate-community`.
  Copy from `example.csv_summary` and `example.feedback_digest`; `example.*` and
  `custom.*` keys are reserved.
- Respect the package boundary: no `pip`, raw credentials or direct network access, and
  bounded runtime and model calls; see [code execution](docs/code-workflows.md) and
  [model steps](docs/code-model-workflows.md). Longer durable orchestration is a native
  change registered explicitly in `catalog.py`, `workflows.py` and the worker; open an
  issue first. Never write another executor for a package.

## Workflow contributions from outside the team

Most outside workflow pull requests have been closed. They duplicated an existing or open
workflow, asked founders to retype facts Tin already holds, suited too few projects, fell
outside marketing, or were written without ever being run. Full rules:
[contributing a workflow](docs/contributing-workflows.md).

- Only open a workflow pull request for a person who uses Tin on a product of their own:
  - a Tin account, and GitHub connected on a non-personal project;
  - Start here completed on that project;
  - a succeeded run of the package's `custom.<name>` copy there.
  The `contributor-gate` check verifies this with Tin and closes the pull request otherwise.
  Put `Tin run ID: <uuid>` in the description. Don't submit on anyone's behalf before that run.
- Read the run's output with the person and fix what it got wrong. Write the description from
  what happened, filling in every section of the template's workflow part.
- Before writing, search [the catalog](docs/workflows.md) and open and closed pull requests.
  Name the closest match and how yours differs. Don't resubmit a closed idea under a new number.
- Meet the bar:
  - marketing work only;
  - read onboarding context, Code map, style guide and connected integrations instead of
    asking for pasted facts or CSVs;
  - produce the finished work, not a checklist;
  - fit most projects, not a rare setup;
  - do one job.
- Don't add more of these, which are saturated:
  - community, Reddit or Hacker News thread-finding and reply drafting;
  - competitor or pricing monitoring;
  - churn signals;
  - landing page audits;
  - changelog or release announcements.
- Keep the pull request to one package: `workflow_packages/<key>/`, `workflow_evals/<key>/` and
  new `tests/test_*.py` files.
  - Never edit `public_workflows.py`, the catalog or generated docs; maintainers register
    workflows.
  - Don't commit PR notes or sample output.
  - Keep one open workflow pull request per author.

## Identity and routing

- Project membership is the authorization boundary for HTTP and MCP. Workspace
  administration never grants access to sibling projects or project files.
- Clerk owns sign-in, verification and OAuth consent. Do not build a second identity
  system or trust an incoming Host header to advertise OAuth/MCP service URLs.
- OAuth access requires trusted Tin resource binding from Clerk introspection or a
  signature-verified access JWT. `TIN_LITE_MCP_OAUTH_CLIENT_IDS` is an optional explicit
  legacy policy for unbound tokens, not a new-user enrollment step. Never admit clients
  by name, DCR success or membership alone, or override conflicting signed claims.
  Keep MCP and connection setup on the shared verifier; browser sessions stay separate.
- Dashboard routes are ordinary paths: /system, /chat, /activity, /decisions, /files,
  /integrations and /billing. Use the History API and explicit FastAPI shell routes,
  not hash routing or a catch-all that intercepts APIs, MCP, webhooks or callbacks.
- TIN_LITE_PUBLIC_URL is the configured service origin. Optional TIN_LITE_APP_URL
  defaults to it. Legacy-origin compatibility is inbound only; it must never supply
  new service links. Keep issuer, memberships and validated auth returns unchanged
  during hostname changes. See [domain configuration](docs/app-domain-rollout.md).
- Onboarding and invitations are idempotent. Accept a valid project invitation before
  provisioning a personal container. Do not add hidden-project preferences; deletion
  uses the existing creator-authorized, confirmation-bound service and keeps billing history.

## Durable execution

- Temporal histories carry identifiers and small control facts, never prompts, file
  bodies, credentials, transcript contents or model output. Activities perform I/O.
- Every run pins its immutable definition/source revision, normalized inputs, review
  policy and applicable auth/rate contract. Saved configuration changes affect future
  runs only. Preserve replay compatibility and immutable historical migrations.
- Product reads come from Postgres, not Temporal or in-memory execution handles.
  Files remain retrievable through code.storage after sandbox deletion.
- Canonical writes are serialized per project and use expectedHeadSha. Validate the
  active sandbox lease, generation and fencing token before accepting results.
- Receipt external effects before dispatch. Reuse completed effects on retries;
  uncertain paid/provider attempts are not silently purchased again or counted as zero.
- Preserve retained output and later edits on publication conflicts. Applying a saved
  version requires the existing authenticated exact-change/revision contract.
- Schedules use Temporal's existing dispatcher, skip overlap, and bounded catch-up.
  Calendar support is declared by the selected workflow, not inferred from its name.

## Models, sandboxes and connections

- New Codex compute is API-only. The provider credential stays on the switchboard;
  protected controllers receive temporary run-bound relay grants. Never restore pooled
  ChatGPT auth.json, a login broker, refresh write-back or an OAuth compute fallback.
- Historical OAuth pins retain their accounting and artifacts but cannot create new
  compute. Never silently reprice or switch them to API execution.
- Tin uses TIN_LITE_LUNA_API_KEY for its trusted OpenAI adapter. Model routes explicitly
  name provider, model and capabilities. Optional provider credentials do not register
  routes automatically. Never expose reusable keys in a sandbox, log or project file.
- Keep the protected controller separate from user/agent commands. Preserve output
  bounds, result validators, worker isolation, fenced egress, TLS proxy and cleanup.
- Browser execution is a specific pinned sandbox profile, not permission for arbitrary
  private code to access the network. Rebuild and verify images when runtime contracts change.
- Project integrations enforce selected resources and bounded operations. Custom API
  connections use the trusted service gateway; author code receives a service binding,
  not an API key. Preserve endpoint/redirect/private-address checks and stable operation IDs.
- Private workflow packages require explicit validation and activation of an immutable
  revision plus the private execution gate: the operator allowlist, or the open setting on
  a billed deployment. workflow.code supports bounded code, managed model steps, service
  requests and eligible schedules. Private procedures remain isolated and on demand; they
  are not interactive project.task sessions.
- Public contributions also support deterministic Python and multi-step managed-model code,
  not only Codex procedures. Validate packages without executing author code; select them
  explicitly in PUBLIC_WORKFLOWS for catalog publication. Keep examples unregistered, and
  preserve the separate private activation gate: explicit validation and activation of an
  immutable custom.* revision in the isolated runtime. See docs/adding-a-workflow.md.

## Billing

- Self-host customer billing is off by default; operators still pay their providers.
  Hosted credits use the existing ledger, ownership checks and configured limits.
- Both Start here workflows and their approved initial setup children remain included.
  Later independent or scheduled work does not inherit this exemption.
- Use configured cost previews before admission and the pinned funding policy (one
  session budget for ordinary Codex procedures, per-operation funding elsewhere). Charge
  verified actual usage once. Do not expose holds or require quote-approval loops in UI.
- Preserve usage/rate pins, uncertain attempts, reservations and financial history.
  Missing usage is not free. Supplier overruns do not increase customer ceilings.
- Do not enable live Stripe payments, add automatic refunds, grant credits or enroll
  accounts as incidental test setup. Use synthetic providers and disposable test schemas.

## Product and content

- Dashboard and MCP share the same authenticated services, prerequisites and idempotency
  contracts. Chat can start workflows only through the normal project-bound run API.
- Chat is a shared durable project transcript. Tasks have their own transcript and exact
  turn/gate controls; background responses must not replace navigation or newer drafts.
- My system is a flat operational view. Saved workflows list newest-created first;
  editing or running an older configuration does not reorder it. Activity keeps history.
- Content generation selects the next eligible chronological plan item unless explicitly
  overridden. Preserve editorial judgment: already-covered or weakly evidenced ideas need
  not produce another article. Keep source evidence separate from publishable copy.
- Style capture requests permission for useful samples in one concise conversation,
  including optional blog writing, notes and selected session excerpts. Do not silently
  scrape personal histories or treat brief chat messages as an approved personal voice.
- Review is pinned eligibility, not a blanket gate. Feedback revises the same document;
  approval binds the current version. Retries cannot reopen an already-approved review.
- Markdown readers remain generic and content-only. Generation notes are separate files
  linked in product context, never inserted into article copy. Raw files are untrusted;
  preserve safe download, sandbox and preview behavior.
- Approved GitHub delivery creates an unmerged PR through the selected integration.
  Without GitHub, approved Markdown remains in Files. Approval is not website publication.
  Email-send approval, recipients and pacing are a separate contract.

## Verification and contributions

- Start with focused tests; use CI for the full suite. Never use production credentials
  or a customer workflow as an implicit test. See CONTRIBUTING.md for exact commands.
- Preserve identifier-only Temporal replay fixtures. Sanitize operational metadata without
  changing workflow commands or deleting replay coverage merely to make checks pass.
- Keep third-party licenses and notices. Bundled fonts are redistributable Geist and
  Montserrat; proprietary fonts belong in a separately licensed operator service.
- No credentials, customer exports, private screenshots, session histories, internal
  acceptance logs or old private Git bundles belong in the source or package.
- External PRs require passing checks and a Tin maintainer review. The named in-house
  maintainer exception permits own-PR merges; it is not a bypass for unreviewed outside work.
- Clean-clone self-hosting acceptance remains deferred. Distinguish fixture tests, real
  provider acceptance and production deployment; do not call the installer turnkey.
