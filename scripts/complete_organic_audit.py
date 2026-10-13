"""Operator-authorized one-answer completion. Dry-run unless --apply; no automatic retry."""

import argparse
import asyncio
import json
from uuid import UUID

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite.billing import configure_billing
from tin_lite.code_storage import CodeStorage
from tin_lite.db import Database
from tin_lite.organic_audit import AUDIT_POLICY
from tin_lite.organic_audit_completion import KIND, completion_seed
from tin_lite.settings import Settings
from tin_lite.workflows import OrganicAuditWorkflow


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, type=UUID)
    parser.add_argument("--project", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", action="append", default=[])
    args = parser.parse_args()
    settings = Settings(_env_file=tuple(args.env_file or [".env"]))
    db = Database(settings.runtime_dsn)
    await db.connect()
    try:
        await configure_billing(db, settings)
        source = await db.get_run(args.source_run)
        if (
            not source
            or source.project_id != args.project
            or source.executor != "organic.audit"
            or source.status.value != "succeeded"
        ):
            raise ValueError("Expected completed project audit not found")
        rows = await db.pool.fetch(
            "SELECT execution_key,result FROM effect_receipts "
            "WHERE execution_key LIKE $1 AND status='completed'",
            f"organic:{source.id}:%",
        )
        stages = {
            row["execution_key"].split(":", 2)[2]: json.loads(row["result"])
            if isinstance(row["result"], str)
            else row["result"]
            for row in rows
        }
        copied = completion_seed(
            source_id=str(source.id),
            project_id=str(source.project_id),
            revision=source.canonical_commit_sha,
            definition_sha=source.definition_commit_sha,
            stages=stages,
            requested_at="preflight",
        )
        member = await db.pool.fetchval(
            "SELECT true FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
            source.project_id,
            source.started_by_clerk_user_id,
        )
        if not member:
            raise ValueError("Original requesting user is no longer a project member")
        fact = {
            "apply": args.apply,
            "source_run_id": str(source.id),
            "retained_pages": len(copied["crawl"]["pages"]),
            "retained_observations": copied["scope"]["completion"]["retained_observations"],
            "retry_index": copied["scope"]["completion"]["retried_index"],
        }
        if args.apply:
            row = await db.pool.fetchrow(
                "SELECT current_commit_sha FROM workflows WHERE id=$1 AND status='active'",
                source.workflow_id,
            )
            storage = CodeStorage(
                organization=settings.code_storage_org,
                private_key=settings.code_storage_api_key.get_secret_value(),
                repo_prefix=settings.code_storage_repo_prefix,
            )
            definition = json.loads(
                await storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=row["current_commit_sha"],
                    path="workflows/organic.audit.json",
                )
            )
            if definition.get("audit_policy") != AUDIT_POLICY:
                raise ValueError("Deploy the current completion-compatible audit before applying")
            run, created = await db.create_run(
                project_id=source.project_id,
                workflow_id=source.workflow_id,
                started_by_clerk_user_id=source.started_by_clerk_user_id,
                start_idempotency_key=f"audit-completion:{source.id}",
                input_payload=source.input,
                definition_commit_sha=row["current_commit_sha"],
                pinned_definition=definition,
                trigger_client="api",
                prerequisite_evidence={
                    "kind": KIND,
                    "source_run_id": str(source.id),
                    "source_revision": source.canonical_commit_sha,
                },
            )
            client = await Client.connect(
                settings.temporal_endpoint,
                namespace=settings.temporal_namespace,
                api_key=settings.temporal_api_key.get_secret_value(),
                tls=True,
            )
            try:
                await client.start_workflow(
                    OrganicAuditWorkflow.run,
                    str(run.id),
                    id=run.temporal_workflow_id,
                    task_queue=settings.task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass
            fact.update(run_id=str(run.id), created=created)
        print(json.dumps(fact))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
