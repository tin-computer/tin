# Technical repair from an organic audit

`organic.technical_fix` takes one evidenced audit finding, reads the live site again,
finds the repository files behind it, and proposes an unmerged GitHub PR. It does not
merge or deploy the repair. The current policy is `site-fix-v4`; older runs and saved
configurations retain their pinned policy (`missing-html-title-v1` through
`html-metadata-v3`).

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

## site-fix-v4

Under `site-fix-v4` the audit's own site findings (robots.txt, sitemaps, page tags) are
repair candidates too, alongside a missing title or description. One table,
`SITE_FIXES` in `src/tin_lite/technical_site_rules.py`, maps each audit check to the one
change Tin may make:

| Audit check | Change |
| --- | --- |
| `metadata.title_missing`, `metadata.description_missing` | Add the title or meta description |
| `robots.sitemap_reference_missing` | Add a `Sitemap:` line (or a new allow-all robots.txt naming it) |
| `robots.ai_search_crawlers_blocked` | Let the blocked AI search crawlers (OAI-SearchBot, ChatGPT-User, PerplexityBot, Perplexity-User, Claude-SearchBot, Claude-User, Bingbot) crawl; every other crawler's rules stay the same |
| `sitemap.non_indexable_urls`, `sitemap.ad_landing_urls` | Remove those pages' `<url>` entries |
| `sitemap.missing_search_pages` | Add `<url>` entries for pages with search impressions |
| `indexation.utility_pages_indexable`, `indexation.ad_landing_pages_indexable` | Add `<meta name="robots" content="noindex">` |
| `indexation.canonical_elsewhere` | Point the canonical at the page itself |
| `indexation.multiple_canonicals` | Keep one existing canonical tag |
| `onpage.lang_missing` | Add `lang` to `<html>` |
| `onpage.h1_missing` | Add one H1 |

Preparation reads robots.txt, the sitemaps it names (or `/sitemap.xml`) and up to three
affected pages from the audited host, pinned to public IPs. When the problem is already
gone the run ends with `already_resolved`. One repair covers up to three pages or ten
sitemap URLs and says how many more remain. The crawl's five-page limit doesn't apply.

**Static mode.** When a repository file matches what the site serves byte for byte, that
file is the source. Codex edits it, and the worker checks the diff: before and after
may differ only in the change above (only Sitemap lines added, only the listed `<url>`
entries gone, only the one tag added). No sandbox verifier runs, so the sandbox image
is unchanged. At delivery Tin reads the live file again; if its bytes changed, the PR
is not sent.

**Framework mode.** When nothing matches and the repository is a Next.js app, Tin names
the files that build the part in question: `app/robots.ts`, `app/sitemap.ts`,
`next-sitemap.config.*`, the root layout or `pages/_document`, or the page and layouts on
the affected route. Codex may change only those files, up to three files and about sixty
lines, and may create only `app/robots.ts` when the site has no robots.txt. Tin can't
build the site, so the PR body must say so in a fixed sentence, which the worker checks.
At delivery Tin confirms the live site still shows the problem.

Other stacks without a byte-for-byte match end with `unsupported_source`. An open PR
touching the same files ends with `open_pr_overlap`. Neither allocates repair compute.

**After merge.** The PR is not a deployed repair. Once GitHub reports it merged, Tin reads
the same files or pages again and records `fixed`, `waiting_for_deploy`, or, more than a
day after the merge, `still_broken`. It checks at most every ten minutes while someone
reads the run: MCP `get_run` returns it as `live_check`, and
`GET /api/projects/{project_id}/technical-fixes/runs/{run_id}/live?check=true` does the
same. It stops a fortnight after the merge. A PR closed without merging reads
`closed_unmerged`.

The traffic system's technical step takes the most urgent eligible finding under
`site-fix-v4` (critical, then high impact, then quick win), where older policies take the
first.

## Earlier policies

The rest of this page describes the metadata-only policies that pinned runs keep.

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
durable no-change explanation. Verification failures remain failures.

## Verification

Synthetic audit publications cover content-only, technical-only and mixed inventories,
legacy/current audit policies, every registered technical check, source integrity and
HTTP/MCP parity. Disposable-Postgres tests carry real source selection through metadata
verification, mocked PR delivery and immutable publication receipts, including replay.
Gateway fixtures separately cover lost GitHub responses and single-PR recovery.
These are offline tests; positive live acceptance requires an authorized repository
and an actual supported finding.
