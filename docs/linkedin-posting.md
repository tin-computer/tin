# LinkedIn posting

Tin posts to LinkedIn through Zernio (zernio.com), on Tin's own Zernio account. Zernio
holds LinkedIn's app approvals for member and organization posting, so Tin needs no
LinkedIn app review. Publishing reuses X's path: an exact preview, an explicit
confirmation and a receipt around every write.

## Connect

The **LinkedIn posting** card (`social.linkedin`) is separate from the **LinkedIn** card
(`network.linkedin`), which collects connections through the browser extension.

1. **Connect** finds the project's Zernio profile by its name, or makes it. There is one
   profile per project, named `tin:<project id>`, with the project ID as its `externalId`.
2. The founder signs in to LinkedIn through Zernio's hosted connect and picks where Tin may
   post: themselves, the pages they administer, or both. Each pick is its own Zernio
   account in the project's profile. The consent covers posting, analytics and comments,
   so later reads need no second sign-in.
3. Zernio returns to `/integrations/callback/linkedin`, carrying Tin's attempt state. Tin
   checks the signed-in member and the attempt, then reads the accounts from the project's
   profile. It ignores any account IDs in the callback's query.

The connection row keeps the profile ID and each account's Zernio ID, LinkedIn URN, kind
and name. Tin stores no LinkedIn credential, because Zernio holds the tokens and refreshes
them. **Check again** reads the accounts again; a profile with no active account needs
reconnecting. **Add accounts** runs the same connect, and Zernio refreshes accounts it
already has.

### Whose profile

Any member may post as a connected page. A personal profile is its owner's alone: no member
posts as another member's profile, over the dashboard or MCP.

- Connect notes the accounts already in the project's profile, and when each one's token
  expires, before the sign-in. A personal profile the sign-in adds, or signs in again so its
  token changes, belongs to the member who signed in. The connection keeps this as `owners`
  (LinkedIn URN to Clerk user), and a later connect or **Check again** keeps it.
- A profile that was already there, has no owner and wasn't signed in again, such as one left
  by a cleanup that didn't finish, belongs to nobody, and nobody posts as it.
- Members connect one at a time: while one member's attempt is open, another's is refused.
  Finishing a connect closes the member's other open attempts.
- A connection from before `owners` was recorded gives its profiles to the member who
  connected it.
- A mapped staging profile has no sign-in, so the member who connects it owns its profiles.

## Drafts

A draft is an ordinary project file at `social/linkedin/drafts/<date>-<slug>.json`:

```json
{
  "schema_version": "tin.social.linkedin_draft.v1",
  "posts": [
    {
      "id": "p1",
      "text": "The exact post",
      "target": "urn:li:organization:108774412",
      "readiness": "ready",
      "support": [{"source_path": "notes/release.md", "excerpt": "What the post rests on"}],
      "editor_notes": "Never published",
      "attachments": [{"type": "image", "path": "social/linkedin/media/shot.png", "alt_text": ""}],
      "missing_assets": []
    }
  ]
}
```

It is X's shape with a per-post `target`, the LinkedIn URN of one connected account, and
LinkedIn's limits: up to six posts, text within 3,000 characters counted in UTF-16 units as
LinkedIn counts them, and up to 20 PNG or JPEG images within Tin's media checks, in a file
within 256 KB. Video, documents and polls come later. An empty `target` posts as the only
account the member may use, and is refused when there are several.

A drafting run also keeps four optional fields with its posts; a hand-written draft needs
none of them:
- `gaps`: what it couldn't support, each an `angle` and a `reason`. The reader lists them
  as **More context needed** beside the posts, and on their own when a run wrote no posts.
- `author_id`: whose voice the posts use.
- `parent`: the batch and post a revision started from (`path`, `sha256`, `post_id`).
- `sources`: the material the batch drew on, so a revision checks against it rather than
  whatever the source file holds by then.

A file under `social/linkedin/` opens in LinkedIn's reader (`static/linkedin-posts.js`,
drawn from Paper boards LI-07A to LI-15); X's drafts keep their own.
- The batch's posts are listed beside the one open, each with its status; without room for
  the list, the title opens it.
- **Draft notes** shows each labelled line of `editor_notes` (`Angle: …`, `Format: …`) as a
  row, the rest as notes, and each supporting excerpt under its source's label.
- **Edit post** edits in place. **Posting as** is a picker only when there's a choice: the
  member's own profile and the project's pages. It says when the post's account left the
  connection or is another member's profile (`unavailable` in the read).
- Images are rows with their alt text. They reorder by dragging or with the arrow keys on
  the grip. Files that aren't PNG or JPEG, or are over 5 MB, are refused before anything is
  sent and stay listed with the reason until removed. Uploads show their progress and go to
  `social/linkedin/media/`; images already in Files can be added from a filtered list.

## Publish

1. **Preview** (`POST /api/projects/{id}/linkedin/preview`, `preview_linkedin_post`) freezes
   the exact text, each image's path, revision and SHA-256, and the connected account it
   posts as. The token lasts 15 minutes.
2. **Publish this exact post** (`…/linkedin/publish`, `publish_linkedin_post`) re-reads the
   draft and refuses if anything changed. It records the approval
   (`linkedin:approved:<approval>`), which names the member who confirmed, and starts
   `social.linkedin_publish`. Only the run ID enters Temporal.
3. The run uploads each approved image to Zernio's storage once (`linkedin:media:*`), reads
   the profile's accounts again, checks that a personal profile still belongs to the member
   who confirmed, and sends the post with `publishNow` under the receipt
   `linkedin:post:<approval>`, written before dispatch.
4. It waits for LinkedIn's answer and keeps it, published or refused, in the run's
   `<run>:linkedin_post` receipt. A published post gets `reports/linkedin/<run>.md` with its
   link.
5. The reader follows the run through `GET …/linkedin/publications?path=`, which
   `read_linkedin_drafts` also returns as `publications`. Each post's latest publication is
   `publishing`, `published` with its URL, `not_published` when nothing reached LinkedIn or
   LinkedIn refused it, or `uncertain` when a send's answer never came. An uncertain post is
   never called unpublished: the reader asks the founder to check the page.

The approval ID derives from the post's fingerprint, so confirming the same post twice
starts one run. A run that ended without sending anything doesn't use the post up:
confirming it again tries once more.

**A lost answer.** Zernio returns the original post for a repeated `Idempotency-Key` within
24 hours, and Tin's key derives from the approval. A post whose answer was lost is sent
again with the same key for 23 hours; after that the run stops and asks the founder to check
the page. A refusal from Zernio made nothing, so its receipt is dropped and the reason shown.
A post LinkedIn rejected after Zernio accepted it, for example as duplicate content, fails
the run with LinkedIn's category in Tin's words.

MCP: `read_linkedin_drafts`, `save_linkedin_draft`, `preview_linkedin_post` and
`publish_linkedin_post`. Each returns the browser editor's link for uploading images; MCP
never takes image bytes.

## Listening

Tin reads how each post did and keeps the numbers itself, because Zernio returns a post's
numbers live but keeps no hourly history. Each reading has impressions, reach, reactions,
comments, shares, clicks (pages), saves and sends (profiles) and LinkedIn's engagement rate.

- **`social.linkedin_listen`, one run per post Tin publishes.** Once a post is out,
  `social.linkedin_publish` prepares this run and starts it as an abandoned child (patch
  `linkedin-listen-v1`, so publish runs from before replay unchanged). Temporal timers wait
  until 1, 4, 24 and 72 hours after posting, and each wake reads the post once. A reading
  LinkedIn doesn't answer is retried five times over about 15 minutes, then skipped with its
  reason. A post deleted on LinkedIn ends the run. Activity shows one row per post. Nobody
  else starts it: a start without the publication refuses.
- **`social.linkedin_results`, daily.** It runs on demand or on a daily schedule, at 08:00 by
  default. It reads every tracked post published in the last 30 days, as `d<N>` for N days
  after posting, and each connected account's follower count. It also picks up page posts made
  directly on LinkedIn, and posts Tin published whose listening run never started. Their
  early readings have passed, so they start with a daily one.

A personal profile's numbers exist only for posts published through Zernio, so listening
covers page posts, Tin's and those made on LinkedIn, and personal posts Tin sent. An account
whose analytics LinkedIn refuses asks the founder to reconnect LinkedIn posting; the daily run
still reads the other accounts and names the refused one.

**The record.** Postgres keeps it (`065_linkedin_listening.sql`):
- `linkedin_posts`: the post, its account, publish time, publish run, and the draft path and
  post ID it came from;
- `linkedin_post_readings`: one row per post and checkpoint, `1h`, `4h`, `24h`, `72h` or
  `d<N>`. A checkpoint keeps its first reading; a daily one keeps that day's latest;
- `linkedin_follower_counts`: one row per account and day.

Deleting the project purges all three.

**The files.** After each reading Tin rewrites two files from that record:
- `social/linkedin/results.json` (`tin.social.linkedin_results.v1`), for agents and Learnings:
  each account's followers and change over 30 days, and for each post its URN, link, account,
  source (`tin` or `linkedin`), draft, excerpt, the 1h, 4h, 24h, 72h, d7 and d30 readings,
  and the latest one.
- `social/linkedin/RESULTS.md`, the same for reading.

Both are Tin's own: each write replaces the last, and edits to them are replaced. They list the
60 most recent posts from the last 90 days; every daily reading stays in Postgres. Writes are
serialized per project. The rendered bytes wait in the write's receipt until the commit lands,
so a retry after a lost answer reconciles the same bytes.

Both workflows read only accounts in the project's own profile, make no model calls and are
included in billing like posting, since Tin pays Zernio. The catalog lists them only for
projects in `TIN_LITE_LINKEDIN_POSTING_PROJECTS`.

## Boundaries

- `TIN_LITE_ZERNIO_API_KEY` stays on the switchboard. It never reaches a sandbox, package,
  project file, log or Temporal history.
- One key acts on every project's LinkedIn. The server picks the profile and account from
  the project, never from a request.
- One LinkedIn sign-in can post as every page the member administers. Tin posts only as
  the accounts connected into the project's profile, by Zernio account ID, and never names
  an organization of its own.
- Zernio bills Tin for each connected account each month. The card is shown only to the
  projects in `TIN_LITE_LINKEDIN_POSTING_PROJECTS`.
- **Disconnect** removes the project's accounts and then its profile from Zernio, and so
  does deleting the project. A failure there is logged; the local disconnection stands.
  Deleting a project also purges its `linkedin:*` receipts.
- Zernio's upload URLs must be on its R2 storage host and the images it serves on
  `media.zernio.com`; the key is never sent to the storage host.

## Staging

Staging and production share Tin's Zernio account, so staging never creates a profile or
connected account of its own. `TIN_LITE_ZERNIO_PROFILE_MAP` (`<project id>=<profile id>`,
comma-separated) points a staging project at an existing profile. Connect then attaches
that profile's accounts without a LinkedIn sign-in, and disconnect never changes the
profile in Zernio. Without a mapping, staging refuses to connect.

## Setup

| Setting | Value |
|---|---|
| `TIN_LITE_ZERNIO_API_KEY` | Tin's Zernio API key |
| `TIN_LITE_LINKEDIN_POSTING_PROJECTS` | Comma-separated project UUIDs that see the card |
| `TIN_LITE_ZERNIO_PROFILE_MAP` | Optional; required on staging |

Apply `064_linkedin_posting.sql`, which adds `social.linkedin` to the integration tables'
provider checks, and `065_linkedin_listening.sql`, which adds listening's three tables.

## Checked against Zernio

Before building, a connection on Tin's own company page confirmed:
- hosted connect and the page picker;
- presigned media upload;
- `Idempotency-Key` replay;
- the 24-hour duplicate-content refusal;
- deleting a scheduled post;
- reading page comments.

A member's own posts are not readable through Zernio, so reading the founder's own posts
stays with the extension.

For listening, read-only calls on the same page and a founder's profile confirmed:
- per-post analytics for a page post made on LinkedIn;
- lifetime totals for a profile;
- `post_not_found` for a deleted post;
- the page's recent posts;
- current follower counts, with no history before the connection.

Fixture tests use a synthetic Zernio (`tests/test_linkedin_posting.py`,
`tests/test_linkedin_publish.py`, `tests/test_linkedin_listening.py`, which also runs the
timetable on a time-skipping Temporal). `web/linkedin-posts.browser.test.js` and
`web/linkedin-reader-page.browser.test.js` cover the reader.
