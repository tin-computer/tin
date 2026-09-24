# Contributed workflow packages

This folder accepts deterministic Python, Python with managed model steps, and Codex
procedures. They use the same versioned package format as project-owned workflows.
CI validates every package without executing its Python, prompts or skills.

Start with code when you know the steps. Add model calls where judgment is useful. Use a
procedure when an agent needs to choose the steps. [Adding a workflow](../docs/adding-a-workflow.md)
covers authoring, tests and the maintainer-controlled public Registry registration.

## How the packages fit together

Registry packages build on what Tin already knows about a project instead of asking the
founder again. Before adding inputs, read these:

- `reports/GROWTH_ONBOARDING_PLAN.md` from Start here: the business, its buyers, budget and
  hard no's. Respect the hard no's.
- `wiki/INDEX.md`: its `### Code map` (`product.code_map`) and `### Feature map`
  (`product.deep_dive`) sections.
- `.agents/skills/writing-style/SKILL.md` from `style.capture`, for any copy drafted in the
  founder's voice.
- Outputs of earlier runs, such as the keyword plan, the organic audit and the signup walkthrough.

For revenue or product data, bind the first-party read-only connections rather than a custom
API: `payments.stripe` (subscriptions, customers, invoices, prices, charges) and
`analytics.posthog` (bounded HogQL and definitions). They return small projected records with
cursor paging, and `tests/connection_fakes.py` serves them offline; see
[Stripe and PostHog connections](../docs/stripe-and-posthog-connections.md).

Declare what you read as `recommended` prerequisites so the dashboard and MCP show readiness.
A procedure writes only its declared output. A weekly package keeps its history in a
`path_template` report ending in a JSON evidence block, which the next run reads back.

| Package | Builds on | Hands off to | Onboarding program |
|---|---|---|---|
| `organic.error_surface` | Code map, keyword plan, audit | `content.plan` (`context_files`), `content.generate` | Organic search content |
| `organic.mention_backlinks` | onboarding plan, organic and visibility audits, style guide | founder sends the asks | AI visibility |
| `competitor.watch` | its last report, keyword plan and ads competitors, Feature map | `content.public_article`, `content.plan`, `research.deep_dive` | Pricing and packaging |
| `qa.buyer_trust` | signup walkthrough, Feature map, onboarding plan | `site.health_improve` (code), founder (policy, host) | Conversion and trust |
| `growth.score_quiz` | Feature map, style guide (filled in by the agent) | founder embeds the widget | Conversion and trust |
| `product.analytics_brief` | PostHog connection (`analytics.posthog`) | its next scheduled brief | Product-led growth |
| `outreach.paying_segment` | Stripe connection (`payments.stripe`), onboarding plan | `outreach.email_shortlist`, `organic.keyword_plan`, `ads.assessment` inputs | Cold outbound, pricing |
| `outreach.speaking_shortlist` | Feature map, onboarding plan, style guide, its earlier reports | founder submits | Earned media, community |
| `outreach.syllabus_placement` | Feature map, onboarding plan, style guide, its earlier reports | founder sends | Partnerships and channel |
| `outreach.campus_events` | onboarding plan, Feature map, style guide | founder pitches the organizer | Community and events |
| `outreach.marketplace_listings` | Code map (required), Feature map, style guide | founder submits the listing | Platform and marketplaces |
| `content.release_announce` | changelog, style guide (filled in by the agent) | founder posts and sends | Owned audience, launches |
| `referral.voice_mining` | mailbox connection (`workspace.google`, gmail read), onboarding plan, Feature map | founder sends the ask and lines | Referrals, cold outbound, owned audience |

The onboarding plan lists these under their programs in
[programs.json](../src/tin_lite/growth_plan_assets/programs.json), with one `workflow_scope`
line each. A new Registry package needs the same.

## Code examples

- [CSV summary](example.csv_summary/workflow.json): deterministic parsing, validation and totals.
- [Feedback digest](example.feedback_digest/workflow.json): two managed model steps, with
  Python validation between classification and summarization.

Each directory contains a manifest and `main.py`. Copy one, change its folder and manifest
key together, and declare every file in `code.files`. Code packages accept `.py`, `.csv`,
`.json`, `.md`, `.txt`, `.yaml` and `.yml`; shell scripts and binary resources are not accepted.
The entrypoint exports `run(ctx, inputs)` and returns the declared path and text content.
The examples' offline tests live in [test_public_workflows.py](../tests/test_public_workflows.py).
They are copyable source examples, not active Registry entries.

See [code workflows](../docs/code-workflows.md) and
[managed model steps](../docs/code-model-workflows.md) for runtime and funding limits.

## Codex procedure example

[PostHog funnel](example.posthog_funnel/workflow.json) is a complete connection example:
a bounded procedure uses `procedure.services` to call the first-party `analytics.posthog`
operations through Tin's gateway. It is not registered or live-qualified. See
[Stripe and PostHog connections](../docs/stripe-and-posthog-connections.md) for the operations,
HogQL rules and offline fakes, and [API connections](../docs/project-api-connections.md) for
bindings, limits and evaluation requirements. A binding's
`max_response_bytes` bounds how much data one call can return; for Search Console that is a few
hundred rows per page, with `start_row` paging and `dimension_filters` to narrow the read.
Its [HogQL resource](example.posthog_funnel/skills/posthog-funnel/HOGQL.md) supplies a reusable
ordered funnel calculation and aggregate validator. It has explicit actor/attempt semantics;
it does not infer instrumentation or qualify a complete analytics product. Offline regression
checks are in [test_posthog_funnel.py](../tests/test_posthog_funnel.py); real provider evaluation
remains separately authorized.
The [synthetic SQL fixture](../tests/fixtures/posthog_funnel/ordered.sql) and its paired response
preserve a provider-dialect regression case for ties, duplicates, mixed attempts, missing steps
and exact medians. CI checks the generated query against that baseline without contacting PostHog.
Its synthetic events CTE uses `UNION ALL`, which Tin's HogQL guard refuses for ordinary calls;
the test builds a guard-compliant CTE with `arrayJoin` for the gateway path.

Use this shape when the workflow needs a bounded agent run. Private trials of procedure
packages remain manual, not scheduled.

```text
workflow_packages/<your.workflow_key>/
├── workflow.json    the contract: inputs, output, which skill to start from
├── PROMPT.md        what the run is asked to do, and what it must not do
└── skills/
    └── <entry-skill>/
        └── SKILL.md the method, in the ordinary skill format
```

The key uses a family and a name, like `growth.reddit_teardown`. The folder name and the `key`
in the manifest have to match. A skill's `name` has to match its own folder name.

Skills may carry extra text beside `SKILL.md`, as `.md`, `.json`, `.txt`, `.yaml` or `.yml`.
List every one of them in `skill_files`. Procedure packages are text-only; unlike code
packages, they do not carry `.py`. Neither package type accepts `.sh`.

### The manifest

This one validates. Copy it and change the parts that describe your workflow.

```json
{
  "package_format": "tin-workflow-package-v1",
  "definition": {
    "key": "growth.example_play",
    "title": "Name the workflow the way a founder would say it",
    "description": "One sentence: who runs this, and what they have at the end.",
    "executor": "codex.procedure",
    "version": "1.0.0",
    "schedule_modes": ["on_demand"],
    "input_schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["project_id"],
      "properties": {
        "project_id": { "type": "string", "format": "uuid" },
        "focus": {
          "type": "string",
          "title": "What to look at",
          "maxLength": 2000,
          "default": "",
          "x-tin-ui": { "control": "textarea", "order": 10 }
        }
      }
    },
    "procedure": {
      "prompt_path": "PROMPT.md",
      "skills_path": "skills",
      "skill_files": ["skills/example-play/SKILL.md"],
      "entry_skill": "example-play",
      "workspace": { "kind": "project.state" },
      "sandbox": { "profile": "isolated", "egress": "fenced", "timeout_seconds": 900 },
      "output": {
        "kind": "project.artifact",
        "path": "reports/EXAMPLE_PLAY.md",
        "media_type": "text/markdown",
        "max_bytes": 250000
      }
    }
  }
}
```

Every text input needs a `maxLength`, every array a `maxItems`, and `project_id` stays as it is.
A package declares one output: either a file in the project, as above, or a GitHub pull request.

For a complete minimal example, save this as `PROMPT.md`:

```markdown
Read the project context and the optional focus input. Follow the example-play skill and
write reports/EXAMPLE_PLAY.md. Do not publish, send messages or change other files.
```

Save this as `skills/example-play/SKILL.md`:

```markdown
---
name: example-play
description: Identify one useful next action from existing project evidence.
---

Read relevant project files. Recommend one concrete next action, cite the project paths
that support it, and distinguish evidence from assumptions. If context is insufficient,
explain what is missing instead of inventing facts. Write only the declared report.
```

## Check your work

```bash
uv python install 3.12
uv sync --frozen
uv run tin-lite validate-community
```

It names the package, the check that failed and where to fix it. CI runs the same command on
your pull request. `--root /path/to/checkout` selects another checkout (not its package folder).
Missing manifests, symlinks and invalid roots fail the check; a README-only folder passes.
Validation checks the contract and resources, not the quality or safety of executing instructions.

## Try the procedure example on a project

Use a project you are authorized to test. Ask its coding agent for
`get_workflow_authoring_guide(project_id)` first: private execution must be operator-enabled.
Normal run billing and integration requirements still apply; validation and activation do not
purchase a model run.

1. Copy the three example files into a **project-local copy** under
   `workflow_packages/custom.example_play/`. Change the manifest key to `custom.example_play`
   to match that folder. Keep `on_demand` and the explicit isolated, fenced sandbox above.
   Leave the contributed source package unchanged.
2. Use `list_project_files` and `commit_project_changes` through MCP to save the copy in project
   Files against the current project revision. Keep the returned commit SHA.
3. Call `validate_workflow_package(project_id, path, revision)` with
   `path: workflow_packages/custom.example_play/workflow.json` and that exact SHA. Resolve
   diagnostics before continuing. Other contributions may need further adaptations to meet
   [private activation policy](../docs/private-workflow-activation.md); a passing contribution
   check alone does not guarantee private activation.
4. Explicitly call `activate_workflow_package` with the same project, path and revision,
   a fresh UUID `request_id`, and `expected_revision: null` for a new workflow. For updates,
   use its currently activated revision instead. Activation does **not** run the workflow.
5. Inspect the returned workflow using `get_workflow`, then explicitly `start_workflow` if you
   want to execute it. Read `reports/EXAMPLE_PLAY.md` from the resulting run. Review the actual
   output before recommending the package; successful validation is not an execution test.

This private test is optional; it isn't the public registration process.

## What we look for

A workflow a founder would want to run, rather than a summary of marketing advice. Say where
the insight came from. If you ran it yourself, say what happened.

Search the open pull requests before you start. Several contributors can reach the same idea,
and a workflow nobody has written is worth more than a second version of one already in review.
If yours overlaps an open one, say how it differs.

## What happens next

A maintainer reviews the implementation, inputs, outputs, tests, cost bounds and permissions.
Adding the key and a stable UUID to `PUBLIC_WORKFLOWS` in
[public_workflows.py](../src/tin_lite/public_workflows.py) selects it for the next deployed
catalog sync. That publishes the exact manifest and resources together for both dashboard
and MCP. No new executor or copy of the implementation is needed.

Merging an unlisted package only adds its source. It does not activate, schedule or run it.
The included `example.*` packages are intentionally unlisted. Existing runs and saved
configurations keep their selected revision when a package is upgraded.
