"""Pin what a refresh run works from: the page, its current text, positioning and results.

Everything here is trusted Tin work done before the writing procedure starts. The choice is
saved once per run, so a retried preparation reads the same page and the same text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite import content_refresh as refresh
from tin_lite.content_delivery import delivery_key
from tin_lite.organic_audit import audit_hosts, audit_paths

LATEST_AUDIT_SQL = """
    SELECT run.id, run.canonical_commit_sha
    FROM workflow_runs AS run
    JOIN workflows AS workflow ON workflow.id = run.workflow_id
    WHERE run.project_id = $1 AND workflow.key = 'organic.audit'
      AND workflow.project_id IS NULL
      AND run.status = 'succeeded' AND run.canonical_commit_sha IS NOT NULL
    ORDER BY run.finished_at DESC NULLS LAST, run.created_at DESC
    LIMIT 1
"""
EARLIER_REFRESHES_SQL = """
    SELECT id, status, review_decision, created_at
    FROM workflow_runs
    WHERE project_id = $1 AND workflow_id = $2 AND id <> $3
    ORDER BY created_at DESC
    LIMIT 50
"""


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ContentRefreshSources:
    def __init__(self, *, database, storage, integrations=None, reader=None, clock=None):
        self.db, self.storage, self.integrations = database, storage, integrations
        # A factory for the audit's bounded site reader; tests pass a fixture.
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(UTC))

    async def saved(self, run_id) -> dict | None:
        receipt = await self.db.get_effect(f"{UUID(str(run_id))}:{refresh.PREPARATION}")
        return receipt.result if receipt and receipt.status == "completed" else None

    async def prepare(self, run) -> dict:
        key = f"{run.id}:{refresh.PREPARATION}"
        async with self.db.effect_lock(key, refresh.PREPARATION) as (conn, saved):
            if saved and saved.status == "completed":
                return saved.result
            context = await self._context(run)
            await self.db.start_effect(conn, execution_key=key, operation=refresh.PREPARATION)
            await self.db.complete_effect(conn, execution_key=key, result=context)
            return context

    async def _context(self, run) -> dict:
        project = await self.db.get_project(run.project_id)
        repo = await self.storage.get_repo(project.state_repo_id)
        revision = await self.storage.head_sha(repo, project.canonical_branch)
        now = self._clock()
        history = await self.history(run, now)
        results = await self.results(run, history, now)
        base = {
            "project_revision": revision,
            "results": results,
            "results_markdown": refresh.results_markdown(results),
            "positioning_sources": await self.positioning_sources(project, revision),
            "style_guide": refresh.STYLE_PATH
            if await self._exists(project, revision, refresh.STYLE_PATH)
            else None,
            "limits": refresh.LIMITS,
            "max_paragraphs": refresh.MAX_PARAGRAPHS,
        }
        audit = await self.latest_audit(project)
        if audit is None:
            return {
                **base,
                "page": None,
                "nothing_due": "No finished organic audit yet. Run the audit first; the "
                "refresh works from its search findings.",
            }
        blocked = {item["path"] for item in history if item["blocks"]}
        planned = await self.planned_refreshes(project, revision, now)
        selection = refresh.choose(audit["findings"], audit["evidence"], blocked, planned)
        if selection is None:
            waiting = sorted(item["path"] for item in history if item["blocks"])
            return {
                **base,
                "audit": audit["pin"],
                "page": None,
                "nothing_due": (
                    "The latest audit found no page with low click-through or searches just "
                    "below the top results that is due for a refresh."
                    + (
                        " Waiting for results or review: " + ", ".join(waiting[:6]) + "."
                        if waiting
                        else ""
                    )
                ),
            }
        current = await self.read_page(selection["url"], audit["evidence"])
        return {**base, "audit": audit["pin"], "page": selection, "current": current}

    async def planned_refreshes(self, project, revision, now: datetime) -> dict[str, set[str]]:
        """Pages a current organic.content_efficacy decision marked for a refresh."""
        from tin_lite import planned_url_changes as planned

        raw = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=revision, path=planned.EFFICACY_PATH
        )
        return planned.refresh_candidates(raw, now.date())

    async def latest_audit(self, project) -> dict | None:
        row = await self.db.pool.fetchrow(LATEST_AUDIT_SQL, project.id)
        if row is None:
            return None
        paths = audit_paths(str(row["id"]))
        try:
            findings = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=row["canonical_commit_sha"],
                    path=paths["findings.json"],
                )
            )
            evidence = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=row["canonical_commit_sha"],
                    path=paths["evidence.json"],
                )
            )
        except (LookupError, ValueError):
            return None
        if not isinstance(findings, dict) or not isinstance(evidence, dict):
            return None
        return {
            "findings": findings,
            "evidence": evidence,
            "pin": {
                "run_id": str(row["id"]),
                "revision": row["canonical_commit_sha"],
                "findings_path": paths["findings.json"],
            },
        }

    async def read_page(self, url: str, evidence: dict) -> dict:
        from tin_lite.organic_audit_fetch import SiteReader

        hosts = audit_hosts(evidence.get("scope") or {})
        reader = self._reader(hosts) if self._reader else SiteReader(hosts)
        async with reader:
            response = await reader.get(url, max_bytes=refresh.MAX_PAGE_BYTES)
        if response.get("status") != "observed" or response.get("status_code") != 200:
            raise ValueError(
                f"Tin could not read {url} (it returned "
                f"{response.get('status_code') or response.get('status')}). "
                "The refresh starts from the page's current text."
            )
        text = refresh.page_text(
            response["body"].decode(response.get("charset") or "utf-8", "replace")
        )
        if not text["title"]:
            raise ValueError(f"{url} has no title for Tin to start from.")
        return text

    async def positioning_sources(self, project, revision) -> list[str]:
        found = [
            path
            for path in refresh.POSITIONING_PATHS
            if await self._exists(project, revision, path)
        ]
        list_files = getattr(self.storage, "list_canonical_files_at", None)
        if list_files is not None:
            try:
                paths = await list_files(repo_id=project.state_repo_id, revision=revision)
            except (LookupError, ValueError, TypeError):
                paths = []
            if isinstance(paths, tuple):
                paths = paths[0]
            found.extend(
                sorted(
                    path
                    for path in paths
                    if path.startswith(refresh.CONTEXT_PREFIX) and path.endswith(".md")
                )[: refresh.MAX_CONTEXT_FILES]
            )
        return found

    async def _exists(self, project, revision, path) -> bool:
        raw = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=revision, path=path
        )
        return raw is not None

    async def history(self, run, now: datetime) -> list[dict[str, Any]]:
        """Earlier refreshes: their page, whether they went live, and whether they block it."""
        rows = await self.db.pool.fetch(
            EARLIER_REFRESHES_SQL, run.project_id, refresh.WORKFLOW_ID, run.id
        )
        items, checks = [], 0
        for row in rows:
            prepared = await self.saved(row["id"])
            page = (prepared or {}).get("page")
            if not page:
                continue
            live_at = await self._live_at(row["id"])
            pending = row["status"] in refresh.ACTIVE_STATES
            # Approved but not live yet: undelivered, or a pull request still open (or one
            # Tin could not check). Either way the page waits, so it is never refreshed and
            # paid for twice. An approval that never reaches the site stops blocking after
            # the same six weeks a live refresh waits.
            waiting = False
            if live_at is None and not pending and row["review_decision"] == "approved":
                delivered = await self.db.get_effect(delivery_key(row["id"]))
                result = (delivered.result or {}) if delivered else {}
                completed = delivered is not None and delivered.status == "completed"
                if completed and result.get("commit"):
                    live_at = _time(result.get("delivered_at"))
                elif completed and type(result.get("number")) is int:
                    if self.integrations is not None and checks < refresh.MAX_PR_CHECKS:
                        checks += 1
                        state = await self.integrations.github_pull_request_state(
                            project_id=run.project_id,
                            repository=result["repository"],
                            number=result["number"],
                        )
                        live_at = _time(state.get("merged_at")) if state.get("merged") else None
                        waiting = state.get("state") == "open" and live_at is None
                    else:
                        waiting = True
                else:
                    waiting = now - row["created_at"] < refresh.WAIT
                if live_at is not None:
                    await self._save_live(row["id"], live_at)
            items.append(
                {
                    "run_id": str(row["id"]),
                    "url": page["url"],
                    "path": page["path"],
                    "live_at": live_at.isoformat() if live_at else None,
                    "blocks": pending
                    or waiting
                    or (live_at is not None and now - live_at < refresh.WAIT),
                }
            )
        return items

    async def _live_at(self, run_id) -> datetime | None:
        receipt = await self.db.get_effect(refresh.run_key(run_id, "live"))
        if receipt and receipt.status == "completed":
            return _time((receipt.result or {}).get("live_at"))
        return None

    async def _save_live(self, run_id, live_at: datetime) -> None:
        key = refresh.run_key(run_id, "live")
        async with self.db.effect_lock(key, refresh.PREPARATION) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=refresh.PREPARATION)
            await self.db.complete_effect(
                conn, execution_key=key, result={"live_at": live_at.isoformat()}
            )

    async def results(self, run, history, now: datetime) -> list[dict]:
        """Before/after Search Console totals for refreshes with 28 days of results."""
        measured = []
        for item in history:
            live_at = _time(item["live_at"])
            if live_at is None or not refresh.measurable(live_at, now):
                continue
            key = refresh.run_key(item["run_id"], "result")
            receipt = await self.db.get_effect(key)
            if receipt and receipt.status == "completed":
                measured.append(receipt.result)
                continue
            if self.integrations is None:
                continue
            windows = refresh.result_windows(live_at)
            try:
                reads = {}
                for name, window in windows.items():
                    reads[name] = refresh.totals(
                        await self.integrations.search_console_analytics(
                            project_id=run.project_id,
                            start_date=window["start"],
                            end_date=window["end"],
                            dimensions=("page",),
                            row_limit=10,
                            dimension_filters=[
                                {
                                    "dimension": "page",
                                    "operator": "equals",
                                    "expression": item["url"],
                                }
                            ],
                            execution_key=f"{key}:{name}",
                            run_id=run.id,
                        )
                    )
            except Exception:  # noqa: S112 - a missing or changed property skips one result
                continue
            result = {
                "run_id": item["run_id"],
                "path": item["path"],
                "url": item["url"],
                "live_at": item["live_at"],
                "windows": windows,
                **reads,
            }
            async with self.db.effect_lock(key, refresh.PREPARATION) as (conn, saved):
                if not (saved and saved.status == "completed"):
                    await self.db.start_effect(
                        conn, execution_key=key, operation=refresh.PREPARATION
                    )
                    await self.db.complete_effect(conn, execution_key=key, result=result)
            measured.append(result)
        measured.sort(key=lambda item: item["live_at"], reverse=True)
        return measured
