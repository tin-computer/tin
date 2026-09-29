"""Paid ads assessment activities: receipted evidence, budgeted research, bounded model steps
and a four-file publication. Only the run identifier crosses Temporal; every result lives in an
effect receipt so a retry replays it and an unconfirmed paid call is never bought twice."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import paid_ads
from tin_lite.growth_plan_site import evidence_text, read_site
from tin_lite.integrations import GSC_PROVIDER
from tin_lite.keyword_data import ENDPOINTS, KeywordData, request_for
from tin_lite.keyword_plan import gsc_property_matches, gsc_rows, phrase
from tin_lite.keyword_plan import host as keyword_host
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
from tin_lite.paid_ads import KEY, LIMITS, POLICY, UnusableModelResult, paths
from tin_lite.paid_ads_sources import upstream_sources
from tin_lite.usage_capture import external_usage_scope

STEP_TOTAL = 5
# The provider's own wait for one model step, just inside the step's 240-second budget. Without
# it the client stops at its 90-second default and a slow but valid answer is lost.
MODEL_TIMEOUT_SECONDS = 225


def _base(step: str) -> str:
    return step.split(":")[0]


class PaidAdsActivities:
    def __init__(
        self,
        *,
        database,
        storage,
        settings,
        router=None,
        integrations=None,
        provider=None,
        gak=None,
        site_reader=None,
    ):
        self.db, self.storage, self.settings = database, storage, settings
        self.router, self.integrations, self.gak = router, integrations, gak
        login, password = (
            getattr(settings, "dataforseo_login", None),
            getattr(settings, "dataforseo_password", None),
        )
        self.provider = provider or (
            KeywordData(login.get_secret_value(), password.get_secret_value())
            if login and password
            else None
        )
        self.read_site = site_reader or read_site
        self._gak_lock = asyncio.Lock()

    # ------------------------------------------------------------ receipts

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"{paid_ads.PREFIX}:{UUID(str(run_id))}:{stage}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            run is None
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "succeeded"}
        ):
            raise ApplicationError("The paid ads assessment is not active.", non_retryable=True)
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
        if needs == "dfs":
            return self.provider is not None
        if needs == "gak":
            return self.gak is not None
        return True

    async def _paid(self, run_id: str, stage: str, request: dict, amount: str, call, *, needs):
        key, fingerprint = self.key(run_id, stage), digest(request)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            saved = (existing.result or {}) if existing else {}
            if saved and saved.get("request_sha256") != fingerprint:
                raise ApplicationError(
                    "A paid request changed after recording its intent.", non_retryable=True
                )
            if existing and existing.status == "completed":
                return saved
            run = await self._active(run_id, conn=conn)
            if run.status.value == "succeeded":
                raise ApplicationError(
                    "Completed runs cannot start new research.", non_retryable=True
                )
            if not self._configured(needs):
                raise ApplicationError(
                    "The assessment's providers are no longer configured.", non_retryable=True
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
                        model_usage_scope(run_id=run_id, step=f"paid_ads:{stage}", conn=conn),
                        external_usage_scope(self.db, conn, run_id, stage, maximum_usd=amount),
                    ):
                        value = await call()
                    result = {"status": "completed", "value": value}
                except Exception as exc:
                    from tin_lite.billing_contracts import BillingError

                    # Raw SDK/provider errors, headers and credentials never enter evidence.
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

    # ------------------------------------------------------------ model and provider calls

    async def _model(self, run_id: str, step: str, system: str, user: str, schema, max_out, effort):
        """One receipted, reserved model step; an unusable answer is receipted so a retry replays
        the refusal and the orchestration may buy one replacement under its `:retry` id."""
        if len(user.encode()) > POLICY["max_model_input_bytes"]:
            raise ApplicationError("A model step's input exceeds its bound.", non_retryable=True)
        request = ModelRequest(
            system=system,
            messages=(ModelMessage(role=MessageRole.USER, content=user),),
            output_schema=schema,
            output_schema_name=paid_ads.schema_name(step),
            max_output_tokens=max_out,
            reasoning_effort=ReasoningEffort(effort),
        )
        route = paid_ads.route_for(step).key

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

    async def _query(self, run_id: str, stage: str, kind: str, value):
        scope = await self._result(run_id, "scope")
        request = request_for(
            kind, market=scope["market"], value=value, tag=self.key(run_id, stage)
        )

        async def call():
            raw = await self.provider.query(
                kind, market=scope["market"], value=value, tag=request["tag"]
            )
            observed = datetime.now(UTC).isoformat()
            metadata = {
                k: raw.get(k)
                for k in ("reported_cost_usd", "provider_task_id", "items_count", "total_count")
            }
            items = raw["items"]
            if kind == "serp":
                compact = {
                    "keyword": value,
                    "paid_slots": sum(1 for i in items if i.get("type") == "paid"),
                    "paid_domains": [i.get("domain") for i in items if i.get("type") == "paid"][
                        :10
                    ],
                    "organic_domains": [
                        i.get("domain") for i in items if i.get("type") == "organic"
                    ][:10],
                }
            elif kind == "ad_traffic":
                compact = {"items": items}
            elif kind == "ads_search":
                shown = sorted(
                    (str(i.get("first_shown") or ""), str(i.get("last_shown") or ""))
                    for i in items
                    if i.get("first_shown")
                )
                compact = {
                    "domain": value,
                    "count": len(items),
                    "formats": sorted({str(i.get("format")) for i in items}),
                    "first_shown": shown[0][0] if shown else None,
                    "last_shown": max((s[1] for s in shown), default=None),
                }
            elif kind == "ads_advertisers":
                compact = {
                    "keyword": value,
                    "count": len(items),
                    "advertisers": [
                        {
                            "title": str(i.get("title") or "")[:80],
                            "verified": bool(i.get("verified")),
                            "approx_ads_count": i.get("approx_ads_count"),
                        }
                        for i in items[:5]
                    ],
                }
            elif kind == "ranked_paid":
                keywords = []
                for item in items:
                    data = item.get("keyword_data") or {}
                    try:
                        text = phrase(data.get("keyword"))
                    except ValueError:
                        continue
                    keywords.append(
                        {"keyword": text, "cpc": (data.get("keyword_info") or {}).get("cpc")}
                    )
                compact = {
                    "domain": value,
                    "count": len(keywords),
                    "sample": [k["keyword"] for k in keywords[:5]],
                    "keywords": keywords[: POLICY["ranked_paid_rows"]],
                }
            else:  # overview_batch
                compact = {
                    "items": [
                        {
                            "keyword": i.get("keyword"),
                            "keyword_info": {
                                k: (i.get("keyword_info") or {}).get(k)
                                for k in ("cpc", "competition_level", "search_volume")
                            },
                            "search_intent_info": {
                                "main_intent": (i.get("search_intent_info") or {}).get(
                                    "main_intent"
                                )
                            },
                        }
                        for i in items
                    ]
                }
            return {
                **metadata,
                **compact,
                "observed_at": observed,
                "endpoint": ENDPOINTS[kind],
                "request": request,
            }

        amount = POLICY[
            {
                "serp": "serp_reservation_usd",
                "ad_traffic": "ad_traffic_reservation_usd",
                "ads_search": "ads_reservation_usd",
                "ads_advertisers": "ads_reservation_usd",
            }.get(kind, "labs_reservation_usd")
        ]
        result = await self._paid(
            run_id,
            stage,
            {"endpoint": ENDPOINTS[kind], "request": request},
            amount,
            call,
            needs="dfs",
        )
        if result["status"] == "completed" and Decimal(
            str(result["value"]["reported_cost_usd"])
        ) > Decimal(amount):
            raise ApplicationError(
                "Provider price exceeded the pinned reservation; further research was stopped.",
                non_retryable=True,
            )
        return result

    async def _gak(self, run_id: str, stage: str, kind: str, value):
        scope = await self._result(run_id, "scope")
        tag = self.key(run_id, stage)

        async def call():
            async with self._gak_lock:  # the planner service serialises requests
                raw = await self.gak.query(kind, market=scope["market"], value=value, tag=tag)
            return {
                "items": raw["items"],
                "items_count": raw["items_count"],
                "reported_cost_usd": raw["reported_cost_usd"],
                "provider_task_id": raw["provider_task_id"],
                "observed_at": datetime.now(UTC).isoformat(),
                "endpoint": kind,
                "request": {"kind": kind, "market": scope["market"], "value": value},
            }

        return await self._paid(
            run_id,
            stage,
            {"endpoint": f"gak:{kind}", "market": scope["market"], "value": value},
            POLICY["gak_reservation_usd"],
            call,
            needs="gak",
        )

    async def _gsc(self, run_id: str):
        scope = await self._result(run_id, "scope")
        selection = scope["gsc"]
        if selection["status"] != "selected":
            return await self._save(run_id, "gather:gsc", selection)
        run = await self._active(run_id)

        async def call():
            raw = await self.integrations.search_console_analytics(
                project_id=run.project_id,
                start_date=selection["start_date"],
                end_date=selection["end_date"],
                dimensions=("query", "page", "country"),
                row_limit=POLICY["gsc_rows"],
                expected_site_url=selection["property"],
                execution_key=self.key(run_id, "gather:gsc:read"),
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
            }

        return await self._paid(run_id, "gather:gsc", selection, "0", call, needs="gsc")

    async def _progress(self, run_id, step, current, summary):
        await self.db.project_run_progress(
            run_id=UUID(str(run_id)),
            mode="steps",
            step=step,
            current=current,
            total=STEP_TOTAL,
            summary=summary,
        )

    # ------------------------------------------------------------ activities

    @activity.defn(name="paid_ads_prepare")
    async def prepare(self, run_id: str) -> None:
        if await self._result(run_id, "scope"):
            await self.db.mark_run_running(UUID(run_id))
            return
        run = await self._active(run_id)
        inputs = dict(run.input or {})
        try:
            paid_ads.check_inputs(inputs)
        except (ValueError, TypeError, KeyError) as exc:
            raise ApplicationError(str(exc) or "Invalid inputs.", non_retryable=True) from None
        maximum = min(
            Decimal(str(getattr(self.settings, "paid_ads_max_cost_usd", 0))),
            Decimal(str(inputs.get("max_cost_usd", 6))),
        )
        if (
            not all(self._configured(n) for n in ("model", "dfs", "gak"))
            or not maximum.is_finite()
            or maximum < 3
        ):
            raise ApplicationError(
                "Configure DataForSEO, the Keyword Planner service, the native model and a "
                "paid-ads ceiling of at least $3.",
                non_retryable=True,
            )
        if not run.definition_commit_sha:
            raise ApplicationError(
                "The assessment requires a pinned definition.", non_retryable=True
            )
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path=f"workflows/{KEY}.json",
            )
        )
        if (
            definition.get("paid_ads_policy") != POLICY
            or definition.get("paid_ads_routes") != paid_ads.route_definitions()
            or definition.get("paid_ads_contract_sha256") != paid_ads.contract_digest()
        ):
            raise ApplicationError(
                "This worker does not serve the selected assessment contract.", non_retryable=True
            )
        url = inputs.get("product_url") or ""
        target = keyword_host(url) if url else None
        scope = {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "definition_sha": run.definition_commit_sha,
            "inputs": {k: v for k, v in inputs.items() if k != "project_id"},
            "url": url,
            "host": target,
            "market": inputs.get("market") or "US",
            "started_at": run.created_at.isoformat(),
            "today": datetime.now(UTC).date().isoformat(),
            "max_cost_usd": str(maximum),
            "policy_version": POLICY["version"],
            "gsc": {"status": "not_requested"},
        }
        if target:
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
                    "start_date": (end - timedelta(days=POLICY["gsc_days"] - 1)).isoformat(),
                    "end_date": end.isoformat(),
                }
            else:
                scope["gsc"] = {
                    "status": "unavailable",
                    "reason": "No connected Search Console property matches this exact website.",
                }
        gate = paid_ads.gate(inputs, "pending" if url else None)
        if gate and gate["reason"] == "no_site" and url:
            gate = None  # the site verdict is only known after the fetch in gather
        scope["gate"] = gate
        await self._save(run_id, "scope", scope)
        await self.db.mark_run_running(run.id)

    @activity.defn(name="paid_ads_gather")
    async def gather(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        if scope["gate"] or await self._result(run_id, "gathered"):
            return
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        await self._progress(
            run_id, "gather", 1, "Reading the site, earlier reports and Search Console"
        )

        async def upstream():
            try:
                found = await upstream_sources(
                    database=self.db,
                    storage=self.storage,
                    project=project,
                    inputs=scope["inputs"],
                    market=scope["market"],
                )
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ApplicationError(
                    str(exc) or "An earlier run could not be verified.", non_retryable=True
                ) from None
            return await self._save(
                run_id, "gather:upstream", {"status": "completed", "value": found}
            )

        async def site():
            saved = await self._result(run_id, "gather:site")
            if saved:
                return saved
            pages = await self.read_site(scope["url"], html_scan=paid_ads.tracking_signals)
            for page in pages.get("pages", []):
                page.pop("html", None)
            return await self._save(run_id, "gather:site", {"status": "completed", "value": pages})

        found, pages = await asyncio.gather(upstream(), site())
        level, signals = paid_ads.tracking_level(pages["value"].get("pages", []))
        await self._save(
            run_id,
            "gather:tracking",
            {"status": "completed", "value": {"level": level, "signals": signals}},
        )
        if activity.in_activity():
            activity.heartbeat({"stage": "site"})
        gsc = await self._gsc(run_id)
        own_ads = await self._query(run_id, "gather:ads_search", "ads_search", scope["host"])
        gate = paid_ads.gate(scope["inputs"], pages["value"].get("verdict"))
        await self._save(
            run_id,
            "gathered",
            {
                "stages": [
                    "gather:upstream",
                    "gather:site",
                    "gather:tracking",
                    "gather:gsc",
                    "gather:ads_search",
                ],
                "site_verdict": pages["value"].get("verdict"),
                "gate": gate,
                "gsc_status": (gsc.get("value") or gsc).get("status", gsc.get("status")),
                "own_ads": (own_ads.get("value") or {}).get("count"),
            },
        )

    async def _gathered(self, run_id: str) -> dict:
        gathered = await self._result(run_id, "gathered") or {}
        out = {}
        for stage in gathered.get("stages", []):
            receipt = await self._result(run_id, stage)
            out[stage.removeprefix("gather:")] = receipt
        return out

    @activity.defn(name="paid_ads_research")
    async def research(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        gathered_meta = await self._result(run_id, "gathered") or {}
        if scope["gate"] or gathered_meta.get("gate") or await self._result(run_id, "collection"):
            return
        gathered = await self._gathered(run_id)
        await self._progress(run_id, "research", 2, "Researching demand, cost and the auction")
        site = (gathered.get("site") or {}).get("value") or {"verdict": "none", "pages": []}
        upstream = (gathered.get("upstream") or {}).get("value") or {}
        text = evidence_text(site)
        gsc_value = (gathered.get("gsc") or {}).get("value") or {}
        inputs = scope["inputs"]

        async def model(step, prompt, schema, max_out, validate=None):
            for attempt, name in enumerate((step, f"{step}:retry")):
                try:
                    result = await self._model(
                        run_id, name, *prompt, schema, max_out, POLICY["reasoning_effort"]
                    )
                    return validate(result) if validate else result
                except UnusableModelResult as exc:
                    if attempt == 1:
                        await self._save(
                            run_id,
                            "failure",
                            {"code": "validation", "stage": step, "detail": str(exc)[:160]},
                        )
                        raise ApplicationError(
                            "A model result was unusable twice; nothing was saved.",
                            non_retryable=True,
                        ) from None

        profile = await model(
            "profile",
            paid_ads.profile_prompt(inputs, text, upstream),
            paid_ads.profile_schema(),
            paid_ads.MAX_OUT["profile"],
        )
        known = {
            "seeds": list((upstream.get("keyword") or {}).get("seeds") or []),
            "competitors": list(inputs.get("competitor_domains") or [])
            + list((upstream.get("keyword") or {}).get("competitors") or []),
        }
        own = (scope["host"] or "").removeprefix("www.")
        if len(known["seeds"]) >= 8 and known["competitors"]:
            selection = {
                "basis": "keyword_plan",
                "seeds": known["seeds"][: POLICY["max_seeds"]],
                "competitors": known["competitors"],
                "negative_themes": [],
            }
        else:
            seeds = await model(
                "seeds",
                paid_ads.seeds_prompt(profile, text, own, known),
                paid_ads.seeds_schema(),
                paid_ads.MAX_OUT["seeds"],
            )
            phrases = []
            for entry in seeds.get("seeds") or []:
                try:
                    clean = phrase(entry["phrase"])
                except (ValueError, KeyError, TypeError):
                    continue
                if clean.casefold() not in {p.casefold() for p in phrases}:
                    phrases.append(clean)
            for seed in known["seeds"]:
                if seed.casefold() not in {p.casefold() for p in phrases}:
                    phrases.append(seed)
            competitors = []
            for value in [*known["competitors"], *(seeds.get("competitor_domains") or [])]:
                try:
                    domain = keyword_host(value).removeprefix("www.")
                except ValueError:
                    continue
                if domain != own and domain not in competitors:
                    competitors.append(domain)
            selection = {
                "basis": "model" if seeds.get("seeds") else "form",
                "seeds": phrases[: POLICY["max_seeds"]],
                "competitors": competitors[: POLICY["max_competitors"]],
                "negative_themes": [str(n)[:60] for n in (seeds.get("negative_themes") or [])][:12],
            }
        if not selection["seeds"]:
            await self._save(run_id, "failure", {"code": "validation", "stage": "seeds"})
            raise ApplicationError("No buyer-intent seeds could be formed.", non_retryable=True)
        selection = await self._save(run_id, "seed_selection", selection)
        receipts: dict = {}
        stage = "research:gak:ideas"
        receipts[stage] = await self._gak(
            run_id, stage, "ideas", {"seeds": selection["seeds"], "limit": 200}
        )
        if scope["url"]:
            stage = "research:gak:ideas_url"
            receipts[stage] = await self._gak(
                run_id, stage, "ideas", {"url": scope["url"], "limit": 100}
            )
        candidates: dict[str, dict] = {}
        source: dict[str, str] = {}
        for stage_name, tag in (("research:gak:ideas", "seed"), ("research:gak:ideas_url", "url")):
            for item in ((receipts.get(stage_name) or {}).get("value") or {}).get("items") or []:
                try:
                    text_kw = phrase(item.get("keyword"))
                except ValueError:
                    continue
                if tag == "url":
                    # URL ideas key off the domain name; keep only those that share two words
                    # with a seed (or the whole seed when it is one word).
                    words = set(text_kw.casefold().split())
                    if not any(
                        len(words & set(seed.casefold().split())) >= min(2, len(seed.split()))
                        for seed in selection["seeds"]
                    ):
                        continue
                key = text_kw.casefold()
                if key not in candidates:
                    candidates[key] = {
                        "keyword": text_kw,
                        "volume": item.get("avg_monthly_searches") or 0,
                    }
                    source[key] = tag
        for seed in selection["seeds"]:
            candidates.setdefault(seed.casefold(), {"keyword": seed, "volume": 0})
            source.setdefault(seed.casefold(), "seed")
        by_volume = sorted(candidates.values(), key=lambda c: -(c["volume"] or 0))
        seeded = [c for c in by_volume if source[c["keyword"].casefold()] != "url"]
        from_url = [c for c in by_volume if source[c["keyword"].casefold()] == "url"]
        top = (seeded[: POLICY["volume_keywords"] - POLICY["url_keywords"]] + from_url)[
            : POLICY["volume_keywords"]
        ]
        keywords = [c["keyword"] for c in top]
        stage = "research:gak:volume"
        receipts[stage] = await self._gak(run_id, stage, "volume", {"keywords": keywords})
        volume_items = ((receipts[stage].get("value") or {}).get("items")) or []
        if not volume_items:
            await self._save(run_id, "failure", {"code": "no_observations", "stage": stage})
            raise ApplicationError(
                "The Keyword Planner returned no observations; nothing was assessed.",
                non_retryable=True,
            )
        stage = "research:dfs:overview:0"
        receipts[stage] = await self._query(
            run_id, stage, "overview_batch", keywords[: POLICY["overview_chunk"]]
        )
        market = scope["market"]
        from tin_lite.keyword_plan import keyword_id

        keyword_source = {
            keyword_id(c["keyword"], market): source[c["keyword"].casefold()] for c in top
        }
        table = paid_ads.keyword_table(
            market=market,
            volume_items=volume_items,
            overview_items=(receipts[stage].get("value") or {}).get("items"),
            gsc_rows=gsc_value.get("rows"),
            cluster_items=None,
            keyword_source=keyword_source,
        )
        ids = [row["id"] for row in table]
        by_id = {row["id"]: row for row in table}
        labels: dict = {}
        chunk_size = POLICY["classify_chunk"]
        for index, chunk in enumerate(
            [ids[i : i + chunk_size] for i in range(0, len(ids), chunk_size)]
        ):
            rows = [
                {k: by_id[i].get(k) for k in ("id", "keyword", "volume", "cpc", "provider_intent")}
                for i in chunk
            ]
            labelled = await model(
                f"classify:{index}",
                paid_ads.classify_prompt(profile, rows),
                paid_ads.classify_schema(chunk),
                paid_ads.MAX_OUT["classify"],
                validate=lambda result, chunk=chunk: paid_ads.validate_classification(
                    result, chunk
                ),
            )
            labels.update({k: list(v) for k, v in labelled.items()})
        targeted = paid_ads.forecast_keywords(table, labels)
        for bid in paid_ads.bid_levels([by_id[k] for k in targeted]):
            stage = f"research:dfs:ad_traffic:{bid}"
            receipts[stage] = await self._query(
                run_id,
                stage,
                "ad_traffic",
                {"keywords": [by_id[k]["keyword"] for k in targeted], "bid": bid},
            )
        for index, kid in enumerate(targeted[: POLICY["serp_samples"]]):
            stage = f"research:dfs:serp:{index}"
            receipts[stage] = await self._query(run_id, stage, "serp", by_id[kid]["keyword"])
        for index, domain in enumerate(selection["competitors"][: POLICY["max_competitors"]]):
            stage = f"research:dfs:ranked_paid:{index}"
            receipts[stage] = await self._query(run_id, stage, "ranked_paid", domain)
        if profile.get("business_name"):
            stage = "research:dfs:ads_advertisers"
            receipts[stage] = await self._query(
                run_id, stage, "ads_advertisers", profile["business_name"][:120]
            )
        await self._save(
            run_id,
            "collection",
            {
                "profile": profile,
                "selection": selection,
                "keywords": table,
                "labels": labels,
                "stages": sorted(receipts),
                "unavailable": sorted(
                    s for s, r in receipts.items() if r.get("status") != "completed"
                ),
            },
        )

    @activity.defn(name="paid_ads_assess")
    async def assess(self, run_id: str) -> None:
        if await self._result(run_id, "assessment"):
            return
        scope = await self._result(run_id, "scope")
        gathered_meta = await self._result(run_id, "gathered") or {}
        gate = scope["gate"] or gathered_meta.get("gate")
        await self._progress(run_id, "assess", 3, "Scoring the economics and writing the verdict")
        if gate:
            documents = paid_ads.not_now_documents(
                run_id=run_id,
                project_id=scope["project_id"],
                definition_sha=scope["definition_sha"],
                today=scope["today"],
                inputs=scope["inputs"],
                reason=gate,
            )
            await self._save(
                run_id,
                "assessment",
                {
                    "documents": {n: c.decode() for n, c in documents.items()},
                    "report": {
                        "decision": "not_now",
                        "binding_constraint": gate["text"],
                        "gate": gate["reason"],
                    },
                },
            )
            return
        collection = await self._result(run_id, "collection")
        gathered = await self._gathered(run_id)
        receipts = {stage: await self._result(run_id, stage) for stage in collection["stages"]}
        research = {
            "profile": collection["profile"],
            "keywords": collection["keywords"],
            "receipts": receipts,
            "labels": {k: tuple(v) for k, v in collection["labels"].items()},
            "negative_themes": (collection.get("selection") or {}).get("negative_themes") or [],
        }

        async def generate(step, system, user, schema, max_out, effort):
            if activity.in_activity():
                activity.heartbeat({"step": step})
            return await self._model(run_id, step, system, user, schema, max_out, effort)

        try:
            result = await paid_ads.build_assessment(scope, gathered, research, generate)
        except (UnusableModelResult, KeyError, TypeError, ValueError, LookupError) as exc:
            await self._save(
                run_id,
                "failure",
                {"code": "validation", "stage": "assess", "detail": str(exc)[:160]},
            )
            raise ApplicationError(
                "The assessment's model results were unusable. Nothing was saved; try again.",
                non_retryable=True,
            ) from None
        await self._save(run_id, "assessment", result)

    @activity.defn(name="paid_ads_publish")
    async def publish(self, run_id: str) -> None:
        key = self.key(run_id, "publish")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            run = await self._active(run_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            assessment = await self._result(run_id, "assessment")
            if assessment is None:
                raise ApplicationError("There is no assessment to publish.", non_retryable=True)
            await self._progress(
                run_id, "publish", 4, "Saving the assessment to your project files"
            )
            names = paths(run_id)
            documents = {names[n]: c.encode() for n, c in assessment["documents"].items()}
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
                    documents=documents,
                    paths=names,
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
                    "artifact_path": names["ASSESSMENT.md"],
                    "documents_sha256": digest(
                        {names[n]: c for n, c in assessment["documents"].items()}
                    ),
                    "summary": paid_ads.summary_line(assessment["report"]),
                },
            )

    @activity.defn(name="paid_ads_project")
    async def project(self, run_id: str) -> None:
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            run = await self._active(run_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            publication = await self._result(run_id, "publish")
            if not publication or publication["artifact_path"] != paths(run_id)["ASSESSMENT.md"]:
                raise ApplicationError(
                    "The assessment has no durable publication receipt.", non_retryable=True
                )
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            ref = f"code.storage://{project.state_repo_id}@{publication['canonical_commit_sha']}/{publication['artifact_path']}"
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

    @activity.defn(name="paid_ads_failure")
    async def failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.executor != KEY:
            return
        failure = await self._result(run_id, "failure") or {}
        message = {
            "validation": (
                "The paid ads assessment stopped because a model result failed validation. "
                "Nothing was published; saved research was retained."
            ),
            "model_unavailable": (
                "The paid ads assessment stopped because a model result could not be confirmed. "
                "Saved research was retained; no replacement call was purchased."
            ),
            "no_observations": (
                "The paid ads assessment stopped because the Keyword Planner returned nothing "
                "for these terms. Check the product URL and try again."
            ),
        }.get(failure.get("code"))
        await self.db.project_failure(
            run_id=run.id,
            error_message=message
            or (
                "The paid ads assessment could not finish. Check the inputs, provider "
                "configuration and spending limit. Saved calls were retained; uncertain "
                "requests were not purchased again."
            ),
        )
