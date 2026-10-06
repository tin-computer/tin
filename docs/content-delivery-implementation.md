# Reviewed content → GitHub PR

## What changes

Keep the content program, draft, review, and PR in the same product experience. A program
can save an explicit GitHub repository, a Markdown destination pattern, and optional site
frontmatter. New drafts pin those settings when admitted. Their approval means “approve
and open PR”; absent settings, an explicit draft-only choice, and historical runs retain
draft-only approval. Nothing merges or publishes a website.

Delivery is a deterministic trusted operation, not another model call. Strip Tin provenance
and verification notes, preserve the reviewed article, and deliver one `.md` file. New
pages use the configured pattern; updates require an explicit item-to-file mapping and
preserve existing frontmatter. Unsupported formats must not be guessed or rewritten.

## Implementation

1. Store bounded delivery settings beside the editable plan, using the normal Files CAS
   service. Pin the repository identity and settings in the draft selection receipt.
2. After approval, run one trusted delivery activity. Freeze the exact draft revision,
   generated file, and GitHub base before using the existing idempotent integration gateway.
3. Keep draft success independent of delivery failure. An internal Temporal operation can
   retry delivery for that same approved draft; it never reruns drafting or changes approval.
   No new saved workflow card, catalog choice, model credential, or publishing engine.
4. Reuse Paper 81/105 work-column disclosures and existing Tin form controls. Display the
   pinned consequence at review, delivery outcome on the program, and a PR link on success.
   MCP uses the same settings, status, and retry services.

## Verification

Cover draft-only and historical approvals, pinned settings under edits, membership isolation,
exact article extraction, explicit update mappings, frontmatter, concurrent/duplicate delivery,
ambiguous GitHub recovery (including newly added files), repository changes, and retry without
another model purchase. Replay existing procedure histories. Exercise light/dark/mobile UI.

Live acceptance must use an explicitly configured destination. Do not approve or publish any
existing customer draft as a test, or assume that Tin's repository hosts a customer website.
Record exactly which deployment and tests passed below before claiming completion.

## Implemented contract and verification

- `content.generate` 1.2.0 adds a default “use program settings” delivery input. Earlier
  pinned versions and existing approvals do not acquire delivery. Users do not pick versions.
- Program configuration lives at `content/plans/{program_id}/delivery.json`; Files, HTTP,
  and MCP share the same revision-checked save. The first supported destination is a regular
  `.md` file, not MDX, a CMS, or a guessed website build adapter.
- Delivery records the reviewed revision and digests, prepared GitHub base, and exact content.
  Recovery accepts new files, finds an already-created open or closed PR, and tolerates
  unrelated default-branch advances. Changed destinations, rewritten history, incomplete
  comparisons, symlinks, and submodules stop delivery without losing the draft. Recovery
  still needs the exact result branch to exist when the PR receipt was not recorded.
- This deliberately uses GitHub's existing merge semantics, not an automatic rebase or merge.
  See the [Git tree contract](https://docs.github.com/en/rest/git/trees#get-a-tree) for regular
  file modes and bounded tree reads.
- Local full Python suite: **1,279 passed, 8 skipped**. Focused tests also record and replay a
  pre-delivery procedure history, plus restart delivery without another Codex turn.
- Six content browser tests passed: full My System page, existing planner and draft controls,
  and new delivery controls in light/dark and mobile layouts. Visual verification used the
  packaged application with synthetic HTTP; the signed-in in-app browser was unavailable.
- Live article-PR acceptance remains pending a suitable explicit repository/content path.
  Never assume the connected repository hosts a particular website; verify ownership and paths.

## Page bundles

An approved article may carry figures, interactive pieces, diagrams, videos and callouts
([review](content-review.md)). content.deliver 1.7.0 and website.change 1.6.0 put them on the
site in its own form:

- The approved source lists each asset's project path and checksum; a delivery run with assets
  checks out the approved revision (`procedure_project_revision`) and copies the files from
  `/home/user/state`. Mermaid blocks use the site's own Mermaid support, or Codex draws them as
  SVG figures in the site's style; Tin's diagram renderer is not involved.
- `figure_check` sits beside `copy_check`: each approved file must arrive byte for byte (same
  checksum), each video by its address or ID, each diagram as a new SVG or the site's Mermaid
  support. `not_confirmed` only holds Tin's own merge; the pull request still opens.
- One pull request or commit may hold up to 30 files and 2 MB (`PULL_REQUEST_MAX_*`), and a
  saved patch is read back up to 4 MB. Procedures that declare nothing keep 512 KB.
