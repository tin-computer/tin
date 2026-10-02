"""Explicit operator completion of one missing answer, never automatic resampling."""

import json
from copy import deepcopy
from uuid import UUID

from tin_lite.organic_audit import (
    AI_ENGINE_POLICY_KEYS,
    AUDIT_POLICY,
    PANEL_PREPARATION_POLICY_KEYS,
    SITE_EVIDENCE_POLICY_KEYS,
    audit_paths,
    audit_policy,
    bundle_sha256,
    digest,
)

KIND = "organic_audit_completion_v1"
# Settings that never change how an answer is requested or graded.
NEUTRAL_KEYS = {
    "version",
    "answer_timeout_seconds",
    "check_applicability",
    "respect_sitemap",
    *SITE_EVIDENCE_POLICY_KEYS,
    *PANEL_PREPARATION_POLICY_KEYS,
    *AI_ENGINE_POLICY_KEYS,
}


def completion_seed(
    *, source_id, project_id, revision, definition_sha, stages, requested_at, target_policy=None
):
    """Reuse only a proven publication; preserve every successfully measured answer."""
    target_policy = target_policy or AUDIT_POLICY
    publication, artifacts = stages["publish"], stages["artifacts"]
    if (
        publication["canonical_commit_sha"] != revision
        or publication["documents_sha256"] != bundle_sha256(source_id, artifacts)
        or publication["artifact_path"] != audit_paths(source_id)["AUDIT.md"]
    ):
        raise ValueError("Source publication proof does not match the completed audit")
    evidence = json.loads(artifacts[audit_paths(source_id)["evidence.json"]])
    scope, crawl, ai = evidence["scope"], evidence["crawl"], evidence["ai_visibility"]
    policy = audit_policy(scope["policy_version"])
    if (
        evidence["run_id"] != source_id
        or evidence["project_id"] != project_id
        or evidence["definition_commit_sha"] != definition_sha
        or evidence["policy"] != policy
        or {k: v for k, v in policy.items() if k not in NEUTRAL_KEYS}
        != {k: v for k, v in target_policy.items() if k not in NEUTRAL_KEYS}
        or scope.get("completion")
        or crawl["status"] != "completed"
        or ai["status"] != "partial"
        or ai["planned"] != len(ai["observations"])
        or ai["completed"] != ai["planned"] - 1
        or stages["brand_checks"]["status"] != "observed"
        or scope != stages["scope"]
        or crawl != stages["crawl"]
        or ai["panel"] != stages["panel"]
    ):
        raise ValueError("Completion requires compatible, proven evidence with one missing answer")
    missing = [item for item in ai["observations"] if item["status"] != "completed"]
    if len(missing) != 1:
        raise ValueError("Exactly one missing answer is required")
    gap = missing[0]
    index = gap["index"]
    if (
        type(index) is not int
        or not 0 <= index < ai["planned"]
        or gap.get("failure_stage") != "answer"
        or gap.get("reason") not in {"provider_result_unavailable", "unconfirmed_previous_request"}
        or gap["answer"]["status"] != "unknown"
        or gap["answer"].get("value")
    ):
        raise ValueError("Only an absent provider answer can be explicitly retried")
    copy_stages = {
        "scope",
        "crawl",
        "crawl_submit",
        "panel",
        "panel_preparation",
        "brand_checks",
        "panel_research",
        "panel_research_recovery",
        "panel_draft",
        "panel_draft_recovery",
        "panel_validation",
        "panel_validation_recovery",
    }
    copied = {key: deepcopy(value) for key, value in stages.items() if key in copy_stages}
    if copied["crawl_submit"]["status"] != "completed":
        raise ValueError("A confirmed source crawl is required")
    for position, observation in enumerate(ai["observations"]):
        if observation.get("index") != position or observation != stages.get(
            f"observation:{position}"
        ):
            raise ValueError("Published observations do not match their trusted receipts")
        if position != index:
            copied[f"observation:{position}"] = deepcopy(observation)
    provenance = {
        "source_run_id": source_id,
        "source_revision": revision,
        "source_definition_commit_sha": definition_sha,
        "source_policy_version": policy["version"],
        "source_evidence_sha256": digest(evidence),
        "requested_at": requested_at,
        "retried_index": index,
        "retained_observations": ai["completed"],
        "original_missing_observation": deepcopy(gap),
        "note": "One explicitly authorized replacement request. "
        "Original attempt and costs remain unchanged.",
    }
    copied["scope"].update(policy_version=target_policy["version"], completion=provenance)
    return copied


async def prepare_completion(activities, run, *, target_policy=None):
    command = run.prerequisite_evidence
    source_id = str(UUID(command["source_run_id"]))
    source = await activities.db.get_run(UUID(source_id))
    if (
        not source
        or source.project_id != run.project_id
        or source.executor != "organic.audit"
        or source.status.value != "succeeded"
        or source.canonical_commit_sha != command["source_revision"]
        or source.input != run.input
    ):
        raise ValueError("The requested completed source audit is unavailable")
    rows = await activities.db.pool.fetch(
        "SELECT execution_key,result FROM effect_receipts "
        "WHERE execution_key LIKE $1 AND status='completed'",
        f"organic:{source_id}:%",
    )
    stages = {
        row["execution_key"].split(":", 2)[2]: json.loads(row["result"])
        if isinstance(row["result"], str)
        else row["result"]
        for row in rows
    }
    copied = completion_seed(
        source_id=source_id,
        project_id=str(run.project_id),
        revision=source.canonical_commit_sha,
        definition_sha=source.definition_commit_sha,
        stages=stages,
        requested_at=run.created_at.isoformat(),
        target_policy=target_policy,
    )
    key = activities.key(str(run.id), "completion")
    async with activities.db.effect_lock(key, KIND) as (conn, existing), conn.transaction():
        if existing and existing.status == "completed":
            return
        await activities._active(str(run.id), conn=conn)
        member = await conn.fetchval(
            "SELECT true FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
            run.project_id,
            run.started_by_clerk_user_id,
        )
        if not member:
            raise ValueError("Completion requires current project membership")
        for stage, result in copied.items():
            target = activities.key(str(run.id), stage)
            if await activities.db.get_effect(target, conn=conn):
                raise ValueError("Completion destination already contains work")
            await activities.db.start_effect(conn, execution_key=target, operation="organic.audit")
            await activities.db.complete_effect(conn, execution_key=target, result=result)
        await activities.db.start_effect(conn, execution_key=key, operation=KIND)
        await activities.db.complete_effect(
            conn, execution_key=key, result=copied["scope"]["completion"]
        )
    await activities.db.mark_run_running(run.id)
