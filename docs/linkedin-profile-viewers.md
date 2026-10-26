# People who viewed your LinkedIn profile

`social.linkedin_profile_viewers` reads who viewed the member's LinkedIn profile, newest first,
with their saved session. It keeps each named viewer as a [People record](people-records.md)
and scores them against the project's ICP file with the [engagers](linkedin-engagers.md) scorer.
Viewers who fit and aren't connected join the member's
[connection requests](linkedin-requests.md) in Decisions. The list goes to project Files. The
workflow reads only and sends nothing. It runs daily by default (08:00 in the founder's time
zone), or on demand.

## Who can run it

The same as engagers:
- **Projects:** the [connection collection](connection-collection.md) allowlist
  (`TIN_LITE_CONNECTION_COLLECTION_PROJECTS`). The workflow is hidden elsewhere, and a start is
  refused.
- **Runtime:** the LinkedIn cloud runtime, with an image that includes the viewers reads, and
  the native model service.
- **Member:** each member runs it with their own LinkedIn connection, with cloud reading on and
  a live session from Tin for Chrome. A run reads the profile of the member who started it.
  Each member saves their own copy, once; its daily schedule runs as them, and only they can
  change it ([personal workflows](connection-collection.md#connect-once-then-run-from-tin)).

## What LinkedIn shows

- **The list** on the profile-views page (`/analytics/profile-views/`), newest first, ten at a
  time. The page is server-driven UI: it loads the list with two read-only requests, and Tin
  makes the same two with the saved session:
  - the first page: `POST /flagship-web/rsc-action/actions/server-request?sduiid=WvmpEntityList`,
    with the page's fixed body (newest first, no date filter);
  - each next page: `POST /flagship-web/rsc-action/actions/pagination?sduiid=com.linkedin.sdui.premium.wvmp.entityList`,
    with `{pagerId, clientArguments, paginationRequest}`. `paginationRequest` is the request for
    the next page that the previous page named, passed back unchanged.

  Answers are React Server Components rows. Each viewer is an item tagged `viewer-list-item`:
  - their name;
  - the degree ("• 2nd");
  - the headline;
  - "Viewed 3d ago";
  - a link to their profile: `/in/<slug>/`, or `/in/<profile ID>/`.

  Someone browsing privately ("Someone at a software company") has no link: they're counted,
  not recorded.
- **The overview**, `GET /voyager/api/identity/wvmpCards`: LinkedIn's highlights of the last 90
  days (the latest viewers, notable ones, by company and by title). Each named viewer comes with
  their profile ID, degree and exact view time, and the overview carries LinkedIn's count of
  views. It's a sample, so it's read once per run, before the list.
- **Premium:** a Premium member's page lists everyone who viewed them. A free account's page
  lists only the latest few, so a daily run is what catches its viewers over time. Tin reads
  only what the page shows the member.

## What a run does

1. **Prepare:** the session, owner and runtime checks, and the ICP file, as engagers does.
2. **Read:** sandbox reads (`linkedin_viewers`), with a random 3 to 9 seconds before every
   request; the first read takes the overview too. A member's first run reads up to ten pages
   a read; once their viewers were read before, two, since a day's views fit and the run reads
   on while it finds new ones.
   - Each viewer is upserted by their profile ID, their slug, or both. A slug is kept decoded
     and lowercased, as People keeps slugs, so the overview's ID and the list's slug name one
     person.
   - Each viewer gets their name and headline, and the starter's relationship from their degree.
   - A `viewed` event is added, timed from "… ago" against the read's time (or the overview's
     exact time).
   - **One view, counted once.** A view LinkedIn shows again (the next day's "2d ago", or the
     same view in the overview and the list) counts once. A second view counts only when no
     view by the same person is recorded within the age's span: a day for minutes, hours and
     days, four days for weeks, 15 for months.
   - A forgotten person is skipped.
3. **When reads stop:**
   - at the first read that adds no new view, since the rest is older;
   - at the end of the list;
   - after three reads (30 pages on a first run, six after), which bounds every run.

   A read LinkedIn objects to (a challenge, a limit, a signed-out session, a changed account, a
   refusal) stops reading and keeps what was read. On the first read, it fails the run with what
   to do.
4. **Score:** the engagers scorer and route (`linkedin.engagers.icp`), under the same ICP
   definition. Anyone with a reaction, a comment or a view is scored once, whichever workflow
   found them.
5. **Look up profile IDs** (`linkedin_profile_ids`): sending a request needs a profile ID.
   - Who: viewers who fit (0.6 or more), aren't connected, aren't customers and are known only
     by their slug.
   - How: up to 20 per run, one read each of LinkedIn's profile for that slug, paced like the
     other reads.
   - Each ID found is added to the person's record, with where the member stands with them.
   - It doesn't run after LinkedIn objected.
6. **Who accepted:** about once a day, one read of where the member stands with up to 20 people
   whose [connection requests](linkedin-requests.md#who-accepted) wait for an answer. The
   summary says how many accepted. This daily run is what reads it, so the sending run can
   finish once its requests are out. It doesn't run after LinkedIn objected.
7. **Propose and publish.** Newly eligible viewers join the member's open batch in Decisions,
   under the same rules as people who reacted. A member who turned on
   [approving automatically](linkedin-requests.md#approved-automatically) has the batch
   approved right after. Then it rewrites:
   - `people/linkedin-profile-viewers.csv`: everyone who viewed the member, best fit first, with
     when they last looked and how many times;
   - `people/linkedin-profile-viewers.md`: counts, LinkedIn's total of views, private viewers,
     and the best fits not yet connected.

## When the page changes

The list is LinkedIn's page format, not an API. When the reader can't recognise it, it doesn't
guess. That covers: no viewer items, a named viewer without "Viewed … ago" (a page in another
language), the page refusing its own request, or a next-page request it doesn't know. The read
then:
- reports `viewers_format_changed`;
- keeps what the overview named;
- stops paging.

The run still succeeds, and its file says it read only the overview; the founder's summary line
doesn't put it to them, since there is nothing for them to do. The reader supports English pages
today.

## Receipts and history

- **Sandbox reads.** Every read has an effect receipt keyed on the run and the read's number.
  - It holds LinkedIn's answer only until those people are written.
  - It then keeps counts, plus LinkedIn's request for the next page, which the next read takes
    from there.
  - A read that started but never answered is cleaned up and never bought again.
- **Model calls.** Scoring batches have their own receipts, as for engagers.
- **Temporal history** holds read numbers and counts. Names, headlines, scores and the
  next-page request stay in Postgres.
- **Project deletion** removes the records and these receipts.

## Billing

Included, with no charge to the founder, under `tin-funded-linkedin-profile-viewers-v1`. Tin
pays the sandbox reads and the scoring calls, and the usage is still recorded per call.

## Not yet

- **Pages in other languages** fall back to the overview.
- **Teammates aren't left out automatically**, as for engagers.
- **One read per account:** a member on two projects is read once for each.
