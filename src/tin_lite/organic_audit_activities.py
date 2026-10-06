"""Trusted, receipt-backed activities for the one native organic audit workflow."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.dataforseo import DataForSEO, DataForSEOError
from tin_lite.organic_audit import (
    AUDIT_KEY,
    AUDIT_POLICY,
    LEGACY_AUDIT_POLICY,
    MARKETS,
    V2_AUDIT_POLICY,
    V3_AUDIT_POLICY,
    V4_AUDIT_POLICY,
    V5_AUDIT_POLICY,
    V6_AUDIT_POLICY,
    V7_AUDIT_POLICY,
    V8_AUDIT_POLICY,
    V9_AUDIT_POLICY,
    V10_AUDIT_POLICY,
    V11_AUDIT_POLICY,
    V12_AUDIT_POLICY,
    V13_AUDIT_POLICY,
    audit_paths,
    audit_policy,
    build_documents,
    bundle_sha256,
    crawl_respects_sitemap,
    digest,
    grounded_preparation,
    in_scope_url,
    normalize_pages,
    panel_repetitions,
    question_results,
    sitemap_page_urls,
    summary_paths,
)
from tin_lite.organic_audit_ai import (
    AnswerGrade,
    AnswerJudgment,
    AuditValidationError,
    BuyerPanel,
    PanelValidation,
    ai_contract,
    ai_schemas,
    classify,
    classify_absent_target,
    graded_panel,
    payload,
    read_response,
    response_diagnostics,
    summarize,
    validate_panel,
)
from tin_lite.organic_audit_fetch import (
    SiteReader,
    read_crawler_access,
    read_pages,
    read_pagespeed,
    read_site_files,
)
from tin_lite.organic_audit_format import count
from tin_lite.organic_audit_panel import prepare_panel
from tin_lite.organic_audit_publication import publish_audit
from tin_lite.organic_audit_scope import audit_hosts, resolve_site_identity
from tin_lite.usage_capture import external_usage_scope
from tin_lite.workflow_evidence import integration_inventory


class OrganicAuditActivities:
    def __init__(
        self,
        *,
        database,
        storage,
        settings,
        responses=None,
        integrations=None,
        provider=None,
        site_resolver=resolve_site_identity,
        site_reader=SiteReader,
        pagespeed_reader=read_pagespeed,
    ) -> None:
        self.db, self.storage, self.settings = database, storage, settings
        self.responses = responses
        self.integrations = integrations
        self.site_resolver = site_resolver
        # Factories so tests can supply synthetic sites; production reads the public site.
        self.site_reader = site_reader
        self.pagespeed_reader = pagespeed_reader
        login, password = (
            getattr(settings, "dataforseo_login", None),
            getattr(settings, "dataforseo_password", None),
        )
        self.provider = provider or (
            DataForSEO(login.get_secret_value(), password.get_secret_value())
            if login and password
            else None
        )

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"organic:{UUID(run_id)}:{stage}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(run_id), conn=conn)
        if run is None or run.executor != AUDIT_KEY or run.status.value in {"failed", "stopped"}:
            raise ApplicationError("Organic audit is not active.", non_retryable=True)
        return run

    async def _result(self, run_id: str, stage: str) -> dict | None:
        receipt = await self.db.get_effect(self.key(run_id, stage))
        return receipt.result if receipt and receipt.status == "completed" else None

    async def _policy_version(self, run_id: str) -> str:
        scope = await self._result(run_id, "scope")
        # Existing v1 receipts predate this field. Never reinterpret their policy.
        return (scope or {}).get("policy_version", LEGACY_AUDIT_POLICY["version"])

    async def _save(self, run_id: str, stage: str, result: dict) -> dict:
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, "organic.audit") as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")
            await self.db.complete_effect(conn, execution_key=key, result=result)
        return result

    async def _exposure(self, conn, run_id: str, scope: dict, ledger: dict) -> Decimal:
        """What the run has spent or reserved: billing's observation when the policy says so."""
        exposure = sum((Decimal(value) for value in ledger.values()), Decimal(0))
        billing = getattr(self.db, "billing", None)
        if billing is not None and audit_policy(
            scope.get("policy_version", LEGACY_AUDIT_POLICY["version"])
        ).get("runtime_budget"):
            observed = await billing.run_operation_exposure(conn, UUID(run_id))
            if observed is not None:
                exposure = Decimal(observed) / 1_000_000_000
        return exposure

    async def _reserve(self, run_id: str, stage: str, amount: str) -> bool:
        scope = await self._result(run_id, "scope")
        key = self.key(run_id, "budget")
        async with self.db.effect_lock(key, "organic.audit") as (conn, existing):
            ledger = dict(existing.result or {}) if existing else {}
            if stage in ledger:
                return True
            exposure = await self._exposure(conn, run_id, scope, ledger)
            if exposure + Decimal(amount) > Decimal(scope["max_cost_usd"]):
                return False
            ledger[stage] = amount
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")
            await self.db.save_effect_progress(conn, execution_key=key, result=ledger)
        return True

    async def _reserve_fitting(self, run_id: str, stage: str, choose) -> Decimal | None:
        """Reserve `choose(left)` for one stage, where `left` is what the audit's limit has
        left; None reserves nothing. A stage reserves once, and a retry gets that amount."""
        scope = await self._result(run_id, "scope")
        key = self.key(run_id, "budget")
        async with self.db.effect_lock(key, "organic.audit") as (conn, existing):
            ledger = dict(existing.result or {}) if existing else {}
            if stage in ledger:
                return Decimal(ledger[stage])
            exposure = await self._exposure(conn, run_id, scope, ledger)
            amount = choose(Decimal(scope["max_cost_usd"]) - exposure)
            if amount is None:
                return None
            ledger[stage] = str(amount)
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")
            await self.db.save_effect_progress(conn, execution_key=key, result=ledger)
        return amount

    async def _paid(
        self, run_id: str, stage: str, request: dict, amount: str, call, *, recover=None
    ) -> dict:
        """At most one dispatch per saved intent, not a claim of exactly-once billing.

        A worker dying between intent and dispatch sacrifices availability rather
        than risking a duplicate charge. Crawl metadata can recover that ambiguity.
        Unrecoverable model requests become missing observations, never fresh samples.
        """
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, "organic.audit") as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self._active(run_id, conn=conn)
            fingerprint = digest(request)
            saved = existing.result or {} if existing else {}
            if saved and saved.get("request_sha256") != fingerprint:
                raise ApplicationError(
                    "Audit request changed after its intent was saved.", non_retryable=True
                )
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")
            if saved.get("attempted_at"):
                result = await recover(saved) if recover else None
                if stage == "crawl_submit" and result and result.get("status") == "completed":
                    from tin_lite.usage_capture import recover_tool_observation

                    await recover_tool_observation(
                        self.db,
                        conn,
                        run_id=run_id,
                        step=stage,
                        endpoint="on_page/task_post",
                        cost=result["value"].get("reported_cost_usd"),
                    )
                if result is None:
                    result = {"status": "unknown", "reason": "unconfirmed_previous_request"}
            elif not await self._reserve(run_id, stage, amount):
                result = {"status": "unavailable", "reason": "spending_limit"}
            else:
                await self._active(run_id, conn=conn)
                saved = {
                    "request_sha256": fingerprint,
                    "attempted_at": datetime.now(UTC).isoformat(),
                    "reservation_usd": amount,
                }
                await self.db.save_effect_progress(conn, execution_key=key, result=saved)
                await self._active(run_id, conn=conn)
                try:
                    with external_usage_scope(self.db, conn, run_id, stage, maximum_usd=amount):
                        result = {"status": "completed", "value": await call()}
                except Exception as exc:
                    from tin_lite.billing_contracts import BillingError

                    if isinstance(exc, BillingError):
                        result = {"status": "unavailable", "reason": "spending_limit"}
                    # Preserve only a safe product fact, not raw provider errors.
                    # Leave crawl intent incomplete so a later attempt can recover it.
                    elif recover:
                        raise ApplicationError(
                            "Crawl submission needs metadata reconciliation."
                        ) from None
                    elif await self._policy_version(run_id) != LEGACY_AUDIT_POLICY[
                        "version"
                    ] and isinstance(exc, AuditValidationError):
                        result = {"status": "unavailable", "reason": exc.reason}
                        if getattr(exc, "diagnostics", None) is not None:
                            result["diagnostics"] = exc.diagnostics
                    else:
                        result = {"status": "unknown", "reason": "provider_result_unavailable"}
                        failure_kind = getattr(exc, "failure_kind", None)
                        if failure_kind in {"timeout", "connection", "http", "api"}:
                            result["diagnostics"] = {"provider_failure_kind": failure_kind}
            result = {**saved, **result}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    async def _model(self, run_id: str, stage: str, request: dict, *, search: bool) -> dict:
        if self.responses is None:
            return await self._save(
                run_id, stage, {"status": "unavailable", "reason": "model_not_configured"}
            )

        policy_version = await self._policy_version(run_id)
        policy = audit_policy(policy_version)
        if stage.startswith("answer:") and policy.get("answer_timeout_seconds"):
            request = {**request, "timeout": policy["answer_timeout_seconds"]}

        async def call():
            response = await self.responses.create(request)
            try:
                return read_response(response, search=search, policy_version=policy_version)
            except AuditValidationError as exc:
                if grounded_preparation(policy_version):
                    exc.diagnostics = response_diagnostics(response)
                raise
            except (AttributeError, KeyError, TypeError, ValueError):
                raise AuditValidationError("response_invalid") from None

        return await self._paid(
            run_id,
            stage,
            request,
            AUDIT_POLICY["search_reservation_usd" if search else "text_reservation_usd"],
            call,
        )

    @activity.defn
    async def organic_prepare(self, run_id: str) -> None:
        if await self._result(run_id, "scope"):
            return
        run = await self._active(run_id)
        maximum = Decimal(str(getattr(self.settings, "organic_audit_max_cost_usd", 0)))
        if (
            self.provider is None
            or not maximum.is_finite()
            or maximum < Decimal(AUDIT_POLICY["crawl_reservation_usd"])
        ):
            raise ApplicationError(
                "Configure DataForSEO and an explicit organic-audit spending limit before running.",
                type="organic_audit_not_configured",
                non_retryable=True,
            )
        if not run.definition_commit_sha:
            raise ApplicationError("Organic audit has no pinned definition.", non_retryable=True)
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path="workflows/organic.audit.json",
            )
        )
        pinned_policy = definition.get("audit_policy")
        if (
            pinned_policy
            not in (
                LEGACY_AUDIT_POLICY,
                V2_AUDIT_POLICY,
                V3_AUDIT_POLICY,
                V4_AUDIT_POLICY,
                V5_AUDIT_POLICY,
                V6_AUDIT_POLICY,
                V7_AUDIT_POLICY,
                V8_AUDIT_POLICY,
                V9_AUDIT_POLICY,
                V10_AUDIT_POLICY,
                V11_AUDIT_POLICY,
                V12_AUDIT_POLICY,
                V13_AUDIT_POLICY,
                AUDIT_POLICY,
            )
            or definition.get("audit_instructions") != ai_contract(pinned_policy["version"])
            or definition.get("audit_schemas") != ai_schemas(pinned_policy["version"])
            or definition.get("model_route")
            != {
                "key": "organic.audit.visibility.v1",
                "provider": "openai",
                "model": AUDIT_POLICY["model"],
                "capabilities": ["json_schema", "text"],
            }
        ):
            raise ApplicationError(
                "The pinned audit version is not supported by this worker.", non_retryable=True
            )
        inputs = run.input
        from tin_lite.organic_audit_completion import KIND, prepare_completion

        if (getattr(run, "prerequisite_evidence", None) or {}).get("kind") == KIND:
            if pinned_policy not in (
                V8_AUDIT_POLICY,
                V9_AUDIT_POLICY,
                V10_AUDIT_POLICY,
                V11_AUDIT_POLICY,
                V12_AUDIT_POLICY,
                V13_AUDIT_POLICY,
                AUDIT_POLICY,
            ):
                raise ValueError("Audit completion requires the current compatible policy")
            await prepare_completion(self, run, target_policy=pinned_policy)
            return
        url, host = await self.provider.validate_target(inputs["site_url"])
        identity = (
            {"site_identity": await self.site_resolver(url)}
            if pinned_policy.get("verified_www_redirects")
            else {}
        )
        if inputs["market"] not in MARKETS:
            raise ApplicationError(
                "This market is not supported by the English audit.", non_retryable=True
            )
        await self._save(
            run_id,
            "scope",
            {
                "url": url,
                "host": host,
                "market": inputs["market"],
                "language": "en",
                "focus": inputs.get("focus", ""),
                "started_at": datetime.now(UTC).isoformat(),
                "max_cost_usd": str(maximum),
                "policy_version": pinned_policy["version"],
                **identity,
                **(
                    {"integrations": await integration_inventory(self.db, run.project_id)}
                    if pinned_policy.get("check_applicability")
                    else {}
                ),
                **(self._site_scope(pinned_policy) if pinned_policy.get("site_checks") else {}),
            },
        )
        await self.db.mark_run_running(UUID(run_id))

    def _site_scope(self, policy: dict) -> dict:
        """Pin this run's page cap and whether speed can be measured; later config is ignored."""
        cap = getattr(self.settings, "organic_audit_max_pages", policy["default_page_cap"])
        if type(cap) is not int or not 10 <= cap <= policy["max_pages"]:
            cap = policy["default_page_cap"]
        return {
            "page_cap": cap,
            "pagespeed": "configured"
            if getattr(self.settings, "pagespeed_api_key", None)
            else "not_configured",
        }

    async def _search_console_evidence(self, run_id, scope):
        from tin_lite.integrations import GSC_PROVIDER
        from tin_lite.keyword_plan import gsc_property_matches

        existing = await self._result(run_id, "search_console")
        if existing and (
            not audit_policy(scope["policy_version"]).get("site_checks")
            or "request_sha256" not in existing  # No matching property: nothing was read.
            or (
                await self._result(run_id, "search_console_queries")
                and (
                    not audit_policy(scope["policy_version"]).get("decay_min_previous_clicks")
                    or await self._result(run_id, "search_console_previous")
                )
            )
        ):
            return
        if self.integrations is None:
            await self._save(
                run_id, "search_console", {"status": "unavailable", "reason": "adapter_unavailable"}
            )
            return
        run = await self._active(run_id)
        connection = await self.db.get_integration_connection(
            project_id=run.project_id, provider_key=GSC_PROVIDER
        )
        site = connection.configuration.get("selected_site_url", "") if connection else ""
        if (
            not connection
            or connection.status != "connected"
            or not gsc_property_matches(site, scope["host"])
        ):
            await self._save(
                run_id,
                "search_console",
                {"status": "not_available", "reason": "matching_property_not_connected"},
            )
            return
        end = run.created_at.date() - timedelta(days=3)
        policy = audit_policy(scope["policy_version"])
        request = {
            "property": site,
            "start_date": (
                end - timedelta(days=policy.get("search_console_days", 28) - 1)
            ).isoformat(),
            "end_date": end.isoformat(),
        }
        if policy.get("site_checks"):
            await self._search_console_reads(run_id, run, scope, request, policy)
            return

        async def read():
            raw = await self.integrations.search_console_analytics(
                project_id=run.project_id,
                start_date=request["start_date"],
                end_date=request["end_date"],
                dimensions=("page",),
                row_limit=100,
                expected_site_url=site,
                execution_key=self.key(run_id, "search_console:read"),
                run_id=run.id,
            )
            from tin_lite.organic_audit import search_console_pages

            return {
                **request,
                **search_console_pages(raw, scope["host"], aliases=audit_hosts(scope)),
            }

        await self._paid(run_id, "search_console", request, "0", read)

    async def _search_console_reads(self, run_id, run, scope, request, policy):
        """v10: page rows with position, then query+page rows, each its own receipt."""
        from tin_lite.organic_audit_search import search_console_rows

        hosts = audit_hosts(scope)

        def in_scope(url):
            return in_scope_url(url, scope["host"], aliases=hosts)

        async def pages():
            raw = await self.integrations.search_console_analytics(
                project_id=run.project_id,
                start_date=request["start_date"],
                end_date=request["end_date"],
                dimensions=("page",),
                row_limit=policy["search_console_page_rows"],
                expected_site_url=request["property"],
                execution_key=self.key(run_id, "search_console:read"),
                run_id=run.id,
            )
            rows, returned = search_console_rows(
                raw,
                ("page",),
                in_scope=in_scope,
                max_rows=policy["search_console_page_rows"],
            )
            return {
                **request,
                "pages": sorted(rows, key=lambda r: (-r["impressions"], r["url"])),
                "returned_rows": returned,
                "row_limit": policy["search_console_page_rows"],
                "note": "Page rows for the selected property and dates, filtered to the audited "
                "hosts; omitted pages are not proven unindexed. Results are not market-filtered.",
            }

        await self._paid(run_id, "search_console", request, "0", pages)

        async def queries():
            raw = await self.integrations.search_console_analytics(
                project_id=run.project_id,
                start_date=request["start_date"],
                end_date=request["end_date"],
                dimensions=("query", "page"),
                row_limit=policy["search_console_query_rows"],
                expected_site_url=request["property"],
                execution_key=self.key(run_id, "search_console_queries:read"),
                run_id=run.id,
            )
            rows, returned = search_console_rows(
                raw,
                ("query", "page"),
                in_scope=in_scope,
                max_rows=policy["search_console_query_rows"],
            )
            rows.sort(key=lambda r: (-r["impressions"], r["query"], r["url"]))
            return {
                **request,
                "fields": ["query", "page", "clicks", "impressions", "position"],
                "queries": [
                    [r["query"], r["url"], r["clicks"], r["impressions"], r["position"]]
                    for r in rows
                ],
                "returned_rows": returned,
                "row_limit": policy["search_console_query_rows"],
                "note": "Query and page rows for the same property and dates. Google omits "
                "rare and anonymized queries; totals are lower than page totals.",
            }

        await self._paid(
            run_id,
            "search_console_queries",
            {**request, "dimensions": ["query", "page"]},
            "0",
            queries,
        )
        if not policy.get("decay_min_previous_clicks"):
            return
        days = policy.get("search_console_days", 28)
        start = datetime.fromisoformat(request["start_date"]).date()
        previous = {
            "property": request["property"],
            "start_date": (start - timedelta(days=days)).isoformat(),
            "end_date": (start - timedelta(days=1)).isoformat(),
        }

        async def previous_pages():
            raw = await self.integrations.search_console_analytics(
                project_id=run.project_id,
                start_date=previous["start_date"],
                end_date=previous["end_date"],
                dimensions=("page",),
                row_limit=policy["search_console_page_rows"],
                expected_site_url=request["property"],
                execution_key=self.key(run_id, "search_console_previous:read"),
                run_id=run.id,
            )
            rows, returned = search_console_rows(
                raw,
                ("page",),
                in_scope=in_scope,
                max_rows=policy["search_console_page_rows"],
            )
            return {
                **previous,
                "pages": sorted(rows, key=lambda r: (-r["impressions"], r["url"])),
                "returned_rows": returned,
                "note": "Page rows for the 28 days before the audit window, to find pages "
                "losing clicks.",
            }

        await self._paid(run_id, "search_console_previous", previous, "0", previous_pages)

    async def _respects_sitemap(self, run_id: str, scope: dict) -> bool:
        """What the crawl request sends as respect_sitemap, from saved receipts only."""
        files = None
        if audit_policy(scope.get("policy_version", LEGACY_AUDIT_POLICY["version"])).get(
            "follow_links_without_sitemap"
        ):
            files = await self._result(run_id, "site_files")
        return crawl_respects_sitemap(scope, files)

    async def _crawl_mode(self, run_id: str, scope: dict) -> dict:
        """From v14 the crawl says whether the provider followed the sitemap or links, and why."""
        if not audit_policy(scope.get("policy_version", LEGACY_AUDIT_POLICY["version"])).get(
            "follow_links_without_sitemap"
        ):
            return {}
        if await self._respects_sitemap(run_id, scope):
            return {"mode": "sitemap", "note": "The provider followed the site's sitemap."}
        return {
            "mode": "links",
            "note": "No sitemap URL on this site was found, so the provider followed links "
            "from the homepage up to the page cap.",
        }

    async def _site_evidence(self, run_id: str, scope: dict) -> dict:
        """robots.txt, sitemaps and the page selection, each saved once before the crawl."""
        from tin_lite.organic_audit_site import select_pages

        policy = audit_policy(scope["policy_version"])
        hosts = audit_hosts(scope)
        files = await self._result(run_id, "site_files")
        if files is None:
            try:
                async with self.site_reader(hosts) as reader:
                    files = await read_site_files(reader, scope["url"], policy)
                files["status"] = "observed"
            except Exception:  # noqa: BLE001 - a site-read defect must not stop the crawl.
                activity.logger.warning("Organic audit site files were not read.", exc_info=True)
                files = {"status": "unavailable", "reason": "site_read_failed"}
            files = await self._save(run_id, "site_files", files)
        plan = await self._result(run_id, "crawl_plan")
        if plan is None:
            search = await self._result(run_id, "search_console") or {}
            rows = (
                search.get("value", {}).get("pages", [])
                if search.get("status") == "completed"
                else []
            )
            sitemap = sitemap_page_urls(files, scope)
            plan = select_pages(
                home=scope["url"],
                sitemap_urls=sitemap,
                search_pages=[r for r in rows if r["impressions"] > 0],
                cap=scope["page_cap"],
            )
            plan["priority_urls"] = [
                row["url"]
                for row in plan["selected"]
                if row["reason"] != "homepage" and urlsplit(row["url"]).hostname == scope["host"]
            ][: policy["max_priority_urls"]]
            plan = await self._save(run_id, "crawl_plan", plan)
        await self._access_step(run_id, scope)
        return plan

    async def _collect_page_facts(self, run_id: str, scope: dict, *, seconds: float) -> bool:
        """Static HTML facts for the selected pages, then for pages the crawl adds.

        Progress is saved after each bounded batch; the receipt completes only when the
        provider crawl is final and every target was read or refused.
        """
        from tin_lite.organic_audit_site import url_key

        key = self.key(run_id, "page_facts")
        existing = await self.db.get_effect(key)
        if existing and existing.status == "completed":
            return True
        policy = audit_policy(scope["policy_version"])
        plan = await self._result(run_id, "crawl_plan") or {"selected": []}
        crawl = await self._result(run_id, "crawl")
        targets, seen = [], set()
        for url in [row["url"] for row in plan["selected"]] + [
            page["url"] for page in (crawl or {}).get("pages", [])
        ]:
            if url_key(url) not in seen and len(targets) < scope["page_cap"]:
                seen.add(url_key(url))
                targets.append(url)
        done = dict(((existing.result or {}) if existing else {}).get("pages", {}))
        pending = [url for url in targets if url not in done]
        if pending:
            files = await self._result(run_id, "site_files") or {}
            found: dict = {}
            try:
                async with self.site_reader(audit_hosts(scope)) as reader:
                    found = await read_pages(
                        reader,
                        pending,
                        robots=files.get("robots"),
                        policy=policy,
                        seconds=seconds,
                    )
            except Exception:  # noqa: BLE001 - unread pages stay unknown, never passes.
                activity.logger.warning("Organic audit page facts were not read.", exc_info=True)
            if not found:
                # Every started read returns a record, so nothing at all means the reader
                # itself failed. Record that instead of retrying until the crawl deadline.
                found = {
                    url: {"url": url, "fetch": "unavailable", "reason": "site_read_failed"}
                    for url in pending
                }
            done.update(found)
        final = crawl is not None and all(url in done for url in targets)
        async with self.db.effect_lock(key, "organic.audit") as (conn, current):
            if current and current.status == "completed":
                return True
            merged = {**((current.result or {}).get("pages", {}) if current else {}), **done}
            result = {"pages": merged, "targets": len(targets)}
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")
            if final:
                await self.db.complete_effect(
                    conn, execution_key=key, result={**result, "status": "complete"}
                )
            else:
                await self.db.save_effect_progress(conn, execution_key=key, result=result)
        return final

    async def _pagespeed_step(self, run_id: str, scope: dict) -> bool:
        """One PageSpeed Insights read per call, so each poll stays short."""
        if await self._result(run_id, "pagespeed"):
            return True
        policy = audit_policy(scope["policy_version"])
        secret = getattr(self.settings, "pagespeed_api_key", None)
        if scope.get("pagespeed") != "configured" or secret is None:
            await self._save(run_id, "pagespeed", {"status": "not_configured", "results": []})
            return True
        plan = await self._result(run_id, "crawl_plan") or {"selected": []}
        urls = [row["url"] for row in plan["selected"]][: policy["pagespeed_max_urls"]]
        results = []
        for index, url in enumerate(urls):
            saved = await self._result(run_id, f"pagespeed:{index}")
            if saved is None:
                result = await self.pagespeed_reader(
                    url,
                    secret.get_secret_value(),
                    **({} if policy.get("site_angles") else {"lighthouse": False}),
                )
                await self._save(run_id, f"pagespeed:{index}", {"url": url, "result": result})
                return False
            results.append(saved)
        await self._save(
            run_id,
            "pagespeed",
            {
                "status": "observed" if results else "unavailable",
                "strategy": "mobile",
                "results": results,
            },
        )
        return True

    @activity.defn
    async def organic_start_crawl(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        policy = audit_policy(scope.get("policy_version", LEGACY_AUDIT_POLICY["version"]))
        if policy.get("check_applicability"):
            await self._search_console_evidence(run_id, scope)
        options = {}
        site_checks = policy.get("site_checks") and not scope.get("completion")
        if site_checks:
            plan = await self._site_evidence(run_id, scope)
            options.update(max_pages=scope["page_cap"], priority_urls=plan["priority_urls"])
        if policy.get("respect_sitemap"):
            # From saved site files only, so recover() rebuilds the request it submitted.
            options["respect_sitemap"] = await self._respects_sitemap(run_id, scope)
        request = self.provider.crawl_request(
            host=scope["host"], tag=f"tin-organic-{run_id}", **options
        )

        async def recover(saved):
            found = await self.provider.recover(request=request, submitted_at=saved["attempted_at"])
            if found:
                return {"status": "completed", "value": found}
            # No match is not proof that a paid request was never accepted. Keep the
            # intent pending for bounded Temporal retries; never call task_post again.
            raise ApplicationError("Crawl task is not yet reconciled; no duplicate was submitted.")

        await self._paid(
            run_id,
            "crawl_submit",
            request,
            AUDIT_POLICY["crawl_reservation_usd"],
            lambda: self.provider.submit(request),
            recover=recover,
        )
        if site_checks:
            # Read the selected pages while the provider crawls; the poll finishes the rest.
            await self._collect_page_facts(run_id, scope, seconds=120)

    @activity.defn
    async def organic_poll_crawl(self, run_id: str) -> bool:
        scope = await self._result(run_id, "scope")
        policy = audit_policy((scope or {}).get("policy_version", LEGACY_AUDIT_POLICY["version"]))
        if not policy.get("site_checks") or scope.get("completion"):
            return await self._poll_provider(run_id)
        crawl_was_final = await self._result(run_id, "crawl") is not None
        crawl_final = await self._poll_provider(run_id)
        # Provider reads take up to 80 s at worst; 20 s of page reads (each at most 15 s once
        # started) keeps the attempt inside its two-minute limit.
        if not await self._collect_page_facts(run_id, scope, seconds=20):
            return False
        if not crawl_was_final and scope.get("pagespeed") == "configured":
            return False  # Keep this attempt short; speed is read on the next poll.
        if not crawl_final or not await self._pagespeed_step(run_id, scope):
            return False
        return await self._inspection_step(run_id, scope, seconds=20)

    async def _access_step(self, run_id: str, scope: dict) -> None:
        """The homepage and one selected page read as a browser and as AI crawlers, once."""
        policy = audit_policy(scope["policy_version"])
        if not policy.get("access_check_pages") or await self._result(run_id, "access"):
            return
        plan = await self._result(run_id, "crawl_plan") or {"selected": []}
        urls = [scope["url"]] + [
            row["url"]
            for row in plan["selected"]
            if row["reason"] != "homepage" and urlsplit(row["url"]).hostname == scope["host"]
        ]
        urls = list(dict.fromkeys(urls))[: policy["access_check_pages"]]
        try:
            async with self.site_reader(audit_hosts(scope)) as reader:
                result = await read_crawler_access(reader, urls, seconds=30)
        except Exception:  # noqa: BLE001 - an unread comparison stays unknown.
            activity.logger.warning("Organic audit crawler access was not read.", exc_info=True)
            result = {"status": "unavailable", "reason": "site_read_failed", "rows": []}
        await self._save(run_id, "access", result)

    async def _inspection_step(self, run_id: str, scope: dict, *, seconds: float) -> bool:
        """Google's URL Inspection for the key pages, each its own receipt, within `seconds`."""
        policy = audit_policy(scope["policy_version"])
        if not policy.get("url_inspection_max_urls") or await self._result(
            run_id, "url_inspection"
        ):
            return True
        search = await self._result(run_id, "search_console") or {}
        if search.get("status") != "completed" or self.integrations is None:
            await self._save(
                run_id,
                "url_inspection",
                {"status": "not_available", "reason": "matching_property_not_connected"},
            )
            return True
        from tin_lite.organic_audit_search import inspection_row, inspection_urls

        run = await self._active(run_id)
        facts = await self.db.get_effect(self.key(run_id, "page_facts"))
        urls = inspection_urls(
            home=scope["url"],
            host=scope["host"],
            search_pages=search["value"].get("pages", []),
            facts=((facts.result or {}) if facts else {}).get("pages", {}),
            cap=policy["url_inspection_max_urls"],
        )
        results = []
        deadline = time.monotonic() + seconds
        for index, url in enumerate(urls):
            saved = await self._result(run_id, f"url_inspection:{index}")
            if saved is None:
                if time.monotonic() > deadline:
                    return False  # The rest are read on the next poll.

                async def inspect(url=url, index=index):
                    raw = await self.integrations.search_console_url_inspection(
                        project_id=run.project_id,
                        url=url,
                        expected_site_url=search["value"]["property"],
                        execution_key=self.key(run_id, f"url_inspection:{index}:read"),
                        run_id=run.id,
                    )
                    return inspection_row(url, raw)

                saved = await self._paid(
                    run_id, f"url_inspection:{index}", {"url": url}, "0", inspect
                )
            results.append(
                saved["value"]
                if saved.get("status") == "completed"
                else {"url": url, "status": "unknown", "reason": saved.get("reason")}
            )
        await self._save(
            run_id,
            "url_inspection",
            {"status": "observed" if results else "unavailable", "results": results},
        )
        return True

    async def _poll_provider(self, run_id: str) -> bool:
        if await self._result(run_id, "crawl"):
            return True
        await self._active(run_id)
        submission = await self._result(run_id, "crawl_submit")
        if not submission or submission["status"] != "completed":
            await self._save(
                run_id,
                "crawl",
                {
                    "status": "unavailable",
                    "pages": [],
                    "note": "Crawl was not confirmed; no second crawl was submitted.",
                },
            )
            return True
        task_id = submission["value"]["task_id"]
        summary = await self.provider.summary(task_id)
        if summary["crawl_progress"] != "finished":
            await self.db.project_run_progress(
                run_id=UUID(run_id),
                mode="indeterminate",
                step="crawl",
                summary="Collecting the bounded public-site crawl.",
            )
            return False
        scope = await self._result(run_id, "scope")
        hosts = audit_hosts(scope)
        if summary.get("domain") not in {None, *hosts}:
            await self._save(
                run_id,
                "crawl",
                {
                    "status": "unavailable",
                    "pages": [],
                    "note": "The crawler resolved a different host; results were excluded.",
                },
            )
            return True
        options = (
            {"include_broken": True}
            if audit_policy(scope.get("policy_version", LEGACY_AUDIT_POLICY["version"])).get(
                "check_applicability"
            )
            else {}
        )
        if scope.get("page_cap"):
            options["limit"] = scope["page_cap"]
        raw_pages = await self.provider.pages(task_id, **options)
        mode = await self._crawl_mode(run_id, scope)
        pages = normalize_pages(
            raw_pages,
            scope["host"],
            aliases=hosts,
            policy_version=scope.get("policy_version", LEGACY_AUDIT_POLICY["version"]),
            respect_sitemap=await self._respects_sitemap(run_id, scope),
        )
        status = (
            "completed"
            if pages and summary.get("extended_crawl_status") in {None, "no_errors"}
            else "partial"
        )
        await self._save(
            run_id,
            "crawl",
            {
                "status": status,
                "pages": pages,
                "summary": summary,
                "task_id": task_id,
                "note": " ".join(
                    ["Static HTML only; no JavaScript or resource rendering."]
                    + ([mode["note"]] if mode else [])
                ),
                **({"crawl_mode": mode["mode"]} if mode else {}),
                **(
                    {
                        "collection": {
                            **self._collection_counts(
                                raw_pages, pages, include_broken="include_broken" in options
                            ),
                        }
                    }
                    if audit_policy(
                        scope.get("policy_version", LEGACY_AUDIT_POLICY["version"])
                    ).get("verified_www_redirects")
                    else {}
                ),
            },
        )
        return True

    @staticmethod
    def _collection_counts(raw_pages, pages, *, include_broken):
        if not include_broken:
            return {
                "provider_html_pages": len(raw_pages),
                "retained_html_pages": len(pages),
                "excluded_html_pages": len(raw_pages) - len(pages),
            }
        html_urls = {p.get("url") for p in raw_pages if p.get("resource_type") == "html"}
        retained_html = len(html_urls & {p["url"] for p in pages})
        provider_html = sum(p.get("resource_type") == "html" for p in raw_pages)
        return {
            "provider_html_pages": provider_html,
            "retained_html_pages": retained_html,
            "excluded_html_pages": provider_html - retained_html,
            "provider_resources": len(raw_pages),
            "retained_resources": len(pages),
        }

    @activity.defn
    async def organic_end_crawl(self, run_id: str) -> None:
        if await self._result(run_id, "crawl"):
            return
        submission = await self._result(run_id, "crawl_submit")
        stopped = False
        pages, mode = [], {}
        if self.provider and submission and submission["status"] == "completed":
            try:
                await self.provider.stop(submission["value"]["task_id"])
                stopped = True
            except DataForSEOError:
                pass
            scope = await self._result(run_id, "scope")
            mode = await self._crawl_mode(run_id, scope)
            try:
                pages = normalize_pages(
                    await self.provider.pages(
                        submission["value"]["task_id"],
                        **(
                            {"include_broken": True}
                            if audit_policy(
                                scope.get("policy_version", LEGACY_AUDIT_POLICY["version"])
                            ).get("check_applicability")
                            else {}
                        ),
                        **({"limit": scope["page_cap"]} if scope.get("page_cap") else {}),
                    ),
                    scope["host"],
                    aliases=audit_hosts(scope),
                    policy_version=scope.get("policy_version", LEGACY_AUDIT_POLICY["version"]),
                    respect_sitemap=await self._respects_sitemap(run_id, scope),
                )
            except (DataForSEOError, ValueError):
                pass
        await self._save(
            run_id,
            "crawl",
            {
                "status": "partial",
                "pages": pages,
                "note": " ".join(
                    ["Crawl collection reached its time or retry limit."]
                    + ([mode["note"]] if mode else [])
                ),
                **({"crawl_mode": mode["mode"]} if mode else {}),
                "provider_stop_confirmed": stopped,
            },
        )

    @activity.defn
    async def organic_prepare_panel(self, run_id: str) -> int:
        if grounded_preparation(await self._policy_version(run_id)):
            return await prepare_panel(self, run_id)
        panel = await self._result(run_id, "panel")
        if panel:
            return panel["planned_observations"] if panel.get("status") == "completed" else 0
        scope = await self._result(run_id, "scope")
        policy_version = await self._policy_version(run_id)
        request = payload(
            stage="panel",
            data={"website": scope["url"], "market": scope["market"]},
            schema=BuyerPanel,
            market=scope["market"],
            search=True,
            policy_version=policy_version,
        )
        research = await self._model(run_id, "panel_research", request, search=True)
        result = {"status": "unavailable", "reason": "public_identity_or_panel_not_validated"}
        try:
            if research["status"] == "completed":
                panel = validate_panel(research["value"], scope["host"])
                judgment = await self._model(
                    run_id,
                    "panel_validation",
                    payload(
                        stage="validate",
                        data={
                            "website": scope["url"],
                            "research": research["value"],
                            "panel": panel,
                        },
                        schema=PanelValidation,
                        market=scope["market"],
                        search=False,
                        policy_version=policy_version,
                    ),
                    search=False,
                )
                if (
                    judgment["status"] == "completed"
                    and PanelValidation.model_validate_json(judgment["value"]["text"]).accepted
                ):
                    result = {"status": "completed", **panel}
        except ValueError:
            pass
        result = await self._save(run_id, "panel", result)
        return result["planned_observations"] if result["status"] == "completed" else 0

    @activity.defn
    async def organic_observe(self, control: dict) -> None:
        run_id, index = control["run_id"], control["index"]
        if await self._result(run_id, f"observation:{index}"):
            return
        await self._active(run_id)
        scope, panel = await self._result(run_id, "scope"), await self._result(run_id, "panel")
        policy_version = await self._policy_version(run_id)
        modern = policy_version != LEGACY_AUDIT_POLICY["version"]
        if type(index) is not int or not 0 <= index < panel["planned_observations"]:
            raise ApplicationError(
                "Observation index is outside its frozen panel.", non_retryable=True
            )
        repetitions = panel_repetitions(panel)
        searched = len(panel["questions"]) * repetitions
        # Answers without web search follow every searched answer, one per question.
        memory = bool(panel.get("unsearched")) and index >= searched
        question_index = index - searched if memory else index // repetitions
        ladder = graded_panel(panel)
        question = panel["questions"][question_index]["question"]
        answer = await self._model(
            run_id,
            f"answer:{index}",
            payload(
                stage="answer_memory" if memory else "answer",
                data=question,
                market=scope["market"],
                search=not memory,
                policy_version=policy_version,
            ),
            search=not memory,
        )
        result = {
            "status": "unavailable",
            "index": index,
            "question_index": question_index,
            "repetition": 1 if memory else index % repetitions + 1,
            "answer": answer,
            **({"mode": "memory"} if memory else {}),
        }
        absent = (
            classify_absent_target(answer["value"], panel, ladder=ladder)
            if modern and answer["status"] == "completed"
            else None
        )
        if absent is not None:
            result.update(
                status="completed", classification=absent, scoring_method="target_name_absent"
            )
        elif answer["status"] == "completed":
            judgment = await self._model(
                run_id,
                f"judge:{index}",
                payload(
                    stage="judge_graded" if ladder else "judge",
                    data={
                        "answer": answer["value"]["text"],
                        "name": panel["name"],
                        "aliases": panel["aliases"],
                    },
                    schema=AnswerGrade if ladder else AnswerJudgment,
                    market=scope["market"],
                    search=False,
                    policy_version=policy_version,
                ),
                search=False,
            )
            if judgment["status"] == "completed":
                try:
                    result.update(
                        {
                            "status": "completed",
                            "classification": classify(
                                answer["value"], judgment["value"], panel, ladder=ladder
                            ),
                            "judgment_receipt": {
                                "response_id": judgment["value"]["response_id"],
                                "usage": judgment["value"]["usage"],
                            },
                        }
                    )
                    if modern:
                        result["scoring_method"] = "evidence_checked_judge"
                except ValueError as exc:
                    result["reason"] = (
                        exc.reason
                        if modern and isinstance(exc, AuditValidationError)
                        else "invalid_answer_judgment"
                    )
                    if modern:
                        result["failure_stage"] = "grading"
            elif modern:
                result.update(
                    reason=judgment.get("reason", "judgment_invalid"), failure_stage="grading"
                )
        elif modern:
            result.update(reason=answer.get("reason", "not_recorded"), failure_stage="answer")
        if len(json.dumps(result, ensure_ascii=False).encode()) > audit_policy(policy_version).get(
            "max_observation_bytes", 21_000
        ):
            result = {
                "status": "unavailable",
                "index": index,
                "question_index": question_index,
                "repetition": 1 if memory else index % repetitions + 1,
                "answer": answer,
                "reason": "classification_exceeded_evidence_budget",
                **({"mode": "memory"} if memory else {}),
            }
            if modern:
                result["failure_stage"] = "grading"
        await self._save(run_id, f"observation:{index}", result)
        await self.db.project_run_progress(
            run_id=UUID(run_id),
            mode="units",
            step="ai_visibility",
            current=index + 1,
            total=panel["planned_observations"],
            summary="Observing the frozen buyer-question panel.",
        )

    async def _content_review(self, run_id: str) -> None:
        """One text-model review of the top content pages' answer structure, saved once."""
        from tin_lite.organic_audit_ai import ContentReview
        from tin_lite.organic_audit_content import model_input, review_pages, validate_review
        from tin_lite.organic_audit_report import query_rows

        scope = await self._result(run_id, "scope")
        policy_version = await self._policy_version(run_id)
        policy = audit_policy(policy_version)
        if (
            not policy.get("content_review_pages")
            or scope.get("completion")
            or await self._result(run_id, "content_review")
        ):
            return
        facts = await self.db.get_effect(self.key(run_id, "page_facts"))
        search = await self._result(run_id, "search_console") or {}
        pages = review_pages(
            facts=((facts.result or {}) if facts else {}).get("pages", {}),
            search_pages=search.get("value", {}).get("pages", [])
            if search.get("status") == "completed"
            else [],
            queries=query_rows(await self._result(run_id, "search_console_queries")),
            host=scope["host"],
            cap=policy["content_review_pages"],
        )
        if not pages:
            await self._save(
                run_id, "content_review", {"status": "not_available", "reason": "no_content_pages"}
            )
            return
        response = await self._model(
            run_id,
            "content_review:model",
            payload(
                stage="content_review",
                data=model_input(scope["url"], pages),
                schema=ContentReview,
                market=scope["market"],
                search=False,
                policy_version=policy_version,
            ),
            search=False,
        )
        result = {"status": "unavailable", "reason": response.get("reason", "not_recorded")}
        if response["status"] == "completed":
            try:
                result = {
                    "status": "completed",
                    "pages": validate_review(response["value"]["text"], pages),
                    "response_id": response["value"]["response_id"],
                }
            except ValueError:
                result = {"status": "unavailable", "reason": "content_review_invalid"}
        await self._save(run_id, "content_review", result)

    @activity.defn
    async def organic_brand_checks(self, run_id: str) -> None:
        await self._content_review(run_id)
        if await self._result(run_id, "brand_checks"):
            return
        panel = await self._result(run_id, "panel")
        if not panel or panel.get("status") != "completed":
            await self._save(run_id, "brand_checks", {"status": "unavailable", "observations": []})
            return
        scope = await self._result(run_id, "scope")
        policy_version = await self._policy_version(run_id)
        questions = [
            f"What does {panel['name']} at {scope['url']} do, and which buyers is it for?",
            "What publicly stated pricing and key limitations does "
            f"{panel['name']} at {scope['url']} have?",
        ]
        answers = []
        for index, question in enumerate(questions):
            result = await self._model(
                run_id,
                f"brand:{index}",
                payload(
                    stage="answer",
                    data=question,
                    market=scope["market"],
                    search=True,
                    policy_version=policy_version,
                ),
                search=True,
            )
            answers.append({"question": question, "result": result})
        await self._save(
            run_id,
            "brand_checks",
            {
                "status": "observed"
                if all(a["result"]["status"] == "completed" for a in answers)
                else "partial",
                "observations": answers,
                "note": "Qualitative branded probes, separate from the unbranded baseline.",
            },
        )

    @activity.defn
    async def organic_prepare_ai_engines(self, run_id: str) -> str | None:
        """organic-audit-v13: save this run's questions for ai_answers_measure.

        Returns the measurement's stage, or None when the pinned policy measures no engines
        or there is nothing to ask within the ceiling; the plan saved here says why. The
        prompts and brand stay in receipts, so Temporal carries only the stage name.
        """
        policy = audit_policy(await self._policy_version(run_id))
        if not policy.get("ai_engines"):
            return None
        saved = await self._result(run_id, "ai_engines")
        if saved is None:
            await self._active(run_id)
            plan, request = await self._engine_plan(run_id, policy)
            if request is not None:
                from tin_lite.ai_answers_activities import save_request

                await save_request(self.db, run_id=run_id, stage=plan["stage"], inputs=request)
            saved = await self._save(run_id, "ai_engines", plan)
        return saved.get("stage")

    async def _engine_plan(self, run_id: str, policy: dict) -> tuple[dict, dict | None]:
        from tin_lite import organic_audit_engines as engines
        from tin_lite.ai_answers import AIAnswersRequest

        scope = await self._result(run_id, "scope")
        panel = await self._result(run_id, "panel")
        plan = {
            "status": "not_measured",
            "stage": None,
            "engines": list(policy["ai_engines"]),
            "questions": len((panel or {}).get("questions") or []),
            "asked": 0,
        }
        if scope.get("completion"):
            return {**plan, "reason": "answer_completion"}, None
        ordered = engines.question_order(panel) if panel else []
        if not panel or panel.get("status") != "completed" or not ordered:
            return {**plan, "reason": "no_question_panel"}, None
        cap = Decimal(policy["ai_engines_max_cost_usd"])
        try:
            AIAnswersRequest.from_inputs(
                engines.request_inputs(panel, scope, policy, [t for _, t in ordered], cap)
            )
        except ValueError:
            return {**plan, "reason": "panel_not_measurable"}, None
        each = engines.per_question_usd(policy)

        def fit(left: Decimal) -> Decimal | None:
            fitting = min(len(ordered), int(min(cap, left) // each)) if left > 0 else 0
            return each * fitting if fitting else None

        reserved = await self._reserve_fitting(run_id, "ai_engines", fit)
        if reserved is None:
            return {**plan, "reason": "cost_ceiling"}, None
        asked = ordered[: int(reserved / each)]
        plan = {
            **plan,
            "status": "planned",
            "reason": None,
            "stage": engines.STAGE,
            "asked": len(asked),
            "question_indexes": [index for index, _ in asked],
            "not_asked": sorted(index for index, _ in ordered[len(asked) :]),
            "max_cost_usd": str(reserved),
            "priority": policy["ai_engines_priority"],
        }
        request = engines.request_inputs(panel, scope, policy, [t for _, t in asked], reserved)
        return plan, request

    @activity.defn
    async def organic_publish(self, run_id: str) -> None:
        key = self.key(run_id, "publish")
        async with self.db.effect_lock(key, "organic.audit") as (conn, existing):
            if existing and existing.status == "completed":
                return
            run = await self._active(run_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            artifacts = await self._result(run_id, "artifacts")
            if not artifacts:
                scope, crawl = (
                    await self._result(run_id, "scope"),
                    await self._result(run_id, "crawl"),
                )
                panel = await self._result(run_id, "panel")
                panel = panel if panel and panel.get("status") == "completed" else None
                observations = [
                    await self._result(run_id, f"observation:{index}")
                    or {"status": "unavailable", "index": index}
                    for index in range(panel["planned_observations"] if panel else 0)
                ]
                budget = await self.db.get_effect(self.key(run_id, "budget"))
                policy_version = await self._policy_version(run_id)
                ai = summarize(panel, observations, policy_version=policy_version)
                ai["public_research"] = await self._result(run_id, "panel_research")
                ai["panel_validation"] = await self._result(run_id, "panel_validation")
                if grounded_preparation(policy_version):
                    ai["preparation"] = await self._result(run_id, "panel_preparation")
                    ai["preparation_receipts"] = {
                        stage: await self._result(run_id, stage)
                        for stage in (
                            "panel_research_recovery",
                            "panel_draft",
                            "panel_draft_recovery",
                            "panel_validation_recovery",
                        )
                    }
                ai["brand_checks"] = await self._result(run_id, "brand_checks")
                if audit_policy(policy_version).get("ai_engines"):
                    from tin_lite.ai_answers_activities import read_result
                    from tin_lite.organic_audit_engines import STAGE, results

                    ai["engines"] = results(
                        await self._result(run_id, "ai_engines"),
                        await read_result(self.db, run_id=run_id, stage=STAGE),
                    )
                # Only v10 question sets carry an answer count and can be reused or compared.
                if (
                    panel
                    and "repetitions" in panel
                    and audit_policy(policy_version).get("reuse_questions")
                ):
                    preparation = ai.get("preparation") or {}
                    ai["question_set"] = {
                        "sha256": panel["sha256"],
                        "questions": len(panel["questions"]),
                        "repetitions": panel_repetitions(panel),
                        "method": preparation.get("method"),
                        "source_run_id": preparation.get("source_run_id"),
                    }
                    baseline = await self._result(run_id, "panel_baseline")
                    if baseline and baseline["panel_sha256"] == panel["sha256"]:
                        ai["comparison"] = {
                            "baseline": baseline,
                            "current": question_results(panel, observations),
                        }
                documents = build_documents(
                    run_id=run_id,
                    project_id=str(run.project_id),
                    definition_sha=run.definition_commit_sha,
                    scope=scope,
                    crawl=crawl,
                    ai=ai,
                    spending={
                        "reservations_usd": budget.result if budget else {},
                        "limit_usd": scope["max_cost_usd"],
                        "note": "Reservations retained; not a final provider invoice.",
                    },
                    policy_version=policy_version,
                    search_console=await self._result(run_id, "search_console"),
                    **(
                        await self._site_documents_input(run_id)
                        if audit_policy(policy_version).get("site_checks")
                        else {}
                    ),
                )
                artifacts = await self._save(
                    run_id,
                    "artifacts",
                    {path: content.decode() for path, content in documents.items()},
                )
            documents = {path: content.encode() for path, content in artifacts.items()}
            summary = None
            if await self._policy_version(run_id) != LEGACY_AUDIT_POLICY["version"]:
                evidence = json.loads(artifacts[audit_paths(run_id)["evidence.json"]])
                crawl, ai = evidence["crawl"], evidence["ai_visibility"]
                partial = crawl["status"] != "completed" or ai["status"] != "completed"
                inventory = json.loads(artifacts[audit_paths(run_id)["findings.json"]])
                partial = partial or inventory.get("evidence_status") == "partial"
                coverage = inventory.get("coverage")
                if coverage:
                    partial = partial or coverage["status"] == "partial"
                    if coverage["sitemap_read"]:
                        inspected = (
                            f" Inspected {coverage['inspected_sitemap_pages']} of "
                            f"{coverage['sitemap_pages']} sitemap pages; "
                        )
                    elif coverage.get("site_collected", True):
                        inspected = (
                            f" Inspected {count(coverage['inspected_pages'], 'page')}; "
                            "no readable sitemap; "
                        )
                    else:
                        inspected = f" Crawled {count(coverage['inspected_pages'], 'page')}; "
                else:
                    inspected = f" Inspected {len(crawl.get('pages', []))} pages; "
                summary = (
                    "Organic visibility audit is ready"
                    + (" with partial evidence." if partial else ".")
                    + inspected
                    + (
                        f"scored {ai['completed']}/{ai['planned']} AI observations."
                        if ai["planned"]
                        else "AI visibility was not measured."
                    )
                )
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")

            async def save_intent(intent):
                await self.db.save_publication_intent(conn, execution_key=key, intent=intent)

            async def validate_active():
                await self._active(run_id, conn=conn)

            completion = bool((await self._result(run_id, "scope") or {}).get("completion"))
            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_audit(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    run_id=run_id,
                    documents=documents,
                    intent=(existing.result or {}).get("publication") if existing else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                    policy_version=await self._policy_version(run_id),
                    completion=completion,
                )
            summary_file = summary_paths(run_id)["SUMMARY.json"]
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "canonical_commit_sha": revision,
                    "artifact_path": audit_paths(run_id)["AUDIT.md"],
                    "documents_sha256": bundle_sha256(run_id, artifacts),
                    **({"summary": summary} if summary is not None else {}),
                    # v12: the summary for code workflows, outside the verified bundle.
                    **(
                        {
                            "summary_path": summary_file,
                            "summary_sha256": hashlib.sha256(
                                artifacts[summary_file].encode()
                            ).hexdigest(),
                        }
                        if summary_file in artifacts
                        else {}
                    ),
                },
            )

    async def _site_documents_input(self, run_id: str) -> dict:
        """Saved v10 evidence for the report; incomplete progress is reported as partial."""
        files = await self._result(run_id, "site_files")
        if files is None:
            return {"search_queries": await self._result(run_id, "search_console_queries")}
        facts = await self.db.get_effect(self.key(run_id, "page_facts"))
        progress = (facts.result or {}) if facts else {}
        pagespeed = await self._result(run_id, "pagespeed")
        scope = await self._result(run_id, "scope")
        if pagespeed is None and scope.get("pagespeed") != "configured":
            pagespeed = {"status": "not_configured", "results": []}
        if pagespeed is None:
            pagespeed = {"status": "not_collected", "results": []}
            for index in range(audit_policy(scope["policy_version"])["pagespeed_max_urls"]):
                saved = await self._result(run_id, f"pagespeed:{index}")
                if saved:
                    pagespeed["results"].append(saved)
                    pagespeed["status"] = "partial"
        inspection = await self._result(run_id, "url_inspection")
        if inspection is None:
            inspection = {"status": "not_collected", "results": []}
            for index in range(
                audit_policy(scope["policy_version"]).get("url_inspection_max_urls", 0)
            ):
                saved = await self._result(run_id, f"url_inspection:{index}")
                if saved and saved.get("status") == "completed":
                    inspection["results"].append(saved["value"])
                    inspection["status"] = "partial"
        return {
            "search_queries": await self._result(run_id, "search_console_queries"),
            "search_previous": await self._result(run_id, "search_console_previous"),
            "site": {
                "files": files,
                "plan": await self._result(run_id, "crawl_plan"),
                "pages": list(progress.get("pages", {}).values()),
                "pages_status": "complete" if facts and facts.status == "completed" else "partial",
                "pagespeed": pagespeed,
                "access": await self._result(run_id, "access") or {"status": "not_collected"},
                "url_inspection": inspection,
                "content_review": await self._result(run_id, "content_review")
                or {"status": "not_collected"},
            },
        }

    @activity.defn
    async def organic_project(self, run_id: str) -> None:
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, "organic.audit") as (conn, existing):
            if existing and existing.status == "completed":
                return
            run = await self._active(run_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            publication = await self._result(run_id, "publish")
            if not publication or publication["artifact_path"] != audit_paths(run_id)["AUDIT.md"]:
                raise ApplicationError(
                    "Audit has no durable publication receipt.", non_retryable=True
                )
            await self.db.start_effect(conn, execution_key=key, operation="organic.audit")
            ref = f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}/{publication['artifact_path']}"
            await self.db.complete_organic_audit_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=publication["canonical_commit_sha"],
                artifact_path=publication["artifact_path"],
                artifact_ref=ref,
                summary=publication.get("summary"),
            )

    @activity.defn
    async def organic_failure(self, run_id: str) -> None:
        await self.organic_end_crawl(run_id)
        await self.db.project_failure(
            run_id=UUID(run_id),
            error_message=(
                "Organic audit could not finish. Check DataForSEO configuration "
                "and the explicit audit spending limit. Paid requests with uncertain outcomes "
                "were not blindly repeated; the website was not changed."
            ),
        )
