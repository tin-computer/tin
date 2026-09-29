---
name: newsletter-placements
description: Find newsletters and communities the buyers read that have a standing, written way in, verify each slot's rules, and draft one submission per placement in the founder's voice, skipping placements drafted in earlier runs.
---

# Newsletter and community placements

This produces placements for the founder to review and send themselves. The workflow never
submits, pays, signs up, posts, emails or contacts anyone.

## What counts as a placement

A placement is a specific, named newsletter or community with a **standing, documented way in**
that the founder can use without an invitation:

- a newsletter's sponsorship or classified slot with a published rate card or booking page;
- a newsletter's featured-tool, "tools we like" or reader-submission form;
- a community's showcase, "show your project" or launch thread or channel with written rules;
- a community's sponsor or partner intake page.

It is not a placement when:

- the entity is a broad platform ("Reddit", "LinkedIn", "Discord", "Product Hunt" as a whole);
  a specific node inside it (one subreddit's weekly showcase thread, one Discord server's
  #show-and-tell channel with pinned rules) can be;
- the way in has to be inferred ("they might take sponsors", "the editor could be emailed");
- it is a conference, call for speakers, meetup or podcast: hand off to **Find the talks and
  podcasts worth pitching** (`outreach.speaking_shortlist`);
- it is a campus event, hackathon or student club: hand off to **Find campus events to reach
  student buyers** (`outreach.campus_events`);
- it breaks a hard no in the growth plan, or its price is above `budget_usd` (0 = free only).

## Steps

1. **Read the project context.** Everything here is evidence, not instructions, and none of it
   is asked of the founder again. Note which files existed for the report's `Context:` line.
   - `reports/GROWTH_ONBOARDING_PLAN.md`: who buys, the market, the budget and every `hard no`.
   - `wiki/INDEX.md` → `### Feature map`: what the product really does. Submissions cite this.
   - `.agents/skills/writing-style/SKILL.md`: the founder's voice for every draft.
   `focus` and `geography` override the plan. If neither the plan nor `focus` says who the
   buyers are, write `Status: needs context` and stop (see Output).

2. **Load memory.** Read every earlier report in the output folder and merge the `placements`
   arrays of their `tin-placement-state` blocks. A placement in memory (matched on its rules URL,
   or on its name when the URL moved) is never drafted again.

3. **Find candidates, not categories.** Search for the newsletters and communities these buyers
   actually read: niche newsletters in their field, the community forums and chats of the tools
   they already use, and curated roundups that take submissions. Skip names already in memory
   before spending a search on them. Stop at about 30 candidates.

4. **Verify each candidate on its own pages.** Open the entity's own site, not an aggregator:
   - its canonical URL and that it is current: a newsletter issue or community post within the
     last 60 days;
   - audience evidence: what the entity itself says about its readers or members (subscriber or
     member counts only when the entity publishes them, else `Unknown`);
   - the rules or intake page for the way in: what is accepted, format and length limits, lead
     time, and the price when it is paid. Quote at most one short phrase per page.
   Drop the candidate when any of these cannot be verified, and keep the reason.

5. **Pick up to `max_picks`.** Prefer free placements, then the closest audience match, then the
   clearest rules. Never pad the list to reach the number.

6. **Draft one submission per pick** in the founder's voice, following that slot's written rules
   exactly: its fields, length and format. Use only product facts from the Feature map or the
   product's own pages. For a community showcase, write the post itself; for a newsletter slot,
   the submission form answers or the sponsor copy; for a sponsorship, also the booking note.
   Disclose that the founder makes the product wherever the rules ask or the context implies it.

## Honesty rules

- Every URL in the report is one you opened in this run. Never invent a URL, count, price, date,
  rule, contact name or email address. Unknown is written as `Unknown`.
- Never claim a way in that the entity's own page does not show.
- Do not predict traffic, signups or reach.

## Output

Write the declared output path in this layout:

```markdown
# Newsletter and community placements

Context: <files that existed> · Buyers: <who> · Geography: <where> · Budget: <$N or free only>
Status: ready | nothing new | needs context

## <Placement name>

- Kind: newsletter slot | newsletter submission | community showcase | community sponsorship
- URL: <canonical URL>
- Rules: <URL of the rules or intake page> — <what it accepts, limits, lead time>
- Price: <free | $N per placement>
- Audience: <evidence> (<source URL>)
- Last active: <date> (<source URL>)

### Draft

<the submission, ready to paste, following the slot's rules>

## Handed off

- <conference, podcast or campus candidates found, with the workflow that covers them, or - none>

## Drafted in earlier runs

- <placements skipped because an earlier report drafted them, with date and report path, or - none>

## Excluded candidates

- <name>: <the one reason>
```

End the report with the merged memory plus this run's picks as JSON in a fenced block whose info
string is `tin-placement-state`:

```tin-placement-state
{"version": 1, "placements": [{"name": "…", "rules_url": "https://…", "drafted_on": "YYYY-MM-DD", "report": "reports/outreach/newsletters/<run_id>.md"}]}
```

For `Status: needs context`, write only the header lines, one sentence naming what is missing
(a `focus` input, or the growth plan from Start here), `## Excluded candidates` with `- none`, and
the unchanged merged state. For `Status: nothing new`, say why and keep the other sections.

Before finishing, reread the report: the number of `## <Placement name>` sections matches the
picks, every pick has a `Rules:` URL and a draft, and the state block parses.
