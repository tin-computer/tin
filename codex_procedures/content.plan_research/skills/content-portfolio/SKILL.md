---
name: content-portfolio
description: Plan a founder's weekly content program as a prioritized portfolio of pages (alternatives, comparisons, answer pages, page families, guides, refreshes) from Tin's brief and the project's files, and write it as content-portfolio/1.
---

# Content portfolio

You plan pages for one product's site, one or more a week, for the whole horizon in the brief.
Tin drafts them later, one at a time, and the founder approves each. Your plan decides what the
founder publishes for months, so think it through: each week's piece should be the best page
this product could publish next.

## 1. Understand the product and the buyer

Read before you plan:

- `wiki/INDEX.md` (project memory and the Code map), the brand guide, the Start here plan and the
  founder notes in `context/`: what the product does, for whom, what it costs, how it is used,
  what it integrates with, what it is not.
- `context.json` → `program`: host, market, horizon, weeks, `pieces_per_week`, `slots`, the
  instruction, and `existing_items` (work already planned; in a revision only `editable` weeks
  are yours to plan).
- `context.json` → `site_signals`: Page decisions (pages to refresh, rewrite, keep, merge or
  retire) and the traffic snapshot (pages that turn visitors into signups, pages with visits but
  few signups).
- `research.json`: every keyword with its measured volume, difficulty, ranking page and Search
  Console clicks and impressions; the keyword groups; the audit's content findings (buyer
  questions AI answers miss the site on); refresh rows (pages near the top results or seen but
  rarely clicked, in order of upside); competitor rows.
- `pages.json`: the text Tin read from up to 60 live pages, and every address Tin knows on the
  site (`site_pages`). An address is not content; read the page text or search before you claim
  what a page says.
- The audit's `evidence.json` (`ai_visibility`: the buyer panel, its questions, the answers AI
  assistants gave and the sites they cited) and the keyword plan's `evidence.json` (search
  competitors and Search Console rows) hold more than the summaries. Read them with a script
  when they are large.

Write down, for yourself: the priority buyer, the decision they are making, the alternatives they
weigh (named products and the manual way: a spreadsheet, an agency, doing it by hand), and the
reasons this product wins, taken from the project's files.

## 2. Settle the competitive set

`context.json` → `competitors` merges the audit's buyer panel, the domains AI answers cite, the
keyword plan's search competitors and competitor.watch, with the sources for each.
`corroborated` means two sources agree.

- Check each candidate on the web: is it a real alternative for the same buyer and job? A site
  that only ranks for the same words (a publisher, a directory, a namesake) is not a competitor;
  it may still be a page to beat for a search.
- Add a competitor the project's files or search results show buyers compare, even if no source
  listed it, and say where you saw it.
- For each real competitor decide its use: an alternatives page, a head-to-head comparison, a
  place in a best-tools page, or nothing (say why). Lead with the competitors buyers name most
  and that the AI answers recommend.
- The manual way is a competitor too: "{product} vs spreadsheets", "vs hiring an agency".

## 3. Find the page families the product really has

A page family is one intentional page type with a shared structure, where each member says
something true and different: a page for each workflow, integration, template, use case, role or
industry the product serves. It is not the same page with one word swapped.

- Find the members in the Code map, the product's pages and the brand files. Every member must
  exist in the product today; list them by name in the family's `members`.
- Each member page needs its own substance: what goes in, what comes out, the steps, who it is
  for, what it replaces. If you cannot say what makes member pages differ beyond the name, it is
  not a family; plan one guide instead.
- Plan the hub first, then members by demand and buyer value, one or two a week mixed with other
  formats. The rest of a large family can follow in later weeks.
- Check `site_pages` first: the site may already have the hub or some members; plan updates or
  the missing members only.

## 4. Build the candidate pool

Draft far more candidates than slots, then choose. Formats (`context.json` → `formats`):

- **refresh / update** (existing pages, `action: update_page`, `destination` = the page URL):
  Page decisions' refresh and rewrite rows; refresh rows near page one or seen but rarely
  clicked; pages losing traffic; a comparison page whose competitor changed. Improving a page
  that already ranks is usually faster than a new page. One item per page.
- **alternative**: "{competitor} alternatives" for one named competitor.
- **comparison**: "{product} vs {competitor}", or a three-way page with two leaders.
- **workaround**: the product against the manual way.
- **roundup**: "best {category} for {buyer}" that includes the product honestly.
- **answer**: one canonical page per core buyer question (the audit's buyer questions, question
  searches in the keyword inventory and Search Console). The answer is in the first paragraph,
  then a dated decision table, who it is best for and not for, and current pricing and limits
  from the project's files.
- **family_hub / family_page**: section 3.
- **use_case**: a problem or job the buyer has, solved with the product.
- **guide**: a how-to or explainer for a real task the product does.

Searches matter most where they are measured: prefer candidates backed by keyword volume,
Search Console impressions or a refresh row. A buyer question from the audit, a competitor buyers
name, or a search result you inspected is real evidence too. A strategic page with no measured
demand is allowed when the reason is clear; label it a bet.

## 5. Check the search results before you commit

For every new page in the first eight weeks, and any page you are unsure of, search its target
query:

- Who ranks: competitors' own pages, publishers' lists, forums, the product itself?
- Is there an AI overview or a direct answer? Prefer a sibling query where results are cleaner.
- Can this site's page be better than the top results for this buyer? Write that reason in
  `win_case`. If you cannot, choose a different query or drop the page.
- On a young site prefer queries with low difficulty (under about 30) and specific intent: a
  winnable search with 200 monthly searches beats a contested one with 5,000. A keyword missing
  from the difficulty data is unknown, not easy.

## 6. Choose and order

Score each candidate on what it does for the founder's goal (signups, customers), how close the
searcher is to buying, demand, winnability and fit with the product. Then order the portfolio:

- Weeks 1–4: refreshes with measured upside, the alternatives and comparison pages for the
  competitors buyers name most, the core answer page, and a family hub if the product has one.
- After that, mix every week: a bottom-of-funnel or answer page, a family member or use case, a
  guide or refresh. Do not run one format for many weeks in a row.
- The program grows the site. Plan a new page in most weeks; keep refreshes and updates to about
  half the slots or fewer, the ones with measured upside first, unless the site truly has
  little new left to add (say so in `strategy`).
- Cover the ground buyers search: an alternatives or comparison page for each real competitor
  the site lacks one for, an answer page for each distinct core buyer question (the audit's
  panel, question searches, AI answers that cite others), and the missing members of each page
  family. A family whose hub and members already exist is updated, and its missing members are
  new `family_page` items.
- Put pages near the pages that convert (traffic snapshot) ahead of others of equal value.
- Every page Page decisions marks for a refresh or rewrite gets an item or a line in `excluded`
  saying why not; Tin adds the ones you leave unanswered after your items.
- Plan nothing on a page Page decisions keeps, merges or retires, and no new page on the topic of
  one it merges or retires.
- Never plan two pages for one search intent, a new page that competes with an existing page on
  the site, or two items that change the same page.

Fill `program.slots` items. If you truly run out, return fewer and say in `gaps` what evidence
(a keyword lookup, a founder fact, a connection) would unlock more.

## 7. Write each brief

`brief` (at most 1,800 characters) is what the writer works from:

- the reader, their question in their words, and the target search;
- the angle and the argument: the answer, the proof to use (name the project files), the main
  objection, the next step to the product;
- the sections the page needs, and pages to link to and from (hub, product pages);
- what not to claim.

Comparison pages: the product's column states a capability; a competitor's column states its
scope, from the competitor's own published pages and units. A parity you cannot verify becomes a
verification task, not an assumption. Include an honest "when {competitor} is the better fit".

`verification` lists the desk checks before drafting: confirm each product claim in the named
files, confirm competitor facts on their current pages, re-read the page being changed. No live
product tests, sign-ups or sends.

`sources` cites what the item rests on, best first, at most 12:

- research rows and pages by their `source_id` (`keyword:…`, `group:…`, `audit:…`, `refresh:…`,
  `efficacy:…`, `competitor:…`, `page:…`);
- project files as `file:<path>`; the writer loads only files of 20 KB or less, so cite the
  small, specific files (the brand guide, a founder note) rather than large JSON;
- web pages you inspected as their `https://` URL.

`evidence_strength`: `measured` (keyword volume, Search Console, a refresh or Page decisions
row), `inferred` (buyer questions, AI answers, competitor presence, results you inspected),
`bet` (a reasoned strategic page without measured demand).

## 8. Write the portfolio

Write the output file as Markdown: a short human summary (buyer, competitive set, families,
the first month and why), then exactly one JSON block between these two marker lines:

```
<!-- content-portfolio.json:start -->
{ ...JSON... }
<!-- content-portfolio.json:end -->
```

```json
{
  "schema": "content-portfolio/1",
  "strategy": "The buyer, their decision, the alternatives, why the product wins (from the files), and how the order serves the founder's goal. At most 4,200 characters.",
  "competitors": [{"name": "", "host": "", "use": "alternative | comparison | roundup | none", "why": ""}],
  "families": [{"id": "", "name": "", "why": "", "members": [""]}],
  "opportunities": [
    {
      "id": "op001",
      "format": "alternative",
      "title": "",
      "target_query": "",
      "intent": "",
      "action": "new_page",
      "destination": "",
      "brief": "",
      "sources": [""],
      "evidence_strength": "measured",
      "why_this": "why it beat the alternatives you considered",
      "win_case": "why this page can rank or be cited",
      "metric": "what it should move",
      "rejected": ["candidates it beat"],
      "verification": [""],
      "competitor": "",
      "family": ""
    }
  ],
  "gaps": [""],
  "excluded": [""]
}
```

`opportunities` is in priority order; Tin fills the weeks in that order. New pages have an empty
`destination`; Tin chooses their address later. In a revision, keep the `id` of every existing
item you keep.

Finally run `python3 check_portfolio.py <output path> <brief folder>` from this skill's folder
and fix every problem it reports.
