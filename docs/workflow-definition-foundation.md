# Phase 1: complete workflow versions and a shared file layout

## Subsequent implementation

This document records the original foundation release. Private lifecycle activation subsequently
shipped; see [private activation](private-workflow-activation.md). The locally implemented
[Slice A code executor](code-workflows.md) now extends this same package format with bounded
isolated Python resources. Native executor implementations still cannot be uploaded into trusted
workers. Foundation-specific limits below do not supersede those follow-up contracts.

## In plain English

A saved workflow is a particular recipe. Publishing a newer recipe must not quietly replace
half of the old one. A saved version now keeps its own input defaults, review requirements,
integration requirements, output contract, and scheduling rules together. Its instructions
are loaded from that same immutable revision when execution needs them.

The second change is a folder convention for recipes. It does not introduce another engine,
copy the project filesystem into a database, or make a file executable just because it exists.

Users do not choose a version when saving a workflow: new saves automatically use the latest
available definition. The internal pin keeps that saved recipe coherent across execution
paths. There is no version picker in the dashboard or MCP authoring flow. Future upgrades
should express relevant changes in plain language, not require people to manage commit hashes.

## Implemented boundary

`workflow_definitions.resolve_execution_contract` resolves a complete selected definition.
Direct starts, saved HTTP/MCP starts, prepared parent children, scheduled dispatch, and
HTTP/MCP configuration creation/editing use it. The latest exact-revision Postgres projection
can supply that revision; older selections are read from immutable code.storage. A saved
schema must equal the selected definition's schema. Missing or mismatched definitions fail
closed instead of borrowing current catalog metadata.

Run creation always receives that complete trusted definition, including review and
system-wiki metadata, and the transaction rechecks active identity and project ownership.
Repeated direct start requests recover the original run's selected revision before applying
defaults. Existing request/input/actor/lineage checks still apply. This does not change the
existing recovery contract for ambiguous Temporal starts.

Published workflow executor, source repository, and source path cannot be repurposed through
catalog synchronization. This preserves historical source resolution without adding another
version table or per-run source copy. Moving a live identity requires a separate explicit
migration design; it is not an ordinary catalog update. Existing parent/child atomic
publication remains unchanged.

Temporal still carries identifiers and small control facts, not definitions, prompts, or
files. Current availability remains an admission check; publishing B does not upgrade A.
The implementation code for native executors remains explicitly registered and deployed in
Python—pinning a definition does not freeze a historical Python binary.

## Prerequisites

A definition may declare `prerequisites`, modeled on `integration_requirements`: authored on
the built-in, emitted into the immutable JSON definition, validated at catalog sync and private
activation, and read from the run's pinned revision at start. A prerequisite is a claim about
project state, never a log entry: satisfaction is derived each time from succeeded pinned runs
(`workflow_runs` joined on the workflow key, since every Codex procedure shares one executor),
the project-state HEAD (files and `wiki/INDEX.md` sections) and active test identities. There is
no milestone table, so nothing can drift from what the project actually holds.

Three kinds and two levels:

- `run` — a succeeded, published run of `workflow` in this project. `match` names scopes
  (`product_host`, `site_origin`, `market`) that the earlier run's stored inputs must share with
  the new one, so a signup walkthrough for host A never satisfies a deep dive for host B.
  `via_input` instead names the input that carries the exact upstream run id.
- `artifact` — a project-state `path` at HEAD, optionally one `### ` `section` of
  `wiki/INDEX.md`. `{input}` placeholders resolve from the run's inputs; an empty placeholder
  input skips the requirement (a demo without a character needs no SVG).
- `identity` — an `active` Tin-owned test account on the host of `host_input`, found with the
  same lookup execution uses.
- `required` blocks the start with a `prerequisite_missing` diagnostic; `recommended` never
  blocks and is returned as `advisories` on the start response.

Enforcement lives in `run_service.start_workflow_run` before any row is written, so direct,
saved, MCP, retry, amendment and parent-child starts share it; an idempotent replay keeps its
original admission. Scheduled dispatch evaluates the same contract and projects a failed run
(`Prerequisite missing: …`) because a schedule cannot ask anyone to run something first. Both
HTTP (409) and MCP (`ToolError` JSON) return `{code, message, prerequisites[]}` where each unmet
item carries the upstream `workflow_key`, `workflow_id`, `how_to_satisfy` and a replayable
`suggested_call` for `start_workflow` with the matching inputs carried over.

### A prerequisite that is still running

When an unmet prerequisite's upstream workflow (the `run` workflow, or the `producer` of an
artifact or identity) has a pending or running run in the same project that would meet it
(same `via_input` run, same `match` scope, same identity host), admission attaches that run as
`running_run_id`. What happens next depends on whether the executor can wait:

- `content.answer_page` and Codex procedures read project state only after admission, so the
  run is admitted, required or recommended, and waits. The start response and evidence list it
  under `waiting`, and the Temporal start carries the `tin_prerequisite_wait` memo with the
  awaited run ids. Behind `workflow.patched("prerequisite-wait-v1")` the workflow polls the
  `prerequisite_wait` activity every 30 seconds on a durable timer, holding no compute, for up
  to 30 minutes. When the awaited runs end or the time runs out, the activity re-checks the
  prerequisites, pins the result under `waited` with plain `notes`, and either lets the run go
  on (reading the finished result, or without it when recommended) or refuses a still-missing
  required prerequisite with a `PrerequisiteMissing` failure before any work. Scheduled
  dispatch passes the memo to the child it starts; parent-dispatched children do not wait.
- `workflow.code` pins its project files at admission, and content drafts and brand capture pin
  their sources, so a result that lands later cannot reach them; other native executors have no
  wait step. For these the start says so instead: a required prerequisite is refused with
  "`<workflow>` run `<id>` is still running; call get_run …" and a `get_run` suggested call, and
  a recommended one starts without it and the note says why.

Every admission view of an unmet prerequisite carries a plain `note`, collected as
`prerequisite_notes` on the MCP start response and on `get_run`.

What the gate resolved is pinned on the run as `prerequisite_evidence` (migration 030): upstream
run ids and revisions, the identity id, the artifact path and HEAD revision, plus the advisories.
`list_workflows`, `get_workflow` and the HTTP catalog add the declaration and an input-free
`readiness` (`ready`, `advisory`, `blocked`) computed with at most one runs query, one identity
query, one HEAD listing and one index read for any catalog size; run scopes, `via_input` ids and
placeholder paths are only checked against real inputs at start, and the response says so.
The full `list_workflows` listing runs to well over 100K characters, too much for an agent's
context. `list_workflows(project_id, detail="short")` returns only `key`, `title`, a one-line
`description`, `schedule_modes`, the `readiness` state and `required_inputs` (required names
without a default) per workflow; `get_workflow` then gives the chosen one's full contract.

Private packages may declare `run` prerequisites naming built-in or their own active `custom.*`
workflows and `artifact` prerequisites on ordinary project files; identities and `producer`
hints stay Tin-owned.

## Versioned authoring layout

```text
workflow_packages/research.deep_dive/
  workflow.json
  PROMPT.md
  skills/
    research-deep-dive/
      SKILL.md
      references/...       # only when explicitly declared
```

The manifest wraps the existing definition:

```json
{
  "package_format": "tin-workflow-package-v1",
  "definition": {
    "key": "research.deep_dive",
    "executor": "codex.procedure",
    "...": "the complete existing workflow contract",
    "procedure": {
      "prompt_path": "PROMPT.md",
      "skills_path": "skills",
      "skill_files": ["skills/research-deep-dive/SKILL.md"],
      "...": "the existing entry-skill, workspace and output contracts"
    }
  }
}
```

This shortened illustration is not a runnable manifest. The executable example in
`tests/test_workflow_packages.py` exports the complete current `research.deep_dive` definition
and exact prompt/skill bytes using `export_workflow_package`. It then loads both that export
and a renamed project-private equivalent and proves parity with the existing procedure.
Export returns a file map; it does not write, activate, or start anything.

One loader recognizes the explicit format and maps relative resource locations to the
existing internal `procedures/<key>/...` contract. No second materialized project copy is
written. Legacy `workflows/<key>.json` and their existing procedure resources continue to
load in their original locations. Existing built-in source locations and catalog versions
are not migrated by this phase.

New-format limits and checks:

- The key and folder must agree. Paths must be normalized, local, package-contained paths.
  Traversal, remote references, symlinks, submodules, and non-directory ancestors are rejected.
- At most 64 declared files, including the manifest and prompt. The manifest is at most
  512,000 bytes; existing procedure limits remain 32,000 prompt bytes, 64,000 bytes per
  skill resource, and 128,000 bytes for the prompt and resources together. Content must
  satisfy the existing UTF-8, skill identity, entry-skill, and result-contract validators.
- The new input language accepts only a bounded, closed subset of supported schema fields:
  at most 32 inputs, bounded text and string arrays, small enums, existing scalar controls,
  and recognized formats. Arbitrary regex, remote references, composition, and unknown
  schema keywords are rejected. Accepted legacy schemas are not retroactively tightened.
- `package_digest` fingerprints the exact manifest and declared resource bytes using framed
  relative names and contents. Unrelated project files are excluded; edits to recipe bytes
  change the digest. It is a helper for later activation, not a new persisted registry.
- Native definitions may use the manifest representation but cannot upload Python executor
  code. Private capability policy and permission checks are still a later gate; successful
  decoding is not authorization.

## Verification

The regression suite uses an isolated local Postgres schema and fake external services. It
saves A, publishes B with changed defaults, review, system wiki, instructions, output and
scheduling policy, then proves A through HTTP, MCP, scheduled dispatch and prepared children.
It also tests editing A, repeat-start recovery after B, project mismatch, saved-schema mismatch,
source-identity stability, exact-byte export/load parity, resource bounds and unsafe paths.
Existing parent-publication, procedure, integration, and Temporal tests remain part of the
full suite. These are foundation tests, not a paid private-execution pilot.

## Deliberately not implemented

No private validation/activation APIs, private execution policy, metering, quotes, billing,
upgrade controls, new dashboard UI, or public-source launch. No database migration, provider
route, sandbox image, credentials, or autonomy change. The next slice is the isolated,
bounded execution proof and policy needed before a private activation pilot—not turning on
private execution merely because the folder format now works.
