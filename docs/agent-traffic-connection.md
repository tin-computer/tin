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
  chat.
- The reporter token is the one token the browser gets: Tin shows it to project members in the
  reporter step, so the founder can paste it on their host (see
  [The reporter's one paste](#the-reporters-one-paste)).

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
CARD-4 and INST-1, INST-4 and INST-5 on the "Analytics · daily report" page). While the site
waits, its second line says where the line stands: waiting in Decisions, being added in a pull
request, added, or to paste. Configure opens rows for the address (with the offer to count a
moved host), the script (its line shown on request) and the reporter (its one paste, or that it
is on). Stop measuring confirms in the card's foot; only that confirmation's "Stop and delete" is
filled, and when Tin's change put the line on the site it adds that Tin then prepares a change
that removes it. The card's data comes from `GET /api/projects/{id}/integrations` (the
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

`/analytics` (`analytics.js`, `analytics.css`; Paper REP-V2, V2b, V2c, V2d, V2e and INST-3) shows in the
rail once the project has set analytics up; before that the card is the way in. It reads
`GET /api/projects/{id}/analytics?view=today|yesterday|week`: the card's state (opening the page
asks for status when it is due, as opening the card does) and, once a visit has arrived, Tin's
stored report for the tab, in the founder's time zone. Today and Yesterday read a day; the last 7
days read a week ending today.

- While no visit has arrived, the page has no tabs: it shows the script line, where it goes (in
  the founder's site builder when Tin isn't adding it through GitHub), the CSP entries and what's
  counted.
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
  asked for pages (the top three, then the rest in one row), and the three pages AI companies
  loaded most, split by company as `ai_pages` gives them, with the rest under "Other pages".
- The needs-you bar shows the report's `needs_you` text as it is, with Count instead for a moved
  host. Every number is exact (the service withholds nothing); a `null` would show as a dash,
  as a guard. The report's notes close the page in small print.

## MCP

`list_integrations` and `get_integration` carry the card's state as `analytics`.
`get_site_analytics(project_id, period, date)` returns Tin's stored report (`day` or `week`,
`date` in the founder's time zone, today by default) with its `markdown` and the `comparison`.
`get_analytics_install(project_id)` is for a coding agent that puts the line on the site itself:
the script line and where it goes, the CSP entries, the published reporter files (url, sha256,
which host each is for, the file name) and how each is wired in, and `tin_change`, where Tin's
own change stands, so the agent doesn't make it twice. None of them returns a token: the agent
asks the founder to copy the reporter's value from the card. Asking for the guide opens the
reporter step on the card.

## Putting the line on the site

Tin Analytics counts a site through its script line and, on hosts that can run it, the request
reporter. How they get onto the site depends on what Tin can reach.

**With GitHub** (`website_change_analytics.py`; Paper INST-1 and INST-2). When the project's
website repository (the one chosen on Integrations, with pull-request access) is connected, Set
up proposes one `analytics` change row in Decisions. Opening the card while it waits proposes it
too, for a repository connected after Set up. A linked site gets no row: its line is already
there.

- The row's pieces are the script line, the CSP entries and the reporter variant for the site's
  host, by its SHA-256. Tin picks the variant from the repository's root files: a `next`
  dependency or `next.config.*` means `nextjs` on any host; otherwise `netlify.toml` means
  `netlify`, a Wrangler file with `main` means `cloudflare-worker`, `vercel.json` (or the setup
  answer naming Vercel) means `vercel`, and an Express or Connect dependency means `node`. With
  none, the change carries the line alone. The host for the reporter's value comes from the same
  files, then from the setup answer (`system_hosting`).
- The approval covers those pieces (`content_sha256`), not the files the run writes. Approving,
  in Decisions or with `approve_website_change`, starts website.change with `source: analytics`.
  If the start is refused (credits, a disconnected repository), the approval stands and the
  card's Configure offers **Start again**.
- The run downloads the reporter file before its Codex session and checks it against the
  approved SHA-256; a file that doesn't match stops the run there. The `site-analytics` skill
  adds the line to the layout every page uses, the CSP entries where the site already sets a
  policy, and the reporter with its wiring.
- `check_patch` holds the patch to the pieces: an insert-only diff of the line, the CSP sources,
  the reporter's import and its wiring words, plus the reporter's copy, which Tin replaces with
  the published bytes whatever the session wrote. Dependency files, CI, `.github/` and
  environment files are refused outright. Any other edit still opens the pull request, but it
  waits for the founder with the reason; Tin merges only a patch that holds just the pieces, once
  the site's build and the repository's required checks pass, under website.change's rules. A
  site that stopped measuring before the merge also leaves the pull request to the founder.
- The merge receipt records the files, the files the change added and where the reporter went,
  which the card reads ("line added oct 22 · pull request #41") and a later remove uses.

**Set up again after stopping.** A new site has a new site ID, so its line differs. When Tin's
line for the old site is still on the site, the proposal is a `replace` (the old line for the new
one, nothing else); otherwise a new `install`.

**Stop measuring** proposes a `remove` row when Tin's change put the line on the site: the line,
the CSP entries and the reporter's wiring come out, and a pending install is withdrawn. Codex
procedures can't delete files, so the reporter's copy and any file Tin's change added (such as a
middleware file of its own) stay; the Decisions card and the pull request name them for the
founder to delete. They do nothing once the site is removed, because every reporter token stops
with it.

**Without GitHub** (Paper INST-3). The card says to paste the line, and the waiting page says
where in the founder's site builder: Webflow, Framer, WordPress, Squarespace, Wix, Shopify or
Ghost, matched from the setup answer or the site's address, else a general entry. **Change**
picks another (`POST .../analytics.agent_traffic/builder`, kept with the site). Site builders get
no reporter.

## The reporter's one paste

Once the reporter is on the site (Tin's merged change added it, or a coding agent asked for it
over MCP), Configure shows the reporter step (Paper INST-4): the name `TIN_REPORTER_TOKEN`, the
value hidden until **Show** or **Copy**, and where it goes on the host (Vercel, Netlify,
Cloudflare, or the server's environment). The card's second line says "reporter: one paste left
on Vercel" until the first record arrives; then the row says the reporter is on, and the step
opens on request.

- The value comes from `POST .../analytics.agent_traffic/reporter-token`, which only browser
  sessions reach (the dashboard API takes no OAuth tokens) and which answers with
  `Cache-Control: private, no-store`. The first request calls the service's reporter-token
  endpoint; Tin seals the token with the integration cipher (its own context, apart from the
  read token) in `agent_traffic_sites`, so a member can show it again. `{"new": true}` asks the
  service for a new one; the previous one keeps working for 24 hours.
- A linked site answers `409 reporter_not_needed`, and the card shows no step.
- The token goes with the site: Stop measuring deletes the row, and a new site starts without one.

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

**A linked site** (`"linked": true`, such as tin.computer's and unbaked's, or a staging project
linked to `organic-tin`) was measured before Tin. Its DELETE answers `200` with
`"state": "unlinked"`: Tin's tokens stop working, Tin deletes its own copies, and the site's
history stays. So the card asks "Stop showing Tin Analytics for this project?", its Stop is
outlined rather than filled, and afterwards the card says "stopped oct 22 · history kept". A
linked site comes back from PUT without `url`; the card names it by its counted hosts.

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
