# RB2B visitors

RB2B matches visits to a project's site with people (or only companies) and posts each one to
a webhook. Every project has its own address. Tin keeps what RB2B posts, starts a
`people.rb2b_visitor` run for each person, and the run puts them in [People](people-records.md),
reads their visit in the project's PostHog and rates them against `strategy/ICP.md`.

## The address

- Integrations has an RB2B card. **Set up** makes the address,
  `https://<TIN_LITE_PUBLIC_URL>/webhooks/rb2b/<token>`, to paste into RB2B under
  Integrations → Webhook. RB2B's "Send repeat visitor data" sends every visit, not only the
  first; its test event marks the card "test event received".
- The token is the credential: RB2B sends no signature, secret or account ID. Tin keeps its
  SHA-256 to find the project, and the token sealed with the integration credential key
  (context `rb2b:<project>`) so the card can show the address again. "New address" replaces
  it (the old one stops working); "Remove" stops it. Any project member can do either.
- Runs start as the member who set the address up. If they leave the project, visitors wait
  and the card says to set it up again.
- Code: `rb2b_api.py` (the endpoint, the card's API), `rb2b.py` (payload, People and PostHog),
  `rb2b_activities.py`, `static/rb2b.js`; migration 087.

### Always answering

RB2B turns the integration off after a delivery that gets no 200 within 15 seconds, and a
founder has to turn it back on by hand. So:

- The endpoint keeps the payload and answers; the run starts after, in the background
  (`rb2b_api.rb2b_webhook`), and keeping it is bounded by `RECEIVE_SECONDS` (8). A start that
  fails is retried by the main process's sweep (`waiting_visits_loop`, every minute, for
  visits waiting over half a minute) and with the project's next visitor.
- Deploys restart the main process (30 to 50 seconds without HTTP). `/webhooks/*` goes
  through Caddy to the webhook intake first (`tin-lite-intake.service`: the same release with
  `TIN_LITE_WEB_ONLY=true`, port 8001, two database connections, no worker or loops) and to
  the main process when the intake is down. `activate_release.sh` restarts the intake only
  after the main process reports startup, so one of them always answers.
- Postgres allows 25 connections in all. A rating holds one for its whole model call, so
  `people_icp.PARALLEL` (4) stays well under the main process's pool (`TIN_LITE_DB_POOL_MAX`,
  10); at 10 a rating run took every connection and the webhook timed out.

## What RB2B posts

A flat JSON object with spaced keys ("LinkedIn URL", "First Name", "Title", "Company Name",
"Website", "City", "State", "Seen At", "Captured URL", "Referrer", "Tags", …). Tin reads empty
strings as missing, "Seen At" leniently (RB2B's own sample isn't ISO 8601; no zone is UTC), and
the repeat flag as either `is_repeat_visit` or `is_repeat_visitor`.

`POST /webhooks/rb2b/{token}` always answers 200 with an empty body, within RB2B's 15 seconds,
for a token nobody has and a body that isn't JSON too: RB2B disconnects an address on anything
else. It keeps the payload in `rb2b_visits`, once per profile, time and page, as one of:

- `person`: starts the visitor's run (start key `rb2b:<visit>`, as the `api` trigger).
- `company`: RB2B named only the company (no first name, a /company/ LinkedIn page). Starts the
  same run, which judges the company and looks for people there (see
  [A company-only visit](#a-company-only-visit)).
- `test`: RB2B's "RB2B Test Payload"; only the card's line changes.

A run that can't start (credits, a member who left) keeps its reason on the visit and on the
card; the project's next visitor tries the waiting ones again, up to three, from the last week.

## The run

`people.rb2b_visitor`, started only from the webhook (`_rb2b`), steps:

1. **Record** (`rb2b_record`): the person by their LinkedIn profile, plus RB2B's email when it
   is real, with name, headline ("Title at Company"), company (with its website), position and
   location wherever nothing else set them. A forgotten person, or one without a readable
   profile, goes no further.
2. **Wait** until ten minutes after RB2B's time, so PostHog has the visit (a Temporal timer).
3. **Enrich** (`rb2b_enrich`): the visit in the project's PostHog connection: the session on
   that page within -60/+120 s of RB2B's time (city breaks a tie), its pages and time, how it
   arrived (ad and keyword, search, AI assistant, social, referral, direct), other visits in
   90 days, how the person first arrived, and a replay link when there's a recording. It
   becomes a `visited` event (key `rb2b:<person>:<minute>`, the same the RB2B Slack reader
   used) and the person's "On the site", summary and "Came from". Without PostHog, or when it
   doesn't answer, the event says so; the visit is kept either way.
4. **Rate** (`rb2b_rate`): the shared ICP rating for this person only (fit 1 to 4, intent hot,
   warm or cold), with web search. Skipped without an ICP file or `GEMINI_API_KEY`.
5. **Post** (`rb2b_notify`): a visitor rated 4 or 3 gets a message of their own in the
   project's Slack channel (see [Slack](#slack)). Others wait for the digest.
6. **Publish** (`rb2b_publish`): `people/rb2b-visitors.md`, the 100 latest visitors with their
   ratings, replaced each run; the run's line is the visitor and their rating ("Jane Doe,
   Founder at Acme: 4 · Strong fit, warm.").

A company-only visit takes its own steps after **Record**; see below.

Temporal history holds the run ID, the visit's outcome and the wait. The payload, the person
and the visit stay in Postgres. Project deletion removes the hook, the visits and the run's
receipts.

## A company-only visit

RB2B often matches a visit's network to an organisation and to nobody in it. The same run then
(`rb2b_company.py`, `rb2b_company_activities.py`, behind the `rb2b-company-v1` patch):

1. **Judge** (`rb2b_company_judge`): the ICP model (`people.icp`, Gemini 4 Neon, web search
   on) reads the ICP file, what RB2B said (name, website, industry, size, revenue, the page and
   referrer) and the people People already holds at that company, with their rating and
   whether the team marked them a customer. It decides whether the company is worth chasing,
   rates it on the person scales (fit 1 to 4, intent) and names the job titles to look for.
   The model searches the web and the visit is the visitor's to write, so it reads the page
   only as a path of at most 120 characters on the project's own site (the product address of
   its Start here plan; elsewhere, none), where the visit came from only as a channel label
   ("Organic search: Google", "Referral: another site"), and every URL in what it reads cut to
   host and path. The prompt calls all of it untrusted data.
   Code skips nothing: an internet provider's or hosting network, a company too small, an
   existing customer are the model's call, and its reason says which. A company judged in the
   last seven days (same website) keeps that verdict and its people: nothing is judged,
   bought or posted again. Without an ICP file or `GEMINI_API_KEY` it isn't judged.
2. **Find** (`rb2b_company_find`, only when worth chasing): through
   [Delegate](https://dlgt.io) with Tin's `DELEGATE_API_KEY`:
   - QuickEnrich's contact finder by the company's domain and the model's titles (free);
   - when that lists nobody with a LinkedIn profile, one ContactOut people search (three
     people at most, $0.10 each);
   - with more than three candidates, the model picks up to three to contact (no web search);
   - an Apify LinkedIn profile read of those (under a cent each) keeps only people whose current
     position is at the company. A read that doesn't answer keeps them unchecked.

   Every LinkedIn URL comes from a data service, never from the model. Each order has a receipt
   whose key is also Delegate's idempotency key, so an order repeated after a crash is the same
   order; a refused quote buys nothing and Delegate refunds a failed job. The people kept go
   into People (source `rb2b`) with a `visited` event (`source: rb2b_company`) saying the visit
   was matched to their company, not to them.
3. **Rate** (`rb2b_company_rate`): each person by the shared ICP rating.
4. **Post** (`rb2b_company_notify`): a company worth chasing rated 4 or 3 gets its own Slack
   message (below); the rest go in the digest.

Runaway guards, since Tin funds all of it and a leaked webhook address must not run up its
bill:

- at most 200 company runs per project a day (`rb2b_api.COMPANY_RUNS_PER_DAY`), counted by
  when each visit claimed its run (`claimed_at`); the count and the claim happen in one
  transaction under a per-project lock. A company visit over it is kept, marked `no_start` and
  never started, not even by the retry of waiting visits;
- at most $5 of Delegate orders per project a day (`rb2b_company.DELEGATE_USD_PER_DAY`, summed
  from the order receipts of the last 24 hours). Each order first reserves its cap in its
  receipt, in one transaction under a second per-project lock, so runs at once count each
  other's reservations; the receipt then settles to the quoted price (nothing when refused or
  refunded). A company that would pass it is still judged and posted; its run's line says the
  people search was skipped for today's limit;
- no backfill: migration 089 marks every company visit received before it `no_start`.

The verdict, the people kept, how many the services listed, how many had left and what
Delegate charged are on the visit (`rb2b_visits.company`, migration 089). The run's line says
the verdict ("RB2B named only Acme: worth chasing (4 · Strong fit); 2 people to contact
there."). Without `DELEGATE_API_KEY` a company is still judged and posted, with nobody found.

## Phone numbers for leads

A person rated a good or strong fit (3 or 4) with a LinkedIn profile, no phone and no
`customer: yes` gets one lookup (`lead_phones`): Delegate's `contactout-linkedin-phone`
($0.15, not billed when ContactOut has no number). It runs right after the rating in
`rb2b_rate` and `rb2b_company_rate`, and in `people.rate` after its rounds, so earlier leads are
looked up too. The numbers go into the person's `phone` value, shown as a Phone row in People's
panel with ContactOut's count; ContactOut doesn't say which number is mobile or current. A
member's own phone is never replaced.

Each person is looked up once, ever: the receipt is keyed `phones:<project>:<person>`, and its
key is also Delegate's idempotency key. The receipt holds the order and its price, never the
numbers, so forgetting a person removes them. A daily guard of $20 a project
(`lead_phones.USD_PER_DAY`) holds further lookups for a later run, reserved under a
per-project lock like company orders. Without `DELEGATE_API_KEY` nothing is looked up.

## Slack

Operator-configured, for Tin's managed clients. The posts go to the channel Tin shares with the
client in Tin's own Slack workspace, through Tin's own Slack connection: the one of the project
`TIN_LITE_RB2B_SLACK_PROJECT_ID` names (Tin Computer in production). An operator sets each
client project's channel:

    tin-lite rb2b-channel <project_id> <channel_id> --name tin-databasin-notifications
    tin-lite rb2b-channel <project_id> none

Tin's bot has to be in a private channel to post there. Founders don't see or set any of this;
the RB2B card shows nothing about Slack. `rb2b_slack.destination` is the one place that reads
it; with the setting unset or no channel, nothing posts. Links in a message are only a LinkedIn
profile rebuilt from the person's identifiers, a PostHog replay of People's own shape, and the
person in Tin built from the app's address and IDs.

- **A good or strong fit, at once.** 🟢 strong fit or 🟡 good fit with the intent (🔥 hot,
  🌤 warm, 🧊 cold); who they are, linked to LinkedIn, with their headline; the company,
  its website and where the visit came from; the rating's reason; the visit as PostHog read
  it; a ⚠️ line when PostHog didn't confirm the visit; buttons for LinkedIn, the replay
  (when there is one) and the person in Tin.
- **A company worth chasing, at once.** The same layout as a person's (one builder,
  `rb2b_slack._message`): the fit and intent with the next step, the company's name, its
  website, industry, size and location in small text, the model's reason, the warm line when
  there is one, the page visited and a ⚠️ line saying who visited is unknown in small text, then
  each person found (their name linked to their LinkedIn profile, headline and fit) and a
  button to People.
- **Everyone else, at 18:00 in the project's time zone.** One digest of the visitors since the
  previous one who rated weak, not a fit, or weren't rated, grouped, and the companies without
  a message of their own (not worth chasing, weak, not judged, visited again), with a button to
  People.
  A day with nobody posts nothing. A small loop in the app (`rb2b_slack.digest_loop`) checks
  every five minutes; a digest is keyed by its local date, so it goes once.
- **Once only.** Every message is recorded as `sending` in `rb2b_slack_posts`, and committed,
  before Slack is called. A refused post is recorded as failed and the run goes on; a row left
  `sending` may be in the channel, so it becomes `unknown` and is never sent again.

## Warm paths

After rating, `rb2b_warm` looks for a way in through the LinkedIn networks of the project's own
members, never another project's (`warm_paths.py`). The good or strong fit's Slack message shows
the closest path as one line, and nothing when there's none:

1. **Already connected** (free): a member's `connected` relationship with the visitor in People.
   `🤝 Ege is already connected`.
2. **Second degree** (one LinkedIn read): for a good or strong fit with no direct connection,
   one search of a member's second-degree network for the visitor's name, through their saved
   session in the existing search sandbox (`kind: search`, document transport). The visitor's
   card is the one with their profile, or the one namesake whose headline names their company;
   its "X, Y & N other mutual connections" gives `🤝 2nd degree via Mira Chen + 11 others`.
   The run starter's session (the member who set up the webhook) is tried first, then any
   other member of the project with cloud reading on: every member's network serves the
   project's warm paths by default, unlike Tin's other LinkedIn reads, which use only their
   starter's. Managed clients are told this when Tin sets them up. It never crosses projects. The
   read is receipted before the sandbox starts and never bought twice; one that can't run
   leaves the message without the line. Searches are capped at
   `warm_paths.SEARCHES_PER_DAY` per project per day as a runaway guard.
3. **Someone at their company** (free): the members' first-degree connections whose current
   `company` (from `social.linkedin_connections`' profile reads) matches the visitor's, compared
   without case, accents, punctuation or legal suffixes.
   `🤝 Ege knows Mira Chen (Treasury) + 1 more at Valley Bank`.

A company-only visit gets the third line too, with the company RB2B named: who on the team
knows someone there, from the same connections. It's read when the message is written.

Members are named by their Tin first name; without one, the line says "Your team". The result
stays in the step's receipt; Temporal carries the run ID only. Tiers 1 and 3 need a member to
have run `social.linkedin_connections`; tier 2 needs cloud reading on a member's LinkedIn card.

## Billing

Tin funds it (`tin-funded-rb2b-visitors-v1`), as it does the LinkedIn reads' rating: the
PostHog reads, the model calls and Delegate's orders cost the founder nothing. Model usage is
still recorded per call; Delegate's charge is on the visit and in each order's receipt. A
company worth chasing costs about $0.10 to $0.15 in model calls and up to $0.35 at Delegate
(nothing when the free finder finds people).

## Next

Setting the channel from the RB2B card once posts are offered beyond Tin's clients.
