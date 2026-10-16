# People who engaged with your LinkedIn posts

`social.linkedin_engagers` (#435) reads the founder's own LinkedIn posts from the last 90 days
and everyone who reacted to them. It keeps each person as a [People record](people-records.md),
scores them against the project's ICP file, and lists them in project Files. It reads only and
sends nothing.

## Who can run it

- **Projects:** the same allowlist as [connection collection](connection-collection.md)
  (`TIN_LITE_CONNECTION_COLLECTION_PROJECTS`). The workflow is hidden elsewhere, and a start is
  refused.
- **Runtime:** it needs the LinkedIn cloud runtime, with a template that includes the
  engagement reads, and the native model service (`TIN_LITE_LUNA_API_KEY`).
- **Member:** each member runs it with their own LinkedIn connection. Their saved session reads
  their own posts. That member needs cloud reading turned on and a live session from Tin for
  Chrome, as collection does.

## What a run does

1. **Prepare.**
   - Checks the session, the owner and the runtime.
   - Reads the ICP file at the project's head: `strategy/ICP.md`, else
     `context/linkedin/ICP.md`, first 8 KB.
   - Each `##` heading names a profile. The scores' definition is the file's path, a hash of
     its text and the scorer's version (`strategy/ICP.md@<hash>:v2`), so editing the ICP or
     changing how Tin scores marks old scores stale and a run rescores them. Other commits
     don't.
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
4. **Score.**
   - People without a score under the current definition go to the model (`gpt-6-luna`,
     route `linkedin.engagers.icp`) in batches of 25, with the ICP file. The model sees
     names and headlines, and the five newest notes members wrote about a person, as data.
   - A fit a member set is never scored again. A score older than the person's newest note is
     scored again, with the note.
   - Each answer sets `icp` and `icp_fit` with a reason, or "unknown" with a reason when the
     headline is too thin.
   - The score ranks whom the founder looks at first; it is not a decision to contact anyone.
     A headline rarely states stage, size or needs, so what it leaves out is unknown, not a
     mismatch. The instructions anchor the scale:
     - 0.8 to 1: the profile's buyer role at its kind of company;
     - 0.6 to 0.79: the buyer role at a company that may be its kind, the rest unknown;
     - 0.3 to 0.59: a partial match, such as a role next to the buyer's;
     - below 0.3: a mismatch the headline shows, and `none` when no profile does better.
   - An answer outside the contract leaves the person unscored with a reason. A batch whose
     call failed is left for a later run.
   - At most 600 people are scored per run.
5. **Publish.** It rewrites `people/linkedin-engagers.csv` (everyone who engaged, best fit
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
- **Model calls.** Each batch's answer is kept in its own receipt before any value is
  written, so a retry reuses it.
- **Temporal history** holds post and thread URNs, publish times, page offsets and counts.
  Names, headlines and scores stay in Postgres.
- **Project deletion** removes the records and these receipts.

## Billing

The workflow is included, with no charge to the founder, under
`tin-funded-linkedin-engagers-v1`. Tin pays the sandbox reads and the scoring calls, and the
usage is still recorded per call.

## Not yet

- **Comments:** they need LinkedIn's current query ID from the browser; see
  [connection collection](connection-collection.md#engagement-reads).
- **Teammates aren't left out automatically.** Tin doesn't know their LinkedIn profile IDs.
  Forgetting a person once keeps them out of every later run.
- **Weekly schedules, `/people`, sending connection requests and the Decisions card** each
  come in a later issue.
