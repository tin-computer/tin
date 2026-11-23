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
5. **Publish** (`rb2b_publish`): `people/rb2b-visitors.md`, the 100 latest visitors with their
   ratings, replaced each run; the run's line is the visitor and their rating ("Jane Doe,
   Founder at Acme: 4 · Strong fit, warm.").

Temporal history holds the run ID, the visit's outcome and the wait. The payload, the person
and the visit stay in Postgres. Project deletion removes the hook, the visits and the run's
receipts.

## Next

A conditional Slack post at the end of the run (who visited, how they rate) when the rating
clears a project's bar, in a format still to settle; and a workflow that finds someone to
contact at a company-only visit.
