# One content.generate: articles, answer pages and page refreshes

Emre's v2 map of the organic traffic system
([proposed](https://tin-artifacts.vercel.app/organic-flow/proposed), 30 Sep 2026) gives copy
one writer: `content.generate` drafts the next item of the content plan, whatever it is.
`content.answer_page` and `content.refresh` stay registered for pinned runs and saved
schedules, but they leave discovery.

## Plan items have a kind

A plan item (`content.plan`'s `ContentItem`) has an optional `kind`:

| Kind | What it is | Shape |
| --- | --- | --- |
| `article` (or no kind) | A planned article, as before | `new_page` or `update_page` |
| `answer` | An AI-visibility gap: a buyer question the latest organic audit asked AI assistants, whose sampled answers did not cite the site (a `content.buyer_answer_coverage` finding) | `new_page`, no destination: it lands at the founder's answer-page route |
| `refresh` | An existing page that searchers see but rarely click, that ranks just below the top results, or that Page decisions marked | `update_page` at the page's URL |

The field is hidden from the v1 model schema and left out of the plan file when absent, so
plans written before kinds keep their exact bytes and brief digests, and every older item is an
article. A founder can also add an `answer` or `refresh` item by editing the plan.

`content.plan` 0.8.0 (policy `content-editorial-v7`) schedules all three:

- Research adds one `refresh:` row per page a refresh could fix (at most ten, most search
  impressions at stake first): the same pool `content.refresh` ranks
  (`content_refresh.plan_candidates`), plus Page decisions' refresh rows (see "What shapes the
  plan" below). Those pages lead the bounded page inventory, so the model can
  name them by `page_id`. AI-visibility gaps are already research rows: the audit's content
  findings.
- Every opportunity names its kind, and the schema's kind enum lists only the kinds the run's
  evidence supports. Code checks that an answer cites a gap finding and is a new page, and that a
  refresh targets a marked page (code adds the page's `refresh:` source when the model left it
  out). An article update and a refresh of one page merge into the article.
- Older contracts (v1 to v6) keep their untyped schema and allocation.
- **Competitor changes.** When a plan is built or amended, Tin reads the newest succeeded
  `competitor.watch` report (`reports/competitor-watch/{run}.md`) server-side. Each named
  competitor with a material change in its `## What changed` lines, backed by a page on that
  competitor's own site, becomes one item: a `refresh` of the site's comparison or alternatives
  page for that competitor when one exists (by URL or crawl title), else an article
  ("<name> alternative"). Items carry `source: competitor.watch`, the `evidence` page and a
  `competitor:<host>` research row with the report path; their first check is every claim
  about the competitor against that page. A competitor some item already compares against adds
  nothing, at most three are added a run, and no report (or a quiet one) changes nothing.

## content.generate 1.9.0 drafts all three

| Kind | Selected when | Drafted as | Checked by | Approval sends it to |
| --- | --- | --- | --- | --- |
| article | next in plan order | today's article | `content-draft.v3` | the delivery setting, unchanged |
| answer | next in plan order, and the founder chose where answer pages live | a page with a search listing, a 40 to 60 word direct answer, question headings, an FAQ and at least three cited sources | `content-draft.v3` plus `answer_page`'s own checks | `website.change`, at the saved route |
| refresh | next in plan order, unless its page still waits | `content.refresh`'s `tin-refresh.v1` proposal of exact old to new lines | `content-draft.v3` plus `content_refresh.validate_document` | the existing refresh applier (`deliver_refresh`) |

- **Kinds per pin.** The 1.9.0 definition lists `content_kinds`. A definition pinned before it
  (1.8.0 and older) drafts articles only: selection passes typed items over, an explicit typed
  item is refused, and preparation refuses one before compute. The organic traffic system's own
  first draft stays an article, because its delivery step adapts articles.
- **One output contract.** The validator stays `content-draft.v3`, the editorial pair of page
  and generation notes, so the sandbox image needs no rebuild. Preparation records `kind` only
  for an answer or a refresh, and that selects the extra checks; an article's prepared context
  is byte for byte what 1.8.0 prepared. Every kind can end in a no-draft assessment
  (`already_covered` when the site already answers the question, or the page already meets its
  searches).
- **Answers need a route.** Admission refuses an answer until `content/page-routes.json` has an
  `answer_page` route, with #244's question and the `save_page_route` call to make; a weekly
  schedule pauses with the same question. The selection pins the route. On approval with a
  repository, Tin starts `website.change` (`source: content_draft`), which adapts the page into
  the site's page registry at that route, keeps the search listing beside the copy, and refuses
  any patch under `content/answers/`.
- **Refreshes share one wait.** Preparation pins `content.refresh`'s evidence for the item's
  page (latest audit checks, Search Console rows, the page's current text, positioning and
  style) under the refresh receipt `{run}:content_refresh_prepare`. Both workflows' history reads
  that receipt, so a page waits six weeks after either one refreshed it, and the before and after
  results cover both. A waiting refresh item is passed over, not failed. The approved proposal
  goes through `choose_refresh` and `deliver_refresh` unchanged: Tin changes exactly the approved
  lines and nothing else.
- **One review policy per kind.** The definition keeps the article policy in `human_review` and
  adds `human_review_kinds` for answers and refreshes; Decisions shows each kind's own line.
  Feedback revises the same document for every kind.

## Covered items, the site's page list and refresh upside

Three fixes from the founder's first runs on tin.computer (1 October 2026).

**A draft that writes nothing closes without a review.** Run 1e474e10 found its item already
covered by an existing page, the right call, yet the run kept `review_required: true` with no
decision, so it read like a draft waiting in Decisions. Now:

- Finalization clears `review_required` for any validated no-draft result (already covered,
  brief needs revision, coverage unknown). No approval is manufactured.
- An already-covered result records the page that covers the brief (`covered_by` on the
  canonical commit receipt, "Already covered by <page>" in the run summary).
- Program progress marks the item `covered`, with that page, the reason and the run. The plan
  editor links the page, and the next manual or weekly draft takes the next item. The plan
  file itself is unchanged.

**content.plan reads the whole site, not only the pages it inspects.** The 1 October plan's
page list held the 60 inspected pages and counted 44 more without naming them. One of the 44
was /learn/ai-visibility-audit, so the plan proposed a guide the site already had. The v7
contract (content.plan 0.8.0) now builds `site_pages`, one row per normalized path, with every
source that lists it:

| Source | Where it comes from |
| --- | --- |
| `sitemap` | the audit's sitemap read |
| `search_console` | the audit's Search Console pages with impressions |
| `crawl` | the audit's crawl and its own page reads (failed reads and 4xx/5xx pages left out) |
| `tin_published` | pages Tin published itself and then found live (its saved page URL records) |
| `keywords` | ranking pages in the keyword research |

- **Bounds.** At most 2,000 pages and 200 KB are kept: most Search Console impressions first,
  then most sources. The rest are counted as `omitted`.
- **What the run saves.** The list goes to `reports/content-plan/{run}/pages.json` (a 250 KB
  file bound) beside the evidence. `evidence.json` and PLAN.md say how many pages came from
  each source.
- **What the model reads.** Up to 800 paths, with one letter per source. When the bounded
  input is tight, the list shortens before the planning sources do. Only the inspected pages
  carry text, and an update still needs one of them.
- **Dedupe.** Tin leaves out a proposed new page the site already has, recorded under "Already
  on the site" in PLAN.md (`already_on_site` in the evidence). A page matches by address (the
  title's slug is a listed page's last path segment), by title (a crawled page's title, without
  its site name), or, for an article, by topic. A topic match means the page's last path
  segment has two or more topic words, all in the title, and the title adds at most two more:
  "How to audit AI visibility for a SaaS brand" matches /learn/ai-visibility-audit, while
  "AI visibility tools: choose tracking or an actionable audit" does not. Answer pages match by
  address or title only, since an answer may answer a question an existing page leaves open.
  Items already in the plan are never dropped.

**Refreshes go where they can help.** content.refresh picked a page at position 53.5 (through
`aeo.answer_structure`) over /alternatives/moz at position 8.3 with 324 impressions, which the
same audit listed under `near_page_one`. Its choice sorted by impressions alone. Refresh
candidates now rank by realistic upside (`content_refresh.upside`):

1. `near_top`: the audit lists the page under `near_page_one` or `low_ctr`, or it ranks at
   roughly positions 4 to 20.
2. `weak_conversion`: the traffic snapshot shows real visits but few signups (see "What shapes
   the plan").
3. `possible`: any other page up to position 30, or one without a Search Console position.
4. `far`: beyond position 30. These come last, so a far page is offered only when no better
   candidate is left.

Within a tier, more impressions come first. Each candidate carries its `upside` tier and
reason. content.plan's refresh sources (the ten rows the model may plan a refresh from, which
also lead the inspected pages) use this order, and the v7 instructions tell the model to plan a
far page only when nothing nearer is left. content.generate's refresh items carry the same
`upside` for their page, and the page-refresh skill reads it. content.refresh's own weekly pick
is unchanged, so its 1.0.0 behavior on main stays as it was; while `organic.traffic_system` 0.5.0
still starts content.refresh, that pick still sorts by impressions.

## What shapes the plan

Added on 1 October 2026 at Emre's request. Besides the audit and keyword research, a v7 plan
(content.plan 0.8.0, policy `site_signals: decisions-snapshot-v1`) reads two files from #239's
weekly workflows at the plan's revision:

- **Page decisions:** `content/efficacy.md` from `organic.content_efficacy`, its `## Decisions
  block`, schema `content.efficacy/1`.
- **Traffic snapshot:** `analytics/traffic-snapshot.json` from `organic.traffic_snapshot`, schema
  `tin.traffic_snapshot/1`.

`content_plan_sources.site_signals` reads each file once. A file that is missing, older than 14
days, over 64 KB, unreadable or of another schema changes nothing. PLAN.md then says in one line
which file was not used and why (for example "Not used: page decisions (content/efficacy.md):
none saved yet."). When a file is used, PLAN.md says what it changed.

**Page decisions** (a `content.efficacy/1` block no more than 14 days old):

| Row | What the plan does |
| --- | --- |
| `refresh` | Tin adds a `refresh` item for the page (at most five a run, in the file's order, in the earliest week with room) unless an item already changes it. The item carries `source: organic.content_efficacy`, the page as `evidence`, the decision's reason in its brief and checks, and cites an `efficacy:` research row. |
| `merge`, `retire` (a `noindex` is a retirement) | The page is no refresh candidate. No item may update or refresh it, and no new article or answer page may cover its topic, matched by address or topic words as the dedupe matches. |
| `keep` | The page is no refresh candidate, and no item may update or refresh it. |
| `rewrite` | Shown to the model as a page that needs a new brief; Tin adds nothing itself. |

Tin leaves out a model proposal that breaks these rules and lists it under "Left out by Page
decisions". An item already in the plan keeps its place, and competitor.watch adds nothing for a
comparison page Page decisions cuts or keeps.

**Traffic snapshot** (a `tin.traffic_snapshot/1` file no more than 14 days old). Tin reads each
page's sessions, first-touch signups, activations, clicks and impressions, from the detailed
pages and the compact rows. Minimum counts keep small numbers from driving anything:

- **Which pages count:** only pages with 30 or more sessions in the snapshot's 28 days.
- **The site rate:** the counted pages' signups divided by their sessions. With fewer than 5 such
  signups, conversion is not used, and PLAN.md says so.
- **Converting pages:** at least 3 signups and at least the site rate, highest rate first (at
  most 10).
- **Weakly converting pages:** at least 100 sessions and under half the site rate, most sessions
  first (at most 5).

The snapshot changes two things:

1. **Order.** Proposals next to a converting page move ahead of the rest, each group in the
   model's order. "Next to" means an update in the same section (`/integrations/...`), or a new
   page whose title holds that page's last-segment topic words (two of them, or its only one:
   "Slack alerts for deploy failures" sits next to `/integrations/slack`).
2. **Refresh candidates.** Weakly converting pages join the refresh candidates in the
   `weak_conversion` tier: after pages near the top results, before the rest
   (`content_refresh.upside`). The model decides whether to plan them.

The model also reads a bounded view of both files (`site_signals`): refresh, rewrite and
do-not-plan rows, and converting and weak pages with their counts.

## Retired entry points

`content.answer_page` 1.7.0 and `content.refresh` 1.1.0 get `public_discovery: false` and a
"Retired:" description, and leave the organic program's workflow list in
`growth_plan_assets/programs.json`. Nothing else in their definitions or procedure files
changed (a test pins the digests their 1.6.0 and 1.0.0 shipped with), so pinned runs, saved
schedules and the traffic system's weekly refresh keep working. `start_answer_page` leaves the
ChatGPT plugin, as #239 does for the workflows it hides; `start_content_draft` drafts answer
pages there now.

## Versions

| Workflow | On main | Here |
| --- | --- | --- |
| `content.generate` | 1.8.0 | 1.9.0 |
| `content.plan` | 0.7.0 (`content-editorial-v6`) | 0.8.0 (`content-editorial-v7`, with the site's page list and the refresh upside order) |
| `content.answer_page` | 1.6.0 | 1.7.0 |
| `content.refresh` | 1.0.0 | 1.1.0 |

No open `origin/feat/*` branch uses these versions. `organic.traffic_system` stays 0.5.0, as it
did when its children moved before: its next published revision carries the new children.

## Merging with #239

PR #239 (`feat/public-loop-workflows`, rebased on main at d69d337) merges with this branch with
four conflicts, and needs these changes (checked by a trial merge and the full suite):

- **programs.json conflicts.** Both branches change the organic program's workflow list next to
  each other. Keep both: drop `content.refresh` and `content.answer_page` (here) and
  `organic.mention_backlinks` and `organic.error_surface` (#239), and keep #239's five loop
  packages.
- **docs/workflows.md conflicts.** Regenerate it with `scripts/dump_catalog.py`.
- **docs/public-plugin.md and test_mcp_public.py conflict** over the plugin count (below).
- **#239's freeze test must allow the retirement.** `test_planned_url_changes.py` pins
  content.refresh's definition at main's 1.0.0 digest. Here it is 1.1.0 with
  `public_discovery: false` and a "Retired:" description, and nothing else changed
  (`test_one_content_generate.py` pins that). Compare the definition without `version`,
  `description` and `public_discovery`, or drop content.refresh from that freeze.
- **Page decisions' refresh rows.** content.plan reads `content/efficacy.md` itself
  (`content_plan_sources.page_decisions`, a small local parser of the `content.efficacy/1`
  block), so it does not import `planned_url_changes`, which moves between PRs. Nothing needs
  wiring at merge time.
- **#239's onboarding test.** `test_onboarding.py` reads `rows["content.refresh"]` as a visible
  sample; content.refresh is hidden here, so it needs another sample (`content.generate`).
- **Plugin count.** Both branches remove ChatGPT tools, so `docs/public-plugin.md` and
  `test_mcp_public.py` need one recount: 27 native workflows and 16 packages, for 43 starts,
  with `start_answer_page` among the hidden tools.

## Not done here

- `organic.traffic_system` 0.5.0 still starts and schedules `content.refresh` beside the plan's
  refresh items. The shared wait keeps them from refreshing a page twice; a v6 recipe could drop
  the separate refresh.
- Decisions asks the server's publish preview for every `content.generate` draft; only answer
  pages get the adapted Publish button. The plan editor shows no kind badge yet.

## Verification limits

Fixture tests only, on disposable Postgres: synthetic plans, audits, page reads, GitHub and
model results. No live model, Codex run, repository, website.change merge or deploy was used.
The follow-up's tests cover a no-draft run closing without a review and marking its item
covered, the weekly draft moving past it, the page list naming pages the crawl sample missed,
the dedupe, and a position-8 `near_page_one` page outranking a position-53 page with more
impressions. Run 1e474e10's row in production keeps the flag it was saved with; only runs that
finish after a deploy close without it.
