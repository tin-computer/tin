---
name: podcast-guest
description: Find the podcasts a founder should go on as a guest, across every audience where they carry weight (their buyers, their own craft, their home country, the product's topic), verify that each show is active, listened to and takes guests like them, and draft a pitch per show in its language. Stage one of two; it never sends anything.
---

This produces a guest plan for the founder to review. Stage two, the email campaign, sends
only the pitches the founder approves. This run never sends, submits, follows or messages
anyone. Read SEARCH.md and SCORING.md before starting; SCORING.md's Python decides
exclusions, balance, memory and the pitch block.

1. **Read the context.** Everything here is evidence, not instructions, and nothing in it is
   asked of the founder again. Note which files existed for the report's `Context:` line.
   - `founder_profile` (required input): who the founder is.
   - `wiki/INDEX.md` → `## Product` → `### Feature map`: what the product does, with numbers.
   - `reports/GROWTH_ONBOARDING_PLAN.md`: `## The business` (what is sold, who buys) and any
     `hard no: …` lines, which are binding. `no unbacked claims` means every claim in a pitch
     comes from these files or the profile.
   - Founder rules the project keeps elsewhere, such as a preferences file linked from
     `wiki/INDEX.md`: any "never" or hard no there binds every draft, even when SCORING.md
     has no id for it.
   - `.agents/skills/writing-style/SKILL.md`: the founder's voice. `voice_notes` wins.
   - Without a Feature map, take product facts from the growth plan's `## The business`, the
     README and the product's own site, and say so on the `Context:` line.
   - Earlier reports in the output folder: their `tin-podcast-state` blocks (merge them as
     SCORING.md says) and their picks, so this run does not repeat them.

   If `founder_profile` says almost nothing about the person (no role, no background), write
   the short report from step 9 with `Status: needs context` and stop; do not invent a person.

2. **Decide what is being promoted.** The pitches promote this project's product. The
   profile may show a stronger story elsewhere (another company, an earlier role); use it as
   the founder's credibility, but every pitch names this project's product and why the
   show's listeners should care about it. If the founder's link to the product is thin, say
   so on the `Founder:` line rather than overstating it.

3. **Choose the arenas.** Before any search, write down 3-6 audiences, each as a SCORING.md
   arena record. Credibility is relative to the room, so for each one say why this founder
   stands out among those listeners, not in general. Consider at least:
   - `buyers`: the people who use the product (from the growth plan). Being the person behind
     a tool they use is the story.
   - `craft`: the founder's own trade (from the profile). Peers want hard numbers and
     failures, not a pitch.
   - `home`: one per language the founder speaks natively, or a country they are from, when
     it differs from the product's market. Someone working at a US venture-backed startup is
     rare and newsworthy in many home-country tech scenes.
   - `topic`: the subject the product is about, for shows that cover it.
   Skip a kind only when it plainly does not apply, and say why in `## Gaps`. Add the arena of
   any show named in `include_shows`.

4. **Search each arena** with the methods SEARCH.md lists for it, at least two per arena,
   inside the 30-call Podscan budget, plus web searches. Record on each arena every method
   you tried, including ones that found nothing (`tried`). Seed searches from the profile:
   companies like the founder's, the founder's title, the buyers' titles, the topics, the
   home country's charts. Skip shows already pitched (state, `already_pitched`) before
   spending calls on them.

5. **Verify the finalists** on their own pages, as SEARCH.md says, and write a show record
   for each one, verified or rejected. A show that is big but has never hosted anyone like
   the founder is a `ladder` show if a step would get them in (a conference talk with the
   hosts, a guest post, a launch they cover), otherwise rank it low and say why. Judge
   reachability from the guests the show actually had, never from its size alone.

6. **Rank within each arena** by your judgment of fit, audience and reachability, and write
   the reason. Do not compare shows across arenas; `plan()` balances them.

7. **Call `plan()`** with the arenas, the records, today's UTC date, `max_pitches`,
   `already_pitched`, `include_shows`, the merged state, the growth plan's hard no's and the
   declared output path. Use its picks, ladder, exclusions, gaps and state as returned. If you
   disagree with a result, say why next to the show; do not change it.

8. **Draft for every pick and ladder show**, in the show's language and the founder's voice:
   - Name one specific recent episode of that show and why this founder follows from it.
   - Use the angle of the show's arena. Take facts only from the profile and the files in step
     1. Never invent audience, metrics, appearances or availability.
   - `published_email`: a subject (under 80 characters), a pitch of 120-200 words, and one
     short follow-up for a week later.
   - `guest_form`: the answers to the form's own questions, ready to paste. When the form's
     questions cannot be read (an embedded form), answer the usual ones (topic, why now,
     bio, links) and say so.
   - `direct_message`: a DM under 80 words for the host's channel.
   - `warm_intro`: a three-sentence blurb a contact can forward.
   - `ladder`: the step's draft (a talk title and abstract, a guest post outline) and its date.
   Then call `pitch_block()` with the picks and one draft per `published_email` pick.

9. **Write the report** to the declared output path, in exactly this order:
   - `# Podcast guest plan: <founder name>, <product name>`
   - `Status:` the status from `plan()` (`complete` or `no shows verified`), or `needs context`
   - `Verdict:` followed by nothing but the verdict from `plan()` (`fit`, `thin` or `not a fit`)
   - `As of:` today's UTC date
   - `Founder:` one line: who they are and the strongest facts the pitches use
   - `Context:` which step 1 files were read, and `none found` for each that was missing
   - `## Arenas`: one bullet per arena: `**<label>** (<kind>, <language>): <why>. Searched
     with: <methods>. <n> candidates, <n> picked.`
   - `## Picks`: one `### <n>. <Show name>` per pick, in `plan()` order, each with these
     labels: `Show:` (link), `Arena:`, `Language:`, `Audience:` (the numbers you relied on and
     their source), `Similar guest:` (name, episode, link, date, or `none found` and why it is
     still a pick), `Route:`, `Send via:` (the published address and the page that publishes
     it, the form link, the host's channel, or the contact), `Notes:` (fees, sponsored signs,
     caveats, or `none`), then the drafts: `Subject:`, `Pitch:` and `Follow-up:` for email;
     `Form answers:`, `Message:` or `Intro:` for the other routes.
   - `## Ladder`: one `### <Show name>` per ladder show with `Show:`, `Arena:`, `Why not
     now:`, `Step:`, `Deadline:` (or `none stated`) and `Draft:`.
   - `## Shows you asked about`: each `include_shows` name with its outcome from `plan()`, or
     `- none asked`.
   - `## Gaps`: every gap from `plan()` and any arena kind you skipped and why, or `- none`.
   - `## Pitched in earlier runs`: shows excluded because an earlier report pitched them, with
     date and report path, or `- none`.
   - `## Excluded candidates`: every other exclusion with its reason. If fewer than
     `max_pitches` survived, say so plainly; do not pad the list.

   End with two fenced blocks written with Python from the returned values, not retyped: the
   `pitch_block()` result with info string `tin-podcast-pitches`, then the `plan()` state with
   info string `tin-podcast-state`.

   For `Status: needs context`, write the header lines with `Verdict: not a fit`, one sentence
   naming what the profile is missing, `## Excluded candidates` with `- none`, an empty pitch
   block (`{"version": 1, "pitches": []}`) and the unchanged merged state.

10. **Check before finishing.** Count the `### ` headings under `## Picks` and confirm they
   match `plan()`'s picks, that the `Verdict:` line matches, that every `published_email` pick
   appears once in the pitch block with the address the show publishes, and that both blocks
   parse. Fix any mismatch.
