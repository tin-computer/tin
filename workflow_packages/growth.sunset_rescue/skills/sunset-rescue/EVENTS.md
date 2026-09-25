# Forced-move events

A forced-move event is a dated change made by a vendor that pushes existing users to reconsider
the tool, whether or not they wanted to. Anything a user can simply ignore does not count.

## Event types

| Type | What it looks like | Typical deadline |
| --- | --- | --- |
| shutdown | The product or a major product line is closing | Service end date, often with a later export end date |
| acquihire_fold | Acquired, then folded into the buyer or closed | Stated wind-down date |
| free_tier_removal | A free plan or free usage level is removed or cut back hard | Date existing free accounts are downgraded or deleted |
| price_shock | List price or effective price for existing users rises about 30% or more, or moves to a per-seat or usage model that raises typical bills | Renewal or grandfathering end date |
| license_change | An open-source license changes to source-available or proprietary, or self-hosting terms tighten | Release after which the new terms apply |
| deprecation | An API, integration, app, region or core feature is retired | Removal date |
| market_exit | The vendor stops serving a country, segment or company size | Exit date |
| policy_change | Platform terms, data use or API access rules change in a way users object to | Effective date |

Small price changes, rebrands, new owners with no announced product change, outages and
layoffs alone are not forced-move events. Put them on the watch list as fragility signals
if they suggest a future event.

## Search patterns

Per tool, over the lookback window and forward into announced future dates:

- `"<tool>" shutting down` / `sunset` / `end of life` / `discontinued`
- `"<tool>" "free plan"` or `"free tier"` with `ending`, `removed` or `changes`
- `"<tool>" pricing change` / `price increase` / `new pricing`
- `"<tool>" license change` / `relicense` / `BSL` / `SSPL`
- `"<tool>" deprecated` / `deprecation` / `will be removed`
- `"<tool>" alternative` (a sudden run of new threads is a sign that an event happened)
- `site:<vendor domain> blog` or changelog pages, for the primary source

Open sweep, to catch tools not on the list (replace <job> with the Station 1 job):

- `<job> app shutting down <current year>`
- `<job> "export your data before"`
- `<job> "free plan" ending`
- `alternatives to <category> after shutdown`

## Evidence rules

- Primary source required: the vendor's own announcement, help article, changelog, status
  page, license file, or an email the vendor sent that is quoted in full in public.
- Record the announcement date, effective date, export end date and affected users exactly as
  the vendor states them. If they conflict, cite both and use the earlier date.
- If the vendor later extends or reverses the change, the latest primary source wins. Search
  for `"<tool>" extended` and `"<tool>" reversed` before scoring.
- Secondary coverage, such as news, forums or social posts, can locate an event and show user
  reaction, but it cannot set dates.
- An event you cannot confirm is "unconfirmed". It goes on the watch list and is never scored.
