# Searching: Podscan, the web, and which method suits which audience

## Podscan through `call_service`

The `podcasts` service is Tin's Podscan account (`managed.podscan`). Call it with
`call_service(service="podcasts", step=..., operation=..., arguments={...})`. Keep each
`step` id stable and unique (`lookalike_company_1`, `chart_tr_technology`); a completed step
replays, so never reuse one for a different request. The run has **30 calls**. Plan them
before the first call and keep about five for checking finalists.

| Operation | Arguments | Returns |
|---|---|---|
| `people.search` | `query`; `search_fields` from `name`, `company`, `occupation`, `industry`; `type` `person` (default) or `organization`; `per_page` 1-50; `page` | people with `company`, `occupation`, `guest_appearances` |
| `people.appearances` | `entity_id`; `role` `guest` (default), `host`, `sponsor`, `mention`; `since`/`before` dates; `per_page`; `page` | that person's episodes, each with its show |
| `episodes.search` | `query` (`"exact phrase"` works); `since`/`before`; `language`; `region`; `has_guests`; `min_audience`; `search_fields` from `transcription`, `title`, `description`; `order_by` `best_match` or `posted_at`; `per_page`; `page` | episodes with `guests` (name, company, occupation), `hosts`, `sponsors`, `is_branded`, a `match` snippet and the show's audience numbers |
| `podcasts.search` | `query`; `language`; `region`; `has_guests`; `min_audience`; `active_since`; `search_fields` from `name`, `description`, `website`, `publisher_name`; `order_by` `best_match`, `audience_size`, `rating`, `last_posted_at` | shows with audience numbers and `last_posted` |
| `podcasts.get` | `podcast_id` | one show in full: description, `style`, website, social links, `listed_email` |
| `podcasts.episodes` | `podcast_id`; `per_page`; `page` | the show's newest episodes with their guests |
| `charts.top` | `platform` `apple` or `spotify`; `country` (`us`, `tr`); `category` (`technology`, `business`, `entrepreneurship`, `marketing`, `all`); `limit` | ranked shows with `podcast_id` |

Results come back as `records` that fit the response bound, with `total`, `truncated` and
`next_page`. When `truncated` is true, ask again with a smaller `per_page` rather than the next
page. A result with `status: "unavailable"` (timed out, or Podscan failed) or `"not_found"` is
an answer: note it and move on. A failed call still counts against the 30. A rate-limit error
means wait a minute and call once more under a new step; after a second rate limit in a row,
or a `provider_quota` error (Tin's daily Podscan allowance is used up), stop calling Podscan
for this run, continue with the web, and say in `## What could not be checked`, in plain
words, which searches could not run.

What the data gets wrong, every run:

- **People are split.** One person often has several entity records, one per episode,
  sometimes misspelled ("Dana Kin" for "Dana Kim"). Search by company to collect them, and
  merge people by company before counting appearances.
- **"Guest" sometimes means "mentioned".** Check the episode's `guests` list before treating
  an appearance as a booking.
- **Keyword search is literal.** Topic searches return off-topic shows that used the words
  (crypto shows for "agents"). Read the show's description and recent guests before keeping it.
- **Name search can return the wrong feed** (a show's "early access" or network feed). Check
  the publisher and `last_posted`.
- **Audience numbers are estimates** that disagree with other sources, and they run low for
  B2B and technical shows: a show the whole industry knows can read 3,000. Use
  `audience_size`, `reach_score` and rating counts to compare shows in the same arena only,
  never to drop a show by itself, and say which numbers you relied on.
- **Name search matches words, not subjects.** `podcasts.search` for "health IT" returns news
  shows. Search for specific phrases, and find a field's leading shows through its charts.
- **`is_branded` marks a paid segment.** On a big show, check the founder interviews: when
  they are all branded, those slots are sold, and the founder needs another way in (a panel,
  comment on the news, a topic proposal). Record that in `notes`.
- **`has_guests` is sometimes wrong.** Recent episodes with named guests settle it.
- **`listed_email` is often a placeholder** (`noreply`, a hosting provider). It never becomes a
  pitch address on its own.
- **Little Turkish (or other non-English) coverage in text search.** Use charts and the web
  for home-country arenas.

## Methods, and which arenas they work for

Record every method that surfaced a show in its `found_by`. Every arena should end with at
least two methods tried; if one found nothing, say so in the report.

- `lookalike_company`: companies like the founder's (same category, stage or story) →
  `people.search` with `search_fields: ["company"]` or `episodes.search` with the company name
  → the shows their people went on as guests. The strongest method for the `craft` and `topic`
  arenas: a show that hosted someone like the founder will likely host the founder.
- `lookalike_title`: the founder's job title (`founding engineer`, `solo founder`) →
  `people.search` with `search_fields: ["occupation"]` → `people.appearances` for the ones in
  a similar company → their shows.
- `buyer_guests`: the product's buyers as guests (`indie hacker`, `bootstrapped founder`,
  `solo SaaS founder`) → their shows. Shows that host the buyers are shows the buyers listen
  to; this is the method for the `buyers` arena.
- `transcript_topic`: `episodes.search` on what the founder would talk about, `has_guests:
  true`, the last six months. Good for `topic` arenas; noisy, so read before keeping.
- `country_chart`: `charts.top` for the home country in `technology`, `business`,
  `entrepreneurship` and `marketing`. The method for `home` arenas: text search in the local
  language finds little.
- `category_chart`: `charts.top` for the product's main market (usually `us`) in the
  category the arena lives in, with `limit` 200, kept to the shows that fit the arena. The
  way to find the shows that carry weight in a field, which is where the `reach` picks come
  from.
- `local_youtube`: a web search in the local language for interview channels and video
  podcasts (many Turkish tech and startup shows are YouTube-first and missing from podcast
  indexes).
- `named_show`: a show from `include_shows` or the growth plan.
- `web_search`: a show you found by searching the web (a "best podcasts for indie hackers"
  list, a guest's own "I was on" page, the show's site).

## Verifying a finalist on the web

Before a show becomes a record, open its own pages:

- the newest episode and its date (the feed or the site, not a directory listing);
- who the recent guests were, to find the `similar_guest` and confirm it takes guests;
- how it takes guests: a guest form, an address it publishes for pitches ("pitch us at"), a
  booking page, or nothing stated (then the route is `direct_message`, `warm_intro` or
  `ladder`). A host's general or personal contact address is not a pitch address: that
  route is `direct_message`;
- whether it hosts guests at all. A show that only has its hosts talking gets a record with
  `takes_guests: false`, so the report shows why it was left out;
- any sign the guest pays: a price, a "production fee", "featured guest" packages, episodes
  marked sponsored by the guest's own company, or Podscan's `is_branded` on interviews.
  Record what you found and its source in `notes`. A fee is the founder's call, not a reason
  to drop the show.

## Finding the shows that carry weight

A list made only of shows that will say yes is a list of small shows. For each arena, also
look for the shows people in that field name first, and mark them `tier: "reach"` when they
book guests a step above the founder.

- **Signs a show carries weight:** a place in a `category_chart` or `country_chart`; a known
  network or publisher (an industry association, a media company, an analyst firm); many
  ratings; guests who lead well-known companies; other shows and newsletters quoting it.
- **What these shows book:** a topic or an argument, rarely a company. Some say so (a CEO
  may not come on to discuss their company). Pitch the founder as the person with a contested
  view or hands-on data on a question the show covers, and name the product once.
- **How to get in:** a topic proposal, a panel or debate slot, comment on a news story in the
  founder's field, or a guest form. Their founder interviews are often sponsored segments
  (see `is_branded` above); find the unpaid way in and say in `notes` which slots are sold.
- **When the only way in is paid,** it is still a candidate: say so in `notes` with the price
  and its source, and let the founder decide. A show that never takes outside guests is
  `takes_guests: false`.

