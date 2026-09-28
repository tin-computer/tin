---
name: migration-map
description: Draft structured switchover pages that map each competitor's features, terminology and migration path to the product's equivalents.
---

When a buyer decides to leave a competitor, they search for "migrate from X to Y" or "X
alternative." These are high-intent searches: the decision to switch is already made, and
the buyer needs to know that their workflow translates and their data moves. This skill
produces the page that answers that search.

Read SCORING.md before starting. Extract the Python block into a scratch module and use it
unchanged for competitor prioritisation, feature classification and run-to-run state.

## Budget

- At most 5 competitors, one page each.
- At most 30 web actions total: one public site visit, one pricing page visit and one docs
  landing page visit per competitor, plus up to three searches for migration guides or
  comparison pages per competitor.
- Stop when the budget is spent. A competitor you could not fully research gets an
  `incomplete` section in the report explaining what is missing.

## 1. Load project context

1. Read `wiki/INDEX.md`. Find `### Feature map` under `## Product`. If it is missing or
   empty, write a diagnostic report and stop: name `product.deep_dive` as the workflow to
   run first. The Feature map is the only source of truth for what the product does.
2. Read `### Code map` when present: it supplies integrations, tech stack and data formats
   that matter for migration steps (export formats, API compatibility, webhooks).
3. Read `reports/GROWTH_ONBOARDING_PLAN.md` when present: `## The business` for who buys
   and how they describe the problem, any competitors named under `## Marketing systems` or
   `## The market`, and any `hard no: …` lines. Never claim anything the founder forbids.
4. Read `.agents/skills/writing-style/SKILL.md` when present: all page copy follows it.
5. Find every earlier report in the output folder (the directory of the declared output
   path), parse each `tin-migration-state` block with `read_state()` and fold them with
   `merge_states()`. A competitor an earlier run already drafted is not drafted again unless
   the `focus` input explicitly asks for a different angle.

## 2. Identify the competitors

Build the competitor list from these sources, in priority order:

1. The `competitors` input, when non-empty. Accept each name as given.
2. Named competitors from the onboarding plan.
3. Competitors from the keyword plan or audit if available in project files.
4. If no competitors can be found from any source, write a diagnostic report and stop:
   explain that the workflow needs at least one competitor name. Suggest the founder fill in
   the `competitors` input or complete Start here.

Remove duplicates. Cap at `max_pages` using the scorer from SCORING.md.

## 3. Research each competitor

For each selected competitor, visit only public, unauthenticated pages:

1. **Marketing site**: the homepage. Extract the product positioning, hero copy and buyer
   language. Note how they describe themselves.
2. **Pricing page**: extract tiers, prices and the billing model. Record the URL and date.
   If there is no public pricing, record `pricing: not public`.
3. **Feature list or docs landing**: extract the feature categories and names as the
   competitor uses them. Use their exact terminology.

Do not sign up, sign in, use a trial, or access any gated content. If a page is behind a
login wall, record `gated` and skip it.

## 4. Map features

For each competitor feature found in step 3, find the closest match in the product's Feature
map. Classify each mapping using the categories from SCORING.md:

- **direct**: the product does the same thing, possibly under a different name
- **partial**: the product covers some of this but not all
- **absent**: the product does not do this
- **stronger**: the product does this and adds a capability the competitor lacks

For `direct` and `stronger`, note the terminology difference if the names differ (e.g.,
competitor calls it "Boards," product calls it "Projects"). For `absent`, be honest: say
the product does not have this. Never invent features or stretch a partial match into a
direct one.

Cite the Feature map line for every classification. A classification without a citation is
a hallucination.

## 5. Draft migration steps

Where the product has concrete migration paths, draft actionable steps:

1. **Data migration**: if the Code map shows import/export capabilities, or the competitor
   has a public export feature, describe the path (e.g., "Export your projects as CSV from
   [Competitor] Settings > Export. Import into [Product] via Settings > Import.").
2. **Integration migration**: if both products integrate with the same services, note which
   integrations carry over and which need reconnecting.
3. **Configuration migration**: if the products share concepts (workflows, automations,
   templates), describe how to recreate them.
4. **Team migration**: if relevant, note how to move team members, permissions and billing.

Only describe steps you can ground in the Feature map or Code map. Never describe a migration
path you are guessing at. When you cannot determine a step, write "Check with the product's
documentation for the latest import options" and move on.

## 6. Write each switchover page

For each competitor, produce a structured draft with these sections:

### Title
"Switch from [Competitor] to [Product]" or "Moving from [Competitor]? Here's what changes."
Follow the writing style guide if available.

### Opening (2-3 paragraphs)
Acknowledge why someone would switch: name the real pain points (cost, complexity, missing
features) that evidence from the research supports. Never trash-talk the competitor. Frame
the product as a fit for people who need what it does, not as universally better.

### Feature comparison table
A Markdown table with columns: Feature area | [Competitor] | [Product] | Notes. Every row
cites the Feature map. Include `absent` rows honestly. Put `stronger` rows first, then
`direct`, then `partial`, then `absent`.

### Key differences
Three to five bullet points on what is genuinely different, not a repeat of the table.
Focus on workflow differences, philosophy differences, and things the table cannot capture.

### Migration steps
The steps from section 5, numbered and actionable. If there are no concrete migration paths,
say "We're working on a migration tool. In the meantime, here's how to set up [Product]
from scratch" and describe the new-user path from the Feature map.

### Pricing comparison
A factual side-by-side of published pricing. If the competitor's pricing is not public, say
so. Never estimate or speculate on pricing. Note the billing model (per seat, usage, flat).

### Call to action
A single sentence pointing to the product's signup or trial, in the founder's voice. No
discounts or time-limited offers unless the onboarding plan explicitly approves them.

## 7. Compile the report

Write one report to the declared output path containing:

1. **Summary**: which competitors were mapped, which had the strongest comparison, and which
   searches ("X alternative," "migrate from X") have the most evidence of demand.
2. **Each switchover page draft**: separated by horizontal rules, with the competitor name
   as a heading.
3. **Gaps found**: features the product is missing that appeared in multiple competitors.
   This is useful intelligence, not a failing.
4. **State block**: call `next_state()` with today's UTC date and the declared output path.
   Write the result as a fenced `tin-migration-state` JSON block at the end.

## Safety

- Never sign up for, sign in to, or use a trial of any competitor product.
- Never contact competitors, their customers, or anyone.
- Never publish, post, submit, or send anything.
- Never change project files other than the declared output.
- Never claim the product has a feature the Feature map does not support.
- Never invent pricing, user counts, market share, or performance benchmarks.
- The founder reviews and publishes. This run drafts.
