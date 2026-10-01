"""Bounded native planning. Temporal history receives only the run identifier."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import content_plan as legacy
from tin_lite import content_plan_editorial as editorial
from tin_lite.content_plan import (
    KEY,
    check_inputs,
    empty_plan,
    normalize_model_destinations,
    paths,
    plan_path,
    render_plan,
    validate_change,
)
from tin_lite.content_plan_sources import (
    SITE_SOURCES,
    competitor_rows,
    competitor_watch,
    context_files,
    positioning_files,
    published_pages,
    research_sources,
    site_signals,
)
from tin_lite.content_programs import ContentPrograms, decoded
from tin_lite.model_providers import MessageRole, ModelMessage, ModelRequest
from tin_lite.model_usage import model_usage_scope
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.technical_fix import fetch_page
from tin_lite.workflow_evidence import integration_inventory

# How long the worker waits for the plan. Kept outside the pinned policy: waiting longer never
# changes the request. Every contract allows 16,000 output tokens, which at about 55 tokens a
# second (the keyword review's measured pace) takes close to five minutes. The provider's
# 90-second default stopped production plans before they finished.
MODEL_TIMEOUT_SECONDS = 300


def plan_kinds(research):
    """The kinds this run's evidence supports: answers need an AI-visibility gap finding and
    refreshes a page a refresh could fix. Articles are always possible."""
    rows = research.get("rows", [])
    kinds = {legacy.ARTICLE}
    if any(row["data"].get("check_id") == editorial.ANSWER_CHECK for row in rows):
        kinds.add(legacy.ANSWER)
    if any(row["source_id"].startswith(editorial.REFRESH_SOURCE_PREFIX) for row in rows):
        kinds.add(legacy.REFRESH)
    return kinds


class ContentPlanActivities:
    def __init__(self, *, database, storage, settings, router):
        self.db, self.storage, self.settings, self.router = database, storage, settings, router
        self.programs = ContentPrograms(database=database, storage=storage)

    async def active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            not run
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "succeeded"}
        ):
            raise ValueError("Content planning is no longer active.")
        return run

    async def saved(self, run_id, stage):
        receipt = await self.db.get_effect(f"content:{run_id}:{stage}")
        return receipt.result if receipt and receipt.status == "completed" else None

    async def save(self, run_id, stage, value):
        key = f"content:{run_id}:{stage}"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=value)
        return value

    async def typed_research(self, contract, project, revision):
        """For a typed (v7) contract: research also lists the pages a refresh could fix, every
        page Tin knows on the site (including the ones Tin published itself), and the newest Page
        decisions and traffic snapshot, read once at `revision`."""
        if not getattr(contract, "TYPED", False):
            return {}
        signals = await site_signals(
            storage=self.storage,
            project=project,
            revision=revision,
            today=datetime.now(UTC).date(),
        )
        return {
            "typed": True,
            "planned": {
                path: set(row["checks"])
                for path, row in (signals["page_decisions"].get("refresh") or {}).items()
            },
            "published": await published_pages(self.db, project),
            "signals": signals,
        }

    async def page_inventory(self, run, context, *, bind_sources=False):
        saved = await self.saved(run.id, "pages")
        if saved:
            return saved
        urls, omitted = editorial.page_candidates(context)
        semaphore = asyncio.Semaphore(editorial.POLICY["page_concurrency"])

        async def inspect(index, url):
            stage = f"page:{digest(url)}"
            async with semaphore:
                saved_page = await self.saved(run.id, stage)
                if saved_page:
                    return saved_page
                await self.active(run.id)
                try:
                    async with asyncio.timeout(editorial.POLICY["page_timeout_seconds"]):
                        observation = await fetch_page(url, host=context["plan"]["host"])
                    if not editorial.clean_url(observation["url"], context["plan"]["host"]):
                        raise ValueError("Page redirect did not yield a clean destination.")
                    page = editorial.page_evidence(
                        observation, url, text_limit=context.get("page_text_limit", 2200)
                    )
                    if bind_sources and page["status"] == "inspected":
                        page["source_id"] = "page:" + digest(
                            {
                                "url": page["url"],
                                "html_sha256": page["html_sha256"],
                            }
                        )
                except (ValueError, httpx.HTTPError, OSError, TimeoutError):
                    # Unavailable is not absence; never expose raw exceptions.
                    page = {
                        "requested_url": url,
                        "url": url,
                        "status": "unavailable",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "text": "",
                        "reason": "No usable bounded public HTML was retrieved.",
                    }
                await self.active(run.id)
                return await self.save(run.id, stage, {**page, "page_id": f"p{index:03d}"})

        pages = await asyncio.gather(*(inspect(i, url) for i, url in enumerate(urls, 1)))
        return await self.save(run.id, "pages", {"pages": pages, "omitted_candidates": omitted})

    async def model(self, run, data, *, contract=legacy, schema=None):
        policy = contract.POLICY
        key = f"content:{run.id}:model"
        encoded = canonical_json(data).decode()
        if len(encoded.encode()) > policy["max_input_bytes"]:
            raise ValueError(
                "Planning sources exceed the bounded model input. Use smaller context files."
            )
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result["data"]
            if receipt:
                raise ValueError(
                    "The previous model request is unconfirmed; no replacement was purchased."
                )
            if (
                not self.settings.luna_api_key
                or getattr(self.settings, "content_plan_max_cost_usd", 0) < 1
            ):
                raise ValueError(
                    "Content planning requires the native model and an explicit $1 spending "
                    "ceiling."
                )
            await self.active(run.id, conn=conn)
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.save_effect_progress(
                conn,
                execution_key=key,
                result={
                    "input_sha256": digest(data),
                    "reservation_usd": policy["reservation_usd"],
                    "attempted_at": datetime.now(UTC).isoformat(),
                },
            )
            try:
                with model_usage_scope(run_id=run.id, step="content:plan", conn=conn):
                    async with asyncio.timeout(MODEL_TIMEOUT_SECONDS + 30):
                        result = await self.router.generate(
                            contract.ROUTE_KEY,
                            ModelRequest(
                                system=contract.INSTRUCTIONS,
                                messages=(ModelMessage(role=MessageRole.USER, content=encoded),),
                                output_schema=schema or contract.MODEL_SCHEMA,
                                output_schema_name="content_program",
                                max_output_tokens=policy["max_output_tokens"],
                            ),
                            timeout_seconds=MODEL_TIMEOUT_SECONDS,
                        )
            except Exception as exc:
                raise ValueError(
                    "The model result is unconfirmed; no replacement was purchased."
                ) from exc
            if not isinstance(result.parsed, dict):
                raise ValueError("The model did not return a content plan.")
            # Save before semantic validation: retries never buy a repair call.
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "data": result.parsed,
                    "usage": asdict(result.usage),
                    "request_id": result.request_id,
                    "input_sha256": digest(data),
                },
            )
            return result.parsed

    async def publish(self, run, project, documents):
        key = f"content:{run.id}:publish"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result["canonical_commit_sha"]
            await self.db.start_effect(conn, execution_key=key, operation=KEY)

            async def save_intent(intent):
                await self.db.save_publication_intent(conn, execution_key=key, intent=intent)

            async def validate_active():
                await self.active(run.id, conn=conn)

            output_paths = paths(str(run.id))
            limits = {"PLAN.md": 300_000, "plan.json": 240_000, "evidence.json": 400_000}
            working_path = plan_path(run.project_workflow_id)
            if working_path in documents:
                output_paths["working_plan"] = working_path
                limits["working_plan"] = 240_000
            if legacy.site_pages_path(str(run.id)) in documents:
                output_paths[legacy.SITE_PAGES_FILE] = legacy.site_pages_path(str(run.id))
                limits[legacy.SITE_PAGES_FILE] = legacy.SITE_PAGES_FILE_BYTES
            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents=documents,
                    paths=output_paths,
                    limits=limits,
                    message=f"content.plan {run.id} [{key}]",
                    intent=(receipt.result or {}).get("publication") if receipt else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                )
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "canonical_commit_sha": revision,
                    "documents_sha256": digest(
                        {path: content.decode() for path, content in documents.items()}
                    ),
                },
            )
            return revision

    @activity.defn
    async def content_plan_execute(self, run_id: str):
        try:
            await self.execute(run_id)
        except ValidationError:
            await self.save(
                run_id,
                "failure",
                {
                    "summary": "The plan did not match its file contract. No replacement "
                    "model call was purchased."
                },
            )
            raise ApplicationError("Content plan validation failed.", non_retryable=True) from None
        except ValueError as exc:
            # Only Tin-owned validation messages, never SDK exceptions or model content.
            await self.save(run_id, "failure", {"summary": str(exc)[:500]})
            raise ApplicationError(
                "Content planning could not validate or prepare this run.", non_retryable=True
            ) from None

    async def execute(self, run_id):
        run = await self.active(run_id)
        if run.status.value == "succeeded":
            return
        check_inputs(run.input)
        if not run.project_workflow_id:
            raise ValueError("Save this content program to My system before starting it.")
        program_id = run.project_workflow_id
        async with self.programs.lock(program_id) as conn:
            run = await self.active(run_id, conn=conn)
            configured = await self.programs.configured(run.project_id, program_id)
            project = await self.db.get_project(run.project_id)
            definition = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=f"workflows/{KEY}.json",
                )
            )
            contract = editorial.contract(definition)
            await self.db.mark_run_running(run.id)
            await self.db.project_run_progress(
                run_id=run.id,
                mode="steps",
                step="prepare",
                current=0,
                total=3,
                summary="Checking saved research and the content program",
            )
            await conn.execute(
                "INSERT INTO content_programs (project_workflow_id, project_id, "
                "initial_run_id) VALUES ($1,$2,$3) ON CONFLICT (project_workflow_id) DO "
                "NOTHING",
                program_id,
                project.id,
                run.id,
            )
            program = await conn.fetchrow(
                "SELECT * FROM content_programs WHERE project_workflow_id = $1", program_id
            )
            if not program["plan_revision"] and program["initial_run_id"] != run.id:
                original = await self.db.get_run(program["initial_run_id"], conn=conn)
                publication = await self.db.get_effect(f"content:{original.id}:publish", conn=conn)
                if publication and publication.status == "completed":
                    # The atomic initial bundle already exists. Recover its projection,
                    # then consume the user's current working file without another model call.
                    await conn.execute(
                        "UPDATE content_programs SET plan_revision = $2 "
                        "WHERE project_workflow_id = $1",
                        program_id,
                        publication.result["canonical_commit_sha"],
                    )
                elif original.status.value in {"failed", "stopped"}:
                    if publication:
                        raise ValueError(
                            "The original publication is uncertain and needs recovery "
                            "before another run."
                        )
                    previous_model = await self.db.get_effect(
                        f"content:{original.id}:model", conn=conn
                    )
                    if previous_model and previous_model.status != "completed":
                        raise ValueError(
                            "The original model request is unconfirmed. "
                            "No replacement was purchased."
                        )
                    # A newly requested run may retry a known failure. Temporal retries of
                    # the same run continue using its original effect receipts.
                    await conn.execute(
                        "UPDATE content_programs SET initial_run_id = $2 "
                        "WHERE project_workflow_id = $1",
                        program_id,
                        run.id,
                    )
                program = await conn.fetchrow(
                    "SELECT * FROM content_programs WHERE project_workflow_id = $1", program_id
                )
            mode = "initial" if program["initial_run_id"] == run.id else "batch"
            amendment = None
            if run.input.get("amendment_id"):
                mode = "revision"
                amendment = await conn.fetchrow(
                    "SELECT * FROM content_plan_revisions WHERE id = $1 AND "
                    "project_workflow_id = $2 AND project_id = $3",
                    UUID(run.input["amendment_id"]),
                    program_id,
                    project.id,
                )
                if (
                    not amendment
                    or amendment["status"] != "pending"
                    or (amendment["run_id"] and amendment["run_id"] != run.id)
                ):
                    raise ValueError("The amendment is no longer pending for this run.")
                await conn.execute(
                    "UPDATE content_plan_revisions SET run_id = $2 WHERE id = $1 AND status "
                    "= 'pending'",
                    amendment["id"],
                    run.id,
                )
            context = await self.saved(run_id, "context")
            if not context:
                repo = await self.storage.get_repo(project.state_repo_id)
                head = await self.storage.head_sha(repo, project.canonical_branch)
                if mode == "initial":
                    if (
                        await self.storage.read_canonical_artifact_if_exists(
                            repo_id=project.state_repo_id,
                            commit_sha=head,
                            path=plan_path(program_id),
                        )
                        is not None
                    ):
                        raise ValueError("A working plan already exists; it was left unchanged.")
                    research = await research_sources(
                        database=self.db,
                        storage=self.storage,
                        project=project,
                        inputs=run.input,
                        **await self.typed_research(contract, project, head),
                    )
                    plan = empty_plan(program_id, run.input, research["scope"])
                    selected = run.input.get("context_files", [])
                    editable = [batch["id"] for batch in plan["batches"]]
                else:
                    if not program["plan_revision"]:
                        raise ValueError(
                            "The initial content plan has not finished. Retry that run first."
                        )
                    if amendment:
                        head = amendment["base_revision"]
                    read = await self.programs.read(
                        project_id=project.id, program_id=program_id, revision=head
                    )
                    plan = read["plan"]
                    original_context = await self.saved(str(program["initial_run_id"]), "context")
                    if not original_context:
                        raise ValueError("The program's original source receipt is unavailable.")
                    original_scope = original_context["research"]["scope"]
                    # Source identity/date edits are not a backdoor to a different program.
                    identity = empty_plan(
                        program_id,
                        configured.inputs,
                        original_scope,
                    )
                    for field in (
                        "audit_run_id",
                        "keyword_run_id",
                        "start_date",
                        "end_date",
                        "host",
                        "market",
                    ):
                        if plan[field] != identity[field]:
                            raise ValueError(
                                "The working plan's research or dates differ from its saved "
                                "configuration."
                            )
                    research = (
                        await research_sources(
                            database=self.db,
                            storage=self.storage,
                            project=project,
                            inputs=configured.inputs,
                            **await self.typed_research(contract, project, head),
                        )
                        if amendment
                        else {"sources": original_context["research"]["sources"]}
                    )
                    selected = (
                        decoded(amendment["context_paths"])
                        if amendment
                        else run.input.get("context_files", [])
                    )
                    editable = decoded(amendment["batch_ids"]) if amendment else []
                files = await context_files(
                    storage=self.storage, project=project, revision=head, paths=selected
                )
                watch = None
                if getattr(contract, "TYPED", False) and research.get("rows") is not None:
                    # Material competitor changes become comparison items (v7).
                    watch = await competitor_watch(
                        database=self.db, storage=self.storage, project=project
                    )
                    if watch:
                        research = {
                            **research,
                            "rows": [*research["rows"], *competitor_rows(watch)],
                        }
                context = await self.save(
                    run_id,
                    "context",
                    {
                        "mode": mode,
                        "source_revision": head,
                        "plan": plan,
                        "research": research,
                        "files": files,
                        **(
                            {"integrations": await integration_inventory(self.db, project.id)}
                            if contract.POLICY["version"]
                            in {"content-editorial-v5", "content-editorial-v6"}
                            else {}
                        ),
                        **(
                            {
                                "positioning": await positioning_files(
                                    storage=self.storage, project=project, revision=head
                                )
                            }
                            if contract.POLICY.get("positioning_files") == "project-v1"
                            else {}
                        ),
                        **(
                            {"competitor_watch": watch} if getattr(contract, "TYPED", False) else {}
                        ),
                        "editable": editable,
                        "instruction": amendment["instruction"]
                        if amendment
                        else "Create the initial roadmap.",
                        "capacity": run.input.get("pieces_per_batch", 2),
                        "page_text_limit": contract.POLICY.get("expanded_page_text_bytes", 2200),
                    },
                )
            plan = context["plan"]
            await self.db.project_run_progress(
                run_id=run.id,
                mode="steps",
                step="plan",
                current=1,
                total=3,
                summary="Preparing a revision preview"
                if amendment
                else "Preparing the content roadmap or next batch",
            )
            batch_id = None
            normalized_destinations = []
            label = "Content roadmap"
            quality = pages = None
            if mode in {"initial", "revision"}:
                if contract.POLICY["live_page_verification"]:
                    readable = contract.POLICY.get("source_aliases") == "readable-v1"
                    pages = await self.page_inventory(run, context, bind_sources=readable)
                    prepared = await self.saved(run.id, "editorial_context")
                    if not prepared:
                        data, aliases = editorial.model_context(
                            context, pages, readable_aliases=readable
                        )
                        prepared = await self.save(
                            run.id,
                            "editorial_context",
                            {
                                "data": data,
                                "aliases": aliases,
                                "schema": editorial.bound_schema(
                                    pages,
                                    aliases,
                                    schema=contract.MODEL_SCHEMA,
                                    kinds=plan_kinds(context["research"]),
                                ),
                            },
                        )
                    data, aliases = prepared["data"], prepared["aliases"]
                    proposed = await self.model(
                        run, data, contract=contract, schema=prepared["schema"]
                    )
                    plan, quality = editorial.allocate(
                        context,
                        proposed,
                        pages,
                        aliases,
                        typed=getattr(contract, "TYPED", False),
                    )
                    if getattr(contract, "TYPED", False):
                        plan, added = editorial.competitor_items(
                            context, plan, pages, cap=contract.POLICY["competitor_items"]
                        )
                        quality["competitor_items"] = added
                        quality["planned_items"] += len(added)
                        quality["unused_capacity"] -= len(added)
                        if "site_signals" in quality:
                            # Page decisions' refresh rows become refresh items.
                            plan, added = editorial.page_decision_items(
                                context,
                                plan,
                                cap=contract.POLICY.get(
                                    "max_page_decision_items", editorial.MAX_PAGE_DECISION_ITEMS
                                ),
                            )
                            quality["site_signals"]["added"] = added
                            quality["planned_items"] += len(added)
                            quality["unused_capacity"] -= len(added)
                        quality["empty_batches"] = sum(
                            not b["items"]
                            for b in plan["batches"]
                            if b["id"] in context["editable"]
                        )
                    quality["model_page_text_limit"] = data["model_page_text_limit"]
                    site = (context["research"] or {}).get("site_pages")
                    if site is not None:
                        # v7: the whole site's page list, saved beside the evidence.
                        quality["site_inventory"] = {
                            "path": legacy.site_pages_path(run_id),
                            "pages": len(site["pages"]),
                            "omitted": site["omitted"],
                            "by_source": site["by_source"],
                            "shown_to_model": len(data["site_pages"]["pages"]),
                        }
                else:
                    proposed = await self.model(run, context, contract=contract)
                    proposed, normalized_destinations = normalize_model_destinations(
                        proposed, host=plan["host"], editable=set(context["editable"])
                    )
                    plan = validate_change(plan, proposed, editable=set(context["editable"]))
                source_ids = {
                    row["source_id"] for row in context["research"]["rows"] + context["files"]
                }
                if pages:
                    source_ids.update(p["source_id"] for p in pages["pages"] if p.get("source_id"))
                for batch in plan["batches"]:
                    if batch["id"] not in context["editable"]:
                        continue
                    if len(batch["items"]) > context["capacity"]:
                        raise ValueError("The proposed batch exceeds its configured capacity.")
                    for item in batch["items"]:
                        if (
                            not item["source_ids"]
                            or not set(item["source_ids"]) <= source_ids
                            or item["readiness"] != "needs_verification"
                        ):
                            raise ValueError(
                                "A proposed item lacks valid sources or claims unverified "
                                "readiness."
                            )
                label = "Content revision preview" if amendment else label
            else:
                previous_decision = await self.saved(run_id, "decision")
                if previous_decision:
                    batch_id = previous_decision["batch_id"]
                    label = previous_decision["label"]
                else:
                    facts = await self.programs.facts(program_id, conn=conn)
                    prior = next((row for row in facts["batches"] if row["run_id"] == run.id), None)
                    if prior:
                        batch_id = prior["batch_id"]
                    else:
                        reserved = {row["batch_id"] for row in facts["batches"]}
                        held = set((facts["pending_revision"] or {}).get("batch_ids", []))
                        used_items = {item for row in facts["batches"] for item in row["item_ids"]}
                        today = (
                            (run.scheduled_for or run.created_at)
                            .astimezone(
                                ZoneInfo(
                                    (configured.schedule or {}).get("timezone", project.timezone)
                                )
                            )
                            .date()
                            .isoformat()
                        )
                        actual_date = (
                            datetime.now(UTC)
                            .astimezone(
                                ZoneInfo(
                                    (configured.schedule or {}).get("timezone", project.timezone)
                                )
                            )
                            .date()
                            .isoformat()
                        )
                        if max(today, actual_date) >= plan["end_date"]:
                            label = "Content program finished"
                        else:
                            for batch in plan["batches"]:
                                if (
                                    batch["id"] in reserved
                                    or batch["id"] in held
                                    or batch["due_date"] > today
                                ):
                                    continue
                                if any(item["id"] in used_items for item in batch["items"]):
                                    raise ValueError(
                                        "A prepared item appears in a future batch. Fix the "
                                        "working plan before continuing."
                                    )
                                batch_id = batch["id"]
                                await self.active(run.id, conn=conn)
                                await conn.execute(
                                    "INSERT INTO content_plan_batches "
                                    "(project_workflow_id,batch_id,run_id,source_revision,"
                                    "item_ids) "
                                    "VALUES ($1,$2,$3,$4,$5::jsonb)",
                                    program_id,
                                    batch_id,
                                    run.id,
                                    context["source_revision"],
                                    json.dumps([item["id"] for item in batch["items"]]),
                                )
                                break
                        if label == "Content roadmap":
                            label = "Content batch prepared" if batch_id else "No content batch due"
                    if batch_id:
                        label = "Content batch prepared"
            decision = await self.save(run_id, "decision", {"batch_id": batch_id, "label": label})
            batch_id, label = decision["batch_id"], decision["label"]
            evidence = {
                "run_id": run_id,
                "program_id": str(program_id),
                "mode": mode,
                "batch_id": batch_id,
                "source_revision": context["source_revision"],
                "sources": (context["research"] or {}).get("sources", {}),
                "context_files": [
                    {k: v for k, v in f.items() if k != "content"} for f in context["files"]
                ],
                "plan_sha256": digest(plan),
                "live_pages_checked": bool(
                    pages and any(p["status"] == "inspected" for p in pages["pages"])
                ),
                "generated_or_published": False,
                "normalized_destinations": normalized_destinations,
            }
            if pages is not None:
                evidence.update(
                    page_inventory=pages,
                    editorial=quality,
                    policy_version=contract.POLICY["version"],
                )
            documents = {
                paths(run_id)["plan.json"]: canonical_json(plan),
                paths(run_id)["evidence.json"]: canonical_json(evidence),
                paths(run_id)["PLAN.md"]: render_plan(
                    plan, label=label, batch_id=batch_id, editorial=quality, pages=pages
                ).encode(),
            }
            if mode == "initial":
                documents[plan_path(program_id)] = canonical_json(plan)
            if quality and quality.get("site_inventory"):
                site = context["research"]["site_pages"]
                documents[legacy.site_pages_path(run_id)] = canonical_json(
                    {
                        "run_id": run_id,
                        "host": site["host"],
                        "sources": SITE_SOURCES,
                        "by_source": site["by_source"],
                        "omitted": site["omitted"],
                        "sitemap_capped": site["sitemap_capped"],
                        "note": "Every page address Tin knows on the site, once per path, with the "
                        "sources that list it. Addresses only: the plan read at most "
                        f"{editorial.POLICY['max_pages']} of these pages.",
                        "pages": site["pages"],
                    }
                )
            saved_documents = await self.save(
                run_id, "artifacts", {path: content.decode() for path, content in documents.items()}
            )
            documents = {path: content.encode() for path, content in saved_documents.items()}
            await self.db.project_run_progress(
                run_id=run.id,
                mode="steps",
                step="publish",
                current=2,
                total=3,
                summary="Saving the exact plan and evidence",
            )
            revision = await self.publish(run, project, documents)
            await self.active(run.id, conn=conn)
            if mode == "initial" and not program["plan_revision"]:
                await conn.execute(
                    "UPDATE content_programs SET plan_revision = $2 WHERE project_workflow_id = $1",
                    program_id,
                    revision,
                )
            if amendment:
                await conn.execute(
                    "UPDATE content_plan_revisions SET preview_revision = $2 WHERE id = $1 "
                    "AND status = 'pending'",
                    amendment["id"],
                    revision,
                )
            if batch_id:
                await conn.execute(
                    "UPDATE content_plan_batches SET status = 'prepared', artifact_revision "
                    "= $3 WHERE project_workflow_id = $1 AND batch_id = $2 AND run_id = $4",
                    program_id,
                    batch_id,
                    revision,
                    run.id,
                )
            projection_key = f"content:{run_id}:projection"
            async with self.db.effect_lock(projection_key, KEY) as (projection_conn, receipt):
                if not receipt or receipt.status != "completed":
                    await self.db.start_effect(
                        projection_conn, execution_key=projection_key, operation=KEY
                    )
                    path = paths(run_id)["PLAN.md"]
                    await self.db._complete_readonly_report_projection(
                        projection_conn,
                        execution_key=projection_key,
                        run_id=run.id,
                        canonical_commit_sha=revision,
                        artifact_path=path,
                        artifact_ref=f"code.storage://{project.state_repo_id}@{revision}/{path}",
                        summary=f"{label}. Nothing generated or published.",
                        workflow_key=KEY,
                    )

    @activity.defn
    async def content_plan_failure(self, run_id: str):
        failure = await self.saved(run_id, "failure") or {}
        await self.db.project_failure(
            run_id=UUID(run_id),
            error_message=failure.get(
                "summary",
                "Content planning could not finish. Saved work and pending revision holds "
                "remain recoverable.",
            ),
        )
