# People who engaged with your LinkedIn posts

`social.linkedin_engagers` (#435) reads the founder's own LinkedIn posts from the last 90 days
and everyone who reacted to them. It keeps each person as a [People record](people-records.md),
scores them against the project's ICP file, and lists them in project Files. People who fit and
aren't connected get a [connection request](linkedin-requests.md) from the founder, sent a few
each weekday.

## Who can run it

- **Projects:** every project, as for [connection collection](connection-collection.md).
- **Runtime:** it needs the LinkedIn cloud runtime, with a template that includes the
  engagement reads, and the native model service (`TIN_LITE_LUNA_API_KEY`).
- **Member:** each member runs it with their own LinkedIn connection. Their saved session reads
  their own posts. That member needs cloud reading turned on and a live session from Tin for
  Chrome, as collection does. Each member saves their own copy, once, and only they can change
  it ([personal workflows](connection-collection.md#connect-once-then-run-from-tin)).

## What a run does

1. **Prepare.**
   - Checks the session, the owner and the runtime.
   - Reads the ICP file at the project's head: `strategy/ICP.md`, else
     `context/linkedin/ICP.md`, first 8 KB.
   - Each `##` heading names a profile. The scores' definition is the file's path, a hash of
     its text and the scorer's version (`strategy/ICP.md@<hash>:v2`). Only the path and the
     version decide whether a score is current (`people_records.rubric`): changing how Tin
     scores, or moving the ICP to another file, marks old scores stale and a run rescores them.
     Editing the ICP file never does; later ratings read the new words.
2. **Posts.** One sandbox read (`linkedin_posts`) finds the member's own posts from the last
   90 days, up to 60. A failure here ends the run with its reason, such as an expired
   session or LinkedIn asking to verify the account.
3. **Reactions.** For each post, sandbox reads (`linkedin_reactions`) of up to 100 people
   at a time.
   - Each person is upserted by their LinkedIn profile ID, with:
     - name and headline;
     - the starter's own relationship with them, from their degree (1st is connected, 2nd or
       3rd is not), kept per member in `record_relationships`. A degree can't show an
       invitation, so it leaves a pending request as it is;
     - a `reacted` event keyed on the post's thread and the person.
   - A forgotten person is skipped.
   - A read LinkedIn objects to (a challenge, a limit, a signed-out session, a changed
     account, a refusal) stops all reading. The run keeps what it read and leads its summary
     with what to do.
   - Any other unreadable page skips that post and is counted.
4. **Rate.** The shared ICP rating (`people_rating`, prompt in `people_icp`):
   - Everyone without a current rating goes to the model one person at a time, six at once:
     Gemini 4 Neon (route `people.icp`) with Google Search grounding, and the ICP file. The
     model reads the person's fields, what they did (reactions, profile views, website visits)
     and the five newest notes members wrote, as data, and searches the web as far as the
     rating needs.
   - Each answer sets `icp` (the closest profile, or `none`), `icp_fit` (1 not a fit, 2 weak
     fit, 3 good fit, 4 strong fit) and `intent` (hot, warm or cold), with a reason. Both are
     null when there's no name or role to judge.
   - A rating a member set is never replaced. A rating older than the person's newest note,
     or than something new they did, is made again.
   - An answer outside the contract is kept as unrated under the ICP definition, so it isn't
     bought again; a call that didn't answer is left for a later run.
   - At most 600 people per run, and no new ones after 15 minutes. Without `GEMINI_API_KEY`
     the step rates nobody and the run still finishes.
5. **Who accepted.** About once a day, one read of where the member stands with up to 20
   people whose [connection requests](linkedin-requests.md#who-accepted) wait for an answer.
   The summary says how many accepted. It doesn't run after LinkedIn objected.
6. **Publish.** It rewrites `people/linkedin-engagers.csv` (everyone who engaged, best fit
   first) and `people/linkedin-engagers.md` (counts and the best fits not yet connected).
   - CSV cells that a spreadsheet would read as a formula are prefixed with `'`.
   - Each run replaces both files. Earlier versions stay in the file history until the
     project is deleted.

## Receipts and history

- **Sandbox reads.** Every read has an effect receipt keyed on the run, the post and the
  page offset.
  - The receipt names the sandbox before the session reaches it.
  - It holds LinkedIn's answer only until those people are written; then it keeps counts.
  - A read that started but never answered is cleaned up and never bought again; that post
    counts as unread.
- **Model calls.** Each person's answer is kept in its own receipt before any value is
  written, so a retry reuses it.
- **Temporal history** holds post and thread URNs, publish times, page offsets and counts.
  Names, headlines and scores stay in Postgres.
- **Project deletion** removes the records and these receipts.

## Billing

The workflow is included, with no charge to the founder, under
`tin-funded-linkedin-engagers-v1`. Tin pays the sandbox reads and the scoring calls, and the
usage is still recorded per call.

## Not yet

- **Comments:** engagers reads reactions only. Comments answer a plain GET now (the
  [feed](linkedin-feed.md) reads them), so commenters could join later.
- **Teammates aren't left out automatically.** Tin doesn't know their LinkedIn profile IDs.
  Forgetting a person once keeps them out of every later run.
- **Weekly schedules** come in a later issue.
