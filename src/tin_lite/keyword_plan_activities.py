"""Trusted keyword research activities; Temporal carries identifiers only."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v3 as v3
from tin_lite import keyword_plan_v4 as v4
from tin_lite import keyword_plan_v5 as v5
from tin_lite.integrations import GSC_PROVIDER
from tin_lite.keyword_data import ENDPOINTS, KeywordData, request_for
from tin_lite.keyword_plan import (
    INSTRUCTIONS,
    KEY,
    LIMITS,
    POLICY,
    ROUTE_KEY,
    SCHEMAS,
    Seeds,
    build_documents,
    check_inputs,
    gsc_property_matches,
    gsc_rows,
    host,
    keyword_rows,
    paths,
    phrases,
    receipt_evidence,
    review_input,
    select_candidates,
    serp_items,
    validate_review,
)
from tin_lite.model_providers import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    model_failure_reason,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.organic_audit import ARTIFACT_LIMITS, audit_hosts, audit_paths, canonical_json, digest
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.usage_capture import external_usage_scope
from tin_lite.workflow_evidence import integration_inventory

# How long the worker waits for one model response, by stage. Kept outside the pinned policy:
# waiting longer never changes the request. Screening and review return one verdict for every
# candidate, so they can run for minutes; seed proposals keep the provider's default wait.
MODEL_TIMEOUT_SECONDS = {"triage": 240, "review": 420}
STAGE_NAMES = {"seeds": "seed proposal", "triage": "keyword screening", "review": "keyword review"}

CONTRACTS = {
    POLICY["version"]: (POLICY, INSTRUCTIONS, SCHEMAS),
    v2.POLICY["version"]: (v2.POLICY, v2.INSTRUCTIONS, v2.SCHEMAS),
    v3.POLICY["version"]: (v3.POLICY, v3.INSTRUCTIONS, v3.SCHEMAS),
    v4.POLICY["version"]: (v4.POLICY, v4.INSTRUCTIONS, v4.SCHEMAS),
    v5.POLICY["version"]: (v5.POLICY, v5.INSTRUCTIONS, v5.SCHEMAS),
}


def modern_scope(scope):
    return scope.get("policy_version") in {
        v2.POLICY["version"],
        v3.POLICY["version"],
        v4.POLICY["version"],
        v5.POLICY["version"],
    }


class KeywordPlanActivities:
    def __init__(
        self, *, database, storage, settings, router=None, integrations=None, provider=None
    ):
        self.db, self.storage, self.settings = database, storage, settings
        self.router, self.integrations = router, integrations
        login, password = (
            getattr(settings, "dataforseo_login", None),
            getattr(settings, "dataforseo_password", None),
        )
        self.provider = provider or (
            KeywordData(login.get_secret_value(), password.get_secret_value())
            if login and password
            else None
        )

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"keyword:{UUID(run_id)}:{stage}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(run_id), conn=conn)
        if (
            run is None
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "succeeded"}
        ):
            raise ApplicationError("Keyword planning is not active.", non_retryable=True)
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

    async def _paid(
        self, run_id: str, stage: str, request: dict, amount: str, call, *, classify=None
    ):
        key, fingerprint = self.key(run_id, stage), digest(request)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            saved = (existing.result or {}) if existing else {}
            if saved and saved.get("request_sha256") != fingerprint:
                raise ApplicationError(
                    "Keyword request changed after recording its intent.", non_retryable=True
                )
            if existing and existing.status == "completed":
                return saved
            run = await self._active(run_id, conn=conn)
            if run.status.value == "succeeded":
                raise ApplicationError(
                    "Completed runs cannot start new research.", non_retryable=True
                )
            if (
                self.provider is None
                or self.router is None
                or not getattr(self.settings, "luna_api_key", None)
            ):
                raise ApplicationError(
                    "Keyword research providers are no longer configured.", non_retryable=True
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
                        model_usage_scope(run_id=run_id, step=f"keyword:{stage}", conn=conn),
                        external_usage_scope(self.db, conn, run_id, stage, maximum_usd=amount),
                    ):
                        value = await call()
                    result = {"status": "completed", "value": value}
                except Exception as exc:
                    from tin_lite.billing_contracts import BillingError

                    # Raw SDK/provider errors, headers and credentials never enter evidence;
                    # a model call keeps only a fixed label for why it failed.
                    result = (
                        {"status": "unavailable", "reason": "spending_limit"}
                        if isinstance(exc, BillingError)
                        else {
                            "status": "unknown",
                            "reason": classify(exc) if classify else "provider_result_unavailable",
                        }
                    )
            result = {"request_sha256": fingerprint, **saved, **result}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    async def _model(self, run_id: str, stage: str, data: dict, *, schema=None):
        scope = await self._result(run_id, "scope")
        policy, instructions, schemas = CONTRACTS[scope.get("policy_version", POLICY["version"])]
        encoded = canonical_json(data).decode()
        if len(encoded.encode()) > POLICY["max_model_input_bytes"]:
            raise ApplicationError("Keyword review input exceeds its bound.", non_retryable=True)
        request = ModelRequest(
            messages=(ModelMessage(role=MessageRole.USER, content=encoded),),
            system=instructions[stage],
            output_schema=schema if schema is not None else schemas[stage],
            output_schema_name=f"keyword_{stage}",
            max_output_tokens=policy[f"{'seed' if stage == 'seeds' else stage}_output_tokens"],
        )

        timeout = MODEL_TIMEOUT_SECONDS.get(stage)

        async def call():
            async with asyncio.timeout(timeout + 30 if timeout else 120):
                result = await self.router.generate(ROUTE_KEY, request, timeout_seconds=timeout)
            parsed = result.parsed
            if not isinstance(parsed, dict):
                raise ValueError("Keyword model result must be a JSON object.")
            return {
                "data": parsed,
                "usage": asdict(result.usage),
                "request_id": result.request_id,
                "model": result.model,
                "provider": result.provider.value,
            }

        result = await self._paid(
            run_id,
            stage,
            {"route": ROUTE_KEY, **asdict(request)},
            policy[f"{'seed' if stage == 'seeds' else stage}_reservation_usd"],
            call,
            classify=model_failure_reason,
        )
        if result["status"] != "completed":
            await self._save(
                run_id,
                "failure",
                {"code": "model_unavailable", "stage": stage, "reason": result.get("reason")},
            )
            raise ApplicationError(
                "The keyword model result was unavailable; no replacement was purchased.",
                non_retryable=True,
            )
        return result["value"]["data"]

    async def _query(self, run_id: str, stage: str, kind: str, value):
        scope = await self._result(run_id, "scope")
        request = request_for(
            kind, market=scope["market"], value=value, tag=self.key(run_id, stage)
        )

        async def call():
            raw = await self.provider.query(
                kind, market=scope["market"], value=value, tag=request["tag"]
            )
            metadata = {
                key: raw.get(key)
                for key in ("reported_cost_usd", "provider_task_id", "items_count", "total_count")
            }
            observed = datetime.now(UTC).isoformat()
            if kind == "serp":
                items = serp_items(raw["items"])
                compact = {
                    "items": items,
                    "organic_rows_omitted": max(
                        0, sum(item.get("type") == "organic" for item in raw["items"]) - len(items)
                    ),
                }
            elif kind == "competitors":
                domains = []
                for item in raw["items"]:
                    try:
                        domain = host(item["domain"]).removeprefix("www.")
                    except (KeyError, TypeError, ValueError):
                        continue
                    if domain not in domains and domain != scope["host"].removeprefix("www."):
                        domains.append(domain)
                compact = {"domains": domains[:5]}
            else:
                compact = {
                    "rows": keyword_rows(
                        raw["items"], source_id=stage, observed_at=observed, market=scope["market"]
                    )
                }
                if raw["items"] and not compact["rows"]:
                    raise ValueError("Keyword rows could not be normalized.")
                if kind == "ranked_relevant":
                    compact["rows"] = [
                        row
                        for row in compact["rows"]
                        if any(
                            seed.casefold() in row["keyword"].casefold() for seed in value["seeds"]
                        )
                    ]
                compact["rows_omitted"] = len(raw["items"]) - len(compact["rows"])
            return {
                **metadata,
                **compact,
                "observed_at": observed,
                "endpoint": ENDPOINTS[kind],
                "request": request,
            }

        amount = POLICY["serp_reservation_usd" if kind == "serp" else "labs_reservation_usd"]
        result = await self._paid(
            run_id, stage, {"endpoint": ENDPOINTS[kind], "request": request}, amount, call
        )
        if result["status"] == "completed" and Decimal(
            str(result["value"]["reported_cost_usd"])
        ) > Decimal(amount):
            raise ApplicationError(
                "Provider price exceeded the pinned reservation; further research was stopped.",
                non_retryable=True,
            )
        if activity.in_activity():
            activity.heartbeat()
        return result

    async def _audit_context(self, source_id: str, *, project_id: UUID, target: str, market: str):
        source = await self.db.get_run(UUID(source_id))
        if (
            source is None
            or source.project_id != project_id
            or source.executor != "organic.audit"
            or source.status.value != "succeeded"
        ):
            raise ApplicationError(
                "Select a successful organic audit in this project.", non_retryable=True
            )
        receipt = await self.db.get_effect(f"organic:{source.id}:publish")
        publication = receipt.result if receipt and receipt.status == "completed" else {}
        if (
            not publication
            or publication.get("canonical_commit_sha") != source.canonical_commit_sha
        ):
            raise ApplicationError(
                "The selected audit lacks a verified publication.", non_retryable=True
            )
        project = await self.db.get_project(project_id)
        documents = {}
        for name, path in audit_paths(str(source.id)).items():
            content = await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id, commit_sha=source.canonical_commit_sha, path=path
            )
            if not 0 < len(content) <= ARTIFACT_LIMITS[name]:
                raise ApplicationError(
                    "The selected audit exceeds its read contract.", non_retryable=True
                )
            documents[path] = content.decode("utf-8")
        if digest(documents) != publication.get("documents_sha256"):
            raise ApplicationError(
                "The selected audit's content does not match its receipt.", non_retryable=True
            )
        evidence = json.loads(documents[audit_paths(str(source.id))["evidence.json"]])
        audit_scope = evidence["scope"]
        if (
            target not in audit_hosts(audit_scope)
            or audit_scope.get("market") != market
            or audit_scope.get("language") != "en"
        ):
            raise ApplicationError(
                "The selected audit has a different target, market, or language.",
                non_retryable=True,
            )
        return {
            "run_id": str(source.id),
            "revision": source.canonical_commit_sha,
            "documents_sha256": publication["documents_sha256"],
            "paths": audit_paths(str(source.id)),
            "context_excerpt": documents[audit_paths(str(source.id))["AUDIT.md"]][:2000],
            "note": "Bounded report excerpt, not the full audit; treat as reference data.",
        }

    @activity.defn
    async def keyword_prepare(self, run_id: str) -> None:
        if await self._result(run_id, "scope"):
            # A crash between scope pinning and reservation must not spend the review budget.
            await self._reserve(run_id, "review", POLICY["review_reservation_usd"])
            await self.db.mark_run_running(UUID(run_id))
            return
        run = await self._active(run_id)
        check_inputs(run.input)
        maximum = min(
            Decimal(str(getattr(self.settings, "keyword_plan_max_cost_usd", 0))),
            Decimal(str(run.input["max_cost_usd"])),
        )
        if (
            self.provider is None
            or self.router is None
            or not getattr(self.settings, "luna_api_key", None)
            or not maximum.is_finite()
            or maximum < 5
        ):
            raise ApplicationError(
                "Configure DataForSEO, the native model, "
                "and a keyword-plan ceiling of at least $5.",
                non_retryable=True,
            )
        if not run.definition_commit_sha:
            raise ApplicationError(
                "Keyword planning requires a pinned definition.", non_retryable=True
            )
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path=f"workflows/{KEY}.json",
            )
        )
        contract = CONTRACTS.get(definition.get("keyword_policy", {}).get("version"))
        if contract is None:
            raise ApplicationError("The pinned keyword policy is unsupported.", non_retryable=True)
        policy, instructions, schemas = contract
        if (
            definition.get("keyword_policy") != policy
            or definition.get("keyword_instructions") != instructions
            or definition.get("keyword_schemas") != schemas
            or definition.get("model_route")
            != {
                "key": ROUTE_KEY,
                "provider": "openai",
                "model": POLICY["model"],
                "capabilities": ["json_schema", "text"],
            }
        ):
            raise ApplicationError(
                "The pinned keyword definition is not supported by this worker.", non_retryable=True
            )
        url, target = await self.provider.validate_target(run.input["site_url"])
        scope = {
            "url": url,
            "host": target,
            "market": run.input["market"],
            "language": "en",
            "buyer_context": run.input["buyer_context"],
            "started_at": run.created_at.isoformat(),
            "max_cost_usd": str(maximum),
            "policy_version": policy["version"],
        }
        if policy["version"] == v5.POLICY["version"]:
            scope["integrations"] = await integration_inventory(self.db, run.project_id)
        if run.input.get("audit_run_id"):
            scope["audit"] = await self._audit_context(
                run.input["audit_run_id"],
                project_id=run.project_id,
                target=target,
                market=scope["market"],
            )
        scope["gsc"] = {"status": "not_requested"}
        if run.input.get("use_search_console"):
            connection = await self.db.get_integration_connection(
                project_id=run.project_id, provider_key=GSC_PROVIDER
            )
            property_url = (
                connection.configuration.get("selected_site_url", "") if connection else ""
            )
            if (
                connection
                and connection.status == "connected"
                and gsc_property_matches(property_url, target)
            ):
                end = run.created_at.date() - timedelta(days=3)
                scope["gsc"] = {
                    "status": "selected",
                    "property": property_url,
                    "start_date": (end - timedelta(days=89)).isoformat(),
                    "end_date": end.isoformat(),
                }
            else:
                scope["gsc"] = {
                    "status": "unavailable",
                    "reason": "No connected Search Console property matches this exact website.",
                }
        await self._save(run_id, "scope", scope)
        await self._reserve(run_id, "review", POLICY["review_reservation_usd"])
        if modern_scope(scope):
            await self._reserve(run_id, "triage", policy["triage_reservation_usd"])
        await self.db.mark_run_running(run.id)

    async def _gsc(self, run_id: str):
        scope = await self._result(run_id, "scope")
        selection = scope["gsc"]
        if selection["status"] != "selected":
            return await self._save(run_id, "gsc", selection)
        run = await self._active(run_id)

        async def call():
            raw = await self.integrations.search_console_analytics(
                project_id=run.project_id,
                start_date=selection["start_date"],
                end_date=selection["end_date"],
                dimensions=("query", "page", "country"),
                row_limit=POLICY["gsc_rows"],
                expected_site_url=selection["property"],
                execution_key=self.key(run_id, "gsc:read"),
                run_id=run.id,
            )
            rows = raw.get("rows", [])
            if (
                not isinstance(rows, list)
                or len(rows) > POLICY["gsc_rows"]
                or any(not isinstance(item, dict) for item in rows)
            ):
                raise ValueError("Search Console exceeded its row contract.")
            return {
                "rows": gsc_rows(
                    rows,
                    target=scope["host"],
                    market=scope["market"],
                    observed_at=datetime.now(UTC).isoformat(),
                ),
                "returned_rows": len(rows),
                "property": selection["property"],
                "note": "Top query/page/country rows, filtered to the exact host and market; "
                "not all search queries.",
            }

        return await self._paid(run_id, "gsc", selection, "0", call)

    async def _reuse_collected_research(self, run) -> bool:
        """An explicit retry can keep the finished research for the exact same job."""
        if run.retry_of_run_id is None:
            return False
        previous = await self.db.get_run(run.retry_of_run_id)
        if (
            previous is None
            or previous.status.value != "failed"
            or previous.project_id != run.project_id
            or previous.workflow_id != run.workflow_id
            or previous.definition_commit_sha != run.definition_commit_sha
            or previous.input != run.input
        ):
            return False
        source_id, run_id = str(previous.id), str(run.id)
        collection = await self._result(source_id, "collection")
        if not collection:
            return False
        scope = await self._result(run_id, "scope")

        def request_fingerprint(owner, stage, keyword):
            return digest(
                {
                    "endpoint": ENDPOINTS["serp"],
                    "request": request_for(
                        "serp", market=scope["market"], value=keyword, tag=self.key(owner, stage)
                    ),
                }
            )

        for index, candidate in enumerate(self._sample_candidates(collection["candidates"])):
            stage = f"serp:{index}"
            saved = await self.db.get_effect(self.key(source_id, stage))
            if saved is None:
                continue  # An unattempted read is still eligible for normal execution.

            record = saved.result or {}
            if saved.operation != KEY or record.get("request_sha256") != request_fingerprint(
                source_id, stage, candidate["keyword"]
            ):
                raise ApplicationError(
                    "Saved keyword research does not match this retry.", non_retryable=True
                )
            if saved.status != "completed":
                record = {"status": "unknown", "reason": "unconfirmed_previous_request"}
            await self._save(
                run_id,
                stage,
                {
                    **record,
                    "request_sha256": request_fingerprint(run_id, stage, candidate["keyword"]),
                    "reused_from_run_id": source_id,
                },
            )
        # Write the collection last: an interrupted copy can finish before collect skips work.
        await self._save(run_id, "collection", {**collection, "reused_from_run_id": source_id})
        return True

    @activity.defn
    async def keyword_collect(self, run_id: str) -> None:
        if await self._result(run_id, "collection"):
            return
        run = await self._active(run_id)
        if await self._reuse_collected_research(run):
            return
        if not await self._reserve(run_id, "review", POLICY["review_reservation_usd"]):
            raise ApplicationError("Keyword review budget is unavailable.", non_retryable=True)
        scope = await self._result(run_id, "scope")
        modern = modern_scope(scope)
        if modern and not await self._reserve(
            run_id, "triage", v2.POLICY["triage_reservation_usd"]
        ):
            raise ApplicationError("Keyword screening budget is unavailable.", non_retryable=True)
        await self.db.project_run_progress(
            run_id=run.id,
            mode="indeterminate",
            step="research",
            summary="Researching buyer queries and search competitors.",
        )
        if run.input.get("seed_phrases"):
            seeds = phrases(run.input["seed_phrases"])
        else:
            core_seeds = scope.get("policy_version") in {
                v3.POLICY["version"],
                v4.POLICY["version"],
                v5.POLICY["version"],
            }
            value = await self._model(
                run_id, "seeds", v3.seed_input(scope) if core_seeds else scope
            )
            try:
                seeds = (
                    v5.seed_values(value)
                    if scope.get("policy_version") == v5.POLICY["version"]
                    else v3.seed_values(value)
                    if core_seeds
                    else phrases(Seeds.model_validate(value).seeds)
                )
            except ValueError:
                raise ApplicationError(
                    "The saved seed proposal failed validation.", non_retryable=True
                ) from None
        seed_record = await self._save(run_id, "seed_selection", {"seeds": seeds})
        seeds = seed_record["seeds"]
        sources = {}
        sources["target"] = await self._query(run_id, "target", "ranked", scope["host"])
        supplied = list(
            dict.fromkeys(
                host(value).removeprefix("www.") for value in run.input.get("competitor_hosts", [])
            )
        )
        if supplied:
            competitors = supplied
        else:
            sources["competitors"] = await self._query(
                run_id, "competitors", "competitors", scope["host"]
            )
            competitors = (
                sources["competitors"]
                .get("value", {})
                .get("domains", [])[: POLICY["max_competitors"]]
            )
        for index, competitor in enumerate(competitors):
            sources[f"competitor:{index}"] = await self._query(
                run_id,
                f"competitor:{index}",
                "ranked_relevant" if modern else "ranked",
                {"host": competitor, "seeds": seeds} if modern else competitor,
            )
        sources["seed_metrics"] = await self._query(run_id, "seed_metrics", "overview", seeds)
        for index, seed in enumerate(seeds):
            for kind in ("suggestions" if modern else "ideas", "related"):
                name = f"{kind}:{index}"
                sources[name] = await self._query(run_id, name, kind, seed)
        sources["gsc"] = await self._gsc(run_id)
        usable = {
            name: source["value"].get("rows", [])
            for name, source in sources.items()
            if source.get("status") == "completed" and "rows" in source["value"]
        }
        if not usable:
            raise ApplicationError(
                "No keyword observations were available; a plan was not fabricated.",
                non_retryable=True,
            )
        if modern:
            # Seed proposals are retained even when absent from the provider's database.
            # They carry no fabricated provider metrics or implied measured demand.
            proposals = keyword_rows(
                [{"keyword": seed} for seed in seeds],
                source_id="seed_proposals",
                observed_at=scope["started_at"],
                market=scope["market"],
            )
            for row in proposals:
                row["basis"] = (
                    "user_supplied" if run.input.get("seed_phrases") else "model_proposed"
                )
            usable = {"seed_proposals": proposals, **usable}
            sources["seed_proposals"] = {
                "status": "completed",
                "value": {
                    "rows": proposals,
                    "provider": "tin",
                    "note": "Proposed queries, not measured search demand.",
                },
            }
        candidates, coverage = select_candidates(usable, limit=POLICY["max_candidates"])
        notes = [
            "Provider lookups are capped; this is not an exhaustive keyword inventory.",
            "Search competitors are overlap-based suggestions, "
            "not verified commercial competitors.",
            "No competitor page content was fetched. "
            "Volume and difficulty are provider estimates, not traffic.",
        ]
        for name, result in sources.items():
            if result.get("status") not in {"completed", "not_requested"}:
                notes.append(f"{name}: {result.get('reason', 'unavailable')}.")
        if scope["gsc"]["status"] == "selected":
            notes.append(
                "Search Console uses a bounded 90-day query/page sample, "
                "filtered to the exact host and market; coverage is not complete."
            )
        coverage["notes"] = notes
        if modern:
            notes += [
                "Competitor footprints are seed-filtered, not whole-domain rankings.",
                "Seed proposals remain explicit hypotheses when provider metrics are unknown.",
                "Buyer relevance is model-assessed before choosing search-result samples; "
                "it is not proven demand.",
            ]
            await self.db.project_run_progress(
                run_id=run.id,
                mode="indeterminate",
                step="screening",
                summary="Screening keywords for the product's actual buyers.",
            )
            if candidates:
                labels = await self._model(
                    run_id,
                    "triage",
                    v2.triage_input(scope, candidates),
                    schema=v2.model_schema("triage", candidates),
                )
                try:
                    candidates = v2.qualified_candidates(labels, candidates)
                except ValueError:
                    await self._save(run_id, "failure", {"code": "validation", "stage": "triage"})
                    raise ApplicationError(
                        "The saved keyword screening failed validation.", non_retryable=True
                    ) from None
            coverage["buyer_fit"] = {
                fit: sum(row["buyer_fit"] == fit for row in candidates) for fit in v2.FIT
            }
        await self._save(
            run_id,
            "collection",
            {
                "seeds": seeds,
                "competitors": competitors,
                "sources": sources,
                "candidates": candidates,
                "coverage": coverage,
            },
        )

    @activity.defn
    async def keyword_sample_count(self, run_id: str) -> int:
        collection = await self._result(run_id, "collection")
        return len(self._sample_candidates(collection["candidates"]))

    @staticmethod
    def _sample_candidates(candidates):
        return (
            v2.sample_candidates(candidates)
            if candidates and "buyer_fit" in candidates[0]
            else candidates[: POLICY["max_serps"]]
        )

    @activity.defn
    async def keyword_inspect(self, control: dict) -> None:
        run_id, index = control["run_id"], control["index"]
        collection = await self._result(run_id, "collection")
        selected = self._sample_candidates(collection["candidates"])
        if type(index) is not int or not 0 <= index < len(selected):
            raise ApplicationError("Keyword sample index is invalid.", non_retryable=True)
        candidate = selected[index]
        await self._query(run_id, f"serp:{index}", "serp", candidate["keyword"])
        await self.db.project_run_progress(
            run_id=UUID(run_id),
            mode="units",
            step="serps",
            current=index + 1,
            total=len(selected),
            summary="Inspecting a bounded sample of search results.",
        )

    async def _samples(self, run_id: str, candidates: list):
        samples, receipts = {}, {}
        for index, candidate in enumerate(self._sample_candidates(candidates)):
            receipt = await self._result(run_id, f"serp:{index}")
            receipt = receipt or {"status": "unavailable", "reason": "not_recorded"}
            receipts[f"serp:{index}"] = receipt
            samples[candidate["id"]] = {
                "status": receipt["status"],
                **receipt.get("value", {}),
                **({"reason": receipt["reason"]} if "reason" in receipt else {}),
            }
        return samples, receipts

    @activity.defn
    async def keyword_review(self, run_id: str) -> None:
        if await self._result(run_id, "review_validated"):
            return
        collection = await self._result(run_id, "collection")
        candidates = collection["candidates"]
        samples, _ = await self._samples(run_id, candidates)
        scope = await self._result(run_id, "scope")
        await self.db.project_run_progress(
            run_id=UUID(run_id),
            mode="indeterminate",
            step="review",
            summary="Reviewing keyword intent, buyer fit, and evidence gaps.",
        )
        modern = modern_scope(scope)
        data = (v2.review_input if modern else review_input)(
            scope=scope, candidates=candidates, samples=samples, coverage=collection["coverage"]
        )
        eligible = (
            [row for row in candidates if row["buyer_fit"] in v2.ELIGIBLE] if modern else candidates
        )
        value = (
            await self._model(
                run_id,
                "review",
                data,
                schema=v2.model_schema("review", eligible) if modern else None,
            )
            if eligible
            else {"groups": [], "excluded": []}
        )
        try:
            validated = (v2.expand_review if modern else validate_review)(value, candidates)
        except ValueError:
            await self._save(run_id, "failure", {"code": "validation", "stage": "review"})
            raise ApplicationError(
                "The saved keyword review failed validation.", non_retryable=True
            ) from None
        await self._save(run_id, "review_validated", validated)

    @activity.defn
    async def keyword_publish(self, run_id: str) -> None:
        key = self.key(run_id, "publish")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            run = await self._active(run_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            artifacts = await self._result(run_id, "artifacts")
            collection = await self._result(run_id, "collection")
            review = await self._result(run_id, "review_validated")
            if artifacts is None:
                scope = await self._result(run_id, "scope")
                samples, receipts = await self._samples(run_id, collection["candidates"])
                budget = await self.db.get_effect(self.key(run_id, "budget"))
                docs = build_documents(
                    run_id=run_id,
                    project_id=str(run.project_id),
                    definition_sha=run.definition_commit_sha,
                    scope=scope,
                    candidates=collection["candidates"],
                    samples=samples,
                    review=review,
                    coverage=collection["coverage"],
                    evidence={
                        **(
                            {"reused_research_from_run_id": collection["reused_from_run_id"]}
                            if collection.get("reused_from_run_id")
                            else {}
                        ),
                        "collection": {
                            stage: receipt_evidence(value)
                            for stage, value in collection["sources"].items()
                        },
                        "serp_calls": {
                            stage: receipt_evidence(value) for stage, value in receipts.items()
                        },
                        "models": {
                            stage: receipt_evidence(await self._result(run_id, stage))
                            for stage in (
                                ("seeds", "triage", "review")
                                if modern_scope(scope)
                                else ("seeds", "review")
                            )
                        },
                        "seeds": collection["seeds"],
                        "competitors": collection["competitors"],
                        "reservations_usd": budget.result if budget else {},
                        "note": "Normalized bounded observations; reservations are not an invoice.",
                    },
                )
                artifacts = await self._save(
                    run_id, "artifacts", {path: content.decode() for path, content in docs.items()}
                )
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
                    documents={path: content.encode() for path, content in artifacts.items()},
                    paths=paths(run_id),
                    limits=LIMITS,
                    message=f"{KEY} {run_id} [{key}]",
                    intent=(existing.result or {}).get("publication") if existing else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                )
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "canonical_commit_sha": revision,
                    "artifact_path": paths(run_id)["PLAN.md"],
                    "documents_sha256": digest(artifacts),
                    "summary": (
                        f"Keyword plan is ready: {len(collection['candidates'])} candidates, "
                        f"{len(review['groups'])} reviewable groups. "
                        "Coverage limits are in the report."
                    ),
                },
            )

    @activity.defn
    async def keyword_project(self, run_id: str) -> None:
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            run = await self._active(run_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            publication = await self._result(run_id, "publish")
            if not publication or publication["artifact_path"] != paths(run_id)["PLAN.md"]:
                raise ApplicationError(
                    "Keyword plan has no durable publication receipt.", non_retryable=True
                )
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            ref = f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}/{publication['artifact_path']}"
            await self.db.complete_keyword_plan_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=publication["canonical_commit_sha"],
                artifact_path=publication["artifact_path"],
                artifact_ref=ref,
                summary=publication["summary"],
            )

    @activity.defn
    async def keyword_failure(self, run_id: str) -> None:
        failure = await self._result(run_id, "failure") or {}
        message = None
        if failure.get("code") == "validation":
            message = (
                "Keyword planning stopped because the saved model assessment failed "
                "validation. No plan was published; saved research was retained."
            )
        elif failure.get("code") == "model_unavailable":
            message = (
                f"Keyword planning stopped because {model_failure_cause(failure)}. "
                "Saved research was retained; no replacement call was purchased."
            )
        await self.db.project_failure(
            run_id=UUID(run_id),
            error_message=message
            or (
                "Keyword planning could not finish. Check the selected inputs, "
                "provider configuration, and spending limit. Saved calls were retained; "
                "uncertain requests were not purchased again."
            ),
        )


def model_failure_cause(failure: dict) -> str:
    """Say in plain words why a model step stopped, from its stage and fixed reason label."""
    stage = STAGE_NAMES.get(failure.get("stage"), "keyword")
    reason = failure.get("reason") or ""
    if reason == "provider_timeout":
        seconds = MODEL_TIMEOUT_SECONDS.get(failure.get("stage"))
        limit = f"within {seconds // 60} minutes" if seconds else "in time"
        return f"the {stage} model call did not finish {limit}"
    if reason.startswith("provider_status_") and reason.removeprefix("provider_status_").isdigit():
        code = reason.removeprefix("provider_status_")
        if code == "429":
            return f"the model provider rate-limited the {stage} call (HTTP 429)"
        return f"the model provider rejected the {stage} call (HTTP {code})"
    if reason == "provider_connection":
        return f"the connection to the model provider failed during the {stage} call"
    if reason == "invalid_result":
        return f"the {stage} model call returned a result Tin could not use"
    if reason == "route_unavailable":
        return f"the model for the {stage} step is not configured on this worker"
    if reason == "spending_limit":
        return f"the {stage} model call would have gone past the run's spending limit"
    if reason == "unconfirmed_previous_request":
        return f"an earlier {stage} model call stopped before its result was confirmed"
    return "a model result could not be confirmed"
