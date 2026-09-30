# Measurement and consistency

These are fixed computations. Use the Python standard library (`csv`, `datetime`, `json`,
`hashlib`, `urllib.parse`, `collections`) for parsing, dates, grouping, graph walks,
invariants and hashing. Report each call's step ID, row count, truncation and dates.

## Search Console

All windows are inclusive dates as `search_analytics.read` takes them. Pick the latest
complete date `D` from a bounded date read; if that read fails, say complete-date confidence
is unknown and claim no complete weekly comparison. Twelve months is `D - 364` through `D`.
Pre-change weeks end on `D, D-7, ... D-49` and must all precede `applied_on` when it is known.

```text
call_service(service="gsc", step=ID, operation="search_analytics.read", arguments={
  "start_date": ISO_DATE, "end_date": ISO_DATE, "dimensions": DIMENSIONS,
  "row_limit": LIMIT, "start_row": 0, "dimension_filters": FILTERS})
```

Rows are `{keys, clicks, impressions, ctr, position}` plus `truncated` and `next_start_row`.
A truncated, refused or malformed response makes absent URLs unknown. Only a complete
filtered response gives a URL zero clicks for that window, and zero is not proof the URL is
indexed. Escape literal paths in a regex page filter and state the expression.

Plan: `G1_dates` [date] for 12 months; `G2_pages_12m` [page] for 12 months; `G3_baseline_a`
and `G4_baseline_b` [date, page] for the first and last four pre-change weeks, filtered to the
moved URLs; `G5_extra` only for one bounded missing group. A stop report may need only G1 and
G2. Follow-up: `G1_dates`, then `G2_weekly_old_new` [date, page] for the complete post-change
weeks, and G3/G4 only when old and new origins need separate reads. Compare seven-day counts,
never percentages, and name each week's dates.

The traffic snapshot's `visits.current.sessions` are 28-day landing sessions; report them as
`sessions_28d`, never as 12-month views.

## Click depth from the repository

Build a directed graph of page routes: an edge from each page to every internal `<a href>` its
template renders, plus the navigation components on every page that uses them (the header
and footer usually reach every page). Resolve dynamic routes to their observed URLs from the
sitemap, the snapshot and Search Console; do not invent instances. Breadth-first search from
`/` gives `click_depth`; a page no edge reaches is an orphan in the repository graph. Count
`inbound_links` as distinct source pages. Links built only in client code, or from data you
could not read, make the affected depth `not assessed`. Label these numbers "repository link
graph" and the audit's numbers "live crawl sample"; when both exist and disagree, show both.

## URLs and redirects

Normalize absolute URLs with `urlsplit`, verify the origin, and keep path spelling, slash
and query as the variant identity for redirects. Every 301/308 row has `old != new`, a target
that is not also a source, and a reason. A source that is already redirected points straight at
the final target; walk existing chains to their end and stop with `Status: incomplete` on a
cycle. Nothing points at `/` except an old home page. Duplicate sources or conflicting statuses
are errors. A whole-folder pattern is allowed only for an unchanged folder, and it says how
many enumerated sources it covers.

## Blocks

`redirects.json`, between `<!-- redirects.json:start -->` and `<!-- redirects.json:end -->`,
inside a ```json fence, read by the technical fix:

```json
{"schema": "site_architecture.redirects/1", "plan_id": "<this report ID>",
 "generated": "YYYY-MM-DD", "redirects": [
   {"old": "/features/reports", "new": "/product/reports", "status": 308,
    "reason": "url_change: /features moves to /product"}]}
```

`old` and `new` are site paths on this site. A domain move writes no rows here: its
redirects live on the old host, so the report lists them for the founder instead. `status`
is 301 or 308. At most 20 rows; with more, keep the most clicked and say how many
wait for the next plan.

`baseline.json`, between `<!-- baseline.json:start -->` and `<!-- baseline.json:end -->`:
`schema` = `site_architecture.baseline/1`, `plan_id`, `plan_hash`, `planned_change`,
`applied_on` (null in a plan), `gsc_complete_through`, `groups` (each with `id`, `old`, `new`
and `weeks` of `{start, end, clicks}`, null for unknown counts) and `evidence_notes`. A
follow-up copies the block and never changes saved numbers or `plan_hash`.

## Report identity

A plan prints a new report ID (UUID) and `Content hash:`. Compute SHA-256 over the report's
UTF-8 bytes with `Content hash: PENDING` and `"plan_hash": "PENDING"` in place, then replace both
with the lowercase hex digest. To verify, put PENDING back in both places and hash again. A
follow-up keeps the plan's ID and hash in its baseline block and hashes only its own report.

## Checks before delivery

Parse every marker block; check the redirect invariants above and that every must-keep or
unknown URL missing from known new routes has a redirect row or a 404/410 question; check
the table headers, the byte length (at most 64000), the lead, the dates and the sections for
the mode. On a failure, fix the report in this run or deliver a fresh `Status: incomplete`
report naming it.
