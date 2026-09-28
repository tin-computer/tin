# Codex API execution pilot

## ELI5

Supported Codex runs use Tin's server-held OpenAI API key when API execution is enabled.
Codex still does the work in its existing sandbox. Tin checks a short-lived run permission
before each API request and records the usage OpenAI returns. The API key never enters the
sandbox. Hosted defaults now enable this for new supported procedures, browser/Studio runs,
`content.design_md` and `project.task`, and deduct verified usage from test credits.
Ordinary starts do not require quote approval. Self-host customer billing defaults off;
operators still pay suppliers. Live Stripe payments stay off. See the current
[coverage](workflow-billing-coverage.md) and [hosted-default acceptance](studio-api-and-hosted-credits.md).

## Scope and configuration

`TIN_LITE_CODEX_API_PROJECTS` is an operator-only comma-separated UUID allowlist, empty by
default. It selects the API path for procedures whose pinned sandbox profile is
`default`, `isolated`, `browser` or `studio`, and for `content.design_md` and `project.task`.
`TIN_LITE_BILLING_HOSTED_DEFAULTS_ENABLED` also enables that API path without project-by-project
API enrollment. It does not open private execution, which keeps its own gate: the project
allowlist or `TIN_LITE_PRIVATE_WORKFLOWS_OPEN`.
Default procedures and tasks use the isolated runtime image, without changing
their declared workspace, result, verification, or review contract. It requires
`TIN_LITE_LUNA_API_KEY` and an API-capable isolated image. No workflow
inputs, version chooser, new saved configuration, or new executor are added.

Studio's protected API runner and fal accounting are described in
[Studio API and hosted credits](studio-api-and-hosted-credits.md), including its
acceptance/rollout status. Native model-service routing is unchanged. The
switchboard still needs its legacy broker for historical OAuth runs.
The existing project-scoped Temporal execution gate and trusted activity lane are unchanged.

Procedure retry recovery is independent of the funding policy: it never purchases a second
attempt. Trusted budget-stop reasons survive cleanup, completed revisions replay, and eligible
interrupted Markdown can remain available as an explicitly incomplete, read-only result.
See [Interrupted Codex procedures](procedure-publication-recovery.md#interrupted-codex-procedures).
Session funding reduces the old per-request headroom failures; it does not imply every stopped
session has a finished artifact. Historical and included/child funding pins remain unchanged.

Browser API runs use `TIN_LITE_E2B_BROWSER_API_TEMPLATE` (default
`tin-lite-codex-browser-api`), built from the isolated controller plus the existing
Camoufox/WARP layer. Browser/MCP tools execute in the controller; shell and repository
code execute as the distinct credential-free author UID. Browser egress remains open,
while the reusable OpenAI key stays on the switchboard. Historical OAuth browser runs
keep the old browser template. No private-workflow browser permission is added.

Tasks pin a `task_codex_auth` receipt and use a separate API attempt/grant for each
`task_turn` identifier, under the existing run-wide token and credit limits. Every
turn starts from checkpoint files and bounded transcript, as before. Pause, stop,
questions, steering and exact-diff review remain task controls, not a new procedure
conversation. Pausing or asking a question does not settle the run prematurely.
A completed turn's redacted result can replay after a projection failure without
another model call. Projection and turn completion are atomic. Old task receipts
without an API budget/auth pin retain OAuth rather than changing authentication on resume.
A task paused before its first compute turn pins its contract at that pause, and a new
turn's failed preflight cannot be misread as an old receipt.

The design executor keeps its stable identity, saved versions and recorded Temporal
commands. Its sandbox-create activity pins the shared API controller's DESIGN.md-only
instructions, authentication contract and timeout, then its persistence activity
uses the existing protected procedure runner. Canonical publication and the primary
artifact remain unchanged. API supplier usage uses the same per-call funding and
root settlement as procedures. Existing design create receipts without an auth
contract remain OAuth; a new preflight failure cannot be misread as such a receipt.
This design-executor change alone did not migrate other profiles or enroll workspaces;
the later browser/task and Studio/hosted-default rollouts are documented separately above.

The execution contract is saved in the sandbox-create receipt before compute and retained
through retries, including create failure. Pre-existing receipts without it remain OAuth.
Changing API enablement affects newly selected runs, not an already pinned execution.
Stopping a run or replacing its sandbox/fencing tuple prevents new relay admissions.

## Transport and usage

The protected isolated controller selects a custom Responses provider in Codex 0.156.1:
`requires_openai_auth=false`, WebSockets disabled, both request and stream retries zero.
An environment-backed `X-Tin-Codex-Grant` reaches only the controller; user tools run under
the existing separate credential-free UID. No `auth.json` is fetched and no broker grant
is issued for an API attempt. The provider key remains on the switchboard.

The relay fixes the upstream to OpenAI and permits only POST `responses` and
`responses/compact`. The original v1 pilot pins `gpt-6-sol`, default service tier, at most eight
requests, 256 KiB request bodies, 4,096 output tokens per ordinary response, one hosted
tool call per response, and stops further requests after 100,000 observed cumulative
tokens. These are request/observed-token bounds, **not a guaranteed dollar ceiling**.
Missing usage remains unknown. Compaction has request/body bounds, not an output-token
parameter unsupported by its protocol. Provider-hosted files, stored response references,
background execution, remote tools and arbitrary upstream routing are not supported.

When a stop happens, the attempt names its cause. The relay records budget, request and
token stops. The controller's observed-token stop is recorded as `token_limit` from its
own usage frame. The relay logs each rejection with run, operation, status and reason
code (a 422 contract rejection names `operation_not_allowed`, `request_too_large` or
`contract_mismatch`), and only the status of an upstream rejection. It never logs request or provider
bodies. The controller also prints each agent narration line (not the structured result)
as a bounded `TIN_CODEX_PROGRESS` frame. The switchboard redacts run secrets and projects
the latest line to the run's `progress_summary` in Postgres, unless product code owns the
run's progress steps. It never enters Temporal. Payment-card runs skip it, as they skip
rollout capture. After a failure it shows as the last update before the stop. Images
built before this frame existed simply report no narration.

Historical default-profile procedures selected **`tin-codex-api-v3`**: 64 requests, 1 MiB request
bodies, 8,192 output tokens per response, 128,000 configured context tokens, automatic
compaction at 96,000, and a stop after 2,000,000 observed cumulative tokens. Both server
and controller use the pinned contract. These are bounded operating limits, not a promise
that every procedure can finish within them or its credit ceiling. Isolated procedures
admitted without a session budget (included onboarding children, child budgets and
qualification) also select v3 for new admissions. Existing isolated v1 quotes, pinned
budgets and receipts retain their original limits; changing flags cannot upgrade them.
V3 keeps v2's context/usage limits but omits `max_tool_calls`, allowing the model to
search, open pages and follow up within one response. Already admitted v1/v2 runs
retain their one-call ceiling. Run timeouts and credit reservations still apply;
this is not unlimited customer liability or unlimited supplier spending protection.

New, customer-funded ordinary procedures (`default`, `isolated` and `browser`) pin
**`tin-codex-api-v4`** and `funding=procedure_session_v1`. Their Responses requests use
GPT-6 Sol's supported 128,000 output-token maximum and 1,050,000-token context. The
controller compacts at 922,000 context tokens, leaving room for one maximum response.
An explicit smaller output limit remains valid. Output tokens include reasoning; these
are per-response/context limits, not a cumulative session allowance. Requests remain
bounded to 8 MiB; artifact, tool, sandbox isolation and timeout contracts still apply.
There is no separate 64-request or lifetime-token stop for these sessions.
Browser procedures joined on 2026-09-24: a signup walkthrough spends one model turn per
page action, and the 64-request v3 ceiling stopped complete walkthroughs before they
wrote a report. Their open-egress `browser_api` image and 1800-second timeout are
unchanged; runs admitted earlier keep v3.
[Model limits](https://developers.openai.com/api/docs/models/gpt-6-sol).

Every contract named `gpt-6-astra` until 2026-09-23. Runs admitted before then keep
that pin; new runs use `gpt-6-sol`.

Tin authorizes the existing $5 session maximum once, internally holding those credits
until settlement. Each request checks the run grant, project membership, current spending
policy and remaining budget, and records a durable intent. It does not predict prompt
cost, repeatedly price prior receipts or fund the shared wallet again. One request may
be outstanding; missing or unpriceable usage blocks continuation. Verified response
usage updates the existing operation and run total. Settlement charges once, rounds
once, releases unused authority and preserves existing uncertain-usage reconciliation.
Compaction is an ordinary paid response in this same session.

The $5 maximum is a **hard customer-charge ceiling and a soft supplier-spend stop**.
A response admitted before the stop may exceed the remaining budget; Tin records and
absorbs the excess, then refuses further calls. This exposure can be material: 128,000
output tokens alone cost $6.40 at the pinned standard rate, or $9.60 in its long-context
band, before input/search charges. There is no claim of an absolute supplier invoice
cap. An interruption cannot undo already-accepted provider work. Changing this tradeoff
requires an explicit provider/operating policy, not an invented precise token estimate.

Only newly admitted ordinary root procedures select v4. Existing budgets and valid
quotes retain their auth, model, price and runtime pins. Included onboarding and its
children, other parent children, Studio, diagrams/video, design tasks, interactive
tasks and managed model steps keep their existing contracts. No data migration or
Temporal command change is required. Before deploying the switchboard, build and verify
an isolated image with `codex_api_config.py --check-v4` and the opt-in `session_context`
isolation probe. Historical readiness checks remain supported. Roll back by stopping
new admissions and retaining a v4-capable worker for admitted v4 runs; do not rewrite
those runs' terms or resume them with an older controller.

Codex 0.156.1's custom provider performs context compaction using an ordinary Responses
model call. That call therefore has the same reservation, actual supplier model/tier/usage,
and settlement as its other model steps. No second summarizer is added. The separate remote
`/responses/compact` endpoint remains excluded from billed execution: it has no requested
output ceiling and does not echo the supplier model/tier. We do not invent those facts to
price it. The opt-in image test forces the real CLI through compaction and continuation.
[Codex context configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[OpenAI compaction](https://developers.openai.com/api/docs/guides/compaction).

Repository-authored verification commands still run as the credential-free tool user.
Only `organic.technical_fix`'s two fixed Tin-owned offline metadata/title verifiers run
as the controller, **after all author processes stop and the checkout is frozen**. They
receive only context-location environment values, not grants, and read the protected
baseline/result. Arbitrary commands and private workflows cannot opt into that path.
The gateway independently validates and delivers any GitHub proposal as before.

Production acceptance exposed two boundary mismatches. First, this CLI omits hosted web
search from its custom-provider tool list. The v2 relay explicitly supplies OpenAI's native
Responses `web_search` tool, preserving the one-call bound and observed usage/price. It does
not enable experimental standalone search, add a second model agent, or grant shell egress.
V2 removes the CLI's internal Responses Lite header, which otherwise rejects hosted search.
The standard Responses endpoint accepts the same context and `additional_tools` items;
their declarations are validated against the same priced-tool allowlist, not translated.
V1 retains its existing protocol negotiation.

All API contracts accept Codex's namespaces of local function/custom tools, including
run-bound MCP gateway tools declared in `additional_tools`. Every member is checked;
remote MCP, hosted execution and nested namespaces remain rejected. This transport
compatibility does not upgrade v1 limits, pricing, authentication or existing run pins.

The v3 relay also requests `web_search_call.results`. A direct provider probe proved
that merely replaying a completed `web_search_call`, even with its returned `results`,
does not carry the source text into a stateless follow-up. The first model step could
read documentation while the next step truthfully reported no visible source content.
The relay now delivers the returned text as a named `function_call_output` with no
`call_id`: the pinned CLI's existing external-tool-observation shape. A second direct
API probe recovered the exact endpoint/auth/body facts from that observation after a
local checkpoint, without another search. No assistant claim or executable call is
manufactured. This is a transport bug fix within v3, not a new workflow or model route.

Source text remains untrusted tool data, with URLs/citation markers and explicit missing
or truncated status. Retention bounds are 48 KiB of source text per web action and
192 KiB per response; small identity/status wrappers are additional. Completion-only
and late-result streams are handled without duplicate observations. Evidence lives in
the ordinary bounded controller history (and its existing operator-only redacted
rollout), not a new database, cache or shared provider conversation. Compaction receives
the evidence through that same history. Original provider usage is settled before the
terminal event is forwarded; source text never enters usage receipts. V1/v2 behavior,
reservations, model, template, prompts, review and publication rules are unchanged.
Regression coverage uses the real pinned CLI across two local tool steps and forced
compaction, plus missing/truncated/late results, UTF-8 bounds, historical contracts,
usage-only receipts and duplicate settlement. Live article acceptance remains a
separate check; documentation retrieval is not proof of a successful real message.

Second, `content.generate`'s selected brief/frontmatter was only present in the controller's
private context file. The bridge now includes the bounded `content_draft` data in the model
prompt explicitly, never the full credential-bearing controller context. Validation remains
strict; no guessed provenance is added after generation.
[OpenAI hosted web search](https://developers.openai.com/api/docs/guides/tools-web-search),
[Codex custom-provider search](https://learn.chatgpt.com/docs/web-search).

`codex_api_attempt_v1` records a hash of the run-bound grant and its fencing/expiry facts.
`codex_api_usage_v1` records dispatch intent before each request, then actual response
model, service tier, request/response IDs and normalized available usage before forwarding
the final response event. It stores no prompt, output, API key or plaintext grant. Existing
operator-only rollout capture remains separately redacted; it is not a usage receipt.

Codex's `x-client-request-id` is a thread ID, not a per-request key. Duplicate detection
binds the actual turn/window metadata to the exact outgoing body and endpoint. Same-turn
identical attempts reject rather than purchase again; it is not a replayable response cache.
An unknown prior request blocks later dispatch in that attempt. A retried activity first
recovers an existing durable artifact; without one it cannot launch a second API attempt.
An interrupted stream is not treated as zero usage or silently rerun through OAuth.

Admission and observation use short Postgres transactions, not connections held throughout
model streaming. A regression test holds all four outer activity locks while the relay
admits and completes. Grant admissions validate the active run, project, sandbox, thread,
lease owner, generation, fence, expiry, and initiating member's continuing access.

HTTP/MCP `get_run_usage` reports the new `codex_openai_api` category separately from native
API calls and OAuth thread counters. API attempts do not also record an OAuth usage receipt.
API receipts with pinned pricing expose a separate `api_list_price` breakdown; historical
unpriced receipts remain unpriced. This is public-list-price accounting, not a supplier invoice.
Credit-enrolled workspaces require an API-priced budget during selection and relay admission;
new starts use configured estimates without quote approval. They cannot use the fictional
OAuth tariff or bypass an existing billing account.

## Test-credit settlement

The immutable `openai-codex-standard-2026-09-23-v1` card names OpenAI, `gpt-6-sol`,
default service tier, cache-write rates, long-context rates, and hosted search charges.
Rates are from the [model page](https://developers.openai.com/api/docs/models/gpt-6-sol),
[caching contract](https://developers.openai.com/api/docs/guides/prompt-caching), and
[tool pricing](https://developers.openai.com/api/docs/pricing). Per million tokens: ordinary
input $2, cached input $0.20, cache writes $2.50, output $10. Above 272,000 input tokens,
all input/cache rates double and output is 1.5x for that response. Search is $0.01/call,
plus its model tokens. Ordinary input is input minus cache hits minus cache writes.
Never modify this card in place or reinterpret old receipts when adding another rate card.
The earlier `openai-codex-standard-2026-09-12-v1` card (`gpt-6-astra`: $10 / $1 / $12.50 /
$50) stays in `HISTORICAL_RATE_CARDS` so runs that pinned it still settle at their rates.

The original quote/run admission pinned those rates and the API auth path atomically with a
$5 reservation. New runs instead use [per-call funding](workflow-credit-simplification.md):
the configured run ceiling is checked at admission but not reserved in full. Historical
whole-run budgets keep their original semantics. The following per-response envelopes remain
internal accounting bounds, not an extra user-facing hold or fixed charge.
Each v1 response reserves $1.4648: the full 100,000-token operating input envelope at the
most expensive standard input rate, 4,096 output tokens and one search call. A v2/v3 response
reserves $2.0196 using its 128,000-token envelope and 8,192 output tokens. In v3 the
search allowance is reservation headroom, not a one-search execution cap: all completed
search actions are priced; opening/finding pages is observed separately without another
search-call fee (model tokens still apply). Incomplete tool observations remain unknown.
The existing overage policy absorbs any excess and stops further
purchases. There is no
flat fee, sandbox tariff or markup. Unused request allowances are released into the run's
budget after observation; insufficient remaining allowance prevents another purchase.

This remains bounded execution, not an unlimited long-running offer. Paid v1 requests
retain their 100,000-byte body bound; v2 uses its 1 MiB bound. Remote compaction is rejected
before purchase; ordinary Responses-based Codex compaction is supported. Long-context rates
still price an unexpectedly large supplier response correctly. Hidden search/opaque context
can exceed a local envelope: Tin absorbs anything beyond the request reservation, and no
further call follows an overage or unresolved usage. This guarantees the **customer** ceiling,
not an absolute supplier expense ceiling. Later browser/task and Studio acceptance is recorded
in [billing coverage](workflow-billing-coverage.md); it is separate from this initial procedure proof.

## Settlement and compatibility

Metadata-only usage persists before billing observation and terminal stream delivery.
Settlement repairs an interrupted billing projection from that receipt without repurchasing.
Missing cache counters, mismatched models/tiers, inconsistent totals or missing usage remain
unpriced and block further paid requests. Existing 24-hour reconciliation absorbs unresolved
cost; it does not fabricate zero-token usage. Valid usage is billable even when generation
or later artifact validation fails. Settlement occurs after paid work stops, including review
waits, rounds half-up **once per root run**, and posts one idempotent ledger charge.

Migration `032_codex_api_billing.sql` adds only the `codex_api` operation kind. There is no
new table, executor, UI control, workflow version, saved card or provider credential path.
OAuth budgets keep the original tariff/auth path, even after allowlisting; admitted API
budgets remain API after removing the flag. Native model pricing is unchanged.

## Verification and rollout

Local tests cover authorization and fencing, duplicate/concurrent requests, invalid or lost
streams, trusted usage before completion, compaction, missing usage, redaction and billing.
Real-image checks separately exercise protected controller/configuration, credential isolation,
artifact validation, repository verification, token stops and cleanup with synthetic responses.
These are not substitutes for a separately authorized live provider run.

Build and verify the selected image before enabling new API execution. Rollback changes
selection for future runs only: keep the relay available for pinned active API runs until
they finish or are explicitly stopped. Never reinterpret their authentication as OAuth.
Use [current profile coverage](workflow-billing-coverage.md) and
[hosted defaults](studio-api-and-hosted-credits.md), not a historical pilot allowlist.
