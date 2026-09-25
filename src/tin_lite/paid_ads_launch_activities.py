"""Google Ads launch activities: verified sources, a receipted account read, one drafted plan,
one founder approval, then an atomic campaign creation. Only the run identifier crosses
Temporal; every provider result lives in an effect receipt so a retry replays it and an
unconfirmed write is never sent twice."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import paid_ads, paid_ads_launch
from tin_lite.google_ads_requests import (
    QUERIES,
    SUBSCRIPTION_TYPES,
    campaign_bundle,
    campaign_status_body,
    conversion_action_body,
    created_resources,
    micros,
    subscriptions_body,
)
from tin_lite.growth_plan_site import evidence_text, read_site
from tin_lite.integrations import (
    ADS_PROVIDER,
    GITHUB_PROVIDER,
    GitHubFileChange,
    GoogleAdsCallError,
    IntegrationError,
    ads_health_summary,
)
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
from tin_lite.paid_ads_launch import (
    KEY,
    PLAN_DOCS,
    POLICY,
    RESULT_DOCS,
    SETUP_DOCS,
    TRACKING_DOCS,
    paths,
)
from tin_lite.paid_ads_sources import pinned_bundle
from tin_lite.usage_capture import external_usage_scope

STEP_TOTAL = 6
PLACEHOLDER = "{{GLOBAL_SITE_TAG}}"


def _base(step: str) -> str:
    return step.split(":")[0]


def _rows(result: dict) -> list[dict]:
    value = (result or {}).get("value") or {}
    rows = value.get("rows") or []
    return [row for row in rows if isinstance(row, dict)]


def _failed(result: dict, action: str, outcome: str) -> str:
    """Name the side that failed: Tin's own gate, a Google Ads refusal, or a lost answer."""
    if result.get("status") == "unavailable":
        return (
            f"Tin could not authorize {action} ({result.get('reason') or 'unavailable'}), so "
            f"Google Ads was not contacted; {outcome}. Check the project's spending limits, "
            "then try again."
        )
    if result.get("error"):
        return f"Google Ads refused {action} ({result['error']}); {outcome}."
    return (
        f"Tin did not receive Google Ads' answer to {action} "
        f"({result.get('reason') or 'unknown'}); {outcome}."
    )


class PaidAdsLaunchActivities:
    def __init__(
        self, *, database, storage, settings, router=None, integrations=None, site_reader=None
    ):
        self.db, self.storage, self.settings = database, storage, settings
        self.router, self.integrations = router, integrations
        self.read_site = site_reader or read_site

    # ------------------------------------------------------------ receipts

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"{paid_ads_launch.PREFIX}:{UUID(str(run_id))}:{stage}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            run is None
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "needs_input", "succeeded"}
        ):
            raise ApplicationError("The Google Ads launch is not active.", non_retryable=True)
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
        if needs == "github":
            return self.integrations is not None
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
                    "The launch's providers are no longer configured.", non_retryable=True
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

                    # Raw provider errors, headers and credentials never enter evidence.
                    # A billing refusal keeps its fixed code so the failure names it.
                    result = (
                        {"status": "unavailable", "reason": exc.code}
                        if isinstance(exc, BillingError)
                        else {"status": "unknown", "reason": "provider_result_unavailable"}
                    )
            result = {"request_sha256": fingerprint, **saved, **result}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            if activity.in_activity():
                activity.heartbeat({"stage": stage})
            return result

    # ------------------------------------------------------------ model and provider calls

    async def _model(self, run_id: str, step: str, system: str, user: str, schema, max_out, effort):
        if len(user.encode()) > POLICY["max_model_input_bytes"]:
            raise ApplicationError("A model step's input exceeds its bound.", non_retryable=True)
        request = ModelRequest(
            system=system,
            messages=(ModelMessage(role=MessageRole.USER, content=user),),
            output_schema=schema,
            output_schema_name=paid_ads_launch.schema_name(step),
            max_output_tokens=max_out,
            reasoning_effort=ReasoningEffort(effort),
        )
        route = paid_ads_launch.route_for(step).key

        async def call():
            try:
                async with asyncio.timeout(240):
                    result = await self.router.generate(route, request)
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

    async def _ads(self, run_id: str, stage: str, kind: str, request: dict) -> dict:
        """One zero-cost, receipted Google Ads request. A provider refusal is a completed
        receipt carrying only the opaque error code; an unconfirmed attempt stays unknown."""
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
        value = result.get("value") or {}
        return {**result, "error": value.get("error")}

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

    async def _assessment(self, run_id: str, scope: dict) -> tuple[dict, str]:
        project = await self.db.get_project(UUID(scope["project_id"]))
        try:
            _, documents = await pinned_bundle(
                database=self.db,
                storage=self.storage,
                project=project,
                run_id=scope["assessment"]["run_id"],
                executor=paid_ads.KEY,
                prefix=paid_ads.PREFIX,
                paths=paid_ads.paths,
                limits=paid_ads.LIMITS,
            )
            assessment = json.loads(documents["assessment.json"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                str(exc) or "The assessment could not be verified.", non_retryable=True
            ) from None
        if not isinstance(assessment, dict):
            raise ApplicationError("The assessment file is not an object.", non_retryable=True)
        return assessment, documents["keywords.csv"]

    # ------------------------------------------------------------ activities

    @activity.defn(name="paid_ads_launch_prepare")
    async def prepare(self, run_id: str) -> None:
        if await self._result(run_id, "scope"):
            await self.db.mark_run_running(UUID(run_id))
            return
        run = await self._active(run_id)
        inputs = dict(run.input or {})
        try:
            paid_ads_launch.check_inputs(inputs)
        except (ValueError, TypeError, KeyError) as exc:
            raise ApplicationError(str(exc) or "Invalid inputs.", non_retryable=True) from None
        if not self._configured("model") or not self._configured("google_ads"):
            raise ApplicationError(
                "Configure Tin's Google Ads manager account and the native model first.",
                non_retryable=True,
            )
        if not run.definition_commit_sha:
            raise ApplicationError("The launch requires a pinned definition.", non_retryable=True)
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path=f"workflows/{KEY}.json",
            )
        )
        if (
            definition.get("paid_ads_launch_policy") != POLICY
            or definition.get("paid_ads_launch_routes") != paid_ads_launch.route_definitions()
            or definition.get("paid_ads_launch_contract_sha256")
            != paid_ads_launch.contract_digest()
        ):
            raise ApplicationError(
                "This worker does not serve the selected launch contract.", non_retryable=True
            )
        project = await self.db.get_project(run.project_id)
        try:
            source, _ = await pinned_bundle(
                database=self.db,
                storage=self.storage,
                project=project,
                run_id=inputs["assessment_run_id"],
                executor=paid_ads.KEY,
                prefix=paid_ads.PREFIX,
                paths=paid_ads.paths,
                limits=paid_ads.LIMITS,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ApplicationError(
                str(exc) or "Choose a successful paid ads assessment.", non_retryable=True
            ) from None
        try:
            connection = await self.integrations.google_ads_link_status(project_id=run.project_id)
            customer = await self.integrations.google_ads_account(project_id=run.project_id)
        except IntegrationError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from None
        scope = {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "definition_sha": run.definition_commit_sha,
            "inputs": {k: v for k, v in inputs.items() if k != "project_id"},
            "assessment": source,
            "customer_id": customer,
            "connection_id": str(connection.id),
            "marker": paid_ads_launch.marker(str(run.id)),
            "started_at": run.created_at.isoformat(),
            "today": datetime.now(UTC).date().isoformat(),
            "max_cost_usd": str(Decimal(str(inputs.get("max_cost_usd", 3)))),
            "policy_version": POLICY["version"],
        }
        await self._save(run_id, "scope", scope)
        await self.db.mark_run_running(run.id)

    @activity.defn(name="paid_ads_launch_gather")
    async def gather(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        if await self._result(run_id, "gathered"):
            return
        await self._progress(run_id, "gather", 1, "Reading the assessment and the Ads account")
        assessment, keywords_csv = await self._assessment(run_id, scope)
        try:
            skeleton = paid_ads_launch.plan_skeleton(
                assessment=assessment,
                keywords_csv=keywords_csv,
                inputs=scope["inputs"],
                account={"customer": {}},
                marker=scope["marker"],
                today=datetime.fromisoformat(scope["today"]).date(),
            )
        except (ValueError, KeyError, TypeError) as exc:
            skeleton = None
            skeleton_error = str(exc)[:200]
        else:
            skeleton_error = None
        reads = {}
        for name, query in (
            ("account", QUERIES["account"]()),
            ("billing", QUERIES["billing"]()),
            ("conversions", QUERIES["conversion_actions"](30)),
            ("subscriptions", QUERIES["subscriptions"]()),
        ):
            reads[name] = await self._ads(run_id, f"gather:{name}", "search", {"query": query})
        existing = None
        if skeleton is not None:
            lookup = await self._ads(
                run_id,
                "gather:campaign_lookup",
                "search",
                {"query": QUERIES["campaign_by_name"](skeleton["campaign_name"])},
            )
            rows = _rows(lookup)
            existing = rows[0].get("campaign") if rows else None
        health = ads_health_summary(
            {name: _rows(result) for name, result in reads.items() if name != "subscriptions"}
        )
        account = {
            "customer": {
                "id": scope["customer_id"],
                "status": health.get("account_status"),
                "currency_code": health.get("currency_code"),
                "time_zone": health.get("time_zone"),
                "auto_tagging_enabled": health.get("auto_tagging_enabled"),
                "conversion_tracking_status": health.get("conversion_tracking_status"),
                "accepted_customer_data_terms": health.get("accepted_customer_data_terms"),
                "conversion_tracking_id": health.get("conversion_tracking_id"),
                "descriptive_name": health.get("descriptive_name"),
            },
            "billing": {
                "approved": bool(health.get("billing_approved")),
                "statuses": health.get("billing_statuses") or [],
            },
            "conversion_actions": health.get("conversion_actions") or [],
            "subscriptions": [
                {
                    "type": (row.get("recommendationSubscription") or {}).get("type"),
                    "status": (row.get("recommendationSubscription") or {}).get("status"),
                }
                for row in _rows(reads["subscriptions"])
            ],
            "link_status": "active",
            "unavailable": [
                name for name, result in reads.items() if result.get("status") != "completed"
            ],
        }
        if reads["account"].get("status") != "completed" or reads["account"].get("error"):
            raise ApplicationError(
                _failed(reads["account"], "the account read", "nothing was changed"),
                non_retryable=reads["account"].get("error") is not None,
            )
        await self._save(run_id, "gathered:account", {"status": "completed", "value": account})
        landing = (
            skeleton["landing_page"]
            if skeleton
            else (assessment.get("campaign") or {}).get("landing_page")
            or (assessment.get("inputs") or {}).get("product_url")
            or ""
        )
        site = {"verdict": "none", "pages": [], "readable": []}
        if landing:
            saved = await self._result(run_id, "gather:site")
            if saved:
                site = saved["value"]
            else:
                site = await self.read_site(landing, html_scan=paid_ads.tracking_signals)
                for page in site.get("pages", []):
                    page.pop("html", None)
                await self._save(run_id, "gather:site", {"status": "completed", "value": site})
        if activity.in_activity():
            activity.heartbeat({"stage": "site"})
        gate = paid_ads_launch.gate(
            assessment=assessment,
            account=account,
            site=site,
            inputs=scope["inputs"],
            existing_campaign=existing,
        )
        if skeleton is None and gate["outcome"] == "ready":
            gate = {
                "outcome": "not_recommended",
                "text": f"The assessment's campaign shape could not be used: {skeleton_error}",
                "conversion_action": None,
            }
        await self._save(
            run_id,
            "gathered",
            {"gate": gate, "skeleton": skeleton, "existing_campaign": existing},
        )

    @activity.defn(name="paid_ads_launch_draft")
    async def draft(self, run_id: str) -> str:
        """Returns the mode as a small control fact: launch, tracking or setup."""
        scope = await self._result(run_id, "scope")
        saved = await self._result(run_id, "draft")
        if saved:
            return saved["mode"]
        gathered = await self._result(run_id, "gathered")
        gate = gathered["gate"]
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        assessment, _ = await self._assessment(run_id, scope)
        account = (await self._result(run_id, "gathered:account"))["value"]
        site = ((await self._result(run_id, "gather:site")) or {}).get("value") or {}
        business = str((assessment.get("profile") or {}).get("business_name") or "the business")
        await self._progress(run_id, "draft", 2, "Drafting the campaign for your approval")

        async def generate(step, system, user, schema, max_out, effort):
            if activity.in_activity():
                activity.heartbeat({"step": step})
            return await self._model(run_id, step, system, user, schema, max_out, effort)

        record: dict = {"mode": None, "gate": gate}
        if gate["outcome"] == "ready":
            record["mode"] = "launch"
            evidence = {
                "plan": gathered["skeleton"],
                "assessment": assessment,
                "readable": site.get("readable") or [],
                "site_text": evidence_text(site) if site.get("pages") else "",
                "negative_themes": assessment.get("negative_themes") or [],
            }
            try:
                built = await paid_ads_launch.build_plan(scope, evidence, generate)
            except (UnusableModelResult, KeyError, TypeError, ValueError, LookupError) as exc:
                await self._save(
                    run_id,
                    "failure",
                    {"code": "validation", "stage": "draft", "detail": str(exc)[:160]},
                )
                raise ApplicationError(
                    "The campaign draft failed validation. Nothing was created; try again.",
                    non_retryable=True,
                ) from None
            record["plan"], record["brief"] = built["plan"], built["brief"]
            documents, names = built["documents"], paths(run_id, PLAN_DOCS)
            artifact = "PLAN.md"
        elif gate["outcome"] == "needs_tracking":
            record["mode"] = "tracking"
            action = gate.get("conversion_action")
            if not action:
                event = (assessment.get("inputs") or {}).get("conversion_event") or ""
                category, counting = paid_ads_launch.conversion_category(event)
                action = {
                    "name": paid_ads_launch.conversion_action_name(business, scope["marker"]),
                    "category": category,
                    "counting_type": counting,
                    "default_value": float(assessment.get("allowable_cpa_usd") or 0) or 1.0,
                    "currency": account["customer"].get("currency_code") or "USD",
                    "create": True,
                }
            record["action"] = action
            record["tag_change"] = await self._tag_change(run_id, run.project_id, generate)
            documents = paid_ads_launch.render_tracking(
                action, None, record["tag_change"], gate, business
            )
            names = paths(run_id, TRACKING_DOCS)
            artifact = "TRACKING.md"
        else:
            record["mode"] = "setup"
            documents = paid_ads_launch.render_setup(gate, assessment)
            names = paths(run_id, SETUP_DOCS)
            artifact = "SETUP.md"
        publication = await self._publish(run_id, "plan_publish", documents, names, project)
        record["publication"] = {**publication, "artifact_path": names[artifact]}
        plan = record.get("plan") or {}
        await self.db.create_paid_ads_campaign(
            run_id=run.id,
            project_id=run.project_id,
            connection_id=UUID(scope["connection_id"]),
            customer_id=scope["customer_id"],
            mode=record["mode"],
            source_run_id=UUID(scope["assessment"]["run_id"]),
            source_commit_sha=scope["assessment"]["revision"],
            plan_path=names[artifact],
            plan_commit_sha=publication["canonical_commit_sha"],
            daily_budget_micros=int(micros(plan["daily_budget_usd"])) if plan else None,
            cpc_ceiling_micros=int(micros(plan["cpc_ceiling_usd"])) if plan else None,
        )
        await self._save(run_id, "draft", record)
        return record["mode"]

    async def _tag_change(self, run_id: str, project_id: UUID, generate) -> dict | None:
        """When GitHub can write, ask once for the place to insert the site tag; the snippet
        itself is only known after the conversion action exists, so a placeholder stands in."""
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=GITHUB_PROVIDER
        )
        if (
            connection is None
            or connection.status != "connected"
            or connection.configuration.get("write_opted_in") is not True
        ):
            return None
        try:
            snapshot = await self.integrations.github_repository_snapshot(
                project_id=project_id,
                execution_key=self.key(run_id, "draft:github_snapshot"),
                run_id=UUID(str(run_id)),
            )
        except IntegrationError:
            return None
        files = {
            item.path: item.content
            for item in snapshot.files
            if item.path.lower().endswith((".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"))
        }
        if not files:
            return None
        system, user = paid_ads_launch.tag_install_prompt(files)
        try:
            result = await generate(
                "tag_install",
                system,
                user,
                paid_ads_launch.tag_install_schema(list(files)),
                paid_ads_launch.MAX_OUT["tag_install"],
                POLICY["reasoning_effort"],
            )
            change = paid_ads_launch.validate_tag_install(result, files)
        except UnusableModelResult:
            return None
        return {
            **change,
            "repository": snapshot.repository,
            "default_branch": snapshot.default_branch,
            "head_sha": snapshot.head_sha,
        }

    async def _publish(self, run_id, stage, documents, names, project) -> dict:
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self._active(run_id, conn=conn)
            encoded = {names[n]: c.encode() for n, c in documents.items()}
            limits = {**PLAN_DOCS, **TRACKING_DOCS, **SETUP_DOCS, **RESULT_DOCS}
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
            result = {
                "canonical_commit_sha": revision,
                "documents_sha256": digest({names[n]: c for n, c in documents.items()}),
                "paths": names,
            }
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    @activity.defn(name="paid_ads_launch_settle_setup")
    async def settle_setup(self, run_id: str) -> None:
        """A blocked launch ends with the setup note as its result and a plain reason."""
        draft = await self._result(run_id, "draft")
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        publication = draft["publication"]
        ref = (
            f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}"
            f"/{publication['artifact_path']}"
        )
        await self.db.fail_paid_ads_launch(
            run_id=run.id,
            message=draft["gate"]["text"],
            canonical_commit_sha=publication["canonical_commit_sha"],
            artifact_path=publication["artifact_path"],
            artifact_ref=ref,
        )

    @activity.defn(name="paid_ads_launch_request_review")
    async def request_review(self, run_id: str) -> None:
        draft = await self._result(run_id, "draft")
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        publication = draft["publication"]
        ref = (
            f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}"
            f"/{publication['artifact_path']}"
        )
        if draft["mode"] == "launch":
            plan = draft["plan"]
            summary = (
                f"Approve and launch: one Google Search campaign, {plan['campaign_name']}, at "
                f"${plan['daily_budget_usd']:.2f} a day in your Google Ads account. Nothing is "
                "created until you approve it."
            )
        else:
            action = draft["action"]
            summary = (
                f"Approve tracking setup: Tin will "
                f"{'create' if action.get('create') else 'use'} the conversion action "
                f"'{action.get('name')}' in your Google Ads account"
                + (
                    " and open a pull request adding the site tag"
                    if draft.get("tag_change")
                    else " and write the tag snippets for you to install"
                )
                + ". No campaign is created."
            )
        required = await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=publication["canonical_commit_sha"],
            artifact_ref=ref,
            artifact_path=publication["artifact_path"],
            summary=summary,
        )
        if not required:
            raise ApplicationError(
                "The Google Ads launch must require explicit approval.", non_retryable=True
            )
        await self._progress(run_id, "review", 3, "Waiting for your approval")

    @activity.defn(name="paid_ads_launch_record_approval")
    async def record_approval(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self.db.record_human_review(
            run_id=run.id, decision="approved", summary="You approved the Google Ads step."
        )
        await self.db.approve_paid_ads_campaign(run_id=run.id)

    @activity.defn(name="paid_ads_launch_apply")
    async def apply(self, run_id: str) -> None:
        draft = await self._result(run_id, "draft")
        if await self._result(run_id, "applied"):
            return
        run = await self._active(run_id)
        if run.review_decision != "approved":
            raise ApplicationError("The launch was not approved.", non_retryable=True)
        await self._progress(run_id, "apply", 4, "Creating the campaign in Google Ads")
        if draft["mode"] == "launch":
            await self._apply_launch(run_id, draft)
        else:
            await self._apply_tracking(run_id, draft)

    async def _refuse(self, run_id: str, code: str, text: str):
        await self._save(run_id, "failure", {"code": "google_ads", "stage": code, "detail": text})
        raise ApplicationError(text, non_retryable=True)

    async def _apply_launch(self, run_id: str, draft: dict) -> None:
        scope = await self._result(run_id, "scope")
        plan = draft["plan"]
        run = await self._active(run_id)
        operations = campaign_bundle(plan, customer_id=scope["customer_id"])
        await self.db.update_paid_ads_campaign(run_id=run.id, status="creating")
        validated = await self._ads(
            run_id,
            "apply:validate",
            "mutate",
            {"operations": operations, "validate_only": True},
        )
        if validated.get("status") != "completed" or validated.get("error"):
            await self._refuse(
                run_id, "validate", _failed(validated, "the plan check", "nothing was created")
            )
        created = await self._ads(
            run_id, "apply:create", "mutate", {"operations": operations, "validate_only": False}
        )
        resources = None
        if created.get("status") == "completed" and not created.get("error"):
            try:
                resources = created_resources(operations, created["value"].get("results") or [])
            except ValueError:
                resources = None
        if resources is None:
            # Adopt what a previous attempt may have created; never send the bundle twice.
            lookup = await self._ads(
                run_id,
                "apply:lookup",
                "search",
                {"query": QUERIES["campaign_by_name"](plan["campaign_name"])},
            )
            rows = _rows(lookup)
            if rows and isinstance(rows[0].get("campaign"), dict):
                campaign = rows[0]["campaign"]
                shared = await self._ads(
                    run_id,
                    "apply:shared_set_lookup",
                    "search",
                    {
                        "query": QUERIES["shared_set_by_name"](
                            f"{plan['campaign_name']} negatives {plan['marker']}"
                        )
                    },
                )
                shared_rows = _rows(shared)
                resources = {
                    "campaign": campaign.get("resourceName"),
                    "budget": campaign.get("campaignBudget"),
                    "shared_set": (
                        (shared_rows[0].get("sharedSet") or {}).get("resourceName")
                        if shared_rows
                        else None
                    ),
                    "adopted": True,
                }
            elif created.get("status") == "unavailable":
                await self._refuse(
                    run_id,
                    "create",
                    _failed(created, "creating the campaign", "nothing was created"),
                )
            elif created.get("error"):
                await self._refuse(
                    run_id,
                    "create",
                    f"Google Ads refused to create the campaign ({created['error']}).",
                )
            else:
                await self._refuse(
                    run_id,
                    "create",
                    "Google Ads did not confirm whether the campaign was created. Check the "
                    "account before trying again; nothing was sent twice.",
                )
        if not resources.get("campaign"):
            await self._refuse(run_id, "create", "Google Ads returned no campaign resource.")
        campaign_id = str(resources["campaign"]).rsplit("/", 1)[-1]
        await self.db.update_paid_ads_campaign(
            run_id=run.id,
            external_campaign_id=campaign_id,
            external_budget_id=str(resources.get("budget") or "").rsplit("/", 1)[-1] or None,
            external_shared_set_id=(
                str(resources.get("shared_set") or "").rsplit("/", 1)[-1] or None
            ),
        )
        account = (await self._result(run_id, "gathered:account"))["value"]
        enabled_types = sorted(
            {
                str(item.get("type"))
                for item in account.get("subscriptions") or []
                if item.get("status") == "ENABLED" and item.get("type") in SUBSCRIPTION_TYPES
            }
        )
        paused: list[str] = []
        if enabled_types:
            segment, body = subscriptions_body(enabled_types)
            result = await self._ads(
                run_id, "apply:subscriptions", "mutate_resource", {"segment": segment, "body": body}
            )
            if result.get("status") == "completed" and not result.get("error"):
                paused = enabled_types
        segment, body = campaign_status_body(resources["campaign"], "ENABLED")
        enabled = await self._ads(
            run_id, "apply:enable", "mutate_resource", {"segment": segment, "body": body}
        )
        switched_on = enabled.get("status") == "completed" and not enabled.get("error")
        if enabled.get("status") == "unknown":
            # The switch may have landed without its answer: read the campaign back first.
            check = await self._ads(
                run_id,
                "apply:enable_check",
                "search",
                {"query": QUERIES["campaign_by_name"](plan["campaign_name"])},
            )
            state = next(
                (
                    row["campaign"].get("status")
                    for row in _rows(check)
                    if isinstance(row.get("campaign"), dict)
                    and row["campaign"].get("resourceName") == resources["campaign"]
                ),
                None,
            )
            switched_on = state == "ENABLED"
            if state not in {"ENABLED", "PAUSED"}:
                await self._refuse(
                    run_id,
                    "enable",
                    "Tin did not receive Google Ads' answer to switching the campaign on and "
                    "could not read its status back, so it may already be running. Check it in "
                    "Google Ads before running the launch again.",
                )
        if not switched_on:
            await self._refuse(
                run_id,
                "enable",
                _failed(
                    enabled,
                    "switching the campaign on",
                    "the campaign was created and is paused in your account; enable it there "
                    "or run the launch again",
                ),
            )
        enabled_at = datetime.now(UTC).isoformat()
        await self.db.update_paid_ads_campaign(
            run_id=run.id, enabled_at=datetime.fromisoformat(enabled_at)
        )
        await self._save(
            run_id,
            "applied",
            {
                "mode": "launch",
                "launch_run_id": str(run.id),
                "customer_id": scope["customer_id"],
                "resources": resources,
                "campaign_id": campaign_id,
                "budget_id": str(resources.get("budget") or "").rsplit("/", 1)[-1] or None,
                "shared_set_id": str(resources.get("shared_set") or "").rsplit("/", 1)[-1] or None,
                "enabled": True,
                "enabled_at": enabled_at,
                "created_at": created.get("value", {}).get("observed_at") or enabled_at,
                "paused_subscriptions": paused,
                "conversion_action": draft["gate"].get("conversion_action"),
                "adopted": bool(resources.get("adopted")),
            },
        )

    async def _apply_tracking(self, run_id: str, draft: dict) -> None:
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        action = dict(draft["action"])
        if action.get("create"):
            existing = await self._ads(
                run_id,
                "apply:action_lookup",
                "search",
                {"query": QUERIES["conversion_actions"](30)},
            )
            match = next(
                (
                    row["conversionAction"]
                    for row in _rows(existing)
                    if isinstance(row.get("conversionAction"), dict)
                    and row["conversionAction"].get("name") == action["name"]
                ),
                None,
            )
            if match is None:
                segment, body = conversion_action_body(
                    action["name"],
                    action["category"],
                    action["counting_type"],
                    action["default_value"],
                    action["currency"],
                )
                created = await self._ads(
                    run_id,
                    "apply:conversion_action",
                    "mutate_resource",
                    {"segment": segment, "body": body},
                )
                if created.get("status") != "completed" or created.get("error"):
                    await self._refuse(
                        run_id,
                        "conversion_action",
                        _failed(
                            created,
                            "creating the conversion action",
                            "check the account before running the launch again",
                        ),
                    )
                results = created["value"].get("results") or []
                resource = (results[0] or {}).get("resourceName") if results else None
            else:
                resource = match.get("resourceName")
            action["resource_name"] = resource
            action["id"] = str(resource or "").rsplit("/", 1)[-1]
        snippets = None
        if action.get("id"):
            read = await self._ads(
                run_id,
                "apply:snippets",
                "search",
                {"query": QUERIES["conversion_action_snippets"](action["id"])},
            )
            rows = _rows(read)
            for item in (
                ((rows[0].get("conversionAction") or {}).get("tagSnippets") or []) if rows else []
            ):
                if isinstance(item, dict) and item.get("pageFormat", "HTML") == "HTML":
                    snippets = {
                        "global_site_tag": str(item.get("globalSiteTag") or ""),
                        "event_snippet": str(item.get("eventSnippet") or ""),
                    }
                    break
        pull_request = None
        change = draft.get("tag_change")
        if change and snippets and snippets["global_site_tag"]:
            content = change["content"].replace(PLACEHOLDER, snippets["global_site_tag"], 1)

            async def open_pull_request():
                try:
                    result = await self.integrations.github_create_pull_request(
                        project_id=run.project_id,
                        execution_key=f"{run.id}:github_pull_request",
                        title="Add the Google Ads site tag",
                        body=(
                            "Tin adds the Google Ads site tag so conversions can be measured. "
                            "Place the event snippet below on the page that confirms the "
                            f"conversion '{action.get('name')}':\n\n```html\n"
                            f"{snippets['event_snippet']}\n```\n"
                        ),
                        files=(GitHubFileChange(path=change["path"], content=content),),
                        base_branch=change.get("default_branch"),
                        expected_base_sha=change.get("head_sha"),
                        run_id=run.id,
                    )
                except IntegrationError as exc:
                    return {"error": str(exc)[:160]}
                return {
                    "repository": result.repository,
                    "branch": result.branch,
                    "number": result.number,
                    "url": result.url,
                }

            saved = await self._paid(
                run_id,
                "apply:tag_pull_request",
                {"path": change["path"], "sha256": digest({"content": content})},
                "0",
                open_pull_request,
                needs="github",
            )
            value = saved.get("value") or {}
            pull_request = (
                value if saved.get("status") == "completed" and value.get("url") else None
            )
        await self._save(
            run_id,
            "applied",
            {
                "mode": "tracking",
                "customer_id": scope["customer_id"],
                "action": action,
                "snippets": snippets,
                "pull_request": pull_request,
            },
        )

    @activity.defn(name="paid_ads_launch_publish")
    async def publish(self, run_id: str) -> None:
        draft = await self._result(run_id, "draft")
        applied = await self._result(run_id, "applied")
        if applied is None:
            raise ApplicationError("There is nothing to publish.", non_retryable=True)
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        scope = await self._result(run_id, "scope")
        assessment, _ = await self._assessment(run_id, scope)
        business = str((assessment.get("profile") or {}).get("business_name") or "the business")
        await self._progress(run_id, "publish", 5, "Saving the result to your project files")
        if draft["mode"] == "launch":
            documents = paid_ads_launch.render_result(
                draft["plan"],
                applied,
                {
                    "summary": (
                        "Google reviews new ads within about a day. The Google Ads monitor "
                        "reports their status on its first run."
                    )
                },
                draft["gate"]["text"],
            )
            status = "live"
            event = "paid_ads_launch_ready"
            summary = paid_ads_launch.summary_line("ready", draft["plan"])
        else:
            rendered = paid_ads_launch.render_tracking(
                applied["action"],
                applied.get("snippets"),
                applied.get("pull_request"),
                draft["gate"],
                business,
            )
            documents = {
                "RESULT.md": rendered["TRACKING.md"],
                "campaign.json": rendered["tracking.json"],
            }
            status = "tracking"
            event = "paid_ads_tracking_ready"
            summary = paid_ads_launch.summary_line("needs_tracking")
        names = paths(run_id, RESULT_DOCS)
        publication = await self._publish(run_id, "publish", documents, names, project)
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            ref = (
                f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}"
                f"/{names['RESULT.md']}"
            )
            await self.db.complete_paid_ads_launch(
                conn,
                execution_key=key,
                run_id=run.id,
                campaign_status=status,
                canonical_commit_sha=publication["canonical_commit_sha"],
                artifact_path=names["RESULT.md"],
                artifact_ref=ref,
                summary=summary,
                event_type=event,
            )

    @activity.defn(name="paid_ads_launch_failure")
    async def failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.executor != KEY:
            return
        failure = await self._result(run_id, "failure") or {}
        message = {
            "validation": (
                "The Google Ads launch stopped because a model result failed validation. "
                "Nothing was created in Google Ads."
            ),
            "model_unavailable": (
                "The Google Ads launch stopped because a model result could not be confirmed. "
                "Nothing was created in Google Ads; no replacement call was purchased."
            ),
            "google_ads": failure.get("detail")
            or "Google Ads did not accept a request. Check the account before trying again.",
        }.get(
            failure.get("code"),
            "The Google Ads launch stopped before finishing. Check the account before "
            "trying again.",
        )
        await self.db.fail_paid_ads_launch(run_id=run.id, message=message)
