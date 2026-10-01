"""Shared membership-scoped selection and receipt-backed source preparation for drafting."""

from __future__ import annotations

import hashlib
from uuid import UUID

from tin_lite import content_draft
from tin_lite.content_draft_progress import check_existing, history, next_item
from tin_lite.content_plan import ANSWER, ARTICLE, KINDS, REFRESH, item_kind, plan_path
from tin_lite.content_plan_sources import positioning_files
from tin_lite.content_programs import ContentPrograms
from tin_lite.content_refresh import url_key
from tin_lite.organic_audit import digest
from tin_lite.project_files import safe_project_file_path
from tin_lite.writing_style import STYLE_PATH

# What a reader calls each kind in a refusal.
KIND_NOUNS = {ARTICLE: "an article", ANSWER: "an answer page", REFRESH: "a page refresh"}
# How a refusal names the workflow to start again once the founder answers.
KEY_START = content_draft.KEY


def answer_brief(selection, evidence, *, route=None, today=None):
    """What an answer draft answers: the item's buyer question, the audit's gap questions it
    cites, and where approved answer pages go."""
    from tin_lite.content_plan_editorial import ANSWER_CHECK

    questions = [
        question["question"]
        for row in evidence
        if (row.get("data") or {}).get("check_id") == ANSWER_CHECK
        for question in ((row["data"].get("verification") or {}).get("questions") or [])
        if isinstance(question, dict) and isinstance(question.get("question"), str)
    ]
    return {
        "question": selection["item"]["title"],
        "gap_questions": list(dict.fromkeys(questions))[:12],
        "route": route,
        "today": today,
    }


class ContentDraftSources:
    def __init__(self, *, database, storage, integrations=None, refreshes=None):
        self.db, self.storage, self.integrations = database, storage, integrations
        self.programs = ContentPrograms(database=database, storage=storage)
        # content.refresh's evidence for a refresh item; tests pass a fixture reader and clock.
        self._refreshes = refreshes

    def refreshes(self):
        if self._refreshes is None:
            from tin_lite.content_refresh_sources import ContentRefreshSources

            self._refreshes = ContentRefreshSources(
                database=self.db, storage=self.storage, integrations=self.integrations
            )
        return self._refreshes

    async def waiting(self, project_id, plan):
        """Refresh items whose page still waits for an earlier refresh's results."""
        pages = {
            url_key(item["destination"])
            for batch in plan["batches"]
            for item in batch["items"]
            if item_kind(item) == REFRESH
        }
        if not pages:
            return set()
        return pages & await self.refreshes().waiting_pages(project_id)

    async def discover(self, *, project_id, program_id=None, kinds=None):
        """The program's items and the next one to draft.

        `kinds` are the kinds the drafting definition writes (all, by default); an item of
        another kind, or a refresh whose page still waits, is passed over, not drafted.
        """
        if program_id is None:
            rows = await self.db.pool.fetch(
                "SELECT p.project_workflow_id AS id, w.name FROM content_programs p "
                "JOIN project_workflows w ON w.id=p.project_workflow_id "
                "WHERE p.project_id=$1 AND p.plan_revision IS NOT NULL AND w.status<>'archived' "
                "ORDER BY p.created_at DESC, p.project_workflow_id LIMIT 100",
                project_id,
            )
            return {"programs": [dict(row) for row in rows]}
        read = await self.programs.read(project_id=project_id, program_id=program_id)
        facts = await self.programs.facts(program_id)
        held = set((facts["pending_revision"] or {}).get("batch_ids", []))
        configured = await self.programs.configured(project_id, program_id)
        progress = await history(self.db.pool, project_id=project_id, program_id=program_id)
        kinds = tuple(kinds or KINDS)
        waiting = await self.waiting(project_id, read["plan"])
        items = []
        for batch in sorted(read["plan"]["batches"], key=lambda b: b["due_date"]):
            for item in batch["items"]:
                prior = progress.get(item["id"])
                assessment = (prior or {}).get("assessment")
                changed = bool(prior and prior["brief_sha256"] != digest(item))
                kind = item_kind(item)
                passed_over = (
                    "unsupported_kind"
                    if kind not in kinds
                    else "refresh_waiting"
                    if kind == REFRESH and url_key(item["destination"]) in waiting
                    else None
                )
                eligible = (
                    item["readiness"] != "deferred"
                    and batch["id"] not in held
                    and configured.status == "active"
                    and passed_over is None
                )
                items.append(
                    {
                        **item,
                        "kind": kind,
                        "passed_over": passed_over,
                        "batch_id": batch["id"],
                        "due_date": batch["due_date"],
                        "held": batch["id"] in held,
                        "draft": prior,
                        "brief_changed": bool(
                            prior
                            and prior["brief_sha256"]
                            and prior["brief_sha256"] != digest(item)
                        ),
                        "available": eligible
                        and not (
                            prior
                            and (
                                prior["has_output"]
                                or prior["stage"] in {"drafting", "saved_result"}
                            )
                        )
                        and not (assessment and not changed),
                        "can_rewrite": eligible
                        and bool(
                            assessment
                            or (prior and prior["stage"] in {"awaiting_review", "drafted"})
                        ),
                    }
                )
        upcoming = next_item(items)
        active = next((p for p in progress.values() if p["stage"] == "drafting"), None)
        ready = bool(upcoming and upcoming["available"] and not active)
        reason = (
            "Resume this content program before drafting."
            if configured.status != "active"
            else f"An article is already drafting. Open run {active['run_id']}."
            if active
            else "All non-deferred articles have drafts or are already covered."
            if upcoming is None
            else "Finish the pending revision for the next batch before drafting."
            if upcoming["held"]
            else "Read the next item's editorial assessment. Revise the brief or explicitly "
            "recheck it before continuing."
            if (upcoming.get("draft") or {}).get("assessment") and not ready
            else "Resolve the next article's saved result before drafting."
            if not ready
            else ""
        )
        return {
            "program_id": str(program_id),
            "plan_revision": read["revision"],
            "host": read["plan"]["host"],
            "items": items,
            "next": {
                "item_id": upcoming["id"] if upcoming else None,
                "available": ready,
                "reason": reason,
                "run_id": active["run_id"]
                if active
                else (upcoming or {}).get("draft", {}).get("run_id")
                if (upcoming or {}).get("draft")
                else None,
            },
            "progress": {
                "total": len(items),
                "drafted": sum(
                    bool(i["draft"] and i["draft"]["stage"] == "drafted") for i in items
                ),
                "awaiting_review": sum(
                    bool(i["draft"] and i["draft"]["stage"] == "awaiting_review") for i in items
                ),
                "drafting": sum(
                    bool(i["draft"] and i["draft"]["stage"] == "drafting") for i in items
                ),
                "deferred": sum(i["readiness"] == "deferred" for i in items),
                "already_covered": sum(
                    bool(
                        i["draft"]
                        and i["draft"]["stage"] == "already_covered"
                        and not i["brief_changed"]
                    )
                    for i in items
                ),
                "needs_attention": sum(
                    bool(
                        i["draft"]
                        and i["draft"]["stage"] in {"needs_replanning", "insufficient_evidence"}
                        and not i["brief_changed"]
                    )
                    for i in items
                ),
            },
            "instruction": "Start content.generate with only program_id to assess and, if useful, "
            "draft the next article "
            "in plan order. Use item_id only when the user explicitly chooses another article; "
            "rewrite=true additionally requires that explicit item_id. Reuse the request ID for "
            "retries. Plan dates are editorial dates, "
            "not automatic publication. Already-covered items are recorded separately from drafts. "
            "A no-draft run never starts another item. Missing coverage or a brief needing "
            "revision "
            "holds the next selection until the brief changes or the user explicitly rechecks it "
            "with item_id and rewrite=true. Revisions are tool metadata, not a user choice.",
        }

    async def choose(self, *, project_id, inputs, retry_of_run_id=None, kinds=None):
        """Select one item for a draft run. `kinds` are the kinds the pinned definition drafts
        (content_draft.supported_kinds); an answer also needs the founder's chosen route."""
        kinds = tuple(kinds or KINDS)
        selected_inputs = dict(inputs)
        mode = "selected" if inputs.get("item_id") else "next"
        if inputs.get("rewrite") and not inputs.get("item_id"):
            raise ValueError("Choose a specific article before requesting a rewrite.")
        if retry_of_run_id:
            retried = await self.db.get_run(retry_of_run_id)
            if (
                not retried
                or retried.project_id != project_id
                or retried.status.value != "failed"
                or retried.input.get("program_id") != inputs["program_id"]
            ):
                raise ValueError("The selected failed draft cannot be retried.")
            old = await self.selection(retried.id) or await self.saved(retried.id)
            item_id = old["item"]["id"] if old else retried.input.get("item_id")
            if not item_id:
                raise ValueError(
                    "The failed run has no selected article. Start a new next-article request."
                )
            selected_inputs.update(
                item_id=item_id,
                plan_revision=old["plan_revision"]
                if old
                else retried.input.get("plan_revision", ""),
            )
            mode = "retry"
        elif not inputs.get("item_id"):
            discovery = await self.discover(
                project_id=project_id, program_id=UUID(inputs["program_id"]), kinds=kinds
            )
            if not discovery["next"]["available"]:
                raise ValueError(discovery["next"]["reason"])
            selected_inputs.update(
                item_id=discovery["next"]["item_id"], plan_revision=discovery["plan_revision"]
            )
        selected = await self.select(project_id=project_id, inputs=selected_inputs)
        prior = await history(
            self.db.pool, project_id=project_id, program_id=UUID(inputs["program_id"])
        )
        check_existing(
            prior.get(selected["item"]["id"]),
            rewrite=inputs.get("rewrite", False),
            item=selected["item"],
        )
        route = await self.check_kind(project_id, selected, kinds)
        return {
            **selected,
            **({"page_route": route} if route else {}),
            "mode": mode,
            "input_sha256": digest(inputs),
        }

    async def check_kind(self, project_id, selected, kinds):
        """Refuse an item this definition cannot draft yet; return an answer's chosen route.

        An older content.generate drafts articles only. A refresh waits while its page's last
        refresh is in review or not yet six weeks live. An answer page goes to the site at the
        route the founder chose for such pages (#244), so Tin asks for it before drafting.
        """
        item = selected["item"]
        kind = item_kind(item)
        if kind not in kinds:
            raise ValueError(
                f"This version of content.generate drafts {', '.join(kinds)} items only, and "
                f'"{item["title"]}" is {KIND_NOUNS[kind]}. Start the current version to draft it.'
            )
        if kind == REFRESH and await self.waiting(project_id, {"batches": [{"items": [item]}]}):
            raise ValueError(
                f"{item['destination']} has a refresh in review, waiting to go live, or live for "
                "less than six weeks. Tin waits so each refresh can show whether it worked."
            )
        if kind != ANSWER:
            return None
        from tin_lite.page_routes import PageRouteService, route_question

        routes = (await PageRouteService(database=self.db, storage=self.storage).read(project_id))[
            "routes"
        ]
        if not routes.get("answer_page"):
            raise ValueError(route_question("answer_page", selected["host"], KEY_START))
        return routes["answer_page"]

    async def selection(self, run_id):
        receipt = await self.db.get_effect(content_draft.selection_key(run_id))
        return receipt.result if receipt and receipt.status == "completed" else None

    async def select(self, *, project_id, inputs):
        program_id = UUID(inputs["program_id"])
        configured = await self.programs.configured(project_id, program_id)
        if configured.status != "active":
            raise ValueError("Resume this content program before drafting.")
        state = await self.db.pool.fetchrow(
            "SELECT initial_run_id, plan_revision FROM content_programs "
            "WHERE project_workflow_id=$1 AND project_id=$2",
            program_id,
            project_id,
        )
        if not state or not state["plan_revision"]:
            raise ValueError("Run this content program to create its roadmap first.")
        initial = await self.db.get_run(state["initial_run_id"])
        if not initial or initial.project_id != project_id or initial.status.value != "succeeded":
            raise ValueError("The program's initial publication is not available.")
        initial_context = await self.db.get_effect(f"content:{initial.id}:context")
        if not initial_context or initial_context.status != "completed":
            raise ValueError("The program's pinned source context is unavailable.")
        current = await self.programs.read(project_id=project_id, program_id=program_id)
        revision = inputs.get("plan_revision") or current["revision"]
        if revision == current["revision"]:
            selected = current
        else:
            selected = await self.programs.read(
                project_id=project_id, program_id=program_id, revision=revision
            )
        plan = selected["plan"]
        if any(
            plan[field] != initial_context.result["plan"][field] for field in ("host", "market")
        ):
            raise ValueError("The plan's site or market differs from its initial research.")
        for field in ("audit_run_id", "keyword_run_id", "start_date"):
            if plan[field] != configured.inputs[field]:
                raise ValueError("The plan's research or dates differ from its content program.")
        candidates = [
            (batch, item)
            for batch in plan["batches"]
            for item in batch["items"]
            if item["id"] == inputs["item_id"]
        ]
        if len(candidates) != 1:
            raise ValueError("Choose an existing item in this content plan.")
        batch, item = candidates[0]
        latest = [
            (b["id"], b["due_date"], i)
            for b in current["plan"]["batches"]
            for i in b["items"]
            if i["id"] == item["id"]
        ]
        if latest != [(batch["id"], batch["due_date"], item)]:
            raise ValueError("This brief changed after selection. Refresh it before drafting.")
        facts = await self.programs.facts(program_id)
        if item["readiness"] == "deferred":
            raise ValueError("This item is deferred. Update its plan before drafting.")
        if batch["id"] in (facts["pending_revision"] or {}).get("batch_ids", []):
            raise ValueError("Finish the pending revision for this batch before drafting.")
        return {
            "program_id": str(program_id),
            "initial_run_id": str(initial.id),
            "plan_path": plan_path(program_id),
            "plan_revision": revision,
            "project_revision": current["revision"],
            "batch_id": batch["id"],
            "due_date": batch["due_date"],
            "host": plan["host"],
            "market": plan["market"],
            "item": item,
        }

    async def saved(self, run_id):
        receipt = await self.db.get_effect(content_draft.receipt_key(run_id))
        return receipt.result if receipt and receipt.status == "completed" else None

    async def prepare(
        self, run, *, output_validator=content_draft.VALIDATOR, positioning=False, kinds=None
    ):
        """Pin the selected item's brief, evidence and style before compute.

        `kinds` are the kinds the pinned definition drafts (articles only by default). An
        answer or a refresh records its kind and its own sources; an article's context is
        exactly what earlier versions prepared.
        """
        kinds = tuple(kinds or (ARTICLE,))
        if output_validator not in content_draft.VALIDATORS:
            raise ValueError("Unsupported draft output contract.")
        key = content_draft.receipt_key(run.id)
        async with self.db.effect_lock(key, content_draft.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                if receipt.result["input_sha256"] != digest(run.input):
                    raise ValueError("Draft inputs changed after preparation.")
                if (
                    receipt.result.get("output_validator", content_draft.VALIDATOR)
                    != output_validator
                ):
                    raise ValueError("Draft contract changed after preparation.")
                return receipt.result
            if not await self.db.has_project_access(
                project_id=run.project_id, clerk_user_id=run.started_by_clerk_user_id
            ):
                raise LookupError("project not found")
            if run.review_source_run_id is not None:
                command = await conn.fetchrow(
                    "SELECT artifact_run_id FROM workflow_review_commands "
                    "WHERE successor_run_id=$1",
                    run.id,
                )
                if not command:
                    raise ValueError("Revision admission is missing its original source.")
                original = await self.saved(command["artifact_run_id"])
                if (
                    not original
                    or original.get("output_validator") not in content_draft.CLEAN_VALIDATORS
                ):
                    raise ValueError("The original clean draft context is unavailable.")
                context = {
                    **original,
                    "input_sha256": digest(run.input),
                    "output_validator": output_validator,
                }
                context["frontmatter"] = content_draft.provenance(context)
                if context.get("refresh"):
                    # The revised refresh applies to the same page text, so it pins the same
                    # refresh context under its own run for history, results and delivery.
                    await self.pin_refresh(run.id, context["refresh"])
                await self.db.start_effect(conn, execution_key=key, operation=content_draft.KEY)
                await self.db.complete_effect(conn, execution_key=key, result=context)
                return context
            chosen = await self.selection(run.id)
            if chosen and chosen["input_sha256"] != digest(run.input):
                raise ValueError("Draft inputs changed after selection.")
            inputs = (
                {
                    **run.input,
                    "item_id": chosen["item"]["id"],
                    "plan_revision": chosen["plan_revision"],
                }
                if chosen
                else run.input
            )
            if not inputs.get("item_id"):
                raise ValueError("The run is missing its pinned article selection.")
            selection = await self.select(project_id=run.project_id, inputs=inputs)
            kind = item_kind(selection["item"])
            if kind not in kinds:
                raise ValueError(
                    f"This version of content.generate drafts articles only, and "
                    f'"{selection["item"]["title"]}" is {KIND_NOUNS[kind]}.'
                )
            project = await self.db.get_project(run.project_id)
            style = await self.storage.read_canonical_artifact_if_exists(
                repo_id=project.state_repo_id,
                commit_sha=selection["project_revision"],
                path=STYLE_PATH,
            )
            if style is not None and len(style) > 24_000:
                raise ValueError("The writing guide exceeds 24 KB; shorten it before drafting.")
            # Reuse trusted planning receipts. Only initial/applied revisions belong to this
            # program; discarded proposals cannot become evidence for a selected brief.
            applied = await conn.fetch(
                "SELECT run_id FROM content_plan_revisions WHERE project_workflow_id=$1 "
                "AND project_id=$2 AND status='applied' ORDER BY updated_at DESC LIMIT 20",
                UUID(selection["program_id"]),
                run.project_id,
            )
            run_ids = [str(row["run_id"]) for row in applied] + [selection["initial_run_id"]]
            wanted, evidence = set(selection["item"]["source_ids"]), {}
            research = {}
            for source_run_id in run_ids:
                context_receipt = await self.db.get_effect(f"content:{source_run_id}:context")
                if not context_receipt or context_receipt.status != "completed":
                    continue
                context = context_receipt.result
                research = research or (context.get("research") or {}).get("sources", {})
                for row in (context.get("research") or {}).get("rows", []):
                    if row["source_id"] in wanted and row["source_id"] not in evidence:
                        evidence[row["source_id"]] = {**row, "planning_run_id": source_run_id}
                pages_receipt = await self.db.get_effect(f"content:{source_run_id}:pages")
                if pages_receipt and pages_receipt.status == "completed":
                    for page in pages_receipt.result.get("pages", []):
                        if page.get("source_id") in wanted and page["source_id"] not in evidence:
                            evidence[page["source_id"]] = {**page, "planning_run_id": source_run_id}
            # File IDs name editable paths, unlike immutable research/page IDs. Read their
            # exact prepared checkout revision, never a later amendment's copy of the file.
            file_bytes = 0
            for source_id in sorted(wanted):
                if not source_id.startswith("file:"):
                    continue
                path = source_id.removeprefix("file:")
                if not safe_project_file_path(path):
                    raise ValueError("The brief references an unsafe context file path.")
                content = await self.storage.read_canonical_artifact_if_exists(
                    repo_id=project.state_repo_id,
                    commit_sha=selection["project_revision"],
                    path=path,
                )
                if content is None:
                    continue
                file_bytes += len(content)
                if len(content) > 20_000 or file_bytes > 60_000:
                    raise ValueError("Draft context files exceed 20 KB each or 60 KB together.")
                evidence[source_id] = {
                    "source_id": source_id,
                    "path": path,
                    "revision": selection["project_revision"],
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content": content.decode("utf-8"),
                }
            context = content_draft.bounded(
                {
                    **selection,
                    "input_sha256": digest(run.input),
                    "research": research,
                    "evidence": list(evidence.values()),
                    "unresolved_source_ids": sorted(wanted - evidence.keys()),
                    "style": {
                        "path": STYLE_PATH,
                        "revision": selection["project_revision"],
                        "sha256": hashlib.sha256(style).hexdigest() if style is not None else None,
                    },
                    "verification": [
                        {"id": f"v{i}", "requirement": requirement}
                        for i, requirement in enumerate(selection["item"]["verification"], 1)
                    ],
                }
            )
            if positioning:
                # Paths and digests only; the writer reads the files from the pinned checkout.
                context["positioning"] = [
                    {key: item[key] for key in ("path", "revision", "sha256")}
                    for item in await positioning_files(
                        storage=self.storage,
                        project=project,
                        revision=selection["project_revision"],
                    )
                ]
            context["frontmatter"] = content_draft.provenance(context)
            if output_validator in content_draft.CLEAN_VALIDATORS:
                context["output_validator"] = output_validator
                context["frontmatter"] = content_draft.provenance(context)
            if kind == ANSWER:
                context["kind"] = ANSWER
                context["answer"] = answer_brief(
                    selection,
                    context["evidence"],
                    route=(chosen or {}).get("page_route"),
                    today=(run.created_at.date().isoformat() if run.created_at else None),
                )
            elif kind == REFRESH:
                context["kind"] = REFRESH
                context["refresh"] = await self.refreshes().planned_page(
                    run,
                    url=selection["item"]["destination"],
                    revision=selection["project_revision"],
                )
            context = content_draft.bounded(context)
            await self.db.start_effect(conn, execution_key=key, operation=content_draft.KEY)
            await self.db.complete_effect(conn, execution_key=key, result=context)
            return context

    async def pin_refresh(self, run_id, refresh_context):
        from tin_lite.content_refresh import PREPARATION

        key = f"{UUID(str(run_id))}:{PREPARATION}"
        async with self.db.effect_lock(key, PREPARATION) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=PREPARATION)
            await self.db.complete_effect(conn, execution_key=key, result=refresh_context)


async def pinned_kinds(storage, workflow, run):
    """The kinds the run's own pinned content.generate definition drafts."""
    import json

    if workflow.current_commit_sha == run.definition_commit_sha:
        return content_draft.supported_kinds(workflow.definition)
    raw = await storage.read_canonical_artifact(
        repo_id=workflow.definition_repo_id,
        commit_sha=run.definition_commit_sha,
        path=workflow.definition_path,
    )
    return content_draft.supported_kinds(json.loads(raw))


class ScheduledDraftHold(Exception):
    """This occurrence waits; the schedule stays active and tries again next time."""


class ScheduledDraftStop(ValueError):
    """The schedule cannot draft again until someone acts; the message says what to do."""


async def scheduled_selection(
    *, database, storage, integrations, project_id, inputs, kinds=(ARTICLE,)
):
    """Choose the next plan article for one weekly occurrence, as a manual start would.

    One draft waits for review at a time: while any article from this program waits for
    review or is still drafting, the occurrence holds. A plan with nothing left to draft, or a
    next article that needs the founder, stops the schedule with that reason instead of
    failing a run every week.
    """
    if inputs.get("item_id") or inputs.get("rewrite"):
        raise ScheduledDraftStop(
            "A weekly schedule drafts the next article in plan order. "
            "Clear the chosen article in this schedule to keep it running."
        )
    sources = ContentDraftSources(database=database, storage=storage, integrations=integrations)
    program_id = UUID(inputs["program_id"])
    try:
        program = await sources.programs.configured(project_id, program_id)
    except LookupError as exc:
        raise ScheduledDraftStop(
            "The content program behind this schedule is no longer available."
        ) from exc
    if program.status != "active":
        raise ScheduledDraftHold()
    discovery = await sources.discover(project_id=project_id, program_id=program_id, kinds=kinds)
    progress, upcoming = discovery["progress"], discovery["next"]
    if progress["awaiting_review"] or progress["drafting"]:
        raise ScheduledDraftHold()
    if not upcoming["available"]:
        if upcoming["item_id"] is None and any(
            item["passed_over"] == "refresh_waiting" for item in discovery["items"]
        ):
            raise ScheduledDraftHold()  # A waiting page comes due on its own.
        if upcoming["item_id"] is None:
            raise ScheduledDraftStop(
                "Every planned article has a draft or is already covered. "
                "Extend the content plan, then resume this schedule."
            )
        if next(item for item in discovery["items"] if item["id"] == upcoming["item_id"])["held"]:
            raise ScheduledDraftHold()  # A pending plan revision finishes on its own.
        raise ScheduledDraftStop(upcoming["reason"])
    selected = await sources.choose(project_id=project_id, inputs=inputs, kinds=kinds)
    if inputs.get("delivery") == "program":
        from tin_lite.content_delivery import ContentDelivery

        selected["delivery"] = await ContentDelivery(
            database=database, storage=storage, integrations=integrations
        ).pin(project_id=project_id, selected=selected)
    return selected
