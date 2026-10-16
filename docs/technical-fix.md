# Technical repair from an organic audit

Technical fixes reach the site through `website.change` with `source: audit`: the audit finds;
website.change plans, fixes and publishes. This page describes `site-fix-v5`, the repair rules
that run applies to the latest audit (`technical_repair_plan.py`, `technical_fix.py`,
`technical_batch.py`, `technical_fix_live.py`). Change rows, approvals, the two publish modes
and the merge rule are in
[website.change, phase 2](website-change.md#phase-2-technical-changes-from-the-latest-audit).

## site-fix-v5: everything the audit found

One run takes a whole audit. `technical_repair_plan.py` gives every audit check one
place:

- **Fixed in the repository**, grouped by the kind of change: indexing directives
  (noindex, canonicals, soft 404s), the sitemap, robots.txt, merges and redirects (after
  a cannibalization decision, redirect chains and loops, HTTP to HTTPS), page structure
  (title and description from existing text, H1, lang, hreflang, viewport),
  accessibility (image alt, accessible names, form labels, Lighthouse markup items),
  structured data, Open Graph tags that mirror the existing title and description, and
  internal links (orphans, broken links).
- **A judgment call**, when the fix depends on what the founder intends:
  - which URL pattern survives a merge;
  - whether a noindexed page with search traffic should be indexed;
  - whether a canonical to another page means a duplicate;
  - which language a mislabeled page is in;
  - whether to allow AI search crawlers, open a closed wildcard group, unblock important
    pages or repeat general rules in named groups;
  - whether to add an llms.txt.
- **Copy**, left to the content workflows: title and description rewrites (length,
  duplicates, low click-through, near page one, decay), answer structure, dates, authors,
  about and contact pages, and content gaps. A technical repair never writes marketing copy.
- **Manual**, outside the repository, with where the step lives: a CDN or bot protection
  refusing crawlers, Core Web Vitals, JavaScript-only rendering, oversized HTML, analytics,
  server errors, an unreachable robots.txt, and asking Google to index pages.

### Judgment calls through MCP

`preflight_website_change` with `source: audit` returns the plan without starting anything,
including `decisions_needed`. Each decision has an `id` (the finding), the `question`,
`options` (`value` and `label`), Tin's `suggestion` and `why`. The `ask` field tells the coding
agent to answer from the codebase and what it knows about the product, and to ask the founder
only the ones it is unsure of, in one message. Answers go back as
`decisions: ["finding_id=choice", ...]`, to the preview to check them and to the start to run
them. A finding whose decision is unanswered, or answered "keep", gets no change row and is
listed.

### One run, one pull request

- The run re-reads the live site: robots.txt, the sitemaps it names, and up to 40 pages.
  It drops findings that are already fixed, then binds the repository. Other open PRs
  never limit it (10/16): a busy repository always had more open-PR files than Tin read,
  so every run stopped. The founder's review reconciles two PRs that change one file.
- The repository snapshot leaves out files over 10 MB. Images, video, fonts and built
  bundles don't count; any other file Tin couldn't read (a source or data file over 10 MB, a
  link, a submodule) ends the run **failed**, and its reason and report name each file. The
  same holds when Tin can't read the snapshot at all or the plan is too large to hand to
  Codex. A patch can't write over a large file Tin left out.
- Codex gets the plan (up to 30 findings, most urgent first) through website.change's
  `site-repair` skill. It traces each finding to its source in the site's own framework,
  fixes it where it applies to every affected page, runs the repository's checks plus
  `git diff --check`, and writes a PR body with the fixes by group, the decisions followed,
  anything left for a later run, manual steps, and checks run and skipped. The skill carries
  site health's repair rules (see below).
- Before the PR opens, the worker checks the patch:
  - at most 20 files and 800 changed lines, files at most 200 KB and new files at most
    20 KB, text only;
  - no dependencies, lockfiles, package manager or workspace settings, CI, deploy or
    build settings, secrets (`.env*`, `.dev.vars`), `.github/` or `.gitmodules`; the one
    deploy setting it may edit is a host's redirect list (`redirects` in `vercel.json` or
    `netlify.toml`), and only that list. A new `vercel.json` or `netlify.toml` holds
    nothing but redirects;
  - files the site serves byte for byte (robots.txt, sitemaps, static pages) change only
    as their findings call for, checked from the diff. A served page with several
    findings keeps its visible text, and its meta tags, `<link>`s, scripts, link and form
    targets, frames and `<base>` stay as they were, except the tags its findings call for
    (a description, a noindex robots tag, a canonical). The live file must still match
    what was read;
  - any other change carries the sentence that Tin couldn't build the site.
- After the PR merges, `get_run`'s `live_check` lists each finding as fixed, waiting for
  the deploy, still broken a day after the merge, or confirmed by the next audit (redirect
  chains, internal links and other changes with no single-page check).
- The run and the live check decide whether a problem is there with the audit's own tests
  (`organic_audit_site`): a link preview needs og:title and og:image, structured data
  fails only on JSON that doesn't parse or a missing required field, a canonical on
  another host names another page, and an empty description is a missing one. Sitemaps
  are found as the audit finds them: robots.txt's Sitemap lines, else `/sitemap.xml`,
  following sitemap indexes. `tests/test_technical_audit_parity.py` runs the same pages
  through both sides.

The organic traffic system starts website.change `source: audit`. Its run leaves judgment
calls out and lists them; a later website.change run can take the coding agent's answers.

### Site health is folded in

`site.health_improve` made one evidenced fix per run. Its repair rules are in the
`site-repair` skill, and its checks are in the audit: title, description, canonical, lang,
viewport, H1s, image alt, and accessible names and form labels on every page the audit
reads. Site health leaves Start here and workflow discovery; saved configurations and
schedules keep running at their pinned revision.

## Buyer-answer coverage

Buyer-answer coverage recommendations mean the site was absent from sampled answer
citations. They do not establish a code defect or missing content, and the repair plan leaves
them out. `content.plan` can use the audit and matching keyword research if content work is
warranted.

## Verification

Fixture tests cover the plan for every registered audit check, the live re-read, the patch
checks and the live check after merge, with synthetic GitHub, storage and live sites. These
are offline tests; positive live acceptance requires an authorized repository and an actual
fixable finding.
