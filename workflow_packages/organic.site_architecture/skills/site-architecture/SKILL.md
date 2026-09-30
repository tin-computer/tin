---
name: site-architecture
description: Plan a site's page tree, URL and navigation rules and the redirects a URL change needs, from its repository, Search Console and project evidence, with a weekly follow-up after the change.
---

# Boundary

The repository at `/home/user/project` is a read-only checkout of the connected GitHub
repository; the project state at `/home/user/state` holds Tin's files. Read both locally.
Use `call_service` only with the `gsc` alias, at most five calls, with stable step IDs. No
live HTTP, browser, crawl, external search or other workflow. State any provider failure or
truncation; never retry an uncertain request under another step ID.

The only delivered file is `reports/organic/site-architecture/SITE_ARCHITECTURE.md`. A
previous copy is input, never evidence of this run. Change no route, navigation, redirect,
metadata or provider data. Other workflows own neighbouring work: `organic.technical_fix`
applies redirects, metadata and BreadcrumbList markup; `organic.content_efficacy` decides
per-page merges and retirements; `content.refresh` adds links inside page copy;
`content.plan` picks hub and comparison pages; `content.diagram` draws the tree;
`organic.audit` crawls the live site. Name them instead of doing their work.

# Inputs and evidence

1. Defaults: mode `plan`, planned_change `none`, key_pages derived. Validate `new_origin` as
   an HTTPS origin with no path, query or fragment; it is required only for `domain_move`.
   Validate `new_paths` line by line as one site path or `old -> new`; reject duplicates,
   conflicts, loops and off-site URLs. Never infer a planned change. A missing required
   input yields the short stop report naming it.
2. **The repository (routes, navigation, links).** Find how the site is built (framework and
   router) from its manifests and config, and list every public page route, including dynamic
   routes and the content source behind them. Find the header, footer, mobile menu and any
   sidebar or breadcrumb components, and record each navigation link with its label and the
   `path:line` that defines it. Record internal links between page templates. Find existing
   redirects (framework config, `vercel.json`, `netlify.toml`, `_redirects`, middleware) and
   the trailing-slash setting. A link built only in client code, or only from data you cannot
   read, is `not assessed`, not absent. Say which directories you read.
3. **Project files.** In `/home/user/state` read, when present:
   - `analytics/traffic-snapshot.json` (schema `tin.traffic_snapshot/1`), fresh for 30 days:
     28-day search and visits per landing page. It has no 12-month series.
   - `content/efficacy.md`: the JSON block after `## Decisions block` (schema
     `content.efficacy/1`, within 14 days). Its `url_changes` are proposals the technical fix
     already asks about; list them under **Already proposed by Page decisions** and never repeat
     them in `redirects.json` unless this plan changes a target.
   - `reports/organic-audit/*/findings.json`: choose the newest by the date inside its file.
     Its orphan, depth, cannibalization and sitemap findings are a live sample of at most 100
     pages; on a larger site they are a sample, not the whole site.
   - `wiki/INDEX.md` (`### Code map` for where routes and navigation live, `### Feature map`
     for labels), `brand/BRAND.md` for the words the site uses, and the newest content plan.
   - The previous report, especially in `follow_up` mode.
   Limit each read to 64000 bytes and each glob to 100 paths. A stale, oversized or unparsable
   file is named and set aside; a missing optional file never triggers another workflow.
4. Normalize URLs to site paths while keeping distinct slash, case, host, query and
   pagination variants for redirect work. Take the site origin from the snapshot or audit host;
   reject rows from other sites. Derive key_pages (at most 10) from home, pricing, signup and
   feature pages ranked by 12-month Search Console clicks; name what you dropped.
5. Use MEASUREMENT.md for Search Console windows, click depth and every calculation. Source
   every number with its window.

# Plan

6. Continue past the gate only when one of these holds, and name which:
   (a) planned_change is not none;
   (b) a key page or a clicked page is an orphan or more than 3 clicks from home, in the
       repository link graph or the audit;
   (c) at least three pages of one non-blog type (comparison, alternative, integration,
       template, free tool, data study) have no index page that links them;
   (d) about 500 or more indexable URLs are observed;
   (e) a section three or more levels deep has no breadcrumbs;
   (f) the audit or Page decisions show pages competing for one search, or utility pages
       competing with the home page for brand searches;
   (g) a section has had no search clicks in 12 months while its pages stay in the sitemap.
   Missing evidence is never a negative finding. With no trigger, write only a stop report:
   a 2-3 sentence lead, `Status: stopped`, `Why it stops`, `What was checked` (route count,
   navigation links read, sources) and `Run again when`, listing every trigger and which ones
   could not be assessed.
7. **URL map.** Build the working inventory from repository routes, the audit's sitemap and
   crawl, the snapshot, 12-month Search Console pages and existing redirects. Leave out
   tokenised and one-time paths (UUID-like segments; `/invite/`, `/connect/`, `/auth/`,
   `/callback`, `/verify`, `/reset`, `/magic`) and print only how many were left out. Table
   columns: `url, type, source, clicks_12m, impressions_12m, sessions_28d, redirect_source,
   click_depth, inbound_links, nav_location, must_keep`. `must_keep` is `yes` when clicks,
   sessions or an existing redirect source is positive, `no` only when all are known to be
   zero, otherwise `unknown`. If the table cannot fit, keep moved and must-keep rows, and give
   the total and the omitted count; the working inventory stays complete.
8. **Redirects.** For a redesign, URL change or platform move, compare every must-keep or
   unknown URL with the new routes; each one missing needs a redirect or a 404/410 question.
   Without `new_paths`, list the must-keep URLs and say the new routes are unknown; never
   invent targets. For a domain move, map each preserved path to `new_origin`. A redirect
   goes to the closest real equivalent: the same slug, then the same-type hub, then a reasoned
   judgment. Never point unrelated pages at `/`. No target may also be a source; point an
   already-redirected source straight at its final target. Keep variants that have traffic.
9. **Page tree and URL rules.** Draw an ASCII tree with a URL on each node, existing sections
   first, changing only triggered sections. Put the URL rules in one JSON object between
   `<!-- url-rules.json:start -->` and `<!-- url-rules.json:end -->`: per page type
   `pattern`, `parent_hub`, `nav_location`, `owning_workflow`; plus lowercase, hyphens, words
   not IDs, no dates, and the trailing-slash policy the repository already uses. Follow the
   site's existing conventions.
10. **Navigation.** From the components you read, give the header, footer and mobile menu as
    they are and as proposed. Order the header by clicks when comparable, otherwise say the
    order is a judgment. Plain labels, pricing included, signup last. Footer groups: Product,
    Resources, Company, Legal. Breadcrumbs only in sections three or more levels deep, with
    the current page unlinked. Every navigation link is an `<a href>` in server HTML. For
    pricing, signup and the top feature pages, give click depth, inbound links and where
    navigation links them; aim for one click from home. Rationale sources when relevant:
    Google says click depth from home matters and slashes do not; NN/g found users do not
    drop off after three clicks and hidden desktop navigation lowered discoverability by more
    than 20% (2016). The three-click and one-line menu rules are rules of thumb, not
    measurements of this site.
11. **Hand-offs.** List up to ten clicked pages that lack a link from their section hub, for
    `content.refresh`. Give `content.diagram` a brief with section groups, navigation zones and
    at most 32 URL nodes.
12. **Redirects for the technical fix.** Put every 301/308 this plan needs in one JSON object
    between `<!-- redirects.json:start -->` and `<!-- redirects.json:end -->`, inside a
    ```json fence, as MEASUREMENT.md specifies. Then a readable table with `old, new, status,
    reason, clicks_12m`. Say plainly that the next technical fix asks about each redirect and
    adds the agreed ones to one pull request in this repository's own redirect config (name the
    file), in the same deploy as the page moves. 404/410 and noindex are questions for the
    founder, never redirect rows. Keep redirects at least a year; indefinitely for URLs with
    links. For a domain move, tell the founder to run Search Console's Change of Address and
    keep the old domain and its certificate.
13. Capture the baseline: eight complete pre-change seven-day Search Console click totals per
    moved group (fewer if fewer exist, stated), in the `baseline.json` block of MEASUREMENT.md.

# Follow-up

14. Read the previous report's `baseline.json` and `redirects.json` blocks. With no complete
    baseline, write a one-line stop note without provider calls. The change date is
    `applied_on`; without it, stop and ask for it rather than guess a week.
15. Run weekly through week 6. At weeks 2 and 6, compare old-plus-new clicks and impressions
    per moved group with the saved weeks. Flag clicks below the lowest baseline week only when
    that week is above zero; otherwise write `no baseline`. At week 6, flag a new URL with zero
    impressions while its old URL still has impressions, only with complete data for both.
    Bring in orphans and depth only from an audit newer than the change.
16. Follow-up sections: lead, `Status`, `Search Console before and after`, `Orphans and depth`,
    `URLs to check by hand` (up to 20, with the expected status, one hop and final target),
    `Fixes` (proposals; new redirects go into a fresh `redirects.json` block) and the carried
    `baseline.json` block. Never claim a redirect works without a live fetch.

# Report

Open with the main finding in two or three sentences, then the reasons and evidence. A plan
has these sections in order: Summary, Evidence, URL map, Page tree, URL rules, Navigation,
Already proposed by Page decisions, Hand-offs, Baseline, Redirects for the technical fix.
Print a new report ID (UUID) and the SHA-256 content hash near the top as MEASUREMENT.md
describes. Short headers, calm plain sentences, every number with its source and window.
Unknown stays unknown, never zero.
