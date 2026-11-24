# Export your LinkedIn connections

`social.linkedin_connections` exports the member's first-degree LinkedIn connections, with
each connection's current company and position, to `people/linkedin-connections.csv`.
LinkedIn's own data export drops email addresses, current companies and job titles from
`Connections.csv` from December 2026; this workflow keeps the companies and positions.
Every connection is also kept as a [People record](people-records.md).

## Who can run it

The same as [profile viewers](linkedin-profile-viewers.md), without the model service:
- **Projects:** every project, as for [connection collection](connection-collection.md).
- **Runtime:** the LinkedIn cloud runtime, with an image that has the `linkedin_connections`
  and `linkedin_positions` runner kinds.
- **Member:** each member runs it with their own LinkedIn connection, with cloud reading on and
  a live session from Tin for Chrome. A run reads the connections of the member who started
  it. Each member saves their own copy, once; its daily schedule runs as them
  ([personal workflows](connection-collection.md#connect-once-then-run-from-tin)).

## What LinkedIn shows

- **The list:** `GET /voyager/api/relationships/dash/connections` with
  `ConnectionListWithProfile-16`, `q=search`, `sortType=RECENTLY_ADDED`, 40 at a time. This is
  the request LinkedIn's My Network page makes. Each connection has `createdAt` (when they
  connected) and the person's profile: profile ID, public slug, first and last name, headline.
  A connection listed without a profile (a closed or hidden account) is counted, not recorded.
- **A profile:** `GET /voyager/api/identity/dash/profiles` with `FullProfileWithEntities-93`,
  about 90 KB. The top card the other readers use names the current company but has no title
  or location (checked on a real account, 23 November). The current position is the most recent position with no end date: its
  company (`companyName`, or the linked company's name) and title. The location is the
  profile's geo name.

## What a run does

1. **Prepare:** the session and owner checks, as for engagers. The member's first run notes
   when Tin first read their list (`linkedin_connection_lists.baseline_at`).
2. **Read the list:** up to 20 sandbox reads of up to ten pages each (8,000 connections),
   with a random 3 to 9 seconds before every request, to the end of the list. Once a run has
   read the member's list to the end (`read_to_end_at`), a later run stops at the first read
   that reaches connections Tin already has: the list is newest first, so the rest are known.
   A daily run then makes one read.
   - Each connection is upserted by profile ID and slug, with their name and headline.
   - The member's relationship is set to `connected`.
   - A `connected` event is added once per member and person, dated `createdAt`. Its detail
     keeps LinkedIn's first and last name for the CSV.
   - A forgotten person is skipped.
   - Someone LinkedIn says connected after the baseline also gets a `new_connection` event
     (see [new connections](#new-connections)).
3. **Read profiles:** up to five reads of 20 profiles each, so a hundred a run. It reads
   connections never read first, newest connection first, then those read longest ago once
   30 days old. A profile LinkedIn doesn't answer is skipped, and the next run reads it again.
   - Company, position and location are written with `refresh`, so the read time is current
     even when nothing changed.
   - A profile with no current position gets an empty company and position with the reason
     "LinkedIn showed no current position", so it isn't read again for 30 days.
   - A value a member set by hand is theirs: the person isn't read again.
4. **Publish:** it rewrites:
   - `people/linkedin-connections.csv`: newest connection first, in the columns of LinkedIn's
     `Connections.csv` (First Name, Last Name, URL, Company, Position, Connected On in
     LinkedIn's `01 Apr 2026` format), plus Headline, Location and Profile read. There is no
     Email Address column.
   - `people/linkedin-connections.md`: counts, how many profiles are still to read and how many
     daily runs that takes.

A read LinkedIn objects to (a challenge, a limit, a signed-out session, a changed account, a
refusal) stops reading and keeps what was read. On the first list read, it fails the run with
what to do. An answer Tin can't read is refused as `unsupported_contract`, never guessed.

## New connections

Everyone connected when Tin first read the member's list is their existing network: People
lists them under Connections, and Tin buys nothing for them. Someone who connects later did
something Tin saw, so their `new_connection` event, dated when LinkedIn says they connected,
counts like a reaction or a profile view (`people_records.SOCIAL`):
- People lists them in All people and Social, not Connections; their panel reads "Became your
  new connection on LinkedIn".
- The ICP rater picks them up the next time it rates the project's people (a LinkedIn read's
  rating step, or `people.rate`), and a good or strong fit gets the
  [phone lookup](rb2b-webhook.md#phone-numbers-for-leads).
- The run's summary says how many connected since the last run.

A member whose list was read before this existed gets the first connection Tin recorded for
them as the baseline (migration 091); their next run reads the list to the end once more.

## Receipts and history

As for [profile viewers](linkedin-profile-viewers.md#receipts-and-history): each read has an
effect receipt keyed on the run and the read's number. It holds LinkedIn's answer only until
those people are written, then keeps counts and where the next read starts. Temporal history
holds read numbers and counts. Project deletion removes the records and these receipts.

## Billing

Included, with no charge to the founder, under `tin-funded-linkedin-connections-v1`. Tin pays
the sandbox reads, and the usage is still recorded per call.

## Not yet

- **Email addresses** are never collected.
- **Removed connections** stay in People and the CSV; the list only adds.
- **Who asked:** a new connection doesn't say whether they asked or the member did, unless Tin
  sent the request (`invited`, then `accepted`). Pending invitations the member received aren't
  read yet.
- **More than 8,000 connections:** only the newest 8,000 are listed, and the summary says so.
