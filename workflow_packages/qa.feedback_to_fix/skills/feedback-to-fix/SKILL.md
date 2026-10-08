---
name: feedback-to-fix
description: Read public threads where people react to the product, keep what at least two different people say, and plan the existing page copy fix behind it for the founder to approve, or report why not.
---

One job: when several people say the same thing about the product in public, plan the change
to the words on the page that caused it. It is not a reply tool and not a page audit. Nothing is
planned unless real quotes from at least two different people point at it. A founder reads these
threads once, on launch day; a visitor who misreads the page keeps misreading it the next day.

You read the repository and change nothing there. The plan goes to the founder in Decisions;
once they approve it, website.change writes your files to the site.

1. **Confirm the target.** The run context's `workspace` names the repository the founder
   selected on GitHub, its default branch and the commit you read. website.change checks open
   pull requests and upstream changes before it writes anything.

2. **Read what Tin knows** from `/home/user/state`, each as evidence, none required. Say which
   were missing in `Sources read`.
   - `wiki/INDEX.md`, `### Feature map` and `### Code map`: what the product does. A fix may only
     claim what these show. When the Feature map is missing, `supported` is false for any fix
     that adds a claim.
   - `reports/GROWTH_ONBOARDING_PLAN.md`: the product name, buyer and hard no's. If the hard no's
     forbid changing the site, plan no change.
   - `.agents/skills/writing-style/SKILL.md`: the founder's voice for any changed copy.
   - Earlier `reports/feedback-to-fix/*/PLAN.md`: collect the quote links in their
     `feedback-fix-patch.json` blocks. Those quotes are not counted again when that plan's
     `after` wording is already in the repository: it shipped. Otherwise they count again: a
     change still waiting in Decisions is replaced by this run's plan. A plan without a patch
     excludes nothing.
   - The product's name and domain come from these files. If none names them, return
     `insufficient_data` saying the context is missing, and stop.

3. **Find the threads.** Search public, indexable pages on the chosen platforms for the product's
   name and domain and for launch or show-and-tell posts about it, within `lookback_days`.
   Never private groups or pages behind a login. Open at most 30 threads, the most recent and
   most discussed first, and read the post and all comments, not only the top ones. When the
   limit stops you, say so in `Sources read`. A platform that cannot be opened or searched is
   named there too, never guessed at.

4. **Keep what is about the product.** Keep a comment that says what the product is, whether it
   is clear, why not to try it, what the person asked, or what they would change. Drop the
   founder's own comments, jokes, and general talk. Give each person an author label (`a1`,
   `a2`) that is the same across threads only when the page shows the same person. Never
   write a username. Copy their words exactly, at most 400 characters, with the link.

5. **Group into themes.** One comment can make several points. Put each point in its own theme,
   quoting only the sentences that make it. Do not merge people yourself: `choose_action` merges
   themes of the same kind whose copy overlaps, so name each target's lines exactly. Give each
   theme a `kind` from THEMES.md. For each, find the existing copy whose text carries it as
   `targets` (path and line range), and decide `supported` from the Feature map or the code.
   Record a one-person observation as its own theme: the code decides whether it counts and
   never acts on one voice. Do not stretch a quote to fit a theme.

6. **Decide.** Run `choose_action` from THEMES.md unchanged and follow its outcome. On anything
   but `patch`, change no files and go to step 8.

7. **Make the change in a scratch copy.** Copy the repository to a scratch folder and edit only
   the files `choose_action` returned, and only wording that already exists: make what people
   misread clear, answer what they asked, meet the objection honestly. Keep the founder's voice
   and the page's structure. Do not add a page, a section, a claim the product does not back, a
   testimonial, anyone's words, a price or a dependency, and do not touch code, pricing, legal
   text, payment or auth. Then build the `feedback-fix-patch.json` block from the edited files
   with code, as PLAN.md says.

8. **Verify and write the plan.** In the scratch copy, run the repository's own lint and test
   commands this environment can run, and `git diff --check`. Fix failures your change caused;
   never edit checks to pass. Re-read each changed file. Write the plan with PLAN.md's headings
   in order. Never open a pull request or contact anyone.

## Honesty rules

- Never include a quote from a page you did not open, or invent a date, count or link.
- A theme needs two different people; the count comes from the quotes you recorded.
- Saying "not enough feedback yet" is a correct result, not a failure.
- Name project files by path in backticks, never as links: a reader cannot open the sandbox.
