# Visitor journeys

`product.visitor_journeys` reads one connected PostHog project each day and writes a journal
of the people who used the site and product in the window. It covers:

- who to act on today, each with their journey, where they stopped and one suggested next step;
- new users and how far they got;
- frictions: rage clicks, dead clicks, errors, failure events, stalls between key steps, and
  pages opened again and again in one session, each with who hit it and the code behind it when
  a repository is connected;
- delights: moments that show the product worked for someone;
- up to five product changes, most costly first;
- tracking problems, such as a renamed key event or signups without an email.

It is a Codex procedure (`workflow_packages/product.visitor_journeys`), because every
project's events, hosts and identity setup differ. A reviewed Python resource, `JOURNEYS.md`,
owns every query, tier, count and ledger entry. The agent writes the map of the project's
events and does the judgment: journeys in words, causes, actions and changes.

It differs from the [product analytics brief](product-analytics-brief.md), which reports
aggregate funnels and trends with no people and no recommendations. This workflow is per
person and ends in actions.

## Who it names

Only people who told the product who they are, by an email on their PostHog person (signing
up, or a form that calls `identify`). Anonymous visitors appear as counts and as anonymous
journeys, never with a guessed identity: no company from an IP address, network, city or device.
Team members (by `exclude_email_domains`, `internal_people`, or the map's `internal` list) and
bots are counted and left out. Replay links open the session in PostHog; the report never
quotes what a person typed.

## Set it up

Connect PostHog in Integrations (read scopes only) and choose the project. Every input is
optional:

| Input | Default | What it does |
|---|---|---|
| `lookback_hours` | 24 | The window, ending when the run starts (1–168). A daily schedule reads the last day. |
| `website_hosts`, `product_hosts` | none | Pageviews to count. Empty: every host except those the map ignores (previews, staging). Server events always count. |
| `exclude_email_domains` | none | Team domains, left out in the queries themselves. |
| `internal_people` | none | Team emails or PostHog person IDs on personal accounts. |
| `max_people_detailed` | 12 | How many people get a full timeline read (up to 30). New users and anyone with a failure always do. |
| `notes` | none | What your events mean, or what to watch. Outranks project files. |

Schedule it daily. The first run maps the events and takes longer.

## Memory between runs

A procedure writes one file, so the memory lives in the report's evidence block:

- **map**: hosts and hosts to ignore, the person properties that hold an email and a name,
  key events by kind (intent, booking, signup, activation, payment, failure, feedback), the
  main path as ordered steps, team people, bot patterns, the replay link base and the capture
  settings, and a dated list of changes.
- **ledger**: each reportable friction with the days it was seen, and each named person with
  when they were first and last surfaced and whether they came back. A friction is `new`, then
  `ongoing`, and `fixed` once it has been seen on two or more days and then not for five. Entries
  older than 90 days drop.

The next run reads the newest report under `reports/visitors/` and starts from its map and
ledger. When a mapped key event stops firing, or a new event looks like its rename,
`check_drift()` says so. The run then re-maps and lists the change under Tracking.

## Queries

All go through `call_service` (service `analytics`, at most 16 calls). A normal run uses 8 to 10:

| Step | Operation | Reads |
|---|---|---|
| `project` | `project.read` | capture settings and the replay link base |
| `inventory`, `hosts` | `query.hogql` | 30 days of event names and hosts; on the first run or on drift |
| `census` | `query.hogql` | one row per person in the window (up to 1000, most engaged first) |
| `prior` | `query.hogql` | those people's sessions and days in the 90 days before |
| `frictions`, `loops` | `query.hogql` | rage and dead clicks, exceptions, failure events; pages opened 3+ times in a session |
| `timelines<n>` | `query.hogql` | up to 15 people per batch, 60 events each, in order |

A refused or truncated census or friction query makes the report `incomplete`, never zero. If
every query is refused, the report measured nothing: the run fails with that reason and the
report stays a saved diagnostic, as for the analytics brief.

## Output and People

The report goes to `reports/visitors/{run_id}.md` and ends with one evidence block
(`visitor-journal.v1`, `src/tin_lite/visitor_journal.py`). Only this package at that path may
declare the validator, because publishing it writes People.

After publication, `visitor_people.py` writes each named person to [People](people-records.md):

- a record recognized by their `email` (lowercase), created or reused;
- `web_tier` (lead, new user or user, with where they stopped as the reason) and `web_summary`;
- their `name`, only when no other source set one;
- one `visited` event per person and window end date, with the summary, where they stopped,
  the suggested action and the replay link.

Writes are idempotent, skip people the founder asked Tin to forget, and never fail the run. A
person row the report gets wrong (no email, an anonymous tier, a person the report never
mentions, a replay link that isn't the project's) is left out. Email-only people never enter
LinkedIn invitations, which need a LinkedIn member ID.

## Limits

- PostHog only. Search Console, Stripe and GitHub stay out of the queries. Connected repositories
  are read only to find the code behind a friction.
- Replays are linked, not read: PostHog keeps recording contents out of its query API for Tin's
  read-only scopes.
- Element text exists only where PostHog captures it. Many sites mask it, so rage clicks often
  name only the page.
- The cost ceiling is $5 per run; the estimate ($1.00) is judged, not measured.
