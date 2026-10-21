# Searching: Podscan, the web, and which method suits which audience

## Podscan through `call_service`

The `podcasts` service is Tin's Podscan account (`managed.podscan`). Call it with
`call_service(service="podcasts", step=..., operation=..., arguments={...})`. Keep each
`step` id stable and unique (`sweep_grc`, `chart_tr_technology`); a completed step
replays, so never reuse one for a different request. The run has **32 calls**, and a
sweep counts as five of them. Plan them before the first call: one or two sweeps first, then
about ten calls on the shows that carry weight and the home arenas, and about ten for
reading finalists' episodes.

| Operation | Arguments | Returns |
|---|---|---|
| `people.search` | `query`; `search_fields` from `name`, `company`, `occupation`, `industry`; `type` `person` (default) or `organization`; `per_page` 1-50; `page` | people with `company`, `occupation`, `guest_appearances` |
| `people.appearances` | `entity_id`; `role` `guest` (default), `host`, `sponsor`, `mention`; `since`/`before` dates; `per_page`; `page` | that person's episodes, each with its show |
| `episodes.search` | `query` (`"exact phrase"` works); `since`/`before`; `language`; `region`; `has_guests`; `min_audience`; `search_fields` from `transcription`, `title`, `description`; `order_by` `best_match` or `posted_at`; `per_page`; `page` | episodes with `guests` (name, company, occupation), `hosts`, `sponsors`, `is_branded`, a `match` snippet and the show's audience numbers |
| `podcasts.search` | `query`; `language`; `region`; `has_guests`; `min_audience`; `active_since`; `search_fields` from `name`, `description`, `website`, `publisher_name`; `order_by` `best_match`, `audience_size`, `rating`, `last_posted_at` | shows with audience numbers and `last_posted` |
| `podcasts.get` | `podcast_id` | one show in full: description, `style`, website, social links, `listed_email` |
| `podcasts.episodes` | `podcast_id`; `per_page`; `page` | the show's newest episodes with their guests |
| `podcasts.sweep` | `topics` (1-10 search phrases); `language` (default `en`); `region`; `active_since` (default 120 days ago); `limit` 10-100 (default 60) | one call (counted as five) that runs four reads a topic (best matches, two pages; biggest matches; shows whose recent episodes match) and returns the active shows merged and ranked: `topics` that found each, `episode_matches` and a `matching_episode`, `about` and `style` (what the show is), `episode_count`, `release_frequency`, `avg_episode_minutes`, `last_posted`, audience numbers, `other_feeds`, and `feed_network` for a publisher with many near-identical shows. `failed_reads` names any read that did not answer |
| `charts.top` | `platform` `apple` or `spotify`; `country` (`us`, `tr`); `category` (`technology`, `business`, `entrepreneurship`, `marketing`, `all`); `limit` | ranked shows with `podcast_id` |

Results come back as `records` that fit the response bound, with `total`, `truncated` and
`next_page`. When `truncated` is true, ask again with a smaller `per_page` rather than the next
page. A result with `status: "unavailable"` (timed out, or Podscan failed) or `"not_found"` is
an answer: note it and move on. A failed call still counts against the 32. A rate-limit error
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
- **`has_guests` is usually wrong on small B2B shows**: every show on a hand-checked list of
  ten GRC interview shows read `false`. Never filter on it; recent episodes with named
  guests settle it.
- **Feed networks** publish dozens of near-identical, often machine-made shows under one
  publisher (`feed_network` on a sweep). They are not shows to pitch.
- **Some shows have no people in them**: a `style` such as "two AI hosts discuss" or
  "narrative episodes with fictional AI hosts" is a generated feed, not a show to pitch.
- **`listed_email` is often a placeholder** (`noreply`, a hosting provider). It never becomes a
  pitch address on its own.
- **Little Turkish (or other non-English) coverage in text search.** Use charts and the web
  for home-country arenas.

## The sweep comes first

For every arena except `home`, start with `podcasts.sweep`. It reads hundreds of shows in one
call, so the shows a field actually listens to are in front of you before you pick. Its rank
only orders them: read every show's `about` and `style`, and keep the ones whose premise is
the founder's argument or the arena's work. Choosing the topics is the judgment:

- 6 to 10 phrases per sweep, a mix of the field's own words (`GRC`, `"SOC 2"`, `vCISO`), its
  job titles (`CISO`, `recruiter`), and one or two broad ones (`"information security"`,
  `developers`). Quote multi-word phrases.
- Spelling variants a field uses on both sides of the Atlantic (`recruiting` and
  `recruitment`), and the words the field's shows use about themselves rather than the
  product's own words: a developer show says `developers` or `"software engineering"`,
  rarely `"AI agents"`.
- One sweep per group of arenas that share words; a second or third when an arena's field is
  different (a buyer arena in healthcare beside a craft arena in security).
- Too broad a topic (`AI`, `business`) fills the list with news shows and feed networks; too
  narrow (`"continuous control monitoring"`) finds nothing. A topic that finds nothing is an
  answer: say so.

Then search the ways below for what a sweep cannot see: lookalike guests, charts and the web.

## Methods, and which arenas they work for

Record every method that surfaced a show in its `found_by`. Every arena should end with at
least two methods tried; if one found nothing, say so in the report.

- `topic_sweep`: `podcasts.sweep` with the arena's topics. The first method for `buyers`,
  `craft` and `topic` arenas.
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

Before a show becomes a record, read its last three episodes (`podcasts.episodes`, or the
feed or site) and open its own pages:

- the newest episode and its date (the feed or the site, not a directory listing), and the
  gaps between the last three: a show that went quiet after years of weekly episodes may have
  stopped, and a gap followed by a new season is a show booking again;
- what the show is: the argument or job it is about, in a sentence, from its own description
  and recent episodes, not its category;
- who hosts it: their name, role and background, from the show's page or the host's own
  profile, and one line on why that matters to this pitch (a host who audits for a living, a
  lawyer co-host, a vendor that sells into the same buyers);
- who the recent guests were, to find the `similar_guest` and confirm it takes guests;
- how it takes guests: a guest form, an address it publishes for pitches ("pitch us at"), a
  booking page, or nothing stated (then the route is `direct_message`, `warm_intro` or
  `ladder`). A host's general or personal contact address is not a pitch address: that
  route is `direct_message`;
- whether it hosts guests at all. A show that only has its hosts talking gets a record with
  `takes_guests: false`, so the report shows why it was left out;
- who publishes it. A show run by a company that competes with the founder's product is
  still a candidate when it books outside practitioners: record the conflict in `notes`, and
  pitch the founder's argument, never the product. A show that only puts its publisher's own
  people or customers on air is `takes_guests: false`;
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

