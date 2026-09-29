# Repository-aware article delivery

## In plain English

The article remains a Markdown document in Tin. Once the founder approves it, **Prepare
PR** adapts that same article to the connected website's existing structure. The user
chooses the article and repository, not Markdown versus a component file. The PR stays
unmerged. WordPress and other CMS publication need their own connectors later.

This is an explicit continuation on the content card. Repository adaptation uses compute,
so the normal spending confirmation applies. Approval of an older draft is still only
approval of its content; it does not silently acquire a publishing action.

## Implementation

- `content.deliver` is an ordinary immutable Organic traffic catalog definition on the
  existing `codex.procedure` executor, not another engine or saved program. It is callable
  independently through HTTP/MCP and from the existing My System content program card.
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
  PR evidence. It may change up to five text files / 180 KB. It cannot add dependencies.
  Markdown-native sites keep Markdown. Component sites use an existing Markdown renderer
  and retain the article as a complete JSON string or JSON data. Unsupported rendering
  arrangements are reported as a prerequisite, not silently approximated as rewritten JSX.
- The switchboard checks exact source preservation before accepting a patch and again
  before delivery. The immutable checkpoint survives sandbox teardown. The existing
  GitHub gateway checks the destination, overlaps and retry identity, and opens an
  unmerged PR. Unrelated base advances use the existing bounded content-delivery exception;
  changed destination paths are not silently rebased.
- Every repository procedure (article delivery, site health, technical fixes, code maps)
  reads a snapshot of up to 20,000 eligible files / 100 MB, each file at most 2 MB. The
  gateway downloads the pinned commit as one tarball and checks every file against its blob
  hash in the pinned tree; paths the tarball omits or rewrites (`export-ignore`,
  `export-subst`) are read individually. Oversized repositories fail before the download,
  with their eligible file count and byte total alongside the bound.
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

An attempt with no accepted patch may be explicitly retried as a **new metered
adaptation**, using “Try adaptation again” on the content card or `retry_run_id`
through MCP (internal attempt context, not a revision selector). A saved
patch or any prior provider delivery receipt prevents that fresh purchase until the
existing effect is reconciled. Changed destination content requires fresh review;
this slice does not silently throw away an earlier checkpoint or redirect its PR.

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

Article, public-article and answer-page runs carry `page_url` in the run API
(`GET /api/projects/{id}/runs`, `GET /api/workflows/runs/{id}`) and in MCP `get_run`.
The decision card and the document page show it as one line. Only a live page is a link.

| Field | Meaning |
| --- | --- |
| `url`, `label` | The address, labelled `Proposed URL`, `Will be published at` or `Live at`. |
| `source` | `delivery_route` (the adaptation PR's `Public URL:` line), `plan_destination` (the plan item; final only for an update to an existing page), `folder_route` (Tin confirmed that the site shows other files from the target folder at this route), or `title_slug` (a proposed slug under the site; never final). |
| `state` | `proposed`, `planned`, `merged` (merged or committed, page not found yet) or `live`. |
| `note` | What approving does, or what happened: for example "Approving commits content/answers/x.md to owner/site as a Markdown file only". |
| `repository`, `file_path`, `pull_request` | The delivery target when Tin writes to GitHub. |
| `checked_at`, `checkable` | When Tin last looked, and whether another look can change anything. |
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

`Publish now` still uses the exact Markdown publisher for answer pages and public articles;
this section only makes its result visible. Routing those drafts through repository
adaptation needs `content.deliver` to accept non-plan sources.
