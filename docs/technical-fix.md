# Technical repair from an organic audit

> **New technical fixes go through website.change** (Emre, 10/1: the audit finds; website.change
> plans, fixes and publishes). A website.change run with `source: audit` applies site-fix-v5's
> rules below to the latest audit, records each fixable finding as a change row the founder
> approves or declines once, and publishes approved rows. See
> [website.change, phase 2](website-change.md#phase-2-technical-changes-from-the-latest-audit).
> `organic.technical_fix` 0.6.1 stays registered, hidden from discovery (`public_discovery:
> false`), for pinned runs, saved schedules and the traffic system's current recipe; its
> behaviour below is unchanged.

`organic.technical_fix` reads an organic audit's findings, checks them on the live site
again, and proposes an unmerged GitHub PR that fixes every one it can. It does not merge
or deploy the repair. The current policy is `site-fix-v5` (catalog 0.6.1, the same contract as
0.6.0); older runs and saved configurations retain their pinned policy (`missing-html-title-v1`
through `html-metadata-v3`, which repair one finding per run).

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
  about and contact pages, and content gaps. The technical fix never writes marketing copy.
- **Manual**, outside the repository, with where the step lives: a CDN or bot protection
  refusing crawlers, Core Web Vitals, JavaScript-only rendering, oversized HTML, analytics,
  server errors, an unreachable robots.txt, and asking Google to index pages.

### Judgment calls through MCP

`preflight_technical_fix(project_id, audit_run_id, audit_revision, expected_repository,
repository_serves_site, decisions=[])` returns the plan without starting anything:
`plan.repairs`, `plan.left_out` (with a reason for each finding) and `decisions_needed`.
Each decision has an `id` (the finding), the `question`, `options` (`value` and `label`),
Tin's `suggestion` and `why`. The `ask` field tells the coding agent to answer from the
codebase and what it knows about the product, and to ask the founder only the ones it is
unsure of, in one message. Answers go back as `decisions: ["finding_id=choice", ...]`, to
the preview to check them and to `start_workflow` to run them. A finding whose decision is
unanswered, or answered "keep", stays out of the PR and is listed. Starting a run with
nothing ready to fix is refused with the count of decisions still waiting.

### One run, one pull request

- The run re-reads the live site: robots.txt, the sitemaps it names, and up to 40 pages.
  It drops findings that are already fixed, then binds the repository and reads open-PR
  evidence.
- The repository snapshot leaves out files over 10 MB. Images, video, fonts and built
  bundles don't count; any other file Tin couldn't read (a source or data file over 10 MB, a
  link, a submodule) ends the run **failed**, and its reason and report name each file. The
  same holds when Tin can't read the snapshot at all or the plan is too large to hand to
  Codex. Only "already resolved", "nothing to fix" and open-PR outcomes end succeeded with
  no change. A patch can't write over a large file Tin left out.
- Preflight reads the repository's file list once and returns `repository_warnings` naming
  the files a run would stop on, so the coding agent can say so before starting.
- Codex gets the plan (up to 30 findings, most urgent first) through the
  `audit-batch-repair` skill. It traces each finding to its source in the site's own
  framework, fixes it where it applies to every affected page, runs the repository's
  checks plus `git diff --check`, and writes a PR body with the fixes by group, the
  decisions followed, anything left for a later run, manual steps, and checks run and
  skipped. The skill carries site health's repair rules (see below).
- Before the PR opens, the worker checks the patch:
  - at most 20 files and 800 changed lines, files at most 200 KB and new files at most
    20 KB, text only;
  - no dependencies, lockfiles, package manager or workspace settings, CI, deploy or
    build settings, secrets (`.env*`, `.dev.vars`), `.github/` or `.gitmodules`; the one
    deploy setting it may edit is a host's redirect list (`redirects` in `vercel.json` or
    `netlify.toml`), and only that list. A new `vercel.json` or `netlify.toml` holds
    nothing but redirects;
  - no file an open PR already changes;
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

The organic traffic system passes the whole audit under v5. Its run leaves judgment calls
out and lists them; a later technical-fix run can take the coding agent's answers.

### Site health is folded in

`site.health_improve` made one evidenced fix per run. Its repair rules are in the
`audit-batch-repair` skill, and its checks are in the audit: title, description,
canonical, lang, viewport, H1s, image alt, and now accessible names and form labels on
every page the audit reads. Site health leaves Start here and workflow discovery; saved
configurations and schedules keep running at their pinned revision.

## Select a finding

Use `get_technical_fix_source(project_id, audit_run_id)` or the corresponding
`GET /api/projects/{project_id}/technical-fixes/sources/{audit_run_id}` endpoint.
The reader verifies the exact published revision and document digests, then recomputes
technical findings from the saved crawl. Its response includes:

- `findings`: technical findings, each with `source_eligible`, `ineligible_reason`
  and the full affected URL list from the bounded crawl.
- `excluded_findings`: content recommendation identifiers with `content_finding`,
  an explanation and `next_action: content.plan`. These are not repair candidates.
- `repair_availability`: whether any technical finding is source-eligible. An empty
  technical inventory reports `no_technical_findings`; an inventory with only
  unsupported checks, incomplete crawls or over-limit findings reports
  `no_eligible_findings`. Inspect `crawl_status` and `check_coverage`: no findings
  does not establish that unobserved checks passed or the whole site is healthy.

`execution_available` describes whether the workflow is implemented, not whether
this particular audit contains a repair candidate. Source eligibility still requires
live verification, source matching and delivery checks before a PR can be created.

Pass the exact audit run, revision and eligible finding ID to
`preflight_technical_fix`, with the selected repository and confirmation that it
serves the site. Preflight is read-only. A content finding returns `content_finding`
(HTTP 409), an unsupported technical check returns `check_not_supported` (409), and
an ID absent from the verified audit returns `finding_not_found` (404). These cases
stop before repository binding or repair compute. Normal workflow starts use the
same preflight, and execution independently pins and revalidates its selection.

Buyer-answer coverage recommendations mean the site was absent from sampled answer
citations. They do not establish a code defect or missing content. Inspect existing
answers first; `content.plan` can use the audit and matching keyword research if
content work is warranted. The repair preview does not start that workflow.

## Earlier policies

The metadata-only policies below repair one finding per run; pinned runs keep them.

## Current repair coverage

| Audit check | Repair support |
| --- | --- |
| `metadata.title_missing` | Supported in current and legacy repair policies. |
| `metadata.description_missing` | Supported in `html-metadata-v2` and `html-metadata-v3`. |
| `metadata.title_duplicate`, `metadata.description_duplicate` | Visible for inspection; no automated repair profile. |
| `canonical.broken`, `canonical.redirect` | Visible for inspection; no automated repair profile. |
| `links.broken`, `discovery.possible_orphan` | Visible for inspection; no automated repair profile. |
| `http.redirect`, `http.redirect_chain`, `http.client_error`, `http.server_error` | Visible for inspection; no automated repair profile. |

Supported findings require a completed technical crawl and at most five affected
URLs. Current source profiles are exact static HTML and bounded Hatchling Python-wheel
HTML templates with known literal substitutions. The verifier permits only the selected
title or description change; application logic, dependencies and build configuration
cannot change. General framework builds need separate repair and verification support.

The current policy can repair matched pages while explicitly listing unsupported
pages as untouched. It never claims the whole finding is repaired when coverage is
partial. Already-resolved pages, unsupported sources and overlapping PRs produce a
durable no-change explanation. A snapshot missing a file the build profile could need ends
the run failed with the file named. Verification failures remain failures.

## Verification

Synthetic audit publications cover content-only, technical-only and mixed inventories,
legacy/current audit policies, every registered technical check, source integrity and
HTTP/MCP parity. Disposable-Postgres tests carry real source selection through metadata
verification, mocked PR delivery and immutable publication receipts, including replay.
Gateway fixtures separately cover lost GitHub responses and single-PR recovery.
These are offline tests; positive live acceptance requires an authorized repository
and an actual supported finding.
