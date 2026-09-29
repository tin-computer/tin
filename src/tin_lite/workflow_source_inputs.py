"""Project-bound provenance for legacy approved-source run receipts."""

from __future__ import annotations

from types import SimpleNamespace

from tin_lite.document_handoff import document_url


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
