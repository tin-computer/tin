# Stripe and PostHog connections

Tin has two first-party, read-only connections for revenue and product data:

- `payments.stripe`: the founder pastes a Stripe **restricted key** with read permissions.
- `analytics.posthog`: the founder authorizes Tin through PostHog **OAuth** and chooses one
  PostHog project.

Workflow packages bind to them like any other project connection (see
[project API connections](project-api-connections.md)) and call a fixed set of reviewed
operations. Tin reads the provider, then **projects** each response on the server into small
records, so one 64 KB call returns dozens to hundreds of records instead of a few raw provider
objects. Packages never see a key, token, host, account ID or PostHog project ID.

This guide is for **workflow authors** (operations, arguments, returned fields, limits,
errors, manifests and offline tests) and **operators** (configuration). It also describes the
founder's setup, so you can explain it. The code lives in
`src/tin_lite/stripe_connection.py`, `src/tin_lite/posthog_connection.py` and
`src/tin_lite/connection_records.py`; when this guide and the code disagree, the code is right.

Packages built on these connections: [`outreach.paying_segment`](../workflow_packages/outreach.paying_segment/main.py)
(code, Stripe), [`product.analytics_brief`](product-analytics-brief.md) (procedure, PostHog)
and the [`example.posthog_funnel`](../workflow_packages/example.posthog_funnel/workflow.json)
authoring example.

## Founder setup

### Stripe

1. In the project's **Integrations**, the Stripe card links to Stripe's *Create restricted key*
   page with Tin's read permissions already selected:

   ```text
   https://dashboard.stripe.com/apikeys/create?name=Tin&permissions[]=rak_customer_read
     &permissions[]=rak_subscription_read&permissions[]=rak_plan_read
     &permissions[]=rak_product_read&permissions[]=rak_invoice_read
     &permissions[]=rak_charge_read
   ```

   Each one backs a capability below. Tin asks for no account permission: on a restricted key
   `GET /v1/account` needs a Connect permission, which a read-only key should not carry.
2. The founder creates the key in Stripe and pastes the `rk_live_…` or `rk_test_…` value into
   the password field on Tin's page. Tin refuses secret (`sk_`) and publishable (`pk_`) keys:
   a full secret key is never stored. Keys go only into that page, never into chat or MCP;
   MCP `start_integration_connection` returns the setup link instead.
3. Tin calls `GET /v1/account` for the account ID and display name. When the key may not read
   it (the usual case), Tin takes the account ID from Stripe's 403 message, or a stand-in
   derived from the key when the message names none, and the card shows no account name. Tin
   then reads one record from each resource to find which reads the key allows. A 403 on one resource means that
   capability is not granted; a 401 rejects the key. The key is stored encrypted.
4. A `rk_test_` key is labelled **test mode** on the card ("Test mode · sandbox data only")
   and in the connection label. Packages see `livemode: false` in every response.
5. **Check again** re-reads the permissions after the founder edits the key in Stripe.
   Entering a new key replaces the old one; a concurrent change is refused, not overwritten.
6. **Disconnect** deletes the stored key. Tin cannot delete it in Stripe, so the founder should
   also delete the Tin key under *Developers → API keys*.

If Stripe later rejects the key (deleted or expired), the connection shows *needs attention*
and asks for a new key.

### PostHog

1. The PostHog card starts OAuth on `https://oauth.posthog.com` with PKCE. PostHog's consent
   screen asks the founder to approve Tin's read scopes and choose **one** PostHog project.
2. The token response names the region (US or EU Cloud); if it does not, Tin asks each region
   who the token belongs to. Only `https://us.posthog.com` and `https://eu.posthog.com` are
   used. Self-hosted PostHog is not supported.
3. The chosen project is selected automatically. The card can switch to another project the
   grant reaches; a workflow cannot start until a project is selected.
4. Tokens are stored encrypted and refreshed under the project's refresh lock (PostHog rotates
   refresh tokens on every use). If PostHog stops accepting them, the connection shows
   *needs attention*; **Reconnect** runs OAuth again and keeps the selected project when the
   new grant still reaches it.
5. **Disconnect** revokes the refresh token at PostHog and deletes the stored tokens.

## Operator setup

Both connections need `TIN_LITE_INTEGRATION_CREDENTIAL_KEY` (the AES-GCM key that encrypts
all integration credentials) and migration `045_stripe_posthog.sql`, which adds both provider
keys to the connection, receipt and auth-attempt constraints. Apply it before deploying.

Stripe needs nothing else: it is available wherever the credential key is configured.

PostHog is off until enabled:

| Setting | Purpose |
| --- | --- |
| `TIN_LITE_POSTHOG_OAUTH_ENABLED=true` | Turns the connection on. Settings validation then requires the credential key and an `https` `TIN_LITE_PUBLIC_URL` that is not localhost. |
| `TIN_LITE_PUBLIC_URL` | Tin's public origin. The OAuth client ID is `{TIN_LITE_PUBLIC_URL}/integrations/posthog/client.json` and the redirect URI is `{TIN_LITE_PUBLIC_URL}/integrations/callback/posthog`. Never derived from the Host header. |
| `TIN_LITE_POSTHOG_OAUTH_VERIFICATION_TOKEN` | Optional `phvt_…` token from PostHog's organization settings that links the client to that organization. It is published in the metadata document, so it is not a secret. |

Tin identifies itself with a **Client ID Metadata Document**: PostHog fetches the client ID URL
and reads Tin's name, redirect URI and scope ceiling from it. There is no registration step and
no client secret, and changing requested scopes later does not invalidate existing grants. The
document returns 404 while the connection is not configured.

Requested scopes: `project:read`, `user:read`, `query:read`, `event_definition:read`,
`property_definition:read`, `insight:read`. Only `project:read` is required for a grant to
count as connected; each capability below needs its own scopes.

## Capabilities

A package's `integration_requirements` names the capabilities it needs. A run cannot start
until the connection grants all of them (for Stripe, until the key can read the resources).

| Provider | Capability | Operations | Needs |
| --- | --- | --- | --- |
| `payments.stripe` | `subscriptions.read` | `subscriptions.list` | Read on Subscriptions and Customers (customers are expanded) |
| `payments.stripe` | `customers.read` | `customers.list` | Read on Customers |
| `payments.stripe` | `invoices.read` | `invoices.list` | Read on Invoices |
| `payments.stripe` | `prices.read` | `prices.list` | Read on Prices and Products (products are expanded) |
| `payments.stripe` | `charges.read` | `charges.list` | Read on Charges |
| `analytics.posthog` | `query.read` | `query.hogql` | `query:read` |
| `analytics.posthog` | `definitions.read` | `event_definitions.list`, `property_definitions.list` | `event_definition:read`, `property_definition:read` |
| `analytics.posthog` | `insights.read` | `insights.list` | `insight:read` |

Every operation is read-only. The Stripe client refuses any method but GET; the PostHog client
sends POST only to the query endpoint.

## Operations

Arguments are closed: an unknown name or a value outside its range is refused before anything
is sent (see [Errors](#errors)). Every argument is optional unless marked required.

### Stripe

All Stripe operations return records newest first and accept:

- `limit`: 1–100, default 100 (Stripe's page size; Tin may return fewer, see
  [Paging](#paging-and-fitting)).
- `cursor`: the `next_cursor` of the previous page.

`created_gte` and `created_lte` (where listed) take Unix seconds or a `YYYY-MM-DD` date in UTC:
the start of that day for `created_gte`, its last second for `created_lte`.

| Operation | Arguments | Record fields |
| --- | --- | --- |
| `subscriptions.list` | `status` (`all` (default), `active`, `past_due`, `unpaid`, `canceled`, `incomplete`, `incomplete_expired`, `trialing`, `paused`, `ended`), `created_gte`, `created_lte` | `id`, `status`, `created`, `start_date`, `current_period_end`, `cancel_at_period_end`, `canceled_at`, `ended_at`, `trial_start`, `trial_end`, `cancellation_details{reason, feedback}`, `livemode`, `metadata`, `discount_ids`, `coupon_id`, `items[{price_id, product_id, unit_amount, currency, interval, interval_count, quantity}]` (at most 20), `customer{id, email, name, email_domain, country, created, metadata}` |
| `customers.list` | `created_gte`, `created_lte`, `email` (one exact address) | `id`, `email`, `name`, `email_domain`, `country`, `created`, `metadata`, `currency`, `delinquent` |
| `invoices.list` | `status` (`draft`, `open`, `paid`, `uncollectible`, `void`), `customer` (`cus_…`), `subscription` (`sub_…`), `created_gte`, `created_lte` | `id`, `customer`, `subscription`, `status`, `billing_reason`, `amount_due`, `amount_paid`, `currency`, `created`, `period_start`, `period_end`, `attempt_count` |
| `prices.list` | `active` (true/false) | `id`, `product{id, name}`, `unit_amount`, `currency`, `type`, `recurring{interval, interval_count}` (null for one-time prices), `active`, `nickname` |
| `charges.list` | `created_gte`, `created_lte` | `id`, `customer`, `amount`, `amount_refunded`, `currency`, `status`, `paid`, `refunded`, `created`, `failure_code` |

Amounts are in the currency's minor unit, as Stripe returns them. Timestamps are Unix seconds.
A deleted customer is `{id, deleted: true}`. `metadata` keeps at most 20 string entries (keys
sorted, keys cut to 40 and values to 200 characters). `country` is the customer's address
country. `current_period_end` falls back to the latest item period end, where newer Stripe
API versions keep it. Tin pins the Stripe API version, so fields do not change with the
account's default version.

### PostHog

Every call reads the project the founder selected. The list operations accept:

- `limit`: 1–100, default 100.
- `cursor`: the `next_cursor` of the previous page (a position, at most 10000).
- `search`: 1–200 characters, matched by PostHog against names.

| Operation | Arguments | Returns |
| --- | --- | --- |
| `query.hogql` | `query` (required, see [HogQL rules](#hogql-rules)), `name` (1–64 letters, digits, spaces or `_.:-`; shown in PostHog's query log as `tin: <name>`) | `{columns, types, rows, has_more, truncated}` |
| `event_definitions.list` | `search`, `cursor`, `limit` | records of `name`, `volume_30_day`, `query_usage_30_day`, `last_seen_at` |
| `property_definitions.list` | `event_names` (1–20 unique names: only properties those events carry), `search`, `cursor`, `limit` | records of `name`, `property_type`, `is_numerical` (event properties only) |
| `insights.list` | `search`, `cursor`, `limit` | records of `id`, `short_id`, `name`, `kind`, `query_kind`, `last_refresh` (saved insights only) |

`query.hogql` runs with PostHog's blocking refresh. `rows` keep PostHog's column order;
`types` are PostHog's type names, one per column. Text cells are cut to 2000 characters,
nested values to 100 items, and NaN becomes null. Definitions carry no descriptions.

#### HogQL rules

PostHog asks integrations not to export data through its query API. Tin enforces that shape
before any request, on the query text with strings, quoted identifiers and comments masked:

- One statement that starts with `SELECT` or `WITH`; a trailing `;` is allowed.
- It ends with `LIMIT n`, where 1 ≤ n ≤ 1000. `LIMIT offset, count` is refused.
- No `OFFSET` anywhere, and no `UNION`, `INTERSECT` or `EXCEPT SELECT`.
- At most 8000 bytes, no control characters, no `#` comments, balanced quotes and parentheses.

Aggregate in the query instead of reading rows. To read further back, narrow with a `WHERE`
on a sort key (for example `timestamp < '<last seen>'`) in a new step, not with an offset. A
`LIMIT` bounds the rows returned, not what PostHog scans; queries also count against the
project's hourly PostHog query budget.

## Paging and fitting

Each binding declares `max_response_bytes` (1024–1000000). Tin measures a response as its
serialized JSON, the way the gateway returns it, and keeps the **leading** records that fit.

List operations (both providers) return:

```json
{"records": [...], "has_more": true, "truncated": true, "next_cursor": "sub_1Pq..."}
```

- `truncated`: Tin left records of this page out to fit the bound.
- `has_more`: more records follow the last one returned, left out here or not read yet.
- `next_cursor`: when `has_more`, the value to pass as `cursor` in a **new step** to continue
  exactly after the last returned record; otherwise `null`. For Stripe it is that record's ID,
  for PostHog a position.

Stripe responses also carry `livemode` from the connected key. `query.hogql` has no cursor:
`truncated` says rows were left out to fit, `has_more` that PostHog has more rows than the
query's `LIMIT` returned. Narrow the query rather than paging it.

Only a single record (or row) larger than the whole bound is an error. Sizes to plan with: a
one-price Stripe subscription with its customer is about 730 bytes, so about 85 fit in 64 KB
and eight calls read about 690; a customer record is roughly 250 bytes.

## Bounds

- At most four service bindings and **32 calls** in total per run, shared across bindings.
  A refused call counts; a call refused as a contract error does not.
- `max_response_bytes` 1024–1000000 per binding. Arguments at most 16 KB of JSON.
- Tin reads at most 8 MB from Stripe or PostHog for one call before projecting it.
- Code packages keep their declared compute window (`timeout_seconds`, at most 900) and Tin
  waits at most 60 seconds for one call; procedures keep their declared timeout.
- Stable step IDs replay completed responses after a restart; a changed request under the
  same step conflicts. See [recovery](project-api-connections.md#recovery-and-costs).

## Errors

A package sees each failure as a `ValueError` from `ctx.services.call` (code) or a tool error
from `call_service` (procedures). The message starts with Tin's own sentence. When the provider
answered and refused, Tin adds what the provider said, never its whole body:

```text
PostHog rejected the query (HTTP 400). PostHog said: validation_error/invalid_input:
Illegal type DateTime64(6, 'UTC') of first argument of aggregate function windowFunnel ...
```

The same facts arrive as a structure. In code, the `ValueError` has `code` (Tin's refusal
code, below) and `provider_error`; a procedure's tool error is JSON with `code`, `message`
and `provider_error`:

```json
{"provider": "PostHog", "status": 400, "type": "validation_error",
 "code": "invalid_input", "message": "Illegal type DateTime64(6, 'UTC') ..."}
```

`provider_error` keeps the HTTP status, the provider's error type and code, and its message.
Tin redacts the message before anyone sees it: the credential the call used, token and key
shapes (bearer values, JWTs, `sk-`/`rk_` keys, `token=` assignments) and the person part of
email addresses (`[redacted]@example.com`; the domain stays). It flattens line breaks and cuts
the message to 1,500 characters. The step's receipt records the same message and
`provider_error`, so a replayed step returns them unchanged. Failures that are not the
provider's answer (a timeout, a response Tin cannot read, a crash inside Tin) keep Tin's
generic message.

| Kind | Message starts with | What it means |
| --- | --- | --- |
| Contract | `The service request differs from its declared contract: <reason>.` | Unknown operation or argument, a value out of range, a capability the binding does not declare, or a HogQL rule. Nothing is sent. Fix the package. |
| Too large | `The service response exceeded this binding's max_response_bytes` | One record is larger than the bound. The step is settled; later steps may run. |
| Refused | see below | The provider answered and refused. The step is settled with Tin's message and what the provider said; a **new** step may try again later. |
| Uncertain | `Service response unavailable or invalid` | The outcome is unknown (for example a timeout). Later calls in the run then fail with `An earlier service request is unresolved`; it is never retried automatically. |

Refusal codes, recorded on the call receipt:

| Provider | Code | Message and cause |
| --- | --- | --- |
| Stripe | `permission_denied` | "Stripe's restricted key cannot read Subscriptions…": the key lost that permission after it was checked. |
| Stripe | `authentication_failed` | "Stripe rejected the stored restricted key…": deleted or expired key; the connection needs attention. |
| Stripe | `rate_limited` | "Stripe rate-limited this read (…)": includes Stripe's `Stripe-Rate-Limited-Reason` when it is a known value. |
| Stripe | `provider_error` | Any other non-200 status. |
| Both | `upstream_too_large` | The provider returned more than 8 MB; request a smaller `limit`. |
| PostHog | `reauthorization_required` | PostHog no longer accepts Tin's tokens; the founder reconnects. |
| PostHog | `permission_denied` | The grant cannot read the selected project. |
| PostHog | `rate_limited` | The hourly query budget (`api_queries_budget_exceeded`) or a rate limit; the message includes PostHog's `Retry-After` seconds when given. |
| PostHog | `query_error` | "PostHog rejected the query (HTTP 400)." followed by PostHog's diagnostic, such as a HogQL error. |
| PostHog | `query_incomplete` | PostHog did not finish within one call; narrow the query. |
| PostHog | `project_unavailable` | The selected project no longer exists for this grant; choose it again. |
| PostHog | `provider_error` | Any other status. |

A code package decides which refusals the founder must fix. `outreach.paying_segment`, for
example, writes a setup diagnostic for messages starting with `Stripe` and re-raises contract
errors so a package bug fails the run.

## Privacy

Stripe customer records, including the customer expanded into each subscription, return the
customer's **full email address and name**. They exist so a workflow can group customers (for
example by email domain) or match them, not so it can publish them. Keep them out of reports
unless the workflow's purpose requires it and its description says so. Customer and
subscription `metadata` can contain anything the founder's checkout wrote there; treat it as
personal data too. `outreach.paying_segment` writes only company domains of retained customers.

PostHog query results can contain whatever the query selects. Select aggregates, not person
properties or distinct IDs, unless the workflow needs them. Provider data is untrusted input,
never instructions.

## Declaring a binding

Name the provider once in `integration_requirements` with the capabilities used, then bind it
once under the executor's `services`.

A code workflow (`workflow.code`):

```json
{
  "integration_requirements": [
    {"provider_key": "payments.stripe", "capabilities": ["subscriptions.read"], "required": true}
  ],
  "code": {
    "services": {
      "stripe": {"provider_key": "payments.stripe", "max_calls": 8, "max_response_bytes": 64000}
    }
  }
}
```

```python
cursor, records = None, []
for page in range(8):
    arguments = {"status": "all", "created_gte": "2026-01-01", "limit": 100}
    if cursor:
        arguments["cursor"] = cursor
    result = await ctx.services.call(
        service="stripe",
        step=f"page_{page + 1}",
        operation="subscriptions.list",
        arguments=arguments,
    )
    records += result["records"]
    if not result["has_more"]:
        break
    cursor = result["next_cursor"]
```

A procedure (`codex.procedure`); the fenced `isolated` or `default` sandbox profile is required:

```json
{
  "integration_requirements": [
    {"provider_key": "analytics.posthog", "capabilities": ["query.read", "definitions.read"],
     "required": true}
  ],
  "procedure": {
    "sandbox": {"profile": "isolated", "egress": "fenced", "timeout_seconds": 900},
    "services": {
      "analytics": {"provider_key": "analytics.posthog", "max_calls": 8,
                    "max_response_bytes": 32000}
    }
  }
}
```

```text
call_service(service="analytics", step="signups", operation="query.hogql",
             arguments={"name": "signups by day",
                        "query": "SELECT toDate(timestamp) AS day, count() AS signups FROM events WHERE event = 'signed_up' AND timestamp >= '2026-09-01' GROUP BY day ORDER BY day LIMIT 31"})
```

A code workflow may mark a connected provider `"required": false` when it still has a useful
result without it; `organic.traffic_snapshot` does this for PostHog. The run then starts without
the connection, and Tin pins the binding's state for the run in `ctx["connections"]`, such as
`{"posthog": "not_connected"}` (or `connected`, `needs_attention`). A call to a binding that is
not `connected` raises a `ValueError` with code `not_connected` and sends nothing; a connection
made mid-run applies from the next run. Tin-held (`managed.*`) and custom API services, and
procedure bindings, stay required.

Both manifests are excerpts; the full packages above validate with
`uv run tin-lite validate-community`. Private `custom.*` copies may bind these connections too.

## Testing offline

`tests/connection_fakes.py` has stand-ins with the gateway's limits, closed-argument checks,
step replay and refusals. They run Tin's real argument validation, HogQL guard and projection
over synthetic provider objects, so a package test sees exactly the records the gateway would
return. No network, keys or database.

```python
from connection_fakes import FakePostHogConnection, FakeStripeConnection, stripe_objects


class Context(dict):
    def __init__(self, services):
        super().__init__(
            run_id="00000000-0000-4000-8000-000000000099", created_at="2026-09-01T12:00:00+00:00"
        )
        self.services = services


async def test_reads_subscriptions_through_the_binding():
    stripe = FakeStripeConnection(stripe_objects(), service="stripe", max_response_bytes=64000)
    result = await MODULE.run(Context(stripe), {})
    assert [call["operation"] for call in stripe.calls] == ["subscriptions.list"]


async def test_refusal_becomes_a_diagnostic():
    stripe = FakeStripeConnection(stripe_objects(), refuse={"page_1": "permission_denied"})
    result = await MODULE.run(Context(stripe), {})
    assert "needs attention" in result["content"]
```

- `stripe_objects()` loads `tests/fixtures/connections/stripe_objects.json` (Stripe-shaped
  `account`, `customers`, `subscriptions`, `invoices`, `prices`, `products`, `charges`). Pass
  your own dict to model a business; a subscription's `customer` may be an ID or an object.
- `refuse={"<step>": kind}` rehearses a refusal: `rate_limited`, `authentication_failed`, or
  any other kind for a missing permission. `capabilities=(...)` narrows the binding.
- `FakePostHogConnection(posthog_objects(), queries={...})` answers `query.hogql` from canned
  PostHog responses keyed by the call's `name` (or exact query text), after running the guard
  and applying the query's own `LIMIT`; it cannot execute SQL. Its refusal kinds are
  `rate_limited`, `budget_exceeded`, `reauthorization_required`, `permission_denied` and
  `query_error`.
- Each fake's `calls` list records each call's step, operation, arguments and projected response.

`tests/fixtures/connections/*_projected.json` show projected responses for every operation.
[test_outreach_paying_segment.py](../tests/test_outreach_paying_segment.py),
[test_product_analytics.py](../tests/test_product_analytics.py) and
[test_posthog_funnel.py](../tests/test_posthog_funnel.py) are complete examples. Include a
plausible but unusable response (a page without a cursor, a truncated query result) as the
package guide asks.
