# Content planner editorial-quality slice

## Outcome

Develop distinct, evidence-backed buyer tasks before allocating the calendar. A six-month
program is still a six-month program, but capacity is a ceiling, never a reason to invent
articles. Show the number of supported briefs, unused capacity, and evidence gaps explicitly.

## Implementation

1. Preserve the editable `content-program-v1` file, amendment controls, saved configurations,
   batch reservations and publication/recovery machinery. Publish a new pinned planning policy;
   historical definitions continue to execute their original policy.
2. Inspect at most 60 same-host public HTML pages from the frozen audit/keyword candidates.
   Reuse the IP-pinned, redirect-bounded fetcher. Save bounded extracted text, timestamps and
   hashes as run evidence; failures are unavailable evidence, not proof a page does not exist.
   From `content-editorial-v7`, the model also reads the whole site's page list (sitemap,
   Search Console, crawl, pages Tin published, keyword ranking pages), saved as `pages.json`;
   see [one content.generate](one-content-generate.md).
3. One receipted model call develops a prioritized portfolio of distinct briefs. Supply all
   retained keywords, including fallible exclusions, selected files and page excerpts. Compact
   source aliases are resolved deterministically into original source IDs. An update must
   identify a page actually inspected; a new-page decision must not point to an observed page.
4. Tin owns calendar dates, capacity, identity, readiness and frozen amendment scope. Spread
   priority-ordered briefs across the selected horizon; never ask the model to repeat these
   bookkeeping fields. Keep every proposed brief `needs_verification`: inspecting a page is
   not validation of product claims, demand, originality or expected SEO/AI results.
5. Render coverage, evidence gaps, destination rationale and page-check scope in the ordinary
   Markdown report. No new UI, schema migration, engine, schedule or generation behavior.

## Verification

Test source/destination binding, unavailable and hostile pages, bounded reads, duplicate
destinations, empty portfolios, calendar allocation, old policy execution, amendment isolation,
model/publication retries and free scheduled preparation. Run the full regression suite.
For live verification, use an explicitly authorized test project and its successful
audit/keyword publications, preserving the old program. Review actual briefs, intent mapping,
   capability restraint and horizon coverage. Do not call schema success editorial acceptance.

AI revisions preserve the batch placement of retained item IDs, so a brief amendment cannot
quietly reshuffle dated work. Newly added briefs use available selected slots. Exact moves
remain ordinary editor/file edits. Inspected model excerpts shrink to the remaining input
budget; all source rows and selected member files are preserved, with fuller observations
and excerpt-limit metadata retained in run evidence.

The first paid editorial pilot stopped on two proposed sections for the same `/docs` page.
The model response was saved successfully; no plan was published and no blind retry occurred.
Same-page updates now consolidate mechanically into one bounded brief, preserving every
section, source and verification check, recording merged IDs, and retaining the original
response. Oversized combinations and overlap with unselected work still fail closed.

Reviewing the saved output also found numerically valid references to unrelated keywords.
The v3 policy labels aliases with their keyword/group meaning, reserves a source slot for
the inspected destination, and binds its immutable page observation in code. The exact
prepared input, alias mapping and compiled output schema are receipted. Historical v1/v2
definitions remain supported; source-reference existence is never described as proof of
semantic relevance. Paid acceptance must inspect the actual mapped queries.

## Related workflows

See [writing style capture](writing-style-capture.md) and
[content generation](content-generation-implementation.md) for the subsequent authoring steps.

## Positioning comes from the project (content-editorial-v6)

`content.plan` 0.7.0 pins `content-editorial-v6`. v5 told the planner that "strategy owns
product positioning", and one plan answered by telling writers to position the product narrowly.
v6 replaces that paragraph: the plan reads the project's positioning files (`brand/BRAND.md`,
up to five `context/*.md` notes, `wiki/INDEX.md` and the Start here plan, bounded to 8 KB each
and 30 KB together, pinned with their digests) and follows them. A brief chooses the reader
question, searches, evidence and sections, and never says how to position the product. As a
check, Tin removes any brief sentence that sets positioning ("Position the product as ...",
"frame it as ...", "Positioning: ...") and counts the removals in the plan's evidence; search
positions such as "average position 8" are left alone. Plans pinned to v5 and older keep their
instructions and their briefs as written.

The drafts read the same files: `content.generate` 1.8.0 lists them in its pinned
`content_draft.positioning` context, `content.public_article` 1.6.0 reads them from the
checkout, `content.answer_page` 1.6.0 receives them as sources (`ANSWER_POSITIONING_V1`) and
`content.refresh` pins them before compute. Each presents the product the way those files do.


## A larger output cap (content-editorial-v8)

`content.plan` 0.9.0 pins `content-editorial-v8`: v7 with a 32,000-token output cap instead of
16,000. GPT-6 Luna counts reasoning against the cap, and a cap only stops a plan; billing
charges the tokens the call actually used. Instructions, schema, page and input bounds are v7's.
At its bounds the call reads about 254,000 tokens and costs at most $0.088 at long-context
rates, still under the $0.10 the plan's $1 share in the organic system is sized from. Plans
pinned to v7 and older keep their caps.

## A planning agent that fills every week (content-editorial-v9)

`content.plan` 1.0.0 pins `content-editorial-v9`. Production plans under v8 held one to seven
items for 52 slots: one model call read a frozen bundle, was told capacity is not a quota, and
had no way to research competitors, page families or search results. v9 replaces the call
with a planning agent and turns the quota rule around: judgment chooses which page comes next,
never whether a week gets one.

1. **Tin prepares** (`content_plan_research` activity): the v8 context and page inventory, plus
   one competitor list merged from earlier steps (the audit's buyer panel `competitor_names`,
   the keyword plan's search competitors with platforms left out, the newest competitor.watch
   report; a competitor whose site AI answers cite gains that source), the other sites AI
   answers cite, and the project files worth reading. It publishes this brief to
   `reports/content-plan/{run}/brief/` (`BRIEF.md`, `context.json`, `research.json`,
   `pages.json`).
2. **The agent plans** (`content.plan_research`, a Codex procedure child of the plan, $6
   ceiling): it reads the brief and the project's files whole (audit and keyword reports, Code
   map, brand guide, Start here plan, founder notes, Page decisions, traffic snapshot), checks
   competitors and search results on the web, and writes `PORTFOLIO.md` with a
   `content-portfolio/1` JSON block: strategy, competitors and their use, page families with
   their members, and opportunities in priority order. Formats: alternative, comparison,
   roundup, workaround (the manual way), answer, family hub, family page, use case, guide,
   refresh, update. Each item names its target search, evidence strength (measured, inferred,
   bet), why it beat the alternatives, why it can win, the metric it should move and its
   desk checks. The skill's `check_portfolio.py` runs Tin's checks inside the session so the
   agent fixes problems before it ends.
3. **Tin fills** (`content_plan_execute`): `content_plan_agent.normalize` keeps each usable
   proposal in order and leaves out, with a named reason, a new page the site already has, an
   item Page decisions rules out, an update of a page Tin does not know, a repeated title or
   page. Page decisions' refreshes the agent missed go first. `fill` then puts items on the
   weeks in order, each week up to its capacity; what does not fit is backlog. The plan's
   strategy names what Tin left out and which weeks stay open. Every item stays
   `needs_verification`; its format, evidence strength and target search travel with it
   (hidden plan-item fields, absent on older plans).

Nothing the agent paid for fails the run: an unusable portfolio still publishes a plan whose
report names every reason. Only an agent run that did not finish fails the plan, with its
reason. Plans pinned to v8 and older keep their single model call. The organic system's v8
recipe (`organic-traffic-v8`) sizes the plan's share of its pool for the agent.
