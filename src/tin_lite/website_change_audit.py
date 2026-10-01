"""website.change, phase 2: the audit finds; website.change plans, fixes and publishes.

A website.change run with `source: audit` repairs what the project's latest organic audit
found, under site-fix-v5's rules (technical_repair_plan, technical_batch): every fixable
finding, at most 30 per run, 20 files and 800 changed lines, checked on the live site after
the pull request merges (technical_fix_live). The code is the technical fix's, shared rather
than copied; organic.technical_fix stays registered for its pinned runs.

Each fixable finding becomes one `website_changes` row, its change ID the audit's stable
finding ID (`oa_…`). A preview (`preflight_website_change`) or a start records the rows: new
IDs are added, a pending row takes the newer plan, and a row the founder approved or declined
stays as it is, so a declined finding never comes back. What a row's approval covers is the
repair itself (`intent`): the check, the kind of change and the judgment-call answer. The
pages it reaches are re-read every run, because a finding ID is stable per check and site
while the pages an audit lists move week to week.

Judgment calls stay with the coding agent and the founder: the preview lists them as
`decisions_needed` with site-fix-v5's `ask`. An answer recorded in an approved row is reused
by later runs.

One run makes one pull request. Rows with a recorded approval that touch no protected page
go first: Tin merges that pull request once the repository's required checks pass. Otherwise
the run takes the rows waiting for approval (and approved rows on protected pages) into a
pull request the founder merges. Rows already in an open or merged website.change pull
request are skipped and named, so a weekly run does not open the same pull request again.

Phase 3 plugs planned URL changes (#239's `planned.redirect` and `planned.noindex`
selections) into the same rows with source `planned_url_change` (`row_source`).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite import content_repository_delivery as delivery
from tin_lite import technical_batch as batch_rules
from tin_lite import technical_fix as contract
from tin_lite import technical_repair_plan as repair_plan
from tin_lite import website_change
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.website_change import (
    ChangeRow,
    WebsiteChangeConflict,
    approval_for,
    file_protected,
    project_protected_paths,
    protected,
    protected_paths,
)

logger = logging.getLogger(__name__)

SOURCE = delivery.AUDIT_SOURCE
# The repair rules a technical run follows. website.change pins them in its definition.
POLICY = repair_plan.POLICY
# How many earlier technical runs Tin looks back over for pull requests still in flight.
MAX_EARLIER_RUNS = 20
ACTIVE = ("pending", "running", "needs_input")
DIRECT_REASON = (
    "You approved these changes in Tin, so Tin merges the pull request once your "
    "repository's required checks pass."
)
UNAPPROVED_REASON = (
    "No one approved these changes in Tin yet, so the pull request waits for your review and merge."
)


def repairs_site(run) -> bool:
    return delivery.repairs_site(run)


# --- The change row of one planned repair ------------------------------------------------


def row_source(entry: dict) -> tuple[str, str]:
    """The row source and kind of a planned repair: an audit finding, or (phase 3) a URL
    change another workflow planned."""
    check = entry.get("check_id", "")
    if check.startswith("planned."):
        return "planned_url_change", check.split(".", 1)[1]
    return SOURCE, entry["kind"]


def intent(entry: dict) -> dict:
    """What an approval covers: the finding, the kind of change and its judgment-call answer."""
    return {
        "finding_id": entry["finding_id"],
        "check_id": entry["check_id"],
        "kind": entry["kind"],
        "decision": entry.get("decision"),
    }


def _site_path(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    path = urlsplit(url).path if url.startswith(("http://", "https://")) else url
    return website_change.site_path(path or "/")


def entry_paths(entry: dict) -> list[str]:
    """The site paths a repair reaches: its pages, both ends of its redirects, and the
    robots.txt or sitemap it edits."""
    paths: list[str] = []
    for value in [
        *entry.get("urls", []),
        *(end for move in entry.get("redirects", []) for end in (move["from"], move["to"])),
    ]:
        path = _site_path(value)
        if path and path not in paths:
            paths.append(path)
    if entry["kind"].startswith("robots_"):
        paths.append("/robots.txt")
    if entry["kind"].startswith("sitemap_"):
        paths.append("/sitemap.xml")
    return paths[:20] or ["/"]


def change_row(entry: dict, audit: dict) -> ChangeRow:
    source, kind = row_source(entry)
    return ChangeRow(
        change_id=entry["finding_id"],
        source=source,
        kind=kind,
        title=f"{entry['issue']}: {entry['change']}",
        paths=tuple(entry_paths(entry)),
        content_sha256=hashlib.sha256(canonical_json(intent(entry))).hexdigest(),
        detail={
            "audit_run_id": audit["audit_run_id"],
            "audit_revision": audit["audit_revision"],
            "check_id": entry["check_id"],
            "group": entry["group"],
            "change": entry["change"],
            **({"decision": entry["decision"]} if entry.get("decision") else {}),
            **({"survivor": entry["survivor"]} if entry.get("survivor") else {}),
            "urls": entry.get("urls", [])[:10],
        },
    )


# --- Pull requests already carrying rows ---------------------------------------------------


async def rows_in_flight(database, integrations, *, project_id) -> dict[str, dict]:
    """Change IDs sitting in an open or merged website.change pull request, with that PR.

    A PR whose state Tin can't read counts as open: better to skip a row than to open a
    second pull request for it.
    """
    runs = await database.pool.fetch(
        """
        SELECT r.id, s.result AS source, p.result AS publication, m.result AS merge
        FROM workflow_runs r
        JOIN effect_receipts s ON s.execution_key = 'content-delivery:' || r.id::text
            || ':source' AND s.status = 'completed'
        JOIN effect_receipts p ON p.execution_key = r.id::text || ':procedure_canonical_commit'
            AND p.status = 'completed'
        LEFT JOIN effect_receipts m ON m.execution_key = 'content-delivery:' || r.id::text
            || ':merge' AND m.status = 'completed'
        WHERE r.project_id = $1 AND r.workflow_id = $2 AND r.input->>'source' = $3
        ORDER BY r.created_at DESC, r.id DESC LIMIT $4
        """,
        project_id,
        website_change.WORKFLOW_ID,
        SOURCE,
        MAX_EARLIER_RUNS,
    )
    found: dict[str, dict] = {}
    for row in runs:
        source, publication, merge = (
            delivery_decode(row["source"]),
            delivery_decode(row["publication"]),
            delivery_decode(row["merge"]),
        )
        number, url = publication.get("pull_request_number"), publication.get("external_url")
        if type(number) is not int or not url:
            continue
        state = "merged" if merge.get("status") == "merged" else None
        if state is None:
            state = "open"
            try:
                current = await integrations.github_pull_request_state(
                    project_id=project_id,
                    repository=publication.get("repository") or source["binding"]["repository"],
                    number=number,
                )
                state = (
                    "merged"
                    if current.get("merged")
                    else "closed"
                    if current.get("state") == "closed"
                    else "open"
                )
            except Exception as exc:
                logger.debug("PR state unavailable for run %s: %s", row["id"], type(exc).__name__)
        if state == "closed":
            continue
        for change in source.get("changes", []):
            found.setdefault(
                change["change_id"],
                {"run_id": str(row["id"]), "number": number, "url": url, "state": state},
            )
    return found


def delivery_decode(value):
    from tin_lite.content_programs import decoded

    return decoded(value or {})


# --- The plan: the latest audit, sorted into rows and one run ------------------------------


async def latest_audit(sources, project_id) -> dict:
    listed = (await sources.list_sources(project_id=project_id))["sources"]
    if not listed:
        raise ValueError(
            "Run an organic audit first: website.change fixes what the latest audit found."
        )
    return listed[0]


async def plan_changes(
    *, database, storage, integrations, project_id: UUID, inputs: dict, bind: bool = True
) -> dict[str, Any]:
    """Read the latest audit, record its fixable findings as change rows, and choose what the
    next run makes. Used by the preview and by admission; neither guesses a judgment call.
    """
    from tin_lite.technical_fix_sources import TechnicalFixError, TechnicalFixSources

    sources = TechnicalFixSources(
        database=database,
        storage=storage,
        integrations=integrations,
        supported_checks=contract.supported_checks(POLICY),
        batch=True,
    )
    latest = await latest_audit(sources, project_id)
    audit = await sources.inspect(project_id=project_id, audit_run_id=latest["id"])
    selections = list(audit["findings"])
    wanted = set(inputs.get("finding_ids") or [])
    if wanted:
        unknown = wanted - {row["finding"]["id"] for row in selections}
        if unknown:
            raise TechnicalFixError(
                "finding_not_found",
                f"Not in the latest audit: {', '.join(sorted(unknown))}.",
                status_code=404,
            )
        selections = [row for row in selections if row["finding"]["id"] in wanted]
    ids = [row["finding"]["id"] for row in selections]
    recorded = {
        row["change_id"]: row
        for row in await database.pool.fetch(
            "SELECT change_id, status, detail FROM website_changes "
            "WHERE project_id=$1 AND change_id = ANY($2::text[])",
            project_id,
            ids,
        )
    }

    def brief(row, reason, **extra):
        finding = row["finding"]
        return {
            "id": finding["id"],
            "check_id": finding["check_id"],
            "issue": finding.get("issue") or finding.get("title") or finding["check_id"],
            "reason": reason,
            **extra,
        }

    declined = [
        brief(row, "You declined this change in Tin; Tin won't propose it again.")
        for row in selections
        if (recorded.get(row["finding"]["id"]) or {}).get("status") == "declined"
    ]
    selections = [
        row
        for row in selections
        if (recorded.get(row["finding"]["id"]) or {}).get("status") != "declined"
    ]
    try:
        answers = repair_plan.parse_decisions(inputs.get("decisions"))
    except ValueError as exc:
        raise TechnicalFixError("invalid_decision", str(exc), status_code=422) from exc
    # A judgment call the founder approved with a row is answered for later runs too.
    for change_id, row in recorded.items():
        detail = delivery_decode(row["detail"])
        if row["status"] == "approved" and detail.get("decision") and change_id not in answers:
            answers[change_id] = detail["decision"]
    try:
        planned = repair_plan.build_plan(selections, answers)
    except ValueError as exc:
        raise TechnicalFixError("invalid_decision", str(exc), status_code=422) from exc

    flight = await rows_in_flight(database, integrations, project_id=project_id)
    entries, in_pull_request = [], []
    for entry in planned["repairs"]:
        carried = flight.get(entry["finding_id"])
        if carried:
            in_pull_request.append(
                {
                    "id": entry["finding_id"],
                    "check_id": entry["check_id"],
                    "issue": entry["issue"],
                    "reason": f"Already in PR #{carried['number']} ({carried['state']}).",
                    "pull_request": carried,
                }
            )
        else:
            entries.append(entry)

    # Bind the repository before recording anything, so a refused preview writes nothing.
    binding = await _bind(integrations, project_id, inputs) if bind and entries else None
    source = {
        "audit_run_id": audit["source"]["audit_run_id"],
        "audit_revision": audit["source"]["audit_revision"],
    }
    rows = [change_row(entry, source) for entry in entries]
    stored = {
        row["change_id"]: row
        for row in (
            await website_change.propose(database, project_id=project_id, rows=rows) if rows else []
        )
    }
    setting = await project_protected_paths(database.pool, project_id=project_id)
    roots = protected_paths(setting["paths"], inputs.get("protected_paths"))
    views, publish, review = [], [], []
    for entry, row in zip(entries, rows, strict=True):
        approval = await approval_for(database.pool, project_id=project_id, change=row.as_dict())
        hit = next((root for path in row.paths if (root := protected(path, roots))), None)
        recorded_row = stored[row.change_id]
        # The row as this plan makes it. A decided row keeps what the founder decided on, so
        # a different answer to its judgment call is not covered by that approval.
        view = {
            **row.as_dict(),
            "status": recorded_row["status"],
            "decided_by": recorded_row["decided_by"],
            "decided_at": recorded_row["decided_at"],
            "approved": approval is not None,
            "approval": approval,
            "protected": hit,
        }
        views.append(view)
        (publish if approval is not None and not hit else review).append((entry, row, view))
    take = publish or review
    if not take:
        mode, reason = None, None
    elif publish:
        mode, reason = "direct", DIRECT_REASON
    elif any(not view["approved"] for _, _, view in review):
        mode, reason = "pull_request", UNAPPROVED_REASON
    else:
        hit = next(view["protected"] for _, _, view in review if view["protected"])
        mode, reason = (
            "pull_request",
            f"They touch {hit}, a protected page, so the pull request waits for your review "
            "even though you approved them.",
        )
    waiting = [
        {
            "id": entry["finding_id"],
            "check_id": entry["check_id"],
            "issue": entry["issue"],
            "reason": "Waiting for your approval; the next run opens a pull request for it.",
        }
        for entry, _, _ in (review if publish else [])
    ]
    left_out = {
        **planned["left_out"],
        **({"declined": declined} if declined else {}),
        **({"in_pull_request": in_pull_request} if in_pull_request else {}),
        **({"waiting": waiting} if waiting else {}),
    }
    run_entries = [entry for entry, _, _ in take]
    result = {
        "source": {**audit["source"], "site_url": latest.get("site_url")},
        "target": audit["target"],
        "crawl_status": audit["crawl_status"],
        "plan": {
            "repairs": run_entries,
            "decisions_needed": planned["decisions_needed"],
            "left_out": left_out,
        },
        "decisions_needed": planned["decisions_needed"],
        "ask": repair_plan.ASK if planned["decisions_needed"] else None,
        "changes": views,
        "next_run": {
            "mode": mode,
            "reason": reason,
            "change_ids": [row.change_id for _, row, _ in take],
        },
        "protected_paths": roots,
        "protected_paths_revision": setting["revision"],
        "summary": {
            "fixable": len(planned["repairs"]),
            "next_run": len(run_entries),
            "decisions_needed": len(planned["decisions_needed"]),
            **{key: len(rows) for key, rows in left_out.items() if rows},
        },
        "caps": {
            "findings": repair_plan.MAX_FINDINGS,
            "files": repair_plan.MAX_FILES,
            "changed_lines": repair_plan.MAX_CHANGED_LINES,
        },
    }
    if binding is None or not run_entries:
        return {**result, "execution_available": False}
    return {
        **result,
        "repository_binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
        "repository_mapping": "member_asserted_not_verified",
        "live_verification": "not_performed",
        "execution_available": True,
    }


async def _bind(integrations, project_id, inputs):
    """The repository a run would write, after the member confirms it serves the site."""
    from tin_lite.integrations import IntegrationError
    from tin_lite.technical_fix_sources import TechnicalFixError

    if inputs.get("repository_serves_site") is not True:
        raise TechnicalFixError(
            "repository_confirmation_required",
            "Confirm that the selected repository serves the audited site.",
        )
    if integrations is None:
        raise TechnicalFixError(
            "github_unavailable", "GitHub preparation is unavailable.", status_code=503
        )
    try:
        return await integrations.github_repository_binding(
            project_id=project_id, expected_repository=inputs["expected_repository"]
        )
    except IntegrationError as exc:
        raise TechnicalFixError("github_binding_failed", str(exc)) from exc


def nothing_to_run(preview: dict) -> str:
    """Why a start has nothing to change, in the founder's words."""
    left = preview["plan"]["left_out"]
    if left.get("in_pull_request"):
        prs = sorted({row["pull_request"]["url"] for row in left["in_pull_request"]})
        return (
            "Every change Tin can make from the latest audit already sits in a website.change "
            f"pull request: {', '.join(prs)}. Merge or close it before starting another."
        )
    if preview["decisions_needed"]:
        return (
            "The changes left in the latest audit wait for judgment calls. Answer "
            "decisions_needed from preflight_website_change, then start again with decisions."
        )
    return "Nothing in the latest audit is left for website.change to fix."


async def select_source(*, database, storage, integrations, project_id, inputs) -> dict:
    """Pin the latest audit, the run's change rows with their approvals, the repository and
    the publish mode for one technical run."""
    preview = await plan_changes(
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
    )
    if not preview["next_run"]["change_ids"]:
        raise ValueError(nothing_to_run(preview))
    taken = set(preview["next_run"]["change_ids"])
    binding = preview["repository_binding"]
    return {
        "source": SOURCE,
        "audit": preview["source"],
        "binding": binding,
        "changes": [
            {
                key: change[key]
                for key in (
                    "change_id",
                    "source",
                    "kind",
                    "title",
                    "paths",
                    "content_revision",
                    "content_sha256",
                    "detail",
                    "approval",
                    "protected",
                )
            }
            for change in preview["changes"]
            if change["change_id"] in taken
        ],
        "protected_paths": preview["protected_paths"],
        "protected_paths_revision": preview["protected_paths_revision"],
        "publish": {
            "mode": preview["next_run"]["mode"],
            "reason": preview["next_run"]["reason"],
        },
        # What site-fix-v5's preparation reads: the plan narrowed to this run's rows.
        "selection": {
            "source": preview["source"],
            "target": preview["target"],
            "crawl_status": preview["crawl_status"],
            "plan": {**preview["plan"], "decisions_needed": []},
            "decisions_needed": [],
            "repository_binding": binding,
            "repository_mapping": preview["repository_mapping"],
            "live_verification": "not_performed",
            "execution_available": True,
        },
    }


async def guard_source(conn, *, project_id, inputs, source) -> None:
    """Under create_run's project lock: the pinned approvals still stand, and no other
    technical run of this project is still working."""
    if inputs.get("source") != SOURCE or source.get("source") != SOURCE:
        raise ValueError("The audit's changes are not the source this run was admitted for.")
    for change in source["changes"]:
        try:
            current = await approval_for(conn, project_id=project_id, change=change)
        except WebsiteChangeConflict:
            current = None
        if current != change.get("approval"):
            raise ValueError("A change's approval changed while starting. Start again.")
    busy = await conn.fetchval(
        "SELECT id FROM workflow_runs WHERE project_id=$1 AND workflow_id=$2 "
        "AND input->>'source'=$3 AND status = ANY($4::text[]) "
        "ORDER BY created_at DESC LIMIT 1",
        project_id,
        website_change.WORKFLOW_ID,
        SOURCE,
        list(ACTIVE),
    )
    if busy:
        raise ValueError(
            f"Website changes run {busy} is still working on the audit's changes; wait for "
            "it to finish."
        )


# --- Preparation, the sandbox and the report ----------------------------------------------


def report(prepared: dict, source: dict, *, reason=None, pull_request=None) -> bytes:
    """site-fix-v5's report, with the change rows and whether Tin merges them."""
    text = batch_rules.report(prepared, reason=reason, pull_request=pull_request).decode()
    lines = ["## Website changes", ""]
    if not reason:
        lines += [source["publish"]["reason"], ""]
    for change in source["changes"]:
        approval = change.get("approval")
        state = (
            f"approved by {approval['by']} on {approval['at'][:10]}"
            if approval
            else "waiting for your approval"
        )
        lines.append(f"- `{change['change_id']}` ({change['kind']}): {state}.")
    lines.append("")
    marker = "## How it was checked"
    head, _, tail = text.partition(marker)
    return (head + "\n".join(lines) + "\n" + marker + tail).encode()


def execution(database, storage, integrations):
    from tin_lite.technical_fix_execution import TechnicalFixExecution

    return TechnicalFixExecution(database=database, storage=storage, integrations=integrations)


async def prepare(*, database, storage, integrations, run) -> bool:
    """Re-read the live site for the pinned rows and name the files the diff can prove, with
    site-fix-v5's preparation. True when nothing is left to change (the run reports it)."""
    from tin_lite.run_reports import publish_run_report

    source = await delivery.saved_source(database, run.id)

    async def select():
        return {**source["selection"], "input_sha256": digest(run.input)}

    async def finish(run, prepared):
        if not prepared["reason"]:
            return False
        await publish_run_report(
            database=database,
            storage=storage,
            run_id=run.id,
            workflow_key=website_change.KEY,
            prefix="technical",
            path=f"website/changes/{run.id}.md",
            content=report(prepared, source, reason=prepared["reason"]),
            summary="Website changes checked. No change proposed.",
        )
        return True

    return await execution(database, storage, integrations).prepare_selection(
        run, select, finish=finish
    )


def workspace(source: dict) -> dict:
    """What the procedure reads about the rows beside site-fix-v5's plan."""
    return {
        "source": SOURCE,
        "change_ids": [change["change_id"] for change in source["changes"]],
        "publish": source["publish"],
        "protected_paths": source["protected_paths"],
    }


# --- After the pull request opens ---------------------------------------------------------


async def hold_reason(database, run, source, manifest, prepared) -> str | None:
    """Why Tin leaves a technical pull request open, or None when it may merge: the pinned
    mode, every row's approval as it stands now, and the protected pages (pinned, plus the
    project's setting as it stands now) against the rows' paths and every changed file."""
    publish = source.get("publish") or {}
    if publish.get("mode") != "direct":
        return publish.get("reason") or UNAPPROVED_REASON
    for change in source["changes"]:
        try:
            current = await approval_for(database.pool, project_id=run.project_id, change=change)
        except WebsiteChangeConflict:
            current = None
        if current != change.get("approval"):
            return (
                f"The approval of {change['change_id']} changed after Tin made the change, so "
                "it waits for your review."
            )
    setting = await project_protected_paths(database.pool, project_id=run.project_id)
    roots = protected_paths(source.get("protected_paths"), setting["paths"])
    paths = [path for change in source["changes"] for path in change["paths"]]
    for entry in (prepared.get("batch") or {}).get("repairs", []):
        paths += entry_paths(entry)
    hit = next((root for path in paths if (root := protected(path, roots))), None) or next(
        (root for item in manifest["files"] if (root := file_protected(item["path"], roots))),
        None,
    )
    if hit:
        return f"It touches {hit}, a protected page, so it waits for your review."
    return None


def live_recheck(database, integrations):
    from tin_lite.technical_fix_live import LiveRecheck

    return LiveRecheck(database=database, integrations=integrations)


async def publish(*, database, storage, integrations, run, sleep=None, clock=None):
    """After the run's pull request opens: merge it when every row in it was approved and it
    touches no protected page, once the required checks pass; else leave it open and say why.
    The outcome is recorded once. After Tin merges, the live check starts right away.
    """
    import asyncio
    from datetime import UTC, datetime

    from tin_lite.technical_fix_execution import prepared_result

    source = await delivery.saved_source(database, run.id)
    publication = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    published = (publication.result or {}) if publication else {}
    if (
        not publication
        or publication.status != "completed"
        or not published.get("external_url")
        or type(published.get("pull_request_number")) is not int
    ):
        return None
    key = delivery.merge_key(run.id)
    sleep = sleep or asyncio.sleep
    clock = clock or (lambda: datetime.now(UTC))
    async with database.effect_lock(key, delivery.MERGE_OPERATION) as (conn, receipt):
        if receipt and receipt.status == "completed":
            return receipt.result
        await database.start_effect(conn, execution_key=key, operation=delivery.MERGE_OPERATION)
        try:
            prepared = await prepared_result(database, run.id)
            manifest = await delivery.saved_manifest(database, storage, run)
            number = published["pull_request_number"]
            base = {"pull_request": published["external_url"], "number": number}
            hold = await hold_reason(database, run, source, manifest, prepared)
            if hold:
                result = {**base, "status": "left_open", "reason": hold}
            else:
                result = {
                    **base,
                    "merge_rule": "approved_changes",
                    **await delivery._merge_when_clean(
                        integrations=integrations,
                        run=run,
                        source=source,
                        manifest=manifest,
                        new_paths=(),
                        number=number,
                        branch=published.get("pull_request_branch"),
                        sleep=sleep,
                        clock=clock,
                        ready=delivery.REQUIRED_CHECKS_READY,
                        record_state=True,
                    ),
                }
            if result["status"] == "merged":
                event = "website_change_merged"
                summary = (
                    f"Tin merged PR #{number} into {manifest['default_branch']}."
                    if result.get("merged_by") == "tin"
                    else f"PR #{number} was merged into {manifest['default_branch']}."
                )
            else:
                event = "website_change_left_open"
                summary = f"PR #{number} is open. {result['reason']}"
            async with conn.transaction():
                await database.complete_effect(conn, execution_key=key, result=result)
                await database.add_activity(
                    conn=conn,
                    run_id=run.id,
                    event_type=event,
                    audience="product",
                    summary=summary[:240],
                    details={
                        "kind": "runs",
                        "external_url": result.get("url") or published["external_url"],
                        "external_label": f"View PR #{number}",
                        "change_ids": [change["change_id"] for change in source["changes"]],
                    },
                    dedupe_key=f"{key}:{event}:{run.id}",
                )
        except Exception:
            await database.fail_effect(
                conn,
                execution_key=key,
                error_message="Tin could not confirm the merge. The pull request is unchanged.",
            )
            raise
    if result["status"] == "merged":
        # The live check after merge: record the merge and read the site once now. It runs
        # again while someone reads the run, until the problems are gone or a fortnight passes.
        await live_recheck(database, integrations).view(run, check=True)
    return result
