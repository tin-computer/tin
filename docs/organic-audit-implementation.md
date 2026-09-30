# Organic audit: first implementation milestone

## ELI5

Tin checks a bounded sample of your public website, asks an AI adviser a fixed set of
buyer questions, and saves what it found. You get a readable report and a structured
list for later technical-fix and content-planning workflows. Nothing edits your site.

This reference includes the version 0.2 scoring contract and its versioned follow-ups.
Use [feature status](feature-status.md) for current availability. Changes to policy or
question preparation must not reinterpret an older run's pinned evidence.

## Implemented

- `organic.audit`: one native Temporal implementation in the Organic Traffic Registry.
  Dashboard, HTTP, and MCP share a start service. No Luna conversation, GitHub,
  sandbox, new database table, or separate engine is required.
- Exact public HTTPS origin, selected US/GB/CA/AU market, English. Credentials, paths,
  queries, private addresses, and ambiguous hostnames fail preflight. Before v10, Tin
  checked DNS but did not fetch the site itself; v10 reads robots.txt, sitemaps and the
  static HTML of chosen pages on the verified hosts (see 0.4 below). DataForSEO owns its
  crawler's DNS and redirect isolation; off-host results are excluded, not silently
  attributed to the target.
- One DataForSEO OnPage crawl, at most 100 pages before v10 and a pinned, configurable
  cap from v10, rendering flags off, ordinary robots restrictions preserved. Short
  activity polls plus Temporal timers; one-hour elapsed collection deadline and 120-poll
  ceiling.
- Deterministic HTTP, redirect, canonical-target, title/description, duplicate-metadata,
  broken-link, and possible-orphan observations. Missing flags are unknown, not passes.
  Deliberate redirects and provider warnings require review, not automatic fixes.
- Public search-backed research creates a frozen buyer panel: one to three supported
  jobs, four question families per job, two fresh answers per question. A separate
  validator rejects identity/fit errors and brand leakage. No repair/resampling loop.
- Explicit OpenAI GPT-6 Luna route. Answering receives only the neutral buyer question
  and market, not the target or project memory. Final-answer citations are distinct
  from provider-returned sources. When a complete answer contains none of the validated
  target names, deterministic code records name absence without a paid grading call.
  A name occurrence still requires an answer-only judge to disambiguate genuine mentions
  and recommendations; positives require exact target-bearing quotes. Negative mentions
  are not recommendations; first in a list does not mean preferred first.
- Two separate branded probes about the offering and pricing/limitations. These expose
  what the API answered, not a verified accuracy score or part of the unbranded baseline.
- Content-review findings flag buyer-answer coverage worth investigating. Each candidate
  requires both answers to that question to be scored and neither to cite the website;
  an unrelated incomplete question no longer suppresses these review findings. A missing
  citation does not prove missing content, explain causality, or predict lift.
- HTTP/MCP stopping fences future paid work in Postgres, signals Temporal, and attempts
  to stop a known crawl. Accepted requests can still incur costs. An uncertain/in-flight
  canonical publication must finish reconciliation before its outcome can be reported.

## Durable handoff

One create-only, compare-and-swap commit publishes:

```text
reports/organic-audit/{run_id}/AUDIT.md       <= 150,000 bytes
reports/organic-audit/{run_id}/findings.json  <= 500,000 bytes
reports/organic-audit/{run_id}/evidence.json  <= 900,000 bytes
```

Every file fits the existing one-million-byte MCP reader. Findings include stable IDs,
status, severity, confidence, ownership, verification, dependencies, and next-action
category. Technical findings carry up to ten example URLs, a full affected count, and
a deterministic reference into the complete bounded crawl evidence. This avoids
repeating long URLs across every check.

The report names the findings digest; findings name their evidence digest. Evidence
pins the run, project, requested host, time, policy, and definition revision. Downstream
workflows should receive the immutable artifact revision, findings digest, and selected
finding IDs; recheck the issue and repository ownership before proposing changes.
Content planning can also consume the frozen questions. Nothing starts automatically.

From `organic-audit-v12` the same commit also writes `SUMMARY.json` beside these files
and, for the latest-started audit, copies it to `reports/organic-audit/LATEST.json`, both
under 64,000 bytes, for code workflows; see 0.8 below.

Existing run paths are never overwritten without a saved intent proving this run's
publication. Lost responses reconcile against first-parent history, all three file
contents, and exact changed paths. Later edits/deletion never cause resurrection.
Success, Activity, and final receipt commit in one Postgres transaction; finalization
needs no new storage read.

## MCP-first journey

1. `list_workflows(project_id)` finds the workflow; `get_workflow` returns its input schema.
2. `start_workflow` takes `workflow_id="organic.audit"`, inputs `site_url` and `market`
   (plus `refresh_questions` from v10), and an optional stable UUID `request_id`. Reuse
   it after an uncertain client response. HTTP retains the existing `Idempotency-Key`
   header. Neither path requires Luna.
3. `get_run` reads Postgres state and the artifact revision. `read_run_output` reads the
   report; `read_project_file` with that revision reads findings/evidence.
4. `stop_organic_audit(run_id)` stops future work before publication. HTTP uses
   `POST /api/workflows/runs/{run_id}/stop-organic-audit` and the same service.

Project membership gates every operation. Saved configurations are manual-only; there
is no new audit dashboard, comparison page, or review gate.

## Scoring and partial results in 0.2

The immutable definition pins `organic-audit-v2`. A scope receipt records this policy;
pre-existing receipts without that field remain v1. The worker accepts the exact old and
new instruction/policy pairs and keeps v1 grading, requests, reporting, and receipts intact.
It does not use the latest catalog to reinterpret an existing run.

- A complete answer without a validated target-name occurrence is a deterministic negative
  for mentions/recommendations, not a missing observation. Website citations remain independent.
  Empty, incomplete, oversized, missing-search, or unavailable responses are **never** negatives.
- Name presence is not itself a positive grade: namesakes and negative mentions still need
  semantic grading. The stricter judge prompt and exact supporting-quote checks reject unsupported
  positives. No paid grading repair or replacement-answer loop is introduced.
- Partial evidence retains `observed_metrics`, alongside explicit `completed`, `planned`, and
  `missing` counts. The comparable full-panel `metrics` field stays null until every planned
  observation is scored. Observed counts are not presented as full-panel percentages or a score.
- The generic Markdown report includes a bounded question table, scored counts per pair, and
  individual gap explanations. Only Tin-owned reason codes are rendered; provider errors,
  credentials, and arbitrary model-generated diagnostic text are never copied into the report.
- Valid content-review findings carry `audit_coverage=partial` when appropriate and cite only
  complete question pairs. They remain hypotheses requiring inspection, not missing-page claims
  or authorization for automatic execution.
- The publication receipt carries a small coverage summary. The existing Postgres result
  summary and Activity event receive it in the same atomic completion transaction. A partial
  report is visibly described as partial rather than merely saying the audit is ready.

Offline regression against the immutable Claw Messenger pilot avoids its two erroneous grading
calls and yields 23 scored observations with one still unknown. Ten complete question pairs
support three content-review findings. This is an **offline replay**, not a new measurement:
no provider calls were made and no historical artifact, receipt, or Activity event was changed.
The legacy report was also reproduced byte-for-byte using the v1 path.

## Spending and retry

Trusted-switchboard configuration: `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`,
`TIN_LITE_ORGANIC_AUDIT_MAX_COST_USD` (default **0**), and existing
`TIN_LITE_LUNA_API_KEY`. Credentials never enter Files, MCP inputs, E2B, or Temporal.
Missing DataForSEO configuration or a zero limit fails the shared start preflight.
The definition pins the model route, policy, instructions, and output schemas.

Before dispatch, Tin persists a conservative reservation, request fingerprint, and
intent. Reservations remain allocated for uncertain calls; they are not an invoice.
Ceilings: $0.05/basic crawl, $0.20/bounded search response, $0.03/no-tools structured
response. The largest panel reserves **$6.20** including branded probes. Lower budgets
produce explicitly unavailable components, never extra attempts or invented observations.

Each model request allows 60 KB input, 6,000 output tokens, default service tier, and at
most three search calls. Normalized responses cap at 16 KB, complete scored observations
at 21 KB, and crawl evidence at 240 KB. Oversized observations become unavailable,
rather than being silently truncated and scored.

Pricing was checked against [DataForSEO OnPage](https://dataforseo.com/pricing/on-page/onpage-api),
[OpenAI model pricing](https://developers.openai.com/api/docs/models/gpt-6-luna),
[hosted-tool pricing](https://developers.openai.com/api/docs/pricing), and the
[search-context limit](https://developers.openai.com/api/docs/guides/tools-web-search).
Basic crawling is $0.00015/page; its reservation includes headroom. Model reservations
allow repeated search-context processing and output under the caps without caching
discounts. **Recheck ceilings and reported usage before a live pilot: these are Tin's
reservations, not provider-enforced dollar caps.**

A DataForSEO tag is correlation, not idempotency. Uncertain submissions recover through
bounded ID List metadata matched to the original request/tag/time window. No match
never authorizes resubmission. Uncertain model results remain missing observations;
incomplete panels withhold comparable metrics while exposing labeled observed counts, per-question
gaps, and supported complete-pair review findings. A crash between saved intent and dispatch
can therefore sacrifice availability rather than risk another charge.

## Acceptance and remaining work

Local tests cover duplicate delivery, paid-request ambiguity, metadata recovery, budget
exhaustion, frozen-panel reuse, negative mentions, exact citation hosts, missing results,
and handoffs. Disposable Postgres schemas prove HTTP/MCP membership and idempotency,
projection rollback, stopping, and no revival. Local Temporal tests exercise retry,
stop signals, identifier-only activity inputs, and replay. Existing accepted Cloud
histories remain unchanged and replay-tested.

The first paid pilot below verifies the real provider host/schema path, authenticated MCP
execution and artifact reads, receipts, Activity projection, spending reservations, and
identifier-only Cloud history. It does not establish final invoiced costs, signed-in visual
acceptance, or a fully measured AI panel. Future pilots still require an approved ceiling;
do not repeat paid requests merely to replace missing observations.

## 0.3 — grounded GEO preparation (September 9, 2026)

The repeated Tin Lite panel failures exposed a preparation mismatch: a single searched
JSON response invented questions and their citations before it could see which source URLs
the API returned. The parser also ignored completed `open_page` and `find_in_page` URLs,
and collapsed missing search, failed search and excess calls into one opaque reason.

The new pinned `organic-audit-v3` separates three steps inside the existing activity:

1. Public research of the exact requested host, with host-filtered search and a concise
   factual note. A parent company or private project context cannot substitute for that site.
2. No-tool question drafting from that saved note and its explicit observed URL list.
   Questions are natural buyer hypotheses grounded in product facts, not alleged web quotes.
3. Deterministic source/identity/neutrality checks followed by independent semantic review.
   Freeze the accepted panel before any answer is measured.

There is at most one separately receipted research recovery after a known validation failure
and one draft correction using the same saved evidence. Both consume the original run's
budget. Unknown provider acceptance stops recovery; retries reuse completed receipts.
Neither answers nor scores select a replacement question or trigger replacement sampling.

The source design borrows `../thinklikeanagent/scripts/generate_prompt_panel.py` and
`website_optimizer/prompts.py`: ground preparation in evidence, use short natural questions,
prevent target leakage and forced choices, then freeze. The 36-prompt operator-reviewed
experimental panel, keyword-universe requirement, intervention framework and QSoA estimands
are not imported into this bounded, automatic API diagnostic.

The OpenAI Docs check confirmed separate search/open/find action shapes and documented source
inclusion/domain filtering. Completed page actions now count as observed URLs. V3 requires a
completed answer and at least one completed web call; failed additional tool attempts remain
visible in diagnostics rather than erasing successful research. The processed-call cap still
applies, failed-only/missing search is unmeasured, and answer evidence is never fabricated.
See [official web-search documentation](https://developers.openai.com/api/docs/guides/tools-web-search).

Safe response IDs, overall status and search-call statuses survive validation failures.
Both preparation attempts and their precise failure reasons are published in bounded evidence.
V1 and V2 policy/instruction/parser paths remain intact for pinned historical runs. The
artifact filenames, finding IDs, source-digest handoff, Temporal activities, and UI stay the same.

Local acceptance: 737 tests passed with disposable Postgres, including the complete frozen
panel's duplicate-execution test, source normalization, both correction paths, budget refusal,
unknown-acceptance refusal, V1/V2 compatibility and identifier-only Temporal replay. Live
acceptance is pending deployment and a new paid run; these tests alone do not establish GEO
measurement quality on the target site.

### 0.3.1 — buyer intent, not interview prompts

Live v3 preparation passed for Tin Lite and Claw Messenger, including one bounded
Claw draft correction. Human inspection found that accepting preparation was insufficient:
some questions were interview prompts or generic supporting-feature markets, and Claw's
panel treated descriptive taglines as brand aliases. The v4 instruction contract makes
each question a standalone buyer-to-assistant ask about the core purchased problem,
prefers one strong job over three padded jobs, and requires genuine brand aliases.
Semantic review checks these distinctions, while explicitly accepting reasonable buyer
problem hypotheses without demanding proof that someone already asked the question.
V3 instructions and existing panels remain pinned, unchanged. No answers are resampled.
V4 live acceptance is pending; prompt-contract unit tests cannot establish question quality.

The v3 Tin branded pricing probe also exposed a completed response with three completed
searches and a fourth attempt left `searching`. V4 accepts this bounded case only once
the processed-call ceiling was reached and the final response completed. Sources from
unfinished attempts remain excluded; diagnostics retain their status. Below-ceiling
unfinished calls, unknown statuses, and incomplete responses still fail closed. This is
an interpretation of the documented ignored-over-budget behavior, not a claim that the
unfinished attempt succeeded. V3 parsing remains unchanged.

## 0.4 — site checks, Search Console queries and honest coverage (organic-audit-v10)

An audit of tin.computer's own site reported almost nothing. The crawl stopped at 100
pages in sitemap order and skipped 37 of 137 sitemap pages, including the pages with the
most search impressions, yet the report said "completed". Search Console was read at page
level only and printed as a top-20 table. robots.txt, sitemap quality, noindex, H1,
language, structured data and speed were never checked, and duplicate pages competing for
the same searches went unnoticed. Each run also drafted new AI buyer questions, so AI
visibility could not be compared between runs. The pinned `organic-audit-v10` policy
fixes these.

### Which pages are inspected

- Tin reads robots.txt and every sitemap it names (or `/sitemap.xml`), following sitemap
  indexes and gzip files on the verified hosts only, up to 20 files and 5,000 URLs.
- It then chooses up to the page cap: the homepage, the pages with the most Search Console
  impressions (up to half the cap), one page from every URL section (first path segment),
  then the remaining pages taken in turn from each section. The first 20 chosen pages go to
  DataForSEO as `priority_urls`; the crawl follows the sitemap after them.
- The page cap comes from `TIN_LITE_ORGANIC_AUDIT_MAX_PAGES` (default 100, 10 to 300) and
  is pinned in the run's scope receipt, so a later configuration change does not alter a
  run. At DataForSEO's basic rate 300 pages cost $0.045, inside the existing $0.05 crawl
  reservation.
- A page counts as inspected when Tin read it. When sitemap pages were skipped the report
  says `partial: N of M sitemap pages inspected (page cap C)` instead of "completed" and
  lists every page not inspected, most impressions first. Search pages that were not read
  are listed separately.

### What Tin reads itself

Tin reads the static HTML of the chosen pages (and pages the crawl adds, within the cap):
HTTPS on the verified audit hosts only, every host resolved and refused unless all its
addresses are public, the connection pinned to the vetted address, redirects recorded but
never followed, at most 2 MB per page, four pages at a time, and robots.txt honored for
the `Tin-Organic-Audit` user agent. It keeps only extracted facts: status, `X-Robots-Tag`,
`<meta name="robots">`, canonical, hreflang alternates, `<html lang>`, H1 count, title and
the presence and types of JSON-LD or microdata. JavaScript is never run. Answers that look
like bot protection (HTTP 401, 403 or 429, or a 503 challenge page) are recorded as refused:
the page's checks are unknown, not errors, and a refused robots.txt or sitemap is unknown,
not missing.

The request deadline includes DNS resolution. Tin requests uncompressed HTTP bodies and
leaves facts unknown if a server ignores that request, so automatic HTTP decompression
cannot bypass the byte cap. Gzipped sitemap files keep their separate bounded decoder.
The optional PageSpeed key is sent in Google's `X-Goog-Api-Key` header, never in the URL.

### Search Console

With a matching connected property, the audit reads page rows (up to 1,000) and
query-and-page rows (up to 5,000) for the 28 days ending three days before the run. Each
read is its own zero-cost receipt (`search_console`, `search_console_queries`), and a
retry after a crash between them reads only what is missing. The checks:

- Competing pages: two or more pages with impressions for the same query. URL patterns
  that cover the same topics, such as `/compare/{x}-alternatives` and `/alternatives/{x}`,
  are grouped, with their page count, share of all impressions and clicks. Translated
  pages under a language prefix are left out of pattern grouping.
- Near page one: queries at average position 4 to 15 with at least 20 impressions.
- Low click-through: pages at average position 10 or better with at least 50 impressions
  that got under 35% of the clicks a conservative position curve expects, and at least
  three expected clicks. The curve is a planning aid, not Google data.
- Brand searches: a query naming the brand (host name, and the AI panel's name and aliases
  when measured) whose top page is not the homepage and either does not name the brand in
  its title or is a sign-in or account page.

### Site checks

- robots.txt: unreadable file, important pages (homepage, sitemap pages, search pages)
  disallowed for Googlebot, no sitemap reference, AI search crawlers (OAI-SearchBot,
  ChatGPT-User, PerplexityBot) blocked, and named groups that reopen paths the `*` group
  closes, since RFC 9309 has no inheritance. The report also states each AI crawler's
  stance, including the training crawlers GPTBot, ClaudeBot, Google-Extended and CCBot.
- Sitemap: none readable, unreadable files, URLs that are noindexed, redirect, error,
  name another canonical, are disallowed or use plain HTTP (judged only for URLs that were
  checked), ad landing URLs (`/offer/`, `/lp/`, `utm_` and similar), pages with
  impressions missing from the sitemap, and a uniform `lastmod`.
- Indexation: sign-in and account pages open to indexing, ad landing pages open to
  indexing, indexable pages whose canonical points elsewhere, noindexed pages that still
  get search traffic, and more than one canonical tag.
- On-page: missing or multiple H1, missing `lang`, a `lang` that differs from the URL's
  language prefix (for example `lang="en"` under `/nl/`), language sections without
  hreflang, and HTML over 2 MB.
- Structured data is reported only when found in static HTML; otherwise it is unknown.
  The audit never reports "no schema" from unrendered HTML.
- Speed uses PageSpeed Insights (mobile) for the homepage and the next two chosen pages
  when `TIN_LITE_PAGESPEED_API_KEY` is set, one page per poll, graded against LCP < 2.5 s,
  INP < 200 ms and CLS < 0.1, field data first. Without a key speed is unknown. The key is
  sent only to Google and never stored.

### One finding format and report order

Every finding carries Issue, Impact (high, medium, low), Evidence, Fix and Priority
(critical, high impact, quick win, long term) plus an area. Findings are ordered by area:
crawlability and indexation, technical foundations, on-page, content, authority. The
report opens with the coverage result, finding counts, the top five issues and quick
wins, and ends the findings with an action plan in the four priority tiers. Authority is
stated as not measured. Earlier finding fields (`id`, `check_id`, `status`, `severity`,
`urls`, `next_action` and so on) are unchanged, so downstream readers keep working.

`findings.json` is schema 3. `evidence_status` keeps its technical-crawl meaning, which
`organic.technical_fix` recomputes; `coverage_status`, `coverage`, `site_check_coverage`
and `summary` are new. Technical fix accepts v10 audits. Under `site-fix-v5` every finding
has a place: repaired in the repository, left to the content workflows, a manual step, or
a judgment call; see [Technical repair](technical-fix.md).

### Durable execution and compatibility

The Temporal workflow is unchanged. New work runs inside existing activities:
`organic_start_crawl` reads Search Console, site files and the page plan, submits the
steered crawl and reads the chosen pages; `organic_poll_crawl` finishes page reads and
PageSpeed Insights while the crawl runs; `organic_publish` builds the report from saved
receipts (`site_files`, `crawl_plan`, `page_facts`, `pagespeed`). Page reads save progress
after each bounded batch and resume without rereading. A reader that reads nothing records
the pages as unknown instead of stalling the poll loop. Page reads still unfinished at the
crawl deadline are published as partial evidence.

v1 to v9 pins keep their crawl request, report and inventory byte for byte; the crawl
request only gains `max_crawl_pages` changes and `priority_urls` under v10. An explicit
answer completion ignores the new site-evidence policy keys and reports that site files
were not collected. When evidence would exceed its 3 MB bound, query rows, unread page
records and then sitemap URLs are trimmed after findings are computed, and the counts are
recorded under `trimmed_for_size`.

### One buyer-question set per site and market

Before v10 each audit drafted new buyer questions, so AI-visibility results could not be
compared between runs. From v10:

- The first v10 audit of a site and market drafts the question set as before (public
  research, grounded draft, blind interpretation, review) and keeps the first two proposed
  buyer jobs, eight questions at most. The selection uses only the proposal's order,
  before any answer is measured.
- Each question gets three answers instead of two. Eight questions times three answers
  keeps the earlier maximum of 24 answers, so the answer spend and its reservations do
  not grow.
- Later audits of the same site and market in the project reuse the newest published v10
  question set unchanged, with no research or drafting calls. The earlier run's
  per-question results are saved as the `panel_baseline` receipt.
- `refresh_questions` (default false) drafts a new set; comparison starts again from it.
- The report names the question set and, when it was reused, shows a table of each
  question's mentions, website citations, shortlists and first choices, before → now, as
  positives out of scored answers. Unknown answers are neither negatives nor positives.
  The comparison is saved in `evidence.json` under `ai_visibility.comparison`.
- A panel records its own answer count. Panels drafted before v10 keep two answers per
  question and their earlier wording, so an explicit answer completion of an older run
  still works under v10 and older question sets are never reused.

### Limits and acceptance

- Language is checked against the URL's language prefix, not detected from the content.
- Search Console omits rare and anonymized queries; query totals are below page totals.
- These are observations and hypotheses, not ranking guarantees.
- The growth plan's program copy (`growth_plan_assets/programs.json`) is part of that plan's
  pinned contract; the 0.5 change below edits it once, so plan runs admitted before a deploy
  that changes it fail the worker's contract check instead of mixing contracts.

Question-set reuse, three answers per question and the comparison table are covered
by `tests/test_organic_audit_questions.py`.

Offline tests model tin.computer's site, Search Console rows and crawl
(`tests/organic_tin_fixture.py`) and require the report to show each defect the earlier
run missed: the overlapping `/compare/semrush-alternatives` and `/alternatives/semrush`
pages for "semrush alternative" with 33 pattern pages holding 53% of impressions and no
clicks; `/alternatives/moz` at position 8.5 and `/compare/ai-tools-for-startups` at 5.8
with no clicks; an indexable `/sign-in` ranking 1.5 for the brand with an old title, no H1
and a canonical to `/`; `/nl/` pages declaring `lang="en"` without hreflang; indexable
`/offer/` pages in the sitemap; and `/about` missing from the sitemap. v10 has since been
deployed and run in production.

## 0.7 — more angles, one AI measure and per-question review (organic-audit-v11)

v10 is deployed, so these additions live in a new pinned policy, `organic-audit-v11`
(catalog organic.audit 0.7.0), with findings schema 3. A run pinned to v10 keeps exactly
v10: its policy, AI instructions and schemas are unchanged (tests freeze their digests),
and every addition below is read from v11-only policy keys (`site_angles`,
`answer_ladder`, `unsearched_answers`, `access_check_pages`, `url_inspection_max_urls`,
`content_review_pages`, `decay_min_previous_clicks`, `max_redirect_hops`,
`cannibalization_min_impressions`, `min_panel_questions`, `next_action_from_repair_plan`).
Under v10 Tin makes none of
the new reads, runs none of the new checks, saves v10's page facts and evidence shape,
and asks PageSpeed for performance only.

### Per-question panel review

A production v10 audit measured no AI visibility because the reviewer rejected each
drafted panel over one ambiguous question (a generic "review work before it ships"
constraint), and v10 discards the whole panel. Under v11:

- The review (`PanelReview`) judges the identity as a whole and each question on its own.
  `accepted: false` still rejects the panel (identity, aliases or evidence); otherwise
  `rejected_questions` names each question not to ask, by number, with a reason.
- Tin drops those questions and keeps the panel when at least three remain
  (`min_panel_questions`). The panel records `dropped_questions`, its digest covers exactly
  the questions asked, and the report lists them under "Questions dropped in review".
- Fewer than three remaining (`panel_questions_too_few`), or a review naming a question
  the panel does not have or naming one twice (`panel_review_invalid`), redrafts through
  the existing recovery attempt with the review as the correction.
- The panel prompt anchors every question, including the constraint question, to the
  product's own category, never to a quality any tool could claim.

### The buyer prompt panel

When an `organic.prompt_panel` run has succeeded for the audited site, a v11 audit asks that
panel's questions instead of drafting its own (`prompt_panel`):

- There is no review step. The panel workflow publishes a panel only when every check in its
  `check_panel` passes, so Tin reads the newest succeeded `organic.prompt_panel` run at its own
  published revision. The panel must name the audited host and carry `"status": "ready"`.
- The audit asks at most `max_questions` (eight) of its 32 prompts, allocated to the four
  families by weight with the largest remainder, one prompt per stage before a second. The
  panel's core family weighs 0.40, so it gets three of the eight. Answers per question and
  the cost bound are unchanged.
- The product name, aliases and competitors come from the panel. There is no research,
  drafting or review call; `panel_preparation.method` is `buyer_prompt_panel`.
- The choice is saved once, so a retry asks the same questions. An earlier audit is reused
  only when it asked exactly this panel; a new panel starts a new baseline.
  `refresh_questions` still drafts a new set.

### What else Tin reads

- Each page's HTML facts now include text length, headings (the first eight H2/H3, and how
  many are phrased as questions), the lead paragraph, meta description length, viewport,
  Open Graph tags, images without alt text, external links, dates, authors, analytics tags
  and JSON-LD parsed as JSON and checked for the common types' required fields.
- Site files add `/llms.txt`, the plain-HTTP homepage (does it redirect to HTTPS?) and a
  made-up URL that should answer 404.
- A redirecting page is followed within the audited site, up to five hops, to find loops.
- The homepage and one selected page are read once as a browser and once with each AI
  crawler's user agent (GPTBot, OAI-SearchBot, ChatGPT-User, PerplexityBot, ClaudeBot,
  Claude-SearchBot). A CDN that serves the browser and refuses a crawler likely blocks it;
  CDNs can verify crawlers by IP address, so the finding says "likely".
- Search Console adds page rows for the 28 days before the audit window, and URL
  Inspection for up to ten key pages (the homepage, the pages with the most impressions and
  pages Tin found noindexed or canonicalized elsewhere), within Google's 2,000-a-day quota.
- PageSpeed Insights also returns Lighthouse SEO, accessibility and best-practice scores in
  the same call. Field data for the whole site is labelled site-wide, not the page's own.

### New findings

| Check | What it says |
| --- | --- |
| `rendering.content_not_in_html` | Content appears only after JavaScript runs; replaces sitewide "no H1" for those pages |
| `access.readers_refused` | Bot protection refused Tin's reader; the pages' other checks are unknown |
| `access.ai_crawlers_refused` | A crawler is refused where a browser is served (likely) |
| `robots.wildcard_blocks_other_crawlers` | `User-agent: *` closes the site to every crawler it does not name |
| `robots.ai_search_crawlers_blocked` | Now covers Perplexity-User, Claude-SearchBot, Claude-User and Bingbot too |
| `indexation.soft_404` | Missing pages answer 200, or pages say "not found" with status 200 |
| `redirects.loop` | A redirect path returns to an address already in it |
| `https.http_not_redirected` | The plain-HTTP homepage does not move to HTTPS |
| `indexation.not_indexed_by_google`, `indexation.google_canonical_differs` | URL Inspection results for key pages |
| `onpage.title_length`, `onpage.description_length`, `onpage.viewport_missing`, `onpage.image_alt_missing`, `onpage.open_graph_missing` | Page basics |
| `onpage.accessible_name_missing`, `onpage.form_label_missing` | Accessibility: links or buttons with no text, aria-label, title or image alt, and form fields with no label. These are the checks site health (`site.health_improve`) made on one page, now on every page the audit reads |
| `schema.invalid` | JSON-LD that does not parse or lacks required fields; missing recommended fields alone are not reported |
| `aeo.llms_txt_missing` | Low severity; no major assistant has confirmed it reads llms.txt |
| `aeo.dates_missing`, `trust.author_missing` | Articles without a date or author |
| `trust.about_contact_missing` | No about or contact page in the sitemap or pages read |
| `measurement.analytics_inconsistent` | A tag on some pages and not others (a hypothesis: bundled analytics are invisible) |
| `lighthouse.failed_audits` | Lighthouse scores under 90 with the failing audits |
| `search.decay` | Pages that lost at least 40% of 10+ clicks since the previous 28 days |
| `aeo.answer_structure` | The model's review of the top five content pages (see below) |
| `ai.cited_instead` | The sites AI answers cite when they cite yours in fewer than half |

Each v11 finding's `next_action` comes from the technical fix's repair plan
(`technical_repair_plan.next_action`): `technical_fix` for findings it repairs,
`content_plan` for copy, `manual` for steps outside the repository and `review` for
findings that ask for no change. v10 findings keep the values they were released with.

Cannibalization now treats translations of one page (`/de/pricing` and `/pricing`) as one
page and needs 10 impressions for a search before two pages count as competing.

A page that is marked noindex but still gets search traffic
(`indexation.noindex_with_search_traffic`) is a question under v11, not a critical
failure: a noindex set on the page itself is often deliberate, for example on event
pages, so the finding asks whether those pages are meant to stay out of search and the
technical fix asks the founder. v10 still reports it as critical.

### One AI measure

The organic audit now grades answers on the AI visibility audit's ladder: found (named, or
the site read or cited while answering), mentioned, evaluated against the buyer's needs,
shortlisted and picked first, and it reports where most answers stop. Each question also
gets one answer without web search, which shows what the model knows before it searches.
The judge's evaluation must quote a passage naming the target, like every other grade.

The panel decides the grading, not the run's policy: panels drafted for the ladder carry
`unsearched: true`, and an explicit answer completion of an older audit keeps grading the
way that audit did. Answer pages take their questions from the newest organic or AI
visibility audit, and Start here no longer suggests a separate AI visibility audit beside
the organic traffic system. `visibility.audit` is out of discovery and the organic
system (catalog 1.3.0) but stays runnable for saved configurations.

### Answer structure of top pages

One text-model call reviews the five content pages with the most impressions, from their
outline: title, H1, first headings, lead paragraph and top searches. The model judges only
whether the lead answers the main search (quoting the answering sentence exactly from the
lead), whether sections stand alone, and whether the page carries specific facts. Dates,
authors, sources and question-shaped headings come from the measured page facts. A result
that names a page that was not supplied, or quotes a sentence the lead does not contain,
is discarded and the review is reported as unavailable.

### Limits

- Crawler comparison, URL Inspection and the content review are offline-tested only.
- Subdomains are still out of scope. Backlinks, competitor pages and search features are
  not measured; they need a paid data source.

## 0.8 — a summary code workflows can read (organic-audit-v12)

Code workflows read project files through `ctx.files`, at most 64,000 bytes per file. On
tin.computer the audit's `evidence.json` was 188 KB, and `findings.json` can pass 64 KB too,
so weekly code workflows such as page decisions and the page tree could not read the crawl.
v11 is on main and may deploy at any time, so the summary is a new pinned policy,
`organic-audit-v12` (catalog organic.audit 0.8.0). A run pinned to v11 reads and writes
exactly what it did before; tests freeze its policy, AI contract and output digests.

### The files

```text
reports/organic-audit/{run_id}/SUMMARY.json   this run, never rewritten
reports/organic-audit/LATEST.json             the latest-started published audit's SUMMARY.json
```

Both stay under the pinned `summary_max_bytes` (60,000). A code workflow reads
`LATEST.json` in one call, without listing runs, and checks `host` before using it: the
file is per project, whatever site the audit covered. It is the only path an audit
publication may replace, and only with a summary whose `audited_at` (the run's start) is
not earlier than the one there: an audit that started earlier but publishes later, such as
a slow crawl, leaves the newer pointer alone. Checked at the destination head under the
project's write lock; a retry repeats its first attempt's choice until reconciliation shows
that attempt never landed. An answer completion re-reports an earlier audit without
reading its pages, so it writes its own `SUMMARY.json` and never `LATEST.json`.
`LATEST.json` is Tin's pointer, not a founder's file: an edit to it is replaced by the next
audit, the one exception to keeping later edits. Every run path stays create-only. The
publish receipt's `documents_sha256` still covers only AUDIT.md, findings.json and
evidence.json, so technical fix, keyword and content plans verify v12 audits unchanged; the
receipt adds `summary_path` and `summary_sha256`.

### What it holds

- `run_id`, `host`, `hosts`, `site_url`, `market`, `audited_at`, `policy_version`, the
  paths of the full files, and the findings and evidence digests.
- `coverage`: the report's coverage counts, plus crawled and read pages.
- `findings`: the total, counts by priority, the top five finding IDs, and `by_check`: per
  check its findings, the pages they affect and how many rows name it (`listed`; fewer than
  `pages` means the finding named only examples).
- `ai_visibility`: planned and completed answers, full-panel `metrics` (null while any
  answer is unknown) or `observed` counts, the ladder counts and main break, answers
  without web search, the question set, and the top five sites cited instead.
- `links`: how many read pages had links, how many links, and whether depth is `exact`
  (every sitemap page read, no link list capped, cut short or missing a link too long to
  keep) or `at_most`.
- `pages`: one row per crawled page, as lists under `columns`: `path`, `status`, `read`
  (Tin's own read), `indexable`, `noindex`, `canonical` (`self`, `missing` or the target),
  `title` and `description` present, `words`, `inbound`, `depth` and `checks` (positions
  in `findings.by_check`). Unknown values are null, never a guess.

Tin's page reader now keeps up to 250 distinct links to the audited site per page. Click
depth is the fewest clicks from the homepage through the pages Tin read, a redirect
costing none; inbound links count the read pages that link to a page. Neither looks past
the crawl or sees links that JavaScript adds. The link lists stay in `evidence.json`, and
are the first detail dropped when evidence nears its bound.

### Staying under 64 KB

Rows come in order of use: the homepage, then pages by search impressions, then by click
depth. Over budget, Tin drops columns in this order: `description`, `title`, `read`,
`canonical`, `words`, `checks`. It then drops the last rows. `truncated` is false, or names
the dropped columns and how many pages were left out; `pages.total` still counts every
crawled page. In the tests a synthetic crawl of 500 pages with 130-character paths comes to
59,913 bytes: all six columns dropped and 381 rows kept.

### For the weekly workflows

The page tree (`organic.site_architecture`) can read click depth and inbound links from
`LATEST.json` instead of reporting click depth as not measured, and page decisions and the
traffic snapshot can take the per-page checks from it instead of globbing
`reports/organic-audit/*/findings.json`. Those workflows need a change of their own to do so.
