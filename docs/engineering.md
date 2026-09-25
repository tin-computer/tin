# Tin Lite engineering reference

For the product overview and agent setup, see the [README](../README.md).

For the current supported/pilot/deferred split, use [feature status](feature-status.md).
For the rest of the contributor and operator guides, use the [documentation index](README.md).

Tin Lite is a greenfield workflow substrate. The current repository implements Phase 1a and the
first sequential Phase 1b slices: catalog-backed `content.design_md`, `project.memory`,
`scan.report`, `visibility.audit`, `content.answer_page`, `project.weekly_brief`,
`research.deep_dive`, `content.public_article`, `content.diagram`, `qa.signup_walkthrough`,
`product.code_map`, `product.deep_dive`, `qa.product_audit`, `creative.character`,
`creative.product_demo`, `growth.onboarding`, and `growth.onboarding_plan` workflows run through Temporal Cloud, persist durable state to code.storage, and project product
reads into PlanetScale Postgres. The
catalog also exposes one bounded `project.task` executor for one-off Codex work that has no narrower
workflow. Luna reads the same Clerk-authenticated catalog, memory, and run endpoints.

See [`AGENTS.md`](../AGENTS.md) for the milestone fences and acceptance criteria.

Customer billing is optional and off by default (`TIN_LITE_BILLING_ENABLED=false`). Self-hosted
operators supply their own infrastructure/provider access without Stripe or a Tin balance;
workflow usage observations remain available. Hosted Tin enables billing, test payments and
hosted credit defaults; self-host settings default off. See [self-hosted billing](self-hosted-billing.md)
before upgrading an existing billed deployment. This is not a turnkey infrastructure installer.

The first `organic.audit` implementation is documented in
[`docs/organic-audit-implementation.md`](../docs/organic-audit-implementation.md).
It is MCP-first, read-only, and spending-disabled by default; provider-backed live
acceptance is separate from the local implementation tests.

The independent `organic.keyword_plan` workflow is documented in
[`docs/keyword-plan-implementation.md`](../docs/keyword-plan-implementation.md).
It researches bounded keyword opportunities and saves a report, inventory, and evidence for
later content planning. It does not create a calendar, write content, or publish anything.
Execution requires the existing DataForSEO/native-model credentials and an explicitly enabled
`TIN_LITE_KEYWORD_PLAN_MAX_COST_USD` ceiling (default `0`; enabled range `5`–`25`).
The optional Search Console input reuses the project's existing integration.

`content.plan` turns exact audit/keyword publications into an amendable saved program.
See [`docs/content-program-implementation.md`](../docs/content-program-implementation.md).
It requires a saved project workflow, defaults to six calendar months, and prepares weekly
batches without repeating research or publishing. Its declared `prerequisites` name the exact
audit and keyword runs through `audit_run_id` and `keyword_run_id`, so a start without them is
refused with a `prerequisite_missing` diagnostic before any row is written. Initial planning and AI amendments require
`TIN_LITE_CONTENT_PLAN_MAX_COST_USD >= 1` (default `0`); each is one bounded model request.
Ordinary batch preparation makes no model/provider call. Future edits use the shared project
file commit contract. Requested AI revisions hold selected batches until Apply or Discard.

## Product API

Hosted Tin is at `https://app.tin.computer`. For a self-hosted deployment, use your
configured public origin. Obtain a project ID from authenticated `GET /api/projects`;
do not use another deployment's project identifiers.

Open the root URL for the Paper-derived product UI. Clerk signs the user in, Tin Lite resolves that
identity to flat project memberships in its own PlanetScale database, Chat uses Luna through
`/api/chat`, Workflows lists and starts the live catalog, and Activity reads product events from
Postgres. A legacy Tin Computer account does not inherit legacy projects or permissions. A
`tin_users` row is created only after that Clerk identity successfully reaches Tin Lite, which gives
the two products a shared identity provider without making Tin Lite depend on the legacy Supabase
database. If that identity has no Tin Lite project membership, the product idempotently creates one
personal workspace and project, initializes the project's code.storage state repository, and grants
both administrative workspace membership and operational project membership. An invitation is
accepted before this check, so an invited user joins only the intended project. They can explicitly
create their personal workspace later.
The UI is packaged vanilla HTML/CSS/JavaScript served by FastAPI; there is no separate frontend
deployment.

Start a run:

```bash
curl --fail --silent --show-error \
  -X POST \
  -H "authorization: Bearer ${TIN_SESSION_TOKEN}" \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"${TIN_PROJECT_ID}\"}" \
  "${TIN_LITE_PUBLIC_URL}/api/workflows/content.design_md/runs"
```

Use the returned `id` with either the JSON status endpoint, the minimal run card, or the durable
artifact endpoints. Markdown artifacts also have a Paper-derived reading view. The viewer itself
stays generic; the run route only supplies it with the file:

```text
GET /api/workflows/runs/{id}
GET /runs/{id}
GET /api/workflows/runs/{id}/artifact
GET /api/workflows/runs/{id}/artifact/document
GET /documents/runs/{id}
```

The status response carries `X-Tin-Read-Source: postgres`; a ready artifact carries
`X-Tin-Artifact-Source: code.storage`.

The product workflow Registry and project-owned configured workflows are available from:

```text
GET  /api/workflows
GET  /api/workflows/{workflow_id}
POST /api/workflows/{workflow_id}/runs
GET  /api/projects/{project_id}/workflows
POST /api/projects/{project_id}/workflows
PUT  /api/projects/{project_id}/workflows/{project_workflow_id}
POST /api/projects/{project_id}/workflows/{project_workflow_id}/runs
POST /api/projects/{project_id}/workflows/{project_workflow_id}/pause
POST /api/projects/{project_id}/workflows/{project_workflow_id}/resume
```

The Registry is the searchable set of immutable workflow templates. Its rows may be grouped by an
optional `system` slug pinned in each definition; the global `workflow_systems` projection supplies
only the display name and order. Unknown and absent slugs remain callable and appear last as
unassigned. This taxonomy applies only to Registry discovery. “Your workflows” contains
project-owned configurations that pin one Registry revision, store schema-validated inputs, and
optionally own a daily or weekly Temporal Schedule in an IANA timezone. Product reads remain
Postgres-only. A scheduled occurrence creates an ordinary pinned workflow run through one explicit
dispatcher; overlap is skipped and at most 24 hours of missed work is caught up.
The dispatcher remains alive while its child awaits human review; the catch-up window
is not a review deadline. Existing deployments must remove the old deadline from stored
schedule actions; see [scheduled review recovery](scheduled-review-recovery.md).

Starting a Registry template directly creates an unsaved run. While active it appears in My system;
after it finishes it remains inspectable in Activity with any durable output in Files. Saving a
template creates a reusable project workflow that remains in My system, where its future inputs and
schedule are editable inline. Opening an active run expands the same card with its Postgres-backed
facts; there is no separate run-inspector page. Email campaign runs additionally read a bounded
delivery projection for recipient, touch, schedule, send, reply, and failure state. Provider message
and request identifiers are not returned to the browser.

All product API routes require a Clerk session token and enforce the requested project's local
membership. Project membership has one access level: any current member can use the project and
create a seven-day invitation for another verified email address. Invitation tokens are stored only
as SHA-256 hashes, can be accepted only by the intended verified Clerk identity, and grant the same
membership as every other member. Only the project's creator (any member when the creator is
unknown) may delete it; deletion refuses the personal project, requires the exact name as
`confirm_name`, is idempotent per `request_id`, and leaves a tombstoned row plus billing history
(`docs/project-deletion-implementation.md`):

```text
GET  /api/auth/me
POST /api/projects/bootstrap
GET  /api/workspaces
POST /api/workspaces/bootstrap
POST /api/workspaces/{workspace_id}/projects
DELETE /api/projects/{project_id}
GET  /api/projects/{project_id}/members
POST /api/projects/{project_id}/invitations
POST /api/invitations/{token}/accept
```

Tin's MCP resource server is mounted at `/mcp`. It uses Clerk-issued OAuth access tokens, publishes
RFC 9728 protected-resource metadata at `/.well-known/oauth-protected-resource/mcp`, and gates
every tool through the same project-membership queries as the browser API. It currently asks only
for Clerk's standard `openid` scope because all project members have the same access. MCP
credentials do not create a second user or tenancy system. Workspace membership permits project
creation and administration only; every project operation still requires an explicit
`project_memberships` row.

MCP exposes the same distinction. `start_workflow` creates an unsaved run;
`create_project_workflow`, `list_project_workflows`, `update_project_workflow`, and
`start_project_workflow` explicitly manage and run reusable configurations. Each write uses the
same membership checks, schema validation, pinned definition, scheduling service, and optimistic
settings revision used by the browser.

The shared Clerk instance currently enables Dynamic Client Registration for broad MCP-client
compatibility, with consent enforced and `openid` as the only default scope. Project access is still
decided locally on every call. Prefer pre-approved Client ID Metadata Document clients and disable
public registration once Clerk's CIMD support and Tin's supported-client list make that practical.

`project.memory` gardens durable workflow outputs into the managed project wiki index and exposes
its Postgres projection at:

```text
GET /api/projects/{project_id}/memory
```

Its agent instructions are ordinary ordered skill packages under
`workflow_skills/project-memory/*/SKILL.md`. These are Tin runtime instructions, separate from
Codex defaults and project-authored skills. The workflow writes only `wiki/INDEX.md`; hand-authored
wiki and project skill files remain untouched. Workspace identity now groups and administers
projects, but workspace-level files and skill layering remain deferred.

`scan.report` is the first small system-wiki proof. Tin publishes one original read-only guide to
`wiki/system` at `growth/project-scanning.md`; each scan run pins that exact commit, reads it with
the current project memory, and writes `reports/SCAN.md` to project state. The guide is evidence,
while `workflow_skills/scan-report/*/SKILL.md` contains the workflow instructions. The trusted
switchboard performs the text-model call and Temporal durably orchestrates the two effects, so no
E2B sandbox or second workflow engine is introduced.

`visibility.audit` is a bounded Luna-only AI visibility diagnostic. It freezes five target-blind
buyer questions, asks each once with required web search and once without tools, and scores the
target through a found → mentioned → evaluated → shortlisted → selected-first ladder. Each run
atomically publishes the readable `reports/AI_VISIBILITY.md` and its raw evidence at
`reports/visibility/{run_id}/evidence.json`. Individual provider results are receipt-backed so an
activity retry resumes completed calls instead of paying for the whole panel again. It adds no
table, E2B sandbox, DataForSEO dependency, or parallel execution engine.

`content.answer_page` turns durable project context into one researched, reviewable Markdown page.
It prefers the latest visibility audit and otherwise works from project memory, so its only input
is `project_id`; GitHub access is not required. One forced-search Luna call publishes
`reports/ANSWER_PAGE.md` and bounded evidence atomically through Temporal. Because this is
customer-facing content, the artifact becomes readable while the run waits in `needs_input`; an
approval from the draft reader resumes and completes it. Workflows presents every live gate through
one needs-you queue banner and one matching filter count; completed reviews remain historical
Activity events rather than permanent stages on finished rows. Report and memory workflows do not
pause for review. The workflow does not edit or publish the customer's website, create an E2B
sandbox, or add a provider credential path.

`project.weekly_brief` summarizes the previous seven days of durable project runs, artifacts,
memory, and product Activity into a dated Markdown report under `reports/weekly/`. It is intended
to be configured from the Registry and scheduled—for example, every Tuesday at 09:00 in the
project member's timezone. Its first delivery surface is the normal in-product Activity feed and
Markdown reader. Email, Slack, and other delivery providers remain separate integrations so a
notification failure cannot invalidate a successfully created report.

Activity is a flat Postgres-backed ledger rather than a card feed. It groups real product events by
day, keeps run facts in fixed mono lanes, links completed Markdown artifacts into the generic
reader, and filters runs, needs-you moments, and founder edits without inventing records for kinds
that do not yet exist.

`project.task` is the escape hatch for concrete one-off work, not a second chat mode and not a row
per ad-hoc workflow. One task may be active per project. Temporal owns its durable lifecycle while
Codex app-server runs one turn at a time in a fenced E2B workspace with web search enabled through
the existing proxy. Its transcript and controls live on `/api/tasks/{run_id}`; Luna Chat stays
available and separate. Pausing saves a branch checkpoint and kills the sandbox. Read-only tasks
finish with a result; modifying tasks expose the exact bounded diff and cannot reach canonical
project state until a member approves it under the existing lease, fencing, and `expectedHeadSha`
guard. Stop never applies unfinished changes.

```text
GET  /api/tasks/{run_id}
POST /api/tasks/{run_id}/messages
POST /api/tasks/{run_id}/pause
POST /api/tasks/{run_id}/resume
POST /api/tasks/{run_id}/stop
POST /api/tasks/{run_id}/approve
```

MCP clients reach the same boundary: `get_run` and `list_project_runs` add a `task` block with the
phase, the question a task is waiting on, and (for `get_run`) proposed file paths and the recent
transcript; `send_project_task_message` answers a waiting question, resumes a paused or reviewing
task with direction, or steers a running one; `approve_workflow_run` applies reviewed task changes.
Both surfaces go through `project_task_control`, so answering in either one continues the run.
Pause and stop remain web-only for now.

`codex.procedure` is the separate reusable executor for registry workflows whose implementation is
a pinned Codex procedure rather than a bespoke trusted activity. A Tin-owned source package under
`codex_procedures/<workflow>/` contains `PROMPT.md` plus one or more Codex skills. Catalog sync
publishes the workflow definition, prompt, and exact skill files in one immutable code.storage
revision; every run reads only that pinned revision. Optional project-owned dependencies remain at
`.agents/skills/<name>/SKILL.md` in project state and are resolved from the run's project checkout.
The executor creates one fenced E2B sandbox, enables Codex web search through the switchboard
proxy, and enforces one typed workspace and result contract. Project-artifact procedures permit
exactly one declared, bounded UTF-8 text output. GitHub procedures receive a tokenless repository snapshot
plus bounded, explicitly untrusted open-PR evidence and may return only a validated pull-request
proposal. The sandbox is destroyed after its ephemeral retry checkpoint is durable. Temporal
carries only the run ID. A procedure can use the normal workflow review policy, but it does not
acquire `project.task` steering, pause/resume, or task-transcript semantics.

`research.deep_dive` uses this executor to test a bounded project question top-down against current,
source-backed evidence and publishes `reports/RESEARCH_DEEP_DIVE.md`. It remains a report workflow
and therefore does not pause for review. `content.public_article` turns durable project evidence
into `reports/PUBLIC_ARTICLE.md`, applies a separate fact-preserving final edit, and enters the
normal human-review queue because it is public-facing content. Neither procedure publishes,
contacts anyone, or acquires the steering and arbitrary-diff semantics of `project.task`.

Workflow definitions may also contain one small `presentation.flow`. It is immutable presentation
metadata, not an executable graph: the Registry renders it only inside a template's setup panel.
The first diagrams describe `site.health_improve`, `project.weekly_brief`, and
`outreach.email_campaign`. They share seven node meanings and two edge meanings so the visual
language stays recognizable as the catalog grows.

`content.diagram` uses the same vocabulary to create a reviewed, editable Mermaid source artifact
at `diagrams/{slug}.mmd`. Tin validates a deliberately small Mermaid dialect before publication;
custom styling, directives, scripts, and disconnected graphs are rejected. Project Files renders
the source as a Tin diagram and also offers Source and ASCII views. SVG and ASCII remain derived
views rather than additional canonical files, and generic Markdown can embed the same safe dialect
in a fenced `mermaid` block.

`growth.onboarding` and `growth.onboarding_plan` form the `start-here` system, the first group in
every list, and are the onboarding for a new project in two parts. Part 1: the founder's coding
agent fills one form from the codebase (product URL, eleven `system_*` fields, notes, a timezone)
plus the founder's multiple-choice constraints (hours, budget, urgency, the sixty-day outcome, hard
no's), and the plan reads the site, any project memory and the live `tin_state` (every registry
workflow with a runnable flag, the run service's refusal reason, who unblocks it, declared
prerequisites, connected integrations) and writes `reports/GROWTH_ONBOARDING_PLAN.md`: the business as understood, the programs worth attention,
what changes for today's activities, and a checklist of three to six streams, each naming the
integrations it needs, over a fenced `tin-plan` block Tin reads itself. Part 2: the native parent
holds ("Set it up"); the agent shows the founder the four levels, starts the integration
connections the founder allows and confirms each, then records the pick, the control choice and
every connection decision with `record_onboarding_picks`, which ticks the plan file itself (a
"connected" pick is checked against the project's integrations, and approval on either surface is
refused until an option and a control are ticked); the parent sets the picked level up (saved
schedules through the same path the API uses, first one-off runs as Temporal children), skipping
and reporting anything still blocked, and
publishes `reports/onboarding/<run>/RESULT.md`: what runs now, what is scheduled, what waits on
whom, the five project views to watch, and what to expect in the next hours, days and weeks. The
MCP server carries instructions and a `get_started` tool that says all of this to a fresh
session. The plan can also run on its own.

The plan is an LLM flow (`growth_plan.py`), not an agent: choosing the steps was never the job.
Code fetches the public site (every hop resolved and refused unless all its addresses are public,
same-site redirects only, bounded pages plus `llms.txt` and the sitemap), reads project memory,
runs the packaged scorer, computes each workflow's availability from `tin_state`, applies hard
no's and the founder's own prohibitions, keeps one configuration per workflow, and renders the
file including the whole `tin-plan` block, so a workflow key, mode, market or integration never
comes from a model. Models supply judgment through schema-bound steps with stable identifiers:
what is known about the business, the scorer profile (each parameter needs a verbatim basis in
the evidence or code drops it), the scope decision (where growth breaks, which few systems, the
founder's part, one number to watch), one role per system, the table cells and the spoken view
with the answers to what the founder asked for. The judgment steps (`facts`, `scope`, `view`)
use `gpt-6-sol`; the mechanical steps use `gpt-6-luna`; both at medium effort, pinned in the
definition with a digest of the rules, rubric, programs, scorer and prompts. When code changes a
system's setup after its text was written, that text is rewritten to match the final setup; a
code-side lint then sends only the offending sentences for one or two short repairs. Each step
is receipted by an owning effect, so an activity retry replays completed steps and buys nothing
twice, an unconfirmed step stops the run, and an unusable result gets exactly one replacement
under its own step identifier. When the direct fetch is blocked, thin or unreachable, one
receipted hosted-web-search read stands in for it, the way `content.answer_page` searches. The
saved file passes the same parsers setup reads it with before it is published.

Founder-facing words on an MCP result come in two fields the agent treats apart, built by
`_founder_words` in `mcp_server.py`. `quote` is Tin's own words to the founder (the plan's
"Tin's view" while the run waits for a pick, the setup handshake's win, roles and what is
under way once it is done): the agent relays it as given, set apart, unchanged. `relay` is a
list of facts the agent tells the founder in its own words (a run started, a connection page
opening, the outlook, the two pages, what was left out and why, an approval recorded): the agent
weaves them into its reply. A producer marks its own text: only words written to be read as
Tin's go in `quote`; status and guidance go in `relay`, and the agent's own next steps go in
`note` or `get_started`, never in either. `growth_onboarding.founder_words` builds the
handshake's split and `render_report` writes it into `RESULT.md` under a fenced `tin-words`
block that `report_words` reads back for `get_run`; the review explanation stored for the plan
is Tin's view alone, since the dashboard shows it to the founder. `tell_the_founder` is the two
joined, kept one release for clients on older instructions.

`creative.character` and `creative.product_demo` form the `creative-studio` system.
`creative.character` is a native model workflow: the switchboard fetches the product page itself
(public HTTPS only, resolved addresses checked, HTML and up to three stylesheets bounded),
extracts what a designer would notice (title, description, headings, calls to action, body
text, brand colors weighted by where they appear, JSON-LD and Open Graph facts for
client-rendered shells), pairs that with project memory and the founder's brief, and sends one
prepared request to `gpt-6-sol` at medium reasoning with a strict JSON schema for the concept
and the drawing. The result is one animatable SVG at `characters/{slug}.svg`; the
`character-svg.v1` contract accepts only pure geometry (no text, images, scripts, or external
references) with the five state groups the video renderer flips: `mouth-closed`, `mouth-mid`,
`mouth-open`, `eyes-open`, `eyes-closed`, plus an optional `expr-happy`. The validator's exact
complaints go back to the model for at most two repairs and one geometry-checklist refinement
pass follows. Context, model call, commit, and projection are separate effect receipts, so a
retry replays instead of paying again; a run takes about a minute and a half and needs no
sandbox. `creative.product_demo` is a `codex.procedure` on the `studio` sandbox profile; the
named `character` must already exist as `characters/{character}.svg`, a required prerequisite
that is skipped when the input is empty, and the Feature map is recommended. The profile is the
browser image plus the `tin-studio` toolkit: ffmpeg, a pinned `resvg`, Pillow, and the capture,
voice, and render scripts under `sandbox/studio/`, built as `tin-lite-codex-studio` with four
vCPUs). It captures the founder's public pages at a 9:16 phone viewport through its own
Camoufox, never in real time: it takes settled keyframes plus a step log and synthesizes the
scroll, tap, zoom, hook, and word-synced caption motion at a fixed frame rate, so the footage
stays smooth however loaded the machine is, and it seats the project's character in the corner
as the narrator. Voice lines go through the run-bound `POST /internal/run-tools/studio/voice`
route; the switchboard holds the fal credential (`FAL_KEY`), calls Gemini TTS and word-level
Whisper, writes one `effect_receipts` row per line so a retried request replays instead of
paying twice, and enforces per-run line and character quotas (`TIN_LITE_STUDIO_MAX_VOICE_LINES`,
`TIN_LITE_STUDIO_MAX_VOICE_CHARACTERS`). The result is one `demos/{slug}.mp4` checked
structurally by `demo-video.v1` (fast-start MP4, 1080x1920, a voice track, 8 to 90 seconds); it
is the first binary procedure artifact, allowed up to 16 MB only for that declared media type.
Both workflows enter the human-review queue, and Files plays the video and shows the character
inline. Migration 027 lets `run_tool_grants` carry a `tin.studio` grant with no integration
connection. The studio image is verified live with `uv run python scripts/verify_studio_sandbox.py`.

`creative.character_direct` is the sandbox-free sibling of `creative.character`. It produces the
same contract-checked SVG through the model provider boundary instead of a Codex procedure: the
switchboard fetches the product page itself (public HTTPS only, resolved addresses checked, HTML
and up to three stylesheets bounded), extracts what a designer would notice (title, description,
headings, calls to action, body text, brand colors weighted by where they appear, JSON-LD and
Open Graph facts for client-rendered shells), pairs that with project memory and the founder's
brief, and sends one prepared request to `gpt-6-sol` at medium reasoning with a strict JSON
schema for the concept and the drawing. The validator's exact complaints go back for at most two
repairs, and one refinement pass plays the role of the agent's look-and-fix step. Each model call
and the publication are effect receipts, so a retry replays instead of paying again. Neither
character workflow has a style picker any more; outline is the house style.

Trusted text-model calls have a provider-neutral routing boundary above the official OpenAI,
Anthropic, and Google Gen AI SDKs, plus an explicit OpenRouter adapter using its OpenAI-compatible
Chat Completions API. A route explicitly names its provider, model, and required
capabilities; Tin never infers the provider from a model-name prefix. The common contract currently
covers text, JSON Schema output, reasoning effort, normalized usage, and request IDs. SDK retries
are disabled so workflow-level receipts and Temporal retries remain the only ambiguity policy.
`TIN_LITE_LUNA_API_KEY` remains the switchboard-only OpenAI credential and continues to serve Luna;
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, and `OPENROUTER_API_KEY` configure the other adapters. Identity-linked Anthropic
keys also require `ANTHROPIC_WORKSPACE_ID`; workspace-scoped keys may omit it. None of these
credentials is sent to Temporal, code.storage, the broker, or E2B. No model routes are silently created merely
because a key exists; each workflow definition must deliberately select a registered route.

Shared-service calls now retain trusted per-run usage receipts, including rejected output and
unconfirmed requests, independently of each workflow's output receipt. Hosted users use Tin's
server-held keys; self-host operators set their own. Codex execution has a separate protected API
runner and trusted usage contract; historical OAuth receipts retain their original meaning.
Native service receipts are not interchangeable with Codex usage or a complete supplier invoice.
See [billing coverage](workflow-billing-coverage.md) and the historical
[model-service foundation](model-service-accounting.md).

The intended knowledge hierarchy is system wiki → workspace wiki → project wiki. Only the system
and project file levels exist today; workspace storage waits for a real shared-knowledge need and
will require its own repository, access, and revision boundary.

Built-in templates and future project-owned forks share the `workflows` table. Their immutable
definition documents live in code.storage, while Postgres holds the searchable projection. Every
run pins the exact definition commit it executed. Temporal's explicit code registry remains the
executor allowlist.

Checkpoint B adds one thin chat endpoint. Browser requests include a stable UUID so a lost response
can be retried without starting the selected workflow twice:

```text
POST /api/chat
{"project_id":"...", "request_id":"...", "message":"Analyze this repository and generate DESIGN.md now."}
GET  /api/projects/{project_id}/chat/messages
```

Luna uses `gpt-6-luna` on the Responses API. Its tools are compiled from `GET /api/workflows`,
and a selected action is executed through `POST /api/workflows/{id}/runs`; there is no privileged
model-only dispatch path. The trusted switchboard reads `TIN_LITE_LUNA_API_KEY`. That credential
is never sent to Temporal history or an E2B sandbox. `OPENAI_API_KEY` and
`CODEX_API_KEY` are not accepted server configuration names; both native OpenAI calls and the
protected Codex API relay use the explicit switchboard setting. Historical OAuth pins cannot start new compute; existing output remains recoverable.
The project transcript is stored in Postgres and shared by project members. Luna receives a bounded
window of completed turns, project memory, and the current Postgres run projection; the transcript
is not coupled to an OpenAI response chain or a Codex session handle. There is intentionally no
chat-thread registry while each project has one chat.

The core ownership and workflow tables include:

- `tin_users`, `workspaces`, `workspace_memberships`, `projects`, `project_memberships`,
  `project_invitations`, `workflow_systems`, `workflows`, `project_workflows`, `workflow_runs`,
  `activity_events`, `chat_messages`, `project_task_entries`
- `effect_receipts`, `broker_grants`, `tin_schema_migrations`

Integrations add `integration_connections`, `integration_auth_attempts`,
`integration_call_receipts`, `integration_webhook_deliveries`, `run_tool_grants`, the outreach
campaign tables, and `project_test_identities`, which holds the product accounts Tin creates for
its own walkthroughs with the password encrypted under the switchboard credential key.

Lease and fencing fields live directly on `workflow_runs`; there is no separate lease table or
database-backed executable registry.

## Local commands

Procedure output preservation and safe publication recovery are documented in
[the first-half implementation and rollout notes](../docs/procedure-publication-recovery.md).
The additive migration must precede runtime deployment; the notes list outstanding live gates.

```bash
uv sync
uv run tin-lite migrate
uv run tin-lite sync-builtins
uv run tin-lite serve
uv run pytest
uv run ruff check .
uv run lint-imports
```

`serve` starts the worker with the application. `tin-lite-worker` is the separate worker entry
point, not an additional prerequisite to running `serve`. For contributor-only checks and a
disposable test database, see [CONTRIBUTING.md](../CONTRIBUTING.md).

CI runs formatting, linting, the test suite, import boundaries, and a replay of the accepted
Temporal Cloud history. The routing corpus can be evaluated without executing workflows:

```bash
uv run python scripts/evaluate_luna.py \
  --project-id "${TIN_PROJECT_ID}"
```

The application reads the existing root `.env`. Browser/API authentication additionally requires
`CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`, and an explicit comma-separated
`CLERK_AUTHORIZED_PARTIES`; `CLERK_JWT_KEY` is optional for networkless verification. Production
pins authorized browser traffic to `https://app.tin.computer` in runtime configuration and keeps
the Clerk secret in `/etc/tin-lite/clerk.env`, outside releases and source control. API-enabled
Codex runs receive only short-lived run-scoped access in the protected controller, never a
reusable provider key. Tin no longer requires a pooled login or `auth.json`. Product Clerk
login and model-provider authentication are separate systems.

MCP and OAuth-based connection setup also require `TIN_LITE_MCP_OAUTH_CLIENT_IDS`, an explicit
comma-separated list of approved Clerk OAuth client IDs or exact CIMD URLs. Empty disables
OAuth access. Configure approved IDs before deployment; browser sessions do not require this
setting. See [client admission](clerk-agent-connection.md#oauth-client-admission).

The trusted switchboard may also read `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, and
`OPENROUTER_API_KEY` for native workflow
model nodes. OpenAI continues to use `TIN_LITE_LUNA_API_KEY`; `OPENAI_API_KEY` and `CODEX_API_KEY`
remain forbidden configuration names. Use the explicit trusted-server setting for API execution;
do not put reusable keys in sandbox environments. See [Codex API execution](codex-api-pilot.md).

Product integrations are project-owned and are disabled until their provider application is
configured. Google Search Console and Google Workspace use
`TIN_LITE_GOOGLE_OAUTH_CLIENT_ID`, `TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET`, and a 32-byte URL-safe
base64 `TIN_LITE_INTEGRATION_CREDENTIAL_KEY`; their registered redirect URI is
`https://app.tin.computer/integrations/callback/google`. Search Console requests only
`webmasters.readonly`. Workspace starts with Gmail read-only and Calendar events read-only, then
requests `gmail.send` separately when a founder enables email delivery. Run
`bash infra/bootstrap_google_workspace.sh` to idempotently enable only Gmail and Calendar APIs in
the guarded `tin-lite-integrations` Google Cloud project. GitHub uses a GitHub App configured
through `TIN_LITE_GITHUB_APP_SLUG`,
`TIN_LITE_GITHUB_APP_ID`, `TIN_LITE_GITHUB_APP_CLIENT_ID`,
`TIN_LITE_GITHUB_APP_CLIENT_SECRET`, `TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH`, and
`TIN_LITE_GITHUB_WEBHOOK_SECRET`, with OAuth-on-install callback URL
`https://app.tin.computer/integrations/callback/github` and webhook URL
`https://app.tin.computer/webhooks/github`. The app requests repository Contents
write and Pull requests write; GitHub shows those permissions and repository selection during
installation. Tin exchanges the one-time OAuth code only to prove the signed-in GitHub user can
access the returned installation; it does not store that user token. Tin stores the installation
ID, mints short-lived installation tokens on demand, and never stores a user PAT. All provider
credentials remain on the trusted switchboard and are explicitly rejected from E2B sandbox
environments.

Google Ads is linked by manager invitation rather than OAuth. Configure Tin's manager account with
`TIN_LITE_GOOGLE_ADS_MANAGER_CUSTOMER_ID` and `TIN_LITE_GOOGLE_ADS_MANAGER_REFRESH_TOKEN` (a refresh
token minted once, with a passkey-capable manager admin, against the same Google OAuth client with
the `https://www.googleapis.com/auth/adwords` scope; when it was minted against another client, set
`TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_ID` and `TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_SECRET` too), optionally
`TIN_LITE_GOOGLE_ADS_DEVELOPER_TOKEN` and `TIN_LITE_GOOGLE_ADS_API_VERSION`. Enable the Google Ads
API on the `tin-lite-integrations` Cloud project; API access levels now attach to that project and
Basic access is required for production accounts. Founders enter their ten-digit customer id, Tin
sends the invitation from the manager account, and they accept it under Admin, Access and
security, Managers in Google Ads.

The first provider capabilities are bounded rather than generic HTTP proxies. Search Console can
list verified properties and read a validated analytics panel for one selected property. GitHub
can list installation repositories and, only after the write-permission opt-in, create or recover
one deterministic `tin/*` branch, write at most ten bounded files, and open one idempotent pull
request. It cannot modify `.github/workflows/`, push to the base branch, or expose an installation
token to a workflow or sandbox. Signed GitHub webhook deliveries are deduplicated; removing the
selected repository or suspending the installation projects `needs_attention` into Tin.

Workflow definitions declare provider dependencies through a strict, versioned
`integration_requirements` list. A run checks the exact definition commit it pins and verifies the
project's connected account, selected resource, health, and required write opt-in before starting
Temporal. Provider effects execute only through switchboard-owned typed adapters and write
run-linked receipts. Tin does not expose a generic provider gateway and never injects provider
credentials into E2B.

`outreach.email_shortlist` is a bounded Codex procedure. Its E2B sandbox receives a short-lived,
run- and lease-bound Tin MCP grant for only the declared Gmail/Calendar read capabilities; Google
credentials remain on the switchboard. It publishes exactly `outreach/email/SHORTLIST.csv`.
Project members and their MCP clients can revise that CSV through the generic project-file API,
using a request UUID and the current code.storage revision as the concurrency guard.

`qa.signup_walkthrough` is the first browser-backed Codex procedure. Its immutable definition
declares a sandbox profile (`browser`, 1800 seconds, open egress) and a test identity. The run
creates the sandbox from the `TIN_LITE_E2B_BROWSER_TEMPLATE` alias, which layers a pinned Camoufox
build (a fingerprint-hardened Firefox), a pinned Cloudflare WARP client, and the Tin-owned
`camoufox` MCP server on the standard Codex image. The runner starts WARP as a local SOCKS proxy
and registers the MCP server, which launches one persistent Camoufox on an Xvfb virtual display
for the whole run and exposes bounded text tools plus viewport resizing and a bounded screenshot
of the visible page; Codex writes no browser scripts and there is no
other browser. By default the browser sends only Cloudflare's challenge hosts through WARP (its
dependencies are IPv6-only) and everything else out directly, so payment processors see the
sandbox's own cloud address rather than a shared VPN exit; `TIN_BROWSER_EGRESS` selects `split`,
`warp`, or `direct`. Every version in the image is pinned in `sandbox/template.py`, including the
Codex CLI release, and the model every sandbox runs is pinned in `sandbox/codex_config.toml`, so a
template rebuild is the only way they change. Before the sandbox starts, the switchboard mints one product
account per run: the email is a plus-addressed alias on the project's connected Google Workspace
mailbox and the password is generated on the switchboard, encrypted with the integration credential
key, and stored in `project_test_identities`. The sandbox receives the plaintext only through its
run context, reads verification mail through the existing `tin-run` Gmail tools, and reports the
account status through the run-bound `record_test_identity_status` tool. The procedure writes
exactly one report at `reports/qa/signup/{host}/{started_at}.md`, resolved on the switchboard from
the run's `product_url` host and UTC creation time so every product and run keeps its own file,
whose frontmatter must declare
`activation_reached: true|false`; both the sandbox and the switchboard refuse an artifact that
contains the password. A retried run reuses the same identity row, so a half-created account is
resumed by logging in rather than re-registering. When a free trial requires a card, the
walkthrough can use an optional card supplied through the one-run start form. The card is
encrypted separately from ordinary inputs and deleted when the run ends. Without one, the
card-required trial is a wall. With one, the walkthrough enters it once and cancels the trial
from the product's billing settings before writing the report; a plan that charges money now
stays a recorded paywall. When a product asks for a phone number, the identity gives the
Tin-owned receive-only Twilio number configured as `TIN_LITE_TEST_PHONE_NUMBER`; Twilio posts
inbound SMS to the signature-checked `/webhooks/twilio/sms` route and the run reads codes through
the `search_sms` run tool. Tin never sends SMS, so a voice-only or reply-required check stays a
wall. It takes no screenshots, spends no money, contacts nobody, and does not
pause for review.

`product.code_map`, `product.deep_dive`, and `qa.product_audit` form the `product-qa` system with
`qa.signup_walkthrough`. The first two write product understanding into project memory rather
than into new files: each owns one subsection of `wiki/INDEX.md` → `## Product` (`### Code map`
from the connected GitHub repository, `### Feature map` from the docs, the live signed-in product,
and the code map), declared through `output.section` and checked by the `memory-section.v1`
validator, which rejects any change outside the owned section and any feature line outside the
closed status and claim vocabulary. `project.memory` keeps that block verbatim, and a procedure
commit to the index refreshes the memory projection immediately. `product.code_map` reads a
read-only repository snapshot while writing into the project-state checkout at `/home/user/state`.
The two browser procedures reuse the walkthrough's active test identity (`identity.reuse: active`)
instead of registering again, and declare it as a required prerequisite: `product.deep_dive`
needs an active identity and a succeeded walkthrough on the same product host (the Code map is
recommended), and `qa.product_audit` additionally needs the `### Feature map` section the deep
dive writes. A start without them returns `prerequisite_missing` with the workflow to run first;
see [`docs/workflow-definition-foundation.md`](../docs/workflow-definition-foundation.md). `qa.product_audit` exercises every mapped feature as a user, runs
cross-cutting checks (dead links, failed requests, console errors, form validation, empty states,
viewport, accessibility, observe-only security smells), and writes one dated
`reports/qa/audit/{host}/{started_at}.md` whose `product-audit.v1` validator enforces the
frontmatter counts, section order, coverage table, and severity-sorted findings.

`outreach.email_campaign` requires `outreach/email/SHORTLIST.csv` at HEAD before it starts,
reads only rows marked `selected`, pins the shortlist revision, sender
account, copy, recipient set, daily cap, pacing, and local send window, and presents a Markdown
review snapshot before any delivery. Approval starts Temporal recipient children. Initial and
follow-up messages use stable delivery keys, an account-global reservation ledger, exact Gmail
message IDs, and fail-closed reconciliation when provider acceptance is ambiguous. A detected
reply suppresses the follow-up. Browser and public MCP endpoints expose the same Postgres campaign
projection and can stop remaining delivery work; already delivered mail is never represented as
recalled.

The public MCP also exposes format-agnostic project-state list, search, read, history, commit, and
revert tools. These tools operate only on safe UTF-8 project-state paths, never a connected source
repository, and every mutation is one optimistic, auditable code.storage commit.

`site.health_improve` is the first integration-backed Codex procedure. Its pinned definition
declares a GitHub repository workspace, bounded pull-request result, and the exact repository checks
that must pass after Codex finishes. The switchboard materializes one immutable repository snapshot
inside E2B without exposing an installation token. It separately supplies the 20 most recently
updated open PRs targeting the same base branch, bounded to 100 changed files and 250 KB of
untrusted titles, bodies, paths, and patches. Codex must avoid duplicate work, and the switchboard
rechecks open PR paths immediately before delivery. It then revalidates the UTF-8 diff, file budget,
protected paths, base commit, and verification result. The GitHub adapter creates or recovers one
deterministic branch and pull request and never merges or deploys it. A short
Markdown receipt is also committed to project state; Activity links directly to the external PR for
human review. Repository content and live-page evidence remain untrusted inputs, while installation
credentials remain entirely on the switchboard. The older native site-health executor stays
registered only for already-recorded Temporal history compatibility.

Sandbox profiles are built by `sandbox/template.py`. API execution uses the isolated,
browser API or Studio API templates. Historical OAuth compute is refused. Select only the template required for a reviewed change with
`--only <alias>`; inspect the aliases and pinned versions in the script.

The following are hosted-operator commands, not a contributor test setup or a portable
self-host installer. Template builds and deployment affect real infrastructure:

```bash
uv run python sandbox/template.py            # add --only <alias> to build one template
TIN_LITE_GCP_PROJECT=your-gcp-project bash infra/deploy.sh
```

`scripts/verify_browser_sandbox.py` creates one browser-profile sandbox with open egress and proves,
as the unprivileged user, that the shared `warp-up` script connects WARP, that the proxy reaches an
IPv6-only host, that the `camoufox` MCP server renders a public page through WARP, and that the same
tools complete Cloudflare's Turnstile demo form end to end, then kills the sandbox. The demo uses
Cloudflare's always-pass test sitekey, so pass `--turnstile-url` to check a real-key page. Run it
after every browser template rebuild and before relying on `qa.signup_walkthrough`.

Deployment does not copy or require a ChatGPT login. The pooled-auth broker and its
refresh/reseed tools are removed. See [API-only execution](oauth-credential-security.md)
for preserving historical output, retiring host credentials, and deployment acceptance.
