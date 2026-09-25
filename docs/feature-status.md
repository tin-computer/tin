# Feature status and release readiness

Current as of September 24, 2026. This is the current capability overview;
internal implementation plans and production acceptance records are not part of this source release.
“Implemented” does not mean enabled for every deployment, independently security-audited,
or verified in a fresh self-hosted installation. The live Registry supplies each workflow's
inputs, prerequisites and supported schedule modes.

## Implemented product capabilities

| Area | Available behavior | Boundary |
| --- | --- | --- |
| Dashboard and MCP | Project files, workflow discovery, saved configurations, runs, Activity and review share the same services. | Exact project membership is required; workspace administration does not grant sibling-project access. |
| Built-in workflows | Context, research, visibility/site audits, keyword and content planning, a paid ads assessment (Google Search, advisory), an approval-gated Google Ads launch and a daily Google Ads monitor, style capture, drafting, diagrams, product QA, video and email outreach. | Provider configuration, connected resources and workflow-specific execution limits still apply. |
| Public workflow packages | Source validation and explicit maintainer registration for deterministic Python, multi-step managed-model Python, and Codex procedures. Catalog sync publishes pinned packages through the existing Registry. | Source support is not a production rollout. Unselected packages and shipped examples do not become customer workflows. Package runtime limits and normal billing still apply. |
| Product analytics brief | Explicit public PostHog procedure package: ordered activation, trends, traffic, error signals and a screened breakdown. Manual, daily and weekly definitions reuse saved workflows. | One connected provider project; no identity joins, recommendations or external delivery. See [qualification limits](product-analytics-brief.md). Source registration is not deployment. |
| Brand and design capture | Public first-capture package inspects a website and optional repository or source packet, then proposes BRAND.md and DESIGN.md for one atomic approval. Existing compatible documents stay unchanged. | Fixture-tested source support; updated browser image and catalog sync required. No site edits or refresh. Diagrams consume pinned active guidance; other marketing consumers and custom-font rendering remain deferred. See [capture scope](brand-capture.md). |
| Stripe and PostHog connections | First-party read-only `payments.stripe` (founder-pasted restricted key, validated per resource) and `analytics.posthog` (OAuth with PKCE, one selected project, US/EU Cloud) connections. Registered operations return server-side projected records with cursor paging for code and procedure bindings; offline fakes support package tests. See [the guide](stripe-and-posthog-connections.md). | Reads only; HogQL limited to one bounded SELECT without OFFSET. PostHog needs `TIN_LITE_POSTHOG_OAUTH_ENABLED` and a public https origin. Fixture-tested; no live Stripe or PostHog acceptance yet. Self-hosted PostHog is not supported. |
| Workflow creation and qualification | A repo-owned creator proposes packages and cases. Shared HTTP/MCP checks validate pinned files and assess existing run outputs and model costs; a CLI can start explicitly budgeted cases. | Creator installation uses private activation. No automatic publication, dashboard qualification editor or live analytics acceptance. Fixture checks do not establish model quality or measured cost. |
| Organic traffic system | The current parent can plan, optionally prepare technical fixes, draft the next eligible planned item, wait for review/revisions and deliver the approved article as an unmerged GitHub PR. | GitHub delivery requires the selected connection. Without GitHub, or in draft-only mode, the approved Markdown remains in project Files. Plan dates are not an automatic six-month publishing schedule. |
| Content review | Read the draft, request changes in text, review a new revision and approve through dashboard or MCP. Generation notes stay separate from publishable copy. | Approval applies to the reviewed revision. PR delivery neither merges the PR nor publishes the website. |
| Saved schedules | Eligible definitions support daily or selected-weekday execution at a local time in an IANA timezone, with skip-overlap and bounded catch-up. | Not every workflow is schedulable. Paid scheduled occurrences need standing spending authority, not just a positive balance. |
| Locked dashboard for browser sign-ups | A project with no workflow or run yet shows a locked dashboard: rail dimmed, one page that sends the person to their coding agent with the install line. Agent connection links and callbacks can finish the requested account and resource setup while the dashboard stays locked. Unlocks on the next reload once a workflow or run exists. `TIN_LITE_BROWSER_LOCK_ENABLED=false` turns it off. | Browser onboarding does not exist yet; the lock stands in for it. |
| One-off tasks | `project.task` has its own conversation, questions, pause/resume and exact-diff review. | It is not a reusable private code workflow or a replacement for the shared workflow engine. |

See [architecture](architecture.md), [content delivery](repository-aware-content-delivery.md), and the
[README](../README.md) for the product and execution contracts.

## Private workflows

Private packages live in project Files. A coding agent commits, validates and explicitly
activates an exact package revision through MCP. New saved configurations select the latest
active definition automatically; existing configurations and runs keep their selected version.
Editing files alone does not activate a workflow. Execution needs the isolated template plus
either the `TIN_LITE_PRIVATE_WORKFLOW_PROJECTS` allowlist or
`TIN_LITE_PRIVATE_WORKFLOWS_OPEN=true`, which admits every project and requires billing so each
run spends credits. Hosted billing enablement alone does not open private execution.

| Executor | Implemented | Not included |
| --- | --- | --- |
| `workflow.code` | Bounded Python, typed inputs, one durable text report/artifact, optional managed model calls, project API connections, and eligible daily/weekly schedules. | Arbitrary runtimes, raw secrets or direct SDK credentials in author code, recursive workflow starts, or delegation to a procedure. |
| Private `codex.procedure` | On-demand isolated procedures with declared project API connections, producing a bounded project artifact or an unmerged PR through the connected GitHub gateway. | Private procedure schedules, browser/Studio profiles, or a general one-command skill import. |

Code-only bounded execution uses no Tin credits. Managed model steps use hosted credits;
custom API, Stripe and PostHog requests use the connected provider account, which may charge
separately. The
trusted gateway inserts credentials; author code never receives the reusable key. Model access
uses Tin's server keys on hosted Tin and the operator's keys when self-hosted. The code
model contract currently admits only the explicitly registered OpenAI Luna and Astra routes;
the existence of Anthropic, Gemini and OpenRouter adapters does not enable arbitrary models
or prices.

Contributor contracts:
[activation](private-workflow-activation.md), [code execution](code-workflows.md),
[model steps](code-model-workflows.md), [API connections](project-api-connections.md),
[Stripe and PostHog connections](stripe-and-posthog-connections.md), and
[code schedules](code-workflow-schedules.md). Code-only calendar execution has been verified;
paid-model scheduling has fixture coverage, not production acceptance.

## Billing and model authentication

Hosted defaults are deployed: supported new Codex procedures, browser/Studio runs, the design
executor and interactive tasks use protected API runners. Native model calls and supported
supplier operations also record and settle usage. Studio includes its fal voice/transcription
costs, not just model tokens. Historical runs retain their pinned API or OAuth contract;
resuming old work does not silently change authentication or pricing.

SEC-04 now removes pooled OAuth support entirely. The broker, login cache handling,
refresh write-back and deployment login requirement are gone. New compute requires
protected API execution; saved artifacts and reviews retain their contracts. See [API-only execution](oauth-credential-security.md) for the execution contract
and operator verification.

Hosted welcome credits are $10 once per authenticated Tin user, pooled in their first joined
workspace—not per login or project. With hosted defaults enabled, project discovery enables
workflow charging for workspaces with established ownership and creates missing project
limits without overwriting existing limits. Both Start here workflows and their approved
initial setup children remain free; later independent or scheduled runs do not inherit that
exemption. Ordinary starts check a configured conservative cost estimate, fund provider
operations internally, and deduct actual verified usage. Users do not approve quotes or manage
holds. Uncertain usage is not treated as free or automatically purchased again.

Stripe is still **test mode only**. API usage can incur real supplier costs even when Tin's
credit ledger and Checkout use test mode. This is not a live-payment launch. Self-hosted
customer billing defaults off; the operator still pays providers and infrastructure directly.
Four provider adapters exist, but a billed route needs explicit registration and pricing.

See [billing coverage](workflow-billing-coverage.md),
[Studio API and hosted defaults](studio-api-and-hosted-credits.md), and
[self-hosted billing](self-hosted-billing.md).

Public procedures have source support for [reviewed document pairs](reviewed-project-documents.md)
and optional read-only repository evidence. `brand.capture` is registered in source;
hosted acceptance still requires an updated sandbox image and catalog sync.

## Deferred product work

- Backlink planning and backlink-specific outreach are not a completed organic-system loop.
  Existing email campaigns are a separate capability.
- A visual node-graph editor, general skill import, arbitrary workflow runtimes, raw-key SDK
  execution and user-supplied model-key routing are not supported by the private pilot.
- Additional adapters/routes, automatic publishing, broader autonomy and experiment tracking
  need their own implementation and acceptance; they are not implied by workflow registration.

## Source release and self-hosting limits

The public repository starts from a reviewed source snapshot, without inherited private
history, branches or PRs. Contributor guidance, license notices and security reporting
are included. Release checks inspect the exact source tree and built distributions for
credentials, private artifacts and proprietary fonts; this is not an independent security
certification or proof of every live workflow.

Clean-clone self-hosting acceptance remains deferred. Configuration and billing-off paths
exist, but a usable .env.example and a fresh-install workflow run with operator-owned
API access have not been verified. The README is an engineering setup path, not a turnkey
installer. Provider configuration and deployment verification remain the operator's job.
