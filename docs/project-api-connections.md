# Project API connections

Code workflows and bounded Codex procedures use the same project-owned connections and
trusted service gateway. For Stripe and PostHog, use the first-party `payments.stripe` and
`analytics.posthog` connections instead of a custom API: see
[Stripe and PostHog connections](stripe-and-posthog-connections.md). Existing definitions without procedure service bindings keep their
behavior; one-off tasks, review rules and recorded Temporal commands are unchanged.
Private execution remains restricted to the existing explicit pilot projects.

## Secure setup

In **Integrations → Custom API**, choose a connection name, HTTPS origin, authentication
method, project secret name and allowed HTTP methods. Bearer tokens and named API-key
headers are supported. OAuth integrations continue using their existing adapters and
credentials; do not copy those credentials into custom connections.

Paste a key into the password field, or select a local env file and check only the names
this project needs. Parsing happens locally; values are masked, unchecked names are not
submitted, and replacing existing names requires an explicit selection. Values are literal:
no shell evaluation, interpolation, multiline values or project-file upload. Imports support
up to 32 selected names, each at most 8 KB; a project holds at most 128 named secrets.

Saving does **not** call the provider or purchase a test request. The connection says
**saved · not verified** until an actual workflow request succeeds. Rotation clears that
verification. Missing secrets stop execution. Disconnecting a custom connection leaves its
named secret available for deliberate reuse; deleting the secret makes dependent connections
need attention. Secret deletion is available through the authenticated metadata/revision API.

The same service backs the dashboard and local helper:

```bash
uv run python -m tin_lite.secret_import \
  --project PROJECT_UUID --file ./selected.env --name CRM_API_KEY --dry-run

uv run python -m tin_lite.secret_import \
  --project PROJECT_UUID --file ./selected.env --name CRM_API_KEY
```

The helper requests an explicitly supplied Tin OAuth/session token through a hidden prompt;
`--token-file` accepts an explicitly chosen token file. It never searches coding-agent
credential stores. `--replace` permits replacement of selected existing names after a fresh
metadata read; concurrent changes still conflict. Values never appear in output or arguments.
For self-hosts, supply `--url https://your-tin-origin`.

MCP `prepare_project_connection` returns the secure setup link and secret **metadata**.
Secret values have no MCP input/output schema. Agents must never ask the user to paste keys
into their conversation or put env files in project state.

HTTP endpoints under `/api/projects/{project_id}/connections`:

- `GET /secrets`: names, credential revisions, encryption key IDs, update times.
- `PUT /secrets`: atomic selected entries `{name, value, expected_revision}`.
- `DELETE /secrets/{name}/{revision}`: remove that exact revision.
- `PUT /custom.api.<name>`: configuration and expected connection revision; no key value.

Membership in the exact project is required. Workspace administration does not grant access.
Malformed secret requests return fixed diagnostics, without FastAPI's input echo.

## Code contract

Use the same required `integration_requirements` used by registered workflows, and bind each
provider once in `code.services`:

```json
{
  "integration_requirements": [
    {"provider_key": "custom.api.crm", "capabilities": ["http.read"], "required": true}
  ],
  "code": {
    "services": {
      "crm": {"provider_key": "custom.api.crm", "max_calls": 2, "max_response_bytes": 8000}
    }
  }
}
```

This is an excerpt; the complete package is in
[`code_connection_example`](../src/tin_lite/code_connection_example/workflow.json), also
returned by MCP `get_workflow_authoring_guide` as `connection_example_files`.
It fetches three account records, filters them, makes one managed classification call,
validates the IDs and business rule, and publishes `reports/custom/CONNECTED_ACCOUNTS.md`.

```python
response = await ctx.services.request(
    service="crm",
    step="fetch_accounts",
    path="/accounts",
    method="GET",
    params={"limit": 3},
)
accounts = response["data"]["accounts"]
```

Requests accept an origin-relative path, scalar query parameters and an optional JSON body.
No author-supplied destination, headers, auth or redirects are accepted. Tin resolves and pins
public DNS addresses before attaching credentials, preserves the host resolver's address
preference, verifies TLS for the approved hostname,
ignores proxy environment variables, refuses compressed responses and bounds returned JSON.
Credential echoes are withheld. Non-redirect HTTP responses return `{status, data}` so code
can validate business results; an empty body, such as a 204 to a DELETE, returns `data: null`.
Unavailable or ambiguous results stop the attempt and do not silently repeat the external
request. An oversized response is different: it arrived, so it is settled as a named
`max_response_bytes` error for that step, counted against the allowance, and later steps can
still call the service. A body that is not JSON is settled the same way, as an
`invalid_response` error naming the HTTP status, with the body withheld; a 401 or 403 still
marks the connection for attention.

A registered operation (Search Console, Gmail, Calendar, Stripe, PostHog, the managed services)
that the provider answers with an error status is settled the same way, and its message
carries what the provider said: HTTP status, error type and code, and the provider's message,
redacted and cut to 1,500 characters. Code reads them from the `ValueError`'s `code` and
`provider_error`; a procedure's `call_service` and `request_service` errors are JSON with
`code`, `message` and `provider_error`. See
[errors](stripe-and-posthog-connections.md#errors) for the format and the redaction rules.

GET requires `http.read`. POST/PUT/PATCH/DELETE require `http.write` **and** that exact method
on the connection. Where supported, configuring `Idempotency-Key` or `X-Idempotency-Key`
sends a stable Tin-derived operation ID. This does not promise universal exactly-once writes.
No live external-write acceptance is claimed by this slice.

Up to four service bindings and 32 total calls share the package's compute window (at most
900 seconds); Tin waits at most 60 seconds for any one call. Requests are at most 16 KB; each
response is bounded to 1,024–1,000,000 bytes. Sandboxes remain networkless
and credential-free. Only the trusted activity invokes the gateway through the existing
protected E2B controller channel and checks the run, membership, lease and fencing tuple.

### Provider cost estimates

Code workflow setup shows connected-provider charges separately from Tin credits. Search
Console API requests are [free](https://developers.google.com/webmaster-tools/pricing), so
Tin reports that directly. Unknown providers get a short per-provider cost note.

Authors may add `provider_cost` to a `code.services` or `procedure.services` binding:

```json
"provider_cost": {
  "estimated_usd": "0.03",
  "basis": "Three requests at $0.01 each on the provider's standard plan.",
  "pricing_url": "https://provider.example/pricing"
}
```

This example is illustrative, not a real provider price. Use the official pricing reference
and explain per-run volume, plan and other assumptions in `basis` (up to 400 characters).
Omit the field when there is no defensible estimate. Tin labels supplied numbers as creator
estimates; they neither change its credit ledger nor enforce a provider spending limit.
Code workflow setup exposes the same `estimate.external_providers` facts through HTTP and
MCP. Procedure bindings accept the metadata, but procedure cost displays are not yet connected.

### Response size and Search Console rows

`max_response_bytes` is measured on the serialized JSON the step receives, and it is what
bounds result size in practice. A Search Console row costs roughly 110–250 bytes depending on
its dimensions, so a 64000-byte binding holds about 250–550 rows and a 1,000,000-byte binding
about 4,000–9,000, still under the provider's 25000-row maximum. `search_analytics.read` accepts:

- `start_date`, `end_date` (YYYY-MM-DD, at most 366 days apart), `dimensions` (up to three of
  `date`, `query`, `page`, `country`, `device`, `searchAppearance`) and `row_limit` (1–25000).
- `start_row` (0–100000) to read a later page.
- `dimension_filters`: up to five `{"dimension", "operator", "expression"}` items, combined
  with AND. Dimensions are the list above except `date`; operators are `equals`, `notEquals`,
  `contains`, `notContains`, `includingRegex` and `excludingRegex`; expressions are 1–4096
  characters.

Tin gives the adapter the binding's bound. It never asks Google for more rows than could fit,
and keeps Google's leading rows (highest clicks first) that do. When rows were left out, or may
exist beyond the page, the response adds `"truncated": true` and `"next_start_row"`; pass that
value as `start_row` in a new step to continue. Filters are usually the better way to get the
rows that matter within the eight-call allowance.

## Codex procedures

Declare the same bindings under `procedure.services`, alongside required
`integration_requirements`. The protected Codex controller receives two Tin MCP tools:

```text
request_service(service="crm", step="fetch_accounts", path="/accounts", method="GET",
                params={"limit": 3}, body=null)
call_service(service="search", step="read_panel", operation="search_analytics.read",
             arguments={"start_date": "2026-09-01", "end_date": "2026-09-07"})
```

`request_service` returns `{status, data}`. `call_service` uses the registered operation's
response shape. Keep stable step IDs; a completed request replays, a changed request conflicts,
and an uncertain request blocks automatic retries even under a different step. All service
aliases share the existing maximum of 32 requests, with per-alias allowances and bounded
responses. Procedures retain their own declared timeout; a code package's `timeout_seconds`
window does not apply to them.

Services require a fenced `default` or `isolated` procedure profile. Private procedures remain
isolated and on demand. Browser, Studio and test-identity tool combinations are unsupported.
When services are declared, every non-workspace integration dependency needs one binding;
Google reads then use `call_service`, not the legacy Gmail/Calendar tools. GitHub repository
workspace and PR delivery requirements remain separate and cannot grant generic write access.

The server resolves only the pinned definition's aliases. It rechecks membership, run status,
lease, grant expiry, private eligibility and connection permissions before new or cached
results. API keys remain in trusted services; the temporary grant remains in the protected
controller and is not exposed to worker commands. It does not grant every connection in the
project. Migration `040_procedure_services.sql` extends the existing grant table; no new
credential store or workflow engine is added. Apply it before deploying this code.

### PostHog example

The [PostHog funnel package](../workflow_packages/example.posthog_funnel/workflow.json) shows
a procedure calling registered operations through `call_service`. It binds the first-party
`analytics.posthog` connection (OAuth, one founder-selected project) and uses
`event_definitions.list`, `property_definitions.list` and `query.hogql`; the operations,
HogQL rules and offline fakes are in [Stripe and PostHog connections](stripe-and-posthog-connections.md).
It is an unregistered authoring example; its real analytics quality and provider behavior
still require a separately authorized evaluation. Copy its folder and key together to
`custom.posthog_funnel` for an operator-enabled private trial, then validate and activate the
exact revision through the ordinary package flow.

Tin's MCP tools expose its HTTP/API gateway to Codex. They do not connect arbitrary remote
MCP servers. No live PostHog, paid model or E2B acceptance is implied by mocked tests. Codex
model charges use existing pricing and settlement; connected-account charges remain separate
and may be unknown.

## Existing adapters and contributions

`ctx.services.call(service=..., step=..., operation=..., arguments={...})` reuses existing
project connections with this explicit reviewed mapping:

| Provider | Operation | Required capability |
| --- | --- | --- |
| `analytics.gsc` | `sites.list` | `sites.list` |
| `analytics.gsc` | `search_analytics.read` | `search_analytics.read` |
| `infra.github` | `repositories.list` | `repositories.list` |
| `workspace.google` | `gmail.messages.search`, `gmail.thread.read` | `gmail.messages.read` |
| `workspace.google` | `calendar.events.list` | `calendar.events.read` |
| `payments.stripe` | `subscriptions.list`, `customers.list`, `invoices.list`, `prices.list`, `charges.list` | `subscriptions.read`, `customers.read`, `invoices.read`, `prices.read`, `charges.read` |
| `analytics.posthog` | `query.hogql`; `event_definitions.list`, `property_definitions.list`; `insights.list` | `query.read`; `definitions.read`; `insights.read` |

Stripe and PostHog arguments, projected fields, paging and errors are documented in
[Stripe and PostHog connections](stripe-and-posthog-connections.md).

Adapter arguments are those of the bounded `IntegrationService` operation; project, run,
account, connection and execution IDs are supplied by Tin. Email sends and GitHub delivery
retain their established workflow-specific contracts. Procedures without service bindings
keep their existing run tools; procedures with bindings use the same mapping through `call_service`.

To contribute a provider adapter:

1. Add its project-owned connection definition and smallest useful capabilities to
   `integrations.py`; credentials remain encrypted on the switchboard.
2. Implement bounded operations with safe diagnostics and explicit result contracts. Keep
   OAuth/account selection and provider-specific semantics inside the adapter.
3. Add a reviewed operation mapping in `code_services.py` and allowed capabilities in
   `workflow_services.py`. Do not dynamically import plugins or dispatch arbitrary method names.
4. Test authorization, revocation, bounded results, failure ambiguity, recovery and usage
   with fixtures before one authorized small live read. Document costs and limitations.

A custom HTTP binding is useful without a registry contribution. It is not automatically
interchangeable with an adapter: authored code must explicitly map the operation and response
shape. Attio and Clay adapters are not included.

## Services Tin holds the key for

Three services need no founder connection: Tin holds the key and pays the vendor. DataForSEO
reads pass their cost through credits; PageSpeed Insights, CrUX and Podscan are free to runs. Declare the provider in `integration_requirements` with its capabilities,
bind it once in `code.services`, and call it with `ctx.services.call`, like any adapter above.

```json
{
  "integration_requirements": [
    {"provider_key": "managed.pagespeed", "capabilities": ["pagespeed.read", "crux.read"],
     "required": true},
    {"provider_key": "managed.dataforseo",
     "capabilities": ["serp.read", "keywords.read", "backlinks.read"], "required": true}
  ],
  "code": {
    "services": {
      "speed": {"provider_key": "managed.pagespeed", "max_calls": 3, "max_response_bytes": 8000},
      "seo": {"provider_key": "managed.dataforseo", "max_calls": 4, "max_response_bytes": 32000}
    }
  }
}
```

```python
lab = await ctx.services.call(
    service="speed",
    step="lab_mobile",
    operation="pagespeed.run",
    arguments={"url": "https://example.com/", "strategy": "mobile"},
)
field = await ctx.services.call(
    service="speed",
    step="field",
    operation="crux.query",
    arguments={"origin": "https://example.com"},
)
serp = await ctx.services.call(
    service="seo",
    step="serp",
    operation="serp.organic",
    arguments={"keyword": "kanban board", "depth": 10},
)
```

| Provider | Operation | Arguments | Returns | Cost |
| --- | --- | --- | --- | --- |
| `managed.pagespeed` | `pagespeed.run` (`pagespeed.read`) | `url`; `strategy` `mobile` (default) or `desktop`; `categories` from `performance` (default), `accessibility`, `best-practices`, `seo` | `status`, `scores` (0-100), `lab` (`lcp_ms`, `cls`, `tbt_ms`, `fcp_ms`, `speed_index_ms`), `field` (`lcp_ms`, `inp_ms`, `cls`, ...) or `field_status: "no_field_data"` | $0 |
| `managed.pagespeed` | `crux.query` (`crux.read`) | one of `origin` or `url`; optional `form_factor` `phone`, `desktop` or `tablet` | `status`, `collection_period`, `metrics` with `p75` and `good`/`needs_improvement`/`poor` shares, or `status: "no_field_data"` | $0 |
| `managed.dataforseo` | `serp.organic` (`serp.read`) | `keyword`; `location_code` (default 2840, US); `language_code` (default `en`); `device`; `depth` 1-100 (default 10) | organic `records` (rank, domain, URL, title, description), `serp_features`, `se_results_count` | $0.002 per 10 results |
| `managed.dataforseo` | `keywords.ideas` (`keywords.read`) | `keywords` (1-20); market as above; `limit` 1-100 (default 20); `offset` | `records` (keyword, search_volume, keyword_difficulty, cpc, competition, intent), `total_count`, `next_offset` | $0.012 + $0.00012 per row |
| `managed.dataforseo` | `keywords.overview` (`keywords.read`) | `keywords` (1-50); market as above | the same records plus 12 `monthly` volumes | $0.012 + $0.00012 per keyword |
| `managed.dataforseo` | `backlinks.summary` (`backlinks.read`) | `target` (a domain such as `example.com`, or an absolute page URL); `include_subdomains` (default true) | rank, backlinks, spam score, referring domains/IPs/pages, broken links | $0.024 + $0.000036 per row |
| `managed.dataforseo` | `backlinks.referring_domains` (`backlinks.read`) | `target`; `include_subdomains`; `limit` 1-100 (default 20); `offset` | `records` (domain, rank, backlinks, spam score, first seen, lost date), highest rank first, `next_offset` | $0.024 + $0.000036 per row |
| `managed.podscan` | `episodes.search` (`podcasts.read`) | `query`; `since`/`before`; `language`; `region`; `has_guests`; `min_audience`; `search_fields` (`transcription`, `title`, `description`); `order_by`; `per_page` 1-50; `page` 1-20 | episode `records` with guests (name, company, occupation), hosts, sponsors, `is_branded`, a `match` snippet and the show's audience numbers; never transcripts | $0 |
| `managed.podscan` | `podcasts.search` (`podcasts.read`) | `query`; `language`; `region`; `has_guests`; `min_audience`; `active_since`; `search_fields`; `order_by`; `per_page`; `page` | show `records`: reach score, audience estimate, Apple and Spotify rating counts, `last_posted` | $0 |
| `managed.podscan` | `podcasts.get`, `podcasts.episodes` (`podcasts.read`) | `podcast_id`; for episodes `per_page`, `page` | one show in full (description, style, website, social links, unverified `listed_email`), or its newest episodes with guests | $0 |
| `managed.podscan` | `people.search`, `people.appearances` (`podcasts.read`) | `query` and `search_fields` (`name`, `company`, `occupation`, `industry`); or `entity_id`, `role`, `since`/`before` | people with company, occupation and appearance counts; or a person's episodes with their shows | $0 |
| `managed.podscan` | `charts.top` (`podcasts.read`) | `platform` `apple` or `spotify`; `country`; `category` slug; `limit` | ranked shows with `podcast_id` | $0 |

Podscan reads are free to runs because Tin pays Podscan a flat subscription; a binding's
`max_calls` is the guard. Podscan's own rate limit comes back as a rate-limit error: wait and
call again under a new step. A timeout or Podscan failure returns `status: "unavailable"` and
an unknown id `status: "not_found"`, both answers rather than errors. Podscan splits one
person across several entity records and its `audience_size` is an estimate, so packages
merge people by company and compare audiences only within a niche; `listed_email` is never a
verified pitch address. Procedures may bind it.

Every DataForSEO response carries `cost_usd`, the cost DataForSEO reported for that call.
Prices are DataForSEO's list prices as of September 2026 (after the July 2026 update); the
reported cost, not this table, is what a run is charged. At the argument bounds every call costs
at most about $0.03.

**How a package pays.** PageSpeed Insights and CrUX are free, so a package that binds only
`managed.pagespeed` keeps the included `bounded-code-v1` policy. A `managed.dataforseo`
binding makes the run metered, the same `managed-code-model-v1` funding that model steps use:
the run needs an enabled credit account, its ceiling is $0.05 per declared call (four calls
cap the run at $0.20), and each call reserves $0.05 before dispatch. The cost DataForSEO
reports then settles it through `service_pricing` and the credit ledger, exactly as native
keyword research does; a supplier overrun above the reservation is Tin's loss. A refused
request (bad arguments, rate limit, Tin's account out of balance) settles at its reported cost,
usually $0, and later steps can still call. A read Tin can't confirm (a server error, or an
oversized, malformed or mismatched answer) stays unconfirmed: billing reconciles it rather than
counting it free, Tin doesn't repeat it, and the run's later service calls stop with a named error. Paid managed services are for `workflow.code`
packages only; a Codex procedure's session budget funds its own model calls, so procedures
may bind `managed.pagespeed` and `managed.podscan` but not `managed.dataforseo`.

**Responses and limits.** Tin cuts each response to what a report needs, never the provider's
raw payload: a Lighthouse report of hundreds of kilobytes comes back as under 1 KB. List operations
return the leading `records` that fit `max_response_bytes`, with `truncated`, `has_more` and,
for the paged operations, `next_offset`; pass it as `offset` in a new step. For `serp.organic`
and `keywords.overview`, ask for less (a smaller `depth`, fewer keywords) instead. Only live
endpoints are exposed; DataForSEO's task_post endpoints, such as the OnPage crawl, stay in
native executors.

**Slow and missing data.** A Lighthouse run takes 10-30 seconds. Tin waits 55 seconds, under
the gateway's 60-second limit, then returns `{"status": "timed_out"}` as a completed result:
the step replays that answer, and a new step can try again. Give each strategy its own step;
size the package's `timeout_seconds` for the runs it makes, since each may wait up to 55 seconds. CrUX has no
record for most small sites. `crux.query` then returns `status: "no_field_data"`, a metric
without enough traffic is `null`, and `pagespeed.run` says `field_status: "no_field_data"`.
Report that as missing; never show it as zero. A page Lighthouse cannot load returns
`status: "lighthouse_error"` with Lighthouse's code, such as `NO_FCP`.

**Operator settings.** `TIN_LITE_PAGESPEED_API_KEY` (a Google Cloud API key with the
PageSpeed Insights API and the Chrome UX Report API enabled) serves both Google operations;
the organic audit already reads the same key. `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`
serve DataForSEO, as for the native keyword and audit workflows. If the Backlinks API is not
enabled on that account, backlinks reads return a named refusal. When a setting is unset, a run that requires the service does not
start, and a call made after the key was removed fails with "... is not configured on this Tin
deployment" and no receipt. Keys go to the provider in a header or Basic auth, never in a URL,
the sandbox, a receipt or the response.

## Recovery and costs

Stable step IDs and fingerprints bind responses in the existing `effect_receipts` table
(`code_service_call_v1`). The first call pins the connection/account/configuration for the
run. Completed responses replay after worker or sandbox loss. A changed request or connection
conflicts. An unresolved attempt blocks new steps too; changing an ID cannot purchase it again.
Credential rotation under the same name retains the binding, while permission checks still
run before cached results. Cancellation stops further calls; it does not recall a provider
write that was already accepted. No execution-state files or second orchestration engine exist.

Connected-provider observations use the existing Postgres usage projection, marked
`connected_api`, `billed_by: connected_provider`, with unknown provider costs left null.
These requests never create Tin credit operations. Managed services are the exception
described above: their observations are Tin's own `tool` usage, `pagespeed` at $0 and
`dataforseo` at its reported cost. Model calls use the existing priced
route, usage recorder and ledger separately. Model-free bounded compute still works at zero
Tin credits; an external account may have its own provider charges.

## Encryption and operations

Migration `036_project_secrets.sql` adds one small project secret table and extends the
existing connection provider constraint. Secret values use the existing AES-GCM cipher and
`TIN_LITE_INTEGRATION_CREDENTIAL_KEY`, with project/name/credential-revision associated data.
The credential revision is a UUID; the encryption key ID is a separate nonsecret fingerprint.
Provider keys never enter workflow source, sandbox environments, Temporal history or logs.

Back up encrypted database state **and** the encryption key. Restoring only one cannot recover
values. A missing/wrong key fails closed. `rewrap_project_secrets` is an operator-only primitive
for drained setup/execution: back up both keys, rewrap in a transaction, switch the configured
key, verify, then retire the old key. It preserves credential revisions and bindings. The
reverse operation supports recovery. Existing OAuth/test-identity credentials also use this
key and require their established re-encryption process; this helper alone is not a global
key rotation.

Apply the additive migration before deploying. Rolling the runtime back retains the table,
connections and receipts; no destructive schema rollback is necessary. Keep the private-project
allowlist unchanged. The outer Temporal workflow and existing history commands do not change.

## Verification

- Local fixture tests cover encrypted storage, selected import, membership, revision conflicts,
  rotation, request origin/header/path bounds, private addresses, redirects, credential echoes,
  payload limits, unknown attempts, capability limits and external-cost separation.
- Real Postgres saturation tests leave one pool slot available and exercise competing setup
  updates, custom service replay/new calls, and the existing Google adapter's refresh, receipts
  and authorization-failure path. These operations reuse the held connection and do not wait
  for another pool slot. Supplier HTTP responses are fixtures in these tests.
- Real Chromium verifies the packaged setup form with synthetic API/auth responses, in light
  and dark themes and at phone width. The in-app browser was unavailable in this session.
