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

GET requires `http.read`. POST/PUT/PATCH/DELETE require `http.write` **and** that exact method
on the connection. Where supported, configuring `Idempotency-Key` or `X-Idempotency-Key`
sends a stable Tin-derived operation ID. This does not promise universal exactly-once writes.
No live external-write acceptance is claimed by this slice.

Up to four service bindings and eight total calls share the existing 60-second compute window.
Requests are at most 16 KB; each response is bounded to 1–64 KB. Sandboxes remain networkless
and credential-free. Only the trusted activity invokes the gateway through the existing
protected E2B controller channel and checks the run, membership, lease and fencing tuple.

### Response size and Search Console rows

`max_response_bytes` is measured on the serialized JSON the step receives, and it is what
bounds result size in practice. A Search Console row costs roughly 110–250 bytes depending on
its dimensions, so a 64000-byte binding holds about 250–550 rows, far fewer than the provider's
25000-row maximum. `search_analytics.read` accepts:

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
aliases share the existing maximum of eight requests, with per-alias allowances and bounded
responses. Procedures retain their own declared timeout; the code executor's 60-second total
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
These requests never create Tin credit operations. Model calls use the existing priced
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
