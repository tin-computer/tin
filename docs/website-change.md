# website.change: approved changes to a founder's site

## In plain English

`website.change` is meant to become the one workflow in the organic traffic system that writes
to the founder's website: content drafts, page decisions (URL changes) and technical fixes
all reach the site through it, after the founder approves them. Today it makes three kinds of
change: approved pages, the technical fixes the latest audit found and the URL changes page
decisions made. organic.site_architecture and content.blog_index are retired for new work
(see [Retired](#retired-the-page-tree-and-the-blog-index)). content.deliver is retired for new
work too: every approval that adapts a page starts website.change
([How content.deliver relates](#how-content-deliver-relates)).

A change the founder approved publishes: Tin opens the pull request and merges it once the
repository's required checks pass. For a page, approved means approved with commit to main as
its delivery; for every other change, a recorded approval of its change row, given in
Decisions or over MCP. Anything else opens
a pull request that the founder merges. A change to a protected page, such as the sign-in page
another app shares or a page the founder listed in the project's settings, always waits for
the founder, approved or not.

- Phase 1 (1.0.0) puts an approved page on the site.
- 1.1.0 adds the decisions below and phase 2: the audit finds; website.change plans, fixes and
  publishes ([Technical changes](#phase-2-technical-changes-from-the-latest-audit)).
- 1.2.0 adds phase 3: planned URL changes and the blog index
  ([Phase 3](#phase-3-planned-url-changes-and-the-blog-index)), and change rows get cards in
  Decisions ([Decisions](#decisions-cards)).

## Decided (Emre, 10/1)

1. **Protected pages are a project setting.** The founder's extra protected pages live in
   Postgres (`project_protected_paths`), with who changed them and when. website.change
   protects the defaults, the project's pages and the run's `protected_paths` input together.
   The defaults cannot be removed. See [Protected paths](#protected-paths).
2. **Tin merges once the required checks pass.** A failing optional check no longer holds a
   pre-approved change. See [Merging](#merging-once-the-required-checks-pass).
3. **Answer pages and public articles both ask where they live.** Without a saved route,
   website.change asks the founder rather than guessing, for both page types. See
   [Pages land at the founder's route](#pages-land-at-the-founders-route).

## Two modes

For a page (`source: content_draft`):

| The change is | Tin |
| --- | --- |
| pre-approved with commit to main, touches no protected path | opens the PR and merges it once the required checks pass |
| not pre-approved, or approved without commit to main | opens an unmerged PR; the founder merges |
| pre-approved, but touches a protected path | opens an unmerged PR; the founder merges |

Tin decides the mode at admission and pins it in the run's source receipt (`publish.mode` is
`direct` or `pull_request`, with the reason in the founder's words). After the pull request
opens, `hold_reason` checks again, from Postgres and the saved patch, before any merge:

- the approval as it stands now still equals the pinned one;
- no file in the patch serves a protected path, and the page's `Public URL:` is not under one.
  The protected paths are the pinned ones plus the project's setting as it stands now, so a
  page the founder protects after the run started still holds the merge;
- the page sits at the founder's chosen route, when the page type has one;
- then content.deliver's merge rules apply: `page_only` (the page as one Markdown file) or
  `chosen_route` (the page plus site code inside the route's own folder), the pinned branch
  and head, and GitHub's verdict that the required checks passed, within about three and a
  half minutes.

Every outcome is recorded once under `content-delivery:{run}:merge`: `merged`, or
`left_open` with the reason. Activity says `website_change_merged` or
`website_change_left_open` on both the change run and the page run.

"Publish directly" is implemented as "open the PR and merge it once the required checks
pass", not as a commit straight to the default branch, so the repository's checks still run.
Technical changes follow the same two modes per change row; see
[Technical changes](#phase-2-technical-changes-from-the-latest-audit).

### Merging once the required checks pass

Tin reads GitHub's `mergeable_state` for the pull request (`github_pull_request_merge_state`)
and merges only while `mergeable` is true and the state is one of these:

| `mergeable_state` | GitHub means | website.change |
| --- | --- | --- |
| `clean` | every check passed | merges |
| `has_hooks` | every check passed; the repository has pre-receive hooks | merges |
| `unstable` | mergeable, but a check the repository does not require failed or is still running | merges when the branch requires checks; otherwise waits, then leaves the PR open |
| `blocked` | a required check has not passed, or a required review is missing | waits, then leaves the PR open |
| `unknown` | GitHub is still computing it | waits, then leaves the PR open |
| `dirty`, `behind`, `draft` | conflicts, out of date, or a draft | leaves the PR open at once |

A failing required check reports `blocked`, not `unstable`, so `clean`, `has_hooks` and
`unstable` all mean the required checks passed. GitHub's merge call also enforces branch
protection itself, so a merge it forbids is refused and the PR stays open.

**No required checks means every check.** On a branch that requires no status checks, every
check counts as optional, so `unstable` would arrive as soon as any check started. Before the
merge loop, Tin reads the base branch's required checks (`github_required_status_checks`: the
classic protection summary of `GET /repos/{repo}/branches/{branch}` and the rulesets of
`GET /repos/{repo}/rules/branches/{branch}`). With none, or none it can read, Tin waits for
`clean` (every check finished and passing) within the same three and a half minutes, then
leaves the PR open with the reason.

The merge receipt records the state that allowed the merge (`mergeable_state`) and the rule
that applied: `checks_rule` is `required_checks` (with the `required_checks` names) or
`all_checks`.

content.deliver keeps its own rule: it merges only on `clean` or `has_hooks`.

## Approvals

Pre-approved means a recorded approve action in Postgres, with who approved, when and the
exact content. It is never a status field in a project file, and no file is read as approval.
Editing a file cannot publish anything.

- **A page** (`content_draft`) is approved by its own review. `review_approval` accepts a run
  that `succeeded` with `review_decision = 'approved'`, `reviewed_by_clerk_user_id` and
  `reviewed_at` set, at the run's `canonical_commit_sha`. Both approval paths record the
  approver: `workflow_review_store.accept_approval` (articles, public articles) sets it with
  the decision, and answer pages set it through `set_review_actor` before the Temporal
  signal. An approval without an approver (older rows) is not a pre-approval, so that page
  opens a PR.
- **Only commit to main lets Tin merge a page.** website.change reads the page's delivery the
  way content.deliver does (`chosen_mode`): the pick recorded with the approval, else the
  delivery the draft pinned when it was selected. Both are receipts in Postgres, never the
  program's settings file. A `github_commit` delivery keeps the approval a pre-approval. A
  pull request, "keep in Tin" or no delivery at all opens a PR for the founder, as AGENTS.md
  requires: approval is not website publication.
- **Every other source** is approved by a row in `website_changes` (migration 053). A source
  proposes rows; the founder approves or declines each row once. The row records the
  decision, `decided_by_clerk_user_id`, `decided_at`, the `decision_request_id` and the
  `content_sha256` (and `content_revision`) it covers. An approval covers only that content:
  different content under the same ID is not approved.

### One decision per stable change ID

`website_changes` is unique on `(project_id, change_id)`. `propose` leaves a decided row
exactly as the founder left it, so next week's run that proposes the same stable ID does not
ask again; `decided()` lets a source drop those IDs before it proposes. A pending row takes
newer content, and `decide` refuses a decision on content the founder did not read (the
caller passes the `content_sha256` it showed, the way a review token binds a version). A
replay of the same request returns the recorded decision; another decision on a decided row
is refused. A trigger in Postgres refuses any update to a decided row.

### Actions

| | HTTP | MCP |
| --- | --- | --- |
| list rows | `GET /api/projects/{id}/website-changes?status=` | `list_website_changes` |
| read one | `GET /api/projects/{id}/website-changes/{change_id}` | |
| approve | `POST …/{change_id}/approve` `{request_id, content_sha256}` | `approve_website_change` |
| decline | `POST …/{change_id}/decline` `{request_id, content_sha256}` | `decline_website_change` |
| preview a source's changes (`source`: `audit`, `planned`) | `POST /api/projects/{id}/website-changes/preflight` | `preflight_website_change` |
| open judgment calls | `GET /api/projects/{id}/website-changes/questions` | (in `preflight_website_change`) |
| read protected pages | `GET /api/projects/{id}/protected-paths` | `get_protected_paths` |
| set protected pages | `PUT /api/projects/{id}/protected-paths` `{request_id, expected_revision, paths}` | `set_protected_paths` |

Project membership is the boundary on both transports. A page is still approved with
`approve_workflow_run`; the new actions decide rows of the other sources.

## Protected paths

These always open a pull request, even when approved, because on a site repository a merge is
a deploy:

- `/sign-in`, `/sign-up` and `/auth-complete`, and any path under them: the auth pages a site
  shares with another app. They are always protected and cannot be removed;
- the project's protected pages, a setting the founder keeps (up to 20 site paths);
- the run's `protected_paths` input (up to 20 site paths), the same shape as the technical
  fix's input in #239. It adds pages for one run and stays for compatibility.

A path is protected when it is, or sits under, one of these. A repository file serves one when
its folders or name spell it (`src/app/(auth)/sign-in/[[...sign-in]]/page.tsx`,
`pages/sign-up.tsx`; route groups in parentheses are skipped).

### The project setting

The setting lives in `project_protected_paths` (migration 055), not in a project file. Each
save appends one revision: the paths, `changed_by_clerk_user_id`, `changed_at` and the
`request_id`. The newest revision is the setting; the older ones are its history, which
`get_protected_paths` returns newest first. A trigger refuses any update to a saved revision.

- **Validation.** Site paths only. A full URL keeps its path; a query, a fragment, a trailing
  slash and repeated slashes are dropped. Repeats and pages the defaults already cover are
  dropped too. `/` (the whole site), route patterns such as `/blog/{slug}`, `..` and anything
  else that is not a site path are refused, as is a list of more than 20 pages.
- **Saving** names the revision the caller read (0 before the first save); a stale revision is
  refused, so two people never overwrite each other unseen. A retried request ID returns what
  it saved. `[]` keeps only the defaults.
- **Admission** pins the effective list and the setting's revision
  (`protected_paths_revision`) in the run's source receipt. Before any merge, Tin reads the
  setting again and adds it to the pinned list.

## Pages land at the founder's route

website.change adapts the page into the site's own format, the way content.deliver adapts
articles: in the site's page registry or content folder, with the copy kept byte for byte.

- An answer page or public article needs a chosen route (#244's `content/page-routes.json`).
  Emre confirmed on 10/1 that this covers both page types. A `content.generate` answer page is
  an answer page here: its approval starts website.change itself, at the route its selection
  pinned (see [one content.generate](one-content-generate.md)).
  website.change never guesses one: without it, admission refuses with #244's question
  (`ask_the_founder`) and the `save_page_route` call to make, and MCP `get_workflow` shows the
  same question in its preparation. The route pinned at approval wins over one saved later.
- A patch that puts the page under `content/answers/` fails: that is Tin's draft folder, not a
  page on the site.
- A page whose `Public URL:` does not follow the chosen route is never merged.

The existing guards stay: the exact-copy proof, five files and 400 KB, and no dependency
files. Both workflows refuse the same list (`content_repository_delivery.DEPENDENCY_FILES`:
package manifests, lockfiles and package-manager settings such as `bun.lock` and `.npmrc`),
and the route-folder check treats them as site-wide wherever they sit.

## The change-row contract

`website_change.ChangeRow` is defined once for every source:

| Field | Meaning |
| --- | --- |
| `change_id` | Stable across runs: two letters, `_`, 20 hex digits. `pg_` for a page (a digest of its run ID); the audit's `oa_` finding IDs for repairs; `planned_url_changes.finding_id`'s `oa_` IDs for planned URL changes; `bi_` for a blog index plan. |
| `source` | `content_draft`, `audit`, `planned` or `blog_index` (migration 056 renamed the unused `technical_fix` placeholder to `audit`, migration 057 `planned_url_change` to `planned`). |
| `kind` | Per source: `page`; the site-fix-v5 repair (`html_noindex`, `sitemap_add_urls`, `merge_redirect`, …); `redirect` or `noindex`; `index`. |
| `title` | What the founder reads. |
| `paths` | Site paths or route patterns it touches (`/blog/{slug}`). |
| `content_sha256`, `content_revision` | The exact content an approval covers. |
| `detail` | Source facts under 16 KB, such as `source_run_id` or redirect ends. |

A page run pins its row with its approval (`change.approval`: decision, by, at, revision,
`content_sha256`, `via: review | decision`) in its source receipt, beside `publish`, `route`
and `protected_paths`. A technical run pins a list of rows (`changes`), each with its approval
and the protected page it touches, if any.

## Phase 2: technical changes from the latest audit

The audit finds; website.change plans, fixes and publishes. A run with `source: audit` repairs
what the project's latest successful organic audit found, under site-fix-v5's rules. The code
is the technical fix's, shared rather than copied: `TechnicalFixSources.inspect` reads the
audit, `technical_repair_plan.build_plan` sorts its findings, `TechnicalFixExecution
.prepare_selection` re-reads the live site and names the files the diff can prove,
`technical_batch` checks the patch, and `technical_fix_live` checks the live site after the
merge. `website_change_audit.py` holds what is new.

### One change row per fixable finding

`preflight_website_change` (or `POST …/website-changes/preflight`) and a start both read the
latest audit and record one `website_changes` row per finding the plan repairs:

- `change_id` is the audit's stable finding ID, `source` is `audit`, `kind` is the repair,
  `paths` are the pages, both ends of a redirect, `/robots.txt` or `/sitemap.xml`.
- `content_sha256` covers the repair itself (`intent`): the finding, its check, the kind of
  change and the judgment-call answer. It leaves out the pages, because a finding ID is
  stable per check and site while the pages an audit lists move week to week; each run
  re-reads them and checks them against the protected paths.
- New IDs are added; a pending row takes the newer plan; a row the founder approved or
  declined stays as it is. A declined finding is left out of every later plan (listed under
  `left_out.declined`), even when a newer audit finds it on more pages.
- The repository is bound before anything is recorded, and only after the member confirms it
  serves the audited site (`repository_serves_site`).

### Judgment calls

They stay MCP → coding agent → founder, as in site-fix-v5. The preview returns
`decisions_needed` and site-fix-v5's `ask`: the coding agent answers from the codebase, asks
the founder only when unsure, and passes the answers as `decisions` (`finding_id=choice`). A
finding whose call is unanswered gets no row. An answer recorded in an approved row is reused
by later runs; an explicit answer in a run wins over it, and a different answer is not what the
founder approved, so it does not publish.

### Which rows a run makes, and how

One run makes one pull request:

| The rows | Tin |
| --- | --- |
| approved, touching no protected page | the run takes these first; Tin merges the PR once the required checks pass |
| waiting for approval | the next run takes them into a PR the founder merges |
| approved, but touching a protected page | a PR the founder merges |
| already in an open or merged website.change PR | skipped, and the preview names the PR |
| declined | never made, never proposed again |

`next_run` in the preview says which rows the next start takes and why. A start with nothing
left to make is refused with the reason: every row already sits in an open PR (named, merge or
close it first), the rest wait for judgment calls, or nothing is left. A row whose earlier PR
was closed without merging can go in a new one. Only one technical run per project works at a
time.

Before any merge, `website_change_audit.hold_reason` checks again: the pinned mode, every
row's approval as it stands now, and the protected paths (pinned, plus the project's setting
as it stands now) against the rows' paths, the planned repairs and every changed file. Then
the merge follows [the required-checks rule](#merging-once-the-required-checks-pass), with
`merge_rule: approved_changes` in the receipt.

### Caps and checks

site-fix-v5's bounds: at most 30 findings per run (the rest are left for a later run), 20
files and 800 changed lines, no dependencies, CI, deploy settings or secrets, files the site
serves byte for byte checked from the diff, and a re-read of those files before the PR opens.
website.change 1.1.0 pins these rules in its definition (`site_repair_policy: site-fix-v5`) and
raises its file cap to 20; a page keeps content.deliver's five files and 400 KB
(`website_change.check_patch`). The `site-repair` skill is the technical fix's
`audit-batch-repair` skill, adapted to website.change's context and to say who merges.

### The live check after merge

Right after Tin merges, the run records the merge and reads the live site once
(`technical_fix_live.LiveRecheck`): per finding, fixed, still showing, or left to the next
audit. It reads again, at most every ten minutes, while someone reads the run (MCP `get_run`),
and stops a fortnight after the merge.

### organic.technical_fix

It stays registered for pinned site-fix-v5 runs and saved schedules, byte for byte as on main
except its catalog flag: 0.6.1 sets `public_discovery: false`, so new setups don't see it, and
it left the growth plan's program lists and onboarding copy. The public ChatGPT plugin no
longer starts, stops or lists technical fixes. New starts are refused (retries, saved
schedules and traffic system runs on v5 or earlier keep their pinned technical fix), and its
preview tools are gone from both MCP servers: use `preflight_website_change`. The traffic
system's v6 recipe starts website.change instead.

## Retired: the page tree and the blog index

`organic.site_architecture` and `content.blog_index` refuse new starts, and website.change
refuses a new run with `source: blog_index` (`run_service.RETIRED`); the preflight tools offer
only `audit` and `planned`. Retries, saved schedules and organic system runs that pinned them
keep running. Both are hidden from discovery and new setups.

- The page tree's triggers repeated the audit's orphan, depth and competing-page checks, and
  no workflow read its page tree, URL rules or navigation. Its redirects needed the founder to
  type every move. Page decisions plan the redirects and noindex changes `planned` makes.
- The blog index planner ran a Codex session to plan an index most sites already have;
  website.change adds each new article to the site's own index, and the audit reports posts
  nothing links to.

The sections below describe what pinned runs still do.

## Phase 3: planned URL changes and the blog index

### Planned URL changes (`source: planned`)

`planned_url_changes.py` (moved here from #239, which drops its copy) reads two planner files
from the project at its current revision. Either may not exist yet; a project that never ran
the planner contributes nothing, and the preview says so.

| File | Block | Fresh for | Changes |
| --- | --- | --- | --- |
| `content/efficacy.md` (page decisions, `organic.content_efficacy`) | `## Decisions block` with a fenced `content.efficacy/1` JSON object; `url_changes[]` of `{from, to, kind: 301 or noindex, reason, confirmed}` | 14 days | redirect, noindex |
| `reports/organic/site-architecture/SITE_ARCHITECTURE.md` (`organic.site_architecture`) | fenced `site_architecture.redirects/1` JSON between `<!-- redirects.json:start -->` and `<!-- redirects.json:end -->`; `redirects[]` of `{old, new, status: 301 or 308, reason}` | 60 days | redirect |

Each change gets a stable `oa_` ID (`finding_id`: source, kind, from, to) and Tin's suggestion:
`ask` when either end is a protected page (defaults, the project setting and the run's input),
else `apply`. A plan redirect wins over the same weekly proposal; a 302 is not a permanent
move and is left out.

- Each change is one `planned` row: kind `redirect` or `noindex`, both ends as paths. An
  approval covers the change itself, both ends included.
- It is planned as a site-fix-v5 repair (`website_change_planned.entry_for`): a redirect as a
  merge redirect in the site's own redirect config (`vercel.json` or `netlify.toml` redirects,
  a `_redirects` file, or the framework's redirect config), a noindex in the page's own
  metadata. From there the audit source's machinery runs unchanged: caps, patch checks, the
  mode rules, the merge rule and the live check after merge (a redirect is fixed when the old
  URL redirects to the new one).
- Deleting a page stays with the founder: the reader never plans one, and the preview lists
  the plan's deletions under `left_out.manual`.
- Decided rows stay decided; rows in an open or merged website.change PR are skipped; pending
  rows the plan no longer proposes are retired.

### The blog index (`source: blog_index`)

`content.blog_index` (#239) is a planner that opens no pull request. Its run writes
`reports/blog-index/{run_id}/PLAN.md` with one fenced JSON block between
`<!-- blog-index-patch.json:start -->` and `<!-- blog-index-patch.json:end -->`:

```json
{"schema": "blog-index-patch/1", "repository": "owner/repo", "base_ref": "<branch>",
 "base_sha": "<sha the plan read>", "route": "/blog", "summary": "…",
 "files": [{"path": "…", "action": "create|update", "content": "<full file text>"}],
 "caps": {"max_files": 5}}
```

- website.change reads the newest succeeded content.blog_index run's PLAN.md server-side
  (`website_change_blog_index`). Without one, the source finds no runs and says so.
- It records one row: change ID `bi_` plus the first 20 hex digits of the SHA-256 of the
  canonical JSON of `files` (the 20 fit the change-ID shape), kind `index`, the route as its
  path, and the full SHA-256 as the content an approval covers.
- Nothing needs judgment, so no Codex session runs: the run opens the pull request with
  exactly the plan's files and, under the same mode rules, merges it once the required checks
  pass when the row was approved and touches no protected page (the route, or a file that
  serves one). The files are re-read from the pinned plan and checked against the approved
  SHA-256 first.
- When `base_sha` is not the branch head, Tin asks GitHub what changed since (compare). If any
  of the plan's files changed upstream, or Tin can't tell, the row stays pending and the start
  says which file moved. A plan for another branch is refused the same way.
- Never more than five files, never `package.json`, lockfiles, package-manager settings, CI,
  deploy settings or secrets, and at most 400 KB.

### Decisions cards

Pending rows and open judgment calls wait in Decisions beside run reviews, in the same list
and detail card:

- A change card shows its source (Audit fix, Planned URL change, Blog index) and kind, what it
  does in one line, its pages, the blog index's files, and "Protected: … opens a pull request
  for you to merge" when a protected page touches it. The list endpoint reports that page
  (`protected`).
- Its button row is Decline and Approve, nothing else. Either posts to the existing
  approve/decline route with a new request ID and the `content_sha256` the card showed, so
  the decision records who, when and the exact content.
- A judgment call from the latest preview (`GET …/website-changes/questions`) shows its
  question and options, with Tin's suggestion marked. It has no buttons: the coding agent
  answers it with the next run and asks the founder when unsure.

## How content.deliver relates

content.deliver is retired for new work. It stays registered, hidden from discovery
(`public_discovery: false`), for its existing runs, retries, saved schedules and traffic
system runs pinned to v5 or earlier; its inputs, prompt, receipt keys and merge rule (the
approval's `github_commit` pick, merging only on `clean` or `has_hooks`) are unchanged for
them, and a test pins the definition's digest.

- `start_workflow_run` refuses a new content.deliver with a pointer to website.change
  (`source: content_draft`).
- The approval of an answer page or public article (`approval-delivery:{run}`) starts
  website.change, as a content.generate answer page's already did. A start content.deliver
  admitted under that key before the retirement is returned, never replaced by a second run.
  Unlike content.deliver, website.change never guesses a route: an approved page whose type
  has none records a failed delivery that asks the founder, and `retry_content_delivery`
  starts it once `save_page_route` has saved one.
- The content program card's **Prepare PR** and **Retry** start website.change, and the card
  shows the latest adaptation by either workflow (`delivery_history`).
- `get_workflow` no longer prepares content.deliver; website.change's preparation covers
  pages.

website.change is built on content.deliver's machinery instead of beside it: the same page
pinning (`page_source`), approval rechecks (`guard_page`), exact-copy proof, saved patch,
recovery, merge loop and status projection, now shared through
`content_repository_delivery.ADAPTER_WORKFLOW_IDS`. content.deliver does not delegate to
website.change, because pinned runs and saved programs depend on its exact receipts and merge
semantics, and delegating would change them. The one shared rule both now follow: a page has
at most one adaptation per repository, whichever workflow made it (`guard_attempts`), so the
two never open two pull requests for the same page.

## What comes next

- **The traffic system's writer steps (done, `organic-traffic-v6`)**: the technical step starts
  website.change `source: audit` and the delivery step starts it for the approved draft,
  instead of organic.technical_fix and content.deliver. v5 pins are unchanged.
- **The recipe rewrite**, after #239 and #267 merge: the set-up step (style, brand, code map
  and the founder's approval), a prerequisite-chained weekly loop that runs website.change
  for each source (`audit`, `planned`, `blog_index` and the approved pages) with the
  system's `expected_repository` and `repository_serves_site`, the traffic snapshot, page
  decisions, the page tree, and typed content.generate items. Judgment calls go through
  `preflight_website_change`; approvals come from Decisions or MCP.
- **Done**: the approval path starts website.change instead of content.deliver.

## Verification limits

Fixture tests only: synthetic GitHub, storage, live site and Postgres. No live repository,
merge, deploy or charge was run. The copy proof shows the approved copy is stored in the patch;
it does not prove the site renders or builds it. A technical patch that touches files Tin
can't prove from the diff carries Tin's sentence that it couldn't build the site; the
repository's required checks and the live check after merge are what verify it.
