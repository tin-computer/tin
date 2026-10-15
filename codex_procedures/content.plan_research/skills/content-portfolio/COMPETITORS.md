# Competitor research

How to settle the competitive set before planning. Run it after you have read the brief, the
Code map, the brand guide and the founder notes, and before you build the candidate pool.

## 1. Frame the search

Write these down from the project's files, not from the leads:

- **Buyer**: who pays, and what they are trying to get done.
- **Job**: the outcome in the buyer's own words, before they know the category exists.
- **Category phrases**: two or three things a buyer types for this kind of product. Include one
  narrow phrase (the exact category, such as "iMessage API") and one broad one (the wider shelf,
  such as "SMS API for AI agents").
- **Stack**: the tools, platforms and frameworks the product plugs into (from the Code map and
  integrations), because rivals often live in the same ecosystems.
- **Leads**: the brief's competitor leads and the earlier research, if any. They are where you
  start, not a checked list.

## 2. Search four channels

Each channel finds a different kind of rival, and no single channel finds most of them. Run all
four. Budget per channel: about 10 searches and 6 opened pages.

1. **roundups**: search "best <phrase>", "top <phrase> tools", "<phrase> alternatives" for the
   narrow and the broad phrase. Open at least three roundup or comparison articles and list
   every product they name. Count how many roundups list each one. This channel works when the
   category is crisp. For a new kind of product, roundups are organised by job and return many
   loose fits, so judge closeness, not frequency.
2. **job_and_stack**: search the job with the stack attached: "<outcome> with <stack tool>",
   "<stack tool> <outcome> integration", "how to <outcome> without <the usual workaround>". Then
   look inside the stack's own ecosystems: integration and plugin directories, MCP server lists,
   skill and template marketplaces, and the framework's docs pages that list channels or
   providers. This channel finds framework-native rivals and do-it-yourself substitutes.
3. **launches**: recent launches that search engines rank poorly. Search Hacker News with
   `site:news.ycombinator.com "Show HN" <job terms>` (and "Launch HN"); if you can open pages,
   `https://hn.algolia.com/api/v1/search_by_date?query=<job terms>&tags=story` lists the newest
   first. Search GitHub sorted by stars:
   `https://github.com/search?q=<job terms>&type=repositories&s=stars`. Also check Product Hunt
   launches for the job. Prefer the last 18 months.
4. **comparison_pages**: once you have a competitor you are sure is direct, open its own
   comparison or alternatives pages (`/compare`, `/vs/...`, `/alternatives`) and its pricing
   page. Vendors in a crowded category list nearly the whole field there. Never start this
   channel from a lead you have not checked. A domain that only ranks for the same words, or a
   general tool an AI answer happened to cite, pulls in a different category.

Skip searches anchored on the product's own brand ("<brand> alternatives"): young brands have
no coverage, and generic names collide with unrelated things.

### With subagents

When `spawn_agent` is among your tools, the channels run as subagents. This is not optional:
one agent searching four channels in turn runs out of patience after a few queries each.

1. Right after you have framed the search (step 1), start four subagents, one per channel, in a
   single step. Call `spawn_agent` directly, never from inside `exec`: collaboration tools are
   not in the `exec` namespace.
2. A subagent cannot read the project, so give each one a self-contained brief: the frame from
   step 1, its channel's method from above, the budget (about 10 searches and 6 opened pages),
   the leads, and the reply format below.
3. While they search, read the rest of the project files and draft page families.
4. Then call `wait_agent` for all four and merge their candidates. If spawning fails, or a
   subagent errors or returns nothing, run that channel yourself. Never wait on a subagent that
   is not running.

Reply format for a subagent:

```json
{"channel": "roundups", "queries": ["..."], "candidates": [{"name": "", "host": "", "evidence_url": "where it was named or launched", "why": "one line"}], "notes": "what worked, what was noise"}
```

## 3. Check every candidate on its own site

Merge the channels' candidates with the leads, dedupe them (a product often has two domains, or
a name shared with something else), and open each one's own homepage, product page or README.
Write one line on what the site says it does and who it is for. Then label it:

- **direct**: same buyer and same job. A buyer evaluating this product would shortlist it.
- **adjacent**: the same buyer, but it covers only part of the job, or the same job for a
  somewhat different buyer.
- **diy**: the way a buyer does the job without buying a product like this: an open-source
  project, a self-hosted setup, scripts or skills, an agency, a spreadsheet.
- **drift**: a real product that does another job: general automation, analytics, a CRM, a
  publisher or directory. A buyer would not pick it instead of this product.

Drop dead sites and anything you cannot find. A candidate you did not open stays out of the set.
A row from the earlier research keeps its label unless its site now shows otherwise.

Only direct, adjacent and diy competitors may get an alternatives or comparison page. A drift
product can still be a page to beat in a search, but it gets no "vs" page; Tin leaves such
pages out.

## 4. Record it

In the portfolio JSON, `competitors` holds every checked competitor with `label`, `does`,
`buyer`, `evidence_url` (the page you opened), `channels` (which channels found it), `use` and
`why`. `competitor_research.channels` lists, per channel, the queries you ran and the names it
found, and `started_from` says whether you started from the earlier research. Tin keeps this set
in `research/competitors.json` for the next plan.
