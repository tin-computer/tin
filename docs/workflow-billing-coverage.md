# Workflow billing coverage

Current through September 16, 2026. Studio API execution and hosted credit defaults are
deployed and activated; their paid acceptance is recorded in
[Studio API and hosted credits](studio-api-and-hosted-credits.md). Stripe remains test-only.
The September 14/15 sections below describe the underlying accounting and executor rollout.

## Native usage and parent budgets — September 14, 2026

Use the existing supplier-operation and append-only credit ledger services. New
runs use [configured estimates and internal per-call funding](workflow-credit-simplification.md),
not mandatory quote approval or upfront whole-run reservations. Old quotes/budgets
retain their semantics. HTTP, MCP, schedules and trusted parent starts share this
admission contract. This accounting change did not itself enroll wallets; the subsequent
hosted-default policy does so. Neither enables live Stripe charging.

### Covered by this change

- Native model workflows: `content.plan`, `style.capture`, `creative.character`,
  `content.answer_page`, `project.memory`, `project.weekly_brief`, `scan.report`,
  `visibility.audit`, `organic.audit`, and `organic.keyword_plan`.
- OpenAI Responses search and DataForSEO crawl/keyword task costs, using trusted
  supplier responses, including usage recorded before content validation fails.
- `organic.traffic_system`: one root spending ceiling;
  child operations reserve per call and are charged once at root settlement. There is
  no extra orchestration fee.
- Both Start here entries, `growth.onboarding` and `growth.onboarding_plan`, are
  free for now. Their approved initial setup children inherit a trusted included
  receipt, not a customer credit budget. No paid quote, reservation or ledger
  charge is created, including at a $0 balance. Model and tool usage still has
  internal receipts (the plan is a native LLM flow of about twenty-five metered model
  steps; started outside the included path it would carry an $8 ceiling); existing execution bounds and project access still apply.
  Later scheduled occurrences or independently started workflows use normal
  billing, and do not inherit free status from the saved configuration.
- `outreach.email_campaign` has no Tin model or per-send charge: it sends the
  supplied messages through the user's connected mailbox. It needs no monetary
  quote, creates no reservation, and reports a $0 Tin charge. The integration's
  send limits and approval requirements are unchanged.

The existing API-priced default/isolated procedures remain covered in API-enabled
projects, including individual content generation, revisions and GitHub delivery.

`content.design_md` now uses the same protected API procedure runner in API-enabled
projects. It keeps its existing native executor identity, saved configurations,
Temporal activity sequence, DESIGN.md-only result and canonical commit path.
Authentication, controller instructions and timeout are pinned before compute.
An existing sandbox receipt without an authentication contract stays on OAuth.
API runs receive no pooled login or provider key; observed Responses usage settles
through the existing credit ledger, with no extra execution/sandbox fee. A lost
attempt can recover its checkpoint but cannot purchase the model work again.

### Prices and ceilings

`service_pricing.py` pins `tin-native-supplier-2026-09-14-v1` in each new budget.
Its currently used routes are OpenAI GPT-5.6 Luna and GPT-6 Astra, standard tier.
The card distinguishes uncached input, cached reads, cache writes, output, long
context and web search. Pricing sources:

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [DataForSEO task response cost](https://docs.dataforseo.com/v3/on_page/task_post/)

Provider adapters remain independent from pricing. A new Anthropic, Gemini or
OpenRouter route needs a verified explicit rate card before a billed request can
be dispatched; keys or model prefixes never choose a provider or a price.

The normal native maximum is $2, the organic audit maximum is $5, and keyword
planning uses its configured total research ceiling. These are conservative estimates
and ceilings, not fixed charges. The organic parent ceiling composes its selected stages:
audit, keyword research, planning, optional technical fix, and, for the current content
continuation, drafting and optional delivery. The pinned definition and inputs determine
the total in `service_pricing.py`; there is no separate orchestration charge. Project
limits and the available balance must
cover the configured estimate; neither is raised automatically. No whole-run
amount is removed from available credits at admission.

Model requests reserve a conservative input/output/cache-write/search envelope
before dispatch. Tool requests use the existing trusted workflow's per-step
ceiling. Actual DataForSEO costs come from the response, not a row-count guess.
Missing or inconsistent usage remains unknown; it is never priced as zero. An
unconfirmed purchase is not repeated automatically. Supplier overages cannot
increase the user's authorization. Round to cents once for the whole root run.
No execution fee or sandbox markup is added by the native price card.

A completed answer can include an ignored over-limit search attempt. Charge its
verified model tokens and confirmed search actions; Tin absorbs unpriced search
fees. Old all-or-nothing search observations receive the same customer-favorable
fee waiver without rewriting their evidence. This does not turn missing model or
DataForSEO usage into a guessed zero cost.

### Review, retries and history

- Completed receipt recovery repairs a missing billing projection without making
  another provider request. A retry cannot create another charge.
- Ordinary content drafts can settle while awaiting review. Paid parent workflows
  retain only incurred and unresolved-call liability until their children finish or stop.
  Free onboarding retains
  its included receipt across review, not a credit reservation.
- Onboarding setup children must match the exact approved option, inputs and
  stable action ID. Fixed organic child edges keep their stable step IDs.
- Recurring schedules fund each occurrence through the existing standing project
  spending limit, not a single reservation for the entire six-month content plan.
- Historical `tin-test-only-v1` quotes/budgets and existing Codex API price cards
  retain their original meaning. No old usage is retroactively charged.
- This adds no tables or Temporal commands. Activities contain all billing I/O;
  workflow histories still contain identifiers, not prompts or financial payloads.

### Browser and interactive executor coverage

Browser procedures (`product.deep_dive`, `qa.product_audit`, `qa.signup_walkthrough`)
now select a separate protected browser API image in API-enabled projects. The
pinned public profile remains `browser`, including its Camoufox tools, open browser
egress, existing identity/integration prerequisites and output contract. Only the
runtime image becomes `browser_api`; this is not a new user-selectable profile.

`project.task` also supports API billing. Authentication is pinned once; each
turn gets a distinct expiring grant and attempted-call receipt. Old-turn grants
cannot purchase requests in a later turn. Completed sanitized turn results replay
without new compute, while ambiguous attempts are not repurchased. Transcript and
turn projection commit with their turn receipt. Questions, review and pause retain
the root budget; terminal completion/stop/failure settles one actual-usage charge.
There is no fresh $5 charge or budget for each answer. Exact-diff approval remains.
Feedback arriving after a recovered checkpoint stays pending for the next turn;
recovery acknowledges only the messages the original turn actually received.

Deployment must select a rebuilt `TIN_LITE_E2B_ISOLATED_TEMPLATE` with
`run-task --check-api` support, not an older procedure-only isolated alias. The
browser API image is selected independently by `TIN_LITE_E2B_BROWSER_API_TEMPLATE`.
Both readiness probes run before model execution, without falling back to OAuth.

### Studio and hosted defaults — September 16, 2026

Studio/video (`creative.product_demo`) now uses the protected `studio_api` runner for
API-enabled runs. Its budget includes OpenAI usage and supplier-reported fal synthesis and
transcription costs. Paid narrated-video acceptance and hosted-default activation are recorded
in [Studio API and hosted credits](studio-api-and-hosted-credits.md). Existing OAuth runs keep
their original contract.

Hosted defaults enable charging on eligible workspaces during authenticated project discovery,
grant the once-per-person welcome credits, and create missing limits without overwriting
existing policies. Self-host billing remains optional and off by default. Live Stripe mode
remains unsupported; the defaults are not a live-payment launch. See
[self-hosted and hosted configuration](self-hosted-billing.md).
