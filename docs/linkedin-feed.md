# Save your LinkedIn feed

`social.linkedin_feed` saves the posts a member's LinkedIn home feed shows them, for the
project's content work. Each post keeps its author, words, media, counts and why LinkedIn showed
it; a post new to the project also gets its first comments and its first reactors. Each run
writes its posts to `social/linkedin/feed/` in Files. Nothing here rates or scores anyone, and
nobody is added to People.

## Who can run it

The same as [connections export](linkedin-connections.md):
- **Projects:** every project, as for [connection collection](connection-collection.md).
- **Runtime:** the LinkedIn cloud runtime, with an image that has the `linkedin_feed` and
  `linkedin_feed_details` runner kinds.
- **Member:** each member runs it with their own LinkedIn connection, with cloud reading on and
  a live session from Tin for Chrome. A run reads the feed of the member who started it. Each
  member saves their own copy, once; its daily schedule runs as them
  ([personal workflows](connection-collection.md#connect-once-then-run-from-tin)).

## Inputs

- `pages` (1 to 30, default 10): feed pages to read, ten posts each.
- `reactors` (0 to 100, default 10): how many of each new post's reactors to keep. 0 keeps none.

## What LinkedIn shows

All three are GETs on `www.linkedin.com`, the requests LinkedIn's web app makes, and need no
query ID from the browser (checked on a real account, 25 November 2026):
- **The feed:** `GET /voyager/api/feed/updatesV2` with `q=feed`, ten updates a page. The next
  page sends `start` and the `paginationToken` the last page returned (it expires after an hour).
  Each update carries a post: the actor (a person's profile ID, name, headline and degree, or a
  company), the commentary, the media, a header saying why it's there ("Ada likes this",
  "Suggested", "Hugging Face reposted this") and the social detail with its counts.
- **Comments:** `GET /voyager/api/feed/comments` with `q=comments`, `sortOrder=RELEVANCE` and
  `updateId` set to the post's thread, ten at a time. Each comment has its author, words, time
  and its own reaction and reply counts.
- **Reactors:** `GET /voyager/api/voyagerSocialDashReactions` with `threadUrn=urn:li:<thread>`,
  as [engagers](linkedin-engagers.md) reads them. The share URN answers with no one; the thread
  must be the social detail's own (`activity:` or `ugcPost:`).

Several updates can carry one post: someone liked it, a company reposted it. Tin names a post by
its thread, the ID its comments and reactions hang off, and dates it by its own activity: the
thread when that is an activity, else the activity that showed it (a ugcPost's ID can be days
older, from a draft). An update Tin can't name a post for is counted, not guessed at.

## What a run does

1. **Prepare:** the session and owner checks, as for engagers.
2. **Read the feed:** reads of up to three pages, with a random 3 to 9 seconds before every
   request, until the run's `pages` are read or the feed ends. Each post is kept once per project
   (`linkedin_feed_posts`, by thread); a post seen before has its words and counts refreshed.
   Each feed's sighting is kept separately (`linkedin_feed_sightings`: whose feed, which run,
   where and why).
3. **Read details:** for the posts this run found that Tin hasn't read before, feed order first,
   the first page of comments (when the post has any) and the first `reactors` reactors (when it
   has reactions). Each read takes up to 24 requests. Promoted posts are kept without details.
   A post seen again later keeps the details read the first time.
   - Posts an earlier run left without details in the last three days (it stopped, or ran out of
     reads) come next, newest first, whichever member's feed showed them.
   - One post's server error, timeout or unreadable answer skips that post (`unread`), not the
     read. A whole read whose answer failed skips its posts, under its own receipt, and the next
     read goes on. A refusal from LinkedIn still stops all reading.
   - Skipped posts are settled without details, so they aren't asked for again; the summary
     says how many.
4. **Publish:** it writes two files, created once per run:
   - `social/linkedin/feed/<date>-<run>.json`: every post the run's feed showed, in feed order,
     with `position`, `reason`, `comments` and `reactors` (schema `tin.social.linkedin_feed.v1`);
   - `social/linkedin/feed/<date>-<run>.md`: a digest to read, with each post's link, counts,
     opening lines and first comments.

A read LinkedIn objects to (a challenge, a limit, a signed-out session, a changed account, a
refusal) stops reading and keeps what was read; posts whose details weren't read get them on a
later run. On the first feed read, it fails the run with what to do.

A daily run of the defaults makes about 4 feed reads and, on a typical day, 5 to 10 detail reads:
roughly 10 feed requests and up to 150 detail requests, all paced.

## Receipts and history

As for [connections export](linkedin-connections.md#receipts-and-history): each read has an
effect receipt keyed on the run and the read's number. It holds LinkedIn's answer only until
those posts are written, then keeps counts and where the next feed page starts. Temporal history
holds read numbers and counts. Project deletion removes the posts, sightings and receipts.

## Billing

Included, with no charge to the founder, under `tin-funded-linkedin-feed-v1`. Tin pays the
sandbox reads, and the usage is still recorded per call.

## Not yet

- **Replies to comments** aren't read, only the first page of top-level comments.
- **Counts over time:** a post seen again has its counts replaced, not kept as a series.
- **Forgetting a person** doesn't remove their posts or comments from feed posts; deleting the
  project does.
- **MCP and the dashboard** show the run and its files only; there is no feed view yet.
