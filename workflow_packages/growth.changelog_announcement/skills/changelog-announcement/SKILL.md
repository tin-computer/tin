---
name: changelog-announcement
description: Turn recently merged pull requests into one user-facing announcement, or explain why none is warranted.
---

## What this does

A founder ships things continuously in GitHub, but "we merged 14 PRs this week" is not
something a user ever reads. This skill closes that gap: it reads what actually merged,
judges which of it a user would care about, and drafts one short announcement in the
founder's own voice — or says plainly that nothing merged is worth announcing.

## Steps

1. **Establish the window.** If `since_run_id` is given, find that run's output file under
   `content/announcements/` and read the date in its frontmatter; only consider pull
   requests merged after that date. If it is empty, look back 14 days from today, or to the
   repository's first commit if that's more recent.

2. **Read every pull request merged into the default branch in that window.** For each one,
   read its title, description, and diff summary — not just the title, titles lie. Note
   the files it touched.

3. **Classify each merge into one of:**
   - **User-visible change**: a user can see, use, or notice the effect directly (new
     feature, changed behavior, fixed bug they could have hit, removed limitation).
   - **Invisible to users**: refactors, internal tooling, test changes, dependency bumps,
     CI config, docs-only changes, changes gated behind a flag nobody has yet.
   - **Unclear**: the diff or description doesn't make the user-facing effect obvious.
     Read more of the diff before giving up and placing it here.

   Discard "invisible to users" entirely — they never appear in the draft, not even as a
   footnote. If everything in the window is invisible, or the window is empty, stop here:
   write the output file with no announcement, name the merges you reviewed and why none
   qualified, and end the run. Producing filler copy about "under-the-hood improvements"
   is not the job.

4. **Rank what's left.** If more than one user-visible change exists, decide whether they
   belong in one announcement or several. A cluster of small related fixes can be one
   post; one large feature and one unrelated small fix usually don't share a post well —
   in that case, pick the change that changes a user's day-to-day the most for this run's
   draft, and name the rest in one line at the end for a future run.

5. **Read the project's writing-style guide** if `content/style/guide.md` (or similar,
   check project memory) exists, and match its voice. If none exists, default to short,
   concrete, first person plural, no marketing adjectives — describe what changed, not how
   great it is.

6. **Write the announcement.** Lead with the change itself, not a preamble ("We shipped
   X" not "We're excited to announce"). State what a user does differently now, or what
   problem no longer exists. Link or reference the actual PR only if project convention
   already does this. Keep it to a few short paragraphs or a tight list — this is a
   changelog entry or short post, not an article.

7. **Write the output file** with YAML frontmatter recording the run date and the PR
   numbers considered (both included and discarded, with a one-line reason each), followed
   by the announcement itself. This frontmatter is what step 1 reads on the next run —
   keep the date field name and format exact so it stays machine-readable.

## What "worth announcing" is not

- A dependency bump, even a major one, unless it changes something a user directly
  interacts with.
- A bug fix for a bug that was never live in production, or that no user plausibly hit.
- A change already covered in a previous announcement in this window.
- General marketing language with no concrete change behind it. If you cannot name the
  one thing that is different for a user, it does not belong in the draft.

## Example frontmatter

```markdown
---
run_date: 2026-09-24
considered:
  - pr: 412
    verdict: included
  - pr: 415
    verdict: discarded
    reason: internal logging refactor, no user-visible effect
  - pr: 417
    verdict: discarded
    reason: fixes a bug behind a flag no customer has enabled yet
---

We shipped ...
```
