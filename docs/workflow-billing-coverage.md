# Workflow billing coverage

Current through September 16, 2026. Studio API execution and hosted credit defaults are
deployed and activated; their paid acceptance is recorded in
[Studio API and hosted credits](studio-api-and-hosted-credits.md). Stripe remains test-only.
The September 14/15 sections below describe the underlying accounting and executor rollout.

## Native usage and parent budgets — September 14, 2026

Use the existing supplier-operation and append-only credit ledger services. Native
runs use [configured cost previews and internal funding](workflow-credit-simplification.md),
not mandatory quote approval or upfront whole-run reservations. Old quotes/budgets
retain their semantics. HTTP, MCP, schedules and trusted parent starts share this
admission contract. This accounting change did not itself enroll wallets; the subsequent
hosted-default policy does so. Neither enables live Stripe charging.

### Covered by this change

- Native model workflows: `content.plan`, `style.capture`, `creative.character`,
  `content.answer_page`, `project.memory`, `project.weekly_brief`, `scan.report`,
  `visibility.audit`, `organic.audit`, `organic.keyword_plan` and
  `ads.assessment` (its ceiling is the run's `max_cost_usd` input, default $6,
  operator-gated at $3; DataForSEO calls carry their provider-reported cost and the
  operator-run gak Keyword Planner service reports $0), `ads.launch` (ceiling
  `max_cost_usd`, default $4) and `ads.monitor` (default $2); both are model steps
  only, since the Google Ads API reports no cost and its calls are receipted at $0.
- OpenAI Responses search and DataForSEO crawl/keyword task costs, using trusted
  supplier responses, including usage recorded before content validation fails.
- `organic.traffic_system`: one root spending ceiling;
  child operations reserve per call and are charged once at root settlement. There is
  no extra orchestration fee. The weekly `content.generate` schedule it saves is outside
  that ceiling: each occurrence is an ordinary scheduled run with its own funding.
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
- `outreach.awesome_submit` is included the same way: it buys no model or provider work and
  opens the approved pull requests or issues through the founder's connected GitHub account,
  so it has no quote or reservation and reports a $0 Tin charge (`tin-connected-github-v1`).

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

`service_pricing.py` pins `tin-native-supplier-2026-09-22-v1` in each new budget.
Its currently used routes are OpenAI GPT-6 Luna and GPT-6 Sol, standard tier.
The card distinguishes uncached input, cached reads, cache writes, output, long
context and web search. Pricing sources:

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [DataForSEO task response cost](https://docs.dataforseo.com/v3/on_page/task_post/)

Provider adapters remain independent from pricing. A new Anthropic, Gemini or
OpenRouter route needs a verified explicit rate card before a billed request can
be dispatched; keys or model prefixes never choose a provider or a price.

The normal native maximum is $2, the organic audit maximum is $2 (it was $5 until
September 29, 2026; see below), and keyword planning uses its configured total research
ceiling. These are conservative estimates
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

## Ceilings sized to measured cost — September 29, 2026

Production runs cost a small part of their ceilings: an organic traffic system run used $0.73
(audit, keyword plan and a content plan that failed) against $31, and a `content.generate` run
was charged $0.97 against $5. Ceilings now sit near three times typical cost, and never below
the worst case one run can reach, so a normal run is never refused. Charges stay actual usage.

| Workflow | Before | After | Why |
| --- | --- | --- | --- |
| `organic.keyword_plan` (new runs) | $10 default, $5 floor | $2 default and floor | Keyword policy v6 reserves at most $1.65 for a full run |
| `organic.audit` | $5 | $2 | Every call at every bound at once costs $1.83 (v10) |
| `organic.traffic_system` | $26 ($31 with a technical fix) | $15 ($20); $10 draft-only | $2 keywords + $2 audit + $1 content plan + $5 draft + $5 PR adaptation |
| `content.generate` | $5 | $5, unchanged | No code-level bound below $5 (see below) |

Keyword policy v6 (`keyword_plan_v6.py`) changes only reservations and the floor; v5 and older
runs keep theirs. From list prices checked September 29, 2026:

- DataForSEO Labs: $0.012 per task plus $0.00012 per returned item. The largest lookup returns
  200 rows, $0.036. Reserve $0.05 (was $0.10).
- Live organic SERP: $0.002 per page of ten results. Reserve $0.005 (was $0.03).
- GPT-6 Luna model steps, at the pinned card's long-context rates ($0.25 per million input
  tokens at the cache-write rate, $0.75 per million output tokens), counting one token per byte
  of the 300,000-byte input cap plus instructions and schema (about 325,000 tokens, $0.081):
  seeds $0.083, screening $0.085, review $0.099. Reserve $0.10, $0.10 and $0.15 (were $0.50,
  $0.50 and $4.00).
- A full run: three model calls ($0.35), 22 lookups ($1.10) and 40 samples ($0.20) reserve
  $1.65, under the $2 floor. At list prices a run whose every lookup returns its full row limit
  costs about $0.95, even with the model calls at their bounds. The measured $0.73 covered the
  audit too, so a typical keyword run costs less than that; $2 is roughly three times it.

Audit policy v10 makes at most 28 searched and 44 unsearched calls plus one crawl (v9 made 52
unsearched: it could interpret twelve questions per panel attempt, where v10 keeps eight). With
every input at its 60,000-byte cap (one token per byte, plus 16,384 tokens of results per
search), 6,000 output tokens and three $0.01 searches per searched call, a v10 run costs $1.83
($1.92 under v9). The traffic system's
content plan share is $1: its one call is under $0.10 at long-context rates. Standalone
`content.plan` runs keep the $2 native maximum. These composition changes apply to every
traffic definition; a saved configuration keeps its own keyword limit (for example $9 gives
$22). Weekly `content.generate` occurrences stay outside the parent's ceiling.

`content.generate` keeps $5. Its session contract bounds a job by spend and sandbox time, not by
tokens or requests. One maximal GPT-6 Sol response (a 1,050,000-token context and 128,000 output
tokens) costs $2.34 at list price with the whole context cached and $6.12 without, so no
code-level bound sits below $5, and one measured run is not enough to show that a lower ceiling
would never stop a normal draft.

## Weekly articles and the default limits — September 29, 2026

Hosted projects start with $10 per run, $10 a month and $10 per scheduled run (migration 047).
These defaults are unchanged. Admission counts a charged run at what it cost and a run still
going at its full maximum. A scheduled run starts only if its maximum fits the per-run and
scheduled-run limits and this month's charges plus its maximum fit the monthly limit.

A weekly `content.generate` occurrence carries a $5 maximum, and its configured estimate is the
same $5. At the measured $0.97 a draft, five Tuesday drafts fit: the fifth needs $3.88 + $5.00.
Every other charge in the month counts against the same $10. When each approved draft also opens
a PR adaptation costing about as much, charges pass $5 during the third week and later drafts
that month do not start. A Start here traffic run is included and not counted; one started
directly is estimated at $15 and needs a higher per-run limit ($10 when drafts stay in Tin).

The Start here handoff now says this. When the report is written, Tin reads the project's
limits, every active saved schedule's maximum as admission prices it, and the weekly articles
an organic traffic system started by this setup will save. If a schedule's maximum exceeds the
per-run or scheduled-run limit, or the schedules' runs in a month at their estimates exceed the
monthly limit, the onboarding result's `relay` gains one line that names the schedule, the
limit and `set_project_spending_limits`. With the defaults and one weekday of articles:

> Spending limit: Weekly article — https://example.com/ can run up to 5 times a month at up to
> $5.00 a run, up to $25.00 a month, above this project's $10.00 monthly limit, so some runs may
> not start. To keep every run, raise the monthly limit with set_project_spending_limits or on
> the Billing page.

Tin starts a run only if this month's charges plus that run's maximum fit the limit, and a run
still going counts at its maximum.

The words are saved with the setup, so a retried report reads the same. Projects without
billing, or with nothing billed on a schedule, get no line.

