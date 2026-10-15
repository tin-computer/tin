# Organic traffic execution — first runnable recipe

This document records the original September 9 recipe and its historical acceptance.
The current v2 continuation adds a reviewed article and optional repository-adapted PR;
see [Organic content continuation](organic-content-continuation.md). The original
acceptance limitations below are historical, not claims about the new recipe.
For the audit's repair rules, see [Technical repair](technical-fix.md).

## ELI5

The system starts two research jobs: inspect the website and research buyer searches.
Their exact saved results become the inputs to an editable content program. With GitHub
connected, Tin also opens one unmerged PR with the audit's fixes in the repository you
selected for the site. Each job is still callable on its own.

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

**v7 (0.7.0): the system measures the site each week.** After the audit, the parent runs
`organic.traffic_snapshot` and then `organic.content_efficacy` (Page decisions) as child runs,
and waits for both before the content plan, which reads `analytics/traffic-snapshot.json` and
`content/efficacy.md`. Each is saved as a weekly configuration on the weekly page refresh's
day: the snapshot two hours before the refresh, Page decisions one hour before, so the
refresh reads this week's decisions and the decisions this week's snapshot. An earlier
saved schedule of either is kept, with its inputs, and still runs once now. Both need Search
Console; without it nothing is saved and the report names the reason. A measurement that
cannot start never fails the recipe. `content.refresh` now picks the first page Page decisions
marks for a refresh that is not waiting, in its order, never a page it keeps, merges or
retires, and falls back to the audit's pick when the file is missing or older than its window.
Onboarding no longer installs the two workflows on their own; the system sets them up. Runs
pinned to `organic-traffic-v6` keep their steps; a test pins v6's policy digest. The parent's
spending pool grows by $1 for Page decisions' bounded model call; the snapshot makes none.

**v6 (0.6.0): both writer steps go through website.change** (Emre, 10/1). The technical step
starts website.change `source: audit`, and the delivery step starts website.change for the
approved draft (its page source): Tin merges the PR once the required checks pass (every check when none are required) when
the approval picked commit to main, and a pull request pick or a protected page leaves the
PR for the founder. `content_delivery: draft_only` still keeps the Markdown in Tin. Both
website.change children have a $5 procedure ceiling. Only `organic-traffic-v6`, `-v7` and
`-v8` remain; a run pinned to an older policy fails at preparation.

Manual-only. Inputs: HTTPS site origin, English-language buyer market, explicit buyer
context, content start date, duration (default six months), and keyword spending ceiling
(default $9). Technical repair is on by default from 0.8.1 (`technical_fix`). It uses the
repository selected on the GitHub connection, which Start here asks the founder to choose as
the one that serves the site; a run that names `expected_repository` itself still needs
`repository_serves_site`. Without GitHub, or without a selected repository, the step is
skipped with that reason; a named repository without the confirmation is skipped as
`repository_not_confirmed`, and an audit whose fixes can't be planned as `fixes_unavailable`. 0.8.0 and earlier defaulted to off and refused to start with it
on until both repository fields were given. From 0.3.0, `article_weekdays` and
`article_local_time` choose the weekly drafting schedule it saves (see
[weekly drafting](organic-content-continuation.md#weekly-drafting-030)).

The parent checks required provider availability and spending ceilings before creating
children. It pins all child definitions from the parent's atomic registry revision.
Child runs do not silently follow a later catalog publication.

1. Start `organic.audit` and `organic.keyword_plan` as independent child runs.
2. After the audit, unless turned off, propose the technical fixes. Under `organic-traffic-v6`
   (0.6.0, the default for new runs) the step starts website.change with the latest audit's
   fixes (`source: audit`) and the system's repository; it passes no judgment-call answers,
   so the rows wait for the founder and the run opens a pull request.
3. After both research runs succeed, create one manual `content.plan` configuration in
   My system, using these exact research run IDs, then run it. From `content.plan` 1.1.0 its
   first plan waits in Decisions until the founder approves it, and the system's draft step
   and weekly articles wait with it (the technical step runs beside it and does not wait). A
   declined plan skips the draft and the weekly articles (`content_plan_declined`); the
   system never falls back to an older saved plan the founder did not choose.
4. Publish `reports/organic-system/{run_id}/RESULT.md`, linking the exact child artifacts
   and recording skipped, blocked, or failed branches honestly.

Temporal coordinates the parallel branches. Existing worker activity concurrency and
provider/sandbox limits still apply; this does not raise the pooled-Codex concurrency.
The content program is amendable through the existing editor and MCP controls. Its
schedule remains off until a member chooses one. Nothing writes articles or publishes.

Failed research prevents dependent planning but does not discard successful sibling
outputs or automatically repeat paid research. No eligible technical finding creates
an explicit skipped step, without allocating repair compute or manufacturing a defect.

## MCP and HTTP

Use normal `get_workflow` / `start_workflow` for `organic.traffic_system`. Standalone
`organic.audit`, `organic.keyword_plan`, and saved `content.plan` remain unchanged.

`preflight_website_change` previews the audit's repairs. Execution independently resolves and
pins its own trusted binding.

`get_run` now includes the parent's `system.steps` facts, read only from Postgres.
The equivalent read is:

`GET /api/projects/{project_id}/organic-system/runs/{run_id}`

Stop: MCP `stop_organic_system`; HTTP `POST .../organic-system/runs/{run_id}/stop`.
It enforces membership and durable stop fences. Parent stop prevents new children
and stops remaining child work. Already-reserved publication/delivery may finish and
is returned as `finishing_run_ids`; it is not recalled or silently terminated.

## Acceptance remaining

1. Positive live PR acceptance of the technical step needs a real fixable finding on an
   authorized repository, or an explicitly authorized test fixture.
2. Investigate buyer-panel search reliability and improve the content planner's depth before
   claiming the entire organic research/editorial offering is quality-complete.
3. Signed-in browser interaction with the newly saved program remains unverified; the existing
   browser fixtures and authenticated MCP/Postgres checks are not a substitute for that check.

For current drafting, review and delivery behavior, see [content continuation](organic-content-continuation.md).
Backlinks, automatic scheduling of this parent, PR merging and deployment of customer sites
remain outside this contract.
