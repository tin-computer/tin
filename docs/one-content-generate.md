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
  (`content_refresh.plan_candidates`), plus Page decisions' refresh rows through the
  `page_decision_refreshes` seam. Those pages lead the bounded page inventory, so the model can
  name them by `page_id`. AI-visibility gaps are already research rows: the audit's content
  findings.
- Every opportunity names its kind, and the schema's kind enum lists only the kinds the run's
  evidence supports. Code checks that an answer cites a gap finding and is a new page, and that a
  refresh targets a marked page (code adds the page's `refresh:` source when the model left it
  out). An article update and a refresh of one page merge into the article.
- Older contracts (v1 to v6) keep their untyped schema and allocation.

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

## Retired entry points

`content.answer_page` 1.7.0 and `content.refresh` 1.1.0 get `public_discovery: false` and a
"Retired:" description, and leave the organic program's workflow list in
`growth_plan_assets/programs.json`. Nothing else in their definitions or procedure files
changed (a test pins the digests their 1.6.0 and 1.0.0 shipped with), so pinned runs, saved
schedules and the traffic system's weekly refresh keep working. `start_answer_page` stays in the
public plugin, as `start_visibility_audit` and `start_public_article` do.

## Versions

| Workflow | On main | Here |
| --- | --- | --- |
| `content.generate` | 1.8.0 | 1.9.0 |
| `content.plan` | 0.7.0 (`content-editorial-v6`) | 0.8.0 (`content-editorial-v7`) |
| `content.answer_page` | 1.6.0 | 1.7.0 |
| `content.refresh` | 1.0.0 | 1.1.0 |

No open `origin/feat/*` branch uses these versions. `organic.traffic_system` stays 0.5.0, as it
did when its children moved before: its next published revision carries the new children.

## Merging with #239

PR #239 (`feat/public-loop-workflows`) touches the same places:

- **programs.json.** Both branches remove lines from the organic program's workflow list next to
  each other, so git reports a conflict. Keep both removals: `content.refresh` and
  `content.answer_page` (here), `organic.mention_backlinks` and `organic.error_surface` (#239).
- **Page decisions' refresh rows.** #239 passes them to `content_refresh.choose` through its
  `planned_refreshes`. It must also return `planned_url_changes.refresh_candidates(...)` from
  `content_plan_sources.page_decision_refreshes`, so content.plan schedules those pages as
  refresh items. `plan_candidates` already merges `planned` rows; it can call #239's
  `candidates(findings, planned, host)` instead once both land.
- **Catalog.** Both add discovery flags, in separate blocks (`RETIRED_CONTENT_KEYS` here, the
  site-health set in #239), and `docs/workflows.md` regenerates with `scripts/dump_catalog.py`.

## Not done here

- `organic.traffic_system` 0.5.0 still starts and schedules `content.refresh` beside the plan's
  refresh items. The shared wait keeps them from refreshing a page twice; a v6 recipe could drop
  the separate refresh.
- Decisions asks the server's publish preview for every `content.generate` draft; only answer
  pages get the adapted Publish button. The plan editor shows no kind badge yet.

## Verification limits

Fixture tests only, on disposable Postgres: synthetic plans, audits, page reads, GitHub and
model results. No live model, Codex run, repository, website.change merge or deploy was used.
