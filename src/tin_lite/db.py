from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import math
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

import asyncpg

from tin_lite import analytics
from tin_lite.domain import (
    PROJECT_TASK_WORKFLOW_NAME,
    ActivityEvent,
    ChatMessage,
    EffectReceipt,
    IntegrationAuthAttempt,
    IntegrationCallReceipt,
    IntegrationConnection,
    Project,
    ProjectInvitation,
    ProjectMembership,
    ProjectTaskEntry,
    ProjectTestIdentity,
    ProjectWorkflow,
    RunRollout,
    RunStatus,
    RunToolGrant,
    SideEffectConflictError,
    StaleSettingsRevisionError,
    StoppedRunHandle,
    StudioUsage,
    TestIdentitySms,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
    Workspace,
)
from tin_lite.projects import ProjectCreationConflictError
from tin_lite.rollouts import RolloutFile
from tin_lite.usage_capture import borrowed_connection, effect_connection

logger = logging.getLogger(__name__)
_warned_unknown_workflow_system_ids: set[str] = set()

# One cheap, Postgres-only eligibility predicate for Decisions and its count.
# Applying decisions stay visible until their uncertain outcome is settled.
_PENDING_OUTPUT_CONFLICT_SQL = """
    run.executor IN ('codex.procedure', 'style.capture', 'workflow.code')
    AND run.status IN ('failed', 'stopped') AND NOT run.lease_active
    AND run.canonical_commit_sha IS NULL
    AND run.retained_output->>'reason' = 'output_conflict'
    AND (run.output_resolution IS NULL OR run.output_resolution->>'state' = 'applying')
    AND EXISTS (
        SELECT 1 FROM effect_receipts AS receipt
        WHERE receipt.execution_key = run.id::text || CASE WHEN run.executor='style.capture'
            THEN ':style_artifact_persist' ELSE ':procedure_artifact_persist' END
          AND receipt.status = 'completed'
          AND receipt.result->'checkpoint' = run.retained_output - 'reason'
          AND receipt.result->'checkpoint'->>'run_id' = run.id::text
          AND receipt.result->'checkpoint'->>'project_id' = run.project_id::text
          AND receipt.result->'checkpoint'->>'generation' = run.generation::text
          AND receipt.result->'checkpoint'->>'source_base_sha' = run.expected_head_sha
          AND receipt.result->'checkpoint'->>'definition_commit_sha' = run.definition_commit_sha
    )
"""


def _email_campaign_progress_text(
    *,
    campaign_status: str,
    total: int,
    resolved: int,
    ready: int,
    next_delivery_at: datetime | None,
    send_timezone: str,
) -> tuple[str, str]:
    if campaign_status == "draft":
        noun = "email" if total == 1 else "emails"
        return "review", f"Preparing {total} {noun} for review"
    if resolved >= total:
        return "finishing", "Finishing the approved email campaign"
    if ready:
        remaining = total - resolved
        noun = "email" if remaining == 1 else "emails"
        return "sending", f"Sending {remaining} approved {noun}"
    if next_delivery_at is not None:
        local_next = next_delivery_at.astimezone(ZoneInfo(send_timezone))
        return "waiting", (
            f"Waiting until {local_next.strftime('%a %H:%M').lower()} for the next approved email"
        )
    return "waiting", "Waiting for the next approved email"


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.billing = None
        self._pool: asyncpg.Pool | None = None
        self._chat_request_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database is not connected")
        return self._pool

    async def ping(self) -> bool:
        return bool(await self.pool.fetchval("SELECT true"))

    async def create_project(self, *, name: str, state_repo_id: str) -> Project:
        project_id = uuid4()
        workspace_id = uuid4()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO workspaces (id, name) VALUES ($1, $2)",
                workspace_id,
                name,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO projects (id, workspace_id, name, state_repo_id)
                VALUES ($1, $2, $3, $4)
                RETURNING *, $3::text AS workspace_name,
                          false AS can_create_project_in_workspace
                """,
                project_id,
                workspace_id,
                name,
                state_repo_id,
            )
            assert row is not None
            return _project(row)

    async def get_project(
        self, project_id: UUID, *, conn: asyncpg.Connection | None = None
    ) -> Project | None:
        row = await (conn or self.pool).fetchrow(
            """
            SELECT projects.*, workspaces.name AS workspace_name,
                   false AS can_create_project_in_workspace
            FROM projects
            JOIN workspaces ON workspaces.id = projects.workspace_id
            WHERE projects.id = $1
            """,
            project_id,
        )
        return _project(row) if row else None

    async def list_projects(self) -> list[Project]:
        rows = await self.pool.fetch(
            """
            SELECT projects.*, workspaces.name AS workspace_name,
                   false AS can_create_project_in_workspace
            FROM projects
            JOIN workspaces ON workspaces.id = projects.workspace_id
            ORDER BY projects.created_at
            """
        )
        return [_project(row) for row in rows]

    async def record_tin_user(self, clerk_user_id: str) -> bool:
        """Upsert the Clerk identity; True only the first time it enters Tin Lite."""
        inserted = await self.pool.fetchval(
            """
            INSERT INTO tin_users (clerk_user_id)
            VALUES ($1)
            ON CONFLICT (clerk_user_id) DO UPDATE
            SET last_seen_at = now()
            WHERE tin_users.last_seen_at < now() - interval '5 minutes'
            RETURNING (xmax = 0) AS inserted
            """,
            clerk_user_id,
        )
        await self.grant_welcome_credit(clerk_user_id)
        return bool(inserted)

    async def grant_welcome_credit(self, clerk_user_id: str) -> None:
        billing = getattr(self, "billing", None)
        if billing is not None:
            await billing.grant_welcome_credit(clerk_user_id)
            await billing.ensure_hosted_projects(clerk_user_id)

    async def list_projects_for_user(self, clerk_user_id: str) -> list[Project]:
        # Both HTTP and MCP use this projection after first-project/invitation setup.
        await self.grant_welcome_credit(clerk_user_id)
        rows = await self.pool.fetch(
            """
            SELECT projects.*, workspaces.name AS workspace_name,
                   EXISTS (
                       SELECT 1
                       FROM workspace_memberships
                       WHERE workspace_memberships.workspace_id = projects.workspace_id
                         AND workspace_memberships.clerk_user_id = $1
                   ) AS can_create_project_in_workspace,
                   (
                       SELECT count(*)
                       FROM project_memberships AS members
                       WHERE members.project_id = projects.id
                   ) AS member_count
            FROM projects
            JOIN project_memberships
              ON project_memberships.project_id = projects.id
            JOIN workspaces ON workspaces.id = projects.workspace_id
            WHERE project_memberships.clerk_user_id = $1
              AND projects.deleted_at IS NULL
            ORDER BY workspaces.created_at, projects.created_at, projects.id
            """,
            clerk_user_id,
        )
        return [_project(row) for row in rows]

    async def list_workspaces_for_user(self, clerk_user_id: str) -> list[Workspace]:
        rows = await self.pool.fetch(
            """
            SELECT workspaces.*
            FROM workspaces
            JOIN workspace_memberships
              ON workspace_memberships.workspace_id = workspaces.id
            WHERE workspace_memberships.clerk_user_id = $1
            ORDER BY workspaces.created_at, workspaces.id
            """,
            clerk_user_id,
        )
        return [_workspace(row) for row in rows]

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM workspaces WHERE id = $1",
            workspace_id,
        )
        return _workspace(row) if row else None

    async def has_workspace_access(self, *, workspace_id: UUID, clerk_user_id: str) -> bool:
        return bool(
            await self.pool.fetchval(
                """
                SELECT true
                FROM workspace_memberships
                WHERE workspace_id = $1 AND clerk_user_id = $2
                """,
                workspace_id,
                clerk_user_id,
            )
        )

    async def grant_workspace_membership(self, *, workspace_id: UUID, clerk_user_id: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO workspace_memberships (workspace_id, clerk_user_id)
            VALUES ($1, $2)
            ON CONFLICT (workspace_id, clerk_user_id) DO UPDATE
            SET clerk_user_id = EXCLUDED.clerk_user_id
            """,
            workspace_id,
            clerk_user_id,
        )

    async def bootstrap_personal_project(
        self,
        *,
        workspace_id: UUID,
        workspace_name: str,
        project_id: UUID,
        name: str,
        state_repo_id: str,
        clerk_user_id: str,
        reuse_existing: bool,
    ) -> Project:
        """Give a projectless user one deterministic project under concurrent retries."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"personal-project:{clerk_user_id}",
            )
            if reuse_existing:
                existing = await conn.fetchrow(
                    """
                    SELECT projects.*, workspaces.name AS workspace_name,
                           EXISTS (
                               SELECT 1
                               FROM workspace_memberships
                               WHERE workspace_memberships.workspace_id = projects.workspace_id
                                 AND workspace_memberships.clerk_user_id = $1
                           ) AS can_create_project_in_workspace
                    FROM projects
                    JOIN project_memberships
                      ON project_memberships.project_id = projects.id
                    JOIN workspaces ON workspaces.id = projects.workspace_id
                    WHERE project_memberships.clerk_user_id = $1
                    ORDER BY projects.created_at
                    LIMIT 1
                    """,
                    clerk_user_id,
                )
            else:
                existing = await conn.fetchrow(
                    """
                    SELECT projects.*, workspaces.name AS workspace_name,
                           true AS can_create_project_in_workspace
                    FROM projects
                    JOIN project_memberships
                      ON project_memberships.project_id = projects.id
                    JOIN workspaces ON workspaces.id = projects.workspace_id
                    WHERE projects.id = $1
                      AND project_memberships.clerk_user_id = $2
                    """,
                    project_id,
                    clerk_user_id,
                )
            if existing is not None:
                return _project(existing)

            await conn.execute(
                """
                INSERT INTO workspaces (id, name, created_by_clerk_user_id)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
                """,
                workspace_id,
                workspace_name,
                clerk_user_id,
            )
            await conn.execute(
                """
                INSERT INTO workspace_memberships (workspace_id, clerk_user_id)
                VALUES ($1, $2)
                ON CONFLICT (workspace_id, clerk_user_id) DO NOTHING
                """,
                workspace_id,
                clerk_user_id,
            )
            await conn.execute(
                """
                INSERT INTO projects (
                    id, workspace_id, name, state_repo_id, created_by_clerk_user_id
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT DO NOTHING
                """,
                project_id,
                workspace_id,
                name,
                state_repo_id,
                clerk_user_id,
            )
            project = await conn.fetchrow(
                """
                SELECT projects.*, workspaces.name AS workspace_name,
                       true AS can_create_project_in_workspace
                FROM projects
                JOIN workspaces ON workspaces.id = projects.workspace_id
                WHERE projects.id = $1
                """,
                project_id,
            )
            if (
                project is None
                or project["state_repo_id"] != state_repo_id
                or project["workspace_id"] != workspace_id
            ):
                raise RuntimeError("personal project identity conflicts with existing state")
            await conn.execute(
                """
                INSERT INTO project_memberships (project_id, clerk_user_id)
                VALUES ($1, $2)
                ON CONFLICT (project_id, clerk_user_id) DO NOTHING
                """,
                project_id,
                clerk_user_id,
            )
            return _project(project)

    async def create_workspace_project(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID,
        name: str,
        state_repo_id: str,
        clerk_user_id: str,
        request_id: UUID,
    ) -> Project:
        """Create one project idempotently without broadening workspace access."""
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"workspace-project:{workspace_id}:{request_id}",
                )
                allowed = await conn.fetchval(
                    """
                    SELECT true
                    FROM workspace_memberships
                    WHERE workspace_id = $1 AND clerk_user_id = $2
                    """,
                    workspace_id,
                    clerk_user_id,
                )
                if not allowed:
                    raise LookupError("workspace not found")

                existing = await conn.fetchrow(
                    """
                    SELECT projects.*, workspaces.name AS workspace_name,
                           true AS can_create_project_in_workspace
                    FROM projects
                    JOIN workspaces ON workspaces.id = projects.workspace_id
                    WHERE projects.workspace_id = $1
                      AND projects.creation_request_id = $2
                    """,
                    workspace_id,
                    request_id,
                )
                if existing is not None:
                    if existing["name"] != name or existing["state_repo_id"] != state_repo_id:
                        raise ProjectCreationConflictError(
                            "request_id was already used for another project"
                        )
                    return _project(existing)

                duplicate_name = await conn.fetchval(
                    """
                    SELECT true
                    FROM projects
                    WHERE workspace_id = $1 AND lower(name) = lower($2)
                      AND deleted_at IS NULL
                    """,
                    workspace_id,
                    name,
                )
                if duplicate_name:
                    raise ProjectCreationConflictError(
                        "a project with this name already exists in the workspace"
                    )

                await conn.execute(
                    """
                    INSERT INTO projects (
                        id, workspace_id, name, state_repo_id,
                        created_by_clerk_user_id, creation_request_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    project_id,
                    workspace_id,
                    name,
                    state_repo_id,
                    clerk_user_id,
                    request_id,
                )
                await conn.execute(
                    """
                    INSERT INTO project_memberships (project_id, clerk_user_id)
                    VALUES ($1, $2)
                    """,
                    project_id,
                    clerk_user_id,
                )
                project = await conn.fetchrow(
                    """
                    SELECT projects.*, workspaces.name AS workspace_name,
                           true AS can_create_project_in_workspace
                    FROM projects
                    JOIN workspaces ON workspaces.id = projects.workspace_id
                    WHERE projects.id = $1
                    """,
                    project_id,
                )
                assert project is not None
                return _project(project)
        except asyncpg.UniqueViolationError as exc:
            raise ProjectCreationConflictError(
                "the project name or request_id is already in use"
            ) from exc

    async def has_project_access(
        self, *, project_id: UUID, clerk_user_id: str, conn: asyncpg.Connection | None = None
    ) -> bool:
        return bool(
            await (conn or self.pool).fetchval(
                """
                SELECT true
                FROM project_memberships
                JOIN projects ON projects.id = project_memberships.project_id
                WHERE project_memberships.project_id = $1
                  AND project_memberships.clerk_user_id = $2
                  AND projects.deleted_at IS NULL
                """,
                project_id,
                clerk_user_id,
            )
        )

    async def grant_project_membership(
        self, *, project_id: UUID, clerk_user_id: str
    ) -> ProjectMembership:
        row = await self.pool.fetchrow(
            """
            INSERT INTO project_memberships (project_id, clerk_user_id)
            VALUES ($1, $2)
            ON CONFLICT (project_id, clerk_user_id) DO UPDATE
            SET clerk_user_id = EXCLUDED.clerk_user_id
            RETURNING *
            """,
            project_id,
            clerk_user_id,
        )
        assert row is not None
        return _project_membership(row)

    async def list_project_members(self, project_id: UUID) -> list[ProjectMembership]:
        rows = await self.pool.fetch(
            """
            SELECT *
            FROM project_memberships
            WHERE project_id = $1
            ORDER BY created_at, clerk_user_id
            """,
            project_id,
        )
        return [_project_membership(row) for row in rows]

    async def list_integration_connections(self, project_id: UUID) -> list[IntegrationConnection]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM integration_connections
            WHERE project_id = $1
            ORDER BY provider_key
            """,
            project_id,
        )
        return [_integration_connection(row) for row in rows]

    async def get_integration_connection(
        self, *, project_id: UUID, provider_key: str
    ) -> IntegrationConnection | None:
        row = await (borrowed_connection(self) or self.pool).fetchrow(
            """
            SELECT * FROM integration_connections
            WHERE project_id = $1 AND provider_key = $2
            """,
            project_id,
            provider_key,
        )
        return _integration_connection(row) if row else None

    async def list_integration_connections_by_external_id(
        self, *, provider_key: str, external_account_id: str
    ) -> list[IntegrationConnection]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM integration_connections
            WHERE provider_key = $1 AND external_account_id = $2
            ORDER BY project_id
            """,
            provider_key,
            external_account_id,
        )
        return [_integration_connection(row) for row in rows]

    async def upsert_integration_connection(
        self,
        *,
        project_id: UUID,
        provider_key: str,
        external_account_id: str | None,
        external_account_label: str | None,
        configuration: dict[str, Any],
        credential_ciphertext: bytes | None,
        credential_key_version: str | None,
        connected_by_clerk_user_id: str,
    ) -> IntegrationConnection:
        row = await (borrowed_connection(self) or self.pool).fetchrow(
            """
            INSERT INTO integration_connections (
                id, project_id, provider_key, status,
                external_account_id, external_account_label, configuration,
                credential_ciphertext, credential_key_version,
                connected_by_clerk_user_id, last_checked_at,
                last_error_code
            )
            VALUES (
                $1, $2, $3, 'connected', $4, $5, $6::jsonb,
                $7, $8, $9, now(), NULL
            )
            ON CONFLICT (project_id, provider_key) DO UPDATE
            SET status = 'connected',
                external_account_id = EXCLUDED.external_account_id,
                external_account_label = EXCLUDED.external_account_label,
                configuration = EXCLUDED.configuration,
                credential_ciphertext = EXCLUDED.credential_ciphertext,
                credential_key_version = EXCLUDED.credential_key_version,
                connected_by_clerk_user_id = EXCLUDED.connected_by_clerk_user_id,
                last_checked_at = now(), last_error_code = NULL, updated_at = now()
            RETURNING *
            """,
            uuid4(),
            project_id,
            provider_key,
            external_account_id,
            external_account_label,
            json.dumps(configuration),
            credential_ciphertext,
            credential_key_version,
            connected_by_clerk_user_id,
        )
        assert row is not None
        return _integration_connection(row)

    async def update_integration_configuration(
        self,
        *,
        project_id: UUID,
        provider_key: str,
        configuration: dict[str, Any],
        external_account_label: str | None = None,
    ) -> IntegrationConnection:
        row = await self.pool.fetchrow(
            """
            UPDATE integration_connections
            SET configuration = $3::jsonb,
                external_account_label = COALESCE($4, external_account_label),
                status = 'connected', last_checked_at = now(),
                last_error_code = NULL, updated_at = now()
            WHERE project_id = $1 AND provider_key = $2
            RETURNING *
            """,
            project_id,
            provider_key,
            json.dumps(configuration),
            external_account_label,
        )
        if row is None:
            raise LookupError("integration connection does not exist")
        return _integration_connection(row)

    async def update_integration_credential(
        self,
        *,
        project_id: UUID,
        provider_key: str,
        connection_id: UUID,
        credential_ciphertext: bytes,
        credential_key_version: str,
    ) -> bool:
        """Replace only the stored credential, for token rotation under the refresh lock.

        Configuration, status and selections are untouched, so a run's pinned binding survives.
        Returns False when the connection was replaced or removed meanwhile.
        """
        result = await (borrowed_connection(self) or self.pool).execute(
            """
            UPDATE integration_connections
            SET credential_ciphertext = $4, credential_key_version = $5, updated_at = now()
            WHERE project_id = $1 AND provider_key = $2 AND id = $3
            """,
            project_id,
            provider_key,
            connection_id,
            credential_ciphertext,
            credential_key_version,
        )
        return result == "UPDATE 1"

    async def mark_integration_attention(
        self, *, project_id: UUID, provider_key: str, error_code: str
    ) -> None:
        await (borrowed_connection(self) or self.pool).execute(
            """
            UPDATE integration_connections
            SET status = 'needs_attention', last_error_code = $3,
                last_checked_at = now(), updated_at = now()
            WHERE project_id = $1 AND provider_key = $2
            """,
            project_id,
            provider_key,
            error_code[:120],
        )

    async def delete_integration_connection(self, *, project_id: UUID, provider_key: str) -> bool:
        result = await self.pool.execute(
            """
            DELETE FROM integration_connections
            WHERE project_id = $1 AND provider_key = $2
            """,
            project_id,
            provider_key,
        )
        return result == "DELETE 1"

    async def create_integration_auth_attempt(
        self,
        *,
        token_hash: str,
        project_id: UUID,
        provider_key: str,
        clerk_user_id: str,
        pkce_verifier_ciphertext: bytes | None,
        requested_capabilities: tuple[str, ...] = (),
        expires_at: datetime,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO integration_auth_attempts (
                token_hash, project_id, provider_key, clerk_user_id,
                pkce_verifier_ciphertext, requested_capabilities, expires_at, context
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb)
            """,
            token_hash,
            project_id,
            provider_key,
            clerk_user_id,
            pkce_verifier_ciphertext,
            json.dumps(list(requested_capabilities)),
            expires_at,
            json.dumps(context or {}),
        )

    async def get_google_integration_auth_attempt(
        self, *, token_hash: str, clerk_user_id: str
    ) -> IntegrationAuthAttempt | None:
        row = await self.pool.fetchrow(
            """
            SELECT * FROM integration_auth_attempts
            WHERE token_hash = $1 AND clerk_user_id = $2
              AND provider_key IN ('analytics.gsc', 'workspace.google')
              AND used_at IS NULL AND expires_at > now()
            """,
            token_hash,
            clerk_user_id,
        )
        return _integration_auth_attempt(row) if row else None

    async def consume_google_integration_auth_attempt(
        self, *, token_hash: str, clerk_user_id: str
    ) -> IntegrationAuthAttempt | None:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM integration_auth_attempts
                WHERE token_hash = $1 AND clerk_user_id = $2
                  AND provider_key IN ('analytics.gsc', 'workspace.google')
                  AND used_at IS NULL AND expires_at > now()
                FOR UPDATE
                """,
                token_hash,
                clerk_user_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE integration_auth_attempts
                SET used_at = now()
                WHERE token_hash = $1 AND used_at IS NULL
                """,
                token_hash,
            )
        return _integration_auth_attempt(row)

    async def consume_integration_auth_attempt(
        self,
        *,
        token_hash: str,
        provider_key: str,
        clerk_user_id: str,
    ) -> IntegrationAuthAttempt | None:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM integration_auth_attempts
                WHERE token_hash = $1 AND provider_key = $2
                  AND clerk_user_id = $3 AND used_at IS NULL
                  AND expires_at > now()
                FOR UPDATE
                """,
                token_hash,
                provider_key,
                clerk_user_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE integration_auth_attempts
                SET used_at = now()
                WHERE token_hash = $1 AND used_at IS NULL
                """,
                token_hash,
            )
        return _integration_auth_attempt(row)

    async def get_integration_auth_attempt(
        self,
        *,
        token_hash: str,
        provider_key: str,
        clerk_user_id: str,
        include_used: bool = False,
    ) -> IntegrationAuthAttempt | None:
        # include_used lets a repeated OAuth callback find the attempt it already completed.
        row = await self.pool.fetchrow(
            """
            SELECT * FROM integration_auth_attempts
            WHERE token_hash = $1 AND provider_key = $2
              AND clerk_user_id = $3 AND ($4 OR used_at IS NULL)
              AND expires_at > now()
            """,
            token_hash,
            provider_key,
            clerk_user_id,
            include_used,
        )
        return _integration_auth_attempt(row) if row else None

    @asynccontextmanager
    async def integration_refresh_lock(
        self, *, project_id: UUID, provider_key: str, conn: asyncpg.Connection | None = None
    ) -> AsyncIterator[None]:
        # Code activities already hold a pool slot while calling the integration gateway.
        conn = conn or borrowed_connection(self)
        if conn is None:
            async with self.pool.acquire() as acquired:
                async with self.integration_refresh_lock(
                    project_id=project_id, provider_key=provider_key, conn=acquired
                ):
                    yield
            return
        lock_key = f"integration-refresh:{project_id}:{provider_key}"
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", lock_key)
        try:
            with effect_connection(self, conn):
                yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key)

    @asynccontextmanager
    async def project_file_change_lock(
        self, *, project_id: UUID, request_id: UUID
    ) -> AsyncIterator[None]:
        lock_key = f"project-file-change:{project_id}:{request_id}"
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", lock_key)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key)

    async def get_project_file_change(
        self, *, project_id: UUID, request_id: UUID
    ) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT * FROM project_file_changes
            WHERE project_id = $1 AND request_id = $2
            """,
            project_id,
            request_id,
        )
        if row is None:
            return None
        result = dict(row)
        if result.get("summary") is not None:
            result["summary"] = _json_object(result["summary"], field="project file summary")
        return result

    async def start_project_file_change(
        self,
        *,
        project_id: UUID,
        request_id: UUID,
        actor_clerk_user_id: str,
        client_id: str | None,
        operation: str,
        request_fingerprint: str,
        expected_head_sha: str,
    ) -> None:
        row = await self.pool.fetchrow(
            """
            INSERT INTO project_file_changes (
                project_id, request_id, actor_clerk_user_id, client_id, operation,
                request_fingerprint, expected_head_sha, status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'started')
            ON CONFLICT (project_id, request_id) DO UPDATE
            SET status = 'started', error_code = NULL, updated_at = now()
            WHERE project_file_changes.actor_clerk_user_id = EXCLUDED.actor_clerk_user_id
              AND project_file_changes.client_id IS NOT DISTINCT FROM EXCLUDED.client_id
              AND project_file_changes.operation = EXCLUDED.operation
              AND project_file_changes.request_fingerprint = EXCLUDED.request_fingerprint
              AND project_file_changes.expected_head_sha = EXCLUDED.expected_head_sha
              AND project_file_changes.status <> 'completed'
            RETURNING project_id
            """,
            project_id,
            request_id,
            actor_clerk_user_id,
            client_id,
            operation,
            request_fingerprint,
            expected_head_sha,
        )
        if row is None:
            raise SideEffectConflictError(
                "project file request ID belongs to a different or completed request"
            )

    async def fail_project_file_change(
        self, *, project_id: UUID, request_id: UUID, error_code: str
    ) -> None:
        await self.pool.execute(
            """
            UPDATE project_file_changes
            SET status = 'failed', error_code = $3, updated_at = now()
            WHERE project_id = $1 AND request_id = $2 AND status = 'started'
            """,
            project_id,
            request_id,
            error_code[:100],
        )

    async def complete_project_file_change(
        self,
        *,
        project_id: UUID,
        request_id: UUID,
        commit_sha: str,
        changed_paths: list[str],
        actor_clerk_user_id: str,
        client_id: str | None,
        message: str,
        operation: str,
    ) -> None:
        details = {
            "kind": "your_edits",
            "operation": operation,
            "revision": commit_sha,
            "changed_paths": changed_paths,
            "actor_clerk_user_id": actor_clerk_user_id,
            "client_id": client_id,
            "request_id": str(request_id),
        }
        async with self.pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE project_file_changes
                SET status = 'completed', commit_sha = $3, summary = $4::jsonb,
                    error_code = NULL, updated_at = now()
                WHERE project_id = $1 AND request_id = $2
                  AND status IN ('started', 'completed')
                  AND (commit_sha IS NULL OR commit_sha = $3)
                RETURNING true
                """,
                project_id,
                request_id,
                commit_sha,
                json.dumps({"changed_paths": changed_paths}),
            )
            if updated is None:
                raise SideEffectConflictError("project file request completion conflicts")
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, NULL, 'project_files_changed', $2::jsonb, $3, 'product', $4)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                project_id,
                json.dumps(details),
                message[:1000],
                f"project-file:{project_id}:{request_id}",
            )

    async def record_integration_activity(
        self,
        *,
        project_id: UUID,
        event_type: str,
        summary: str,
        details: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO activity_events (
                project_id, run_id, event_type, details, summary, audience, dedupe_key
            )
            VALUES ($1, NULL, $2, $3::jsonb, $4, 'product', $5)
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
            """,
            project_id,
            event_type,
            json.dumps(details),
            summary[:1000],
            dedupe_key,
        )

    async def record_integration_call(
        self,
        *,
        execution_key: str,
        project_id: UUID,
        run_id: UUID | None = None,
        connection_id: UUID | None,
        provider_key: str,
        capability: str,
        request_fingerprint: str,
        status: str,
        response_summary: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        result = await (borrowed_connection(self) or self.pool).execute(
            """
            INSERT INTO integration_call_receipts (
                execution_key, project_id, run_id, connection_id, provider_key,
                capability, request_fingerprint, status, provider_request_id,
                response_summary, error_code
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
            ON CONFLICT (execution_key) DO UPDATE
            SET status = EXCLUDED.status,
                provider_request_id = EXCLUDED.provider_request_id,
                response_summary = EXCLUDED.response_summary,
                error_code = EXCLUDED.error_code,
                updated_at = now()
            WHERE integration_call_receipts.project_id = EXCLUDED.project_id
              AND integration_call_receipts.run_id IS NOT DISTINCT FROM EXCLUDED.run_id
              AND integration_call_receipts.connection_id
                  IS NOT DISTINCT FROM EXCLUDED.connection_id
              AND integration_call_receipts.provider_key = EXCLUDED.provider_key
              AND integration_call_receipts.capability = EXCLUDED.capability
              AND integration_call_receipts.request_fingerprint = EXCLUDED.request_fingerprint
            """,
            execution_key,
            project_id,
            run_id,
            connection_id,
            provider_key,
            capability,
            request_fingerprint,
            status,
            provider_request_id,
            json.dumps(response_summary) if response_summary is not None else None,
            error_code,
        )
        if result == "INSERT 0 0":
            raise SideEffectConflictError(
                "integration execution key belongs to a different request"
            )

    async def get_integration_call_receipt(
        self, execution_key: str
    ) -> IntegrationCallReceipt | None:
        row = await self.pool.fetchrow(
            """
            SELECT * FROM integration_call_receipts
            WHERE execution_key = $1
            """,
            execution_key,
        )
        return _integration_call_receipt(row) if row else None

    @asynccontextmanager
    async def integration_call_lock(self, execution_key: str) -> AsyncIterator[None]:
        lock_key = f"integration-call:{execution_key}"
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", lock_key)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key)

    async def record_integration_webhook_delivery(
        self,
        *,
        provider_key: str,
        delivery_id: str,
        project_id: UUID | None,
        event_type: str,
        payload_sha256: str,
        status: str,
    ) -> bool:
        result = await self.pool.execute(
            """
            INSERT INTO integration_webhook_deliveries (
                provider_key, delivery_id, project_id, event_type,
                payload_sha256, status
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (provider_key, delivery_id) DO NOTHING
            """,
            provider_key,
            delivery_id,
            project_id,
            event_type,
            payload_sha256,
            status,
        )
        return result == "INSERT 0 1"

    @asynccontextmanager
    async def chat_request_lock(self, *, project_id: UUID, request_id: UUID) -> AsyncIterator[None]:
        """Serialize duplicate delivery in this single switchboard process."""
        lock_name = f"chat:{project_id}:{request_id}"
        lock = self._chat_request_locks.get(lock_name)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_request_locks[lock_name] = lock
        async with lock:
            yield

    async def create_chat_user_message(
        self,
        *,
        project_id: UUID,
        request_id: UUID,
        content: str,
        author_clerk_user_id: str,
    ) -> ChatMessage:
        row = await self.pool.fetchrow(
            """
            INSERT INTO chat_messages (
                id, project_id, request_id, role, source, content, author_clerk_user_id
            )
            VALUES ($1, $2, $3, 'user', 'founder', $4, $5)
            ON CONFLICT (project_id, request_id, role) DO NOTHING
            RETURNING *
            """,
            uuid4(),
            project_id,
            request_id,
            content,
            author_clerk_user_id,
        )
        if row is None:
            row = await self.pool.fetchrow(
                """
                SELECT * FROM chat_messages
                WHERE project_id = $1 AND request_id = $2 AND role = 'user'
                """,
                project_id,
                request_id,
            )
        if row is None:
            raise RuntimeError("chat request conflicted without an existing user message")
        message = _chat_message(row)
        if message.content != content or message.author_clerk_user_id != author_clerk_user_id:
            raise ValueError("chat request ID was already used for a different message")
        return message

    async def get_chat_assistant_message(
        self, *, project_id: UUID, request_id: UUID
    ) -> ChatMessage | None:
        row = await self.pool.fetchrow(
            """
            SELECT * FROM chat_messages
            WHERE project_id = $1 AND request_id = $2 AND role = 'assistant'
            """,
            project_id,
            request_id,
        )
        return _chat_message(row) if row else None

    async def create_chat_assistant_message(
        self,
        *,
        project_id: UUID,
        request_id: UUID,
        content: str,
        response_id: str,
        routed_workflow_key: str | None,
        run_id: UUID | None,
    ) -> ChatMessage:
        row = await self.pool.fetchrow(
            """
            INSERT INTO chat_messages (
                id, project_id, request_id, role, source, content,
                response_id, routed_workflow_key, run_id
            )
            VALUES ($1, $2, $3, 'assistant', 'luna', $4, $5, $6, $7)
            ON CONFLICT (project_id, request_id, role) DO NOTHING
            RETURNING *
            """,
            uuid4(),
            project_id,
            request_id,
            content,
            response_id,
            routed_workflow_key,
            run_id,
        )
        if row is None:
            row = await self.pool.fetchrow(
                """
                SELECT * FROM chat_messages
                WHERE project_id = $1 AND request_id = $2 AND role = 'assistant'
                """,
                project_id,
                request_id,
            )
        if row is None:
            raise RuntimeError("chat response conflicted without an existing assistant message")
        message = _chat_message(row)
        expected = (content, response_id, routed_workflow_key, run_id)
        actual = (
            message.content,
            message.response_id,
            message.routed_workflow_key,
            message.run_id,
        )
        if actual != expected:
            raise RuntimeError("chat request already has a different assistant response")
        return message

    async def list_chat_messages(self, *, project_id: UUID, limit: int = 100) -> list[ChatMessage]:
        rows = await self.pool.fetch(
            """
            SELECT *
            FROM (
                SELECT * FROM chat_messages
                WHERE project_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2
            ) AS recent
            ORDER BY created_at, id
            """,
            project_id,
            limit,
        )
        return [_chat_message(row) for row in rows]

    async def list_chat_context_messages(
        self,
        *,
        project_id: UUID,
        exclude_request_id: UUID,
        turn_limit: int = 6,
    ) -> list[ChatMessage]:
        rows = await self.pool.fetch(
            """
            WITH recent_requests AS (
                SELECT request_id, max(created_at) AS completed_at
                FROM chat_messages
                WHERE project_id = $1
                  AND request_id <> $2
                GROUP BY request_id
                HAVING bool_or(role = 'assistant')
                ORDER BY completed_at DESC, request_id DESC
                LIMIT $3
            )
            SELECT messages.*
            FROM chat_messages AS messages
            JOIN recent_requests USING (request_id)
            WHERE messages.project_id = $1
            ORDER BY messages.created_at, messages.id
            """,
            project_id,
            exclude_request_id,
            turn_limit,
        )
        return [_chat_message(row) for row in rows]

    async def create_project_invitation(
        self,
        *,
        project_id: UUID,
        email: str,
        token_hash: str,
        created_by_clerk_user_id: str,
        expires_at: datetime,
    ) -> ProjectInvitation:
        invitation_id = uuid4()
        row = await self.pool.fetchrow(
            """
            INSERT INTO project_invitations (
                id, project_id, email, token_hash, created_by_clerk_user_id, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *, (SELECT name FROM projects WHERE id = $2) AS project_name
            """,
            invitation_id,
            project_id,
            email,
            token_hash,
            created_by_clerk_user_id,
            expires_at,
        )
        assert row is not None
        return _project_invitation(row)

    async def get_project_invitation(self, token_hash: str) -> ProjectInvitation | None:
        row = await self.pool.fetchrow(
            """
            SELECT invitations.*, projects.name AS project_name
            FROM project_invitations AS invitations
            JOIN projects ON projects.id = invitations.project_id
            WHERE invitations.token_hash = $1
            """,
            token_hash,
        )
        return _project_invitation(row) if row else None

    async def accept_project_invitation(
        self,
        *,
        token_hash: str,
        clerk_user_id: str,
        expected_email: str,
    ) -> ProjectInvitation:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT invitations.*, projects.name AS project_name
                FROM project_invitations AS invitations
                JOIN projects ON projects.id = invitations.project_id
                WHERE invitations.token_hash = $1
                FOR UPDATE OF invitations
                """,
                token_hash,
            )
            if row is None or row["revoked_at"] is not None:
                raise LookupError("invitation not found")
            if row["email"] != expected_email:
                raise RuntimeError("invitation identity changed")
            accepted_by = row["accepted_by_clerk_user_id"]
            if accepted_by is not None and accepted_by != clerk_user_id:
                raise RuntimeError("invitation has already been accepted")
            # Its member may replay an accepted invitation after expiry; it grants nothing new.
            if row["expires_at"] <= datetime.now(UTC) and (
                accepted_by is None
                or not await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM project_memberships "
                    "WHERE project_id = $1 AND clerk_user_id = $2)",
                    row["project_id"],
                    clerk_user_id,
                )
            ):
                raise RuntimeError("invitation has expired")
            await conn.execute(
                """
                INSERT INTO project_memberships (project_id, clerk_user_id)
                VALUES ($1, $2)
                ON CONFLICT (project_id, clerk_user_id) DO NOTHING
                """,
                row["project_id"],
                clerk_user_id,
            )
            if accepted_by is None:
                row = await conn.fetchrow(
                    """
                    UPDATE project_invitations
                    SET accepted_by_clerk_user_id = $2,
                        accepted_at = now()
                    WHERE id = $1
                    RETURNING *, $3::text AS project_name
                    """,
                    row["id"],
                    clerk_user_id,
                    row["project_name"],
                )
                assert row is not None
        return _project_invitation(row)

    async def upsert_workflow_system(
        self,
        *,
        system_id: str,
        name: str,
        display_order: int,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO workflow_systems (id, name, display_order)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                display_order = EXCLUDED.display_order,
                updated_at = now()
            WHERE workflow_systems.name IS DISTINCT FROM EXCLUDED.name
               OR workflow_systems.display_order IS DISTINCT FROM EXCLUDED.display_order
            """,
            system_id,
            name,
            display_order,
        )

    async def upsert_registry_workflow(
        self,
        *,
        workflow_id: UUID,
        key: str,
        title: str,
        description: str,
        executor: str,
        definition_repo_id: str,
        definition_path: str,
        current_commit_sha: str,
        version_label: str,
        definition: dict[str, Any],
        replaces_executor: str | None = None,
    ) -> Workflow:
        row = await self.pool.fetchrow(
            """
            INSERT INTO workflows (
                id, project_id, key, title, description, executor,
                definition_repo_id, definition_path, current_commit_sha,
                version_label, definition, status
            )
            VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, 'active')
            ON CONFLICT (id) DO UPDATE
            SET key = EXCLUDED.key,
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                executor = EXCLUDED.executor,
                definition_repo_id = EXCLUDED.definition_repo_id,
                definition_path = EXCLUDED.definition_path,
                current_commit_sha = EXCLUDED.current_commit_sha,
                version_label = EXCLUDED.version_label,
                definition = EXCLUDED.definition,
                updated_at = now()
            WHERE workflows.project_id IS NULL AND workflows.key = EXCLUDED.key
              AND (workflows.current_commit_sha IS NULL OR (
                  (workflows.executor = EXCLUDED.executor OR workflows.executor = $11)
                  AND workflows.definition_repo_id = EXCLUDED.definition_repo_id
                  AND workflows.definition_path = EXCLUDED.definition_path
              ))
            RETURNING *
            """,
            workflow_id,
            key,
            title,
            description,
            executor,
            definition_repo_id,
            definition_path,
            current_commit_sha,
            version_label,
            json.dumps(definition),
            replaces_executor,
        )
        if row is None:
            raise RuntimeError("built-in workflow ID belongs to a different workflow or source")
        return _workflow(row)

    async def get_workflow(
        self, workflow_id: UUID, *, conn: asyncpg.Connection | None = None
    ) -> Workflow | None:
        row = await (conn or self.pool).fetchrow(
            """
            SELECT workflow.*,
                   workflow.definition ->> 'system' AS system_id,
                   system.name AS system_name,
                   system.display_order AS system_order
            FROM workflows AS workflow
            LEFT JOIN workflow_systems AS system
              ON system.id = workflow.definition ->> 'system'
            WHERE workflow.id = $1
            """,
            workflow_id,
        )
        return _workflow(row) if row else None

    async def get_registry_workflow(self, key: str) -> Workflow | None:
        row = await self.pool.fetchrow(
            """
            SELECT workflow.*,
                   workflow.definition ->> 'system' AS system_id,
                   system.name AS system_name,
                   system.display_order AS system_order
            FROM workflows AS workflow
            LEFT JOIN workflow_systems AS system
              ON system.id = workflow.definition ->> 'system'
            WHERE workflow.project_id IS NULL AND workflow.key = $1
            """,
            key,
        )
        return _workflow(row) if row else None

    async def list_workflows(self, *, project_id: UUID | None = None) -> list[Workflow]:
        if project_id is None:
            rows = await self.pool.fetch(
                """
                SELECT workflow.*,
                       workflow.definition ->> 'system' AS system_id,
                       system.name AS system_name,
                       system.display_order AS system_order
                FROM workflows AS workflow
                LEFT JOIN workflow_systems AS system
                  ON system.id = workflow.definition ->> 'system'
                WHERE workflow.project_id IS NULL AND workflow.status <> 'archived'
                ORDER BY (system.id IS NULL), system.display_order, system.name, workflow.key
                """
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT workflow.*,
                       workflow.definition ->> 'system' AS system_id,
                       system.name AS system_name,
                       system.display_order AS system_order
                FROM workflows AS workflow
                LEFT JOIN workflow_systems AS system
                  ON system.id = workflow.definition ->> 'system'
                WHERE (workflow.project_id IS NULL OR workflow.project_id = $1)
                  AND workflow.status <> 'archived'
                ORDER BY workflow.project_id NULLS FIRST,
                         (system.id IS NULL), system.display_order, system.name, workflow.key
                """,
                project_id,
            )
        workflows = [_workflow(row) for row in rows]
        for workflow in workflows:
            if (
                workflow.system_id
                and workflow.system_name is None
                and workflow.system_id not in _warned_unknown_workflow_system_ids
            ):
                logger.warning(
                    "workflow %s references unknown registry system %s; treating it as unassigned",
                    workflow.key,
                    workflow.system_id,
                )
                _warned_unknown_workflow_system_ids.add(workflow.system_id)
        return workflows

    async def get_workflow_template_state(
        self, *, project_id: UUID, clerk_user_id: str
    ) -> dict[UUID, dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT workflow.id,
                   (saved.workflow_id IS NOT NULL) AS saved,
                   count(configured.id) FILTER (
                       WHERE configured.status <> 'archived'
                   )::integer AS project_workflow_count
            FROM workflows AS workflow
            LEFT JOIN saved_workflow_templates AS saved
              ON saved.workflow_id = workflow.id AND saved.clerk_user_id = $2
            LEFT JOIN project_workflows AS configured
              ON configured.workflow_id = workflow.id AND configured.project_id = $1
            WHERE workflow.status <> 'archived'
              AND (workflow.project_id IS NULL OR workflow.project_id = $1)
            GROUP BY workflow.id, saved.workflow_id
            """,
            project_id,
            clerk_user_id,
        )
        return {
            row["id"]: {
                "saved": bool(row["saved"]),
                "project_workflow_count": int(row["project_workflow_count"] or 0),
            }
            for row in rows
        }

    async def save_workflow_template(self, *, workflow_id: UUID, clerk_user_id: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO saved_workflow_templates (clerk_user_id, workflow_id)
            VALUES ($1, $2)
            ON CONFLICT (clerk_user_id, workflow_id) DO NOTHING
            """,
            clerk_user_id,
            workflow_id,
        )

    async def remove_saved_workflow_template(
        self, *, workflow_id: UUID, clerk_user_id: str
    ) -> None:
        await self.pool.execute(
            """
            DELETE FROM saved_workflow_templates
            WHERE clerk_user_id = $1 AND workflow_id = $2
            """,
            clerk_user_id,
            workflow_id,
        )

    async def list_project_workflows(self, *, project_id: UUID) -> list[ProjectWorkflow]:
        rows = await self.pool.fetch(
            """
            SELECT configured.*, workflows.key AS workflow_key,
                   workflows.title AS workflow_title,
                   workflows.description AS workflow_description,
                   workflows.version_label,
                   latest.id AS last_run_id,
                   latest.status AS last_run_status,
                   latest.artifact_path AS last_artifact_path,
                   latest.artifact_title AS last_artifact_title,
                   latest.result_summary AS last_result_summary,
                   latest.started_at AS last_started_at,
                   latest.finished_at AS last_finished_at,
                   stats.run_count, stats.done_count, stats.failed_count,
                   stats.typical_duration_seconds,
                   (SELECT jsonb_build_object('id', revision.id,
                        'preview_revision', revision.preview_revision,
                        'status', revision.status, 'held_batches', jsonb_array_length(batch_ids))
                    FROM content_plan_revisions AS revision
                    WHERE revision.project_workflow_id = configured.id
                      AND revision.status IN ('pending', 'applying')) AS content_revision
            FROM project_workflows AS configured
            JOIN workflows ON workflows.id = configured.workflow_id
            LEFT JOIN LATERAL (
                SELECT id, status, artifact_path, artifact_title, result_summary,
                       started_at, finished_at
                FROM workflow_runs
                WHERE project_workflow_id = configured.id
                  AND status IN ('succeeded', 'failed', 'stopped')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) AS latest ON true
            LEFT JOIN LATERAL (
                SELECT count(*)::integer AS run_count,
                       count(*) FILTER (WHERE status = 'succeeded')::integer AS done_count,
                       count(*) FILTER (WHERE status = 'failed')::integer AS failed_count,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY extract(epoch FROM (finished_at - started_at))
                       ) FILTER (
                           WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
                       ) AS typical_duration_seconds
                FROM workflow_runs
                WHERE project_workflow_id = configured.id
            ) AS stats ON true
            WHERE configured.project_id = $1 AND configured.status <> 'archived'
            ORDER BY configured.created_at DESC, configured.id DESC
            """,
            project_id,
        )
        return [_project_workflow(row) for row in rows]

    async def get_project_workflow(
        self, project_workflow_id: UUID, *, include_archived: bool = False
    ) -> ProjectWorkflow | None:
        row = await self.pool.fetchrow(
            """
            SELECT configured.*, workflows.key AS workflow_key,
                   workflows.title AS workflow_title,
                   workflows.description AS workflow_description,
                   workflows.version_label,
                   latest.id AS last_run_id,
                   latest.status AS last_run_status,
                   latest.artifact_path AS last_artifact_path,
                   latest.artifact_title AS last_artifact_title,
                   latest.result_summary AS last_result_summary,
                   latest.started_at AS last_started_at,
                   latest.finished_at AS last_finished_at,
                   stats.run_count, stats.done_count, stats.failed_count,
                   stats.typical_duration_seconds,
                   (SELECT jsonb_build_object('id', revision.id,
                        'preview_revision', revision.preview_revision,
                        'status', revision.status, 'held_batches', jsonb_array_length(batch_ids))
                    FROM content_plan_revisions AS revision
                    WHERE revision.project_workflow_id = configured.id
                      AND revision.status IN ('pending', 'applying')) AS content_revision
            FROM project_workflows AS configured
            JOIN workflows ON workflows.id = configured.workflow_id
            LEFT JOIN LATERAL (
                SELECT id, status, artifact_path, artifact_title, result_summary,
                       started_at, finished_at
                FROM workflow_runs
                WHERE project_workflow_id = configured.id
                  AND status IN ('succeeded', 'failed', 'stopped')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) AS latest ON true
            LEFT JOIN LATERAL (
                SELECT count(*)::integer AS run_count,
                       count(*) FILTER (WHERE status = 'succeeded')::integer AS done_count,
                       count(*) FILTER (WHERE status = 'failed')::integer AS failed_count,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY extract(epoch FROM (finished_at - started_at))
                       ) FILTER (
                           WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
                       ) AS typical_duration_seconds
                FROM workflow_runs
                WHERE project_workflow_id = configured.id
            ) AS stats ON true
            WHERE configured.id = $1 AND (configured.status <> 'archived' OR $2)
            """,
            project_workflow_id,
            include_archived,
        )
        return _project_workflow(row) if row else None

    async def get_project_system_summary(self, *, project_id: UUID) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            f"""
            SELECT projects.timezone,
                   (SELECT count(*) FROM project_workflows
                    WHERE project_id = projects.id AND status <> 'archived')::integer
                       AS workflow_count,
                   (SELECT count(*) FROM workflow_runs
                    WHERE project_id = projects.id AND executor <> 'project.task'
                      AND status IN ('pending', 'running'))::integer
                       AS running_count,
                   (SELECT count(*) FROM workflow_runs
                    WHERE project_id = projects.id
                      AND executor <> 'project.task'
                      AND created_at >= date_trunc('month', now() AT TIME ZONE projects.timezone)
                          AT TIME ZONE projects.timezone)::integer
                       AS runs_this_month,
                   (SELECT count(*) FROM workflow_runs AS run
                    WHERE run.project_id = projects.id AND (
                        (run.status = 'needs_input'
                         AND (run.review_required OR run.executor = 'project.task'))
                        OR ({_PENDING_OUTPUT_CONFLICT_SQL})
                    ))::integer
                       AS waiting_count,
                   (SELECT count(*) FROM activity_events
                    WHERE project_id = projects.id AND audience = 'product'
                      AND created_at >= now() - interval '30 days')::integer
                       AS activity_count_30_days,
                   (SELECT min(next_run_at) FROM project_workflows
                    WHERE project_id = projects.id AND status = 'active'
                      AND schedule IS NOT NULL) AS next_run_at,
                   (SELECT max(finished_at) FROM workflow_runs
                    WHERE project_id = projects.id AND executor = 'growth.onboarding'
                      AND status = 'succeeded') AS set_up_at,
                   usage.used_at AS last_mcp_used_at,
                   usage.tool_name AS last_mcp_tool_name
            FROM projects
            LEFT JOIN project_mcp_usage AS usage ON usage.project_id = projects.id
            WHERE projects.id = $1
            """,  # noqa: S608 — static SQL predicate, no caller text
            project_id,
        )
        if row is None:
            raise LookupError(f"project {project_id} does not exist")
        return dict(row)

    async def record_mcp_usage(
        self,
        *,
        project_id: UUID,
        clerk_user_id: str,
        oauth_client_id: str | None,
        tool_name: str,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO project_mcp_usage (
                project_id, clerk_user_id, oauth_client_id, tool_name, used_at
            )
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (project_id) DO UPDATE
            SET clerk_user_id = EXCLUDED.clerk_user_id,
                oauth_client_id = EXCLUDED.oauth_client_id,
                tool_name = EXCLUDED.tool_name,
                used_at = EXCLUDED.used_at
            """,
            project_id,
            clerk_user_id,
            oauth_client_id,
            tool_name[:100],
        )

    async def create_project_workflow(
        self,
        *,
        project_id: UUID,
        workflow_id: UUID,
        definition_commit_sha: str,
        name: str,
        inputs: dict[str, Any],
        input_schema: dict[str, Any],
        schedule: dict[str, Any] | None,
        request_id: UUID,
        created_by_clerk_user_id: str,
        pinned_definition: dict[str, Any] | None = None,
    ) -> ProjectWorkflow:
        project_workflow_id = uuid4()
        async with self.pool.acquire() as conn, conn.transaction():
            workflow = await conn.fetchrow(
                """
                SELECT * FROM workflows
                WHERE id = $1 AND status = 'active'
                  AND (project_id IS NULL OR project_id = $2)
                """,
                workflow_id,
                project_id,
            )
            if workflow is None:
                raise LookupError("workflow is not available to this project")
            if pinned_definition is not None and (
                not re.fullmatch(r"[0-9a-f]{40}", definition_commit_sha)
                or pinned_definition.get("key") != workflow["key"]
                or pinned_definition.get("executor") != workflow["executor"]
                or pinned_definition.get("input_schema") != input_schema
            ):
                raise RuntimeError("The pinned configuration definition is invalid")
            if (
                pinned_definition is None
                and workflow["current_commit_sha"] != definition_commit_sha
            ):
                raise RuntimeError("workflow definition changed while it was being configured")
            if workflow["key"] == "content.plan":
                from tin_lite.content_plan import bound_schedule

                schedule = bound_schedule(inputs, schedule)
            await conn.execute(
                """
                INSERT INTO project_workflows (
                    id, project_id, workflow_id, definition_commit_sha, name, inputs,
                    input_schema, schedule, request_id, created_by_clerk_user_id
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10)
                ON CONFLICT (project_id, request_id) DO NOTHING
                """,
                project_workflow_id,
                project_id,
                workflow_id,
                definition_commit_sha,
                name,
                json.dumps(inputs),
                json.dumps(input_schema),
                json.dumps(schedule) if schedule is not None else None,
                request_id,
                created_by_clerk_user_id,
            )
            row = await conn.fetchrow(
                """
                SELECT configured.*, workflows.key AS workflow_key,
                       workflows.title AS workflow_title,
                       workflows.description AS workflow_description,
                       workflows.version_label,
                       NULL::uuid AS last_run_id,
                       NULL::text AS last_run_status,
                       NULL::text AS last_artifact_path
                FROM project_workflows AS configured
                JOIN workflows ON workflows.id = configured.workflow_id
                WHERE configured.project_id = $1 AND configured.request_id = $2
                """,
                project_id,
                request_id,
            )
            assert row is not None
            if (
                row["workflow_id"] != workflow_id
                or row["definition_commit_sha"] != definition_commit_sha
                or row["name"] != name
                or _json_object(row["inputs"], field="project workflow inputs") != inputs
                or _json_object(row["input_schema"], field="project workflow schema")
                != input_schema
                or (
                    _json_object(row["schedule"], field="project workflow schedule")
                    if row["schedule"] is not None
                    else None
                )
                != schedule
            ):
                raise RuntimeError("project workflow request ID belongs to different content")
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, NULL, 'workflow_settings_updated', $2::jsonb, $3, 'product', $4)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                project_id,
                json.dumps(
                    {
                        "kind": "your_edits",
                        "actor_clerk_user_id": created_by_clerk_user_id,
                        "workflow_key": row["workflow_key"],
                        "workflow_title": row["workflow_title"],
                        "project_workflow_id": str(row["id"]),
                        "settings_revision": row["settings_revision"],
                        "changed_fields": ["workflow created"],
                    }
                ),
                f"{name} saved to Your workflows.",
                f"project-workflow:{row['id']}:settings:{row['settings_revision']}",
            )
        return _project_workflow(row)

    async def update_project_workflow(
        self,
        *,
        project_workflow_id: UUID,
        project_id: UUID,
        name: str,
        inputs: dict[str, Any],
        schedule: dict[str, Any] | None,
        expected_settings_revision: int,
        clerk_user_id: str,
        changed_fields: list[str],
        workflow_key: str,
        workflow_title: str,
    ) -> ProjectWorkflow:
        if workflow_key == "content.plan":
            from tin_lite.content_plan import bound_schedule

            schedule = bound_schedule(inputs, schedule)
        async with self.pool.acquire() as conn, conn.transaction():
            if workflow_key == "content.plan":
                current = await conn.fetchrow(
                    "SELECT inputs FROM project_workflows WHERE id = $1 AND project_id = $2 "
                    "FOR UPDATE",
                    project_workflow_id,
                    project_id,
                )
                started = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workflow_runs WHERE project_workflow_id = $1)",
                    project_workflow_id,
                )
                original_inputs = (
                    _json_object(current["inputs"], field="project workflow inputs")
                    if current
                    else {}
                )
                if started and any(
                    original_inputs.get(key) != inputs.get(key)
                    for key in ("audit_run_id", "keyword_run_id", "start_date", "duration")
                ):
                    raise ValueError(
                        "Research and dates are pinned from the first run. "
                        "Create a new program to change them."
                    )
            row = await conn.fetchrow(
                """
                UPDATE project_workflows
                SET name = $3, inputs = $4::jsonb, schedule = $5::jsonb,
                    status = 'provisioning', last_error = NULL,
                    settings_revision = settings_revision + 1, updated_at = now()
                WHERE id = $1 AND project_id = $2 AND status <> 'archived'
                  AND settings_revision = $6
                RETURNING id, settings_revision
                """,
                project_workflow_id,
                project_id,
                name,
                json.dumps(inputs),
                json.dumps(schedule) if schedule is not None else None,
                expected_settings_revision,
            )
            if row is None:
                current_revision = await conn.fetchval(
                    """
                    SELECT settings_revision
                    FROM project_workflows
                    WHERE id = $1 AND project_id = $2 AND status <> 'archived'
                    """,
                    project_workflow_id,
                    project_id,
                )
                if current_revision is None:
                    raise LookupError("project workflow not found")
                raise StaleSettingsRevisionError(
                    "workflow settings changed after this editor was opened"
                )
            fields = ", ".join(changed_fields) if changed_fields else "settings"
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, NULL, 'workflow_settings_updated', $2::jsonb, $3, 'product', $4)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                project_id,
                json.dumps(
                    {
                        "kind": "your_edits",
                        "actor_clerk_user_id": clerk_user_id,
                        "workflow_key": workflow_key,
                        "workflow_title": workflow_title,
                        "project_workflow_id": str(project_workflow_id),
                        "settings_revision": row["settings_revision"],
                        "changed_fields": changed_fields,
                    }
                ),
                f"{name} updated: {fields}.",
                f"project-workflow:{project_workflow_id}:settings:{row['settings_revision']}",
            )
        configured = await self.get_project_workflow(project_workflow_id)
        assert configured is not None
        return configured

    async def project_workflow_synced(
        self,
        *,
        project_workflow_id: UUID,
        temporal_schedule_id: str | None,
        next_run_at: datetime | None,
        paused: bool = False,
    ) -> ProjectWorkflow:
        row = await self.pool.fetchrow(
            """
            UPDATE project_workflows
            SET status = $2, temporal_schedule_id = $3, next_run_at = $4,
                last_error = NULL, updated_at = now()
            WHERE id = $1 AND status <> 'archived'
            RETURNING id
            """,
            project_workflow_id,
            "paused" if paused else "active",
            temporal_schedule_id,
            next_run_at,
        )
        if row is None:
            raise LookupError("project workflow not found")
        configured = await self.get_project_workflow(project_workflow_id)
        assert configured is not None
        return configured

    async def project_workflow_failed(
        self, *, project_workflow_id: UUID, error_message: str
    ) -> None:
        await self.pool.execute(
            """
            UPDATE project_workflows
            SET status = 'failed', last_error = $2, updated_at = now()
            WHERE id = $1 AND status <> 'archived'
            """,
            project_workflow_id,
            error_message[:1000],
        )

    async def set_project_workflow_paused(
        self, *, project_workflow_id: UUID, project_id: UUID, paused: bool
    ) -> ProjectWorkflow:
        row = await self.pool.fetchrow(
            """
            UPDATE project_workflows
            SET status = $3, next_run_at = CASE WHEN $3 = 'paused' THEN NULL ELSE next_run_at END,
                last_error = NULL, updated_at = now()
            WHERE id = $1 AND project_id = $2 AND schedule IS NOT NULL
              AND status IN ('active', 'paused')
            RETURNING id
            """,
            project_workflow_id,
            project_id,
            "paused" if paused else "active",
        )
        if row is None:
            raise RuntimeError("scheduled workflow cannot change pause state")
        configured = await self.get_project_workflow(project_workflow_id)
        assert configured is not None
        return configured

    async def advance_project_workflow_schedule(
        self,
        *,
        project_workflow_id: UUID,
        next_run_at: datetime | None,
        expected_settings_revision: int | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE project_workflows
            SET next_run_at = $2, updated_at = now()
            WHERE id = $1 AND status = 'active' AND schedule IS NOT NULL
              AND ($3::integer IS NULL OR settings_revision = $3)
            """,
            project_workflow_id,
            next_run_at,
            expected_settings_revision,
        )

    async def skip_project_workflow_once(
        self,
        *,
        project_workflow_id: UUID,
        project_id: UUID,
        skipped_for: datetime,
        next_run_at: datetime,
        clerk_user_id: str,
        workflow_key: str,
        workflow_title: str,
    ) -> ProjectWorkflow:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE project_workflows
                SET skip_scheduled_for = $3, next_run_at = $4, updated_at = now()
                WHERE id = $1 AND project_id = $2 AND status = 'active'
                  AND schedule IS NOT NULL AND next_run_at = $3
                RETURNING id
                """,
                project_workflow_id,
                project_id,
                skipped_for,
                next_run_at,
            )
            if row is None:
                raise RuntimeError("the next run changed before it could be skipped")
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, 'project_workflow_skipped', $2::jsonb, $3, 'product', $4)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                project_id,
                json.dumps(
                    {
                        "kind": "your_edits",
                        "actor_clerk_user_id": clerk_user_id,
                        "workflow_key": workflow_key,
                        "workflow_title": workflow_title,
                        "project_workflow_id": str(project_workflow_id),
                        "skipped_for": skipped_for.isoformat(),
                    }
                ),
                f"{workflow_title} will skip its next run.",
                f"project-workflow:{project_workflow_id}:skip:{skipped_for.isoformat()}",
            )
        configured = await self.get_project_workflow(project_workflow_id)
        assert configured is not None
        return configured

    async def consume_project_workflow_skip(
        self, *, project_workflow_id: UUID, scheduled_for: datetime
    ) -> bool:
        return bool(
            await self.pool.fetchval(
                """
                UPDATE project_workflows
                SET skip_scheduled_for = NULL, updated_at = now()
                WHERE id = $1 AND skip_scheduled_for IS NOT NULL
                  AND $2 >= skip_scheduled_for
                  AND $2 < skip_scheduled_for + interval '24 hours'
                RETURNING true
                """,
                project_workflow_id,
                scheduled_for,
            )
        )

    async def archive_project_workflow(
        self,
        *,
        project_workflow_id: UUID,
        project_id: UUID,
        expected_settings_revision: int,
        clerk_user_id: str,
        workflow_key: str,
        workflow_title: str,
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            archived = await conn.fetchval(
                """
                UPDATE project_workflows
                SET status = 'archived', next_run_at = NULL,
                    skip_scheduled_for = NULL, updated_at = now()
                WHERE id = $1 AND project_id = $2 AND status <> 'archived'
                  AND settings_revision = $3
                RETURNING true
                """,
                project_workflow_id,
                project_id,
                expected_settings_revision,
            )
            if not archived:
                raise StaleSettingsRevisionError(
                    "workflow settings changed after this editor was opened"
                )
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, 'project_workflow_removed', $2::jsonb, $3, 'product', $4)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                project_id,
                json.dumps(
                    {
                        "kind": "your_edits",
                        "actor_clerk_user_id": clerk_user_id,
                        "workflow_key": workflow_key,
                        "workflow_title": workflow_title,
                        "project_workflow_id": str(project_workflow_id),
                    }
                ),
                f"{workflow_title} was removed from My system.",
                f"project-workflow:{project_workflow_id}:removed",
            )

    async def get_run_by_start_key(
        self, *, project_id: UUID, start_idempotency_key: str
    ) -> WorkflowRun | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM workflow_runs WHERE project_id = $1 AND start_idempotency_key = $2",
            project_id,
            start_idempotency_key,
        )
        return _run(row) if row else None

    async def create_run(
        self,
        *,
        project_id: UUID,
        workflow_id: UUID,
        started_by_clerk_user_id: str | None = None,
        start_idempotency_key: str | None = None,
        input_payload: dict[str, Any] | None = None,
        project_workflow_id: UUID | None = None,
        definition_commit_sha: str | None = None,
        trigger_source: str = "manual",
        trigger_client: str | None = None,
        started_by_oauth_client_id: str | None = None,
        retry_of_run_id: UUID | None = None,
        scheduled_for: datetime | None = None,
        schedule_settings_revision: int | None = None,
        pinned_definition: dict[str, Any] | None = None,
        billing_quote_id: UUID | None = None,
        billing_parent_run_id: UUID | None = None,
        prerequisite_evidence: dict[str, Any] | None = None,
        draft_selection: dict[str, Any] | None = None,
        content_delivery_source: dict[str, Any] | None = None,
        review_transition: dict[str, Any] | None = None,
        payment_card=None,
    ) -> tuple[WorkflowRun, bool]:
        input_payload = input_payload or {}
        run_id = uuid4()
        async with self.pool.acquire() as conn, conn.transaction():
            if draft_selection is not None:
                # Same ordering as plan edits/amendments: program lock before project row.
                # Selection, duplicate check, run, budget and receipt commit atomically.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"content-program:{UUID(input_payload['program_id'])}",
                )
            exists = await conn.fetchval(
                "SELECT true FROM projects WHERE id = $1 FOR UPDATE", project_id
            )
            if not exists:
                raise LookupError(f"project {project_id} does not exist")
            if review_transition is not None:
                from tin_lite.workflow_review_store import guard_transition

                await guard_transition(conn, project_id=project_id, command=review_transition)
            if start_idempotency_key is not None:
                existing = await conn.fetchrow(
                    """
                    SELECT * FROM workflow_runs
                    WHERE project_id = $1 AND start_idempotency_key = $2
                    """,
                    project_id,
                    start_idempotency_key,
                )
                if existing is not None:
                    if (
                        pinned_definition is not None
                        and existing["definition_commit_sha"] != definition_commit_sha
                    ):
                        raise RuntimeError(
                            "workflow start idempotency key belongs to another revision"
                        )
                    if existing["workflow_id"] != workflow_id:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different workflow"
                        )
                    if existing["started_by_clerk_user_id"] != started_by_clerk_user_id:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different actor"
                        )
                    if _json_object(existing.get("input", {}), field="run input") != input_payload:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to different input"
                        )
                    if existing.get("project_workflow_id") != project_workflow_id:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different configuration"
                        )
                    if existing.get("trigger_source", "manual") != trigger_source:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different trigger"
                        )
                    if existing.get("trigger_client") != trigger_client:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different client"
                        )
                    if existing.get("started_by_oauth_client_id") != started_by_oauth_client_id:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different OAuth client"
                        )
                    if existing.get("retry_of_run_id") != retry_of_run_id:
                        raise RuntimeError(
                            "workflow start idempotency key belongs to a different retry"
                        )
                    if payment_card is not None:
                        await payment_card.check_retry(conn, run=existing)
                    return _run(existing), False
            workflow = await conn.fetchrow(
                """
                SELECT * FROM workflows
                WHERE id = $1
                  AND status = 'active'
                  AND (project_id IS NULL OR project_id = $2)
                """,
                workflow_id,
                project_id,
            )
            if workflow is None:
                raise LookupError(f"workflow {workflow_id} is not available to this project")
            if workflow["current_commit_sha"] is None:
                raise RuntimeError("workflow definition has not been published")
            if pinned_definition is not None and (
                not isinstance(definition_commit_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", definition_commit_sha)
                or pinned_definition.get("key") != workflow["key"]
                or pinned_definition.get("executor") != workflow["executor"]
            ):
                raise RuntimeError(
                    "The pinned child definition does not match its registry identity"
                )
            if review_transition is not None:
                origin = await conn.fetchrow(
                    "SELECT project_workflow_id, input FROM workflow_runs "
                    "WHERE id=$1 AND project_id=$2 AND workflow_id=$3",
                    review_transition["source_run_id"],
                    project_id,
                    workflow_id,
                )
                if (
                    origin is None
                    or origin["project_workflow_id"] != project_workflow_id
                    or _json_object(origin["input"], field="review inputs") != input_payload
                ):
                    raise RuntimeError(
                        "A revision must preserve its source configuration and inputs"
                    )
            elif project_workflow_id is not None:
                configured = await conn.fetchrow(
                    """
                    SELECT * FROM project_workflows
                    WHERE id = $1 AND project_id = $2 AND workflow_id = $3
                      AND status IN ('active', 'paused')
                    FOR UPDATE
                    """,
                    project_workflow_id,
                    project_id,
                    workflow_id,
                )
                if trigger_source == "schedule" and workflow["executor"] == "workflow.code":
                    from tin_lite.schedules import ScheduledWorkflowSkip

                    if (
                        configured is None
                        or configured["status"] != "active"
                        or configured["schedule"] is None
                        or configured["settings_revision"] != schedule_settings_revision
                    ):
                        raise ScheduledWorkflowSkip("The saved schedule changed before dispatch.")
                    if started_by_clerk_user_id != configured[
                        "created_by_clerk_user_id"
                    ] or not await self.has_project_access(
                        project_id=project_id,
                        clerk_user_id=started_by_clerk_user_id,
                        conn=conn,
                    ):
                        raise ValueError("The schedule's author no longer has project access.")
                if trigger_source == "schedule":
                    from tin_lite.schedules import ScheduledWorkflowSkip

                    # A recovered child can outlive its original Temporal dispatcher.
                    # Serialize admission against the durable run projection as well.
                    if await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM workflow_runs WHERE project_workflow_id=$1 "
                        "AND status IN ('pending','running','needs_input','paused'))",
                        project_workflow_id,
                    ):
                        raise ScheduledWorkflowSkip(
                            "An earlier run of this configuration is active."
                        )
                if configured is None:
                    raise LookupError("project workflow is not available")
                if definition_commit_sha != configured["definition_commit_sha"]:
                    raise RuntimeError("project workflow definition revision does not match")
                configured_inputs = _json_object(
                    configured["inputs"], field="project workflow inputs"
                )
                if workflow["key"] == "content.plan" and input_payload.get("amendment_id"):
                    amendment = await conn.fetchval(
                        """SELECT id FROM content_plan_revisions
                        WHERE id = $1 AND project_workflow_id = $2 AND project_id = $3
                          AND status = 'pending'""",
                        UUID(input_payload["amendment_id"]),
                        project_workflow_id,
                        project_id,
                    )
                    if amendment is not None:
                        configured_inputs = {**configured_inputs, "amendment_id": str(amendment)}
                if configured_inputs != input_payload:
                    raise RuntimeError(
                        "project workflow inputs do not match its saved configuration"
                    )
            elif pinned_definition is None:
                definition_commit_sha = workflow["current_commit_sha"]
            if retry_of_run_id is not None:
                retried = await conn.fetchrow(
                    """
                    SELECT project_id, workflow_id, project_workflow_id, status,
                           review_source_run_id
                    FROM workflow_runs
                    WHERE id = $1
                    """,
                    retry_of_run_id,
                )
                if (
                    retried is None
                    or retried["project_id"] != project_id
                    or retried["workflow_id"] != workflow_id
                    or retried["project_workflow_id"] != project_workflow_id
                    or retried["status"] != "failed"
                ):
                    raise RuntimeError("the selected failed run cannot be retried")
                if retried["review_source_run_id"] is not None:
                    raise RuntimeError(
                        "Open the failed revision and use Retry revision to preserve its feedback."
                    )
            executor = workflow["executor"]
            task_title: str | None = None
            if executor == PROJECT_TASK_WORKFLOW_NAME:
                task_title = input_payload.get("title")
                instruction = input_payload.get("instruction")
                if not isinstance(task_title, str) or not task_title.strip():
                    raise ValueError("project.task requires a title")
                if not isinstance(instruction, str) or not instruction.strip():
                    raise ValueError("project.task requires an instruction")
                task_title = task_title.strip()
                instruction = instruction.strip()
                input_payload = {"title": task_title, "instruction": instruction}
                active_task_id = await conn.fetchval(
                    """
                    SELECT id FROM workflow_runs
                    WHERE project_id = $1 AND executor = 'project.task'
                      AND status IN ('pending', 'running', 'needs_input', 'paused')
                    """,
                    project_id,
                )
                if active_task_id is not None:
                    raise RuntimeError(f"project already has active task {active_task_id}")
            definition = (
                pinned_definition
                if pinned_definition is not None
                else _json_object(workflow["definition"], field="workflow definition")
            )
            from tin_lite import content_repository_delivery

            if workflow_id == content_repository_delivery.WORKFLOW_ID:
                if content_delivery_source is None:
                    raise ValueError(
                        "Select the approved article before creating its delivery run."
                    )
                await content_repository_delivery.guard_source(
                    conn,
                    project_id=project_id,
                    inputs=input_payload,
                    source=content_delivery_source,
                )
            elif content_delivery_source is not None:
                raise ValueError("Delivery sources belong only to the content delivery procedure.")
            if draft_selection is not None:
                from tin_lite.content_draft_progress import guard_selection

                if workflow["key"] != "content.generate" or workflow["project_id"] is not None:
                    raise ValueError("Article selection belongs only to planned content drafting.")
                if review_transition is None:
                    await guard_selection(
                        conn, project_id=project_id, inputs=input_payload, selection=draft_selection
                    )
            elif workflow["key"] == "content.generate" and not input_payload.get("item_id"):
                raise ValueError("Prepare the next article selection before creating a draft run.")
            system_wiki = definition.get("system_wiki")
            system_wiki_commit_sha = None
            if system_wiki is not None:
                if not isinstance(system_wiki, dict) or not isinstance(
                    system_wiki.get("commit_sha"), str
                ):
                    raise RuntimeError("workflow definition has an invalid system wiki reference")
                system_wiki_commit_sha = system_wiki["commit_sha"]
            human_review = definition.get("human_review")
            review_required = False
            if human_review is not None:
                if not isinstance(human_review, dict) or human_review.get("eligible") is not True:
                    raise RuntimeError("workflow definition has an invalid human review policy")
                # This is the current conservative project default. A future project-wide
                # autonomy setting will resolve this boolean here, once, when the run starts.
                review_required = True
            temporal_workflow_id = f"{executor}:{run_id}"
            thread_id = str(project_workflow_id or workflow_id)
            generation = await conn.fetchval(
                """
                SELECT COALESCE(MAX(generation), 0) + 1
                FROM workflow_runs
                WHERE project_id = $1 AND thread_id = $2
                """,
                project_id,
                thread_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO workflow_runs (
                    id, project_id, workflow_id, executor, definition_commit_sha,
                    system_wiki_commit_sha, temporal_workflow_id, thread_id, generation, status,
                    review_required, started_by_clerk_user_id, start_idempotency_key,
                    input, task_title, task_phase, project_workflow_id, trigger_source,
                    scheduled_for, trigger_client, started_by_oauth_client_id, retry_of_run_id,
                    prerequisite_evidence
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending', $10, $11, $12,
                    $13::jsonb, $14, CASE WHEN $4 = 'project.task' THEN 'preparing' END,
                    $15, $16, $17, $18, $19, $20, $21::jsonb
                )
                RETURNING *
                """,
                run_id,
                project_id,
                workflow_id,
                executor,
                definition_commit_sha,
                system_wiki_commit_sha,
                temporal_workflow_id,
                thread_id,
                generation,
                review_required,
                started_by_clerk_user_id,
                start_idempotency_key,
                json.dumps(input_payload),
                task_title,
                project_workflow_id,
                trigger_source,
                scheduled_for,
                trigger_client,
                started_by_oauth_client_id,
                retry_of_run_id,
                json.dumps(prerequisite_evidence) if prerequisite_evidence is not None else None,
            )
            assert row is not None
            await self._track_run(run_id, "run_created", conn=conn)
            if trigger_source == "schedule" and executor == "workflow.code":
                from tin_lite.schedules import WorkflowSchedule, next_run_after

                await conn.execute(
                    "UPDATE project_workflows SET next_run_at=$2, updated_at=now() "
                    "WHERE id=$1 AND status='active' AND settings_revision=$3",
                    project_workflow_id,
                    next_run_after(
                        WorkflowSchedule.model_validate(
                            _json_object(configured["schedule"], field="schedule")
                        ),
                        scheduled_for,
                    ),
                    schedule_settings_revision,
                )
            if content_delivery_source is not None:
                key = content_repository_delivery.source_key(run_id)
                await self.start_effect(
                    conn, execution_key=key, operation=content_repository_delivery.OPERATION
                )
                await self.complete_effect(conn, execution_key=key, result=content_delivery_source)
            if payment_card is not None:
                await payment_card.store(conn, run_id=run_id, project_id=project_id)
            if draft_selection is not None:
                from tin_lite import content_draft

                selection_key = content_draft.selection_key(run_id)
                await self.start_effect(
                    conn, execution_key=selection_key, operation=content_draft.SELECTION_OPERATION
                )
                await self.complete_effect(
                    conn, execution_key=selection_key, result=draft_selection
                )
                row = await conn.fetchrow(
                    "UPDATE workflow_runs SET progress_mode='steps', progress_current=0, "
                    "progress_total=3, progress_summary=$2 WHERE id=$1 RETURNING *",
                    run_id,
                    f"Selected article: {draft_selection['item']['title']}",
                )
            if self.billing is not None:
                await self.billing.admit(
                    conn,
                    run=row,
                    definition=definition,
                    quote_id=billing_quote_id,
                    parent_id=billing_parent_run_id,
                )
            if review_transition is not None:
                from tin_lite.workflow_review_store import accept_revision

                row = await accept_revision(conn, command=review_transition, successor=row)
            if executor == PROJECT_TASK_WORKFLOW_NAME:
                await conn.execute(
                    """
                    INSERT INTO project_task_entries (
                        id, run_id, kind, source, content, author_clerk_user_id
                    )
                    VALUES ($1, $2, 'instruction', 'founder', $3, $4)
                    """,
                    uuid4(),
                    run_id,
                    input_payload["instruction"],
                    started_by_clerk_user_id,
                )
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'workflow_run_started', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                project_id,
                run_id,
                json.dumps(
                    {
                        "kind": "runs" if trigger_source == "schedule" else "your_edits",
                        "actor_clerk_user_id": started_by_clerk_user_id,
                        "trigger_source": trigger_source,
                        "retry_of_run_id": str(retry_of_run_id) if retry_of_run_id else None,
                    }
                ),
                (
                    f"{workflow['title']} started on schedule."
                    if trigger_source == "schedule"
                    else f"{workflow['title']} started."
                ),
                f"{run_id}:workflow_run_started",
            )
        return _run(row), True

    async def list_task_entries(self, *, run_id: UUID) -> list[ProjectTaskEntry]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM project_task_entries
            WHERE run_id = $1
            ORDER BY created_at, id
            """,
            run_id,
        )
        return [_project_task_entry(row) for row in rows]

    async def append_task_entry(
        self,
        *,
        run_id: UUID,
        kind: str,
        source: str,
        content: str,
        request_id: UUID | None = None,
        author_clerk_user_id: str | None = None,
        delivered: bool = False,
    ) -> ProjectTaskEntry:
        normalized = content.strip()
        if not normalized:
            raise ValueError("task entry cannot be empty")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO project_task_entries (
                    id, run_id, request_id, kind, source, content,
                    author_clerk_user_id, delivered_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, CASE WHEN $8 THEN now() END)
                ON CONFLICT (run_id, request_id) WHERE request_id IS NOT NULL DO NOTHING
                RETURNING *
                """,
                uuid4(),
                run_id,
                request_id,
                kind,
                source,
                normalized,
                author_clerk_user_id,
                delivered,
            )
            if row is None and request_id is not None:
                row = await conn.fetchrow(
                    """
                    SELECT * FROM project_task_entries
                    WHERE run_id = $1 AND request_id = $2
                    """,
                    run_id,
                    request_id,
                )
                if row is None:
                    raise RuntimeError("task message idempotency lookup failed")
                if (
                    row["kind"] != kind
                    or row["source"] != source
                    or row["content"] != normalized
                    or row["author_clerk_user_id"] != author_clerk_user_id
                ):
                    raise RuntimeError("task message request ID belongs to different content")
            assert row is not None
        return _project_task_entry(row)

    async def append_task_event(
        self,
        *,
        run_id: UUID,
        entry_id: UUID,
        content: str,
    ) -> None:
        normalized = content.strip()
        if not normalized:
            return
        await self.pool.execute(
            """
            INSERT INTO project_task_entries (id, run_id, kind, source, content)
            VALUES ($1, $2, 'event', 'tin', $3)
            ON CONFLICT (id) DO NOTHING
            """,
            entry_id,
            run_id,
            normalized[:1000],
        )

    async def mark_task_entry_delivered(self, *, entry_id: UUID, run_id: UUID) -> None:
        await self.pool.execute(
            """
            UPDATE project_task_entries
            SET delivered_at = COALESCE(delivered_at, now())
            WHERE id = $1 AND run_id = $2
            """,
            entry_id,
            run_id,
        )

    async def undelivered_task_directions(self, *, run_id: UUID) -> list[ProjectTaskEntry]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM project_task_entries
            WHERE run_id = $1
              AND source = 'founder'
              AND kind IN ('direction', 'answer')
              AND delivered_at IS NULL
            ORDER BY created_at, id
            """,
            run_id,
        )
        return [_project_task_entry(row) for row in rows]

    async def set_task_running(self, *, run_id: UUID, sandbox_id: str | None = None) -> None:
        await self.pool.execute(
            """
            UPDATE workflow_runs
            SET status = 'running', task_phase = 'working', task_control = NULL,
                task_question = NULL, task_question_requested_at = NULL,
                sandbox_id = COALESCE($2, sandbox_id),
                started_at = COALESCE(started_at, now())
            WHERE id = $1 AND executor = 'project.task'
              AND status IN ('pending', 'running', 'needs_input', 'paused')
            """,
            run_id,
            sandbox_id,
        )

    async def request_task_control(self, *, run_id: UUID, control: str) -> WorkflowRun:
        if control not in {"pause", "stop"}:
            raise ValueError("unsupported task control")
        row = await self.pool.fetchrow(
            """
            UPDATE workflow_runs
            SET task_control = $2
            WHERE id = $1 AND executor = 'project.task'
              AND status IN ('pending', 'running', 'needs_input', 'paused')
            RETURNING *
            """,
            run_id,
            control,
        )
        if row is None:
            raise RuntimeError("task cannot accept that control in its current state")
        return _run(row)

    async def clear_task_control(self, *, run_id: UUID) -> WorkflowRun:
        row = await self.pool.fetchrow(
            """
            UPDATE workflow_runs
            SET task_control = NULL, status = 'running', task_phase = 'working',
                task_question = NULL, task_question_requested_at = NULL,
                expected_head_sha = CASE
                    WHEN task_phase = 'review' THEN NULL
                    ELSE expected_head_sha
                END
            WHERE id = $1 AND executor = 'project.task'
              AND status IN ('paused', 'needs_input')
            RETURNING *
            """,
            run_id,
        )
        if row is None:
            raise RuntimeError("task is not paused or waiting for an answer")
        return _run(row)

    async def set_task_review_actor(self, *, run_id: UUID, clerk_user_id: str) -> None:
        await self.pool.execute(
            """
            UPDATE workflow_runs
            SET reviewed_by_clerk_user_id = COALESCE(reviewed_by_clerk_user_id, $2)
            WHERE id = $1 AND executor = 'project.task'
              AND status = 'needs_input' AND task_phase = 'review'
            """,
            run_id,
            clerk_user_id,
        )

    async def begin_task_approval(self, *, run_id: UUID, clerk_user_id: str) -> WorkflowRun:
        row = await self.pool.fetchrow(
            """
            UPDATE workflow_runs
            SET status = 'running', task_phase = 'applying',
                reviewed_by_clerk_user_id = COALESCE(reviewed_by_clerk_user_id, $2),
                error_message = NULL
            WHERE id = $1 AND executor = 'project.task'
              AND status = 'needs_input' AND task_phase = 'review'
            RETURNING *
            """,
            run_id,
            clerk_user_id,
        )
        if row is None:
            current = await self.get_run(run_id)
            if (
                current is not None
                and current.executor == PROJECT_TASK_WORKFLOW_NAME
                and (
                    (current.status == RunStatus.RUNNING and current.task_phase == "applying")
                    or (
                        current.status == RunStatus.SUCCEEDED
                        and current.review_decision == "approved"
                    )
                )
            ):
                return current
            raise RuntimeError("task changes are not waiting for approval")
        return _run(row)

    async def defer_task_approval(self, *, run_id: UUID, summary: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'needs_input', task_phase = 'review',
                    task_summary = $2, error_message = NULL,
                    review_requested_at = now()
                WHERE id = $1 AND executor = 'project.task'
                  AND status = 'running' AND task_phase = 'applying'
                  AND review_decision IS NULL
                RETURNING project_id
                """,
                run_id,
                summary[:1000],
            )
            if row is None:
                return
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'project_task_apply_deferred', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                row["project_id"],
                run_id,
                json.dumps({"kind": "needs_you", "status": "needs_input"}),
                summary[:1000],
                f"{run_id}:task:apply_deferred",
            )

    async def project_task_outcome(
        self,
        *,
        run_id: UUID,
        outcome: str,
        summary: str,
        message: str,
        question: str | None = None,
        task_diff: dict[str, Any] | None = None,
        has_changes: bool = False,
        execution_key: str | None = None,
    ) -> None:
        if outcome not in {
            "completed",
            "continue",
            "needs_input",
            "paused",
            "stopped",
            "review",
        }:
            raise ValueError("unsupported project task outcome")
        status_value = {
            "completed": "succeeded",
            "continue": "running",
            "needs_input": "needs_input",
            "paused": "paused",
            "stopped": "stopped",
            "review": "needs_input",
        }[outcome]
        phase_value = "working" if outcome == "continue" else outcome
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM workflow_runs WHERE id = $1 FOR UPDATE", run_id
            )
            if row is None:
                raise LookupError(f"run {run_id} does not exist")
            if execution_key is not None:
                receipt = await self.get_effect(execution_key, conn=conn)
                if receipt is not None and receipt.status == "completed":
                    return
            await conn.execute(
                """
                UPDATE workflow_runs
                SET status = $2, task_phase = $3, task_summary = $4,
                    task_question = $5::text,
                    task_question_requested_at = CASE
                        WHEN $5::text IS NULL THEN NULL ELSE now()
                    END,
                    review_requested_at = CASE
                        WHEN $9 = 'review' THEN COALESCE(review_requested_at, now())
                        ELSE review_requested_at
                    END,
                    task_turn_number = task_turn_number + 1,
                    task_control = NULL, task_result = $6,
                    task_diff = $7::jsonb, task_has_changes = $8,
                    finished_at = CASE WHEN $2 IN ('succeeded', 'stopped') THEN now() ELSE NULL END,
                    sandbox_id = CASE WHEN $9 = 'review' THEN sandbox_id ELSE NULL END,
                    lease_active = CASE WHEN $9 = 'review' THEN lease_active ELSE false END,
                    lease_released_at = CASE
                        WHEN $9 = 'review' THEN lease_released_at
                        ELSE COALESCE(lease_released_at, now())
                    END
                WHERE id = $1
                """,
                run_id,
                status_value,
                phase_value,
                summary[:1000],
                question,
                message[:32000],
                json.dumps(task_diff) if task_diff is not None else None,
                has_changes,
                outcome,
            )
            entry_kind = "question" if outcome == "needs_input" else "message"
            entry_content = question if outcome == "needs_input" and question else message
            await conn.execute(
                """
                INSERT INTO project_task_entries (id, run_id, kind, source, content)
                VALUES ($1, $2, $3, 'codex', $4)
                """,
                uuid4(),
                run_id,
                entry_kind,
                entry_content[:32000],
            )
            event_summary = {
                "completed": "One-off task finished.",
                "continue": "One-off task continued with new direction.",
                "needs_input": "One-off task needs your answer.",
                "paused": "One-off task paused at a saved checkpoint.",
                "stopped": "One-off task stopped without applying changes.",
                "review": "One-off task changes are ready for review.",
            }[outcome]
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'project_task_state_changed', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                row["project_id"],
                run_id,
                json.dumps(
                    {
                        "kind": "needs_you" if status_value == "needs_input" else "runs",
                        "status": status_value,
                    }
                ),
                event_summary,
                f"{run_id}:task:{row['task_turn_number'] + 1}:{outcome}",
            )
            if execution_key is not None:
                await self.complete_effect(
                    conn, execution_key=execution_key, result={"outcome": outcome}
                )

    async def complete_task_approval(self, *, run_id: UUID, canonical_commit_sha: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM workflow_runs WHERE id = $1 FOR UPDATE", run_id
            )
            if row is None:
                raise LookupError(f"run {run_id} does not exist")
            if (
                row["status"] == RunStatus.SUCCEEDED.value
                and row["review_decision"] == "approved"
                and row["canonical_commit_sha"] == canonical_commit_sha
            ):
                return
            if (row["status"], row["task_phase"]) not in {
                (RunStatus.NEEDS_INPUT.value, "review"),
                (RunStatus.RUNNING.value, "applying"),
            }:
                raise RuntimeError("task is not waiting for change approval")
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'succeeded', task_phase = 'finished',
                    canonical_commit_sha = $2, review_decision = 'approved',
                    reviewed_at = now(),
                    finished_at = now(), lease_active = false,
                    lease_released_at = COALESCE(lease_released_at, now())
                WHERE id = $1 AND executor = 'project.task'
                  AND (
                    (status = 'needs_input' AND task_phase = 'review')
                    OR (status = 'running' AND task_phase = 'applying')
                  )
                RETURNING project_id
                """,
                run_id,
                canonical_commit_sha,
            )
            assert row is not None
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'project_task_changes_approved', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                row["project_id"],
                run_id,
                json.dumps({"kind": "your_edits"}),
                "You approved the one-off task changes.",
                f"{run_id}:task:approved",
            )

    async def set_review_actor(self, *, run_id: UUID, clerk_user_id: str) -> None:
        await self.pool.execute(
            """
            UPDATE workflow_runs
            SET reviewed_by_clerk_user_id = COALESCE(reviewed_by_clerk_user_id, $2)
            WHERE id = $1
              AND review_required
              AND status = 'needs_input'
            """,
            run_id,
            clerk_user_id,
        )

    async def get_run(
        self, run_id: UUID, *, conn: asyncpg.Connection | None = None
    ) -> WorkflowRun | None:
        row = await (conn or self.pool).fetchrow(
            "SELECT * FROM workflow_runs WHERE id = $1", run_id
        )
        return _run(row) if row else None

    async def latest_active_run(self, *, project_id: UUID, executor: str) -> WorkflowRun | None:
        row = await self.pool.fetchrow(
            """
            SELECT * FROM workflow_runs
            WHERE project_id = $1 AND executor = $2
              AND status IN ('pending', 'running', 'needs_input')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            project_id,
            executor,
        )
        return _run(row) if row else None

    async def list_runs(self, *, project_id: UUID, limit: int = 100) -> list[WorkflowRun]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM workflow_runs
            WHERE project_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            project_id,
            limit,
        )
        return [_run(row) for row in rows]

    async def list_product_activity(
        self, *, project_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[ActivityEvent]:
        rows = await self.pool.fetch(
            """
            SELECT events.*,
                   jsonb_strip_nulls(
                       events.details || jsonb_build_object(
                           'trigger_source', runs.trigger_source,
                           'trigger_client', runs.trigger_client
                       )
                   ) AS details,
                   COALESCE(runs.executor, events.details->>'workflow_key') AS workflow_key,
                   COALESCE(workflows.title, events.details->>'workflow_title') AS workflow_title
            FROM activity_events AS events
            LEFT JOIN workflow_runs AS runs ON runs.id = events.run_id
            LEFT JOIN workflows ON workflows.id = runs.workflow_id
            WHERE events.project_id = $1
              AND events.audience = 'product'
              AND events.created_at >= now() - interval '30 days'
            ORDER BY events.created_at DESC, events.id DESC
            LIMIT $2 OFFSET $3
            """,
            project_id,
            limit,
            offset,
        )
        return [_activity_event(row) for row in rows]

    async def list_pending_decisions(self, *, project_id: UUID) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            f"""
            WITH pending AS (
            SELECT COALESCE(decision.id, run.id) AS id,
                   run.id AS run_id,
                   run.project_id,
                   workflow.key AS workflow_key,
                   workflow.title AS workflow_title,
                   COALESCE(decision.kind, 'review') AS kind,
                   CASE WHEN workflow.key='content.generate' AND workflow.project_id IS NULL
                       AND COALESCE(decision.title, 'Review workflow output')
                           ='Review workflow output'
                       THEN COALESCE('Review: ' || (
                           SELECT receipt.result->'item'->>'title' FROM effect_receipts receipt
                           WHERE receipt.execution_key=
                               'content-draft:' || run.id::text || ':prepare'
                             AND receipt.operation='content.generate' AND receipt.status='completed'
                       ), 'Review ' || workflow.title)
                       ELSE COALESCE(decision.title, 'Review ' || workflow.title) END AS title,
                   COALESCE(
                       NULLIF(decision.explanation, ''),
                       CASE
                           WHEN run.executor = 'project.task'
                               THEN 'Review the proposed project changes before they are applied.'
                           ELSE 'Review the complete output before this workflow continues.'
                       END
                   ) AS explanation,
                   COALESCE(NULLIF(decision.consequence, ''), '') AS consequence,
                   COALESCE(
                       decision.items,
                       CASE WHEN run.artifact_path IS NULL THEN '[]'::jsonb ELSE
                           jsonb_build_array(jsonb_build_object(
                               'id', run.artifact_path,
                               'title', regexp_replace(run.artifact_path, '^.*/', ''),
                               'file', run.artifact_path,
                               'revision', run.canonical_commit_sha,
                               'media_type', CASE
                                   WHEN lower(run.artifact_path) ~ '\\.(md|markdown)$'
                                       THEN 'text/markdown'
                                   WHEN lower(run.artifact_path) ~ '\\.svg$'
                                       THEN 'image/svg+xml'
                                   WHEN lower(run.artifact_path) ~ '\\.mp4$'
                                       THEN 'video/mp4'
                                   ELSE 'application/octet-stream'
                               END
                           ))
                       END
                   ) AS items,
                   COALESCE(decision.response_schema, '{{}}'::jsonb) AS response_schema,
                   COALESCE(decision.feedback_supported, false) AS feedback_supported,
                   COALESCE(decision.status, 'pending') AS status,
                   decision.deadline_at,
                   COALESCE(decision.created_at, run.review_requested_at, run.created_at)
                       AS created_at,
                   NULL::jsonb AS output_resolution
            FROM workflow_runs AS run
            JOIN workflows AS workflow ON workflow.id = run.workflow_id
            LEFT JOIN LATERAL (
                SELECT pending.*
                FROM run_decisions AS pending
                WHERE pending.run_id = run.id AND pending.status = 'pending'
                ORDER BY pending.created_at DESC, pending.id DESC
                LIMIT 1
            ) AS decision ON true
            WHERE run.project_id = $1
              AND run.status = 'needs_input'
              AND (run.review_required OR run.executor = 'project.task')
            UNION ALL
            SELECT run.id, run.id, run.project_id, workflow.key, workflow.title,
                   'output_conflict',
                   'Choose a version of ' || regexp_replace(
                       run.retained_output->>'artifact_path', '^.*/', ''),
                   'This file changed while the workflow ran. Tin left the file alone '
                       || 'and saved the result. Compare the two before choosing.',
                   'Nothing changes until you confirm on the comparison.',
                   jsonb_build_array(jsonb_build_object(
                       'file', run.retained_output->>'artifact_path',
                       'revision', run.retained_output->>'ephemeral_commit_sha',
                       'media_type', run.retained_output->>'media_type',
                       'source', 'retained'
                   )), '{{}}'::jsonb, false, 'pending', NULL::timestamptz,
                   run.created_at, run.output_resolution
            FROM workflow_runs AS run
            JOIN workflows AS workflow ON workflow.id = run.workflow_id
            WHERE run.project_id = $1 AND ({_PENDING_OUTPUT_CONFLICT_SQL})
            ) SELECT * FROM pending ORDER BY created_at, run_id
            """,  # noqa: S608 — static SQL predicate, no caller text
            project_id,
        )
        return [
            {
                **dict(row),
                "items": _json_list(row["items"], field="decision items"),
                "response_schema": _json_object(
                    row["response_schema"], field="decision response schema"
                ),
                "output_resolution": (
                    _json_object(row["output_resolution"], field="output resolution")
                    if row.get("output_resolution") is not None
                    else None
                ),
            }
            for row in rows
        ]

    async def get_pending_decision(self, *, decision_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(decision.id, run.id) AS id,
                   run.id AS run_id,
                   run.project_id,
                   run.executor AS workflow_name,
                   COALESCE(decision.kind, 'review') AS kind,
                   COALESCE(decision.feedback_supported, false) AS feedback_supported
            FROM workflow_runs AS run
            LEFT JOIN run_decisions AS decision
              ON decision.run_id = run.id AND decision.status = 'pending'
            WHERE run.status = 'needs_input'
              AND (decision.id = $1 OR (decision.id IS NULL AND run.id = $1))
            ORDER BY decision.created_at DESC NULLS LAST
            LIMIT 1
            """,
            decision_id,
        )
        return dict(row) if row is not None else None

    async def apply_run_decision(
        self,
        *,
        decision_id: UUID,
        clerk_user_id: str,
        response: dict[str, Any],
    ) -> None:
        await self.pool.execute(
            """
            UPDATE run_decisions
            SET status = 'applied', response = $3::jsonb, applied_at = now(),
                applied_by_clerk_user_id = $2
            WHERE id = $1 AND status = 'pending'
            """,
            decision_id,
            clerk_user_id,
            json.dumps(response),
        )

    async def list_runs_for_period(
        self,
        *,
        project_id: UUID,
        period_start: datetime,
        period_end: datetime,
        exclude_run_id: UUID,
        limit: int = 20,
    ) -> list[WorkflowRun]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM workflow_runs
            WHERE project_id = $1 AND id <> $2
              AND COALESCE(finished_at, started_at, created_at) >= $3
              AND COALESCE(finished_at, started_at, created_at) < $4
            ORDER BY COALESCE(finished_at, started_at, created_at), id
            LIMIT $5
            """,
            project_id,
            exclude_run_id,
            period_start,
            period_end,
            limit,
        )
        return [_run(row) for row in rows]

    async def list_product_activity_for_period(
        self,
        *,
        project_id: UUID,
        period_start: datetime,
        period_end: datetime,
        exclude_run_id: UUID,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        rows = await self.pool.fetch(
            """
            SELECT events.*,
                   jsonb_strip_nulls(
                       events.details || jsonb_build_object(
                           'trigger_source', runs.trigger_source,
                           'trigger_client', runs.trigger_client
                       )
                   ) AS details,
                   COALESCE(runs.executor, events.details->>'workflow_key') AS workflow_key,
                   COALESCE(workflows.title, events.details->>'workflow_title') AS workflow_title
            FROM activity_events AS events
            LEFT JOIN workflow_runs AS runs ON runs.id = events.run_id
            LEFT JOIN workflows ON workflows.id = runs.workflow_id
            WHERE events.project_id = $1 AND events.audience = 'product'
              AND events.created_at >= $2 AND events.created_at < $3
              AND (events.run_id IS NULL OR events.run_id <> $4)
            ORDER BY events.created_at, events.id
            LIMIT $5
            """,
            project_id,
            period_start,
            period_end,
            exclude_run_id,
            limit,
        )
        return [_activity_event(row) for row in rows]

    async def list_memory_source_runs(
        self, *, project_id: UUID, exclude_run_id: UUID, limit: int = 20
    ) -> list[WorkflowRun]:
        rows = await self.pool.fetch(
            """
            SELECT *
            FROM (
                SELECT * FROM workflow_runs
                WHERE project_id = $1
                  AND id <> $2
                  AND executor <> 'project.memory'
                  AND status = 'succeeded'
                  AND canonical_commit_sha IS NOT NULL
                  AND artifact_path IS NOT NULL
                  AND artifact_path <> 'wiki/INDEX.md'
                  AND artifact_ref IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT $3
            ) AS recent
            ORDER BY finished_at, id
            """,
            project_id,
            exclude_run_id,
            limit,
        )
        return [_run(row) for row in rows]

    async def list_prerequisite_runs(
        self, *, project_id: UUID, workflow_keys: Sequence[str], limit: int = 200
    ) -> list[tuple[str, WorkflowRun]]:
        """Succeeded, published runs of the named workflows, newest first, keyed by workflow.

        Codex procedures share one executor, so the workflow identity comes from the catalog
        row rather than the run's executor column.
        """
        if not workflow_keys:
            return []
        rows = await self.pool.fetch(
            """
            SELECT run.*, workflow.key AS workflow_key
            FROM workflow_runs AS run
            JOIN workflows AS workflow ON workflow.id = run.workflow_id
            WHERE run.project_id = $1
              AND workflow.key = ANY($2::text[])
              AND (workflow.project_id IS NULL OR workflow.project_id = $1)
              AND run.status = 'succeeded'
              AND run.canonical_commit_sha IS NOT NULL
            ORDER BY run.finished_at DESC NULLS LAST, run.created_at DESC, run.id DESC
            LIMIT $3
            """,
            project_id,
            list(workflow_keys),
            limit,
        )
        return [(row["workflow_key"], _run(row)) for row in rows]

    async def create_email_campaign(
        self,
        *,
        run_id: UUID,
        project_id: UUID,
        connection_id: UUID,
        external_account_id: str,
        shortlist_path: str,
        shortlist_commit_sha: str,
        plan_path: str,
        plan_commit_sha: str,
        follow_up_delay_days: int | None,
        send_interval_seconds: int,
        daily_send_cap: int,
        send_window_start: time,
        send_window_end: time,
        send_timezone: str,
        recipients: tuple[Any, ...],
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"email-campaign:{run_id}",
            )
            existing = await conn.fetchrow(
                "SELECT * FROM outreach_campaigns WHERE run_id = $1 FOR UPDATE", run_id
            )
            if existing is not None:
                matches = (
                    existing["project_id"] == project_id
                    and existing["channel"] == "email"
                    and existing["integration_connection_id"] == connection_id
                    and existing["external_account_id"] == external_account_id
                    and existing["source_path"] == shortlist_path
                    and existing["source_commit_sha"] == shortlist_commit_sha
                    and existing["review_path"] == plan_path
                    and existing["review_commit_sha"] == plan_commit_sha
                    and existing["follow_up_delay_days"] == follow_up_delay_days
                    and existing["send_interval_seconds"] == send_interval_seconds
                    and existing["daily_send_cap"] == daily_send_cap
                    and existing["send_window_start"] == send_window_start
                    and existing["send_window_end"] == send_window_end
                    and existing["send_timezone"] == send_timezone
                    and existing["recipient_count"] == len(recipients)
                )
                if not matches:
                    raise SideEffectConflictError("email campaign snapshot conflicts")
                await self._refresh_email_campaign_progress(conn, run_id=run_id)
                return
            await conn.execute(
                """
                INSERT INTO outreach_campaigns (
                    run_id, project_id, channel, integration_connection_id,
                    external_account_id, source_path, source_commit_sha,
                    review_path, review_commit_sha, follow_up_delay_days,
                    send_interval_seconds, daily_send_cap, send_window_start,
                    send_window_end, send_timezone, recipient_count
                )
                VALUES (
                    $1, $2, 'email', $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15
                )
                """,
                run_id,
                project_id,
                connection_id,
                external_account_id,
                shortlist_path,
                shortlist_commit_sha,
                plan_path,
                plan_commit_sha,
                follow_up_delay_days,
                send_interval_seconds,
                daily_send_cap,
                send_window_start,
                send_window_end,
                send_timezone,
                len(recipients),
            )
            for recipient in recipients:
                await conn.execute(
                    """
                    INSERT INTO outreach_recipients (
                        id, campaign_run_id, project_id, contact_key, address,
                        display_name
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    recipient.id,
                    run_id,
                    project_id,
                    recipient.candidate_id,
                    recipient.email,
                    recipient.name,
                )
                for step_key in (
                    ("initial", "follow_up") if follow_up_delay_days is not None else ("initial",)
                ):
                    await conn.execute(
                        """
                        INSERT INTO outreach_deliveries (
                            execution_key, campaign_run_id, recipient_id, channel, step_key
                        )
                        VALUES ($1, $2, $3, 'email', $4)
                        """,
                        f"{run_id}:email:{recipient.id}:{step_key}",
                        run_id,
                        recipient.id,
                        step_key,
                    )
            await self._refresh_email_campaign_progress(conn, run_id=run_id)

    async def approve_email_campaign(self, *, run_id: UUID) -> None:
        updated = await self.pool.fetchval(
            """
            UPDATE outreach_campaigns
            SET status = 'running', approved_at = COALESCE(approved_at, now()), updated_at = now()
            WHERE run_id = $1 AND status IN ('draft', 'approved', 'running')
            RETURNING true
            """,
            run_id,
        )
        if updated is None:
            raise RuntimeError("email campaign is not awaiting approval")
        await self.refresh_email_campaign_progress(run_id=run_id)

    async def get_email_campaign(self, run_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM outreach_campaigns WHERE run_id = $1", run_id)
        return dict(row) if row is not None else None

    async def get_outreach_campaign_projection(self, run_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT campaign.*,
                   COALESCE(delivery.delivered_count, 0) AS delivered_count,
                   COALESCE(delivery.unknown_delivery_count, 0) AS unknown_delivery_count,
                   COALESCE(delivery.failed_delivery_count, 0) AS failed_delivery_count,
                   delivery.next_delivery_at,
                   COALESCE(approved.follow_up_body, run.input->>'follow_up_body')
                       AS current_follow_up_body,
                   pending.id AS pending_revision_id,
                   pending.revision_number AS pending_revision_number,
                   pending.previous_follow_up_body,
                   pending.follow_up_body AS proposed_follow_up_body,
                   pending.review_path AS pending_revision_path,
                   pending.review_commit_sha AS pending_revision_commit_sha,
                   pending.requested_at AS pending_revision_requested_at
            FROM outreach_campaigns AS campaign
            JOIN workflow_runs AS run ON run.id = campaign.run_id
            LEFT JOIN LATERAL (
                SELECT count(*) FILTER (WHERE status = 'sent') AS delivered_count,
                       count(*) FILTER (WHERE status = 'unknown') AS unknown_delivery_count,
                       count(*) FILTER (WHERE status = 'failed') AS failed_delivery_count,
                       min(scheduled_for) FILTER (WHERE status = 'pending') AS next_delivery_at
                FROM outreach_deliveries
                WHERE campaign_run_id = campaign.run_id
            ) AS delivery ON true
            LEFT JOIN LATERAL (
                SELECT follow_up_body
                FROM outreach_campaign_revisions
                WHERE campaign_run_id = campaign.run_id AND status = 'approved'
                ORDER BY revision_number DESC
                LIMIT 1
            ) AS approved ON true
            LEFT JOIN LATERAL (
                SELECT id, revision_number, previous_follow_up_body, follow_up_body,
                       review_path, review_commit_sha, requested_at
                FROM outreach_campaign_revisions
                WHERE campaign_run_id = campaign.run_id AND status = 'pending'
                LIMIT 1
            ) AS pending ON true
            WHERE campaign.run_id = $1
            """,
            run_id,
        )
        return dict(row) if row is not None else None

    async def list_outreach_campaign_deliveries(self, *, run_id: UUID) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT recipient.address AS recipient_address,
                   recipient.display_name AS recipient_name,
                   recipient.status AS recipient_status,
                   delivery.step_key AS step,
                   delivery.status,
                   delivery.scheduled_for,
                   delivery.sent_at,
                   recipient.replied_at
            FROM outreach_deliveries AS delivery
            JOIN outreach_recipients AS recipient
              ON recipient.id = delivery.recipient_id
            WHERE delivery.campaign_run_id = $1
            ORDER BY recipient.created_at, recipient.id,
                     CASE delivery.step_key WHEN 'initial' THEN 0 ELSE 1 END
            """,
            run_id,
        )
        return [dict(row) for row in rows]

    async def get_pending_email_campaign_revision(self, *, run_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT * FROM outreach_campaign_revisions
            WHERE campaign_run_id = $1 AND status = 'pending'
            """,
            run_id,
        )
        return dict(row) if row is not None else None

    async def begin_email_campaign_revision(
        self,
        *,
        revision_id: UUID,
        run_id: UUID,
        request_id: UUID,
        follow_up_body: str,
        review_path: str,
        clerk_user_id: str,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn, conn.transaction():
            campaign = await conn.fetchrow(
                """
                SELECT campaign.*, run.input AS campaign_input, run.status AS run_status
                FROM outreach_campaigns AS campaign
                JOIN workflow_runs AS run ON run.id = campaign.run_id
                WHERE campaign.run_id = $1
                FOR UPDATE OF campaign, run
                """,
                run_id,
            )
            if campaign is None:
                raise LookupError("email campaign not found")
            existing = await conn.fetchrow(
                """
                SELECT * FROM outreach_campaign_revisions
                WHERE project_id = $1 AND request_id = $2
                """,
                campaign["project_id"],
                request_id,
            )
            if existing is not None:
                if (
                    existing["campaign_run_id"] != run_id
                    or existing["follow_up_body"] != follow_up_body
                ):
                    raise SideEffectConflictError("email campaign revision request conflicts")
                result = dict(existing)
                result["pending_recipient_count"] = int(
                    await conn.fetchval(
                        """
                        SELECT count(*) FROM outreach_deliveries
                        WHERE campaign_run_id = $1
                          AND step_key = 'follow_up' AND status = 'pending'
                        """,
                        run_id,
                    )
                )
                return result
            if campaign["status"] != "running" or campaign["run_status"] not in {
                RunStatus.RUNNING.value,
                RunStatus.NEEDS_INPUT.value,
            }:
                raise RuntimeError("only a running email campaign can be revised")
            pending = await conn.fetchrow(
                """
                SELECT * FROM outreach_campaign_revisions
                WHERE campaign_run_id = $1 AND status = 'pending'
                """,
                run_id,
            )
            if pending is not None:
                raise RuntimeError("email campaign already has a revision awaiting approval")
            delivery_state = await conn.fetchrow(
                """
                SELECT count(*) FILTER (
                           WHERE step_key = 'follow_up' AND status = 'pending'
                       ) AS pending_follow_ups,
                       count(*) FILTER (
                           WHERE status IN ('started', 'unknown')
                       ) AS in_flight
                FROM outreach_deliveries
                WHERE campaign_run_id = $1
                """,
                run_id,
            )
            assert delivery_state is not None
            if delivery_state["in_flight"]:
                raise RuntimeError("a campaign delivery is already in flight; try again shortly")
            if not delivery_state["pending_follow_ups"]:
                raise RuntimeError("email campaign has no pending follow-up to revise")
            approved = await conn.fetchrow(
                """
                SELECT follow_up_body
                FROM outreach_campaign_revisions
                WHERE campaign_run_id = $1 AND status = 'approved'
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                run_id,
            )
            campaign_input = _json_object(
                campaign["campaign_input"], field="outreach campaign input"
            )
            previous_body = (
                str(approved["follow_up_body"])
                if approved is not None
                else str(campaign_input.get("follow_up_body", "")).strip()
            )
            if not previous_body:
                raise RuntimeError("email campaign has no approved follow-up copy")
            if previous_body == follow_up_body:
                raise ValueError("revised follow-up must differ from the approved copy")
            revision_number = int(
                await conn.fetchval(
                    """
                    SELECT COALESCE(max(revision_number), 0) + 1
                    FROM outreach_campaign_revisions
                    WHERE campaign_run_id = $1
                    """,
                    run_id,
                )
            )
            row = await conn.fetchrow(
                """
                INSERT INTO outreach_campaign_revisions (
                    id, campaign_run_id, project_id, request_id, revision_number,
                    previous_follow_up_body, follow_up_body, review_path,
                    requested_by_clerk_user_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                revision_id,
                run_id,
                campaign["project_id"],
                request_id,
                revision_number,
                previous_body,
                follow_up_body,
                review_path,
                clerk_user_id,
            )
            await conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'needs_input', review_requested_at = now(), error_message = NULL
                WHERE id = $1 AND status = 'running'
                """,
                run_id,
            )
        assert row is not None
        result = dict(row)
        result["pending_recipient_count"] = int(delivery_state["pending_follow_ups"])
        return result

    async def finalize_email_campaign_revision(
        self,
        *,
        revision_id: UUID,
        review_commit_sha: str,
        artifact_ref: str,
        pending_recipient_count: int,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn, conn.transaction():
            revision = await conn.fetchrow(
                """
                UPDATE outreach_campaign_revisions
                SET review_commit_sha = COALESCE(review_commit_sha, $2)
                WHERE id = $1 AND status = 'pending'
                  AND (review_commit_sha IS NULL OR review_commit_sha = $2)
                RETURNING *
                """,
                revision_id,
                review_commit_sha,
            )
            if revision is None:
                raise RuntimeError("email campaign revision cannot be finalized")
            await conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'needs_input', canonical_commit_sha = $2,
                    artifact_ref = $3, artifact_path = $4, error_message = NULL
                WHERE id = $1 AND status = 'needs_input'
                """,
                revision["campaign_run_id"],
                review_commit_sha,
                artifact_ref,
                revision["review_path"],
            )
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'email_campaign_revision_requested', $3::jsonb, $4,
                        'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                revision["project_id"],
                revision["campaign_run_id"],
                json.dumps(
                    {
                        "kind": "needs_you",
                        "revision_id": str(revision_id),
                        "artifact_ref": artifact_ref,
                    }
                ),
                (
                    f"A revised follow-up for {pending_recipient_count} pending recipient(s) "
                    "is ready for approval. Remaining deliveries are paused."
                ),
                f"{revision['campaign_run_id']}:email_campaign_revision:{revision_id}:requested",
            )
        return dict(revision)

    async def approve_email_campaign_revision(
        self, *, run_id: UUID, revision_id: UUID, clerk_user_id: str
    ) -> WorkflowRun:
        async with self.pool.acquire() as conn, conn.transaction():
            revision = await conn.fetchrow(
                """
                SELECT * FROM outreach_campaign_revisions
                WHERE id = $1 AND campaign_run_id = $2
                FOR UPDATE
                """,
                revision_id,
                run_id,
            )
            if revision is None:
                raise LookupError("email campaign revision not found")
            if revision["status"] == "approved":
                row = await conn.fetchrow("SELECT * FROM workflow_runs WHERE id = $1", run_id)
                assert row is not None
                return _run(row)
            if revision["status"] != "pending" or revision["review_commit_sha"] is None:
                raise RuntimeError("email campaign revision is not awaiting approval")
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'running', reviewed_at = now(),
                    reviewed_by_clerk_user_id = $2, error_message = NULL
                WHERE id = $1 AND status = 'needs_input'
                RETURNING *
                """,
                run_id,
                clerk_user_id,
            )
            if row is None:
                raise RuntimeError("email campaign is not waiting for revision approval")
            await conn.execute(
                """
                UPDATE outreach_campaign_revisions
                SET status = 'approved', reviewed_by_clerk_user_id = $2,
                    reviewed_at = now()
                WHERE id = $1 AND status = 'pending'
                """,
                revision_id,
                clerk_user_id,
            )
            await conn.execute(
                """
                UPDATE outreach_campaigns
                SET review_path = $2, review_commit_sha = $3, updated_at = now()
                WHERE run_id = $1 AND status = 'running'
                """,
                run_id,
                revision["review_path"],
                revision["review_commit_sha"],
            )
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'email_campaign_revision_approved', $3::jsonb, $4,
                        'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                revision["project_id"],
                run_id,
                json.dumps(
                    {
                        "kind": "your_edits",
                        "revision_id": str(revision_id),
                        "actor_clerk_user_id": clerk_user_id,
                    }
                ),
                "You approved the revised follow-up. Pending deliveries resumed.",
                f"{run_id}:email_campaign_revision:{revision_id}:approved",
            )
        return _run(row)

    async def discard_email_campaign_revision(
        self, *, run_id: UUID, revision_id: UUID, clerk_user_id: str
    ) -> WorkflowRun:
        async with self.pool.acquire() as conn, conn.transaction():
            revision = await conn.fetchrow(
                """
                SELECT * FROM outreach_campaign_revisions
                WHERE id = $1 AND campaign_run_id = $2
                FOR UPDATE
                """,
                revision_id,
                run_id,
            )
            if revision is None:
                raise LookupError("email campaign revision not found")
            if revision["status"] == "discarded":
                row = await conn.fetchrow("SELECT * FROM workflow_runs WHERE id = $1", run_id)
                assert row is not None
                return _run(row)
            if revision["status"] != "pending":
                raise RuntimeError("only a pending email campaign revision can be discarded")
            campaign = await conn.fetchrow(
                """
                SELECT campaign.*, project.state_repo_id
                FROM outreach_campaigns AS campaign
                JOIN projects AS project ON project.id = campaign.project_id
                WHERE campaign.run_id = $1
                FOR UPDATE OF campaign
                """,
                run_id,
            )
            assert campaign is not None
            await conn.execute(
                """
                UPDATE outreach_campaign_revisions
                SET status = 'discarded', reviewed_by_clerk_user_id = $2,
                    reviewed_at = now()
                WHERE id = $1 AND status = 'pending'
                """,
                revision_id,
                clerk_user_id,
            )
            artifact_ref = (
                f"code.storage://{campaign['state_repo_id']}@{campaign['review_commit_sha']}"
                f"/{campaign['review_path']}"
            )
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'running', canonical_commit_sha = $2,
                    artifact_ref = $3, artifact_path = $4, error_message = NULL
                WHERE id = $1 AND status = 'needs_input'
                RETURNING *
                """,
                run_id,
                campaign["review_commit_sha"],
                artifact_ref,
                campaign["review_path"],
            )
            if row is None:
                raise RuntimeError("email campaign is not waiting on this revision")
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'email_campaign_revision_discarded', $3::jsonb, $4,
                        'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                revision["project_id"],
                run_id,
                json.dumps({"kind": "your_edits", "revision_id": str(revision_id)}),
                "You discarded the follow-up revision. The approved campaign resumed unchanged.",
                f"{run_id}:email_campaign_revision:{revision_id}:discarded",
            )
        return _run(row)

    async def list_email_campaign_recipient_ids(self, *, run_id: UUID) -> list[UUID]:
        rows = await self.pool.fetch(
            """
            SELECT id FROM outreach_recipients
            WHERE campaign_run_id = $1
            ORDER BY created_at, id
            """,
            run_id,
        )
        return [row["id"] for row in rows]

    async def get_email_campaign_recipient(self, recipient_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT recipient.*, campaign.follow_up_delay_days,
                   campaign.integration_connection_id, campaign.external_account_id,
                   campaign.status AS campaign_status, run.input AS campaign_input,
                   COALESCE(approved.follow_up_body, run.input->>'follow_up_body')
                       AS effective_follow_up_body
            FROM outreach_recipients AS recipient
            JOIN outreach_campaigns AS campaign ON campaign.run_id = recipient.campaign_run_id
            JOIN workflow_runs AS run ON run.id = campaign.run_id
            LEFT JOIN LATERAL (
                SELECT follow_up_body
                FROM outreach_campaign_revisions
                WHERE campaign_run_id = campaign.run_id AND status = 'approved'
                ORDER BY revision_number DESC
                LIMIT 1
            ) AS approved ON true
            WHERE recipient.id = $1
            """,
            recipient_id,
        )
        if row is None:
            return None
        result = dict(row)
        result["campaign_input"] = _json_object(
            row["campaign_input"], field="outreach campaign input"
        )
        return result

    async def record_email_sent(
        self,
        *,
        recipient_id: UUID,
        stage: str,
        gmail_message_id: str,
        gmail_thread_id: str,
        execution_key: str,
    ) -> None:
        if stage == "initial":
            status_value = "initial_sent"
            query = """
                UPDATE outreach_recipients
                SET status = $2,
                    provider_thread_id = $3,
                    initial_sent_at = COALESCE(initial_sent_at, now()),
                    error_code = NULL,
                    updated_at = now()
                WHERE id = $1 AND status = ANY($4::text[])
                """
            accepted_states = ["pending", "initial_sent"]
        elif stage == "follow_up":
            status_value = "follow_up_sent"
            query = """
                UPDATE outreach_recipients
                SET status = $2,
                    provider_thread_id = $3,
                    follow_up_sent_at = COALESCE(follow_up_sent_at, now()),
                    error_code = NULL,
                    updated_at = now()
                WHERE id = $1 AND status = ANY($4::text[])
                """
            accepted_states = ["initial_sent", "follow_up_sent"]
        else:
            raise ValueError("email campaign stage is unsupported")
        async with self.pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                query,
                recipient_id,
                status_value,
                gmail_thread_id,
                accepted_states,
            )
            if result == "UPDATE 0":
                current = await conn.fetchrow(
                    "SELECT status FROM outreach_recipients WHERE id = $1", recipient_id
                )
                terminal_states = {"replied", "follow_up_sent", "completed"}
                if current is None or (
                    current["status"] != status_value and current["status"] not in terminal_states
                ):
                    raise RuntimeError("email recipient state does not accept this send")
            await conn.execute(
                """
                UPDATE outreach_deliveries
                SET status = 'sent', provider_message_id = $2,
                    sent_at = COALESCE(sent_at, now()), error_code = NULL, updated_at = now()
                WHERE execution_key = $1 AND status IN ('pending', 'started', 'unknown', 'sent')
                """,
                execution_key,
                gmail_message_id,
            )
            if stage == "initial":
                await conn.execute(
                    """
                    UPDATE outreach_deliveries AS delivery
                    SET scheduled_for = COALESCE(
                            delivery.scheduled_for,
                            recipient.initial_sent_at
                                + make_interval(days => campaign.follow_up_delay_days)
                        ),
                        updated_at = now()
                    FROM outreach_recipients AS recipient
                    JOIN outreach_campaigns AS campaign
                      ON campaign.run_id = recipient.campaign_run_id
                    WHERE delivery.recipient_id = recipient.id
                      AND delivery.recipient_id = $1
                      AND delivery.step_key = 'follow_up'
                      AND delivery.status = 'pending'
                    """,
                    recipient_id,
                )
            campaign_run_id = await conn.fetchval(
                "SELECT campaign_run_id FROM outreach_recipients WHERE id = $1", recipient_id
            )
            if campaign_run_id is not None:
                await self._refresh_email_campaign_progress(conn, run_id=campaign_run_id)

    async def start_outreach_delivery(self, *, execution_key: str) -> None:
        result = await self.pool.execute(
            """
            UPDATE outreach_deliveries
            SET status = 'started', started_at = COALESCE(started_at, now()),
                error_code = NULL, updated_at = now()
            WHERE execution_key = $1 AND status IN ('pending', 'started', 'unknown', 'failed')
            """,
            execution_key,
        )
        if result == "UPDATE 0":
            row = await self.pool.fetchrow(
                "SELECT status FROM outreach_deliveries WHERE execution_key = $1",
                execution_key,
            )
            if row is None or row["status"] != "sent":
                raise RuntimeError("outreach delivery cannot start")

    async def reserve_outreach_delivery(
        self,
        *,
        recipient_id: UUID,
        stage: str,
        now: datetime | None = None,
    ) -> int:
        """Atomically reserve one account-wide send slot or return seconds to wait."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT delivery.execution_key, delivery.status,
                       delivery.campaign_run_id,
                       campaign.external_account_id, campaign.daily_send_cap,
                       campaign.send_interval_seconds, campaign.send_window_start,
                       campaign.send_window_end, campaign.send_timezone,
                       EXISTS (
                           SELECT 1 FROM outreach_campaign_revisions AS revision
                           WHERE revision.campaign_run_id = campaign.run_id
                             AND revision.status = 'pending'
                       ) AS revision_pending
                FROM outreach_deliveries AS delivery
                JOIN outreach_campaigns AS campaign
                  ON campaign.run_id = delivery.campaign_run_id
                WHERE delivery.recipient_id = $1 AND delivery.step_key = $2
                FOR UPDATE OF delivery, campaign
                """,
                recipient_id,
                stage,
            )
            if row is None:
                raise RuntimeError("outreach delivery does not exist")
            if row["status"] in {"started", "sent", "unknown", "skipped"}:
                await self._refresh_email_campaign_progress(conn, run_id=row["campaign_run_id"])
                return 0
            if row["status"] != "pending":
                raise RuntimeError("outreach delivery cannot be reserved")

            async def defer(seconds: int) -> int:
                await conn.execute(
                    """
                    UPDATE outreach_deliveries
                    SET scheduled_for = $2, updated_at = now()
                    WHERE execution_key = $1 AND status = 'pending'
                    """,
                    row["execution_key"],
                    current + timedelta(seconds=seconds),
                )
                await self._refresh_email_campaign_progress(conn, run_id=row["campaign_run_id"])
                return seconds

            if row["revision_pending"]:
                return await defer(60)

            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"outreach-account:{row['external_account_id']}",
            )
            timezone = ZoneInfo(row["send_timezone"])
            local_now = current.astimezone(timezone)
            window_start = datetime.combine(local_now.date(), row["send_window_start"], timezone)
            window_end = datetime.combine(local_now.date(), row["send_window_end"], timezone)
            if local_now < window_start:
                return await defer(max(1, math.ceil((window_start - local_now).total_seconds())))
            if local_now >= window_end:
                next_start = datetime.combine(
                    local_now.date() + timedelta(days=1),
                    row["send_window_start"],
                    timezone,
                )
                return await defer(max(1, math.ceil((next_start - local_now).total_seconds())))
            day_start = datetime.combine(local_now.date(), time.min, timezone).astimezone(UTC)
            day_end = datetime.combine(
                local_now.date() + timedelta(days=1), time.min, timezone
            ).astimezone(UTC)
            used_today = await conn.fetchval(
                """
                SELECT count(*)
                FROM outreach_deliveries AS delivery
                JOIN outreach_campaigns AS campaign
                  ON campaign.run_id = delivery.campaign_run_id
                WHERE campaign.external_account_id = $1
                  AND delivery.status IN ('started', 'sent', 'unknown')
                  AND delivery.started_at >= $2
                  AND delivery.started_at < $3
                """,
                row["external_account_id"],
                day_start,
                day_end,
            )
            if int(used_today or 0) >= row["daily_send_cap"]:
                next_start = datetime.combine(
                    local_now.date() + timedelta(days=1),
                    row["send_window_start"],
                    timezone,
                )
                return await defer(max(1, math.ceil((next_start - local_now).total_seconds())))
            last_started_at = await conn.fetchval(
                """
                SELECT max(delivery.started_at)
                FROM outreach_deliveries AS delivery
                JOIN outreach_campaigns AS campaign
                  ON campaign.run_id = delivery.campaign_run_id
                WHERE campaign.external_account_id = $1
                  AND delivery.status IN ('started', 'sent', 'unknown')
                """,
                row["external_account_id"],
            )
            if last_started_at is not None:
                next_slot = last_started_at + timedelta(seconds=row["send_interval_seconds"])
                if current < next_slot:
                    return await defer(max(1, math.ceil((next_slot - current).total_seconds())))
            result = await conn.execute(
                """
                UPDATE outreach_deliveries
                SET status = 'started', scheduled_for = $2, started_at = $2,
                    error_code = NULL, updated_at = now()
                WHERE execution_key = $1 AND status = 'pending'
                """,
                row["execution_key"],
                current,
            )
            if result != "UPDATE 1":
                raise RuntimeError("outreach delivery reservation changed concurrently")
            await self._refresh_email_campaign_progress(conn, run_id=row["campaign_run_id"])
        return 0

    async def fail_outreach_delivery(
        self, *, execution_key: str, error_code: str, unknown: bool
    ) -> None:
        await self.pool.execute(
            """
            UPDATE outreach_deliveries
            SET status = $2, error_code = $3, updated_at = now()
            WHERE execution_key = $1 AND status <> 'sent'
            """,
            execution_key,
            "unknown" if unknown else "failed",
            error_code[:200],
        )

    async def record_email_reply_check(self, *, recipient_id: UUID, replied: bool) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            campaign_run_id = await conn.fetchval(
                """
                UPDATE outreach_recipients
                SET status = CASE WHEN $2 THEN 'replied' ELSE status END,
                    reply_checked_at = now(),
                    replied_at = CASE WHEN $2 THEN COALESCE(replied_at, now()) ELSE replied_at END,
                    updated_at = now()
                WHERE id = $1 AND status IN ('initial_sent', 'replied')
                RETURNING campaign_run_id
                """,
                recipient_id,
                replied,
            )
            if campaign_run_id is None:
                raise RuntimeError("email recipient is not waiting for a reply check")
            if replied:
                await conn.execute(
                    """
                    UPDATE outreach_deliveries
                    SET status = 'skipped', error_code = NULL, updated_at = now()
                    WHERE recipient_id = $1 AND step_key = 'follow_up' AND status = 'pending'
                    """,
                    recipient_id,
                )
            await self._refresh_email_campaign_progress(conn, run_id=campaign_run_id)

    async def complete_email_recipient(self, *, recipient_id: UUID) -> None:
        await self.pool.execute(
            """
            UPDATE outreach_recipients
            SET status = CASE
                    WHEN status IN ('replied', 'completed') THEN status
                    ELSE 'completed'
                END,
                updated_at = now()
            WHERE id = $1 AND status IN ('initial_sent', 'replied', 'follow_up_sent', 'completed')
            """,
            recipient_id,
        )

    async def fail_email_recipient(self, *, recipient_id: UUID, error_code: str) -> None:
        await self.pool.execute(
            """
            UPDATE outreach_recipients
            SET status = 'failed', error_code = $2, updated_at = now()
            WHERE id = $1 AND status NOT IN ('replied', 'follow_up_sent', 'completed', 'failed')
            """,
            recipient_id,
            error_code[:200],
        )

    async def complete_email_campaign(self, *, run_id: UUID) -> dict[str, Any]:
        async with self.pool.acquire() as conn, conn.transaction():
            counts = await conn.fetchrow(
                """
                SELECT count(delivery.execution_key) FILTER (
                           WHERE delivery.status = 'sent'
                       ) AS sent_count,
                       count(DISTINCT recipient.id) FILTER (
                           WHERE recipient.status = 'replied'
                       ) AS replied_count,
                       count(DISTINCT recipient.id) FILTER (
                           WHERE recipient.status = 'failed'
                       ) AS failed_count
                FROM outreach_recipients AS recipient
                LEFT JOIN outreach_deliveries AS delivery
                  ON delivery.recipient_id = recipient.id
                WHERE recipient.campaign_run_id = $1
                """,
                run_id,
            )
            assert counts is not None
            if counts["failed_count"]:
                raise RuntimeError("one or more email recipients failed")
            campaign = await conn.fetchrow(
                """
                UPDATE outreach_campaigns
                SET status = 'completed', sent_count = $2, replied_count = $3,
                    failed_count = $4,
                    completed_at = COALESCE(completed_at, now()), updated_at = now()
                WHERE run_id = $1 AND status IN ('approved', 'running', 'completed')
                RETURNING *
                """,
                run_id,
                counts["sent_count"],
                counts["replied_count"],
                counts["failed_count"],
            )
            if campaign is None:
                raise RuntimeError("email campaign cannot complete")
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'email_campaign_completed', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                campaign["project_id"],
                run_id,
                json.dumps(
                    {
                        "kind": "runs",
                        "recipient_count": campaign["recipient_count"],
                        "sent_count": counts["sent_count"],
                        "replied_count": counts["replied_count"],
                    }
                ),
                (
                    f"Email campaign delivered {counts['sent_count']} message(s) "
                    f"to {campaign['recipient_count']} recipient(s)."
                ),
                f"{run_id}:email_campaign_completed",
            )
        return dict(campaign)

    async def fail_email_campaign(self, *, run_id: UUID, error_code: str) -> None:
        await self.pool.execute(
            """
            UPDATE outreach_campaigns AS campaign
            SET status = 'failed',
                failed_count = (
                    SELECT count(*) FROM outreach_recipients AS recipient
                    WHERE recipient.campaign_run_id = campaign.run_id
                      AND recipient.status = 'failed'
                ),
                updated_at = now()
            WHERE run_id = $1 AND status NOT IN ('completed', 'stopped')
            """,
            run_id,
        )
        await self.project_failure(run_id=run_id, error_message=error_code)

    # ------------------------------------------------------------ Google Ads campaigns

    async def stop_paid_ads_monitor(
        self, *, run_id: UUID, project_id: UUID, actor: str
    ) -> WorkflowRun:
        return await self._stop_paid_report(
            run_id=run_id,
            project_id=project_id,
            actor=actor,
            workflow_key="ads.monitor",
        )

    async def create_paid_ads_campaign(
        self,
        *,
        run_id: UUID,
        project_id: UUID,
        connection_id: UUID | None,
        customer_id: str,
        mode: str,
        source_run_id: UUID,
        source_commit_sha: str,
        plan_path: str,
        plan_commit_sha: str,
        daily_budget_micros: int | None,
        cpc_ceiling_micros: int | None,
    ) -> dict[str, Any]:
        """Record the exact plan a launch run put up for approval; a replay must match it."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"paid-ads-campaign:{run_id}",
            )
            existing = await conn.fetchrow(
                "SELECT * FROM paid_ads_campaigns WHERE run_id = $1 FOR UPDATE", run_id
            )
            if existing is not None:
                if (
                    existing["project_id"] != project_id
                    or existing["customer_id"] != customer_id
                    or existing["mode"] != mode
                    or existing["source_run_id"] != source_run_id
                    or existing["plan_path"] != plan_path
                    or existing["plan_commit_sha"] != plan_commit_sha
                ):
                    raise SideEffectConflictError("paid ads campaign snapshot conflicts")
                return dict(existing)
            row = await conn.fetchrow(
                """
                INSERT INTO paid_ads_campaigns (
                    run_id, project_id, integration_connection_id, customer_id, mode,
                    source_run_id, source_commit_sha, plan_path, plan_commit_sha,
                    daily_budget_micros, cpc_ceiling_micros
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING *
                """,
                run_id,
                project_id,
                connection_id,
                customer_id,
                mode,
                source_run_id,
                source_commit_sha,
                plan_path,
                plan_commit_sha,
                daily_budget_micros,
                cpc_ceiling_micros,
            )
        assert row is not None
        return dict(row)

    async def get_paid_ads_campaign(self, run_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM paid_ads_campaigns WHERE run_id = $1", run_id)
        return dict(row) if row is not None else None

    async def list_paid_ads_campaigns(self, project_id: UUID) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM paid_ads_campaigns WHERE project_id = $1
            ORDER BY created_at DESC LIMIT 50
            """,
            project_id,
        )
        return [dict(row) for row in rows]

    async def approve_paid_ads_campaign(self, *, run_id: UUID) -> None:
        updated = await self.pool.fetchval(
            """
            UPDATE paid_ads_campaigns
            SET status = CASE WHEN status = 'draft' THEN 'approved' ELSE status END,
                approved_at = COALESCE(approved_at, now()), updated_at = now()
            WHERE run_id = $1 AND status IN ('draft', 'approved', 'creating', 'live')
            RETURNING true
            """,
            run_id,
        )
        if updated is None:
            raise RuntimeError("paid ads campaign is not awaiting approval")

    async def update_paid_ads_campaign(self, *, run_id: UUID, **fields: Any) -> None:
        allowed = {
            "status",
            "external_campaign_id",
            "external_budget_id",
            "external_shared_set_id",
            "enabled_at",
            "completed_at",
        }
        unknown = set(fields) - allowed
        if unknown or not fields:
            raise ValueError("unsupported paid ads campaign fields")
        await self.pool.execute(
            """
            UPDATE paid_ads_campaigns
            SET status = COALESCE($2, status),
                external_campaign_id = COALESCE($3, external_campaign_id),
                external_budget_id = COALESCE($4, external_budget_id),
                external_shared_set_id = COALESCE($5, external_shared_set_id),
                enabled_at = COALESCE($6, enabled_at),
                completed_at = COALESCE($7, completed_at),
                updated_at = now()
            WHERE run_id = $1
            """,
            run_id,
            fields.get("status"),
            fields.get("external_campaign_id"),
            fields.get("external_budget_id"),
            fields.get("external_shared_set_id"),
            fields.get("enabled_at"),
            fields.get("completed_at"),
        )

    async def complete_paid_ads_launch(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        campaign_status: str,
        canonical_commit_sha: str,
        artifact_path: str,
        artifact_ref: str,
        summary: str,
        event_type: str,
    ) -> None:
        """A review-pinned run finishes only after its approval; the campaign row follows."""
        if campaign_status not in {"live", "tracking", "setup"}:
            raise ValueError("unsupported paid ads campaign outcome")
        async with conn.transaction():
            projected = await conn.fetchval(
                """
                UPDATE workflow_runs
                SET status = 'succeeded', canonical_commit_sha = $2, artifact_ref = $3,
                    artifact_path = $4, result_summary = $5, error_message = NULL,
                    finished_at = COALESCE(finished_at, now()), progress_percent = 100,
                    progress_updated_at = now(), heartbeat_at = now()
                WHERE id = $1 AND executor = 'ads.launch'
                  AND (NOT review_required OR review_decision = 'approved')
                  AND status NOT IN ('failed', 'stopped', 'superseded')
                RETURNING id
                """,
                run_id,
                canonical_commit_sha,
                artifact_ref,
                artifact_path,
                summary[:1000],
            )
            if projected is None:
                raise SideEffectConflictError("launch cannot complete in its current state")
            await conn.execute(
                """
                UPDATE paid_ads_campaigns
                SET status = $2, completed_at = COALESCE(completed_at, now()), updated_at = now()
                WHERE run_id = $1 AND status NOT IN ('failed', 'stopped')
                """,
                run_id,
                campaign_status,
            )
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type=event_type,
                details={"kind": "runs", "status": "succeeded", "artifact_ref": artifact_ref},
                summary=summary,
                audience="product",
                dedupe_key=f"{execution_key}:{event_type}",
            )
            await self.complete_effect(
                conn, execution_key=execution_key, result={"artifact_ref": artifact_ref}
            )
        await self._track_run(run_id, "run_succeeded", artifact_path=artifact_path)

    async def fail_paid_ads_launch(
        self,
        *,
        run_id: UUID,
        message: str,
        canonical_commit_sha: str | None = None,
        artifact_path: str | None = None,
        artifact_ref: str | None = None,
    ) -> None:
        """A launch that cannot proceed ends failed with a founder-readable reason; when a
        setup note was published first it stays linked as the run's artifact."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'failed', error_message = $2,
                    canonical_commit_sha = COALESCE($3, canonical_commit_sha),
                    artifact_path = COALESCE($4, artifact_path),
                    artifact_ref = COALESCE($5, artifact_ref),
                    finished_at = COALESCE(finished_at, now()),
                    progress_updated_at = now(), heartbeat_at = now()
                WHERE id = $1 AND executor = 'ads.launch'
                  AND status NOT IN ('succeeded', 'stopped', 'superseded', 'failed')
                RETURNING *
                """,
                run_id,
                message[:2000],
                canonical_commit_sha,
                artifact_path,
                artifact_ref,
            )
            if row is None:
                return
            await conn.execute(
                """
                UPDATE paid_ads_campaigns SET status = 'failed', updated_at = now()
                WHERE run_id = $1 AND status NOT IN ('live', 'tracking', 'setup', 'stopped')
                """,
                run_id,
            )
            await conn.execute(
                """
                UPDATE run_decisions SET status = 'dismissed'
                WHERE run_id = $1 AND status = 'pending'
                """,
                run_id,
            )
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type="paid_ads_launch_failed",
                details={"kind": "runs", "status": "failed"},
                summary=message[:1000],
                audience="product",
                dedupe_key=f"paid_ads_launch:{run_id}:failed",
            )
        await self._track_run(run_id, "run_failed", error=message[:1000])

    async def stop_paid_ads_launch(
        self, *, run_id: UUID, project_id: UUID, actor: str
    ) -> WorkflowRun:
        """Stop before the campaign is enabled; afterwards the founder pauses it in Google Ads."""
        async with self.pool.acquire() as conn, self.project_state_lock(conn, project_id):
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM workflow_runs WHERE id=$1 FOR UPDATE", run_id
                )
                if (
                    row is None
                    or row["project_id"] != project_id
                    or row["executor"] != "ads.launch"
                ):
                    raise LookupError("run not found")
                if row["status"] == "stopped":
                    return _run(row)
                if row["status"] not in {"pending", "running", "needs_input"}:
                    raise SideEffectConflictError("This Google Ads launch has already finished.")
                enabling = await conn.fetchval(
                    "SELECT status FROM effect_receipts WHERE execution_key=$1",
                    f"paid_ads_launch:{run_id}:apply:enable",
                )
                if enabling:
                    raise SideEffectConflictError(
                        "The campaign is being switched on and can no longer be stopped here. "
                        "Pause it in Google Ads."
                    )
                row = await conn.fetchrow(
                    "UPDATE workflow_runs SET status='stopped', finished_at=now() "
                    "WHERE id=$1 RETURNING *",
                    run_id,
                )
                await conn.execute(
                    """
                    UPDATE paid_ads_campaigns SET status = 'stopped', updated_at = now()
                    WHERE run_id = $1 AND status NOT IN ('live', 'tracking', 'setup')
                    """,
                    run_id,
                )
                await conn.execute(
                    "UPDATE run_decisions SET status = 'dismissed' "
                    "WHERE run_id = $1 AND status = 'pending'",
                    run_id,
                )
                await self.add_activity(
                    conn=conn,
                    run_id=run_id,
                    event_type="paid_ads_launch_stopped",
                    details={
                        "kind": "your_edits",
                        "status": "stopped",
                        "actor_clerk_user_id": actor,
                    },
                    summary=(
                        "You stopped the Google Ads launch. Anything already created in "
                        "Google Ads stays paused there."
                    ),
                    audience="product",
                    dedupe_key=f"paid_ads_launch:{run_id}:stopped",
                )
                return _run(row)

    async def begin_paid_ads_proposal(
        self,
        *,
        proposal_id: UUID,
        monitor_run_id: UUID,
        campaign_run_id: UUID,
        project_id: UUID,
        request_id: UUID,
        kind: str,
        previous: dict[str, Any],
        proposed: dict[str, Any],
        rationale: str,
        review_path: str,
    ) -> dict[str, Any]:
        """One open proposal per campaign; the same request replays its row."""
        async with self.pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM paid_ads_proposals WHERE project_id = $1 AND request_id = $2",
                project_id,
                request_id,
            )
            if existing is not None:
                if existing["campaign_run_id"] != campaign_run_id or existing["kind"] != kind:
                    raise SideEffectConflictError("paid ads proposal request conflicts")
                return _proposal(existing)
            campaign = await conn.fetchrow(
                "SELECT * FROM paid_ads_campaigns WHERE run_id = $1 FOR UPDATE", campaign_run_id
            )
            if campaign is None or campaign["project_id"] != project_id:
                raise LookupError("paid ads campaign not found")
            if campaign["status"] != "live":
                raise RuntimeError("proposals need a live campaign")
            open_row = await conn.fetchrow(
                """
                SELECT id FROM paid_ads_proposals
                WHERE campaign_run_id = $1 AND status IN ('pending', 'approved')
                """,
                campaign_run_id,
            )
            if open_row is not None:
                raise RuntimeError("the campaign already has a proposal awaiting you")
            number = int(
                await conn.fetchval(
                    "SELECT COALESCE(max(proposal_number), 0) + 1 FROM paid_ads_proposals "
                    "WHERE campaign_run_id = $1",
                    campaign_run_id,
                )
            )
            row = await conn.fetchrow(
                """
                INSERT INTO paid_ads_proposals (
                    id, monitor_run_id, project_id, campaign_run_id, request_id,
                    proposal_number, kind, previous, proposed, rationale, review_path
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11)
                RETURNING *
                """,
                proposal_id,
                monitor_run_id,
                project_id,
                campaign_run_id,
                request_id,
                number,
                kind,
                json.dumps(previous),
                json.dumps(proposed),
                rationale[:4000],
                review_path,
            )
        assert row is not None
        return _proposal(row)

    async def finalize_paid_ads_proposal(
        self,
        *,
        proposal_id: UUID,
        review_commit_sha: str,
        artifact_ref: str,
        review_path: str | None = None,
    ) -> dict[str, Any]:
        """The numbered document path is known only after the row took its number."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE paid_ads_proposals
                SET review_commit_sha = COALESCE(review_commit_sha, $2),
                    review_path = COALESCE($3, review_path)
                WHERE id = $1
                RETURNING *
                """,
                proposal_id,
                review_commit_sha,
                review_path,
            )
            if row is None:
                raise LookupError("paid ads proposal not found")
            proposal = _proposal(row)
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'paid_ads_proposal_ready', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                proposal["project_id"],
                proposal["monitor_run_id"],
                json.dumps(
                    {
                        "kind": "needs_you",
                        "artifact_ref": artifact_ref,
                        "proposal_id": str(proposal_id),
                        "proposal_kind": proposal["kind"],
                        "external_label": "Review proposal",
                    }
                ),
                _proposal_summary(proposal),
                f"paid_ads_proposal:{proposal_id}:ready",
            )
        return proposal

    async def get_paid_ads_proposal(self, proposal_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM paid_ads_proposals WHERE id = $1", proposal_id
        )
        return _proposal(row) if row is not None else None

    async def list_paid_ads_proposals(
        self, *, project_id: UUID, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM paid_ads_proposals
            WHERE project_id = $1 AND ($2::text IS NULL OR status = $2)
            ORDER BY requested_at DESC LIMIT 100
            """,
            project_id,
            status,
        )
        return [_proposal(row) for row in rows]

    async def review_paid_ads_proposal(
        self, *, proposal_id: UUID, decision: str, clerk_user_id: str
    ) -> dict[str, Any]:
        """pending → approved | discarded, once; replays return the row unchanged."""
        if decision not in {"approved", "discarded"}:
            raise ValueError("unsupported proposal decision")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM paid_ads_proposals WHERE id = $1 FOR UPDATE", proposal_id
            )
            if row is None:
                raise LookupError("paid ads proposal not found")
            if row["status"] == decision or (
                decision == "approved" and row["status"] in {"applied", "unknown", "failed"}
            ):
                return _proposal(row)
            if row["status"] != "pending":
                raise RuntimeError("this proposal has already been decided")
            row = await conn.fetchrow(
                """
                UPDATE paid_ads_proposals
                SET status = $2, reviewed_by_clerk_user_id = $3, reviewed_at = now()
                WHERE id = $1
                RETURNING *
                """,
                proposal_id,
                decision,
                clerk_user_id,
            )
            assert row is not None
            proposal = _proposal(row)
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, 'product', $6)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                proposal["project_id"],
                proposal["monitor_run_id"],
                f"paid_ads_proposal_{decision}",
                json.dumps(
                    {
                        "kind": "your_edits",
                        "proposal_id": str(proposal_id),
                        "actor_clerk_user_id": clerk_user_id,
                    }
                ),
                (
                    "You approved the Google Ads change; Tin is applying it."
                    if decision == "approved"
                    else "You set the Google Ads proposal aside. Nothing changed."
                ),
                f"paid_ads_proposal:{proposal_id}:{decision}",
            )
        return proposal

    async def settle_paid_ads_proposal(
        self, *, proposal_id: UUID, status: str, error_code: str | None = None
    ) -> dict[str, Any]:
        if status not in {"applied", "unknown", "failed"}:
            raise ValueError("unsupported proposal settlement")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE paid_ads_proposals
                SET status = $2, error_code = $3, settled_at = COALESCE(settled_at, now())
                WHERE id = $1 AND status IN ('approved', 'unknown')
                RETURNING *
                """,
                proposal_id,
                status,
                error_code[:120] if error_code else None,
            )
            if row is None:
                current = await conn.fetchrow(
                    "SELECT * FROM paid_ads_proposals WHERE id = $1", proposal_id
                )
                if current is None:
                    raise LookupError("paid ads proposal not found")
                return _proposal(current)
            proposal = _proposal(row)
            if status == "applied":
                await conn.execute(
                    """
                    INSERT INTO activity_events (
                        project_id, run_id, event_type, details, summary, audience, dedupe_key
                    )
                    VALUES ($1, $2, 'paid_ads_proposal_applied', $3::jsonb, $4, 'product', $5)
                    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                    """,
                    proposal["project_id"],
                    proposal["monitor_run_id"],
                    json.dumps({"kind": "runs", "proposal_id": str(proposal_id)}),
                    f"Applied in Google Ads: {_proposal_summary(proposal)}",
                    f"paid_ads_proposal:{proposal_id}:applied",
                )
        return proposal

    async def stop_email_campaign(self, *, run_id: UUID) -> WorkflowRun:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'stopped', finished_at = COALESCE(finished_at, now()),
                    error_message = NULL
                WHERE id = $1
                  AND executor = 'outreach.email_campaign'
                  AND status IN ('pending', 'running', 'needs_input', 'paused', 'stopped')
                RETURNING *
                """,
                run_id,
            )
            if row is None:
                raise RuntimeError("email campaign cannot be stopped")
            await conn.execute(
                """
                UPDATE outreach_campaigns
                SET status = 'stopped', updated_at = now()
                WHERE run_id = $1 AND status NOT IN ('completed', 'failed', 'stopped')
                """,
                run_id,
            )
            await conn.execute(
                """
                UPDATE outreach_campaign_revisions
                SET status = 'discarded', reviewed_at = COALESCE(reviewed_at, now())
                WHERE campaign_run_id = $1 AND status = 'pending'
                """,
                run_id,
            )
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'email_campaign_stopped', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                row["project_id"],
                run_id,
                json.dumps({"kind": "your_edits", "status": "stopped"}),
                "You stopped the email campaign. No further messages will be started.",
                f"{run_id}:email_campaign_stopped",
            )
        return _run(row)

    async def stop_organic_audit(
        self, *, run_id: UUID, project_id: UUID, actor: str
    ) -> WorkflowRun:
        return await self._stop_paid_report(
            run_id=run_id, project_id=project_id, actor=actor, workflow_key="organic.audit"
        )

    async def stop_keyword_plan(self, *, run_id: UUID, project_id: UUID, actor: str) -> WorkflowRun:
        return await self._stop_paid_report(
            run_id=run_id, project_id=project_id, actor=actor, workflow_key="organic.keyword_plan"
        )

    async def stop_paid_ads_assessment(
        self, *, run_id: UUID, project_id: UUID, actor: str
    ) -> WorkflowRun:
        return await self._stop_paid_report(
            run_id=run_id,
            project_id=project_id,
            actor=actor,
            workflow_key="ads.assessment",
        )

    async def _stop_paid_report(
        self, *, run_id: UUID, project_id: UUID, actor: str, workflow_key: str
    ) -> WorkflowRun:
        prefix, event, label = {
            "organic.traffic_system": (
                "traffic",
                "organic_system_stopped",
                "organic traffic system",
            ),
            "content.plan": ("content", "content_plan_stopped", "content plan"),
            "organic.audit": ("organic", "organic_audit_stopped", "audit"),
            "organic.keyword_plan": ("keyword", "keyword_plan_stopped", "keyword plan"),
            "ads.assessment": (
                "paid_ads",
                "paid_ads_assessment_stopped",
                "paid ads assessment",
            ),
            "ads.monitor": (
                "paid_ads_monitor",
                "paid_ads_monitor_stopped",
                "Google Ads check",
            ),
        }[workflow_key]
        async with self.pool.acquire() as conn, self.project_state_lock(conn, project_id):
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM workflow_runs WHERE id=$1 FOR UPDATE", run_id
                )
                if (
                    row is None
                    or row["project_id"] != project_id
                    or row["executor"] != workflow_key
                ):
                    raise LookupError("run not found")
                if row["status"] == "stopped":
                    return _run(row)
                if row["status"] not in {"pending", "running"}:
                    raise SideEffectConflictError(f"This {label} has already finished.")
                publication = await conn.fetchval(
                    "SELECT result FROM effect_receipts WHERE execution_key=$1",
                    f"{prefix}:{run_id}:publish",
                )
                if publication:
                    raise SideEffectConflictError(
                        f"The {label} is publishing its report and can no longer be stopped."
                    )
                row = await conn.fetchrow(
                    "UPDATE workflow_runs SET status='stopped', finished_at=now() "
                    "WHERE id=$1 RETURNING *",
                    run_id,
                )
                await self.add_activity(
                    conn=conn,
                    run_id=run_id,
                    event_type=event,
                    details={
                        "kind": "your_edits",
                        "status": "stopped",
                        "actor_clerk_user_id": actor,
                    },
                    summary=(
                        f"You stopped the {label}. "
                        "Accepted provider requests may still incur costs."
                    ),
                    audience="product",
                    dedupe_key=f"{prefix}:{run_id}:stopped",
                )
                return _run(row)

    async def stop_technical_fix(self, *, run_id: UUID, project_id: UUID, actor: str):
        return await self.stop_codex_procedure(
            run_id=run_id,
            project_id=project_id,
            actor=actor,
            workflow_key="organic.technical_fix",
        )

    async def stop_codex_procedure(
        self,
        *,
        run_id: UUID,
        project_id: UUID,
        actor: str,
        expected_generation: int | None = None,
        workflow_key: str | None = None,
    ) -> WorkflowRun:
        # Do not wait behind an external write and then pretend it was stopped.
        key = f"{run_id}:procedure_canonical_commit"
        # Serialize duplicates on this same connection; nesting a second pool
        # checkout here can deadlock when many members stop work together.
        async with self.effect_lock(f"{run_id}:procedure_stop", "procedure_stop") as (conn, _):
            locked = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
            )
            if not locked:
                raise SideEffectConflictError(
                    "Publication or delivery is in progress; it cannot be recalled."
                )
            try:
                async with self.project_state_lock(conn, project_id), conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT r.*, w.key AS workflow_key FROM workflow_runs r "
                        "JOIN workflows w ON w.id=r.workflow_id "
                        "WHERE r.id=$1 AND r.project_id=$2 "
                        "AND r.executor IN ('codex.procedure', 'workflow.code') "
                        "AND ($3::text IS NULL OR w.key=$3) "
                        "FOR UPDATE OF r",
                        run_id,
                        project_id,
                        workflow_key,
                    )
                    if row is None:
                        raise LookupError("run not found")
                    if expected_generation is not None and row["generation"] != expected_generation:
                        raise SideEffectConflictError("The run generation has changed.")
                    if row["status"] == "stopped":
                        return _run(row)
                    if row["status"] not in {"pending", "running"}:
                        raise SideEffectConflictError(
                            "The procedure finished or is already waiting for review."
                        )
                    publication = await self.get_effect(key, conn=conn)
                    receipt = await self.get_effect(f"technical:{run_id}:publish", conn=conn)
                    delivery = await conn.fetchval(
                        "SELECT true FROM integration_call_receipts WHERE execution_key=$1",
                        f"{run_id}:procedure_pull_request",
                    )
                    if (
                        row["canonical_commit_sha"] is not None
                        or publication is not None
                        or (receipt and receipt.result)
                        or delivery
                    ):
                        raise SideEffectConflictError(
                            "Publication or delivery has started. Its result must be reconciled, "
                            "not recalled."
                        )
                    technical = row["workflow_key"] == "organic.technical_fix"
                    code = row["executor"] == "workflow.code"
                    row = await conn.fetchrow(
                        "UPDATE workflow_runs SET status='stopped', finished_at=now(), "
                        "lease_active=false, lease_released_at=COALESCE(lease_released_at,now()) "
                        "WHERE id=$1 RETURNING *",
                        run_id,
                    )
                    # Revoke fetch and native-refresh writeback together with the stop.
                    # Minting a replacement grant takes the same run row lock below.
                    await conn.execute("DELETE FROM broker_grants WHERE run_id=$1", run_id)
                    await self.add_activity(
                        conn=conn,
                        run_id=run_id,
                        event_type=(
                            "technical_fix_stopped"
                            if technical
                            else "code_workflow_stopped"
                            if code
                            else "codex_procedure_stopped"
                        ),
                        details={
                            "kind": "your_edits",
                            "status": "stopped",
                            "actor_clerk_user_id": actor,
                        },
                        summary=(
                            "You stopped the technical repair. No new PR will be delivered."
                            if technical
                            else "You stopped the code workflow. Saved output remains available."
                            if code
                            else "You stopped the procedure. Saved output remains available; "
                            "already accepted provider work may still incur costs."
                        ),
                        audience="product",
                        dedupe_key=f"{'technical' if technical else 'procedure'}:{run_id}:stopped",
                    )
                    return _run(row)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)

    async def mark_run_running(self, run_id: UUID) -> None:
        await self.pool.execute(
            """
            UPDATE workflow_runs
            SET status = 'running', started_at = COALESCE(started_at, now()),
                heartbeat_at = now(), error_message = NULL
            WHERE id = $1 AND status IN ('pending', 'running')
            """,
            run_id,
        )
        await self._track_run(run_id, "run_running")

    async def project_run_progress(
        self,
        *,
        run_id: UUID,
        mode: str,
        step: str | None = None,
        current: int | None = None,
        total: int | None = None,
        percent: int | None = None,
        summary: str | None = None,
    ) -> bool:
        if mode not in {"steps", "units", "indeterminate"}:
            raise ValueError("run progress mode is invalid")
        if (current is None) != (total is None):
            raise ValueError("run progress current and total must be reported together")
        if current is not None and (current < 0 or total is None or total <= 0 or current > total):
            raise ValueError("run progress counts are invalid")
        if percent is not None and not 0 <= percent <= 100:
            raise ValueError("run progress percent is invalid")
        if summary is not None and len(summary) > 240:
            raise ValueError("run progress summary is too long")
        updated = await self.pool.fetchval(
            """
            UPDATE workflow_runs
            SET progress_mode = $2, progress_step = $3,
                progress_current = $4, progress_total = $5,
                progress_percent = $6, progress_summary = $7,
                progress_updated_at = now(), heartbeat_at = now()
            WHERE id = $1 AND status IN ('pending', 'running')
            RETURNING true
            """,
            run_id,
            mode,
            step,
            current,
            total,
            percent,
            summary,
        )
        return bool(updated)

    async def project_run_narration(self, *, run_id: UUID, summary: str) -> bool:
        """Show the agent's latest progress line unless product code owns the steps."""
        summary = " ".join(summary.split())
        if not summary:
            return False
        if len(summary) > 240:
            summary = summary[:239].rstrip() + "…"
        updated = await self.pool.fetchval(
            """
            UPDATE workflow_runs
            SET progress_summary = $2, progress_updated_at = now()
            WHERE id = $1 AND status IN ('pending', 'running') AND progress_step IS NULL
            RETURNING true
            """,
            run_id,
            summary,
        )
        return bool(updated)

    async def refresh_email_campaign_progress(self, *, run_id: UUID) -> bool:
        async with self.pool.acquire() as conn:
            return await self._refresh_email_campaign_progress(conn, run_id=run_id)

    async def _refresh_email_campaign_progress(
        self, conn: asyncpg.Connection, *, run_id: UUID
    ) -> bool:
        progress = await conn.fetchrow(
            """
            SELECT campaign.status, campaign.send_timezone,
                   count(delivery.execution_key) AS total,
                   count(delivery.execution_key) FILTER (
                       WHERE delivery.status IN ('sent', 'skipped')
                   ) AS resolved,
                   count(delivery.execution_key) FILTER (
                       WHERE delivery.status = 'started'
                          OR (
                              delivery.status = 'pending'
                              AND (
                                  delivery.scheduled_for IS NULL
                                  OR delivery.scheduled_for <= now()
                              )
                          )
                   ) AS ready,
                   min(delivery.scheduled_for) FILTER (
                       WHERE delivery.status = 'pending'
                         AND delivery.scheduled_for > now()
                   ) AS next_delivery_at
            FROM outreach_campaigns AS campaign
            JOIN outreach_deliveries AS delivery
              ON delivery.campaign_run_id = campaign.run_id
            WHERE campaign.run_id = $1
            GROUP BY campaign.status, campaign.send_timezone
            """,
            run_id,
        )
        if progress is None:
            return False
        total = int(progress["total"])
        resolved = int(progress["resolved"])
        if total <= 0:
            return False
        step, summary = _email_campaign_progress_text(
            campaign_status=progress["status"],
            total=total,
            resolved=resolved,
            ready=int(progress["ready"]),
            next_delivery_at=progress["next_delivery_at"],
            send_timezone=progress["send_timezone"],
        )
        updated = await conn.fetchval(
            """
            UPDATE workflow_runs
            SET progress_mode = 'units', progress_step = $2,
                progress_current = $3, progress_total = $4,
                progress_percent = $5, progress_summary = $6,
                progress_updated_at = now(), heartbeat_at = now()
            WHERE id = $1 AND status IN ('pending', 'running', 'needs_input')
            RETURNING true
            """,
            run_id,
            step,
            resolved,
            total,
            (resolved * 100) // total,
            summary,
        )
        return bool(updated)

    async def attach_sandbox(
        self,
        *,
        run_id: UUID,
        sandbox_id: str,
        lease_owner: str,
        expected_head_sha: str,
        ephemeral_branch: str,
        require_active: bool = False,
    ) -> WorkflowRun:
        async with self.pool.acquire() as conn, conn.transaction():
            run = await conn.fetchrow("SELECT * FROM workflow_runs WHERE id = $1", run_id)
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{run['project_id']}:{run['thread_id']}",
            )
            run = await conn.fetchrow(
                "SELECT * FROM workflow_runs WHERE id = $1 FOR UPDATE", run_id
            )
            assert run is not None
            await conn.execute(
                """
                UPDATE workflow_runs
                SET lease_active = false,
                    lease_released_at = COALESCE(lease_released_at, now())
                WHERE project_id = $1
                  AND thread_id = $2
                  AND lease_active = true
                  AND id <> $3
                """,
                run["project_id"],
                run["thread_id"],
                run_id,
            )
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET lease_owner = $2,
                    sandbox_id = $3,
                    lease_active = true,
                    lease_released_at = NULL,
                    ephemeral_branch = $4,
                    expected_head_sha = $5
                WHERE id = $1
                  AND (lease_owner IS NULL OR lease_owner = $2)
                  AND ((NOT $6::boolean AND executor <> 'codex.procedure')
                       OR status IN ('pending', 'running'))
                RETURNING *
                """,
                run_id,
                lease_owner,
                sandbox_id,
                ephemeral_branch,
                expected_head_sha,
                require_active,
            )
            if row is None:
                raise RuntimeError("generation lease is owned by a different execution")
        return _run(row)

    async def validate_lease(
        self,
        *,
        project_id: UUID,
        thread_id: str,
        generation: int,
        lease_owner: str,
        fencing_token: int,
        sandbox_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        return bool(
            await (conn or self.pool).fetchval(
                """
                SELECT true
                FROM workflow_runs
                WHERE project_id = $1
                  AND thread_id = $2
                  AND generation = $3
                  AND lease_owner = $4
                  AND fencing_token = $5
                  AND sandbox_id = $6
                  AND lease_active = true
                """,
                project_id,
                thread_id,
                generation,
                lease_owner,
                fencing_token,
                sandbox_id,
            )
        )

    async def release_lease(self, run_id: UUID) -> None:
        await self.pool.execute(
            """
            UPDATE workflow_runs
            SET lease_active = false,
                lease_released_at = COALESCE(lease_released_at, now())
            WHERE id = $1 AND lease_active = true
            """,
            run_id,
        )

    async def create_run_tool_grant(
        self,
        *,
        run_id: UUID,
        sandbox_id: str,
        token: str,
        connection_id: UUID | None,
        external_account_id: str,
        provider_key: str,
        capabilities: tuple[str, ...],
        ttl_seconds: int,
    ) -> None:
        if not capabilities:
            raise ValueError("run tool grants require at least one capability")
        if (connection_id is None) != (provider_key in {"tin.studio", "tin.services"}):
            raise ValueError("only internal tool grants may omit an integration connection")
        token_hash = _token_hash(token)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        result = await self.pool.execute(
            """
            INSERT INTO run_tool_grants (
                token_hash, project_id, run_id, connection_id, external_account_id,
                sandbox_id, generation,
                fencing_token, provider_key, capabilities, expires_at
            )
            SELECT $1, project_id, id, $4, $5, sandbox_id, generation,
                   fencing_token, $6, $7::jsonb, $8
            FROM workflow_runs
            WHERE id = $2
              AND sandbox_id = $3
              AND lease_active = true
            ON CONFLICT (token_hash) DO NOTHING
            """,
            token_hash,
            run_id,
            sandbox_id,
            connection_id,
            external_account_id,
            provider_key,
            json.dumps(list(capabilities)),
            expires_at,
        )
        if result != "INSERT 0 1":
            raise RuntimeError("could not bind tools to the active procedure lease")

    async def authorize_run_tool_grant(
        self,
        *,
        token: str,
        capability: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> RunToolGrant | None:
        if conn is None:
            async with self.pool.acquire() as acquired:
                return await self.authorize_run_tool_grant(
                    token=token, capability=capability, conn=acquired
                )
        token_hash = _token_hash(token)
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT grant_row.project_id, grant_row.run_id, grant_row.connection_id,
                       grant_row.external_account_id, grant_row.sandbox_id,
                       grant_row.provider_key, grant_row.capabilities, grant_row.expires_at
                FROM run_tool_grants AS grant_row
                JOIN workflow_runs AS run ON run.id = grant_row.run_id
                WHERE grant_row.token_hash = $1
                  AND grant_row.expires_at > now()
                  AND run.project_id = grant_row.project_id
                  AND run.sandbox_id = grant_row.sandbox_id
                  AND run.generation = grant_row.generation
                  AND run.fencing_token = grant_row.fencing_token
                  AND run.lease_active = true
                FOR UPDATE OF grant_row
                """,
                token_hash,
            )
            if row is None:
                return None
            capabilities = tuple(
                _json_list(row["capabilities"], field="run tool grant capabilities")
            )
            if capability is not None and capability not in capabilities:
                return None
            await conn.execute(
                """
                UPDATE run_tool_grants
                SET last_used_at = now()
                WHERE token_hash = $1
                """,
                token_hash,
            )
        return RunToolGrant(
            project_id=row["project_id"],
            run_id=row["run_id"],
            connection_id=row["connection_id"],
            external_account_id=row["external_account_id"],
            sandbox_id=row["sandbox_id"],
            provider_key=row["provider_key"],
            capabilities=capabilities,
            expires_at=row["expires_at"],
        )

    async def studio_voice_usage(self, run_id: UUID, *, conn=None) -> StudioUsage:
        """Count attempted lines, including ambiguous/failed purchases, against the quota."""
        row = await (conn or self.pool).fetchrow(
            """
            SELECT count(*) AS lines,
                   COALESCE(sum((result->>'characters')::int), 0) AS characters
            FROM effect_receipts
            WHERE operation = 'studio_voice'
              AND execution_key LIKE $1
            """,
            f"{run_id}:studio-voice:%",
        )
        return StudioUsage(lines=int(row["lines"]), characters=int(row["characters"]))

    async def create_test_identity(
        self,
        *,
        identity_id: UUID,
        project_id: UUID,
        run_id: UUID,
        target_host: str,
        label: str,
        email: str,
        auth_kind: str,
        password_ciphertext: bytes | None,
        credential_key_version: str | None,
        phone_number: str | None = None,
    ) -> ProjectTestIdentity:
        """Create the one test identity a run owns; a retry returns the existing row."""
        if auth_kind not in {"password", "magic_link", "otp"}:
            raise ValueError("test identity auth kind is unsupported")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO project_test_identities (
                    id, project_id, created_by_run_id, target_host, label, email,
                    auth_kind, password_ciphertext, credential_key_version, phone_number
                )
                SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
                WHERE EXISTS (
                    SELECT 1 FROM workflow_runs WHERE id = $3 AND project_id = $2
                )
                ON CONFLICT (created_by_run_id) DO NOTHING
                """,
                identity_id,
                project_id,
                run_id,
                target_host,
                label,
                email,
                auth_kind,
                password_ciphertext,
                credential_key_version,
                phone_number,
            )
            row = await conn.fetchrow(
                "SELECT * FROM project_test_identities WHERE created_by_run_id = $1",
                run_id,
            )
            if row is not None:
                await conn.execute(
                    """
                    INSERT INTO project_test_identity_uses (run_id, identity_id, mode)
                    VALUES ($1, $2, 'created')
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    run_id,
                    row["id"],
                )
        if row is None:
            raise RuntimeError("could not bind a test identity to the workflow run")
        return _project_test_identity(row)

    async def record_test_identity_sms(
        self, *, message_sid: str, to_number: str, from_number: str, body: str
    ) -> bool:
        """Store one inbound SMS to a Tin test phone number; a redelivery is a no-op."""
        result = await self.pool.execute(
            """
            INSERT INTO test_identity_sms_messages (id, message_sid, to_number, from_number, body)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (message_sid) DO NOTHING
            """,
            uuid4(),
            message_sid[:64],
            to_number,
            from_number[:32],
            body[:2000],
        )
        return result.endswith("1")

    async def list_test_identity_sms(
        self, *, to_number: str, since: datetime, limit: int
    ) -> list[TestIdentitySms]:
        """Messages a Tin test phone number received since `since`, newest first."""
        rows = await self.pool.fetch(
            """
            SELECT id, message_sid, to_number, from_number, body, received_at
            FROM test_identity_sms_messages
            WHERE to_number = $1 AND received_at >= $2
            ORDER BY received_at DESC
            LIMIT $3
            """,
            to_number,
            since,
            max(1, min(int(limit), 50)),
        )
        return [
            TestIdentitySms(
                id=row["id"],
                message_sid=row["message_sid"],
                to_number=row["to_number"],
                from_number=row["from_number"],
                body=row["body"],
                received_at=row["received_at"],
            )
            for row in rows
        ]

    async def get_test_identity_for_run(self, *, run_id: UUID) -> ProjectTestIdentity | None:
        """The identity a run created or reused, resolved through its recorded use."""
        row = await self.pool.fetchrow(
            """
            SELECT identity.*
            FROM project_test_identity_uses AS used
            JOIN project_test_identities AS identity ON identity.id = used.identity_id
            WHERE used.run_id = $1
            """,
            run_id,
        )
        if row is None:
            row = await self.pool.fetchrow(
                "SELECT * FROM project_test_identities WHERE created_by_run_id = $1",
                run_id,
            )
        return _project_test_identity(row) if row else None

    async def get_test_identity_use_mode(self, *, run_id: UUID) -> str | None:
        mode = await self.pool.fetchval(
            "SELECT mode FROM project_test_identity_uses WHERE run_id = $1",
            run_id,
        )
        if mode is None:
            exists = await self.pool.fetchval(
                "SELECT true FROM project_test_identities WHERE created_by_run_id = $1",
                run_id,
            )
            return "created" if exists else None
        return str(mode)

    async def find_reusable_test_identity(
        self, *, project_id: UUID, target_host: str
    ) -> ProjectTestIdentity | None:
        """The project's newest active password identity on one product host."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM project_test_identities
            WHERE project_id = $1
              AND target_host = $2
              AND status = 'active'
              AND auth_kind = 'password'
              AND password_ciphertext IS NOT NULL
            ORDER BY COALESCE(verified_at, created_at) DESC, created_at DESC
            LIMIT 1
            """,
            project_id,
            target_host,
        )
        return _project_test_identity(row) if row else None

    async def bind_test_identity_to_run(
        self, *, run_id: UUID, identity_id: UUID, project_id: UUID
    ) -> ProjectTestIdentity:
        """Record that a run reuses an earlier run's active identity; retries are idempotent."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO project_test_identity_uses (run_id, identity_id, mode)
                SELECT $1::uuid, $2::uuid, 'reused'
                WHERE EXISTS (
                    SELECT 1 FROM workflow_runs WHERE id = $1::uuid AND project_id = $3::uuid
                ) AND EXISTS (
                    SELECT 1 FROM project_test_identities
                    WHERE id = $2::uuid AND project_id = $3::uuid AND status = 'active'
                )
                ON CONFLICT (run_id) DO NOTHING
                """,
                run_id,
                identity_id,
                project_id,
            )
            row = await conn.fetchrow(
                """
                UPDATE project_test_identities AS identity
                SET last_used_run_id = $1::uuid, last_used_at = now(), updated_at = now()
                FROM project_test_identity_uses AS used
                WHERE used.run_id = $1::uuid
                  AND used.identity_id = identity.id
                  AND identity.id = $2::uuid
                  AND identity.project_id = $3::uuid
                RETURNING identity.*
                """,
                run_id,
                identity_id,
                project_id,
            )
        if row is None:
            raise RuntimeError("could not bind the reused test identity to the workflow run")
        return _project_test_identity(row)

    async def list_test_identities(
        self,
        *,
        project_id: UUID,
        target_host: str | None = None,
    ) -> list[ProjectTestIdentity]:
        if target_host is None:
            rows = await self.pool.fetch(
                """
                SELECT * FROM project_test_identities
                WHERE project_id = $1
                ORDER BY created_at DESC
                """,
                project_id,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT * FROM project_test_identities
                WHERE project_id = $1 AND target_host = $2
                ORDER BY created_at DESC
                """,
                project_id,
                target_host,
            )
        return [_project_test_identity(row) for row in rows]

    async def update_test_identity_status(
        self,
        *,
        run_id: UUID,
        status: str,
        note: str | None,
    ) -> ProjectTestIdentity:
        """Record the run's verdict for the identity it created or reused.

        A run that created its identity may move it from pending or blocked to active or blocked;
        repeating the same verdict is idempotent and a later attempt may upgrade `blocked` to
        `active`. A run that reused an earlier run's active identity may confirm it `active`
        again or report it `blocked` when login fails, and never marks it `failed`. An `active`
        identity never regresses through a creating run and a `failed` or `retired` one never
        changes.
        """
        if status not in {"active", "blocked", "failed"}:
            raise ValueError("test identity status transition is unsupported")
        mode = await self.get_test_identity_use_mode(run_id=run_id)
        if mode is None:
            raise LookupError("no test identity for this run")
        if mode == "reused":
            if status == "failed":
                raise LookupError("a reused test identity is never failed by a later run")
            row = await self.pool.fetchrow(
                """
                UPDATE project_test_identities AS identity
                SET status = $2,
                    status_note = $3,
                    verified_at = CASE WHEN $2 = 'active' THEN now() ELSE verified_at END,
                    updated_at = now()
                FROM project_test_identity_uses AS used
                WHERE used.run_id = $1
                  AND used.identity_id = identity.id
                  AND identity.status IN ('active', 'blocked')
                RETURNING identity.*
                """,
                run_id,
                status,
                (note or "")[:2000] or None,
            )
        else:
            row = await self.pool.fetchrow(
                """
                UPDATE project_test_identities
                SET status = $2,
                    status_note = $3,
                    verified_at = CASE WHEN $2 = 'active' THEN now() ELSE verified_at END,
                    updated_at = now()
                WHERE created_by_run_id = $1
                  AND status IN ('pending', 'blocked')
                  AND NOT (status = 'blocked' AND $2 = 'failed')
                RETURNING *
                """,
                run_id,
                status,
                (note or "")[:2000] or None,
            )
        if row is not None:
            return _project_test_identity(row)
        existing = await self.get_test_identity_for_run(run_id=run_id)
        if existing is None:
            raise LookupError("no test identity for this run")
        if existing.status == status:
            return existing
        raise LookupError(f"test identity is already {existing.status}")

    async def finalize_pending_test_identity(
        self,
        *,
        run_id: UUID,
        status: str,
        note: str,
    ) -> bool:
        """Host-side fallback used when a run ends without a status report."""
        if status not in {"blocked", "failed"}:
            raise ValueError("test identity fallback status is unsupported")
        result = await self.pool.execute(
            """
            UPDATE project_test_identities
            SET status = $2, status_note = $3, updated_at = now()
            WHERE created_by_run_id = $1
              AND status = 'pending'
            """,
            run_id,
            status,
            note[:2000],
        )
        return result == "UPDATE 1"

    @asynccontextmanager
    async def effect_lock(
        self, execution_key: str, operation: str, *, conn: asyncpg.Connection | None = None
    ) -> AsyncIterator[tuple[asyncpg.Connection, EffectReceipt | None]]:
        if conn is None:
            async with self.pool.acquire() as acquired:
                async with self.effect_lock(execution_key, operation, conn=acquired) as locked:
                    yield locked
        else:
            await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", execution_key)
            try:
                row = await conn.fetchrow(
                    "SELECT * FROM effect_receipts WHERE execution_key = $1", execution_key
                )
                receipt = _effect_receipt(row) if row else None
                if receipt is not None and receipt.operation != operation:
                    raise SideEffectConflictError(
                        f"{execution_key} belongs to {receipt.operation}, not {operation}"
                    )
                with effect_connection(self, conn):
                    yield conn, receipt
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", execution_key
                )

    @asynccontextmanager
    async def project_state_lock(
        self, conn: asyncpg.Connection, project_id: UUID
    ) -> AsyncIterator[None]:
        lock_key = f"project-state:{project_id}"
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", lock_key)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key)

    # Project deletion: the tombstone, the fence, and the purge. The orchestrator in
    # project_deletion.py owns the transaction and lock boundaries; these are plain steps.

    async def get_project_for_deletion(
        self, conn: asyncpg.Connection, project_id: UUID
    ) -> asyncpg.Record | None:
        return await conn.fetchrow(
            """
            SELECT id, name, state_repo_id, created_by_clerk_user_id,
                   deleted_at, deleted_by_clerk_user_id
            FROM projects WHERE id = $1 FOR UPDATE
            """,
            project_id,
        )

    async def has_project_membership(
        self, conn: asyncpg.Connection, *, project_id: UUID, clerk_user_id: str
    ) -> bool:
        """Raw membership, tombstone or not; only deletion authorization reads it."""
        return bool(
            await conn.fetchval(
                "SELECT true FROM project_memberships WHERE project_id = $1 AND clerk_user_id = $2",
                project_id,
                clerk_user_id,
            )
        )

    async def tombstone_project(
        self, conn: asyncpg.Connection, *, project_id: UUID, actor: str
    ) -> datetime:
        return await conn.fetchval(
            """
            UPDATE projects
            SET deleted_at = COALESCE(deleted_at, now()),
                deleted_by_clerk_user_id = COALESCE(deleted_by_clerk_user_id, $2)
            WHERE id = $1
            RETURNING deleted_at
            """,
            project_id,
            actor,
        )

    async def stop_runs_for_project(
        self, conn: asyncpg.Connection, *, project_id: UUID
    ) -> list[StoppedRunHandle]:
        rows = await conn.fetch(
            """
            UPDATE workflow_runs
            SET status = 'stopped', finished_at = COALESCE(finished_at, now()),
                lease_active = false, lease_released_at = COALESCE(lease_released_at, now())
            WHERE project_id = $1
              AND status IN ('pending', 'running', 'needs_input', 'paused')
            RETURNING id, temporal_workflow_id, sandbox_id
            """,
            project_id,
        )
        if rows:
            await conn.execute(
                "DELETE FROM broker_grants WHERE run_id = ANY($1::uuid[])",
                [row["id"] for row in rows],
            )
        return [
            StoppedRunHandle(
                run_id=row["id"],
                temporal_workflow_id=row["temporal_workflow_id"],
                sandbox_id=row["sandbox_id"],
            )
            for row in rows
        ]

    async def archive_project_workflows_for_deletion(
        self, conn: asyncpg.Connection, *, project_id: UUID
    ) -> list[UUID]:
        """Archive every saved workflow; return the ids that own a Temporal schedule."""
        await conn.execute(
            """
            UPDATE project_workflows
            SET status = 'archived', next_run_at = NULL, skip_scheduled_for = NULL,
                updated_at = now()
            WHERE project_id = $1 AND status <> 'archived'
            """,
            project_id,
        )
        await conn.execute(
            """
            UPDATE workflows SET status = 'archived', updated_at = now()
            WHERE project_id = $1 AND status <> 'archived'
            """,
            project_id,
        )
        rows = await conn.fetch(
            "SELECT id FROM project_workflows WHERE project_id = $1 "
            "AND temporal_schedule_id IS NOT NULL ORDER BY created_at, id",
            project_id,
        )
        return [row["id"] for row in rows]

    async def revoke_project_access(self, conn: asyncpg.Connection, *, project_id: UUID) -> None:
        await conn.execute("DELETE FROM project_invitations WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM project_memberships WHERE project_id = $1", project_id)

    async def billing_root_runs_for_project(
        self, conn: asyncpg.Connection, *, project_id: UUID
    ) -> list[UUID]:
        rows = await conn.fetch(
            """
            SELECT run_id FROM billing_run_budgets
            WHERE project_id = $1 AND run_id = root_run_id AND status <> 'settled'
            ORDER BY run_id
            """,
            project_id,
        )
        return [row["run_id"] for row in rows]

    async def purge_project_data(self, conn: asyncpg.Connection, *, project_id: UUID) -> None:
        """Remove everything a deleted project owns except runs, saved workflows and billing.

        Those rows are referenced by the append-only ledger and stay behind the tombstone.
        Every statement is a no-op on replay. Activity goes last so the events written by
        integration disconnects during the same deletion are swept too.
        """
        statements = (
            "DELETE FROM content_plan_revisions WHERE project_id = $1",
            "DELETE FROM content_plan_batches WHERE project_workflow_id IN "
            "(SELECT project_workflow_id FROM content_programs WHERE project_id = $1)",
            "DELETE FROM content_programs WHERE project_id = $1",
            "DELETE FROM workflow_review_commands WHERE project_id = $1",
            "DELETE FROM outreach_campaigns WHERE project_id = $1",
            "DELETE FROM run_tool_grants WHERE project_id = $1",
            "DELETE FROM run_decisions WHERE project_id = $1",
            "DELETE FROM broker_grants WHERE run_id IN "
            "(SELECT id FROM workflow_runs WHERE project_id = $1)",
            "DELETE FROM run_rollouts WHERE run_id IN "
            "(SELECT id FROM workflow_runs WHERE project_id = $1)",
            "DELETE FROM project_test_identities WHERE project_id = $1",
            "DELETE FROM integration_auth_attempts WHERE project_id = $1",
            "DELETE FROM integration_call_receipts WHERE project_id = $1",
            "UPDATE integration_webhook_deliveries SET project_id = NULL WHERE project_id = $1",
            "DELETE FROM integration_connections WHERE project_id = $1",
            "DELETE FROM project_secrets WHERE project_id = $1",
            "DELETE FROM chat_messages WHERE project_id = $1",
            "DELETE FROM project_file_changes WHERE project_id = $1",
            "DELETE FROM project_mcp_usage WHERE project_id = $1",
            "DELETE FROM activity_events WHERE project_id = $1 OR run_id IN "
            "(SELECT id FROM workflow_runs WHERE project_id = $1)",
            # The Temporal schedules are gone once the purge runs; a later delete lists none.
            "UPDATE project_workflows SET temporal_schedule_id = NULL WHERE project_id = $1",
        )
        for statement in statements:
            await conn.execute(statement, project_id)

    async def start_effect(
        self, conn: asyncpg.Connection, *, execution_key: str, operation: str
    ) -> None:
        await conn.execute(
            """
            INSERT INTO effect_receipts (execution_key, operation, status)
            VALUES ($1, $2, 'started')
            ON CONFLICT (execution_key) DO NOTHING
            """,
            execution_key,
            operation,
        )

    async def get_effect(
        self, execution_key: str, *, conn: asyncpg.Connection | None = None
    ) -> EffectReceipt | None:
        row = await (conn or self.pool).fetchrow(
            "SELECT * FROM effect_receipts WHERE execution_key = $1", execution_key
        )
        return _effect_receipt(row) if row else None

    async def complete_effect(
        self, conn: asyncpg.Connection, *, execution_key: str, result: dict[str, Any]
    ) -> None:
        updated = await conn.fetchval(
            """
            UPDATE effect_receipts
            SET status = 'completed', result = $2::jsonb, error_message = NULL, updated_at = now()
            WHERE execution_key = $1 AND status <> 'completed'
            RETURNING execution_key
            """,
            execution_key,
            json.dumps(result),
        )
        if updated is None:
            row = await conn.fetchrow(
                "SELECT status, result FROM effect_receipts WHERE execution_key = $1",
                execution_key,
            )
            if (
                row is None
                or row["status"] != "completed"
                or _json_object(row["result"], field="effect result") != result
            ):
                raise SideEffectConflictError("effect completion conflicts with its saved result")

    async def fail_effect(
        self, conn: asyncpg.Connection, *, execution_key: str, error_message: str
    ) -> None:
        await conn.execute(
            """
            UPDATE effect_receipts
            SET status = 'failed', error_message = $2, updated_at = now()
            WHERE execution_key = $1 AND status <> 'completed'
            """,
            execution_key,
            error_message[:2000],
        )

    async def save_publication_intent(
        self, conn: asyncpg.Connection, *, execution_key: str, intent: dict[str, Any]
    ) -> None:
        updated = await conn.fetchval(
            """
            UPDATE effect_receipts
            SET result = jsonb_set(COALESCE(result, '{}'::jsonb), '{publication}', $2::jsonb),
                updated_at = now()
            WHERE execution_key = $1 AND status <> 'completed'
            RETURNING execution_key
            """,
            execution_key,
            json.dumps(intent),
        )
        if updated is None:
            raise SideEffectConflictError("publication intent cannot replace a completed effect")

    async def save_effect_progress(
        self, conn: asyncpg.Connection, *, execution_key: str, result: dict[str, Any]
    ) -> None:
        """Persist a paid request's reservation/intent before dispatch, under its effect lock."""
        updated = await conn.fetchval(
            """
            UPDATE effect_receipts SET result = $2::jsonb, updated_at = now()
            WHERE execution_key = $1 AND status = 'started' RETURNING execution_key
            """,
            execution_key,
            json.dumps(result),
        )
        if updated is None:
            raise SideEffectConflictError(
                "effect progress cannot replace a completed or failed effect"
            )

    async def complete_organic_audit_projection(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_path: str,
        artifact_ref: str,
        summary: str | None = None,
    ) -> None:
        await self._complete_readonly_report_projection(
            conn,
            execution_key=execution_key,
            run_id=run_id,
            canonical_commit_sha=canonical_commit_sha,
            artifact_path=artifact_path,
            artifact_ref=artifact_ref,
            summary=summary,
            workflow_key="organic.audit",
        )

    async def complete_keyword_plan_projection(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_path: str,
        artifact_ref: str,
        summary: str,
    ) -> None:
        await self._complete_readonly_report_projection(
            conn,
            execution_key=execution_key,
            run_id=run_id,
            canonical_commit_sha=canonical_commit_sha,
            artifact_path=artifact_path,
            artifact_ref=artifact_ref,
            summary=summary,
            workflow_key="organic.keyword_plan",
        )

    async def _complete_readonly_report_projection(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_path: str,
        artifact_ref: str,
        summary: str | None,
        workflow_key: str,
        final_status: str = "succeeded",
    ) -> None:
        if final_status not in {"succeeded", "failed"}:
            raise ValueError("Unsupported report result status")
        event, default_summary = {
            "style.capture": ("style_capture_ready", "Writing style is ready."),
            "growth.onboarding_plan": ("onboarding_plan_ready", "The growth plan is ready."),
            "content.plan": ("content_plan_ready", "Content plan is ready."),
            "organic.audit": ("organic_audit_ready", "Organic visibility audit is ready."),
            "organic.keyword_plan": ("keyword_plan_ready", "Keyword opportunity plan is ready."),
            "ads.assessment": (
                "paid_ads_assessment_ready",
                "The paid ads assessment is ready.",
            ),
            "ads.monitor": (
                "paid_ads_monitor_ready",
                "Today's Google Ads check is done.",
            ),
            "organic.traffic_system": ("organic_system_ready", "Organic traffic system finished."),
            "organic.technical_fix": ("technical_fix_ready", "Technical fix inspection finished."),
        }[workflow_key]
        if final_status == "failed":
            event = "organic_system_incomplete"
        async with conn.transaction():
            projected = await conn.fetchval(
                """
                UPDATE workflow_runs
                SET status = $7, canonical_commit_sha = $2, artifact_path = $3,
                    artifact_ref = $4,
                    error_message = CASE WHEN $7 = 'failed' THEN $5::text ELSE NULL END,
                    result_summary = COALESCE($5::text, result_summary),
                    progress_summary = CASE WHEN $6 = 'organic.audit'
                        THEN COALESCE($5::text, progress_summary) ELSE progress_summary END,
                    finished_at = COALESCE(finished_at, now()), progress_percent = 100,
                    progress_updated_at = now(), heartbeat_at = now()
                WHERE id = $1 AND executor = $6
                  AND status NOT IN ('failed', 'stopped', 'superseded') AND NOT review_required
                  AND (canonical_commit_sha IS NULL OR canonical_commit_sha = $2)
                RETURNING id
                """,
                run_id,
                canonical_commit_sha,
                artifact_path,
                artifact_ref,
                summary,
                "codex.procedure" if workflow_key == "organic.technical_fix" else workflow_key,
                final_status,
            )
            if projected is None:
                raise SideEffectConflictError("report cannot complete in its current state")
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type=event,
                details={"artifact_ref": artifact_ref},
                summary=summary or default_summary,
                audience="product",
                dedupe_key=f"{execution_key}:{event}",
            )
            await self.complete_effect(
                conn, execution_key=execution_key, result={"artifact_ref": artifact_ref}
            )

    async def retain_procedure_output(
        self,
        conn: asyncpg.Connection,
        *,
        run_id: UUID,
        checkpoint: dict[str, Any],
        reason: str,
        only_if_missing: bool = False,
    ) -> None:
        from tin_lite.publication import OUTPUT_REASONS

        if reason not in OUTPUT_REASONS:
            raise ValueError("unsupported saved-output reason")
        await conn.execute(
            """
            UPDATE workflow_runs SET retained_output = $2::jsonb
            WHERE id = $1 AND executor IN ('codex.procedure', 'style.capture', 'workflow.code')
              AND canonical_commit_sha IS NULL
              AND (NOT $3 OR retained_output IS NULL)
            """,
            run_id,
            json.dumps({**checkpoint, "reason": reason}),
            only_if_missing,
        )

    async def set_output_resolution(
        self,
        conn: asyncpg.Connection,
        *,
        run_id: UUID,
        checkpoint: dict,
        resolution: dict | None,
    ) -> None:
        updated = await conn.fetchval(
            """UPDATE workflow_runs SET output_resolution = $2::jsonb
               WHERE id = $1 AND executor IN ('codex.procedure', 'style.capture', 'workflow.code')
                 AND status IN ('failed', 'stopped') AND NOT lease_active
                 AND canonical_commit_sha IS NULL
                 AND retained_output->>'reason' = 'output_conflict'
                 AND retained_output->>'ephemeral_commit_sha' = $3
                 AND retained_output->>'sha256' = $4
               RETURNING id""",
            run_id,
            json.dumps(resolution) if resolution is not None else None,
            checkpoint["ephemeral_commit_sha"],
            checkpoint["sha256"],
        )
        if updated is None:
            raise SideEffectConflictError("saved output is not eligible for resolution")

    async def begin_output_resolution(
        self,
        conn: asyncpg.Connection,
        *,
        run_id: UUID,
        execution_key: str,
        request: dict,
        checkpoint: dict,
        resolution: dict,
    ) -> None:
        async with conn.transaction():
            await self.start_effect(
                conn, execution_key=execution_key, operation="output_resolution"
            )
            updated = await conn.fetchval(
                """UPDATE effect_receipts SET result = $2::jsonb
                   WHERE execution_key = $1 AND status = 'started' AND result IS NULL
                   RETURNING execution_key""",
                execution_key,
                json.dumps({"request": request}),
            )
            if updated is None:
                raise SideEffectConflictError("resolution request already exists")
            await self.set_output_resolution(
                conn,
                run_id=run_id,
                checkpoint=checkpoint,
                resolution=resolution,
            )

    async def complete_output_resolution(
        self,
        conn: asyncpg.Connection,
        *,
        run_id: UUID,
        execution_key: str,
        request: dict,
        checkpoint: dict,
        outcome: dict,
    ) -> None:
        async with conn.transaction():
            await self.set_output_resolution(
                conn,
                run_id=run_id,
                checkpoint=checkpoint,
                resolution=None if outcome["state"] == "stale" else outcome,
            )
            await self.complete_effect(
                conn,
                execution_key=execution_key,
                result={"request": request, "outcome": outcome},
            )
            if outcome["state"] == "stale":
                return
            applied = outcome["action"] == "use_saved"
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type="procedure_output_applied" if applied else "procedure_output_kept",
                summary=(
                    "Applied the saved result to project Files."
                    if outcome["changed"]
                    else "The saved result already matches project Files."
                    if applied
                    else "Kept the current project file. The generated result remains saved."
                ),
                audience="product",
                dedupe_key=f"{execution_key}:resolved",
                details={
                    **outcome,
                    "kind": "your_edits" if outcome["changed"] else "decision",
                    "actor_clerk_user_id": request["actor_clerk_user_id"],
                    "client_id": request["client_id"],
                    "basis": "member_request",
                },
            )

    async def complete_procedure_persist(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        result: dict[str, Any],
        sandbox_killed: bool,
        sandbox_stage: str = "codex_procedure",
    ) -> None:
        async with conn.transaction():
            await self.complete_effect(conn, execution_key=execution_key, result=result)
            await conn.execute(
                """UPDATE workflow_runs
                   SET progress_mode = 'steps', progress_step = 'save',
                       progress_current = 2, progress_total = 3, progress_percent = NULL,
                       progress_summary = 'The result is saved. Finishing delivery.',
                       progress_updated_at = now(), heartbeat_at = now()
                   WHERE id = $1 AND executor IN ('codex.procedure', 'workflow.code')
                     AND status IN ('pending', 'running') AND canonical_commit_sha IS NULL""",
                run_id,
            )
            if isinstance(result.get("checkpoint"), dict):
                await self.retain_procedure_output(
                    conn,
                    run_id=run_id,
                    checkpoint=result["checkpoint"],
                    reason="publication_pending",
                    only_if_missing=True,
                )
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type="procedure_artifact_persisted",
                dedupe_key=f"{execution_key}:artifact_persisted",
            )
            if sandbox_killed:
                await self.add_activity(
                    conn=conn,
                    run_id=run_id,
                    event_type="sandbox_killed",
                    details={"stage": sandbox_stage},
                    dedupe_key=f"{execution_key}:sandbox_killed",
                )

    async def complete_procedure_publication(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        result: dict[str, Any],
        artifact_ref: str,
    ) -> None:
        """Commit availability, its receipt, and its fact together, without completing the run."""
        async with conn.transaction():
            await self.complete_effect(conn, execution_key=execution_key, result=result)
            projected = await conn.fetchval(
                """
                UPDATE workflow_runs
                SET canonical_commit_sha = $2, artifact_path = $3, artifact_ref = $4,
                    retained_output = NULL
                WHERE id = $1 AND executor IN ('codex.procedure', 'workflow.code')
                  AND (canonical_commit_sha IS NULL OR canonical_commit_sha = $2)
                RETURNING id
                """,
                run_id,
                result["canonical_commit_sha"],
                result["artifact_path"],
                artifact_ref,
            )
            if projected is None:
                raise SideEffectConflictError("publication conflicts with the run's saved output")
            await conn.execute(
                """UPDATE workflow_runs
                   SET progress_mode = 'steps', progress_step = 'finalize',
                       progress_current = 2, progress_total = 3, progress_percent = NULL,
                       progress_summary = 'Result saved to Files. Finishing the run.',
                       progress_updated_at = now(), heartbeat_at = now()
                   WHERE id = $1 AND status IN ('pending', 'running')
                     AND review_requested_at IS NULL""",
                run_id,
            )
            if result.get("changed", True):
                await self.add_activity(
                    conn=conn,
                    run_id=run_id,
                    event_type="procedure_canonical_commit_created",
                    details={"canonical_commit_sha": result["canonical_commit_sha"]},
                    dedupe_key=f"{execution_key}:canonical_commit_created",
                )

    async def complete_procedure_projection(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_path: str,
        artifact_ref: str,
        details: dict[str, Any],
        summary: str,
    ) -> None:
        async with conn.transaction():
            projected = await conn.fetchval(
                """
                UPDATE workflow_runs
                SET status = 'succeeded', canonical_commit_sha = $2, artifact_path = $3,
                    artifact_ref = $4, retained_output = NULL, error_message = NULL,
                    finished_at = COALESCE(finished_at, now()), progress_percent = 100,
                    progress_mode = 'steps', progress_step = 'complete',
                    progress_current = 3, progress_total = 3, progress_summary = $5,
                    progress_updated_at = now(), heartbeat_at = now()
                WHERE id = $1 AND executor IN ('codex.procedure', 'workflow.code')
                  AND status NOT IN ('failed', 'stopped', 'superseded')
                  AND (NOT review_required OR review_decision = 'approved' OR (
                    workflow_id = '00000000-0000-4000-8000-000000000031'
                    AND review_requested_at IS NULL AND review_decision IS NULL
                    AND EXISTS (
                      SELECT 1 FROM effect_receipts publication
                      WHERE publication.execution_key = workflow_runs.id::text
                        || ':procedure_canonical_commit'
                        AND publication.operation = 'procedure_canonical_commit'
                        AND publication.status = 'completed'
                        AND publication.result->>'canonical_commit_sha' = $2
                        AND publication.result->>'artifact_path' = $3
                        AND publication.result->'content_editorial'->>'schema'
                          = 'content-editorial-check.v1'
                        AND publication.result->'content_editorial'->>'outcome'
                          IN ('already_covered','needs_replanning','insufficient_evidence')
                    )
                  ))
                RETURNING id
                """,
                run_id,
                canonical_commit_sha,
                artifact_path,
                artifact_ref,
                summary[:240],
            )
            if projected is None:
                raise RuntimeError("run cannot complete before review or after terminal failure")
            await conn.execute(
                """
                UPDATE project_test_identities
                SET status = 'blocked',
                    status_note = 'The run finished without reporting the test identity status.',
                    updated_at = now()
                WHERE created_by_run_id = $1 AND status = 'pending'
                """,
                run_id,
            )
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type=(
                    "code_workflow_ready"
                    if await conn.fetchval(
                        "SELECT executor='workflow.code' FROM workflow_runs WHERE id=$1", run_id
                    )
                    else "codex_procedure_ready"
                ),
                details=details,
                summary=summary,
                audience="product",
                dedupe_key=f"{execution_key}:procedure_ready",
            )
            await self.complete_effect(conn, execution_key=execution_key, result=details)

    async def complete_visibility_projection(
        self,
        conn: asyncpg.Connection,
        *,
        execution_key: str,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_path: str,
        artifact_ref: str,
    ) -> None:
        """One transaction for visibility status, its product fact, and its receipt."""
        async with conn.transaction():
            projected = await conn.fetchval(
                """
                UPDATE workflow_runs
                SET status = 'succeeded', canonical_commit_sha = $2, artifact_path = $3,
                    artifact_ref = $4, error_message = NULL,
                    finished_at = COALESCE(finished_at, now()), progress_percent = 100,
                    progress_updated_at = now(), heartbeat_at = now()
                WHERE id = $1 AND executor = 'visibility.audit'
                  AND status NOT IN ('failed', 'stopped', 'superseded')
                  AND (NOT review_required OR review_decision = 'approved')
                  AND (canonical_commit_sha IS NULL OR canonical_commit_sha = $2)
                RETURNING id
                """,
                run_id,
                canonical_commit_sha,
                artifact_path,
                artifact_ref,
            )
            if projected is None:
                raise SideEffectConflictError("visibility run cannot complete in its current state")
            await self.add_activity(
                conn=conn,
                run_id=run_id,
                event_type="visibility_audit_ready",
                details={"artifact_ref": artifact_ref},
                summary="AI visibility audit is ready.",
                audience="product",
                dedupe_key=f"{execution_key}:visibility_audit_ready",
            )
            await self.complete_effect(
                conn, execution_key=execution_key, result={"artifact_ref": artifact_ref}
            )

    async def project_success(
        self, *, run_id: UUID, canonical_commit_sha: str, artifact_ref: str, artifact_path: str
    ) -> None:
        projected = await self.pool.fetchval(
            """
            UPDATE workflow_runs
            SET status = 'succeeded',
                canonical_commit_sha = $2,
                artifact_ref = $3,
                artifact_path = $4,
                error_message = NULL,
                finished_at = COALESCE(finished_at, now()),
                progress_percent = 100,
                progress_updated_at = now(),
                heartbeat_at = now()
            WHERE id = $1
              AND (NOT review_required OR review_decision = 'approved')
              AND status NOT IN ('failed', 'stopped', 'superseded')
            RETURNING id
            """,
            run_id,
            canonical_commit_sha,
            artifact_ref,
            artifact_path,
        )
        if projected is None:
            status = await self.pool.fetchval(
                "SELECT status FROM workflow_runs WHERE id = $1", run_id
            )
            if status in {"failed", "stopped", "superseded"}:
                raise SideEffectConflictError("run cannot complete in its current state")
            raise RuntimeError("run cannot complete before required human review")
        await self._track_run(run_id, "run_succeeded", artifact_path=artifact_path)

    async def request_human_review(
        self,
        *,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_ref: str,
        artifact_path: str,
        summary: str = "Answer page draft is ready for your review.",
        artifact_title: str | None = None,
    ) -> bool:
        """Expose a reviewable artifact and pause only when this run pinned review."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM workflow_runs WHERE id = $1 FOR UPDATE",
                run_id,
            )
            if row is None:
                raise LookupError(f"run {run_id} does not exist")
            if row["status"] in {
                RunStatus.FAILED.value,
                RunStatus.STOPPED.value,
                RunStatus.SUPERSEDED.value,
            }:
                raise RuntimeError("finished run cannot request human review")
            review_required = bool(row["review_required"])
            if row["status"] != RunStatus.SUCCEEDED.value:
                await conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status = CASE WHEN review_required AND review_decision IS NULL
                            THEN 'needs_input' ELSE 'running' END,
                        canonical_commit_sha = $2,
                        artifact_ref = $3,
                        artifact_path = $4,
                        artifact_title = $5,
                        review_requested_at = CASE
                            WHEN review_required THEN COALESCE(review_requested_at, now())
                            ELSE review_requested_at
                        END,
                        error_message = NULL,
                        progress_step = CASE WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN CASE WHEN review_required AND review_decision IS NULL
                                THEN 'review' ELSE 'finalize' END ELSE progress_step END,
                        progress_summary = CASE
                            WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN CASE WHEN review_required AND review_decision IS NULL
                                THEN 'Result saved. Waiting for your review.'
                                ELSE 'Result saved. Finishing the run.' END
                            ELSE progress_summary END,
                        progress_current = CASE
                            WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN 3 ELSE progress_current END,
                        progress_total = CASE WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN 3 ELSE progress_total END,
                        progress_mode = CASE WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN 'steps' ELSE progress_mode END,
                        progress_percent = CASE
                            WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN 100 ELSE progress_percent END,
                        progress_updated_at = now(), heartbeat_at = now()
                    WHERE id = $1
                    """,
                    run_id,
                    canonical_commit_sha,
                    artifact_ref,
                    artifact_path,
                    artifact_title,
                )
            if review_required and row["review_decision"] is None:
                filename = artifact_title or artifact_path.rsplit("/", 1)[-1]
                revision_supported = await conn.fetchval(
                    "SELECT project_id IS NULL "
                    "AND key IN ('content.generate','content.public_article') "
                    "FROM workflows WHERE id=$1",
                    row["workflow_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO run_decisions (
                        id, project_id, run_id, kind, title, explanation, consequence,
                        items, response_schema, feedback_supported, status
                    )
                    VALUES (
                        $5, $2, $1, 'review', 'Review workflow output', $3,
                        '',
                        $4::jsonb, '{}'::jsonb, $6, 'pending'
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        run_id=EXCLUDED.run_id, explanation=EXCLUDED.explanation,
                        items=EXCLUDED.items, feedback_supported=EXCLUDED.feedback_supported,
                        status='pending', response=NULL, applied_at=NULL,
                        applied_by_clerk_user_id=NULL
                    WHERE run_decisions.status='dismissed'
                    """,
                    run_id,
                    row["project_id"],
                    summary[:2000],
                    json.dumps(
                        [
                            {
                                "id": artifact_path,
                                "title": filename,
                                "file": artifact_path,
                                "revision": canonical_commit_sha,
                                "media_type": (
                                    "text/markdown"
                                    if artifact_path.casefold().endswith((".md", ".markdown"))
                                    else "image/svg+xml"
                                    if artifact_path.casefold().endswith(".svg")
                                    else "video/mp4"
                                    if artifact_path.casefold().endswith(".mp4")
                                    else "application/octet-stream"
                                ),
                            }
                        ]
                    ),
                    row["review_root_run_id"] or run_id,
                    bool(revision_supported),
                )
                await conn.execute(
                    """
                    INSERT INTO activity_events (
                        project_id, run_id, event_type, details, summary, audience, dedupe_key
                    )
                    VALUES ($1, $2, 'human_review_requested', $3::jsonb, $4, 'product', $5)
                    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                    """,
                    row["project_id"],
                    run_id,
                    json.dumps({"kind": "needs_you", "artifact_ref": artifact_ref}),
                    summary[:1000],
                    f"{run_id}:human_review_requested",
                )
                await self._track_run(run_id, "run_held", conn=conn, summary=summary[:1000])
            return review_required

    async def record_human_review(
        self,
        *,
        run_id: UUID,
        decision: str,
        summary: str = "You approved the answer page draft.",
    ) -> None:
        if decision != "approved":
            raise ValueError("unsupported human review decision")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM workflow_runs WHERE id = $1 FOR UPDATE",
                run_id,
            )
            if row is None:
                raise LookupError(f"run {run_id} does not exist")
            if not row["review_required"]:
                raise RuntimeError("run does not require human review")
            if row["review_decision"] is None:
                if row["status"] != RunStatus.NEEDS_INPUT.value:
                    raise RuntimeError("run is not waiting for human review")
                await conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status = 'running',
                        review_decision = 'approved',
                        reviewed_at = now(),
                        progress_step = CASE WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN 'finalize' ELSE progress_step END,
                        progress_summary = CASE
                            WHEN executor IN ('codex.procedure', 'workflow.code')
                            THEN 'Approved. Finishing the run.' ELSE progress_summary END,
                        progress_updated_at = now(), heartbeat_at = now()
                    WHERE id = $1
                    """,
                    run_id,
                )
            elif row["review_decision"] != decision:
                raise RuntimeError("run already has a different review decision")
            await conn.execute(
                """
                UPDATE run_decisions
                SET status = 'applied', response = $2::jsonb,
                    applied_at = COALESCE(applied_at, now()),
                    applied_by_clerk_user_id = COALESCE(
                        applied_by_clerk_user_id, $3
                    )
                WHERE run_id = $1 AND status = 'pending'
                """,
                run_id,
                json.dumps({"action": decision}),
                row["reviewed_by_clerk_user_id"],
            )
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'human_review_approved', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                row["project_id"],
                run_id,
                json.dumps(
                    {
                        "kind": "your_edits",
                        "decision": decision,
                        "actor_clerk_user_id": row["reviewed_by_clerk_user_id"],
                    }
                ),
                summary[:1000],
                f"{run_id}:human_review_approved",
            )
            await self._track_run(run_id, "run_review_recorded", conn=conn, decision=decision)

    async def refresh_project_memory_projection(
        self,
        *,
        project_id: UUID,
        canonical_commit_sha: str,
        artifact_path: str,
        memory_index: str,
    ) -> None:
        """Project a memory index committed by a section-owning procedure run."""
        updated = await self.pool.fetchval(
            """
            UPDATE projects
            SET memory_commit_sha = $2,
                memory_index_path = $3,
                memory_index = $4,
                memory_updated_at = now()
            WHERE id = $1
            RETURNING id
            """,
            project_id,
            canonical_commit_sha,
            artifact_path,
            memory_index,
        )
        if updated is None:
            raise LookupError(f"project {project_id} does not exist")

    async def project_memory_success(
        self,
        *,
        run_id: UUID,
        canonical_commit_sha: str,
        artifact_ref: str,
        artifact_path: str,
        memory_index: str,
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE projects
                SET memory_commit_sha = $2,
                    memory_index_path = $3,
                    memory_index = $4,
                    memory_updated_at = now()
                FROM workflow_runs
                WHERE projects.id = workflow_runs.project_id
                  AND workflow_runs.id = $1
                RETURNING projects.id
                """,
                run_id,
                canonical_commit_sha,
                artifact_path,
                memory_index,
            )
            if updated is None:
                raise LookupError(f"run {run_id} does not exist")
            await conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'succeeded',
                    canonical_commit_sha = $2,
                    artifact_ref = $3,
                    artifact_path = $4,
                    error_message = NULL,
                    finished_at = COALESCE(finished_at, now())
                WHERE id = $1
                """,
                run_id,
                canonical_commit_sha,
                artifact_ref,
                artifact_path,
            )

    async def project_failure(self, *, run_id: UUID, error_message: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE workflow_runs
                SET status = 'failed',
                    error_message = $2,
                    task_phase = CASE
                        WHEN executor = 'project.task' THEN 'failed'
                        ELSE task_phase
                    END,
                    task_summary = CASE
                        WHEN executor = 'project.task'
                            THEN 'This Codex task could not finish. Ask Tin to start it again.'
                        ELSE task_summary
                    END,
                    finished_at = COALESCE(finished_at, now())
                WHERE id = $1 AND status NOT IN ('succeeded', 'stopped', 'superseded')
                RETURNING project_id, executor
                """,
                run_id,
                error_message[:2000],
            )
            if row is None:
                return
            await conn.execute(
                """
                INSERT INTO activity_events (
                    project_id, run_id, event_type, details, summary, audience, dedupe_key
                )
                VALUES ($1, $2, 'workflow_run_failed', $3::jsonb, $4, 'product', $5)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                row["project_id"],
                run_id,
                json.dumps({"kind": "runs", "status": "failed"}),
                _failure_summary(str(row["executor"])),
                f"{run_id}:workflow_run_failed",
            )
            await self._track_run(run_id, "run_failed", conn=conn, error=error_message[:2000])

    async def run_start_idempotency_key(self, run_id: UUID) -> str | None:
        return await self.pool.fetchval(
            "SELECT start_idempotency_key FROM workflow_runs WHERE id = $1", run_id
        )

    async def record_run_auto_retry(
        self,
        *,
        run_id: UUID,
        retry_run_id: UUID,
        failure: str,
        restarted_recently: bool,
    ) -> None:
        """Note on the failed run that Tin started it again; analytics learns which run retried."""
        await self.add_activity(
            run_id=run_id,
            event_type="workflow_run_auto_retried",
            details={
                "kind": "runs",
                "retry_run_id": str(retry_run_id),
                "restarted_recently": restarted_recently,
            },
            summary="Tin started this run again after a passing failure.",
            audience="product",
            dedupe_key=f"{run_id}:workflow_run_auto_retried",
        )
        await self._track_run(
            retry_run_id,
            "run_auto_retried",
            retried_from=str(run_id),
            failure=failure[:600],
            restarted_recently=restarted_recently,
        )

    async def _track_run(
        self,
        run_id: UUID,
        event: str,
        *,
        conn: asyncpg.Connection | None = None,
        **properties: Any,
    ) -> None:
        """Send one run event to analytics, with the run's project and workflow attached."""
        if not analytics.current().enabled:
            return
        row = await (conn if conn is not None else self.pool).fetchrow(
            """
            SELECT r.project_id, r.status, r.executor, r.trigger_source, r.review_required,
                   w.key AS workflow_key
            FROM workflow_runs r JOIN workflows w ON w.id = r.workflow_id
            WHERE r.id = $1
            """,
            run_id,
        )
        if row is None:
            return
        analytics.capture(
            event,
            distinct_id=row["project_id"],
            project_id=row["project_id"],
            properties={
                "run_id": str(run_id),
                "workflow": row["workflow_key"],
                "status": row["status"],
                "executor": row["executor"],
                "trigger_source": row["trigger_source"],
                "review_required": row["review_required"],
                **properties,
            },
        )

    async def add_activity(
        self,
        *,
        run_id: UUID,
        event_type: str,
        details: dict[str, Any] | None = None,
        summary: str | None = None,
        audience: str = "internal",
        dedupe_key: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        await (conn if conn is not None else self.pool).execute(
            """
            INSERT INTO activity_events (
                project_id, run_id, event_type, details, summary, audience, dedupe_key
            )
            SELECT project_id, id, $2, $3::jsonb, $4, $5, $6
            FROM workflow_runs
            WHERE id = $1
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
            """,
            run_id,
            event_type,
            json.dumps(details or {}),
            summary,
            audience,
            dedupe_key,
        )
        await self._track_run(
            run_id,
            f"activity_{event_type}",
            conn=conn,
            summary=summary,
            details=details or {},
            audience=audience,
        )

    async def insert_run_rollouts(
        self,
        *,
        run_id: UUID,
        generation: int,
        execution_key: str,
        activity_attempt: int,
        stage: str,
        sandbox_id: str,
        files: Sequence[RolloutFile],
    ) -> int:
        """Store redacted rollout files for one sandbox; re-inserts are no-ops."""
        inserted = 0
        async with self.pool.acquire() as conn, conn.transaction():
            for file in files:
                row = await conn.fetchval(
                    """
                    INSERT INTO run_rollouts (
                        run_id, generation, execution_key, activity_attempt, stage, sandbox_id,
                        thread_id, filename, content_gzip, size_bytes, stored_bytes, sha256,
                        truncated, redactions
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    ON CONFLICT (run_id, generation, filename) DO NOTHING
                    RETURNING id
                    """,
                    run_id,
                    generation,
                    execution_key,
                    activity_attempt,
                    stage,
                    sandbox_id,
                    file.thread_id,
                    file.filename,
                    gzip.compress(file.content, compresslevel=6),
                    file.size_bytes,
                    len(file.content),
                    hashlib.sha256(file.content).hexdigest(),
                    file.truncated,
                    file.redactions,
                )
                if row is not None:
                    inserted += 1
        return inserted

    async def list_run_rollouts(self, run_id: UUID) -> list[RunRollout]:
        rows = await self.pool.fetch(
            """
            SELECT id, run_id, generation, execution_key, activity_attempt, stage, sandbox_id,
                   thread_id, filename, size_bytes, stored_bytes, sha256, truncated, redactions,
                   created_at
            FROM run_rollouts
            WHERE run_id = $1
            ORDER BY id
            """,
            run_id,
        )
        return [_run_rollout(row) for row in rows]

    async def read_run_rollout(self, rollout_id: int) -> tuple[RunRollout, bytes] | None:
        row = await self.pool.fetchrow("SELECT * FROM run_rollouts WHERE id = $1", rollout_id)
        if row is None:
            return None
        return _run_rollout(row), gzip.decompress(bytes(row["content_gzip"]))

    async def activity_counts(self, run_id: UUID) -> dict[str, int]:
        rows = await self.pool.fetch(
            """
            SELECT event_type, count(*) AS count
            FROM activity_events
            WHERE run_id = $1
            GROUP BY event_type
            """,
            run_id,
        )
        return {row["event_type"]: row["count"] for row in rows}


async def apply_migrations(dsn: str, migrations_dir: Path) -> list[str]:
    conn = await asyncpg.connect(dsn)
    applied: list[str] = []
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tin_schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        paths = await asyncio.to_thread(lambda: sorted(migrations_dir.glob("*.sql")))
        for path in paths:
            exists = await conn.fetchval(
                "SELECT true FROM tin_schema_migrations WHERE version = $1", path.name
            )
            if exists:
                continue
            async with conn.transaction():
                sql = await asyncio.to_thread(path.read_text)
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO tin_schema_migrations (version) VALUES ($1)", path.name
                )
            applied.append(path.name)
    finally:
        await conn.close()
    return applied


def _project(row: asyncpg.Record) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        state_repo_id=row["state_repo_id"],
        canonical_branch=row["canonical_branch"],
        memory_commit_sha=row["memory_commit_sha"],
        memory_index_path=row["memory_index_path"],
        memory_index=row["memory_index"],
        memory_updated_at=row["memory_updated_at"],
        timezone=row.get("timezone", "UTC"),
        workspace_id=row.get("workspace_id"),
        workspace_name=row.get("workspace_name"),
        can_create_project_in_workspace=row.get("can_create_project_in_workspace", False),
        created_by_clerk_user_id=row.get("created_by_clerk_user_id"),
        member_count=row.get("member_count", 1),
        deleted_at=row.get("deleted_at"),
        deleted_by_clerk_user_id=row.get("deleted_by_clerk_user_id"),
    )


def _workspace(row: asyncpg.Record) -> Workspace:
    return Workspace(
        id=row["id"],
        name=row["name"],
        created_by_clerk_user_id=row["created_by_clerk_user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _project_membership(row: asyncpg.Record) -> ProjectMembership:
    return ProjectMembership(
        project_id=row["project_id"],
        clerk_user_id=row["clerk_user_id"],
        created_at=row["created_at"],
    )


def _project_invitation(row: asyncpg.Record) -> ProjectInvitation:
    return ProjectInvitation(
        id=row["id"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        email=row["email"],
        created_by_clerk_user_id=row["created_by_clerk_user_id"],
        accepted_by_clerk_user_id=row["accepted_by_clerk_user_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        accepted_at=row["accepted_at"],
        revoked_at=row["revoked_at"],
    )


def _chat_message(row: asyncpg.Record) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        project_id=row["project_id"],
        request_id=row["request_id"],
        role=row["role"],
        source=row["source"],
        content=row["content"],
        author_clerk_user_id=row["author_clerk_user_id"],
        response_id=row["response_id"],
        routed_workflow_key=row["routed_workflow_key"],
        run_id=row["run_id"],
        created_at=row["created_at"],
    )


def _project_task_entry(row: asyncpg.Record) -> ProjectTaskEntry:
    return ProjectTaskEntry(
        id=row["id"],
        run_id=row["run_id"],
        request_id=row["request_id"],
        kind=row["kind"],
        source=row["source"],
        content=row["content"],
        author_clerk_user_id=row["author_clerk_user_id"],
        delivered_at=row["delivered_at"],
        created_at=row["created_at"],
    )


def _workflow(row: asyncpg.Record) -> Workflow:
    definition = _json_object(row["definition"], field="workflow definition")
    system_id = row.get("system_id")
    if system_id is None and isinstance(definition.get("system"), str):
        system_id = definition["system"]
    return Workflow(
        id=row["id"],
        project_id=row["project_id"],
        key=row["key"],
        title=row["title"],
        description=row["description"],
        executor=row["executor"],
        definition_repo_id=row["definition_repo_id"],
        definition_path=row["definition_path"],
        current_commit_sha=row["current_commit_sha"],
        version_label=row["version_label"],
        definition=definition,
        status=WorkflowStatus(row["status"]),
        system_id=system_id,
        system_name=row.get("system_name"),
        system_order=row.get("system_order"),
        forked_from_workflow_id=row["forked_from_workflow_id"],
        forked_from_commit_sha=row["forked_from_commit_sha"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _project_workflow(row: asyncpg.Record) -> ProjectWorkflow:
    raw_status = row.get("last_run_status")
    return ProjectWorkflow(
        id=row["id"],
        project_id=row["project_id"],
        workflow_id=row["workflow_id"],
        workflow_key=row["workflow_key"],
        workflow_title=row["workflow_title"],
        workflow_description=row["workflow_description"],
        version_label=row["version_label"],
        definition_commit_sha=row["definition_commit_sha"],
        name=row["name"],
        inputs=_json_object(row["inputs"], field="project workflow inputs"),
        input_schema=_json_object(row["input_schema"], field="project workflow input schema"),
        schedule=(
            _json_object(row["schedule"], field="project workflow schedule")
            if row["schedule"] is not None
            else None
        ),
        status=row["status"],
        temporal_schedule_id=row["temporal_schedule_id"],
        next_run_at=row["next_run_at"],
        last_run_id=row.get("last_run_id"),
        last_run_status=RunStatus(raw_status) if raw_status is not None else None,
        last_artifact_path=row.get("last_artifact_path"),
        last_artifact_title=row.get("last_artifact_title"),
        last_error=row["last_error"],
        settings_revision=row["settings_revision"],
        created_by_clerk_user_id=row["created_by_clerk_user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        skip_scheduled_for=row.get("skip_scheduled_for"),
        last_result_summary=row.get("last_result_summary"),
        last_started_at=row.get("last_started_at"),
        last_finished_at=row.get("last_finished_at"),
        run_count=row.get("run_count", 0) or 0,
        done_count=row.get("done_count", 0) or 0,
        failed_count=row.get("failed_count", 0) or 0,
        typical_duration_seconds=row.get("typical_duration_seconds"),
        content_revision=(
            _json_object(row["content_revision"], field="content revision")
            if row.get("content_revision") is not None
            else None
        ),
    )


def _proposal(row: asyncpg.Record) -> dict[str, Any]:
    value = dict(row)
    for key in ("previous", "proposed"):
        raw = value.get(key)
        value[key] = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return value


def _proposal_summary(proposal: dict[str, Any]) -> str:
    proposed = proposal.get("proposed") or {}
    if proposal.get("kind") == "budget_change":
        return (
            f"Proposal {proposal.get('proposal_number')}: change the daily budget to "
            f"${proposed.get('daily_budget_usd')}."
        )
    return (
        f"Proposal {proposal.get('proposal_number')}: switch bidding to "
        f"{str(proposed.get('strategy', '')).replace('_', ' ')}."
    )


def _run(row: asyncpg.Record) -> WorkflowRun:
    return WorkflowRun(
        id=row["id"],
        project_id=row["project_id"],
        workflow_id=row["workflow_id"],
        executor=row["executor"],
        definition_commit_sha=row["definition_commit_sha"],
        temporal_workflow_id=row["temporal_workflow_id"],
        thread_id=row["thread_id"],
        generation=row["generation"],
        fencing_token=row["fencing_token"],
        status=RunStatus(row["status"]),
        project_workflow_id=row.get("project_workflow_id"),
        trigger_source=row.get("trigger_source", "manual"),
        scheduled_for=row.get("scheduled_for"),
        review_required=row.get("review_required", False),
        review_root_run_id=row.get("review_root_run_id"),
        review_source_run_id=row.get("review_source_run_id"),
        review_version=row.get("review_version", 1),
        review_decision=row.get("review_decision"),
        review_requested_at=row.get("review_requested_at"),
        reviewed_at=row.get("reviewed_at"),
        started_by_clerk_user_id=row.get("started_by_clerk_user_id"),
        reviewed_by_clerk_user_id=row.get("reviewed_by_clerk_user_id"),
        system_wiki_commit_sha=row.get("system_wiki_commit_sha"),
        sandbox_id=row["sandbox_id"],
        lease_owner=row["lease_owner"],
        lease_active=row["lease_active"],
        lease_released_at=row["lease_released_at"],
        ephemeral_branch=row["ephemeral_branch"],
        expected_head_sha=row["expected_head_sha"],
        canonical_commit_sha=row["canonical_commit_sha"],
        artifact_path=row["artifact_path"],
        artifact_title=row.get("artifact_title"),
        artifact_ref=row["artifact_ref"],
        retained_output=(
            _json_object(row["retained_output"], field="retained output")
            if row.get("retained_output") is not None
            else None
        ),
        output_resolution=(
            _json_object(row["output_resolution"], field="output resolution")
            if row.get("output_resolution") is not None
            else None
        ),
        error_message=row["error_message"],
        input=_json_object(row.get("input", {}), field="run input"),
        task_title=row.get("task_title"),
        task_phase=row.get("task_phase"),
        task_summary=row.get("task_summary"),
        task_question=row.get("task_question"),
        task_question_requested_at=row.get("task_question_requested_at"),
        task_turn_number=row.get("task_turn_number", 0),
        task_control=row.get("task_control"),
        task_result=row.get("task_result"),
        task_diff=(
            _json_object(row.get("task_diff"), field="task diff")
            if row.get("task_diff") is not None
            else None
        ),
        task_has_changes=row.get("task_has_changes"),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        trigger_client=row.get("trigger_client"),
        started_by_oauth_client_id=row.get("started_by_oauth_client_id"),
        retry_of_run_id=row.get("retry_of_run_id"),
        progress_mode=row.get("progress_mode", "indeterminate"),
        progress_step=row.get("progress_step"),
        progress_current=row.get("progress_current"),
        progress_total=row.get("progress_total"),
        progress_percent=row.get("progress_percent"),
        progress_summary=row.get("progress_summary"),
        progress_updated_at=row.get("progress_updated_at"),
        heartbeat_at=row.get("heartbeat_at"),
        result_summary=row.get("result_summary"),
        prerequisite_evidence=(
            _json_object(row["prerequisite_evidence"], field="prerequisite evidence")
            if row.get("prerequisite_evidence") is not None
            else None
        ),
    )


def _effect_receipt(row: asyncpg.Record) -> EffectReceipt:
    raw_result = row["result"]
    result = (
        _json_object(raw_result, field="effect receipt result") if raw_result is not None else None
    )
    return EffectReceipt(
        execution_key=row["execution_key"],
        operation=row["operation"],
        status=row["status"],
        result=result,
        error_message=row["error_message"] if "error_message" in row else None,
    )


def _run_rollout(row: asyncpg.Record) -> RunRollout:
    return RunRollout(
        id=int(row["id"]),
        run_id=row["run_id"],
        generation=int(row["generation"]),
        execution_key=row["execution_key"],
        activity_attempt=int(row["activity_attempt"]),
        stage=row["stage"],
        sandbox_id=row["sandbox_id"],
        thread_id=row["thread_id"],
        filename=row["filename"],
        size_bytes=int(row["size_bytes"]),
        stored_bytes=int(row["stored_bytes"]),
        sha256=row["sha256"],
        truncated=bool(row["truncated"]),
        redactions=int(row["redactions"]),
        created_at=row["created_at"],
    )


def _activity_event(row: asyncpg.Record) -> ActivityEvent:
    return ActivityEvent(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        event_type=row["event_type"],
        details=_json_object(row["details"], field="activity event details"),
        summary=row["summary"],
        audience=row["audience"],
        created_at=row["created_at"],
        workflow_key=row.get("workflow_key"),
        workflow_title=row.get("workflow_title"),
    )


def _project_test_identity(row: asyncpg.Record) -> ProjectTestIdentity:
    return ProjectTestIdentity(
        id=row["id"],
        project_id=row["project_id"],
        created_by_run_id=row["created_by_run_id"],
        target_host=row["target_host"],
        label=row["label"],
        email=row["email"],
        auth_kind=row["auth_kind"],
        username=row["username"],
        password_ciphertext=row["password_ciphertext"],
        credential_key_version=row["credential_key_version"],
        status=row["status"],
        status_note=row["status_note"],
        verified_at=row["verified_at"],
        last_used_run_id=row["last_used_run_id"],
        last_used_at=row["last_used_at"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        phone_number=row["phone_number"] if "phone_number" in row.keys() else None,
    )


def _integration_connection(row: asyncpg.Record) -> IntegrationConnection:
    return IntegrationConnection(
        id=row["id"],
        project_id=row["project_id"],
        provider_key=row["provider_key"],
        status=row["status"],
        external_account_id=row["external_account_id"],
        external_account_label=row["external_account_label"],
        configuration=_json_object(row["configuration"], field="integration configuration"),
        credential_ciphertext=row["credential_ciphertext"],
        credential_key_version=row["credential_key_version"],
        connected_by_clerk_user_id=row["connected_by_clerk_user_id"],
        last_checked_at=row["last_checked_at"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _integration_auth_attempt(row: asyncpg.Record) -> IntegrationAuthAttempt:
    return IntegrationAuthAttempt(
        token_hash=row["token_hash"],
        project_id=row["project_id"],
        provider_key=row["provider_key"],
        clerk_user_id=row["clerk_user_id"],
        pkce_verifier_ciphertext=row["pkce_verifier_ciphertext"],
        requested_capabilities=tuple(
            _json_list(row["requested_capabilities"], field="requested integration capabilities")
        ),
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        created_at=row["created_at"],
        context=(
            _json_object(row["context"], field="integration attempt context")
            if row.get("context") is not None
            else {}
        ),
    )


def _integration_call_receipt(row: asyncpg.Record) -> IntegrationCallReceipt:
    return IntegrationCallReceipt(
        execution_key=row["execution_key"],
        project_id=row["project_id"],
        connection_id=row["connection_id"],
        provider_key=row["provider_key"],
        capability=row["capability"],
        request_fingerprint=row["request_fingerprint"],
        status=row["status"],
        response_summary=(
            _json_object(row["response_summary"], field="integration response summary")
            if row["response_summary"] is not None
            else None
        ),
        provider_request_id=row["provider_request_id"],
        error_code=row["error_code"],
        run_id=row["run_id"],
    )


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be a JSON object")
    return value


def _json_list(value: Any, *, field: str) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a JSON list")
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _failure_summary(executor: str) -> str:
    subject = {
        "content.design_md": "Project design",
        "project.memory": "Project memory update",
        "scan.report": "Project scan",
        "site.health_improve": "Site-health improvement",
        "visibility.audit": "AI visibility audit",
        "content.answer_page": "Answer page draft",
        "project.task": "One-off project task",
    }.get(executor, "Workflow run")
    if executor == PROJECT_TASK_WORKFLOW_NAME:
        return f"{subject} could not finish."
    return f"{subject} stopped before it finished."
