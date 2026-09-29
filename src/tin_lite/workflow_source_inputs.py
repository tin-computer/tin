"""Project-bound presentation of approved source choices and pinned run provenance."""

from __future__ import annotations

from types import SimpleNamespace

from tin_lite.document_handoff import document_url


def _present_candidate(candidate, *, project_id, settings):
    view = dict(candidate)
    view["read_url"] = document_url(
        settings, SimpleNamespace(id=view["run_id"], project_id=project_id)
    )
    view["review_url"] = view["read_url"]
    return view


def _slot(*, name, input_name, required, workflow_key, kind, candidates, project_id, settings):
    return {
        "slot": name,
        "input": input_name,
        "kind": kind,
        "required": required,
        "workflow_key": workflow_key,
        "candidates": [
            _present_candidate(candidate, project_id=project_id, settings=settings)
            for candidate in candidates
        ],
        "missing_reason": (
            None
            if candidates
            else "No approved source is available. Review an existing draft in Decisions first."
        ),
    }


async def discover_slots(*, database, storage, settings, project_id, definition, inputs=None):
    """Candidate discovery uses the same complete proof as run admission."""
    from tin_lite.approved_evidence import discover, discover_articles
    from tin_lite.workflow_code import approved_article_input, evidence_specs

    specs = evidence_specs(definition)
    generic = (
        await discover(
            database, storage, project_id=project_id, definition=definition, inputs=inputs
        )
        if specs
        else {}
    )
    slots = [
        _slot(
            name=spec.name,
            input_name=spec.input,
            required=spec.required,
            workflow_key=spec.workflow_key,
            kind="approved_output",
            candidates=generic.get(spec.name, []),
            project_id=project_id,
            settings=settings,
        )
        for spec in specs
    ]
    article_input = approved_article_input(definition)
    if article_input is not None:
        articles = await discover_articles(
            database,
            storage,
            project_id=project_id,
            source_run_id=(inputs or {}).get(article_input),
        )
        slots.append(
            _slot(
                name="approved_article",
                input_name=article_input,
                required=True,
                workflow_key="content.generate",
                kind="approved_article",
                candidates=articles,
                project_id=project_id,
                settings=settings,
            )
        )
    return slots


def source_readiness(readiness, slots):
    """Extend input-free readiness only when a required approved source is absent."""
    if readiness is None:
        return None
    missing = [slot for slot in slots if slot["required"] and not slot["candidates"]]
    if not missing:
        return readiness
    result = {**readiness, "state": "blocked", "unmet": list(readiness.get("unmet") or [])}
    for slot in missing:
        result["unmet"].append(
            {
                "kind": "approved_source",
                "level": "required",
                "satisfied": False,
                "reason": (
                    f"A reviewed {slot['workflow_key']} result is required for {slot['input']}."
                ),
                "workflow_key": slot["workflow_key"],
                "workflow_id": None,
                "title": None,
                "how_to_satisfy": slot["missing_reason"],
            }
        )
    return result


async def source_readiness_for_workflows(
    *, database, storage, settings, project_id, workflows, readiness
):
    """One cheap catalog query; exact source proof waits for inspection and admission."""
    from tin_lite.workflow_code import approved_article_input, evidence_specs

    result = dict(readiness)
    declarations = []
    keys = set()
    for workflow in workflows:
        if workflow.executor != "workflow.code":
            continue
        slots = [
            (spec.name, spec.input, spec.workflow_key, spec.required)
            for spec in evidence_specs(workflow.definition)
        ]
        article_input = approved_article_input(workflow.definition)
        if article_input is not None:
            slots.append(("approved_article", article_input, "content.generate", True))
        if slots:
            declarations.append((workflow.id, slots))
            keys.update(key for _, _, key, required in slots if required)
    if not keys:
        return result
    rows = await database.pool.fetch(
        "SELECT DISTINCT w.key FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id "
        "WHERE r.project_id=$1 AND w.key=ANY($2::text[]) AND r.status='succeeded' "
        "AND r.review_required AND r.review_decision='approved' "
        "AND r.executor IN ('workflow.code','codex.procedure')",
        project_id,
        sorted(keys),
    )
    present = {row["key"] for row in rows}
    for workflow_id, declared in declarations:
        slots = [
            {
                "slot": name,
                "input": input_name,
                "workflow_key": key,
                "required": required,
                "candidates": [True] if key in present else [],
                "missing_reason": "Review an existing draft in Decisions first.",
            }
            for name, input_name, key, required in declared
        ]
        value = source_readiness(result.get(workflow_id), slots)
        if value and value["state"] == "ready" and any(slot["required"] for slot in slots):
            value = {
                **value,
                "state": "advisory",
                "note": (
                    "An approved result may be available; exact source proof is checked "
                    "when you open this workflow and at start."
                ),
            }
        result[workflow_id] = value
    return result


async def selected_run_sources(*, database, settings, run):
    """Only metadata from the admission receipt; never include source text."""
    if run.executor != "workflow.code":
        return {}
    from uuid import UUID

    from tin_lite import code_article_sources, code_evidence
    from tin_lite.approved_evidence import source_metadata

    sources = {}
    receipt = await database.get_effect(code_evidence.source_key(run.id))
    if (
        receipt is not None
        and receipt.status == "completed"
        and receipt.operation == code_evidence.OPERATION
        and isinstance(receipt.result, dict)
    ):
        try:
            sources = source_metadata(receipt.result)
        except (KeyError, TypeError, ValueError):
            sources = {}
    legacy = await database.get_effect(code_article_sources.source_key(run.id))
    if (
        legacy is not None
        and legacy.status == "completed"
        and legacy.operation == code_article_sources.OPERATION
        and isinstance(legacy.result, dict)
    ):
        source = legacy.result
        try:
            source_id = UUID(source["source_run_id"])
        except (KeyError, TypeError, ValueError):
            source_id = None
        if source_id is not None:
            producer = await database.get_run(source_id)
            if producer is not None and producer.project_id == run.project_id:
                sources["approved_article"] = {
                    "present": True,
                    "run_id": str(source_id),
                    "title": source.get("title") or "Approved article",
                    "workflow_key": "content.generate",
                    "artifact_path": source.get("source_path"),
                    "revision": source.get("source_revision"),
                    "sha256": source.get("source_sha256"),
                    "created_at": producer.created_at.isoformat(),
                }
    for source in sources.values():
        if source.get("present") and source.get("run_id"):
            url = document_url(
                settings, SimpleNamespace(id=source["run_id"], project_id=run.project_id)
            )
            source["read_url"] = source["review_url"] = url
    return sources
