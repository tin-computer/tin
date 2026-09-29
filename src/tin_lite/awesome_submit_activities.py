"""Awesome-list submission activities: the source report, one pinned plan of exact changes,
one founder approval, then one receipted fork-and-pull-request (or issue) per list from the
founder's own GitHub account. Only the run identifier crosses Temporal; every GitHub result
lives in an effect receipt.

Each list's submission receipt is keyed by project and list, not by run, so Tin never
submits the same project to the same list twice, even from a later run. A retried or
recovered attempt looks up what GitHub already holds before writing anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import awesome_submit
from tin_lite.awesome_submit import (
    KEY,
    PLAN_DOCS,
    RESULT_DOCS,
    SOURCE_KEYS,
    SOURCE_MAX_BYTES,
    PacketError,
    paths,
    source_path,
)
from tin_lite.github_account import SubmissionRefused
from tin_lite.integrations import (
    GITHUB_USER_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationError,
)
from tin_lite.organic_audit import digest
from tin_lite.organic_audit_publication import publish_artifacts

STEP_TOTAL = 5


class AwesomeSubmitActivities:
    def __init__(self, *, database, storage, integrations=None):
        self.db, self.storage, self.integrations = database, storage, integrations

    # ------------------------------------------------------------ receipts

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"{awesome_submit.PREFIX}:{UUID(str(run_id))}:{stage}"

    @staticmethod
    def submission_key(project_id, repository: str) -> str:
        return f"{awesome_submit.PREFIX}:{UUID(str(project_id))}:list:{repository.lower()}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            run is None
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "needs_input", "succeeded"}
        ):
            raise ApplicationError("The list submission is not active.", non_retryable=True)
        return run

    async def _result(self, run_id: str, stage: str):
        receipt = await self.db.get_effect(self.key(run_id, stage))
        return receipt.result if receipt and receipt.status == "completed" else None

    async def _save(self, run_id: str, stage: str, value: dict):
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=value)
        return value

    async def _refuse(self, run_id: str, text: str):
        await self._save(run_id, "failure", {"detail": text})
        raise ApplicationError(text, non_retryable=True)

    async def _progress(self, run_id, step, current, summary):
        await self.db.project_run_progress(
            run_id=UUID(str(run_id)),
            mode="steps",
            step=step,
            current=current,
            total=STEP_TOTAL,
            percent=int(current * 100 / STEP_TOTAL),
            summary=summary,
        )

    async def _accounts(self):
        if self.integrations is None or not self.integrations.is_configured(GITHUB_USER_PROVIDER):
            return None
        return self.integrations.github_account

    async def _source(self, run, inputs: dict) -> dict:
        """The awesome-lists report this run submits from, verified against its publication."""
        if inputs.get("source_run_id"):
            source_id = UUID(str(inputs["source_run_id"]))
        else:
            source_id = await self.db.pool.fetchval(
                """
                SELECT r.id FROM workflow_runs r JOIN workflows w ON w.id = r.workflow_id
                WHERE r.project_id = $1 AND w.key = ANY($2::text[]) AND r.status = 'succeeded'
                ORDER BY r.finished_at DESC NULLS LAST, r.created_at DESC
                LIMIT 1
                """,
                run.project_id,
                sorted(SOURCE_KEYS),
            )
            if source_id is None:
                raise PacketError("Run the awesome lists workflow first.")
        source = await self.db.get_run(source_id)
        definition = await self.db.get_workflow(source.workflow_id) if source else None
        if (
            source is None
            or source.project_id != run.project_id
            or definition is None
            or definition.key not in SOURCE_KEYS
            or source.status.value != "succeeded"
        ):
            raise PacketError("Choose a finished awesome lists run from this project.")
        receipt = await self.db.get_effect(f"{source.id}:procedure_canonical_commit")
        if (
            not receipt
            or receipt.status != "completed"
            or source.artifact_path != source_path(source.id)
            or not source.canonical_commit_sha
            or (receipt.result or {}).get("canonical_commit_sha") != source.canonical_commit_sha
            or (receipt.result or {}).get("artifact_path") != source_path(source.id)
        ):
            raise PacketError("The awesome lists report's saved copy could not be verified.")
        project = await self.db.get_project(run.project_id)
        raw = await self.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=source.canonical_commit_sha,
            path=source_path(source.id),
        )
        if len(raw) > SOURCE_MAX_BYTES:
            raise PacketError("The awesome lists report is larger than Tin reads.")
        return {
            "run_id": str(source.id),
            "revision": source.canonical_commit_sha,
            "sha256": digest(raw.decode("utf-8", errors="strict")),
            "report": raw.decode("utf-8"),
        }

    async def _publish(self, run_id, stage, documents, names, project) -> dict:
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self._active(run_id, conn=conn)
            encoded = {names[n]: c.encode() for n, c in documents.items()}
            limits = {**PLAN_DOCS, **RESULT_DOCS}
            await self.db.start_effect(conn, execution_key=key, operation=KEY)

            async def save_intent(intent):
                await self.db.save_publication_intent(conn, execution_key=key, intent=intent)

            async def validate_active():
                await self._active(run_id, conn=conn)

            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents=encoded,
                    paths=names,
                    limits={n: limits[n] for n in names},
                    message=f"{KEY} {run_id} [{key}]",
                    intent=(existing.result or {}).get("publication") if existing else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                )
            result = {"canonical_commit_sha": revision, "paths": names}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    # ------------------------------------------------------------ activities

    @activity.defn(name="awesome_submit_prepare")
    async def prepare(self, run_id: str) -> None:
        """Pin the source report, the chosen packets and the account they are sent from."""
        if await self._result(run_id, "scope"):
            await self.db.mark_run_running(UUID(run_id))
            return
        run = await self._active(run_id)
        inputs = dict(run.input or {})
        try:
            awesome_submit.check_inputs(inputs)
        except (ValueError, TypeError) as exc:
            await self._refuse(run_id, str(exc) or "Invalid inputs.")
        accounts = await self._accounts()
        if accounts is None:
            await self._refuse(run_id, "This Tin deployment has no GitHub OAuth App configured.")
        try:
            connection = await accounts.connection(run.project_id)
            source = await self._source(run, inputs)
            packets = awesome_submit.parse_packets(source.pop("report"))
            chosen = awesome_submit.select(packets, inputs.get("lists") or [])
        except (PacketError, IntegrationError) as exc:
            await self._refuse(run_id, str(exc))
        if not chosen:
            await self._refuse(
                run_id,
                "The awesome lists report has no submission Tin can send"
                + (
                    ": " + "; ".join(f"{r['list']}: {r['reason']}" for r in packets["rejected"])
                    if packets["rejected"]
                    else "."
                ),
            )
        await self._save(
            run_id,
            "scope",
            {
                "project_id": str(run.project_id),
                "source": source,
                "product": packets["product"],
                "submissions": chosen,
                "rejected": packets["rejected"],
                "account": {
                    "connection_id": str(connection.id),
                    "external_account_id": connection.external_account_id,
                    "login": connection.configuration.get("login"),
                },
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        await self.db.mark_run_running(run.id)

    @activity.defn(name="awesome_submit_draft")
    async def draft(self, run_id: str) -> None:
        """Read each list's current file and place the entry with code; pin the exact plan."""
        if await self._result(run_id, "draft"):
            return
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        await self._progress(run_id, "draft", 1, "Reading each list's current file")
        accounts = await self._accounts()
        if accounts is None:
            await self._refuse(run_id, "The GitHub OAuth App is no longer configured.")
        try:
            connection = await accounts.connection(run.project_id)
        except IntegrationError as exc:
            await self._refuse(run_id, str(exc))
        product = scope["product"]
        changes, skipped = [], list(scope.get("rejected") or [])

        async def earlier(name: str) -> str | None:
            receipt = await self.db.get_effect(self.submission_key(run.project_id, name))
            if receipt is None:
                return None
            return (
                "already submitted from this project"
                if receipt.status == "completed"
                else "an earlier submission is unconfirmed; check GitHub first"
            )

        for submission in scope["submissions"]:
            repository = submission["list"]
            if reason := await earlier(repository):
                skipped.append({"list": repository, "reason": reason})
                continue
            body = awesome_submit.with_disclosure(submission["body"], product["name"])
            try:
                if submission["method"] == "issue":
                    listing = await accounts.repository(connection, repository)
                    if not listing["has_issues"]:
                        raise SubmissionRefused("The list does not take issues.")
                    # A renamed list answers with its current name; receipts use that name.
                    if reason := await earlier(listing["full_name"]):
                        raise SubmissionRefused(reason)
                    changes.append({**submission, "list": listing["full_name"], "body": body})
                    continue
                current = await accounts.list_file(connection, repository, submission["path"])
                if reason := await earlier(current["full_name"]):
                    raise SubmissionRefused(reason)
                placed = awesome_submit.place(
                    current["content"],
                    section=submission["section"],
                    entry=submission["entry"],
                    order=submission["order"],
                    links=product["links"],
                )
            except (PacketError, SubmissionRefused) as exc:
                skipped.append({"list": repository, "reason": str(exc)})
                continue
            except IntegrationAuthorizationError as exc:
                await self._refuse(run_id, str(exc))
            changes.append(
                {
                    **submission,
                    "list": current["full_name"],
                    "body": body,
                    "entry": placed["entry"],
                    "default_branch": current["default_branch"],
                    "base_commit": current["base_commit"],
                    "base_blob": current["blob_sha"],
                    "line": placed["line"],
                    "context": placed["context"],
                    "content_sha256": digest(placed["content"]),
                }
            )
            if activity.in_activity():
                activity.heartbeat({"list": repository})
        login = scope["account"]["login"]
        documents = {
            "PLAN.md": awesome_submit.render_plan(
                account=login, product=product["name"], changes=changes, skipped=skipped
            )
        }
        project = await self.db.get_project(run.project_id)
        publication = (
            await self._publish(run_id, "plan", documents, paths(run_id, PLAN_DOCS), project)
            if changes
            else None
        )
        await self._save(
            run_id,
            "draft",
            {
                "changes": changes,
                "skipped": skipped,
                "plan_sha256": awesome_submit.plan_digest(changes),
                "publication": publication,
            },
        )
        if not changes:
            await self._refuse(
                run_id,
                "No list could take the submission right now: "
                + "; ".join(f"{s['list']}: {s['reason']}" for s in skipped),
            )

    @activity.defn(name="awesome_submit_request_review")
    async def request_review(self, run_id: str) -> None:
        draft = await self._result(run_id, "draft")
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        publication = draft["publication"]
        path = publication["paths"]["PLAN.md"]
        count = len(draft["changes"])
        required = await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=publication["canonical_commit_sha"],
            artifact_ref=(
                f"code.storage://{project.state_repo_id}@"
                f"{publication['canonical_commit_sha']}/{path}"
            ),
            artifact_path=path,
            summary=(
                f"Approve and send {count} awesome list submission{'s' if count != 1 else ''} "
                f"from @{scope['account']['login']}: "
                + ", ".join(c["list"] for c in draft["changes"])
                + ". Nothing is sent until you approve."
            ),
        )
        if not required:
            raise ApplicationError(
                "List submissions must require explicit approval.", non_retryable=True
            )
        await self._progress(run_id, "review", 2, "Waiting for your approval")

    @activity.defn(name="awesome_submit_record_approval")
    async def record_approval(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self.db.record_human_review(
            run_id=run.id, decision="approved", summary="You approved the list submissions."
        )

    @activity.defn(name="awesome_submit_apply")
    async def apply(self, run_id: str) -> None:
        """Send each approved change once. The placement is recomputed on the list's current
        file, but only the approved line in the approved section is ever added."""
        if await self._result(run_id, "applied"):
            return
        draft = await self._result(run_id, "draft")
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        if run.review_decision != "approved":
            raise ApplicationError("The submissions were not approved.", non_retryable=True)
        await self._progress(run_id, "apply", 3, "Opening the approved submissions on GitHub")
        accounts = await self._accounts()
        if accounts is None:
            await self._refuse(run_id, "The GitHub OAuth App is no longer configured.")
        try:
            connection = await accounts.connection(run.project_id)
        except IntegrationError as exc:
            await self._refuse(run_id, f"{exc}; nothing was sent.")
        if connection.external_account_id != scope["account"]["external_account_id"]:
            await self._refuse(
                run_id,
                "A different GitHub account is connected than the one you approved; "
                "nothing was sent.",
            )
        results = []
        for change in draft["changes"]:
            try:
                results.append(await self._submit(run, scope, change, accounts, connection))
            except IntegrationAuthorizationError as exc:
                await self._refuse(
                    run_id,
                    f"{exc}. {len(results)} submission(s) were sent before GitHub stopped "
                    "accepting Tin's access. Reconnect, then run it again; lists already "
                    "sent are skipped.",
                )
            if activity.in_activity():
                activity.heartbeat({"list": change["list"]})
        await self._save(run_id, "applied", {"results": results})

    async def _submit(self, run, scope, change, accounts, connection) -> dict:
        key = self.submission_key(run.project_id, change["list"])
        base = {"list": change["list"], "method": change["method"]}
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                saved = existing.result or {}
                if saved.get("run_id") == str(run.id):
                    return saved
                return {**base, "status": "skipped", "reason": "already submitted earlier"}
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.save_effect_progress(
                conn,
                execution_key=key,
                result={
                    **base,
                    "run_id": str(run.id),
                    "attempted_at": datetime.now(UTC).isoformat(),
                },
            )
            try:
                if change["method"] == "issue":
                    sent = await accounts.open_issue(
                        connection,
                        upstream=change["list"],
                        title=change["title"],
                        body=change["body"],
                    )
                else:
                    current = await accounts.list_file(connection, change["list"], change["path"])
                    placed = awesome_submit.place(
                        current["content"],
                        section=change["section"],
                        entry=change["entry"],
                        order=change["order"],
                        links=scope["product"]["links"],
                    )
                    sent = await accounts.submit_pull_request(
                        connection,
                        execution_key=key,
                        upstream=change["list"],
                        default_branch=current["default_branch"],
                        base_commit=current["base_commit"],
                        path=change["path"],
                        content=placed["content"],
                        title=change["title"],
                        body=change["body"],
                        commit_message=change["commit_message"],
                    )
                result = {**base, "run_id": str(run.id), "status": "completed", **sent}
            except (PacketError, SubmissionRefused) as exc:
                result = {**base, "run_id": str(run.id), "status": "refused", "reason": str(exc)}
            # Anything else (GitHub unreachable, rate limited) leaves the receipt started, so the
            # activity retry looks up the branch's pull request or the issue before writing.
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    @activity.defn(name="awesome_submit_publish")
    async def publish(self, run_id: str) -> None:
        applied = await self._result(run_id, "applied")
        if applied is None:
            raise ApplicationError("There is nothing to publish.", non_retryable=True)
        draft = await self._result(run_id, "draft")
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        await self._progress(run_id, "publish", 4, "Saving the result to your project files")
        names = paths(run_id, RESULT_DOCS)
        documents = {
            "RESULT.md": awesome_submit.render_result(
                account=scope["account"]["login"],
                product=scope["product"]["name"],
                results=applied["results"],
                skipped=draft["skipped"],
            )
        }
        publication = await self._publish(run_id, "publish", documents, names, project)
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_awesome_submit_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=publication["canonical_commit_sha"],
                artifact_path=names["RESULT.md"],
                artifact_ref=(
                    f"code.storage://{project.state_repo_id}@"
                    f"{publication['canonical_commit_sha']}/{names['RESULT.md']}"
                ),
                summary=awesome_submit.summary_line(applied["results"]),
            )

    @activity.defn(name="awesome_submit_failure")
    async def failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.executor != KEY:
            return
        failure = await self._result(run_id, "failure") or {}
        await self.db.project_failure(
            run_id=run.id,
            error_message=failure.get("detail")
            or "The list submission stopped before finishing. Check the result on GitHub before "
            "trying again; Tin never sends the same list twice.",
        )
