# Numbers

Search Console is the only service: at most four calls, stable step IDs, bounded requests.
Filter pages to the blog folder with a `dimension_filters` page filter on the escaped
origin and folder, then drop returned URLs outside that folder, since a filter can
overmatch. Windows are the latest complete 28 days, the 28 days before, and the latest
complete 90 days; name the dates.

```text
call_service(service="gsc", step=ID, operation="search_analytics.read", arguments={
  "start_date": ISO_DATE, "end_date": ISO_DATE, "dimensions": DIMENSIONS,
  "row_limit": 1000, "start_row": 0, "dimension_filters": FILTERS})
```

1. `gsc_current_28`: [page], clicks and impressions per post for the latest 28 days.
2. `gsc_prior_28`: [page], the same for the 28 days before.
3. `gsc_last_90`: [page], 90-day clicks for the featured choice; a post missing from a complete
   response had no impressions in that window, which does not prove it is unindexed.
4. `gsc_first_90`: [date, page], first observed impression per post published in the window.
   Posts published earlier have no first-impression date here.

A truncated, refused or malformed response makes the affected numbers unknown. Per post,
28-day change is `(current - prior) / prior` only when prior is above zero; otherwise say
`new` with both counts. First-impression latency is days from publication to the first
observed impression; report the median over posts with both dates and how many are pending.
Never call a before-and-after difference causal.

From `/home/user/state/analytics/traffic-snapshot.json` (schema `tin.traffic_snapshot/1`, fresh
for 14 days), add each post's 28-day landing sessions and first-touch signups when it is
listed; otherwise say not listed.

The receipt's Numbers section gives: posts in the source, eligible, future-hidden, within two
clicks (or unknown), and a table of up to 30 posts by 28-day impressions with clicks and
impressions for both windows, the change, 90-day clicks and 28-day sessions.
