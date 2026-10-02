"""website.change, phase 3: the URL changes other workflows planned.

Page decisions (`organic.content_efficacy`, `content/efficacy.md`) and the site architecture
plan (`organic.site_architecture`, its `redirects.json` block) plan redirects and noindex
changes but edit nothing. A website.change run with `source: planned` reads both from the
project files at the current revision (planned_url_changes) and records one `website_changes`
row per change: source `planned`, kind `redirect` or `noindex`, both ends as paths, the
change's stable `oa_` ID.

Each change is planned as a site-fix-v5 repair, so the rest is shared with the audit source
(website_change_audit): a redirect goes in the site's own redirect config the way a merge does
(`merge_redirect`), a noindex in the page's own metadata (`html_noindex`); the same caps,
patch checks, mode rules, merge rule and live check after merge apply. A change to or from a
protected page is still a row, but Tin's suggestion is `ask` and it always opens a pull
request. Deleting a page is never planned here: those are listed for the founder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite import planned_url_changes as planned
from tin_lite import technical_repair_plan as repair_plan
from tin_lite import website_change_audit as shared

SOURCE = "planned"
WORKFLOWS = {
    planned.EFFICACY_SOURCE: "Page decisions",
    planned.ARCHITECTURE_SOURCE: "The site architecture plan",
}
REDIRECT_HOW = (
    "Add a permanent (301 or 308) redirect from {old} to {new} in the framework's or host's "
    "redirect config, the way a merge is written, point internal links at {new} and drop {old} "
    "from the sitemap. Never delete or rewrite either page."
)
NOINDEX_HOW = "Add robots noindex in the page's own metadata. Never delete the page."


def current_date():
    """Today in UTC, which decides whether a plan is still fresh."""
    return datetime.now(UTC).date()


async def read_files(database, storage, project_id: UUID) -> tuple[dict[str, bytes | None], str]:
    """The two planner files at the project's current revision; absent files read as None."""
    project = await database.get_project(project_id)
    if project is None:
        raise LookupError("project not found")
    repo = await storage.get_repo(project.state_repo_id)
    revision = await storage.head_sha(repo, project.canonical_branch)
    files = {}
    for path in (planned.EFFICACY_PATH, planned.ARCHITECTURE_PATH):
        files[path] = (
            await storage.read_canonical_artifact_if_exists(
                repo_id=project.state_repo_id, commit_sha=revision, path=path
            )
            if revision
            else None
        )
    return files, revision


def entry_for(change: dict, host: str) -> dict:
    """One planned change as a site-fix-v5 repair entry."""
    old = f"https://{host}{change['from']}"
    workflow = WORKFLOWS.get(change["source"], "A plan")
    base = {
        "finding_id": change["id"],
        "check_id": f"planned.{change['kind']}",
        "fix": change["reason"],
        "priority": "quick_win",
        "urls": [old],
        "affected_count": 1,
        "planned": {
            "source": change["source"],
            "from": change["from"],
            "to": change["to"],
            "confirmed": change["confirmed"],
        },
    }
    if change["kind"] == "redirect":
        new = f"https://{host}{change['to']}"
        return {
            **base,
            "kind": "merge_redirect",
            "group": "redirects",
            "change": f"redirects {change['from']} to {change['to']}",
            "how": REDIRECT_HOW.format(old=change["from"], new=change["to"]),
            "live": "redirects_to",
            "issue": f"{workflow} proposes redirecting {change['from']} to {change['to']}",
            "redirects": [{"from": old, "to": new}],
        }
    return {
        **base,
        "kind": "html_noindex",
        "group": "indexing",
        "change": f"keeps {change['from']} out of search (noindex)",
        "how": NOINDEX_HOW,
        "live": "noindex",
        "issue": f"{workflow} proposes keeping {change['from']} out of search",
    }


async def plan_changes(
    *, database, storage, integrations, project_id: UUID, inputs: dict, bind: bool = True
) -> dict[str, Any]:
    """Record the planned URL changes as rows and choose what the next run makes."""
    from tin_lite.technical_fix_sources import TechnicalFixError, TechnicalFixSources
    from tin_lite.website_change import project_protected_paths, protected_paths

    # The site's address comes from the latest audit, as for the audit's own changes.
    latest = await shared.latest_audit(
        TechnicalFixSources(database=database, storage=storage, integrations=integrations),
        project_id,
    )
    url = latest.get("site_url") or ""
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("The latest audit names no site address, so Tin can't plan redirects.")
    files, revision = await read_files(database, storage, project_id)
    today = current_date()
    setting = await project_protected_paths(database.pool, project_id=project_id)
    roots = protected_paths(setting["paths"], inputs.get("protected_paths"))
    changes = planned.read_changes(files, today, protected_paths=roots)
    wanted = set(inputs.get("finding_ids") or [])
    if wanted:
        unknown = wanted - {change["id"] for change in changes}
        if unknown:
            raise TechnicalFixError(
                "finding_not_found",
                f"Not among the planned changes: {', '.join(sorted(unknown))}.",
                status_code=404,
            )
        changes = [change for change in changes if change["id"] in wanted]
    recorded = await shared.recorded_rows(database, project_id, [c["id"] for c in changes])
    entries = [entry_for(change, host) for change in changes]
    declined = [
        shared.brief(
            {"id": entry["finding_id"], "check_id": entry["check_id"], "issue": entry["issue"]},
            "You declined this change in Tin; Tin won't propose it again.",
        )
        for entry in entries
        if (recorded.get(entry["finding_id"]) or {}).get("status") == "declined"
    ]
    entries = [
        entry
        for entry in entries
        if (recorded.get(entry["finding_id"]) or {}).get("status") != "declined"
    ][: repair_plan.MAX_FINDINGS]
    deletions = [
        {
            "id": planned.finding_id(
                {"source": planned.EFFICACY_SOURCE, "kind": "delete", "from": row["from"]}
            ),
            "check_id": "planned.delete",
            "issue": f"Page decisions proposes removing {row['from']}",
            "reason": "Deleting a page stays with you: remove it, or redirect it in a later "
            "plan." + (f" ({row['reason']})" if row["reason"] else ""),
        }
        for row in planned.read_deletions(files, today)
    ]
    left_out = {key: rows for key, rows in (("declined", declined), ("manual", deletions)) if rows}
    found = [path for path, raw in files.items() if raw is not None]
    origin = {"project_revision": revision, "plan_files": found}
    return await shared.finish_plan(
        database=database,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
        bind=bind,
        source=SOURCE,
        planned={"repairs": entries, "left_out": left_out, "decisions_needed": []},
        origin=origin,
        complete=not wanted,
        result={
            "source": {**origin, "site_url": url},
            "target": {"url": url, "host": host, "site_hosts": [host]},
            "crawl_status": "not_applicable",
            **(
                {}
                if found
                else {
                    "note": "No page decisions or site architecture plan in this project yet; "
                    "nothing is planned."
                }
            ),
        },
    )
