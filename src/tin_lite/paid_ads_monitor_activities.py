"""Google Ads monitor activities: read the live campaign, apply the bounded automatic changes,
save proposals for the founder and publish the day's report. The run never waits on anyone;
every provider call is receipted at zero cost and an unconfirmed change is reconciled from the
account on the next run rather than sent again."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import paid_ads_launch, paid_ads_monitor
from tin_lite.google_ads_requests import (
    QUERIES,
    negatives_body,
    pause_ads_body,
    pause_keywords_body,
)
from tin_lite.integrations import ADS_PROVIDER, GoogleAdsCallError, IntegrationError
from tin_lite.model_providers import (
    MessageRole,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ReasoningEffort,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.organic_audit import digest
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.paid_ads import UnusableModelResult
from tin_lite.paid_ads_monitor import KEY, LIMITS, POLICY, paths
from tin_lite.paid_ads_sources import pinned_bundle
from tin_lite.usage_capture import external_usage_scope

STEP_TOTAL = 5
# The provider's own wait for one model step, just inside the step's 240-second budget. Without
# it the client stops at its 90-second default and a slow but valid answer is lost.
MODEL_TIMEOUT_SECONDS = 225
READS = (
    ("campaign_7d", "campaign_health", 7),
    ("campaign_14d", "campaign_health", 14),
    ("campaign_30d", "campaign_health", 30),
    ("search_terms", "search_terms", 7),
    ("keywords", "keywords", 30),
    ("ads", "ad_policy", None),
    ("assets", "asset_policy", None),
    ("conversion_actions", "conversion_actions", 30),
    ("subscriptions", "subscriptions", None),
)


def _base(step: str) -> str:
    return step.split(":")[0]


def _rows(result: dict) -> list[dict]:
    rows = ((result or {}).get("value") or {}).get("rows") or []
    return [row for row in rows if isinstance(row, dict)]


class PaidAdsMonitorActivities:
    def __init__(self, *, database, storage, settings, router=None, integrations=None):
        self.db, self.storage, self.settings = database, storage, settings
        self.router, self.integrations = router, integrations

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"{paid_ads_monitor.PREFIX}:{UUID(str(run_id))}:{stage}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            run is None
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "succeeded"}
        ):
            raise ApplicationError("The Google Ads check is not active.", non_retryable=True)
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

    async def _reserve(self, run_id: str, stage: str, amount: str) -> bool:
        scope = await self._result(run_id, "scope")
        key = self.key(run_id, "budget")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            ledger = dict(existing.result or {}) if existing else {}
            total = sum((Decimal(value) for value in ledger.values()), Decimal(0))
            if stage in ledger:
                return total <= Decimal(scope["max_cost_usd"])
            if total + Decimal(amount) > Decimal(scope["max_cost_usd"]):
                return False
            ledger[stage] = amount
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.save_effect_progress(conn, execution_key=key, result=ledger)
            return True

    def _configured(self, needs: str) -> bool:
        if needs == "model":
            return self.router is not None and bool(getattr(self.settings, "luna_api_key", None))
        if needs == "google_ads":
            return self.integrations is not None and self.integrations.is_configured(ADS_PROVIDER)
        return True

    async def _paid(self, run_id: str, stage: str, request: dict, amount: str, call, *, needs):
        key, fingerprint = self.key(run_id, stage), digest(request)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            saved = (existing.result or {}) if existing else {}
            if saved and saved.get("request_sha256") != fingerprint:
                raise ApplicationError(
                    "A request changed after recording its intent.", non_retryable=True
                )
            if existing and existing.status == "completed":
                return saved
            run = await self._active(run_id, conn=conn)
            if run.status.value == "succeeded":
                raise ApplicationError("Completed runs cannot start new work.", non_retryable=True)
            if not self._configured(needs):
                raise ApplicationError(
                    "The monitor's providers are no longer configured.", non_retryable=True
                )
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            if saved.get("attempted_at"):
                result = {"status": "unknown", "reason": "unconfirmed_previous_request"}
            elif not await self._reserve(run_id, stage, amount):
                result = {"status": "unavailable", "reason": "spending_limit"}
            else:
                saved = {
                    "request_sha256": fingerprint,
                    "reservation_usd": amount,
                    "attempted_at": datetime.now(UTC).isoformat(),
                }
                await self.db.save_effect_progress(conn, execution_key=key, result=saved)
                await self._active(run_id, conn=conn)
                try:
                    with (
                        model_usage_scope(run_id=run_id, step=f"{KEY}:{stage}", conn=conn),
                        external_usage_scope(self.db, conn, run_id, stage, maximum_usd=amount),
                    ):
                        value = await call()
                    result = {"status": "completed", "value": value}
                except Exception as exc:
                    from tin_lite.billing_contracts import BillingError

                    result = (
                        {"status": "unavailable", "reason": "spending_limit"}
                        if isinstance(exc, BillingError)
                        else {"status": "unknown", "reason": "provider_result_unavailable"}
                    )
            result = {"request_sha256": fingerprint, **saved, **result}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            if activity.in_activity():
                activity.heartbeat({"stage": stage})
            return result

    async def _model(self, run_id: str, step: str, system: str, user: str, schema, max_out, effort):
        if len(user.encode()) > POLICY["max_model_input_bytes"]:
            raise ApplicationError("A model step's input exceeds its bound.", non_retryable=True)
        request = ModelRequest(
            system=system,
            messages=(ModelMessage(role=MessageRole.USER, content=user),),
            output_schema=schema,
            output_schema_name=paid_ads_monitor.schema_name(step),
            max_output_tokens=max_out,
            reasoning_effort=ReasoningEffort(effort),
        )
        route = paid_ads_monitor.route_for(step).key

        async def call():
            try:
                async with asyncio.timeout(240):
                    result = await self.router.generate(
                        route, request, timeout_seconds=MODEL_TIMEOUT_SECONDS
                    )
            except ModelProviderError as exc:
                return {"unusable": (str(exc)[:160] or "unusable model result")}
            parsed = result.parsed
            if not isinstance(parsed, dict):
                return {"unusable": "model result is not a JSON object"}
            return {
                "data": parsed,
                "usage": asdict(result.usage),
                "request_id": result.request_id,
                "model": result.model,
                "provider": result.provider.value,
            }

        amount = POLICY[f"{_base(step)}_reservation_usd"]
        result = await self._paid(
            run_id,
            f"model:{step}",
            {"route": route, **asdict(request)},
            amount,
            call,
            needs="model",
        )
        if result["status"] != "completed":
            await self._save(run_id, "failure", {"code": "model_unavailable", "stage": step})
            raise ApplicationError(
                "A model result was unavailable; no replacement was purchased.",
                non_retryable=True,
            )
        if result["value"].get("unusable"):
            raise UnusableModelResult(result["value"]["unusable"])
        return result["value"]["data"]

    async def _generate(self, run_id, step, system, user, schema, max_out):
        """One step with one retry, then one repair-free refusal; the caller validates."""
        try:
            return await self._model(run_id, step, system, user, schema, max_out, "medium")
        except UnusableModelResult:
            return await self._model(
                run_id, f"{step}:retry", system, user, schema, max_out, "medium"
            )

    async def _ads(self, run_id: str, stage: str, kind: str, request: dict) -> dict:
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)

        async def call():
            try:
                result = await self.integrations.google_ads_call(
                    project_id=run.project_id,
                    kind=kind,
                    request=request,
                    execution_key=self.key(run_id, f"{stage}:call"),
                    run_id=run.id,
                    expected_customer_id=scope["customer_id"],
                )
            except GoogleAdsCallError as exc:
                return {"error": exc.code, "observed_at": datetime.now(UTC).isoformat()}
            return {
                "rows": result.get("rows"),
                "results": result.get("results"),
                "provider_request_id": result.get("provider_request_id"),
                "observed_at": datetime.now(UTC).isoformat(),
            }

        result = await self._paid(
            run_id,
            stage,
            {"kind": kind, **request},
            POLICY["google_ads_reservation_usd"],
            call,
            needs="google_ads",
        )
        if result["status"] != "completed":
            return result
        return {**result, "error": (result.get("value") or {}).get("error")}

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

    # ------------------------------------------------------------ activities

    @activity.defn(name="paid_ads_monitor_prepare")
    async def prepare(self, run_id: str) -> None:
        if await self._result(run_id, "scope"):
            await self.db.mark_run_running(UUID(run_id))
            return
        run = await self._active(run_id)
        inputs = dict(run.input or {})
        try:
            paid_ads_monitor.check_inputs(inputs)
        except (ValueError, TypeError, KeyError) as exc:
            raise ApplicationError(str(exc) or "Invalid inputs.", non_retryable=True) from None
        if not self._configured("model") or not self._configured("google_ads"):
            raise ApplicationError(
                "Configure Tin's Google Ads manager account and the native model first.",
                non_retryable=True,
            )
        if not run.definition_commit_sha:
            raise ApplicationError("The monitor requires a pinned definition.", non_retryable=True)
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path=f"workflows/{KEY}.json",
            )
        )
        if (
            definition.get("paid_ads_monitor_policy") != POLICY
            or definition.get("paid_ads_monitor_routes") != paid_ads_monitor.route_definitions()
            or definition.get("paid_ads_monitor_contract_sha256")
            != paid_ads_monitor.contract_digest()
        ):
            raise ApplicationError(
                "This worker does not serve the selected monitor contract.", non_retryable=True
            )
        project = await self.db.get_project(run.project_id)
        launch_id = inputs["launch_run_id"]
        try:
            source, documents = await pinned_bundle(
                database=self.db,
                storage=self.storage,
                project=project,
                run_id=launch_id,
                executor=paid_ads_launch.KEY,
                prefix=paid_ads_launch.PREFIX,
                paths=lambda rid: paid_ads_launch.paths(rid, paid_ads_launch.RESULT_DOCS),
                limits=paid_ads_launch.RESULT_DOCS,
            )
            campaign = json.loads(documents["campaign.json"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                str(exc) or "Choose a successful Google Ads launch.", non_retryable=True
            ) from None
        row = await self.db.get_paid_ads_campaign(UUID(str(launch_id)))
        if row is None or row["status"] != "live" or not row.get("external_campaign_id"):
            raise ApplicationError(
                "That launch did not leave a live campaign, so there is nothing to check.",
                non_retryable=True,
            )
        try:
            await self.integrations.google_ads_link_status(project_id=run.project_id)
            customer = await self.integrations.google_ads_account(project_id=run.project_id)
        except IntegrationError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from None
        if customer != row["customer_id"]:
            raise ApplicationError(
                "The linked Google Ads account is not the one the campaign lives in.",
                non_retryable=True,
            )
        scope = {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "definition_sha": run.definition_commit_sha,
            "inputs": {k: v for k, v in inputs.items() if k != "project_id"},
            "launch": source,
            "customer_id": customer,
            "campaign_id": str(row["external_campaign_id"]),
            "campaign": {
                **campaign,
                "name": campaign.get("campaign_name"),
                "enabled_at": (row["enabled_at"].isoformat() if row.get("enabled_at") else None)
                or campaign.get("enabled_at"),
                "shared_set": (campaign.get("resources") or {}).get("shared_set"),
            },
            "started_at": run.created_at.isoformat(),
            "today": datetime.now(UTC).date().isoformat(),
            "max_cost_usd": str(Decimal(str(inputs.get("max_cost_usd", 2)))),
            "auto_negatives_cap": int(
                inputs.get("auto_negatives_per_run", POLICY["max_negatives_default"])
            ),
            "policy_version": POLICY["version"],
        }
        await self._save(run_id, "scope", scope)
        await self.db.mark_run_running(run.id)

    @activity.defn(name="paid_ads_monitor_read")
    async def read(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        if await self._result(run_id, "reads"):
            return
        await self._progress(run_id, "read", 1, "Reading the campaign in Google Ads")
        campaign_id = scope["campaign_id"]
        raw: dict = {}
        unavailable = []
        for name, query, days in READS:
            builder = QUERIES[query]
            if query in {"campaign_health", "search_terms", "keywords"}:
                gaql = builder(campaign_id, days)
            elif query in {"ad_policy", "asset_policy"}:
                gaql = builder(campaign_id)
            elif query == "conversion_actions":
                gaql = builder(days)
            else:
                gaql = builder()
            result = await self._ads(run_id, f"read:{name}", "search", {"query": gaql})
            if result.get("status") != "completed" or result.get("error"):
                unavailable.append(name)
                raw[name] = []
            else:
                raw[name] = _rows(result)
            if activity.in_activity():
                activity.heartbeat({"stage": name})
        if "campaign_7d" in unavailable:
            raise ApplicationError(
                "Google Ads did not answer the campaign read. Nothing was changed.",
                non_retryable=False,
            )
        normalized = paid_ads_monitor.normalize_reads(raw)
        await self._save(run_id, "reads", {"normalized": normalized, "unavailable": unavailable})

    @activity.defn(name="paid_ads_monitor_decide")
    async def decide(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        if await self._result(run_id, "decision"):
            return
        reads = (await self._result(run_id, "reads"))["normalized"]
        await self._progress(run_id, "decide", 2, "Deciding what to change and what to propose")
        terms = [item["term"] for item in reads.get("search_terms") or []]
        labels: dict = {}
        if terms:
            system, user = paid_ads_monitor.classify_prompt(
                str((scope["campaign"].get("profile") or {}).get("summary") or "")
                or scope["campaign"].get("campaign_name")
                or "",
                reads["search_terms"],
            )
            try:
                result = await self._generate(
                    run_id,
                    "classify",
                    system,
                    user,
                    paid_ads_monitor.classify_schema(terms),
                    paid_ads_monitor.MAX_OUT["classify"],
                )
                labels = paid_ads_monitor.validate_labels(result, terms)
            except UnusableModelResult:
                labels = {}
        decision = paid_ads_monitor.decide(
            campaign=scope["campaign"],
            reads=reads,
            labels=labels,
            today=datetime.fromisoformat(scope["today"]).date(),
            auto_negatives_cap=scope["auto_negatives_cap"],
        )
        await self._save(run_id, "decision", {"decision": decision, "labels": labels})

    @activity.defn(name="paid_ads_monitor_apply")
    async def apply(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        if await self._result(run_id, "applied"):
            return
        decision = (await self._result(run_id, "decision"))["decision"]
        await self._progress(run_id, "apply", 3, "Applying the automatic changes")
        auto = decision.get("auto") or {}
        applied: dict = {"negatives": [], "pause_ads": [], "pause_keywords": []}

        async def run_step(stage, items, build):
            if not items:
                return []
            segment, body = build(items)
            result = await self._ads(
                run_id, f"apply:{stage}", "mutate_resource", {"segment": segment, "body": body}
            )
            status = (
                "applied"
                if result.get("status") == "completed" and not result.get("error")
                else "failed"
                if result.get("error")
                else "unknown"
            )
            return [{**item, "status": status} for item in items]

        shared_set = scope["campaign"].get("shared_set")
        if shared_set:
            applied["negatives"] = await run_step(
                "negatives",
                auto.get("negatives") or [],
                lambda items: negatives_body(
                    shared_set, [{"text": i["text"], "match_type": i["match_type"]} for i in items]
                ),
            )
        applied["pause_ads"] = await run_step(
            "pause_ads",
            auto.get("pause_ads") or [],
            lambda items: pause_ads_body([i["resource_name"] for i in items]),
        )
        applied["pause_keywords"] = await run_step(
            "pause_keywords",
            auto.get("pause_keywords") or [],
            lambda items: pause_keywords_body([i["resource_name"] for i in items]),
        )
        await self._save(run_id, "applied", applied)

    @activity.defn(name="paid_ads_monitor_propose")
    async def propose(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        if await self._result(run_id, "proposed"):
            return
        decision = (await self._result(run_id, "decision"))["decision"]
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        saved = []
        for proposal in decision.get("proposals") or []:
            request_id = uuid5(run.id, proposal["kind"])
            proposal_id = uuid5(run.id, f"proposal:{proposal['kind']}")
            try:
                row = await self.db.begin_paid_ads_proposal(
                    proposal_id=proposal_id,
                    monitor_run_id=run.id,
                    campaign_run_id=UUID(scope["launch"]["run_id"]),
                    project_id=run.project_id,
                    request_id=request_id,
                    kind=proposal["kind"],
                    previous=proposal["previous"],
                    proposed=proposal["proposed"],
                    rationale=proposal["rationale"],
                    review_path=(
                        f"{paid_ads_monitor.CAMPAIGN_DIR}/{scope['launch']['run_id']}"
                        "/proposals/pending.md"
                    ),
                )
            except RuntimeError as exc:
                saved.append({**proposal, "status": "skipped", "reason": str(exc)[:160]})
                continue
            number = int(row["proposal_number"])
            path = paid_ads_monitor.proposal_path(scope["launch"]["run_id"], number)
            document = paid_ads_monitor.proposal_document(
                proposal,
                scope["campaign"],
                number,
                datetime.fromisoformat(scope["today"]).date(),
            )
            key = self.key(run_id, f"proposal:{proposal['kind']}")
            async with self.db.effect_lock(key, KEY) as (conn, existing):
                if existing and existing.status == "completed":
                    sha = existing.result["review_commit_sha"]
                else:
                    await self.db.start_effect(conn, execution_key=key, operation=KEY)
                    async with self.db.project_state_lock(conn, project.id):
                        sha, _ = await self.storage.publish_state_documents(
                            repo_id=project.state_repo_id,
                            branch=project.canonical_branch,
                            documents={path: document.encode()},
                            workflow_key=KEY,
                            execution_key=key,
                            run_id=str(run.id),
                        )
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={"review_commit_sha": sha, "review_path": path},
                    )
            ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
            await self.db.finalize_paid_ads_proposal(
                proposal_id=UUID(str(row["id"])),
                review_commit_sha=sha,
                artifact_ref=ref,
                review_path=path,
            )
            saved.append(
                {
                    **proposal,
                    "status": "pending",
                    "id": str(row["id"]),
                    "number": number,
                    "path": path,
                }
            )
        await self._save(run_id, "proposed", {"proposals": saved})

    @activity.defn(name="paid_ads_monitor_publish")
    async def publish(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        decision_record = await self._result(run_id, "decision")
        reads = await self._result(run_id, "reads")
        applied = await self._result(run_id, "applied") or {}
        proposed = (await self._result(run_id, "proposed") or {}).get("proposals") or []
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        await self._progress(run_id, "publish", 4, "Writing today's report")
        decision = decision_record["decision"]
        brief = await self._result(run_id, "brief")
        if brief is None:
            system, user = paid_ads_monitor.brief_prompt(decision, scope["campaign"])
            try:
                result = await self._generate(
                    run_id,
                    "brief",
                    system,
                    user,
                    paid_ads_monitor.brief_schema(),
                    paid_ads_monitor.MAX_OUT["brief"],
                )
                brief = paid_ads_monitor.validate_brief(result)
            except UnusableModelResult:
                brief = {"summary": "", "changes_explained": [], "watch_for": []}
            await self._save(run_id, "brief", brief)
        documents = paid_ads_monitor.render(
            decision=decision,
            campaign=scope["campaign"],
            reads_normalized=reads["normalized"],
            brief=brief,
            applied=applied,
            proposals_saved=[p for p in proposed if p.get("status") == "pending"],
            run_id=str(run.id),
            today=datetime.fromisoformat(scope["today"]).date(),
        )
        names = paths(scope["launch"]["run_id"], str(run.id))
        key = self.key(run_id, "publish")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                publication = existing.result
            else:
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
                        documents={names[n]: c.encode() for n, c in documents.items()},
                        paths=names,
                        limits=LIMITS,
                        message=f"{KEY} {run_id} [{key}]",
                        intent=(existing.result or {}).get("publication") if existing else None,
                        save_intent=save_intent,
                        validate_active=validate_active,
                    )
                publication = {
                    "canonical_commit_sha": revision,
                    "artifact_path": names["MONITOR.md"],
                    "documents_sha256": digest({names[n]: c for n, c in documents.items()}),
                    "summary": paid_ads_monitor.summary_line(decision, applied, proposed),
                }
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            ref = (
                f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}"
                f"/{publication['artifact_path']}"
            )
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=publication["canonical_commit_sha"],
                artifact_path=publication["artifact_path"],
                artifact_ref=ref,
                summary=publication["summary"],
                workflow_key=KEY,
            )

    @activity.defn(name="paid_ads_monitor_failure")
    async def failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.executor != KEY:
            return
        failure = await self._result(run_id, "failure") or {}
        message = {
            "model_unavailable": (
                "The Google Ads check stopped because a model result could not be confirmed. "
                "Automatic changes already applied stand; nothing else was changed."
            ),
        }.get(
            failure.get("code"),
            "The Google Ads check stopped before finishing. Changes already applied stand; "
            "the next check reconciles anything unconfirmed.",
        )
        await self.db.project_failure(run_id=run.id, error_message=message)
