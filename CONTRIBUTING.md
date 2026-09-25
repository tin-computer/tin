# Contributing to Tin

Start with the [README](README.md), [feature status](docs/feature-status.md), and
[architecture](docs/architecture.md). For workflow contributions, read
[Adding a workflow](docs/adding-a-workflow.md). Public contributions can be deterministic
Python, Python with multiple managed model steps, or Codex procedures. Start with the
[copyable packages](workflow_packages/README.md); maintainers explicitly select reviewed
packages for the Registry. Merging source alone does not activate a workflow.
Use the shared [creation and qualification checks](docs/workflow-qualification.md) for proposed
packages, whether authored by a person or Tin. Include cases and disclose what was measured.
Coding agents should also read [AGENTS.md](AGENTS.md).
The [documentation index](docs/README.md) groups contributor and self-hosting references.

Workflow contributions have their own requirements: you need to use Tin on a product of your
own and run the package there before opening a pull request. An automatic check closes
workflow pull requests that don't meet them. Read
[contributing a workflow](docs/contributing-workflows.md) first.

Bug fixes, documentation corrections and focused integration contributions can go straight
to a pull request; no prior issue or permission is required. Discuss broad architecture or
breaking changes before building them. Report security
issues privately as described in [SECURITY.md](SECURITY.md), not in public issues.

## Review and merging

External contributions need passing CI and a review from a Tin maintainer before merging.
Keep the PR focused and use the short PR template to explain the change and verification.
AI-assisted contributions follow the same rules; the contributor remains responsible for
understanding and testing the change.

Tin's in-house maintainers can merge their own PRs after checks pass, without waiting for a
second reviewer. They also retain direct-push access for small internal changes. This is an
explicit maintainer-team exception, not a blanket exemption for every repository admin.
Do not use it to skip review of external contributions. See the
[repository policy](docs/repository-policy.md) for the exact settings and release checklist.

PR tests run on GitHub-hosted runners with a read-only repository token and no production
credentials. A maintainer may need to approve a first-time contributor's CI run; that only
allows the tests to run, not the PR to merge or deploy. Live provider tests and deployment
remain separate maintainer operations.

## Contributing an integration

A company integration should connect a project to its service and document how to use its
API. You do not need to wrap the entire API, invent a workflow, or ship an arbitrary minimum
number of named operations. Workflow authors can choose endpoints and compose their own
logic from the provider's documentation.

Include the authentication method, exact API origin, requested permissions/scopes, official
API documentation and fixture-based tests. Explain any provider-specific restrictions or
paid effects. Never include a real credential; contributors should not need Tin's production
infrastructure to run the ordinary checks.

Today, custom API-key connections already support bounded authenticated requests through
`ctx.services.request` in code workflows and `request_service` in Codex procedures;
registered GitHub/Google adapters also have existing named operations.
Read [project API connections](docs/project-api-connections.md) for the current extension
points. A connection must preserve secret isolation, project access and retry safeguards;
it must not bypass GitHub's selected repository or email-send approval rules.

## Local checks

Use Python 3.12, [uv](https://docs.astral.sh/uv/) and Node.js 22, matching
[CI](.github/workflows/ci.yml):

```bash
uv python install 3.12
uv sync --frozen
npm ci --ignore-scripts --no-audit --no-fund
uv run ruff format --check .
uv run ruff check .
uv run lint-imports
uv run pytest -n auto
```

`-n auto` spreads the suite across CPU cores with pytest-xdist, as CI does; plain
`uv run pytest` runs it serially. Database-backed tests need a disposable PostgreSQL 17 database configured through
`TIN_LITE_TEST_DATABASE_DSN`. Use a local test database and test-only credentials, never
a hosted customer or production database. Tests create isolated schemas; without the test
DSN, database-dependent tests skip. Report skips rather than calling that a complete test run.

For browser changes, install Chromium with `npx playwright install chromium`, then run the
relevant `test:*-browser` script from [package.json](package.json). For example:

```bash
npm run test:auth-browser
npm run test:workflow-review-browser
```

Changes to generated diagram or comparison assets also need `npm run check:diagrams` or
`npm run check:comparison`. Commit the corresponding generated assets when their sources
change. CI runs the broader browser and backend suites; start locally with tests focused on
your change. `npm run test:browser` runs the whole browser suite CI runs in one concurrent
pass. CI skips it for pull requests that change only `workflow_packages/`.

Ordinary tests use fixtures and mocked provider responses. Live provider, E2B and integration
acceptance tests are separate opt-in checks: inspect their prerequisites, use authorized test
resources, and account for real costs and external effects. Do not copy production credentials
into fixtures or run deployment scripts as a test setup shortcut.

Running the complete service requires operator-owned infrastructure and credentials described
in the [engineering reference](docs/engineering.md). Clean-clone self-hosting acceptance is
still outstanding; the test commands above are not proof of a turnkey deployment.

## Keep the existing boundaries

- Project membership is the authorization boundary for both HTTP and MCP. Workspace access
  must not grant access to another project's files or operations.
- Temporal orchestrates durable work; activities perform I/O. Keep prompts, files and secrets
  out of workflow history. Preserve compatibility with recorded histories.
- Product reads use Postgres projections. Durable artifacts use code.storage, fenced writes
  and revision checks. External effects need stable operation IDs and retry/recovery coverage.
- Provider credentials stay in trusted services. Use registered model routes and project
  integration capabilities, not raw keys in workflow inputs, generated files or sandbox code.
- Billing estimates and usage receipts must match the executor's actual paid operations;
  missing usage is not zero. Preserve included onboarding and billing-off behavior.
- Reuse the packaged HTML/CSS/JavaScript UI and existing components. Do not introduce a frontend
  framework, separate workflow engine or node-graph editor as incidental scope.

## Before opening a pull request

Explain the user-visible change, its scope, tests run and known limitations. Include a regression
test for a bug where practical. For UI changes, provide screenshots using synthetic content;
do not upload customer data. For new workflows, include the registered contract, instructions,
prerequisites, billing behavior and review/output rules—not just a prompt.

Keep commits focused and preserve unrelated work. Do not commit `.env` files, credentials,
customer project exports, session histories or licensed proprietary fonts. Geist is bundled
under its included license; FK Grotesk Neue is not redistributable with the source. See
[font serving](docs/font-serving.md) and [third-party notices](THIRD_PARTY_NOTICES.md).

Update current documentation when behavior changes. Keep internal plans and production
acceptance logs out of public guides. A fixture test, a live run and a production rollout
are different claims; document verification limits without including customer run records.

Contributions are provided under this repository's [Apache 2.0 license](LICENSE), unless an
explicitly documented third-party license applies. Keep copyright and license notices intact.
