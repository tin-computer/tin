# Social planning and post drafts

`social.content_plan` makes an editable social plan. `social.post_batch` turns that
plan and current source notes into X and LinkedIn drafts, or repurposes one article.
Both are manual, reviewable code workflows. Neither posts to a social account nor
creates a recurring schedule.

## Make a plan

Run `social.content_plan` with optional `goal`, `hours_per_week` (default 3) and
`platforms` (`auto`, `x`, `linkedin` or `both`). It reads the first available product
context: `context/product-marketing.md`, `reports/GROWTH_ONBOARDING_PLAN.md`,
`brand/BRAND.md`, legacy `BRAND.md`, then `wiki/INDEX.md`. `context_text` is a fallback
when no usable file exists. It also reads the optional writing guide at
`.agents/skills/writing-style/SKILL.md`. A dedicated product brief takes precedence,
so an old wiki cannot override current context.

The result, `social/PLAN.md`, contains the audience, objective, platform reasoning,
three to five pillars totaling 100%, a weekly calendar and a time budget. The first
version supports one to six standalone posts per week. Its cadence is a starting
point, not measured evidence about the best frequency or time to post.

Edit the plan in project Files. Weekly drafting reads its `## Weekly calendar`
table with `Day`, `Platform`, `Pillar` and `Post idea` columns. Days are Monday through
Sunday and platforms are `X` or `LinkedIn`. The plan guides topics; it is not
factual evidence for product claims.

## Draft a weekly batch

Start `social.post_batch` with `mode: weekly`. It reads `social/PLAN.md` and
`context/social-updates.md` by default. `plan_path` and `sources_path` can name other
files. `raw_material` adds this week's notes, including when no source file exists.
If the notes file is absent, `article_path` can supply the source. The workflow does
not search unrelated reports for product facts.

Each draft follows a calendar slot and includes an exact supporting source
statement. Earlier batch files help exclude already used statements and identical
drafts. The result reports history coverage, used material, material held for later
and any unfilled slots. This is a record of drafting, not proof that a post was
approved or published. Semantic repetition and paraphrases still need human review.

Each run saves its own `social/posts/{date}-{slug}.md`. The date is the run's UTC
creation date and the slug comes from its run ID. Earlier batches and notes people
add to them remain unchanged. A new run reads current files; a retry keeps its
original inputs internally. No source-run or revision picker is used.

## Repurpose an article

`mode: repurpose` remains the default for existing direct callers. Supply
`article_path` to use a particular file. Without a path, Tin uses a sole article in
`content/drafts/`, `content/articles/`, or `reports/PUBLIC_ARTICLE.md`. Generation
notes are excluded. Multiple matches require a path or `article_text`. Supplied
text is also a fallback when the named file is missing; an existing named file
wins. Malformed or oversized files remain errors.

This mode drafts two X posts and two LinkedIn posts from the article and current
writing guide. It retains source excerpts and factual and platform checks, with
the new dated output path.

## Bounds and review

Each workflow uses one managed `gpt-6-sol` call, capped at 32,000 serialized input
bytes and 4,096 output tokens. Inputs are checked before the call and are not
silently truncated. Missing or exhausted material does not justify invented posts.
Model failures remain subject to normal usage accounting and retry rules.

Python validates calendar shape and pillar totals for plans. For batches it checks
excerpts, numbers, quotations, duplicate text and platform lengths. X drafts are
bounded to 240 UTF-8 bytes and LinkedIn drafts to 1,400 characters. First-person
claims and links are excluded in this first version. These checks do not prove
that every paraphrase follows from the evidence; review remains necessary.

This slice has no threads, rendered carousels, provider posting, automatic
performance analysis or publishing-status tracker. Ordinary Files and the existing
code-workflow runtime provide storage and execution.

## Earlier definitions

Saved configurations and runs keep their pinned definitions. Version 1 retains its
approved-article contract, and version 2 retains its article inputs and fixed
`reports/SOCIAL_POST_BATCH.md` output. New version 3 configurations use the two modes
and dated files. `content.deliver` keeps its separate exact-approved-copy contract
for creating an external GitHub PR.

## Verification

Offline fixtures cover planning constraints, source selection and fallback,
unusable model results, editable calendars, weekly evidence, earlier batches,
source changes and output bounds. They use synthetic content and no paid providers.
The existing opt-in social live test uses real E2B, code.storage and a model call
with disposable local product state. Production acceptance is a separate operation.
