# website.change: the one workflow that edits a site

## In plain English

In the organic traffic system, only one workflow writes to the founder's website:
`website.change`. Content drafts, page decisions (URL changes), the page tree, the blog index
and technical fixes all reach the site through it, after the founder approves them. Other
workflows find, plan and draft; they no longer open their own pull requests.

A change the founder approved in Tin publishes: Tin opens the pull request and merges it once
GitHub reports it clean. Anything else opens a pull request that the founder merges. A change
to a protected page, such as the sign-in page another app shares, always waits for the
founder, approved or not.

Phase 1 (this version, 1.0.0) makes one kind of change: it puts an approved page on the site.

## Two modes

| The change is | Tin |
| --- | --- |
| pre-approved, touches no protected path | opens the PR and merges it once GitHub reports it `clean` |
| not pre-approved | opens an unmerged PR; the founder merges |
| pre-approved, but touches a protected path | opens an unmerged PR; the founder merges |

Tin decides the mode at admission and pins it in the run's source receipt (`publish.mode` is
`direct` or `pull_request`, with the reason in the founder's words). After the pull request
opens, `hold_reason` checks again, from Postgres and the saved patch, before any merge:

- the approval as it stands now still equals the pinned one;
- no file in the patch serves a protected path, and the page's `Public URL:` is not under one;
- the page sits at the founder's chosen route, when the page type has one;
- then content.deliver's merge rules apply unchanged: `page_only` (the page as one Markdown
  file) or `chosen_route` (the page plus site code inside the route's own folder), the
  pinned branch and head, and GitHub's `clean` verdict within about three and a half minutes.

Every outcome is recorded once under `content-delivery:{run}:merge`: `merged`, or
`left_open` with the reason. Activity says `website_change_merged` or
`website_change_left_open` on both the change run and the page run.

"Publish directly" is implemented as "open the PR and merge it once clean", not as a commit
straight to the default branch, so the repository's checks still run. `clean` means every
check passed, which is stricter than "required checks": a failing optional check also leaves
the PR open.

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
- **An explicit pull-request pick narrows a page approval.** When the approval recorded a
  delivery pick of `github_pr` or "keep in Tin", the founder asked for a PR, so website.change
  opens one. A commit-to-main pick, or no pick, leaves the approval as a pre-approval. The
  program's saved delivery setting is a project file, so website.change never reads it to
  decide a merge.
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

Project membership is the boundary on both transports. A page is still approved with
`approve_workflow_run`; the new actions decide rows of the other sources.

## Protected paths

These always open a pull request, even when approved, because on a site repository a merge is
a deploy:

- `/sign-in`, `/sign-up` and `/auth-complete`, and any path under them: the auth pages a site
  shares with another app;
- the run's `protected_paths` input (up to 20 site paths), the same shape as the technical
  fix's input in #239.

A path is protected when it is, or sits under, one of these. A repository file serves one when
its folders or name spell it (`src/app/(auth)/sign-in/[[...sign-in]]/page.tsx`,
`pages/sign-up.tsx`; route groups in parentheses are skipped).

The project stores no off-limits site paths today. Onboarding's hard no's are kinds of work
(`no_paid_ads`, `no_cold_email`, …), the growth plan's `founder_limits` are free text, and
#239's `protected_paths` is a per-run input. So the defaults plus the input are the whole list
for now.

## Pages land at the founder's route

website.change adapts the page into the site's own format, the way content.deliver adapts
articles: in the site's page registry or content folder, with the copy kept byte for byte.

- An answer page or public article needs a chosen route (#244's `content/page-routes.json`).
  website.change never guesses one: without it, admission refuses with #244's question
  (`ask_the_founder`) and the `save_page_route` call to make, and MCP `get_workflow` shows the
  same question in its preparation. The route pinned at approval wins over one saved later.
- A patch that puts the page under `content/answers/` fails: that is Tin's draft folder, not a
  page on the site.
- A page whose `Public URL:` does not follow the chosen route is never merged.

The existing guards stay: the exact-copy proof, five files and 400 KB, and no dependency
files. website.change also refuses lockfiles and package-manager settings that content.deliver
does not list (`bun.lock`, `npm-shrinkwrap.json`, `.npmrc`, `pnpm-workspace.yaml`, …).

## The change-row contract

`website_change.ChangeRow` is defined once for every source:

| Field | Meaning |
| --- | --- |
| `change_id` | Stable across runs: two letters, `_`, 20 hex digits. `pg_` for a page (a digest of its run ID); the audit's `oa_` finding IDs for URL changes and repairs. |
| `source` | `content_draft`, `planned_url_change`, `technical_fix` or `blog_index`. |
| `kind` | Per source: `page`; `redirect` or `noindex`; `repair`; `index`. |
| `title` | What the founder reads. |
| `paths` | Site paths or route patterns it touches (`/blog/{slug}`). |
| `content_sha256`, `content_revision` | The exact content an approval covers. |
| `detail` | Source facts under 16 KB, such as `source_run_id` or redirect ends. |

A run pins the row with its approval (`change.approval`: decision, by, at, revision,
`content_sha256`, `via: review | decision`) in its source receipt, beside `publish`, `route`
and `protected_paths`.

## How content.deliver relates

content.deliver stays registered and unchanged. Its definition (1.3.0), inputs, prompt, receipt
keys, approval start (`approval-delivery:{run}`) and merge rule (the approval's
`github_commit` pick) are byte for byte what they were; a test pins the definition's digest.
The organic traffic system still starts it, until the recipe switches later.

website.change is built on content.deliver's machinery instead of beside it: the same page
pinning (`page_source`), approval rechecks (`guard_page`), exact-copy proof, saved patch,
recovery, merge-when-clean loop and status projection, now shared through
`content_repository_delivery.ADAPTER_WORKFLOW_IDS`. content.deliver does not delegate to
website.change, because pinned runs and saved programs depend on its exact receipts and merge
semantics, and delegating would change them. The one shared rule both now follow: a page has
at most one adaptation per repository, whichever workflow made it (`guard_attempts`), so the
two never open two pull requests for the same page.

## What phases 2 and 3 add

- **Phase 2, planned URL changes** (after #239): `planned_url_changes.read_changes` becomes a
  source that proposes `planned_url_change` rows (`finding_id`'s `oa_` IDs, kind `redirect`
  or `noindex`, paths `from` and `to`, `protected` from the same list). Approved rows go to
  website.change instead of becoming technical-fix judgment calls. Needs: an input that
  names change IDs, a redirect writer in the skill, a duplicate guard keyed on `change_id`,
  and Decisions cards for pending rows.
- **Phase 3, the technical fix and the blog index** (after #246): site-fix-v5's repairs and
  judgment calls become `technical_fix` rows per audit finding (`oa_` IDs), and
  `content.blog_index` hands its planned page over as a `blog_index` row. website.change
  takes their larger caps (20 files, 800 lines) under a new version, and its skill gains the
  repair rules. `protected_paths` then comes from one project setting instead of each run.
- **Later**: the approval path starts website.change instead of content.deliver, and the
  traffic system's recipe switches to it.

## Verification limits

Fixture tests only: synthetic GitHub, storage and Postgres. No live repository, merge, deploy
or charge was run. The copy proof shows the approved copy is stored in the patch; it does not
prove the site renders or builds it.
