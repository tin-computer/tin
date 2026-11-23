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

## What RB2B posts

A flat JSON object with spaced keys ("LinkedIn URL", "First Name", "Title", "Company Name",
"Website", "City", "State", "Seen At", "Captured URL", "Referrer", "Tags", …). Tin reads empty
strings as missing, "Seen At" leniently (RB2B's own sample isn't ISO 8601; no zone is UTC), and
the repeat flag as either `is_repeat_visit` or `is_repeat_visitor`.

`POST /webhooks/rb2b/{token}` always answers 200 with an empty body, within RB2B's 15 seconds,
for a token nobody has and a body that isn't JSON too: RB2B disconnects an address on anything
else. It keeps the payload in `rb2b_visits`, once per profile, time and page, as one of:

- `person`: starts the visitor's run (start key `rb2b:<visit>`, as the `api` trigger).
- `company`: RB2B named only the company (no first name, a /company/ LinkedIn page). Kept for
  the workflow that will find someone there; nothing runs yet.
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

Temporal history holds the run ID, the visit's outcome and the wait. The payload, the person
and the visit stay in Postgres. Project deletion removes the hook, the visits and the run's
receipts.

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
- **Everyone else, at 18:00 in the project's time zone.** One digest of the visitors since the
  previous one who rated weak, not a fit, or weren't rated, grouped, with a button to People.
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

Members are named by their Tin first name; without one, the line says "Your team". The result
stays in the step's receipt; Temporal carries the run ID only. Tiers 1 and 3 need a member to
have run `social.linkedin_connections`; tier 2 needs cloud reading on a member's LinkedIn card.

## Billing

Tin funds it (`tin-funded-rb2b-visitors-v1`), as it does the LinkedIn reads' rating: the
PostHog reads and the rating call cost the founder nothing, and the usage is still recorded
per call.

## Next

Setting the channel from the RB2B card once posts are offered beyond Tin's clients, and a
workflow that finds someone to contact at a company-only visit.
