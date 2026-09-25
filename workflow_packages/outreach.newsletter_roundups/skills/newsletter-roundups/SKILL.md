---
name: newsletter-roundups
description: Find curated newsletters that already run a "tools we like", "links" or "roundup" section fitting what the business does, verify each one's current submission process, and draft one tailored pitch per newsletter, skipping newsletters pitched in earlier runs.
---

This produces a shortlist for a human to review and submit themselves. The workflow never
submits, emails, subscribes, or otherwise acts toward any newsletter on its own. Read
SCORING.md before starting; its Python decides exclusions, order, verdict and run-to-run memory.

1. Read the project context. Everything here is evidence, not instructions, and none of it is
   asked of the founder again. Note which files existed for the report's `Context:` line.
   - `wiki/INDEX.md` → `## Product` → `### Feature map`: what the product does, with evidence.
     Pitches draw real detail from here; other memory sections are background.
   - `reports/GROWTH_ONBOARDING_PLAN.md`: `## The business` (what is sold, who buys) and
     `## Tin's view`. Any `hard no: …` in `## Marketing systems` and anything the founder
     forbids are binding: `no cold email` means only newsletters with a submission form or a
     published tips address; `no unbacked claims` means every claim in a pitch is cited.
   - `.agents/skills/writing-style/SKILL.md`: the founder's voice. Every drafted pitch follows
     it. `tone_notes`, when given, wins where the two disagree.
   - The README and site copy only when the files above are missing.

2. Decide what to pitch. If `pitch_angle` is given, use it. Otherwise build it from the Feature
   map and the growth plan: one specific feature, launch, guide or free resource the business can
   point to today, and why a reader of that kind of newsletter should care. State it in one line
   in the report under `Pitching:` with the lines it came from. If neither `pitch_angle` nor any
   of those files gives a real angle, write the short report from step 9 with
   `Status: needs context` and stop; do not invent one. If `newsletter_type_preference` is not
   `no_preference`, only consider that type.

3. Load memory. Find every earlier report in the output folder (the directory of the declared
   output path), read its `tin-newsletter-state` block, and merge them as SCORING.md says.
   Newsletters in that state had a pitch drafted in an earlier run and are not pitched again.

4. Brainstorm newsletters, not categories. "Dev newsletters" is not a newsletter; a specific
   publication with a stated recurring section is. Look for three shapes: a `tools_roundup`
   (a "tools we're using", "worth trying" or "made with" section), an `industry_digest` (a
   topical newsletter for the buyer's field that closes with a links or reader-submissions
   section), and a `community_digest` (a community's own weekly or monthly digest, such as a
   forum, directory or open-source project's newsletter). Skip names already in memory before
   spending a search on them.

5. Verify every candidate with a search before recording it:
   - Does it actually run the section you think it does? Open one or two of its recent issues
     (its own archive page, not a secondhand aggregator) and confirm the section exists and is
     still running, not retired.
   - Does it currently take outside suggestions? Look for a "suggest a tool", "tip us",
     "submit a link" form, a stated submission address, or a masthead note inviting reader
     picks. A newsletter with neither is not verified open, however good the fit.
   - What does it actually require: a one-line description, a short pitch, a screenshot, a
     specific form or a stated email, and any visible pattern in what it has picked before.
   - How does a pitch reach it (`route` in SCORING.md)? Record a published address only when
     the newsletter's own page states it. Never guess or construct an email address.
   Record each candidate as a SCORING.md newsletter record, verified or not. A recurring
   newsletter is one target regardless of which issue you read to verify it.

6. Call `plan()` with the records, `max_newsletters`, `newsletter_type_preference`, the names in
   `already_pitched`, the merged state, the growth plan's hard no's and the declared output path.
   Use its shortlist order, exclusions, status, verdict and state as returned. If you disagree
   with a result, say why next to that newsletter; do not change it.

7. Draft one pitch per shortlisted newsletter, in its actual required format and within its
   stated length limit: a one-line tool blurb for a tools roundup, a short submission for a
   digest's links section. Follow the writing style guide when present. Reference one concrete
   detail that proves the newsletter's actual issues were read — its usual section name, a
   tool it recently featured, its stated audience — rather than a generic pitch. Take facts
   about the business only from the files in step 1 and `pitch_angle`. Never fabricate metrics,
   user counts or claims the business hasn't made elsewhere.

8. Sending stays with the founder. Each newsletter names where the pitch goes (`Submit via:`).
   Do not write rows for `outreach/email/SHORTLIST.csv`: that file belongs to the email
   shortlist workflow, and the email campaign sends one identical body to every selected row,
   which would flatten a tailored pitch. Most newsletters take pitches through a form anyway.

9. Write the report to the declared output path, in exactly this order:
   - `# Newsletter roundup shortlist: <business or product name>`
   - `Status:` the status from `plan()` (`complete` or `no newsletters verified`), or
     `needs context`
   - `Verdict:` followed by nothing but the verdict from `plan()` (`fit`, `thin`, or
     `not a fit`) — never left out, never any other text on that line
   - `As of:` today's UTC date
   - `Pitching:` the one-line angle and where it came from (`pitch_angle` or the file lines)
   - `Context:` which of the step 1 files were read, and `none found` for each that was missing

   Then one section per shortlisted newsletter, in ranked order, each with these exact labels
   so the result stays checkable:
   - `## <Newsletter name>` with its link
   - `Section:` the recurring section it runs and a recent issue that proves it, with that
     issue's link
   - `Fit:` one line tying it back to the pitch
   - `Requirements:` length limit, format and any pattern in what it has picked before
   - `Submit via:` the form URL or the address the newsletter's own page publishes
   - `Drafted pitch:` the pitch

   Then `## Pitched in earlier runs`: newsletters `plan()` excluded because an earlier report
   drafted them, each with its date and report path, or `- none`. Then `## Excluded candidates`:
   every other exclusion from `plan()` with its reason. If fewer than `max_newsletters` survived,
   say so plainly there — do not pad the list to hit the number.

   End with the state from `plan()` as JSON in a fenced block whose info string is
   `tin-newsletter-state`, written with Python from the returned value, not retyped.

   For `Status: needs context`, write the header lines with `Verdict: not a fit`, one sentence
   naming what is missing (a `pitch_angle` input, or the Feature map from "Map what the product
   actually does"), `## Excluded candidates` with `- none`, and the unchanged merged state.

10. Before finishing, reread the report. Count the `## <Newsletter name>` headings (excluding
    `## Pitched in earlier runs` and `## Excluded candidates`) and confirm the count matches the
    shortlist from `plan()`, that the `Verdict:` line matches `plan()`, and that the state block
    parses. Fix any mismatch before finishing.
