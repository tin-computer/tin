# Refreshing existing pages

> Retired from discovery in `content.refresh` 1.1.0: `content.generate` 1.9.0 drafts the
> refresh items the content plan schedules, with the same evidence, document, wait and applier
> (see [one content.generate](one-content-generate.md)). Pinned runs and saved schedules keep
> running as described below; both workflows share one six-week wait per page.

`content.refresh` (1.0.0) rewrites the parts of one existing page that keep it from earning
clicks, then changes exactly that text in the site's source after the founder approves. It runs
on demand or weekly, one page a run, and the organic traffic system starts it before drafting
new pages.

## What a run does

1. **Prepare (code, no model).** Tin reads the latest finished `organic.audit` and its Search
   Console evidence, then chooses one page:
   - It considers pages the audit flagged with `search.low_ctr` or `search.near_page_one`, plus
     `search.decay` and `aeo.answer_structure` when the audit reports them.
   - It picks the page with the most impressions at stake. (content.plan's refresh candidates
     and content.generate's refresh items rank by realistic upside instead: pages near the top
     results first, pages beyond position 30 last. See
     [one content.generate](one-content-generate.md).)
   - It skips a page whose refresh is still waiting for review, was approved but has not
     reached the site, sits in an open PR (or one Tin could not check), or went live less than
     six weeks ago, so each refresh has time to show whether it worked and no page is refreshed
     and paid for twice. An approval that never reaches the site stops holding its page after
     six weeks.

   Tin reads the page's live title, meta description, H1, opening answer and paragraphs
   with the audit's bounded site reader. It also lists the project's positioning files
   (`brand/BRAND.md`, founder notes under `context/`, `wiki/INDEX.md` and the Start here plan)
   and the writing style guide. Everything is saved once per run, so a retry works from the
   same page and text. When no page is due, Tin writes a short report and finishes with no
   compute and no review.
2. **Write (Codex procedure).** The `page-refresh` skill proposes the fewest replacements that
   answer the page's searches: the title and meta description, and the H1 and opening answer
   when they miss the search. Body paragraphs change only when the audit flagged decay or weak
   answer structure. The document shows each change as Now, Proposed and Why, and carries the
   exact replacements in one `tin-refresh.v1` JSON block. The worker refuses a document whose
   old text differs from the page's current text in any way, whose new text is empty,
   unchanged, over its length limit or contains markup, or that drops the results table.
3. **Review in Decisions.** The founder reads the old and new text side by side and picks a pull
   request or Publish now, or keeps the refresh in Tin. "Do this for future drafts" saves the
   pick for the program when the refresh belongs to a saved workflow.
4. **Deliver (code, no agent).** Tin reads the repository bundle and looks for each approved old
   text itself, in plain, HTML-escaped and JSON-escaped forms, with whitespace allowed to wrap
   and whole words only ("Pricing" never matches inside `PricingTable`).
   - The page's own file is the one that holds most of its texts; a text found elsewhere must
     have exactly one holder, and each file must hold the text exactly once.
   - The new text is written for where it sits: a TS/JS, Python, JSON or YAML string escaped
     for its quote, an HTML attribute or text escaped as HTML, or JSX text with an entity for
     a quote the source did not already write raw. Text Tin cannot place safely stops
     delivery.
   - Tin then checks that each approved text became its new text and that nothing else in any
     file changed.
   - If a text is not in the source verbatim (a title built from a template, for example) or
     appears in several places, delivery stops and says which text and why. It never guesses.
   - Several changed files land as one commit, so a failure never leaves main half changed.
   - A retry after a failed delivery reads the current head when the failed attempt opened no
     pull request or commit; an attempt that did, or may have, replays.

   A commit to main is live when it lands; a PR is live when GitHub reports it merged.

## Results

Every later run reads Search Console for each refresh that has been live for 28 days plus
Search Console's three-day delay. It compares the 28 days before going live with the 28 days
after: clicks, impressions, click-through rate and average position, filtered to that page.
Tin saves each result once and copies the table into every new refresh document and into the
no-refresh report.

## Cost

The procedure's ceiling is $2.50. A run reads one page, a few positioning files and the style
guide, then writes a short document. That comes to about 150,000 input and 6,000 output tokens,
roughly $0.45 at list price, so the ceiling is about five times the estimate
(`PROCEDURE_MAXIMUMS` in `codex_api_pricing.py`). Delivery makes no model call.

## Where it lives

- Pure logic: `src/tin_lite/content_refresh.py` (selection, page text, document validation,
  source matching and the patch check).
- Preparation and history: `src/tin_lite/content_refresh_sources.py`.
- Delivery: `ContentDelivery.choose_refresh` and `ContentDelivery.deliver_refresh` in
  `src/tin_lite/content_delivery.py`.
- Procedure: `codex_procedures/content.refresh/`.
- Traffic system: `organic_system_refresh` and `first_refresh` in
  `src/tin_lite/organic_system_activities.py`, the refresh child budget in `valid_child` in
  `src/tin_lite/billing.py`, policy
  `organic-traffic-v5` (see [content continuation](organic-content-continuation.md)).

## Verification boundary

Offline tests cover selection, the six-week wait, open and pending refreshes, the 28-day
measurement, the no-refresh report, document validation (including a paraphrased old title as
the plausible but unusable result), source matching and escaping in TSX, TS, Python, HTML,
JSON and YAML, the patch check, commit and PR delivery with a fixture GitHub, retries after
main moves, one commit for several files, and the traffic system's first refresh with billing
enabled. No live
page, Search Console property, GitHub repository or paid Codex run was used.
