"""After a site repair's pull request merges, check the live site for that one finding.

A PR is not a deployed repair. Once GitHub says it merged, Tin reads the same files or pages
it read before the change and records whether the problem is gone. It looks at most every ten
minutes while someone reads the run (MCP `get_run`, the run's live-check API), and stops a
fortnight after the merge. Only `site-fix-v4` runs have this record; older policies don't.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from tin_lite import technical_fix as contract
from tin_lite import technical_site_rules as site_rules
from tin_lite.technical_fix_execution import body_of

logger = logging.getLogger(__name__)

OPERATION = "technical_live_check"
CHECK_EVERY = timedelta(minutes=10)
DEPLOY_GRACE = timedelta(hours=24)
STOP_AFTER = timedelta(days=14)
CHECK_SECONDS = 30

MESSAGES = {
    "waiting_for_merge": "Tin checks the live site after this pull request merges.",
    "closed_unmerged": "The pull request was closed without merging. Nothing was checked.",
    "waiting_for_deploy": (
        "Merged. The live site still shows the problem; Tin checks again once it deploys."
    ),
    "fixed": "Merged, and the live site no longer shows this problem.",
    "still_broken": (
        "Merged more than a day ago, and the live site still shows this problem. Check that "
        "the change deployed."
    ),
}


def key(run_id) -> str:
    return f"technical:{run_id}:live"


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def present(record: dict, now: datetime) -> dict:
    if record.get("closed"):
        state = "closed_unmerged"
    elif not record.get("merged"):
        state = "waiting_for_merge"
    elif record.get("live") == "fixed":
        state = "fixed"
    else:
        merged_at = _parse(record.get("merged_at"))
        late = merged_at is not None and now - merged_at > DEPLOY_GRACE
        state = (
            "still_broken" if late and record.get("live") == "not_fixed" else "waiting_for_deploy"
        )
    return {
        "state": state,
        "message": MESSAGES[state],
        "pull_request": record.get("pull_request"),
        "merged_at": record.get("merged_at"),
        "checked_at": record.get("checked_at"),
    }


class LiveRecheck:
    def __init__(
        self,
        *,
        database,
        integrations,
        fetch=contract.fetch_page,
        fetch_file=contract.fetch_site_file,
    ):
        self.db, self.integrations, self.fetch, self.fetch_file = (
            database,
            integrations,
            fetch,
            fetch_file,
        )

    async def view(self, run, *, check=False, now=None):
        """One run's live check, or None. A lookup error never breaks the response."""
        if getattr(run, "executor", None) != "codex.procedure":
            return None
        try:
            return await self._view(run, check=check, now=now or datetime.now(UTC))
        except Exception as exc:
            logger.debug("live check unavailable for run %s: %s", run.id, type(exc).__name__)
            return None

    async def _view(self, run, *, check, now):
        prepared = await self.db.get_effect(f"technical:{run.id}:prepare")
        if not prepared or prepared.status != "completed":
            return None
        prepared = prepared.result
        if not (prepared or {}).get("site_fix") or prepared.get("reason"):
            return None
        receipt = await self.db.get_integration_call_receipt(f"{run.id}:procedure_pull_request")
        if not receipt or receipt.status != "completed" or not receipt.response_summary:
            return None
        summary = receipt.response_summary
        record = await self._load(run.id) or {
            "pull_request": {
                "repository": summary.get("repository"),
                "number": summary.get("number"),
                "url": summary.get("url"),
            }
        }
        if check and self._due(record, now):
            record = await self._check(run, prepared, record, now)
            await self._save(run.id, record)
        return present(record, now)

    def _due(self, record, now):
        if self.integrations is None or record.get("closed") or record.get("live") == "fixed":
            return False
        merged_at = _parse(record.get("merged_at"))
        if merged_at is not None and now - merged_at > STOP_AFTER:
            return False
        checked = _parse(record.get("checked_at"))
        return checked is None or now - checked >= CHECK_EVERY

    async def _check(self, run, prepared, record, now):
        record = dict(record)
        pull_request = record["pull_request"]
        stamp = now.isoformat()
        try:
            async with asyncio.timeout(CHECK_SECONDS):
                if not record.get("merged"):
                    state = await self.integrations.github_pull_request_state(
                        project_id=run.project_id,
                        repository=pull_request["repository"],
                        number=pull_request["number"],
                    )
                    if state["merged"]:
                        record.update(merged=True, merged_at=state["merged_at"] or stamp)
                    elif state["state"] == "closed":
                        record["closed"] = True
                if record.get("merged"):
                    record["live"] = await self._live(prepared)
        except Exception as exc:
            # A provider error or timeout keeps what Tin already knew; only the type is logged.
            logger.debug("live check failed for run %s: %s", run.id, type(exc).__name__)
        record["checked_at"] = stamp
        return record

    async def _live(self, prepared):
        """fixed when every file or page that needed the change no longer does."""
        fix, target = prepared["site_fix"], prepared["target"]
        rows = [row for row in fix["observations"] if row["needed"]]
        for row in rows:
            host = contract.verified_page_host(row["url"], target)
            try:
                if fix["target"] == "html":
                    current = await self.fetch(row["url"], host=host)
                else:
                    current = await self.fetch_file(row["url"], host=host, kind=fix["target"])
            except (ValueError, OSError, TimeoutError, UnicodeError):
                return "unknown"
            if site_rules.still_needed(
                fix["kind"],
                body_of(current),
                row["page_url"] or row["url"],
                fix["expected"],
                status_code=current["status_code"],
            ):
                return "not_fixed"
        return "fixed" if rows else "unknown"

    async def _load(self, run_id):
        row = await self.db.pool.fetchrow(
            "SELECT result FROM effect_receipts WHERE execution_key = $1 AND operation = $2",
            key(run_id),
            OPERATION,
        )
        if row is None:
            return None
        value = row["result"]
        return json.loads(value) if isinstance(value, str) else value

    async def _save(self, run_id, record):
        await self.db.pool.execute(
            "INSERT INTO effect_receipts (execution_key, operation, status, result) "
            "VALUES ($1, $2, 'completed', $3::jsonb) ON CONFLICT (execution_key) DO UPDATE "
            "SET result = EXCLUDED.result, updated_at = now() "
            "WHERE effect_receipts.operation = EXCLUDED.operation",
            key(run_id),
            OPERATION,
            json.dumps(record),
        )


def live_service(runtime):
    return LiveRecheck(
        database=runtime.database, integrations=getattr(runtime, "integrations", None)
    )
