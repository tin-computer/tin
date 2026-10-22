# Analytics (Agent Traffic)

Analytics shows who visits a founder's site: people, AI agents that browse with a real browser,
crawlers, and visits that arrived from an AI assistant's answer. Agent Traffic records the
visits on its own service at `https://edge.tin.computer`. Tin's side is the connection
`analytics.agent_traffic` (`agent_traffic_connection.py`). The interface between the two is
Agent Traffic's [API contract](https://with.md/s/qkptksy_), and only Agent Traffic changes it.
When something in it doesn't fit Tin, tell Ege, who passes it on.

## Settings

| Setting | What it does |
|---|---|
| `TIN_LITE_AGENT_TRAFFIC_SERVICE_TOKEN` | Tin's credential for the admin calls: create, change and remove a project's site. It reads no numbers. Ege sets it in the host settings; it never passes through chat. Without it, the card says that analytics isn't set up on this deployment. |
| `TIN_LITE_AGENT_TRAFFIC_URL` | The service's origin, `https://edge.tin.computer` by default. Tests point it elsewhere. |
| `TIN_LITE_AGENT_TRAFFIC_PROJECTS` | The pilot: only these project IDs (comma-separated) see the card. |

Production sends project references as `tin:<project id>`. Staging and development send
`tin-staging:<project id>`, which only the staging credential can reach. Staging's sites are
real sites on the service, kept for 7 days, and at most 20 can be active at once; a set up past
that limit says so on the card (`409 staging_limit`).

## Tokens

- The service credential is Tin's own. A `401` or `403` on it is logged as an error, and the
  card says that analytics isn't set up on this deployment. Tin has no alerting.
- Each site has a read token. Every PUT issues a new one; the old one keeps working for ten
  minutes. Tin seals it with the connection (`seal_credential`) in the same statement that
  stores the rest of the connection, under the project's integration lock.
- Neither token goes into a log, the browser, a workflow input, an MCP response or an agent's
  chat. The reporter token (step 3) is the one exception: Tin shows it to project members in the
  reporter step, so the founder can paste it on their host.

## The connection

**Set up** sends `PUT /v1/admin/projects/{ref}/site` with the project's website address. The
address is the `product_url` of the project's latest Start here run (`growth.onboarding`, not
superseded). A project without one gets a single address field on the card. Tin refuses what
the contract would refuse as `invalid_url` before sending anything: an IP address, `localhost`,
an internal name such as `*.local`, or anything that isn't http or https.

The connection's `external_account_id` is the site ID. Its configuration holds what the service
said at the last PUT: the site ID and reference, the address, the counted hosts, the script line,
the CSP entries and the reporter's variants (only those with a valid `sha256`). It changes only
when Tin sends PUT again, because a run fails when its connection's configuration changes under
it. Setting up again with the same facts keeps the connection's ID and configuration, so a
running workflow keeps going with the new token.

The site's state moves on its own, so Tin keeps it apart from the configuration, in
`agent_traffic_sites`, along with which Start here run the address came from.

**Address changes.** When a newer Start here names a different host (compared without a leading
`www.`), Tin sends PUT again the next time it reads the site's status. A newer setup with the
same host changes nothing. When status lists a real domain under `uncounted_hosts` (not a
preview on a shared host such as `vercel.app` or `netlify.app`), the card offers to count that
address instead; one press sends PUT with it. That choice stands until a newer Start here names
another host.

## The card

| Card state | When |
|---|---|
| `unavailable` | No service token, or the service refused Tin's credential. |
| `not_set_up` | No connection. Shows the address from Start here, or the address field; after Stop measuring, the removal's state and when the data is deleted by. |
| `waiting` | Status says `waiting`: no page recorded yet. |
| `live` | A page in the last 7 days. Shows `pages_today`, and a second line when the reporter sent a record in the last 7 days. |
| `quiet` | Pages before, none in 7 days. |
| `needs_attention` | A read got `401` or `404` without Tin asking to stop. **Set up again** sends PUT. |

The card is **Tin Analytics** in Integrations (`app.js`, `renderAnalyticsCard`; Paper CARD-1 to
CARD-4 on the "Analytics · daily report" page). Configure opens rows for the address (with the
offer to count a moved host), the script (its line shown on request) and, once records arrive,
the reporter. Stop measuring confirms in the card's foot; only that confirmation's "Stop and
delete" is filled. The card's data comes from `GET /api/projects/{id}/integrations` (the
`analytics` field) and
`POST .../integrations/analytics.agent_traffic/refresh`. Set up is `POST .../setup` (with `url`
only for a project without Start here's address), and counting a moved host is
`POST .../address` with `host`.

**When Tin asks the service.** Opening the card (the integrations list, the refresh route, or
MCP's `list_integrations` and `get_integration`) asks for status at most once every 30 seconds
for each project while the site is waiting. Otherwise Tin shows its stored copy and asks again
once it is more than ten minutes old. A claim on `agent_traffic_sites.status_claimed_at` keeps
that true across processes. A `429` waits for its `retry-after`; an outage waits a minute,
doubling up to an hour. Meanwhile the card shows the stored copy with `stale: true`.

## The Analytics page

`/analytics` (`analytics.js`, `analytics.css`; Paper REP-V2, V2b, V2c, V2d and V2e) shows in the
rail once the project has set analytics up; before that the card is the way in. It reads
`GET /api/projects/{id}/analytics?view=today|yesterday|week`: the card's state (opening the page
asks for status when it is due, as opening the card does) and, once a visit has arrived, Tin's
stored report for the tab, in the founder's time zone. Today and Yesterday read a day; the last 7
days read a week ending today.

- While no visit has arrived, the page has no tabs: it shows the script line, where it goes, the
  CSP entries and what's counted.
- Four numbers. Visits and visits from AI assistants show their change from the period before.
  Page views shows pages per visit, since its change would mostly repeat the visits'. Automated
  browsers shows the companies that signed or came from their network, named only where the
  report's `named` list says so. Declared crawlers stay in the chart's legend.
- Companies are named from Agent Traffic's fixed list of operator IDs (`COMPANIES` in
  `analytics.js`); an ID added later shows as the ID until Tin names it.
- Hour by hour for a day and day by day for a week, by kind of visit; the detector's split sits
  under it, marked provisional. Pages, where visits came from (an AI assistant's site is tagged)
  and regions.
- With the request reporter: page loads by class ("verified bots", "named bots, unconfirmed",
  "other bots", and browser-like loads with how many never ran the script), the companies that
  asked for pages, and the pages AI companies loaded, split by company as `ai_pages` gives them.
- The needs-you bar shows the report's `needs_you` text as it is, with Count instead for a moved
  host. Every number is exact (the service withholds nothing); a `null` would show as a dash,
  as a guard. The report's notes close the page in small print.

## MCP

`list_integrations` and `get_integration` carry the card's state as `analytics`.
`get_site_analytics(project_id, period, date)` returns Tin's stored report (`day` or `week`,
`date` in the founder's time zone, today by default) with its `markdown` and the `comparison`.
Neither returns a token.

## Stored reports and comparisons

The page and the MCP tool read Tin's stored copy of a report (`agent_traffic_reports`, keyed by
site, period, date and time zone). Tin asks again once the copy is more than ten minutes old,
unless the report says `complete`, which is final. A failed read keeps the stored copy.

Each stored report comes with `comparison`, built from the report's `previous` (the day before
for a day, the 7 days before for a week): visits, page views, visits from AI assistants
(`totals.assistant_visits`) and each kind of visit, each as `now` and `before`, with `percent`
only when the period before had at least ten ("up from 4" reads truer than "+50%"). A number
that is `null` in either period (the service sends none) has no change, and `comparison` is null when `previous` is (the
period before is past retention). Before the site's first visit, `before` is 0. While the
period is unfinished (Today, or the last 7 days ending today), `previous` counts the period
before only up to the same point, which follows the report's `built_at`, and `until` says where
it stops, so the page says "against Tuesday up to 10:50". `until` is null for a whole period.

## Stop measuring

Stop measuring (the card's disconnect) and project deletion send
`DELETE /v1/admin/projects/{ref}/site`. The founder's data on the service has to be deleted, so
the removal is durable:

1. Tin records it in `agent_traffic_removals`, deletes its stored status and reports, and sends
   the first DELETE at once. The connection and its read token go with the disconnect.
2. Until the service answers `202`, `200` or `404`, the switchboard's `removal_loop` sends it
   again, a minute later, then doubling up to six hours, or after the service's `retry-after`.
   It keeps trying after timeouts, `401`, `403`, `429` and `5xx`, and logs the credential
   refusals as errors. Each pass sends at most 20, under the service's 60 admin calls a minute.
3. The project purge leaves `agent_traffic_removals` in place (it is in `KEPT` in
   `tests/test_project_purge.py`), so removals continue after the project's own data is gone.

DELETE removes the project's site by its reference, so Set up waits while an earlier removal
hasn't been accepted: it sends that DELETE first, and says to try again in a few minutes if the
service still hasn't accepted it. After `202`, a new PUT makes a new site with a new site ID.

## Workflow reads

A code workflow or procedure binds `analytics.agent_traffic` like any first-party connection
(`SERVICE_CAPABILITIES`, `code_services.OPERATIONS`). The site is always the connection's, and
the time zone is the founder's (Start here's answer, then the newest schedule's, then UTC).

| Operation | Capability | Arguments | Returns |
|---|---|---|---|
| `site.status` | `status.read` | none | state, first and last page, `pages_today`, hosts seen, uncounted hosts, `requests_last_at`, `checked_at` |
| `report.read` | `reports.read` | `period` (`day` or `week`, default `day`), `date` (`YYYY-MM-DD` within the last 180 days, default today) | the contract's report record, whole, including its `markdown` |

Each call writes an `integration_call_receipts` row. A `401` or `404` marks the connection as
needing attention and settles the step with `authentication_failed` or `site_not_found`; a
`429` is Tin's rate-limit refusal; both are known outcomes, so a later step may try again. A
`week` read ending on a date covers the 7 days up to and including it.

## Tests

`tests/agent_traffic_fake.py` is an offline service that keeps sites per reference the way the
contract describes and answers with the contract's examples (`tests/fixtures/agent_traffic/`,
with synthetic tokens and hashes where the contract elides them). `refuse` makes the next calls
to a path fail with a chosen status. `tests/test_agent_traffic_connection.py` runs against real
migrations in a disposable schema (`TIN_LITE_TEST_DATABASE_DSN`).

## Staging test

Once Agent Traffic says the service is deployed: Ege sets the staging credential in staging's
settings, the staging project's ID goes on `TIN_LITE_AGENT_TRAFFIC_PROJECTS`, and Agent Traffic
links that project to `organic-tin`, a site with real visits. Then test set up, the waiting
state, the live card, a workflow read and Stop measuring. A linked site's DELETE only unlinks it
(`200 unlinked`). To test deletion itself, set up another staging project on an exact host such
as `something.vercel.app`.
