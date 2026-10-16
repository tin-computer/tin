# Repository-aware page delivery

Approved pages reach the founder's site through `website.change` (`source: content_draft`).
This page describes the delivery machinery it uses (`content_repository_delivery.py`): how a
page is adapted to the site, the repository snapshot, retries, page URLs and page routes.
Approvals, protected pages and the merge rule are in [website.change](website-change.md).

## In plain English

The page remains a Markdown document in Tin. Once the founder approves it, **Prepare
PR** adapts that same page to the connected website's existing structure. The user
chooses the page and repository, not Markdown versus a component file. WordPress and other
CMS publication need their own connectors later.

This is an explicit continuation on the content card. Repository adaptation uses compute,
so the normal spending confirmation applies. Approval of an older draft is still only
approval of its content; it does not silently acquire a publishing action.

Approved **answer pages** go further: with a repository selected and Codex API execution on,
their approval card has one **Publish** button. Publish adapts the page into the site's own format and then follows the founder's saved delivery setting. The
card's footer says which, with the cost preview: "Tin adapts it to your site and commits it
to main · about $5" or "Tin adapts it to your site and opens a pull request · about $5".
Without adaptation (no repository, or no Codex API execution) the card keeps Publish now
and Open a pull request, which use the exact Markdown publisher below.

## Implementation

- `website.change` is an ordinary immutable catalog definition on the existing
  `codex.procedure` executor, not another engine or saved program. It is callable through
  HTTP/MCP and from the existing My System content program card.
- Admission reads only an approved, successfully published `content.generate` artifact
  in the same project. It verifies the canonical receipt and source frontmatter, removes
  Tin metadata/verification notes, and pins the exact body, digest, source revision,
  repository ID, installation, connection, default branch and Git head.
- Run, source receipt and ordinary billing reservation commit in one transaction. A
  concurrent second start for the same article/repository cannot buy another adaptation.
  The same request ID returns the original run. Versions remain internal metadata.
- The normal project-scoped Codex execution queue, isolated/API authentication selection,
  usage accounting, lease fencing and sandbox cleanup remain unchanged. The article is
  passed as trusted source context; neither source text nor credentials enter Temporal.
- The agent inspects repository instructions, existing articles, layouts, routing and open
  PR evidence. It may change up to five text files / 400 KB. It cannot add dependencies.
  Markdown-native sites keep Markdown. Component sites use an existing Markdown renderer
  and retain the article as a complete JSON string or JSON data. Unsupported rendering
  arrangements are reported as a prerequisite, not silently approximated as rewritten JSX.
- The switchboard checks exact source preservation before accepting a patch and again
  before delivery. The immutable checkpoint survives sandbox teardown. The existing
  GitHub gateway checks the destination, later commits and retry identity, and opens an
  unmerged PR. Other open PRs never block it (10/16); GitHub shows a conflict if the same
  lines clash, and the founder reconciles it. For article delivery only the page file counts
  as a blocking path: a later commit on the default branch that edits a shared file such as
  the sitemap or an index does not block the article. A later commit that changes the page
  file itself still refuses the PR, and changed page paths are not silently rebased. Other
  procedures keep every file as a blocking path.
- Every repository procedure (page delivery, audit fixes, site health, code maps)
  reads a snapshot of up to 100,000 eligible files / 250 MB, each file at most 10 MB. The
  gateway downloads the pinned commit as one tarball and checks every file against its blob
  hash in the pinned tree; paths the tarball omits or rewrites (`export-ignore`,
  `export-subst`) are read individually (at most 500). Oversized repositories fail before
  the download, with their eligible file count and byte total alongside the bound. The
  tarball (at most 1 GB) and the verified files wait in temporary files on the switchboard;
  only the finished compressed snapshot is held in memory before it goes to the sandbox. The
  byte bounds are set by the switchboard VM's free memory and disk, which every run in flight
  shares, not by the sandbox.
- A file over 10 MB stays out of the snapshot. Images, video, audio, fonts, archives, PDFs,
  WebAssembly, source maps, minified files and scripts or stylesheets under a public or static
  folder (`public/`, `static/`, `dist/`, `build/`, `out/` and the like) can't hold what a fix
  edits, so the snapshot still counts as complete without them. Any other file left out (a
  source or data file over 10 MB, a symbolic link, a submodule) makes it incomplete. The
  read's receipt lists both, with each file's size and reason.
- An incomplete snapshot doesn't stop audit fixes or page refreshes: what they read and
  check is always small served text or source, never a file that was left out. The pull
  request lands on GitHub's own tree, so left-out files stay as they are. A patch that writes
  to a left-out path, or under one (a submodule, a linked folder), is refused and the file
  named; the fix report lists what was left out, and a refresh that can't find the page's
  text names it too.
- A canonical receipt at `content/deliveries/{run_id}.md` links the PR. The original
  Markdown and the roadmap are unchanged. Status, article choices, titles and PR links
  are Postgres projections, shared by HTTP/MCP and the existing content card.

## Verification limits — do not overstate these

Exact Markdown/string preservation proves the original copy was retained in the patch.
It does **not** prove that every byte is rendered, that the wrapper has no additional copy,
that Markdown extensions display identically, or that a site build succeeded. The mandatory
runner check is `git diff --check`; it is not a compiler or build check.

The procedure must use the site's existing renderer, run available relevant checks, and
separately report checks it could not run. The PR needs a site-preview/editorial review
before merging. There is no universal dependency installer or framework-specific build
adapter in this slice. Verify live adaptation, build and preview against an explicitly
approved article. A stronger renderer/build contract should be shared across repository
procedures, not a hidden special case for one website.

## Failure and retry

Draft approval/success never changes because delivery fails. A member can invoke
`retry_content_delivery` for a failed adaptation that has a completed immutable patch.
The existing internal delivery workflow revalidates and sends that saved patch using
the original GitHub execution key. No model runs and no new article is generated.
The confirmed result is separately receipted and shown on the content card/Activity;
the failed adaptation's historical status is not rewritten as success.

A failed attempt whose PR never opened may be replaced by a **new metered adaptation**:
Prepare PR, “Retry” on that draft’s row of the content card or `retry_run_id` through MCP (internal attempt
context, not a revision selector). "Never opened" means GitHub refused the request before
writing anything (for example because another open PR changed a shared file, as delivery
refused before September 29) or never received it. A saved patch alone no longer blocks this;
Retry delivery stays the free way to send it. A PR that opened, or a PR request whose outcome
is unknown, still has to be reconciled first, and a delivery run that is still working must
finish. A refusal is never replayed: each retry and each Prepare PR checks again.

The older automatic `.md` delivery contract remains intact for pinned runs/settings.
An article already enrolled in that contract must use its existing delivery action;
the new continuation cannot start a second competing PR for it.

## Acceptance

Automated coverage includes source approval/project boundaries, canonical proof, exact
Markdown and component-string copies, changed-copy/dependency rejection, atomic and
concurrent starts, HTTP/MCP parity, ordinary API billing admission/idempotence, normal
PR publication, frozen-checkpoint recovery, and reverse projection into the content card.
Browser coverage uses the existing controls in both themes, title-based article choices,
hidden run IDs, responsive widths, scoped saves and preservation of unsaved plan/settings.
Existing Temporal replay/queue tests remain part of the suite; no workflow command
sequence or registered execution engine was changed.

## Page URLs: where a draft will appear, and whether it is live

Article and answer-page runs carry `page_url` in the run API
(`GET /api/projects/{id}/runs`, `GET /api/workflows/runs/{id}`) and in MCP `get_run`.
Before approval, the decision card shows one line, `Proposed URL <address>` or
`Will be published at <address>`, and nothing when Tin found no page on the site that
shows the file's folder. After approval, the document page shows one status line above the
title: the open pull request, the deploy, `Live at <address>` (the only link), or
"Committed, but not a page on <site> yet". The `note` field stays in the API for agents.
Resolving or checking a page URL never fails the response that carries it: any lookup or
provider error means no URL is known, and only the error's type is logged.

| Field | Meaning |
| --- | --- |
| `url`, `label` | The address, labelled `Proposed URL`, `Will be published at` or `Live at`. |
| `source` | `delivery_route` (the adaptation PR's `Public URL:` line), `plan_destination` (the plan item; final only for an update to an existing page), `folder_route` (Tin confirmed that the site shows other files from the target folder at this route), or `title_slug` (a proposed slug under the site; never final). |
| `state` | `proposed`, `planned`, `merged` (merged or committed, page not found yet) or `live`. |
| `note` | What approving does, or what happened: for example "Approving commits content/answers/x.md to owner/site as a Markdown file only". |
| `repository`, `file_path`, `pull_request` | The delivery target when Tin writes to GitHub. |
| `checked_at`, `checkable` | When Tin last looked, and whether another look can change anything. |
| `route_missing` | Tin looked and found no page on the site that shows the file's folder. |
| `deploy_overdue` | Merged or committed longer ago than a deploy takes, and still not found. |
| `published_outside_tin` | No GitHub delivery: the proposed URL is only a suggestion. |

Card polling reads one Postgres projection per run (`page-url:<run_id>` in
`effect_receipts`). Provider reads happen only in an explicit check
(`GET /api/workflows/runs/{id}/page-url?check=true`, and MCP `get_run`), at most every ten
minutes per run and within a 20-second bound:

- an open PR is read once from GitHub to learn whether it merged;
- after a merge or a direct commit, one vetted public GET (public addresses only, same-site
  redirects) must return 200 at the same path with the article's title before Tin says
  `Live at`. Until then the page is not called published. After 30 minutes the note says
  "Committed to owner/site main; not a page on example.com yet" and names the file;
- for the Markdown publisher, Tin lists the target folder and tries up to two existing files
  under three common routes (`/<folder>/<slug>`, `/<slug>`, `/blog/<slug>`). A route counts
  only when a made-up slug under it does not also answer 200. Without one, the card says
  before approval that approving commits a Markdown file only.

For an adapted page, the page's delivery view is its
adaptation's: the PR, its `Public URL:` line (so the page URL's source is
`delivery_route`), and a merge when Tin made one. Before approval, `note` says that Tin
adapts the page and then opens a pull request or commits it to main; the card line stays
the one `Proposed URL` line, since no file path exists until the adaptation picks one.

## Adapting approved pages

- **Sources.** website.change accepts an approved `content.generate` article or answer page.
  Admission checks the same project, a succeeded and approved run and its publication receipt,
  and pins the bytes by SHA-256. The page's own frontmatter is split off: `article` is the
  copy the patch must keep byte-for-byte, and `page_metadata` carries `meta_title` and
  `meta_description` for the site's own title and description fields. The PR limit is
  400 KB.
- **Structured sites.** When no route renders the target folder but the site already
  depends on a Markdown renderer (react-markdown, marked, markdown-it, remark, an MDX
  loader), the adaptation may add one minimal route that renders `.md` files from one
  folder with that renderer and the site's layout, once, within the five-file limit, and
  then place the page there as a `.md` file. Without a renderer it stops with a
  prerequisite message instead of committing a page the site will not show.
- **Trigger.** A repository pick at approval (HTTP approve, the decision apply, MCP
  `approve_workflow_run`) records `adapter: "repository"` in the choice receipt when Codex
  API execution is on. The existing `deliver_content_draft` activity then starts one
  `website.change` run through the ordinary run service, as the approver, under the start
  key `approval-delivery:{run}`. Its start receipt uses the Markdown publisher's key under
  another operation, so one approval can never run both.
- **Delivery setting.** The adaptation always opens a PR. When the founder's setting commits
  to main (`github_commit`), Tin merges it under
  [website.change's rules](website-change.md#two-modes); otherwise the founder merges.
- **Billing.** The adaptation is an ordinary Codex procedure session charged on actual
  usage. The approval response and the publish preview
  (`GET /api/projects/{id}/content-drafts/{run}/publish-preview`, MCP `get_run`
  `delivery_preview`) carry its configured cost preview. When admission is refused (too
  few credits, a spending limit, a changed connection), the page's delivery is recorded as
  failed with the reason and a pointer to retrying delivery or Prepare PR. Retrying the
  page's delivery tries the same start again; once the adaptation exists, it retries that
  run's saved patch instead.

## Where adapted pages live

Every page has one address, chosen before it is written. Nobody picks a folder at delivery.

- **In the plan.** content.plan gives every new page a `destination`: the full URL it will
  have, in the section where the site keeps that kind of page (`/alternatives/zapier`). The
  planning agent proposes it from the site's own pages; Tin drops one that is already a page on
  the site or another item's address and proposes one beside the plan's other pages of the same
  format instead (`content_plan.page_address`). The founder approves it with the plan and can
  change it with `edit_content_plan`. An update's destination is the page it changes.
- **On the draft.** content.generate pins the address in its selection (`page_url`); a new page
  from an older plan, which has none, gets one the same way. The draft's review shows it as
  "Will be published at", so approving the draft approves where it goes.
- **At delivery.** website.change pins the address as the page's change row path and its
  `route`, and the adaptation publishes the page at exactly that path, adding the smallest route
  the site needs to serve it. A draft from before addresses were planned has none; its PR names
  one, and Tin merges only the page itself.
- **Merging.** With a commit-to-main setting, Tin merges the adaptation's PR when it adds
  only the page (`merge_rule: page_only`), or when the PR's `Public URL:` line is the page's
  address and its other files sit in that address's folder (`merge_rule: chosen_route`), so the
  first PR that adds a section's route can merge too. The exact-copy proof, the five-file limit,
  the dependency ban, the pinned branch and the required-checks rule still apply; a PR that puts
  the page anywhere else stays open and says so.
