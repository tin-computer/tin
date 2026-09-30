# Organic traffic: plan → reviewed draft → article PR

## Scope

`organic.traffic_system` 0.2.0 extends the existing fixed recipe. It does not add a
workflow engine or a publisher. Audit and keyword research still run in parallel,
their exact outputs initialize an editable content program, and optional technical
repair remains a separate branch. The parent then runs `content.generate` for the
next article in chronological plan order.

With a selected GitHub repository, the review action is **Approve & open PR**.
After the final revision is approved, the existing `content.deliver` procedure
adapts that exact copy to the repository's format and opens an unmerged PR.
The original Markdown remains readable in Tin. Generation notes stay separate
from the public article.

Without GitHub (or with explicit `content_delivery=draft_only`), review completes
with the Markdown in Tin, available for manual export/copy or later explicit
delivery. There is no pretend CMS publication and no requirement to connect GitHub.
An existing writing guide is used when available; this recipe does not manufacture
style samples or implicitly run style extraction.

The parent itself drafts **one next article per execution**. Existing content-plan
scheduling prepares batches; it is not silently upgraded into a publishing schedule.
Backlink planning and outreach are deferred. Re-running the research parent creates a
new program; continue an existing program through its existing Draft next action, rather
than repurchasing research.

## Weekly drafting (0.3.0)

`organic.traffic_system` 0.3.0 (policy `organic-traffic-v3`) keeps the same child runs.
Once the content plan exists, and beside the first draft, it also saves one weekly
`content.generate` configuration for that program in My system:

- `article_weekdays` (lowercase weekday names, default `["tuesday"]`) and
  `article_local_time` (`HH:MM`, default `10:00`) set the schedule. An empty list saves
  nothing and leaves drafting on demand.
- The timezone is the one Start here recorded for the founder, then the newest saved
  schedule's, then the project's (UTC by default).
- The first occurrence is on or after the start of the founder's day one week after the
  parent saves it, so a Tuesday system run first drafts again the following Tuesday, never on
  top of the system's own first article. (Counting seven days from the moment of saving skipped
  a week when the run finished after the drafting time.)
- Each occurrence selects the next eligible plan article exactly as a manual start does and
  waits for review. It holds, without a run, while any article from the program waits for
  review or is drafting, or while a plan revision holds the next batch. When nothing is left
  to draft, or the next article needs the founder (an editorial assessment, a saved result,
  a changed brief), the schedule pauses with that reason in My system instead of failing a
  run every week.
- Occurrences follow the program's saved delivery settings, like any manual draft. They do
  not reuse the parent's `content.deliver` adaptation, which belongs to the parent run.
- The configuration is not a child run and adds nothing to the parent's spending bound.
  Each occurrence is an ordinary scheduled run with its own funding: the content.generate
  ceiling ($5), project limits and standing schedule authority apply. Under the hosted
  default $10 monthly limit, later occurrences in a month may not start; the Start here
  handoff says so (see [workflow billing coverage](workflow-billing-coverage.md#weekly-articles-and-the-default-limits--september-29-2026)).
- Saving the schedule never fails the recipe. `RESULT.md` and the system facts record it
  as succeeded, skipped (`weekly_articles_off`, `content_plan_unavailable`), blocked
  (`weekly_schedule_unsupported`, `scheduling_unavailable`) or failed
  (`weekly_schedule_not_saved`).

In-flight v2 histories replay through the `organic-weekly-articles-v1` patch boundary; a
v2 parent that reaches the new step records `not_in_pinned_recipe` and saves nothing.

## Shared dashboard and MCP behavior

- Both start the same parent and use the existing article review/revision APIs.
- System-linked approvals retain repository adaptation; the standalone Publish now
  option does not route them through the generic Markdown publisher. An explicit
  `delivery=none` approval keeps the copy in Tin and skips the parent's PR step.
- Requests for changes stay on the selected article, preserving its source brief,
  writing guide and destination. The parent waits for the revision chain and delivers
  only its final approved copy.
- The parent captures the connected repository identity before research starts.
  Changing the integration cannot silently redirect an already-reviewed article.
- While the parent owns automatic delivery, the saved content card does not offer a
  duplicate manual Prepare PR action. Existing recovery controls remain available.
- A no-draft assessment produces no PR. Already-covered content is explicitly skipped;
  missing evidence or a replanning judgment requires attention, not a random substitute.
- Stopping the parent prevents subsequent automatic delivery. Drafts already waiting
  for review stay available; unfinished compute uses existing procedure stop controls.
- A failed delivery does not delete, redraft or lose the approved article. Review and
  delivery status are Postgres projections; public copy never receives status notes.

## Compatibility and cost

The parent and all six child definitions/resources publish in one registry revision.
The v2 recipe pins that revision for every child. Historic v1 definitions and saved
configurations retain the research/planning-only recipe. Existing in-flight Temporal
histories replay through the `organic-content-continuation-v1` patch boundary.
Users do not choose internal definition versions.

The v2 estimate includes one draft and, for automatic delivery, one repository
adaptation. New content children share the parent's existing usage ledger only when
their pinned definitions match its prepared recipe. This is not a second charge or
a six-month reservation. Historical quotes remain unchanged; API-billed execution
must be available before a new billed v2 parent starts research. User-requested
revision runs retain their existing separate accounting.

No database migration, new model route, sandbox build, credentials, payment-mode
change, private-workflow activation or automatic approval is part of this change.
The project-scoped execution gate and existing worker concurrency are unchanged.

## Verification boundary

Tests exercise real disposable Postgres, local Temporal execution and replay of both
old/new recipes, exact final-revision selection, shared billing, stop/repository-change
guards, Markdown retention and light/dark browser controls. Model output, storage and
GitHub calls use fixtures. No paid ClawMessenger generation, article approval, customer
PR or website publication is claimed by these tests; that acceptance exercise is
intentionally not a release blocker for this composition change.

## When the new plan does not finish (0.4.0)

`organic.traffic_system` 0.4.0 pins policy `organic-traffic-v4`. If its own content plan
fails or is blocked, for example because the plan's model call timed out or research was
unavailable, the draft and the weekly articles use the project's most recent content program
whose plan did finish. Tin picks that program once per run and saves the choice, so a retried
step reads the same program. `RESULT.md` gains a "Content plan used" section naming it. The
run itself still reports that a step could not finish. Without any finished plan, the draft
and weekly steps stay blocked or skipped with `content_plan_unavailable`, as before. v3 and
older recipes keep their behaviour.

Reusing a program also preserves its existing article schedule and any pause or timing edits.
Tin creates a schedule only when that program has none; concurrent system runs share this
short save. A paused schedule stays paused and the result says `existing_schedule_paused`.
