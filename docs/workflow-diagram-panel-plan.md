# Workflow diagram panel: implementation plan

Status: pull request 1 built, 2026-10-06. Pull request 2 (batches 2 and 3) follows.
Design: the Paper file thinklikeanagent, page "System · workflow diagram panel". The chosen layout is PANEL-3B; the grammar is on the ANATOMY board.

## Goal

Every workflow a person can add or has saved shows how it runs in a right-side panel, read top to bottom.

- The diagram ships inside the workflow definition, as the existing `presentation.flow`. Each node has a kind, a short label and a one-line description (the `fact` field).
- New workflows carry a diagram from the moment they are authored: by hand, as a community package, or by Tin's own creator (`custom.workflow_create`).
- Existing workflows are backfilled.

## What exists today (origin/main c83697b6)

- **Contract.** `workflow_diagrams.py` already defines the vocabulary:
  - node kinds: step, surface, store, wait, gate, receipt, ghost
  - edge kinds: call, signal
  - limits: 2–8 nodes, 1–12 edges, label ≤32, fact ≤48, edge label ≤40
  - direction: `LR` or `TD`
- **Where it can be set.** Only native workflows can carry a diagram, via `BuiltinWorkflow.presentation` (`catalog.py:422`, emitted at `:472`). Three do, all left to right: `site.health_improve`, `project.weekly_brief` and `outreach.email_campaign`.
- **Packages reject it.**
  - `workflow.code` and private `custom.*` manifests refuse a `presentation` field, because their field allowlists don't include it (`workflow_code.py:139`, `private_workflows.py:71`).
  - `codex.procedure` packages accept it without validating it.
- **No live UI.** No screen on main shows a diagram. `workflowHowItRuns` and `hydrateWorkflowDiagrams` in `app.js` are only reachable through `renderRegistryWorkflows`, which nothing calls.
- **Versioning.** The catalog sync compares the whole definition JSON. A presentation-only change publishes a new registry commit under the same `version_label` and updates the row (`catalog.py:2911`, `code_storage.py:1335`).
  - Saved workflows keep their pinned commit (`workflow_definitions.py:40`).
  - Temporal histories hold no definition JSON, so replay is unaffected.
- **The workflow that writes workflows** is `custom.workflow_create` (`workflow_creator_package/`). It writes `reports/WORKFLOW_CANDIDATE.json`, and that candidate is checked through `community.validate_files`.
- **Scope.** The catalog shows 52 workflows: 28 native and 24 packages. Another 8 are hidden from discovery but still run from saved schedules.

## Decisions

1. **One field, described better.** Keep `presentation.flow` and its validator. The node `fact` is the node's description, so docs and the creator call it that. New and backfilled diagrams use `direction: "TD"`; `LR` stays valid for old pinned definitions. The validator's limits stay as they are, because `content.diagram` artifacts share it (`tin-diagram.v1`/`v2`).
2. **Display-only.** Presentation never affects execution: no prompts, no sandbox inputs, no contract checks. Changing only the diagram needs no version bump. The `content.refresh` and `content.answer_page` contract-digest test (`test_one_content_generate.py`) leaves `presentation` out of the digest, as it already does for `description`.
3. **Which diagram a saved workflow shows.** Prefer the pinned definition's diagram.
   - If the pinned commit has none but differs from the current definition only by `presentation`, show the current diagram. This is the backfill case, and it is exact.
   - Otherwise show the current diagram, and the panel's key line says so in a few words: `content.generate · runs v1.12.0 · drawn from v1.15.0`.
4. **Required where we control authoring, optional where users already have packages.**
   - Required:
     - every catalog-visible built-in and every package in `PUBLIC_WORKFLOWS`
     - new community contributions (`validate-community`)
     - creator candidates
   - Private `custom.*` packages: allowed and validated, not required. Old activated revisions keep loading and running. The panel hides its button when a workflow has no diagram.
5. **Layout B.** While the panel is open the content area narrows beside it instead of running underneath. The last-run and status columns hide, and the panel sits a step below the page. No eyebrows: no "HOW IT RUNS", and no "needs you" (a gate is a bermellón dot before its title).

## Work

### A. Contract and validation

- `workflow_diagrams.py`: add `validate_presentation(value)`, which checks for exactly `{"flow": ...}` and calls `validate_workflow_diagram`. The worktree already has a draft of it.
- Widen the allowlists to accept and validate `presentation`:
  - `workflow_code.py:139` (code packages)
  - `private_workflows.py:71` (private packages)
  - These lists only ever widen, so pinned revisions keep validating.
- `community._validate_metadata`: validate `presentation` for every executor, and require it for public packages and new contributions. The check also runs for creator candidates and `load_public_workflows`.
- `catalog.sync_builtin_workflows`: validate presentation at sync, and fail when a catalog-visible built-in has none.
  - The test reads from a `PRESENTATION_PENDING` set, which PR 2 empties (see sequencing below).
- Tests to update:
  - `tests/test_workflow_diagrams.py`: today it expects exactly three diagrams; that list goes.
  - `tests/test_private_workflows.py:612`
  - the contract digest in `test_one_content_generate.py`
- Tests to add: allow, validate and require for both package kinds; private optional; sync failure for a missing diagram.

### B. A diagram for a saved workflow

- `GET /api/projects/{project_id}/workflows/{project_workflow_id}/diagram` returns `{flow, version, pinned_version, exact}`, applying decision 3 (`workflow_diagrams.diagram_for_saved_workflow`).
- It reads the pinned definition through `resolve_execution_contract` and is authorized by project membership, like the other project routes.
- The catalog's Add workflows view keeps reading `definition.presentation.flow` from `/api/workflows`.
- MCP and the public catalog stay unchanged for now.

### C. Panel UI

The paused worktree branch `feat/workflow-diagram-panel` gets rebased onto main and updated to the final boards.

- **`static/workflow-spine.js`** lays out the top-to-bottom spine: single rows of 248, pairs of 161 + 12 + 161, a loop rail, a skip rail, the wait dot and the schedule chip. Changes to bring it in line with the final boards:
  - open chevrons at the line's 1.2 weight
  - every node opaque
  - a store as a card with a file mark in its corner
  - descriptions in the sans at 12px
  - the gate as a dot plus title
- **Panel:** 420px, header with the title and close button and the key and version underneath, a dot-grid canvas, a footer with the step summary and Workflow settings. Its background sits one step below the page, with the page's 16px shadow on its left edge, in coal and paper tokens.
- **Layout B:**
  - While the panel is open the app shell gets `has-diagram-panel`, and the content area loses 420px of width.
  - System rows drop the last-run and status columns under a container query.
  - The tab-row summary shortens.
  - Below 640px the panel becomes a full-width sheet.
- **Diagram buttons:**
  - each My system row, in the row's grammar, pressed while open
  - the expanded Add workflows card (`systemTemplateSetupCard`)
  - the saved workflow editor (`systemProjectWorkflowEditor`)
- **Behaviour:** Escape closes the panel and focus returns to the button. The panel is a labelled `complementary` region and the canvas scrolls vertically.
- **Cleanup:** remove the dead `workflowHowItRuns`, `hydrateWorkflowDiagrams` and `renderRegistryWorkflows` path and its CSS (`app.css:2982`). Update `tests/test_product_ui.py:448` and `docs/engineering.md:315`.

### D. Authoring paths

- **The creator** (`workflow_creator_package/`):
  - `SKILL.md` gets a step to draw the run, before the candidate is written.
  - `CONTRACT.md` gets the presentation rules: the vocabulary, the limits and how to choose each kind. One gate per approval; a store for something Tin keeps; a surface for an outside service; a receipt last; descriptions that state what is true, not adjectives.
  - Both example files and the creator's own `workflow.json` get a diagram.
  - Bump its version and add a qualification `expect` that the candidate's diagram validates.
- **The MCP guide** (`private_workflows.authoring_guide`): a diagram in the example manifest, its steps and `workflow_code.example_files`.
- **Docs and templates:**
  - `docs/adding-a-workflow.md` gets a "Draw how it runs" section, linked from both the package and native paths.
  - Add a diagram to the `workflow_packages/README.md` manifest template and to the `example.csv_summary` and `example.feedback_digest` manifests.
  - `docs/contributing-workflows.md`
  - `.github/pull_request_template.md` gets a "How it runs" section: confirm each node and description against the code or prompt.
  - `AGENTS.md` (Workflows section) gets: "Every visible workflow ships a top-to-bottom presentation; change it in the same pull request as its steps."
- **Drift test:** for systems with a declared step map, each child step key must appear as a diagram node ID. This covers `organic_system.STEPS` and the bundled children of `growth.onboarding` and `x_draft`.

### E. Backfill

Each diagram is written from the step logic. The inventory points to the code, the procedure prompt or `main.py` for each one, and every fact has to be checkable there. A browser test renders every diagram at 420px and fails on overlapping cards, a label on a card, anything spilling past the edge, or an edge left undrawn. Labels beside a line wrap inside their own lane and push their row down, so a 40-character label still fits beside the right card of a pair. A sheet of screenshots goes in the pull request for review.

| Batch | Workflows | Count |
|---|---|---|
| 1 | organic.traffic_system, organic.audit, organic.keyword_plan, content.plan, content.generate, content.deliver, content.refresh, organic.technical_fix, style.capture, outreach.community_threads, revenue.payment_recovery; site.health_improve, project.weekly_brief and outreach.email_campaign flipped to top-to-bottom. Also drawn: example.csv_summary, example.feedback_digest, example.posthog_funnel, the three code examples and the creator itself | 14 |
| 2 | the remaining visible built-ins: product-qa (4), creative (2), x (2), paid ads (3), project.memory, scan.report, content.design_md, research.deep_dive, content.diagram, outreach.email_shortlist, outreach.awesome_submit; hidden but schedulable: visibility.audit, content.answer_page, content.public_article | 21 |
| 3 | the 28 packages still in `PRESENTATION_PENDING`, example.project_files among them | 28 |

Batch 1 already includes outreach.community_threads, so batch 3 covers the rest of the 24 public packages. Agent-only workflows and `project.task` have no saved row, so they need no button. New ones still get a diagram from the creator rule.

## Pull requests

1. **Foundation, UI, authoring paths and batch 1** (this branch). `PRESENTATION_PENDING` lists the built-ins still waiting, and the package requirement covers new contributions only.
2. **Batches 2 and 3.** This empties `PRESENTATION_PENDING` and turns on the requirement for every public package.

A presentation-only change publishes a new registry commit at the next deploy's catalog sync. No migration and no SQL is needed.

## Out of scope

- Generating diagrams from code.
- Diagrams in MCP responses and the public catalog API.
- Clicking a node to see run status.
- Live progress on the diagram.

## Settled

- Two pull requests.
- The version note stays, folded into the key line so it costs no extra row.
