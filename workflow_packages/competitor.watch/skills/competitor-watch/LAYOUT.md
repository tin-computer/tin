# Report layout

Write this layout to `context.output.path` (`reports/competitor-watch/<run_id>.md`). Keep the
prose short: a quiet week is a few lines plus the evidence block. Insert values produced by
SCHEMA.md's code instead of retyping them. Use workflow titles in prose, with the key in
backticks after it.

````markdown
# Competitor watch, <YYYY-MM-DD>

Status: <baseline | changes | no-change | diagnostic>
Watched: <Name> (<id>, from <source>); <Name> (<id>, from <source>)
Previous check: <checked_at> in <path> | none
Coverage: <n> of <m> pages read

<One sentence. For changes: the change that matters most and what to do about it.
For no-change: "Nothing changed that needs a response this week." For baseline: what is now
tracked and that the next run will diff against it.>

## What changed
<Only material changes, those matching `watch_for` first. One line each:
- **<Name>**: <subject> <before> -> <after> (<page URL>). <Why it matters against your plans,
  citing the line in the Feature map, Code map or onboarding plan.>
When there are none: "Nothing material." Omit this section on a baseline.>

## Suggested responses
<At most three rows, each tied to a change above. Leave out any row that a hard no rules out.>
| Change | Response | Tin workflow | Start it with |
|---|---|---|---|
| <Name> raised Pro from $12 to $16 | A comparison page while the gap is fresh | Plan upcoming content (`content.plan`) | context_files: this report, with a planned page "<one or two sentences naming the change, the source URL and this report's path>" |
<When nothing needs a response: "Nothing needs a response this week." and no table.>

## Also seen
<Non-material items, one line each: shipped changelog entries with their dates, reworded copy,
features added to or dropped from highlights, currency differences. Omit when empty.>

## Current pricing
<On a baseline, and for any competitor whose pricing changed this run: one table each.>
| Tier | Monthly | Annual (per month) | Unit / minimum | Trial / call to action | Limits |
|---|---|---|---|---|---|
<Otherwise one line per competitor: "<Name>: unchanged since <previous checked_at date>.">

## Not read
<Every page that is not `read`, with its status and what that means, e.g. "linear.app/pricing:
empty (rendered by JavaScript); pricing carried from 2026-09-10, not compared." Earlier reports
skipped for an unreadable evidence block are listed here by path. Omit when empty.>

## Sources
- Competitors: <which input or project file supplied each one>
- Your side: <paths and sections read for your own plans and positioning, or the override>

```tin-competitor-watch
<the exact output of fence(next_evidence(...))>
```
````

A diagnostic report has the title, `Status: diagnostic`, one paragraph saying no competitor could
be resolved and what was checked, and the two ways forward: pass `competitor_urls`, or run Plan
keyword opportunities (`organic.keyword_plan`) so search competitors are on file. It has no
evidence block.

## Choosing a response

Pick from this table. A row applies only to a material change listed above. Pricing is the
founder's decision: suggest research, never "match their price".

| Material change | Usual response | Workflow |
|---|---|---|
| They raised a price, added a seat minimum, dropped a free tier or trial, or moved a feature up-tier, and your plans compare well on that point | A comparison or "alternative to <them>" page | Plan upcoming content (`content.plan`), with this report in `context_files` if it is under 20 KB; content.generate drafts the page. |
| They cut a price, added a free tier or a cheaper plan, or now include something you charge for | Test whether your pricing still holds for your buyers | Research a question deeply (`research.deep_dive`), with the question written out |
| They shipped or now highlight a capability the Feature map shows you lack, and it matches `watch_for` | Decide whether it is table stakes for your buyers | Research a question deeply (`research.deep_dive`) |
| They moved a plan from self-serve to "contact sales", or stopped showing prices | Point buyers to your public prices and self-serve start | Plan upcoming content (`content.plan`) only when your own pricing page already shows prices; otherwise one line for the founder, no workflow |

Never suggest paid ads, cold email or any channel the onboarding plan lists as a hard no. Never
suggest contacting the competitor, and never invent urgency from a copy change.
