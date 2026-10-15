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

## Every listed page, its outline and its nearest pages (content-editorial-v10)

`content.plan` 1.1.0 pins `content-editorial-v10`. On 2026-10-08 a production plan's
brief held 65 site addresses but the text of 15: the inventory reads only pages with search
signal (refresh rows, plan destinations, ranking pages). The agent read `pages.json` but never
fetched a page it had only the address of, and the title-only duplicate check let through new
pages for questions `/docs` and four agency guides already answered. `content.generate` then
judged two of them `already_covered` and a third `needs_replanning`, each a paid run.

- **Tin reads every listed page.** After the up to 60 candidates (read at 6,000 bytes as
  before), `page_inventory` reads every other page in `site_pages`, most impressions first, up
  to 150 (a runaway guard; `omitted_known_pages` counts the rest), at 1,500 bytes, eight at a
  time. Every page keeps its `title`, meta `description` and `outline`: its H1 to H3 headings
  in order (30 at most, 100 characters each; a page's own `<header>` counts, navigation and
  footers do not). The outline shows coverage the opening misses: `/docs` opens with a
  quickstart and reaches "Receiving Messages" far below it. Reading is Tin's code, not an
  instruction the agent may skip; the brief's `pages.json` (bound raised to 2 MB) carries it all.
- **Tin names each new item's nearest pages.** `normalize` scores every read page against the
  item's title and target search (`nearby_pages`): words weighted by how rare they are on the
  site, so the brand name counts for nothing, matches in the title, description, outline and
  address counting double, crude stemming so "replies" meets "reply". The home, about, legal
  and sign-in pages and redirect aliases are skipped. Up to three pages carrying at least 35%
  of the item's weighted words join its `source_ids` (content.generate loads them as evidence;
  they join only while the item's pages stay under 24 KB of its 64 KB context), and its first
  check says to read them and choose `already_covered` or an update when one already answers
  the reader. The rationale records them (`nearby`). Nothing is left out on this score.
- **A refresh brief stays within a refresh.** content.generate's refresh replaces the title,
  meta description, H1, lead and up to three body paragraphs in place; it cannot change a table,
  list, link, section or FAQ. The planner's skill and `formats` say so, and `normalize` plans a
  refresh whose brief names a table, row, column, section or FAQ as an `update`, with the reason
  in its rationale (a refresh asking for "a compact choice table" was judged `needs_replanning`).

Plans pinned to v9 keep its page reads, briefs and normalization exactly.

### The plan opens with its targets, and the founder approves it first

The 10/8 outside run found nothing to approve before the first article: the targets, the
prompts and the plan were files, and no view said which keyword or question each page serves.

- **Targets and pages.** `PLAN.md` opens with `## Targets and pages`
  (`content_plan_targets`), computed from the plan and its research, never written by a model.
  One row per keyword group (served by an item citing the group or one of its keywords, or
  targeting one of its searches; demand is its keywords' summed volume and impressions), per
  page searchers see (refresh rows and Page decisions' refresh or rewrite, served by the item
  changing that page), per competitor the planner chose to write about (served by an item
  naming it), and per buyer question in the audit's panel (served by an item whose title,
  target search and brief carry at least half of its distinctive words). Targets with no page
  and pages with no target are listed under it. `evidence.json` keeps the mapping
  (`editorial.targets`). On the 10/8 plan of a production project it showed the two largest
  keyword groups with no page and ten late bets serving no target.
- **Approval before drafting.** `content.plan` 1.1.0 declares a review policy. Only a
  program's first plan requires review (`db.create_run`): a later batch follows the approved
  plan, and a revision preview keeps its own apply. The first run publishes the plan as before,
  then requests review instead of finishing (`request_human_review`, artifact `PLAN.md`, the
  card's line from `review_line`), and the workflow waits on its `approve` signal behind
  `content-plan-approval-v1`, with no timer; `content_plan_approved` records the approval and
  the success. Until then `content.generate` refuses with "Approve the content plan in
  Decisions before drafting from it." (`ContentPrograms.approval_hold`, checked by discovery
  and selection before any run or spend). Declining ends the run; nothing drafts from that
  program. Programs saved before 1.1.0 have no review policy and draft as before.
- **Request changes on the waiting plan** (`content_plan_feedback`). The plan's reader offers
  Request changes like a draft's ("Revise plan"; HTTP `request-changes`, MCP
  `request_workflow_changes`). The feedback becomes an ordinary plan revision
  (`begin_revision` over every week not started, plus one metered revision run of the planning
  agent); a receipt (`content_plan_feedback_v1`) ties it to the waiting run. While it runs the
  plan stays readable, approving it is refused ("Tin is revising this plan from your
  feedback…") and a second request is refused. When the revision run finishes, Tin applies it
  (the existing exact base check) and points the same waiting review at the revised `PLAN.md`
  (`repoint_review_artifact`, only while no decision exists), so the card never moves and an
  approval binds the copy shown; the reader notes "Revised once by Tin, from your feedback". If
  the founder edited the plan meanwhile, the revision is discarded and the review says their
  edits were kept; if the revision run fails, the review says so and the plan they read stays
  approvable. A plan decided meanwhile leaves the revision as an ordinary preview.


## Competitor research the plan can stand on (content-editorial-v11)

`content.plan` 1.2.0 pins `content-editorial-v11`, and its planning agent is
`content.plan_research` 1.2.0. Two production plans on 2026-10-14 took their competitors from
the merged list alone. For one project the list held three directories and publishers that rank
for the same searches. The audit adds no competitors because its research reads only the
target's own site, and the keyword plan's overlap domains were the site's search neighbours
(airdroid and digitaltrends for an iMessage API). The agent seeded its searches from the domains
AI answers cite, and the plan wrote alternatives pages for general automation tools while the
product's direct rivals went unnamed. An offline comparison of six discovery methods on two
products found that no single method finds most rivals. Category roundups work for a crisp
category, job-and-stack queries and recent launches (Hacker News, GitHub) find new and
framework-native rivals, and a competitor's own comparison page lists the field only when that
competitor is checked first. Seeding from a cited domain or a search neighbour pulls in another
category.

- **A competitor research phase.** The skill's `COMPETITORS.md` frames the search from the
  project's files (buyer, job, two or three category phrases, the stack), searches four channels
  (`roundups`, `job_and_stack`, `launches`, `comparison_pages`), and opens every candidate's own
  site to label it `direct`, `adjacent`, `diy` or `drift`. The portfolio records each competitor's
  label, what its site says it does, the page opened (`evidence_url`) and the channels that found
  it. `competitor_research` records the queries per channel. The procedure's check script asks
  for labels and evidence and notes a channel with no queries.
- **The channels run as subagents.** `content.plan_research` pins
  `codex_api.PARALLEL_PROCEDURE_CONTRACT`: v5 with `parallel_requests: 5`. Under it the relay
  admits up to five unsettled requests instead of one, and the controller enables Codex
  multi-agent (`agents.max_concurrent_threads_per_session=4`, `agents.max_depth=1`). Only the
  lead thread's messages and turn end count as the procedure's result. Every request still
  reserves its own $1.93 maximum before dispatch. If a reservation is refused while other
  requests hold the room, only that request is refused (`parallel_reservation`), not recorded as
  the run's cause and not a spending stop; with nothing in flight the ceiling stops the run as
  before. The planning agent's ceiling is $12 (five reservations are $9.65), and so is the
  standalone plan's. An image without the controller change ignores the field, and the agent
  runs the channels itself. A parallel request refused only because others hold the slots or
  the room waits up to 170 seconds for one to settle; Codex does not retry, so a refusal would
  fail the turn that sent it. The parallel contract is pinned only when the run can reserve
  six requests at once ($11.58: the credits available, the project's limits and the parent's
  budget). With less, the subagents would starve each other for credits, so the agent gets
  the one-request contract and runs the channels itself. On staging a $4 wallet held two
  reservations, and long multi-search responses kept the rest waiting past the old 120 seconds.
- **Leads, not a list.** Tin adds two sources to the merged leads: the comparison pages the site
  already has (`/alternatives/<name>`, `/vs/<name>`, `/compare/<a>-vs-<b>`, `/<a>-vs-<b>`,
  `/<name>-alternatives`, from the site's page list) and the set an earlier plan checked. Leads
  rank by what their sources say: the site's comparison pages and the checked set first, search
  neighbours last, and a rival checked as drift is ranked down. The brief presents them as
  leads to check, in plain words ("ranks for the same searches (often a publisher, directory or a
  tool for another job)"), and lists the sites AI answers cite as where answers come from, not
  competitors.
- **The checked set is kept.** Tin writes the labelled set to `research/competitors.json` with
  the plan's files (a replaceable file: the newest plan wins). It is not written when the agent
  labelled nothing. The next plan's brief starts from it: within 30 days the agent checks new
  leads and searches launches again; older, it runs every channel.
- **No page against a tool for another job.** `normalize` leaves out an alternatives or
  comparison item whose competitor is labelled drift, with the reason. A roundup may still
  include it. The plan's target table skips drift competitors.

Plans pinned to v9 or v10 keep their briefs, schema and normalization. The contract and ceiling
follow the planning agent's output validator, so a new admission of a pinned plan's agent also
gets the parallel contract and the $12 ceiling. Its older prompt never starts subagents, and a
ceiling is a runaway guard, not what a run buys.
