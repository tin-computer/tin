# Organic traffic execution — first runnable recipe

This document records the original September 9 recipe and its historical acceptance.
The current v2 continuation adds a reviewed article and optional repository-adapted PR;
see [Organic content continuation](organic-content-continuation.md). The original
acceptance limitations below are historical, not claims about the new recipe.
For current metadata repair profiles and finding eligibility, see
[Technical repair](technical-fix.md).

## ELI5

The system starts two research jobs: inspect the website and research buyer searches.
Their exact saved results become the inputs to an editable content program. If you
enable technical fixes and confirm the GitHub repository, Tin can also inspect one
supported audit finding and propose a small, unmerged PR. Each job is still callable
on its own.

This is a fixed Temporal recipe, not a graph engine or a second database in Files.
The files remain the working material. Existing Postgres run rows and effect receipts
identify which exact outputs belong to this execution.

An explicit retry of a failed keyword plan reuses its completed research collection and
saved search-result reads when the project, workflow, definition and inputs still match.
It runs the remaining review and publication normally. Original observation dates and source
run provenance stay in the evidence; the retry does not charge for cached reads. Unconfirmed
reads remain unknown, and reads that were never attempted can run. Starting a fresh run,
changing the job, or retrying before collection finished still performs new research.
Retrying the traffic system passes the previous failed keyword child to this same recovery
path; it does not require a separately saved keyword workflow.

## Verification

Use disposable Postgres, local Temporal replay, mocked GitHub delivery/recovery,
and the packaged browser fixtures. A positive live repair requires an authorized
repository and a genuine supported finding; fixtures do not prove deployed delivery.
Parent and child definitions publish atomically so first sync cannot pin an incomplete
recipe. Old revisions retain their original children and resources.

## Callable workflows

### `organic.traffic_system`

**v6 (0.6.0): both writer steps go through website.change** (Emre, 10/1). The technical step
starts website.change `source: audit` instead of organic.technical_fix, and the delivery step
starts website.change for the approved draft (its page source) instead of content.deliver:
Tin merges the PR once the required checks pass (every check when none are required) when
the approval picked commit to main, and a pull request pick or a protected page leaves the
PR for the founder. `content_delivery: draft_only` still keeps the Markdown in Tin. Runs pinned
to `organic-traffic-v5` keep their children, receipts and report word for word; a test pins
v5's policy digest. The spending ceiling is v5's: both website.change children have the same
$5 procedure ceiling as the workflows they replace.

Manual-only. Inputs: HTTPS site origin, English-language buyer market, explicit buyer
context, content start date, duration (default six months), and keyword spending ceiling
(default $9). Optional technical repair requires both an exact `owner/repository` and
the member's confirmation that it serves this site. From 0.3.0, `article_weekdays` and
`article_local_time` choose the weekly drafting schedule it saves (see
[weekly drafting](organic-content-continuation.md#weekly-drafting-030)).

The parent checks required provider availability and spending ceilings before creating
children. It pins all child definitions from the parent's atomic registry revision.
Child runs do not silently follow a later catalog publication.

1. Start `organic.audit` and `organic.keyword_plan` as independent child runs.
2. After the audit, if requested, propose one technical fix. Under `organic-traffic-v6`
   (0.6.0, the default for new runs) the step starts website.change with the latest audit's
   fixes (`source: audit`) and the system's repository; it passes no judgment-call answers,
   so the rows wait for the founder and the run opens a pull request. A recipe pinned to v5
   or earlier still starts `organic.technical_fix` (under `site-fix-v5` one PR for every
   fixable finding; earlier policies take one finding).
3. After both research runs succeed, create one manual `content.plan` configuration in
   My system, using these exact research run IDs, then run it.
4. Publish `reports/organic-system/{run_id}/RESULT.md`, linking the exact child artifacts
   and recording skipped, blocked, or failed branches honestly.

Temporal coordinates the parallel branches. Existing worker activity concurrency and
provider/sandbox limits still apply; this does not raise the pooled-Codex concurrency.
The content program is amendable through the existing editor and MCP controls. Its
schedule remains off until a member chooses one. Nothing writes articles or publishes.

Failed research prevents dependent planning but does not discard successful sibling
outputs or automatically repeat paid research. No eligible technical finding creates
an explicit skipped step, without allocating repair compute or manufacturing a defect.

### `organic.technical_fix`

Manual-only Tin-owned template on the existing `codex.procedure` executor. Inputs:

- `audit_run_id` and `audit_revision` from the verified source picker/API.
- `finding_ids` (optional, empty means every fixable finding) and `decisions`
  (`finding_id=choice` answers to the judgment calls `preflight_technical_fix` returns).
- `expected_repository` and `repository_serves_site=true`.
- Optional bounded `context`; project identity remains bound outside editable inputs.

New runs pin `site-fix-v5`: one PR fixes every fixable finding of the audit, grouped by
kind of change, and the live check reports each finding after the merge; see
[Technical repair](technical-fix.md#site-fix-v5-everything-the-audit-found). The parent
passes the whole audit with no decisions, so judgment calls are listed for a later run.
The rest of this section describes the metadata policies that older pinned runs keep; they
take one finding (`finding_id`).

The first supported finding is `metadata.title_missing`, with a completed technical
crawl and at most five affected URLs. Partial AI observations do not invalidate a
complete technical crawl.

The **only enabled verification profile is static HTML without a build step**:

- Fresh HTTPS responses must stay on the audited host, return bounded UTF-8 HTML,
  and still lack the title. DNS is validated and the actual connection is pinned to
  the public IP, retaining the original HTTP Host and TLS server name.
- Every still-affected response must match exactly one pinned repository HTML file.
  At most three files and 60 KB of original HTML are allowed, with a further bound on
  escaped sandbox-context size.
- Recognized build manifests cause no-change, even when one HTML file happens to
  match. Python/JS/framework builds need separately proven build/test profiles.
  In particular, this does **not** claim a build-verified repair path for Tin Lite itself.
- The sandbox and switchboard independently require missing-before/present-after
  titles inside the existing head, with every other byte unchanged. The sandbox
  runs the Tin-owned verifier outside the editable checkout.
- A patch must cover exactly the matched files. An opted-in no-change checkpoint
  must contain zero changed files and cannot claim the finding was fixed.
- Delivery rechecks live applicability, repository/installation identity, default
  branch and base commit, and refreshed open-PR overlap. Incomplete PR evidence
  fails closed. Unexpected retry-branch edits are never overwritten.

Already-resolved pages, unsupported source/build profiles, and existing overlapping
PRs produce a durable explanation without a sandbox or new branch. Unavailable
evidence and failed verification are failures, not successful repairs. A sandbox
that cannot prepare a safe patch may return an explicit unresolved no-change result.

Successful external delivery creates one unmerged GitHub PR and a run-scoped
`reports/technical-fix/{run_id}/RESULT.md`. The report retains audit provenance, repository
revision, fresh observations, checks, and limitations. A PR is not a deployed repair.

Completed delivery receipts recover without another provider write, even if GitHub is
later disconnected. Ambiguous delivery still requires reconciliation against the
bound repository and the exact proposed branch contents.

## MCP and HTTP

Use normal `get_workflow` / `start_workflow` for either registry key. Standalone
`organic.audit`, `organic.keyword_plan`, and saved `content.plan` remain unchanged.

Existing source discovery tools are `list_technical_fix_sources`,
`get_technical_fix_source`, and `preflight_technical_fix`. The preview itself is read-only;
execution independently resolves and pins its own trusted binding.

`get_run` now includes the parent's `system.steps` facts, read only from Postgres.
The equivalent read is:

`GET /api/projects/{project_id}/organic-system/runs/{run_id}`

New controls:

- MCP `stop_organic_system`; HTTP `POST .../organic-system/runs/{run_id}/stop`.
- MCP `stop_technical_fix`; HTTP `POST .../technical-fixes/runs/{run_id}/stop`.

Both enforce membership and durable stop fences. Parent stop prevents new children
and stops remaining child work. Already-reserved publication/delivery may finish and
is returned as `finishing_run_ids`; it is not recalled or silently terminated. Technical
stop rejects while canonical delivery holds its lock or an external delivery needs
reconciliation. A stopped repair cannot reacquire a sandbox lease.

## Acceptance remaining

1. Positive live PR acceptance needs a real missing-title finding on an authorized repository
   within the static-HTML profile, or an explicitly authorized test fixture. Framework and
   missing-description support require separately proven verification profiles. The current
   Tin Lite site cannot supply a positive case within this version's scope.
2. Investigate buyer-panel search reliability and improve the content planner's depth before
   claiming the entire organic research/editorial offering is quality-complete.
3. Signed-in browser interaction with the newly saved program remains unverified; the existing
   browser fixtures and authenticated MCP/Postgres checks are not a substitute for that check.

For current drafting, review and delivery behavior, see [content continuation](organic-content-continuation.md).
Backlinks, automatic scheduling of this parent, PR merging and deployment of customer sites
remain outside this contract.
