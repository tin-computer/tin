# Visitor journeys

`product.visitor_journeys` reads one connected PostHog project each day, or each week, and
writes a journal of the people who used the site and product in the window. It covers:

- who to act on today, each with their journey, where they stopped and one suggested next step;
- new users and how far they got, with where they work: employer, role, what the company does
  and their LinkedIn profile, found on the public web;
- frictions: rage clicks, dead clicks, errors, failure events, stalls between key steps, and
  pages opened again and again in one session, each with who hit it and the code behind it when
  a repository is connected;
- delights: moments that show the product worked for someone;
- how visitors use the site, most of them anonymous: where they come in, the common routes, where
  they leave, how many reach pricing, booking or signup, and a few anonymous journeys;
- up to five product changes, most costly first;
- tracking problems, such as a renamed key event or signups without an email.

It is a Codex procedure (`workflow_packages/product.visitor_journeys`), because every
project's events, hosts and identity setup differ. A reviewed Python resource, `JOURNEYS.md`,
owns every query, tier, count and ledger entry. The agent writes the map of the project's
events and does the judgment: journeys in words, causes, actions and changes.

The brief at the top of each report is also the run's [summary](run-summaries.md), which a saved
schedule can post to a Slack channel. Its lines say what happened, never what to do about it or
what it means; suggested actions stay in the report's People to act on.

It differs from the [product analytics brief](product-analytics-brief.md), which reports
aggregate funnels and trends with no people and no recommendations. This workflow is per
person and ends in actions.

## Who it names

Only people who told the product who they are, by an email on their PostHog person (signing
up, or a form that calls `identify`). Anonymous visitors appear as counts and as anonymous
journeys, never with a guessed identity: no company from an IP address, network, city or device.
Team members (by `exclude_email_domains`, `internal_people`, or the map's `internal` list) and
bots are counted and left out. A product account seen only in server events, with no email and
no pageviews (often a tenant ID), is counted apart from website visitors. Replay links open the session in PostHog; the report never
quotes what a person typed.

## New users and where they work

A named person is new when they signed up in the window, or gave an email for the first time
(a lead with no earlier sessions). When `look_up_new_people` is on, the agent looks up each new
person before writing about them: at most 10 people and about 8 steps each (a search, a page it
opens or a GitHub lookup), in this order, stopping once it knows their role and company:

1. What they gave the product: name, email, company, role, website.
2. Their email domain. A work domain names the company, and the agent opens its site (home,
   about or team page) for what it does, its size, and their role and profile when the team page
   names them. A free mail domain (gmail.com and the like) names nothing; a shared inbox such
   as info@ is the company, not a person.
3. GitHub, through Tin's `managed.github` lookup: the account whose commits carry their exact
   email, the owner of the repositories those commits went to when it carries the same name,
   or, with no commits, an account whose username is their email's local part and whose profile
   has their full name. It returns the company, site, bio and LinkedIn profile they published.
   It only knows people who write code; for everyone else the other steps do the work.
4. Their own links: the site they gave or GitHub names, and the employer and LinkedIn it links.
5. Web search with what the steps above found (name with company, domain, site or username),
   never the name alone.

A LinkedIn profile or role is kept only when it ties to their own data, strongest first: exact
email, a link they published, username and full name, full name and their email's company.
`profile()` refuses a role or profile without that `basis`. With a free mail address and nothing
else that ties, the report says nothing: a namesake is worse than no profile.
- Work only: employer, role, the company, its size and a public professional profile. Never a
  home address, family, age, personal social accounts or private life. Anonymous visitors, team
  members and people who are not new are never looked up.

The brief lists the new people under **New users**, above **Some visitors**:

```text
- **Ana Souza** (ana@fieldnotes.dev): Founder at Fieldnotes; a research notebook for UX teams.
  Came from the hackathon and created an account. [LinkedIn](…) · [Fieldnotes](…) · [Replay](…)
```

Web search runs on the model provider's side and is billed per search, about $0.01 each; opening
a page is not billed per call. A day with ten new people adds about $0.50. The GitHub lookup is
free; it sends the email being looked up to GitHub's search. Turn it off for a consumer product, where a user's
employer is none of the founder's business.

## Set it up

Connect PostHog in Integrations (read scopes only) and choose the project. Every input is
optional:

| Input | Default | What it does |
|---|---|---|
| `period` | day | `day` or `week`. A saved daily schedule sets `day` and a weekly one `week`, whatever was chosen; on demand keeps the choice. |
| `lookback_hours` | 24 | A day's window, ending when the run starts (1–168). A week always reads 168 hours. |
| `website_hosts`, `product_hosts` | none | Pageviews to count. Empty: every host except those the map ignores (previews, staging). Server events always count. |
| `exclude_email_domains` | none | Team domains, left out in the queries themselves. |
| `internal_people` | none | Team emails or PostHog person IDs on personal accounts. |
| `max_people_detailed` | 12 | How many people get a full timeline read (up to 30). New users and anyone with a failure always do. |
| `look_up_new_people` | on | Look up where new users work and their LinkedIn profile on the public web. |
| `notes` | none | What your events mean, or what to watch. Outranks project files. |

Schedule it daily, or weekly for a report read carefully once a week. The first run maps the
events and takes longer.

A weekly run reads the last 7 days. Its table compares the week with the weekly average of the 4
weeks before, where a daily one compares yesterday with the last 7 days. Its brief is longer: up
to 20 new users, 12 other visitors and 10 observations, with lines of up to 400 characters
instead of 10, 5, 5 and 240. It looks up as many as 16 new people instead of 10. "Only what is
new" is measured against the last weekly brief, so a project with a daily run as well still
gets a full weekly brief. Slack carries a
long section on in the next block rather than cutting it. The period follows the schedule when
the config is saved (`visitor_journal.follow_schedule`), so Start here pacing a daily pick to
weekly also reads the week.

## Memory between runs

A procedure writes one file, so the memory lives in the report's evidence block:

- **map**: hosts and hosts to ignore, the person properties that hold an email and a name,
  key events by kind (intent, booking, signup, activation, payment, failure, feedback), the
  main path as ordered steps, team people, bot patterns, the replay link base and the capture
  settings, and a dated list of changes.
- **site**: the last run's landing pages, routes and exits, so the next report can say what
  changed.
- **ledger**: each reportable friction with the days it was seen, and each named person with
  when they were first and last surfaced and whether they came back. A friction is `new`, then
  `ongoing`, and `fixed` once it has been seen on two or more days and then not for five. Entries
  older than 90 days drop.
- **brief**: the table, new users, visitors and observations that opened the last report. The
  founder reads the brief every morning, so it says only what is new since then. `assemble()`
  refuses a person who was in the last brief and has done nothing since its window ended, and an
  observation it made word for word. A visitor who came back is listed for what they did since. A tracking gap
  or an ongoing friction is mentioned again only when it changes.

The next run reads the newest report under `reports/visitors/` and starts from its map and
ledger. When a mapped key event stops firing, or a new event looks like its rename,
`check_drift()` says so. The run then re-maps and lists the change under Tracking.

## Queries

All go through `call_service` (service `analytics`, at most 16 calls). A normal run uses 10 to 12:

| Step | Operation | Reads |
|---|---|---|
| `project` | `project.read` | capture settings and the replay link base |
| `inventory`, `hosts` | `query.hogql` | 30 days of event names and hosts; on the first run or on drift |
| `census` | `query.hogql` | one row per person in the window (up to 1000, most engaged first) |
| `prior` | `query.hogql` | those people's sessions and days in the 90 days before |
| `frictions`, `loops` | `query.hogql` | rage and dead clicks, exceptions, failure events; pages opened 3+ times in a session |
| `paths` | `query.hogql` | website sessions grouped by landing page, first four pages and exit page |
| `timelines<n>` | `query.hogql` | up to 15 people per batch, 60 events each, in order |
| `recordings` | `query.hogql` | which of up to 120 of those sessions PostHog recorded |

A replay link goes only to a session in PostHog's replay table. A project can have replay on
while a site's SDK turns recording off or samples it; such a session opens to "Recording not
found", so its person gets no link, and the report says once under Tracking that recording is
off or sampled on that host. A refused `recordings` call means no links, not a failed run.

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
- their `headline` ("Founder at Fieldnotes") from a new person's work, likewise only when unset.
  Their LinkedIn profile is never added as an identifier, so a namesake found by search cannot
  merge two records;
- one `visited` event per person and window end date, with the summary, where they stopped,
  the suggested action and the replay link.

Writes are idempotent, skip people the founder asked Tin to forget, and never fail the run. A
person row the report gets wrong (no email, an anonymous tier, a person the report never
mentions, a replay link that isn't the project's) is left out. Email-only people never enter
LinkedIn invitations, which need a LinkedIn member ID.

## Limits

- PostHog only. Search Console, Stripe and GitHub stay out of the queries. Connected repositories
  are read only to find the code behind a friction. Web search is only for new people's work.
- Replays are linked, not read: PostHog keeps recording contents out of its query API for Tin's
  read-only scopes.
- `paths` counts signup only when the signup event carries the browser session. A signup on
  another host, such as the app, shows as none there; the report says so.
- Element text exists only where PostHog captures it. Many sites mask it, so rage clicks often
  name only the page.
- The cost ceiling is $8 per run, for a weekly read of up to 30 people; the estimate ($1.00, a
  daily run) is judged, not measured. A daily run on 12 people cost $0.65.
