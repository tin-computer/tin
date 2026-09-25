"""Shared prepaid admission and settlement, separate from provider expense."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tin_lite.billing_contracts import (
    BillingError,
    ProjectSpendingPolicy,
    digest,
    final_charge,
    object_value,
    receipt_charge,
    test_terms,
    usd,
)
from tin_lite.codex_api import api_enabled as codex_api_enabled
from tin_lite.workflow_costs import configured_terms, incremental, liability, session_funded
from tin_lite.workflow_definitions import resolve_execution_contract
from tin_lite.workflow_inputs import normalize_workflow_inputs


async def configure_billing(database, settings):
    """Choose the payment policy once; observations do not depend on this service.

    Refuse to silently turn an enrolled hosted database into unbilled execution.
    Pausing paid work uses billing_test_enabled, while keeping reconciliation alive.
    Fresh self-hosted databases use the same migrations with empty billing tables.
    """
    if getattr(settings, "billing_enabled", False):
        database.billing = BillingService(database=database, settings=settings)
        return
    if await database.pool.fetchval("SELECT EXISTS (SELECT 1 FROM billing_accounts)"):
        raise RuntimeError(
            "This database contains billing accounts. Set TIN_LITE_BILLING_ENABLED=true; "
            "pause paid work with TIN_LITE_BILLING_TEST_ENABLED=false instead."
        )
    database.billing = None


LIMIT_HINT = " Raise the project's limits with set_project_spending_limits or on the Billing page."


def project_limit_message(policy, estimate, usage) -> str | None:
    """Name the one project limit that blocks admission, or None when none does."""
    if not policy:
        return "This project has no spending policy yet." + LIMIT_HINT
    if estimate > policy["per_run_nanos"]:
        return (
            f"This workflow is estimated at up to ${usd(estimate)}; "
            f"the project's per-run limit is ${usd(policy['per_run_nanos'])}." + LIMIT_HINT
        )
    if usage["exposure"] + estimate > policy["monthly_nanos"]:
        return (
            f"This workflow is estimated at up to ${usd(estimate)}, which would exceed "
            f"this month's ${usd(policy['monthly_nanos'])} project limit "
            f"(${usd(usage['exposure'])} already committed)." + LIMIT_HINT
        )
    if usage["active"] >= policy["concurrency"]:
        active = usage["active"]
        return (
            f"{active} run{'' if active == 1 else 's'} {'is' if active == 1 else 'are'} "
            f"already active; the project's concurrent-run limit is {policy['concurrency']}. "
            "Wait for a run to finish, or raise the limit with set_project_spending_limits "
            "or on the Billing page."
        )
    return None


class BillingService:
    def __init__(self, *, database, settings):
        self.db, self.settings = database, settings

    async def workspace_billing_admin(self, conn, workspace_id):
        """One authority for wallet creation and activation, including older workspaces.

        An assigned billing admin wins; never replace one implicitly. Without an
        account, use the recorded creator, or the sole legacy workspace administrator.
        Project membership alone never confers billing authority.
        """
        return await conn.fetchval(
            """SELECT m.clerk_user_id FROM workspaces w
            LEFT JOIN billing_accounts a ON a.workspace_id=w.id
            JOIN workspace_memberships m ON m.workspace_id=w.id
              AND m.clerk_user_id=CASE
                WHEN a.workspace_id IS NOT NULL THEN a.admin_clerk_user_id
                WHEN w.created_by_clerk_user_id IS NOT NULL THEN w.created_by_clerk_user_id
                ELSE (SELECT min(legacy.clerk_user_id) FROM workspace_memberships legacy
                      WHERE legacy.workspace_id=w.id HAVING count(*)=1)
              END
            WHERE w.id=$1""",
            workspace_id,
        )

    async def ensure_hosted_project(self, conn, project_id):
        """Hosted policy only. Preserve existing ownership, credits and spending limits."""
        if not getattr(self.settings, "billing_hosted_defaults_enabled", False):
            return
        workspace_id = await conn.fetchval(
            "SELECT workspace_id FROM projects WHERE id=$1 AND deleted_at IS NULL",
            project_id,
        )
        admin = await self.workspace_billing_admin(conn, workspace_id)
        if admin is None:
            # Ambiguous or revoked authority must not become unbilled execution.
            return
        await conn.execute(
            """INSERT INTO billing_accounts(workspace_id, mode,
                admin_clerk_user_id, run_billing_enabled) VALUES($1,'test',$2,true)
            ON CONFLICT(workspace_id) DO UPDATE SET run_billing_enabled=true
            WHERE billing_accounts.workspace_id=$1 AND NOT billing_accounts.run_billing_enabled""",
            workspace_id,
            admin,
        )
        await conn.execute(
            """INSERT INTO billing_project_policies(project_id, workspace_id, per_run_nanos,
                monthly_nanos, concurrency, revision)
            VALUES($1,$2,10000000000,10000000000,1,1)
            ON CONFLICT(project_id) DO NOTHING""",
            project_id,
            workspace_id,
        )

    async def ensure_hosted_projects(self, actor):
        if not getattr(self.settings, "billing_hosted_defaults_enabled", False):
            return
        async with self.db.pool.acquire() as conn, conn.transaction():
            projects = await conn.fetch(
                "SELECT m.project_id FROM project_memberships m "
                "JOIN projects p ON p.id=m.project_id "
                "WHERE m.clerk_user_id=$1 ORDER BY p.workspace_id, p.id",
                actor,
            )
            for project in projects:
                await self.ensure_hosted_project(conn, project["project_id"])

    async def grant_welcome_credit(self, actor):
        """One $10 grant per Tin user, pooled in their first joined workspace.

        Called after authenticated identity recording / project discovery, and by
        the bounded operator backfill. No Clerk directory import, payment, or
        billing enrollment. A user without a project is retried after provisioning.
        """
        if not getattr(self.settings, "billing_welcome_credits_enabled", False):
            return False
        # Normal authenticated reads need no user/account locks once credited.
        if await self.db.pool.fetchval(
            "SELECT true FROM billing_welcome_grants WHERE clerk_user_id=$1", actor
        ):
            return False
        async with self.db.pool.acquire() as conn, conn.transaction():
            # Same identity is serialized even across different workspaces/clients.
            user = await conn.fetchval(
                "SELECT clerk_user_id FROM tin_users WHERE clerk_user_id=$1 FOR UPDATE", actor
            )
            if not user or await conn.fetchval(
                "SELECT true FROM billing_welcome_grants WHERE clerk_user_id=$1", actor
            ):
                return False
            target = await conn.fetchrow(
                """SELECT p.id AS project_id, p.workspace_id
                   FROM project_memberships m JOIN projects p ON p.id=m.project_id
                   JOIN workspaces w ON w.id=p.workspace_id
                   WHERE m.clerk_user_id=$1
                   ORDER BY m.created_at, p.created_at, p.id LIMIT 1""",
                actor,
            )
            if target is None:
                return False
            admin = await self.workspace_billing_admin(conn, target["workspace_id"])
            if admin:
                await conn.execute(
                    """INSERT INTO billing_accounts(workspace_id, mode, admin_clerk_user_id,
                           run_billing_enabled) VALUES($1,'test',$2,false)
                       ON CONFLICT(workspace_id) DO NOTHING""",
                    target["workspace_id"],
                    admin,
                )
            account = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
                target["workspace_id"],
            )
            # Ambiguous legacy workspaces need an explicitly assigned billing admin.
            # A joining member must never acquire that authority by claiming a grant.
            if account is None:
                return False
            from tin_lite.billing_contracts import NANOS_PER_DOLLAR

            amount, event_key = 10 * NANOS_PER_DOLLAR, f"welcome:{digest(actor)}"
            await self.post_ledger(
                conn,
                account=account,
                event_key=event_key,
                kind="welcome_credit",
                amount=amount,
                project_id=target["project_id"],
                reference="One-time welcome credit",
            )
            await conn.execute(
                """UPDATE billing_accounts SET welcome_remaining_nanos=welcome_remaining_nanos+$2
                   WHERE workspace_id=$1""",
                target["workspace_id"],
                amount,
            )
            await conn.execute(
                """INSERT INTO billing_welcome_grants(clerk_user_id, workspace_id, ledger_id,
                       amount_nanos) SELECT $1,$2,id,$3 FROM billing_ledger WHERE event_key=$4""",
                actor,
                target["workspace_id"],
                amount,
                event_key,
            )
            return True

    async def require_project(self, conn, project_id, actor):
        row = await conn.fetchrow(
            """SELECT p.* FROM projects p JOIN project_memberships m ON m.project_id=p.id
               WHERE p.id=$1 AND m.clerk_user_id=$2""",
            project_id,
            actor,
        )
        if row is None:
            raise LookupError("project not found")
        return row

    async def require_admin(self, conn, workspace_id, actor, *, lock=False):
        account = await conn.fetchrow(
            """SELECT a.* FROM billing_accounts a
               WHERE a.workspace_id=$1 AND a.admin_clerk_user_id=$2
                 AND EXISTS (SELECT 1 FROM workspace_memberships m
                             WHERE m.workspace_id=a.workspace_id AND m.clerk_user_id=$2)""",
            workspace_id,
            actor,
        )
        if account is None:
            raise LookupError("billing account not found")
        if lock:
            account = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE", workspace_id
            )
        return account

    async def require_spending_actor(self, conn, run, project_id):
        actor = run["started_by_clerk_user_id"]
        if not actor and run["trigger_source"] == "schedule":
            actor = await conn.fetchval(
                """SELECT created_by_clerk_user_id FROM project_workflows
                   WHERE id=$1 AND project_id=$2 AND status='active'""",
                run["project_workflow_id"],
                project_id,
            )
        return await self.require_project(conn, project_id, actor)

    async def enroll_test(self, workspace_id, actor):
        """Explicit operator/pilot action, never implicit first-payer ownership."""
        if not getattr(self.settings, "billing_test_enabled", False):
            raise BillingError("billing_disabled", "Billing test mode is not enabled.")
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.fetchval(
                "SELECT id FROM workspaces WHERE id=$1 FOR UPDATE",
                workspace_id,
            )
            if await self.workspace_billing_admin(conn, workspace_id) != actor:
                raise LookupError("billing administrator not found")
            existing = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1", workspace_id
            )
            if existing and existing["admin_clerk_user_id"] != actor:
                raise BillingError("billing_admin_exists", "A billing admin is already assigned.")
            await conn.execute(
                """INSERT INTO billing_accounts(workspace_id, mode, admin_clerk_user_id)
                   VALUES($1,'test',$2) ON CONFLICT(workspace_id) DO UPDATE
                   SET run_billing_enabled=true WHERE billing_accounts.workspace_id=$1""",
                workspace_id,
                actor,
            )
        return {"workspace_id": str(workspace_id), "mode": "test"}

    async def update_policy(self, project_id, actor, policy: ProjectSpendingPolicy):
        if not policy.per_run_nanos or not policy.monthly_nanos or policy.schedule_max_nanos == 0:
            raise BillingError("invalid_limits", "Spending limits must be positive.", 422)
        async with self.db.pool.acquire() as conn, conn.transaction():
            workspace_id = await conn.fetchval(
                "SELECT workspace_id FROM projects WHERE id=$1", project_id
            )
            await self.require_admin(conn, workspace_id, actor, lock=True)
            old = await conn.fetchrow(
                "SELECT * FROM billing_project_policies WHERE project_id=$1", project_id
            )
            if policy.expected_revision != (old["revision"] if old else 0):
                raise BillingError("stale_policy", "Limits changed. Review the latest values.")
            await conn.execute(
                """INSERT INTO billing_project_policies(project_id, workspace_id, per_run_nanos,
                       monthly_nanos, concurrency, schedule_max_nanos)
                   VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(project_id) DO UPDATE
                   SET per_run_nanos=$3, monthly_nanos=$4, concurrency=$5, schedule_max_nanos=$6,
                       revision=billing_project_policies.revision+1, updated_at=now()
                   WHERE billing_project_policies.project_id=$1""",
                project_id,
                workspace_id,
                policy.per_run_nanos,
                policy.monthly_nanos,
                policy.concurrency,
                policy.schedule_max_nanos,
            )
        return {"revision": policy.expected_revision + 1}

    def terms(self, definition, project_id, inputs=None, *, session_budget=True):
        return configured_terms(
            self._terms(definition, project_id, inputs, session_budget=session_budget),
            definition,
            inputs,
        )

    def _terms(self, definition, project_id, inputs=None, *, session_budget=True):
        from tin_lite.codex_api import supports_api_definition
        from tin_lite.free_workflows import onboarding_is_free
        from tin_lite.service_pricing import service_terms

        if definition.get("executor") == "workflow.code":
            from tin_lite.workflow_code import POLICY, validate_code_definition

            if validate_code_definition(definition).model_routes:
                from tin_lite.code_models import model_terms

                return model_terms(definition)
            return {
                "rate_card": POLICY,
                "kind": "included",
                "mode": "test",
                "currency": "USD",
                "maximum_nanos": 0,
                "definition_sha256": digest(definition),
            }
        if onboarding_is_free(definition):
            return {
                "rate_card": "tin-funded-onboarding-v1",
                "kind": "included",
                "mode": "test",
                "currency": "USD",
                "maximum_nanos": 0,
                "definition_sha256": digest(definition),
            }
        native = service_terms(definition, inputs=inputs)
        if native is not None:
            if native["kind"] == "parent":
                native["codex_api_children"] = codex_api_enabled(self.settings, project_id)
                if not native["codex_api_children"] and (
                    definition["executor"] == "growth.onboarding"
                    or (inputs or {}).get("technical_fix")
                    or definition.get("organic_system_policy", {}).get("version")
                    == "organic-traffic-v2"
                ):
                    raise BillingError(
                        "unmetered_profile", "This system needs API-billed Codex execution enabled."
                    )
            return native
        if definition.get("executor") == "outreach.email_campaign":
            return {
                "rate_card": "tin-connected-email-v1",
                "kind": "included",
                "mode": "test",
                "currency": "USD",
                "maximum_nanos": 0,
                "definition_sha256": digest(definition),
            }
        if supports_api_definition(definition) and codex_api_enabled(self.settings, project_id):
            from tin_lite.codex_api_pricing import api_terms

            return api_terms(definition, session_budget=session_budget)
        return test_terms(definition)

    async def quote(
        self,
        *,
        runtime,
        project_id,
        actor,
        workflow_id=None,
        inputs=None,
        project_workflow_id=None,
        _review_source_run_id=None,
        preview_only=False,
    ):
        async with self.db.pool.acquire() as conn:
            project = await self.require_project(conn, project_id, actor)
            account = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1", project["workspace_id"]
            )
        if not account or not account["run_billing_enabled"]:
            return {"enabled": False, "mode": "disabled"}
        configured = None
        if _review_source_run_id is not None:
            source = await self.db.get_run(_review_source_run_id)
            if source is None or source.project_id != project_id:
                raise LookupError("review not found")
            # A revision belongs to its saved card, but is not a fresh occurrence of
            # today's (possibly edited or archived) configuration.
            workflow_id, inputs = source.workflow_id, source.input
            project_workflow_id = source.project_workflow_id
        elif project_workflow_id:
            configured = await self.db.get_project_workflow(project_workflow_id)
            if configured is None or configured.project_id != project_id:
                raise LookupError("saved workflow not found")
            workflow_id, inputs = configured.workflow_id, configured.inputs
        workflow = await self.db.get_workflow(workflow_id)
        if workflow is None or workflow.project_id not in {None, project_id}:
            raise LookupError("workflow not found")
        workflow = await resolve_execution_contract(
            storage=runtime.storage,
            workflow=workflow,
            project_id=project_id,
            revision=configured.definition_commit_sha if configured else None,
            input_schema=configured.input_schema if configured else None,
        )
        normalized = normalize_workflow_inputs(
            schema=workflow.definition["input_schema"], project_id=project_id, inputs=inputs
        )
        terms = self.terms(workflow.definition, project_id, normalized)
        if terms["kind"] == "included":
            return {
                "enabled": False,
                "mode": "test",
                "maximum_usd": "0.00",
                "estimated_usd": "0.00",
                "approval_required": False,
                "notice": (
                    "Onboarding is free. No credits are reserved or deducted."
                    if terms["rate_card"] == "tin-funded-onboarding-v1"
                    else "Code-only execution is included; no credits are reserved or deducted."
                    if terms["rate_card"] == "bounded-code-v1"
                    else "No Tin model or delivery fee; uses your connected mailbox."
                ),
            }
        preview = {
            "enabled": True,
            "mode": "test",
            "currency": "USD",
            "maximum_usd": usd(terms["maximum_nanos"]),
            "estimated_usd": usd(
                terms.get("estimate", {}).get("amount_nanos", terms["maximum_nanos"])
            ),
            "estimate": terms.get("estimate"),
            "approval_required": False,
            "rate_card": terms["rate_card"],
            "notice": (
                "Configured spending maximum, not a measured estimate. "
                "Only actual verified usage is charged."
            ),
        }
        if preview_only:
            return preview
        quote_id, expires = uuid4(), datetime.now(UTC) + timedelta(minutes=10)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.require_project(conn, project_id, actor)
            await conn.execute(
                """INSERT INTO billing_quotes(id, project_id, actor_clerk_user_id, workflow_id,
                   project_workflow_id, definition_revision, input_sha256, terms,
                   maximum_nanos, expires_at)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)""",
                quote_id,
                project_id,
                actor,
                workflow.id,
                project_workflow_id,
                workflow.current_commit_sha,
                digest(normalized),
                json.dumps(terms),
                terms["maximum_nanos"],
                expires,
            )
        return {
            **preview,
            "id": str(quote_id),
            "mode": "test",
            "currency": "USD",
            "maximum_usd": usd(terms["maximum_nanos"]),
            "expires_at": expires.isoformat(),
            "rate_card": terms["rate_card"],
            "terms": terms,
            "notice": (
                "Test funds; published OpenAI API rates, no markup or sandbox fee. "
                "Only verified usage is charged, up to this maximum."
                if terms["kind"] == "codex_api"
                else "Test funds; verified model and data-provider usage at the quoted rates. "
                "Child steps share this maximum; no orchestration fee."
                if "service_pricing" in terms
                else "Test funds and illustrative rates; no real customer charge."
            ),
        }

    async def admit(self, conn, *, run, definition, quote_id=None, parent_id=None):
        """Runs inside Database.create_run's transaction, including all alternate start paths."""
        if await self.admit_included(conn, run=run, definition=definition, parent_id=parent_id):
            if quote_id:
                raise BillingError("stale_quote", "This included workflow needs no paid quote.")
            return
        project = await conn.fetchrow(
            "SELECT workspace_id FROM projects WHERE id=$1", run["project_id"]
        )
        await self.ensure_hosted_project(conn, run["project_id"])
        account = await conn.fetchrow(
            "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
            project["workspace_id"],
        )
        if not account or not account["run_billing_enabled"]:
            if definition.get("executor") == "workflow.code" or getattr(
                self.settings, "billing_hosted_defaults_enabled", False
            ):
                # Included code was admitted above. Hosted managed model calls must
                # not fall through to legacy unenrolled, unbilled execution.
                raise BillingError(
                    "billing_not_enrolled",
                    "Workflow billing needs an administrator to complete setup. "
                    "Your credits are unchanged; contact support.",
                )
            if quote_id:
                raise BillingError(
                    "billing_not_enrolled", "Billing is not enabled for this workspace."
                )
            return
        if definition.get("executor") == "outreach.email_campaign":
            if quote_id:
                raise BillingError("stale_quote", "This email workflow does not need a paid quote.")
            return
        if (
            not getattr(self.settings, "billing_test_enabled", False)
            or account["status"] != "active"
        ):
            raise BillingError("billing_paused", "New paid runs are paused.")
        if parent_id:
            # Only trusted fixed-parent code passes this argument; it is never a client field.
            parent = await conn.fetchrow(
                """SELECT b.*, r.executor, r.status AS run_status FROM billing_run_budgets b
                   JOIN workflow_runs r ON r.id=b.run_id
                   WHERE b.run_id=$1 AND b.project_id=$2""",
                parent_id,
                run["project_id"],
            )
            if parent is None:
                historical = await conn.fetchrow(
                    """SELECT id AS run_id, executor, status AS run_status
                       FROM workflow_runs WHERE id=$1 AND project_id=$2""",
                    parent_id,
                    run["project_id"],
                )
                if (
                    historical
                    and historical["run_status"] in {"pending", "running"}
                    and await self.valid_child(conn, historical, run, definition)
                ):
                    # A pre-enrollment parent must not acquire retroactive liability.
                    return
            if (
                not parent
                or parent["run_status"] not in {"running", "pending"}
                or parent["status"] != "reserved"
                or not await self.valid_child(conn, parent, run, definition)
            ):
                raise BillingError(
                    "invalid_child_budget", "The parent spending allocation is unavailable."
                )
            parent_terms = object_value(parent["terms"])
            root = await conn.fetchrow(
                "SELECT status FROM billing_run_budgets WHERE run_id=$1", parent["root_run_id"]
            )
            if root["status"] != "reserved":
                raise BillingError("invalid_child_budget", "The parent budget is no longer active.")
            profile = definition.get("procedure", {}).get("sandbox", {}).get("profile", "default")
            if definition.get("executor") == "codex.procedure" and profile in {
                "default",
                "isolated",
            }:
                from tin_lite.codex_api_pricing import api_terms

                if parent_terms.get("codex_api_children"):
                    terms = api_terms(definition)
                else:
                    terms = test_terms(definition)
            else:
                terms = self.terms(definition, run["project_id"], object_value(run["input"]))
            if "service_pricing" in terms:
                terms["service_pricing"] = parent_terms["service_pricing"]
            if incremental(parent_terms):
                terms = configured_terms(terms, definition, object_value(run["input"]))
            else:
                terms.pop("funding", None)
                terms.pop("estimate", None)
            if terms["kind"] == "parent":
                terms["codex_api_children"] = parent_terms.get("codex_api_children", False)
            await self._insert_budget(
                conn,
                run,
                parent["workspace_id"],
                parent["root_run_id"],
                terms,
                parent["maximum_nanos"],
                None,
                parent["period_start"],
            )
            return
        terms = self.terms(definition, run["project_id"], object_value(run["input"]))
        if run["trigger_source"] == "schedule":
            await self.require_spending_actor(conn, run, run["project_id"])
            # Standing monetary authority belongs to an explicitly configured policy.
            policy = await conn.fetchrow(
                "SELECT * FROM billing_project_policies WHERE project_id=$1", run["project_id"]
            )
            if not policy or (policy["schedule_max_nanos"] or 0) < terms["maximum_nanos"]:
                raise BillingError(
                    "schedule_not_funded",
                    "This schedule has no sufficient standing spending limit.",
                )
        else:
            await self.require_project(conn, run["project_id"], run["started_by_clerk_user_id"])
            quote = (
                await conn.fetchrow("SELECT * FROM billing_quotes WHERE id=$1", quote_id)
                if quote_id
                else None
            )
            if quote_id and not quote:
                raise BillingError(
                    "stale_quote", "This quote is unavailable. Start again without a quote.", 402
                )
            quoted_terms = object_value(quote["terms"]) if quote else None
            # A still-valid pre-session quote keeps its original runtime and funding.
            if quoted_terms and session_funded(terms) and not session_funded(quoted_terms):
                terms = self.terms(
                    definition, run["project_id"], object_value(run["input"]), session_budget=False
                )
            # A still-valid quote issued before isolated procedures joined v3 keeps v1.
            from tin_lite.codex_api_pricing import isolated_v1_terms, issued_before_isolated_v3

            if quoted_terms and issued_before_isolated_v3(quoted_terms, terms, definition):
                terms = configured_terms(
                    isolated_v1_terms(
                        self._terms(
                            definition,
                            run["project_id"],
                            object_value(run["input"]),
                            session_budget=False,
                        )
                    ),
                    definition,
                    object_value(run["input"]),
                )
            # Previously issued quotes preserve their whole-run funding contract.
            if quoted_terms and "funding" not in quoted_terms:
                expected = {k: v for k, v in terms.items() if k not in {"funding", "estimate"}}
            else:
                expected = terms
            if quote and (
                quote["project_id"] != run["project_id"]
                or quote["actor_clerk_user_id"] != run["started_by_clerk_user_id"]
                or quote["workflow_id"] != run["workflow_id"]
                or quote["project_workflow_id"] != run["project_workflow_id"]
                or quote["definition_revision"] != run["definition_commit_sha"]
                or quote["input_sha256"] != digest(object_value(run["input"]))
                or quoted_terms != expected
                or quote["expires_at"] <= datetime.now(UTC)
            ):
                raise BillingError(
                    "stale_quote", "This quote changed or expired. Request a new quote."
                )
            if await conn.fetchval(
                "SELECT run_id FROM billing_run_budgets WHERE quote_id=$1", quote_id
            ):
                raise BillingError(
                    "quote_used", "This quote already belongs to a run. Retry its original request."
                )
            if quoted_terms:
                terms = quoted_terms
        maximum = terms["maximum_nanos"]
        estimate = terms.get("estimate", {}).get("amount_nanos", maximum)
        policy = await conn.fetchrow(
            "SELECT * FROM billing_project_policies WHERE project_id=$1", run["project_id"]
        )
        period = datetime.now(UTC).date().replace(day=1)
        usage = await conn.fetchrow(
            """SELECT COALESCE(sum(CASE WHEN status='settled'
                                        AND settled_at >= ($2::date::timestamp AT TIME ZONE 'UTC')
                                        THEN charged_nanos
                                      WHEN status<>'settled'
                                        AND terms->>'funding'='per_operation_v1'
                                        THEN LEAST(maximum_nanos,
                                          ((committed_nanos + CASE WHEN committed_nanos>0
                                            THEN (terms->>'execution_fee_nanos')::bigint ELSE 0 END
                                            + 9999999) / 10000000) * 10000000)
                                      WHEN status<>'settled' THEN maximum_nanos
                                      ELSE 0 END),0) AS exposure,
                      count(*) FILTER(WHERE status<>'settled' AND EXISTS (
                        SELECT 1 FROM billing_run_budgets child
                        JOIN workflow_runs r ON r.id=child.run_id
                        WHERE child.root_run_id=b.root_run_id
                          AND (r.status NOT IN ('succeeded','failed','stopped')
                               OR r.lease_active))) AS active
               FROM billing_run_budgets b WHERE project_id=$1 AND run_id=root_run_id""",
            run["project_id"],
            period,
        )
        if limit := project_limit_message(policy, estimate, usage):
            raise BillingError("project_limit", limit, 402)
        if account["balance_nanos"] - account["reserved_nanos"] < estimate:
            raise BillingError(
                "insufficient_funds",
                f"This workflow is estimated at up to ${usd(estimate)}. "
                f"Available credits: ${usd(account['balance_nanos'] - account['reserved_nanos'])}. "
                "Add credits before starting.",
                402,
            )
        await conn.execute(
            "UPDATE billing_accounts SET reserved_nanos=reserved_nanos+$2 WHERE workspace_id=$1",
            account["workspace_id"],
            liability(terms, 0),
        )
        await self._insert_budget(
            conn, run, account["workspace_id"], run["id"], terms, maximum, quote_id, period
        )

    async def admit_included(self, conn, *, run, definition, parent_id):
        from tin_lite.free_workflows import (
            OPERATION,
            included_execution,
            onboarding_is_free,
            receipt_key,
        )

        parent_included = None
        if parent_id:
            parent_included = await included_execution(self.db, parent_id, conn=conn)
            if parent_included:
                parent = await conn.fetchrow(
                    """SELECT id AS run_id, executor, status AS run_status
                       FROM workflow_runs WHERE id=$1 AND project_id=$2""",
                    parent_id,
                    run["project_id"],
                )
                if (
                    not parent
                    or parent["run_status"] not in {"pending", "running"}
                    or not await self.valid_child(conn, parent, run, definition)
                ):
                    raise BillingError(
                        "invalid_child_budget", "The free setup step is unavailable."
                    )
        code_only = definition.get("executor") == "workflow.code"
        if code_only:
            from tin_lite.workflow_code import validate_code_definition

            code_only = not validate_code_definition(definition).model_routes
        if not parent_included and not code_only and not onboarding_is_free(definition):
            return False
        await self.require_spending_actor(conn, run, run["project_id"])
        api_enabled = (
            parent_included["codex_api"]
            if parent_included
            else codex_api_enabled(self.settings, run["project_id"])
        )
        api_terms = None
        from tin_lite.codex_api import supports_api_definition

        if api_enabled and supports_api_definition(definition):
            from tin_lite.codex_api_pricing import api_terms as priced_api_terms

            api_terms = priced_api_terms(definition)
        key = receipt_key(run["id"])
        await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
        await self.db.complete_effect(
            conn,
            execution_key=key,
            result={
                "run_id": str(run["id"]),
                "project_id": str(run["project_id"]),
                # A billing parent dispatches its prepared child; recovery never starts it.
                "parent_run_id": str(parent_id) if parent_id else None,
                "root_run_id": parent_included["root_run_id"]
                if parent_included
                else str(run["id"]),
                "reason": "bounded-code-v1" if code_only else "onboarding",
                "codex_api": api_enabled if not code_only else False,
                "api_terms": api_terms,
            },
        )
        return True

    async def _insert_budget(
        self, conn, run, workspace_id, root_id, terms, maximum, quote_id, period
    ):
        await conn.execute(
            """INSERT INTO billing_run_budgets(run_id, root_run_id, workspace_id, project_id,
                 quote_id, terms, maximum_nanos, period_start)
                 VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8)""",
            run["id"],
            root_id,
            workspace_id,
            run["project_id"],
            quote_id,
            json.dumps(terms),
            maximum,
            period,
        )

    async def valid_child(self, conn, parent, run, definition):
        """A budget edge follows the fixed recipe or the founder's pinned picks.

        A client-provided key/prefix alone cannot authorize a new paid child.
        Child admission is inside the parent's account-locked transaction.
        """
        key = run["start_idempotency_key"]
        parent_id = parent["run_id"]
        if parent["executor"] == "organic.traffic_system":
            from tin_lite.organic_system import STEPS

            step = next((s for s, workflow in STEPS.items() if workflow == definition["key"]), None)
            if step in {"draft", "delivery"}:
                from tin_lite.organic_system import POLICY

                prepared = await self.db.get_effect(f"traffic:{parent_id}:prepare", conn=conn)
                if (
                    not prepared
                    or prepared.status != "completed"
                    or prepared.result.get("policy") != POLICY
                    or prepared.result["definitions"].get(step) != definition
                ):
                    return False
            return step is not None and key == f"system:{parent_id}:{step}"
        if parent["executor"] != "growth.onboarding":
            return False
        if key == f"onboarding:{parent_id}:plan":
            return definition["key"] == "growth.onboarding_plan"
        from tin_lite.growth_onboarding import (
            chosen_systems,
            picked_actions,
            plan_block,
            plan_picks,
        )
        from tin_lite.growth_onboarding_activities import repaired_action_inputs

        approved = await self.db.get_effect(f"onboarding:{parent_id}:approved_plan", conn=conn)
        if not approved or approved.status != "completed" or not approved.result:
            return False
        text = approved.result["text"]
        picked, offered = plan_picks(text)
        actions = picked_actions(plan_block(text), chosen_systems(picked, offered))
        parent_input = None
        for index, action in enumerate(actions):
            suffix = ":first" if action["mode"] != "once" else ""
            if key != f"onboarding:{parent_id}:setup:{index}:{action['key']}{suffix}":
                continue
            if definition["key"] != action["key"]:
                return False
            if parent_input is None:
                parent_input = object_value(
                    await conn.fetchval("SELECT input FROM workflow_runs WHERE id=$1", parent_id)
                )
            # Setup repairs padded or out-of-set inputs before starting the child; admit the
            # same repaired inputs, or the free onboarding budget refuses its own children.
            repaired, _notes = repaired_action_inputs(
                action, definition["input_schema"], parent_input or {}
            )
            expected = normalize_workflow_inputs(
                schema=definition["input_schema"],
                project_id=run["project_id"],
                inputs=repaired,
            )
            return expected == object_value(run["input"])
        return False

    async def run_operation_exposure(self, conn, run_id):
        """Observed plus uncertain supplier usage for an incrementally funded run.

        Read-only runtime ceiling input, not spending authorization. Every dispatch
        still requires begin_operation's locked wallet/root checks.
        """
        return await conn.fetchval(
            """SELECT COALESCE((SELECT sum(CASE WHEN o.status='pending' THEN o.maximum_nanos
                       ELSE COALESCE(o.observed_nanos,0) END)
                   FROM billing_operations o WHERE o.run_id=b.run_id),0)
               FROM billing_run_budgets b WHERE b.run_id=$1
                 AND b.terms->>'funding'='per_operation_v1'""",
            run_id,
        )

    async def begin_operation(self, conn, *, run_id, operation_id, kind, maximum):
        """Reserve a paid unit before dispatch. An existing intent is not permission to retry."""
        async with conn.transaction():
            budget = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1", run_id
            )
            if not budget:
                return None
            terms = object_value(budget["terms"])
            session = session_funded(terms)
            if session and (budget["root_run_id"] != run_id or kind != "codex_api"):
                raise BillingError(
                    "unmetered_operation", "This session only funds its own model calls."
                )
            # A session's entire authorization is already held. Its calls only lock
            # the run budget; no shared-wallet mutation or repeated funding occurs.
            account = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1"
                if session
                else "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
                budget["workspace_id"],
            )
            root = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1 FOR UPDATE",
                budget["root_run_id"],
            )
            run = await conn.fetchrow(
                """SELECT status, started_by_clerk_user_id, trigger_source, project_workflow_id
                   FROM workflow_runs WHERE id=$1""",
                run_id,
            )
            if (
                not run
                or run["status"] not in {"pending", "running"}
                or root["status"] != "reserved"
                or account["status"] != "active"
                or not getattr(self.settings, "billing_test_enabled", False)
            ):
                raise BillingError("spending_stopped", "No additional paid work is authorized.")
            await self.require_spending_actor(conn, run, budget["project_id"])
            if await conn.fetchval("SELECT id FROM billing_operations WHERE id=$1", operation_id):
                raise BillingError(
                    "operation_already_attempted",
                    "Recover the existing paid operation; do not purchase it again.",
                )
            if kind not in terms.get("operations", [terms["kind"]]):
                raise BillingError(
                    "unmetered_operation", "This operation is outside the quoted profile."
                )
            if session:
                # This is remaining customer authority, not a supplier-cost forecast.
                # One pending response occupies it until verified usage arrives.
                amount = root["maximum_nanos"] - root["committed_nanos"]
                if amount <= 0:
                    pending = await conn.fetchval(
                        "SELECT true FROM billing_operations WHERE root_run_id=$1 "
                        "AND status='pending' LIMIT 1",
                        run_id,
                    )
                    raise BillingError(
                        "usage_pending" if pending else "run_limit",
                        "A prior API request is active or unconfirmed."
                        if pending
                        else "This session has reached its spending maximum.",
                        409 if pending else 402,
                    )
            else:
                amount = maximum(terms) if callable(maximum) else maximum
            if type(amount) is not int or amount < 0:
                raise ValueError("invalid operation maximum")
            own_committed = (
                root["committed_nanos"]
                if session
                else await conn.fetchval(
                    """SELECT COALESCE(sum(CASE WHEN status='pending' THEN maximum_nanos
                    ELSE COALESCE(observed_nanos,0) END),0)
                   FROM billing_operations WHERE run_id=$1""",
                    run_id,
                )
            )
            if own_committed + amount > terms["maximum_nanos"]:
                raise BillingError(
                    "run_limit", "This step has reached its quoted spending maximum.", 402
                )
            policy = await conn.fetchrow(
                "SELECT * FROM billing_project_policies WHERE project_id=$1", budget["project_id"]
            )
            period = datetime.now(UTC).date().replace(day=1)
            exposure = await conn.fetchval(
                """SELECT COALESCE(sum(CASE
                    WHEN status='settled'
                      AND settled_at >= ($2::date::timestamp AT TIME ZONE 'UTC') THEN charged_nanos
                    WHEN status<>'settled' AND terms->>'funding'='per_operation_v1'
                      THEN LEAST(maximum_nanos,
                        ((committed_nanos + CASE WHEN committed_nanos>0
                          THEN (terms->>'execution_fee_nanos')::bigint ELSE 0 END
                          + 9999999) / 10000000) * 10000000)
                    WHEN status<>'settled' THEN maximum_nanos ELSE 0 END),0)
                   FROM billing_run_budgets WHERE project_id=$1 AND run_id=root_run_id""",
                budget["project_id"],
                period,
            )
            root_terms = object_value(root["terms"])
            root_run = (
                run
                if root["run_id"] == run_id
                else await conn.fetchrow(
                    "SELECT trigger_source FROM workflow_runs WHERE id=$1", root["run_id"]
                )
            )
            added_liability = liability(root_terms, root["committed_nanos"] + amount) - liability(
                root_terms, root["committed_nanos"]
            )
            if (
                not policy
                or root["committed_nanos"] + amount + terms["execution_fee_nanos"]
                > policy["per_run_nanos"]
                or exposure + added_liability > policy["monthly_nanos"]
                or (
                    root_run["trigger_source"] == "schedule"
                    and (policy["schedule_max_nanos"] or 0)
                    < (
                        liability(root_terms, root["committed_nanos"] + amount)
                        if incremental(root_terms)
                        else root["maximum_nanos"]
                    )
                )
            ):
                raise BillingError(
                    "project_limit", "Current project limits block further spending."
                )
            if (
                root["committed_nanos"] + amount + terms["execution_fee_nanos"]
                > root["maximum_nanos"]
            ):
                raise BillingError(
                    "run_limit", "The next paid operation would exceed this run's maximum.", 402
                )
            if incremental(root_terms):
                if account["balance_nanos"] - account["reserved_nanos"] < added_liability:
                    raise BillingError(
                        "insufficient_funds",
                        "Not enough credits for the next paid step. No new call was made; "
                        "completed work is preserved. Add credits before further paid work.",
                        402,
                    )
                await conn.execute(
                    """UPDATE billing_accounts SET reserved_nanos=reserved_nanos+$2
                       WHERE workspace_id=$1""",
                    root["workspace_id"],
                    added_liability,
                )
            await conn.execute(
                "UPDATE billing_run_budgets SET committed_nanos=committed_nanos+$2 WHERE run_id=$1",
                root["run_id"],
                amount,
            )
            await conn.execute(
                """INSERT INTO billing_operations(id, run_id, root_run_id, kind, maximum_nanos)
                   VALUES($1,$2,$3,$4,$5)""",
                operation_id,
                run_id,
                root["run_id"],
                kind,
                amount,
            )
            return terms

    async def observe_operation(self, conn, *, operation_id, nanos, observation):
        if nanos is None:
            return  # A missing supplier response retains its justified pending liability.
        if type(nanos) is not int or nanos < 0:
            raise ValueError("invalid observation")
        async with conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM billing_operations WHERE id=$1", operation_id
            )
            if not operation:
                return
            workspace_id = await conn.fetchval(
                "SELECT workspace_id FROM billing_run_budgets WHERE run_id=$1",
                operation["root_run_id"],
            )
            terms = object_value(
                await conn.fetchval(
                    "SELECT terms FROM billing_run_budgets WHERE run_id=$1",
                    operation["root_run_id"],
                )
            )
            # Every wallet mutation follows account -> root -> operation lock order.
            # Session observations only change their already-funded root and receipt.
            if not session_funded(terms):
                await conn.fetchrow(
                    "SELECT workspace_id FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
                    workspace_id,
                )
            root = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1 FOR UPDATE",
                operation["root_run_id"],
            )
            operation = await conn.fetchrow(
                "SELECT * FROM billing_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if operation["status"] != "pending" or root["status"] == "settled":
                return
            # Supplier overage is Tin's loss, not a larger customer authorization.
            billable = min(nanos, operation["maximum_nanos"])
            terms = object_value(root["terms"])
            if incremental(terms):
                delta = liability(
                    terms, root["committed_nanos"] - operation["maximum_nanos"] + billable
                ) - liability(terms, root["committed_nanos"])
                await conn.execute(
                    """UPDATE billing_accounts SET reserved_nanos=reserved_nanos+$2
                       WHERE workspace_id=$1""",
                    workspace_id,
                    delta,
                )
            await conn.execute(
                """UPDATE billing_operations SET status='observed', observed_nanos=$2,
                   observation=$3::jsonb WHERE id=$1 AND status='pending'""",
                operation_id,
                billable,
                json.dumps({**observation, "overage_absorbed_nanos": nanos - billable}),
            )
            await conn.execute(
                """UPDATE billing_run_budgets SET committed_nanos=committed_nanos-$2+$3
                   WHERE run_id=$1""",
                root["run_id"],
                operation["maximum_nanos"],
                billable,
            )

    async def settle(self, root_id):
        async with self.db.pool.acquire() as conn, conn.transaction():
            initial = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1 AND root_run_id=$1", root_id
            )
            if not initial:
                return None
            account = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
                initial["workspace_id"],
            )
            root = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1 FOR UPDATE", root_id
            )
            if root["status"] == "settled":
                return root["charged_nanos"]
            # ads.launch makes its metered Google Ads calls after approval, so its budget
            # must stay open while it waits for review.
            active = await conn.fetchval(
                """SELECT true FROM billing_run_budgets b JOIN workflow_runs r ON r.id=b.run_id
                   WHERE b.root_run_id=$1
                     AND (r.status IN ('pending','running','paused')
                          OR (r.status='needs_input'
                              AND (b.terms->>'kind'='parent'
                                   OR r.executor IN ('project.task', 'ads.launch')))
                          OR (r.lease_active AND r.status<>'needs_input'))
                   LIMIT 1""",
                root_id,
            )
            if active:
                return None
            # The usage receipt is durable before its billing projection. Repair that
            # write gap without contacting the provider or buying another attempt.
            observations = await conn.fetch(
                """SELECT o.id, o.kind, o.run_id, e.operation, e.result, b.terms
                   FROM billing_operations o
                   JOIN effect_receipts e ON e.execution_key=o.id
                   JOIN billing_run_budgets b ON b.run_id=o.run_id
                   WHERE o.root_run_id=$1 AND o.status='pending' AND e.result IS NOT NULL""",
                root_id,
            )
            expected_operations = {
                "native_model": {"native_model_usage_v1", "external_usage_v1"},
                "isolated_codex": {"isolated_codex_attempt_v1"},
                "codex_api": {"codex_api_usage_v1"},
                "tool": {"external_usage_v1"},
            }
            for item in observations:
                record = object_value(item["result"])
                if item["operation"] not in expected_operations.get(
                    item["kind"], set()
                ) or record.get("run_id") != str(item["run_id"]):
                    continue
                priced = receipt_charge(object_value(item["terms"]), item["kind"], record)
                if priced is not None:
                    await self.observe_operation(
                        conn, operation_id=item["id"], nanos=priced[0], observation=priced[1]
                    )
            operations = await conn.fetch(
                "SELECT * FROM billing_operations WHERE root_run_id=$1", root_id
            )
            root = await conn.fetchrow("SELECT * FROM billing_run_budgets WHERE run_id=$1", root_id)
            unknown = any(op["status"] == "pending" for op in operations)
            if unknown and datetime.now(UTC) < root["reconcile_by"]:
                await conn.execute(
                    """UPDATE billing_run_budgets SET status='pending'
                       WHERE root_run_id=$1 AND status<>'settled'""",
                    root_id,
                )
                return None
            terms = object_value(root["terms"])
            subtotal = sum(op["observed_nanos"] or 0 for op in operations)
            # No paid operation, no fee. Unknown-only work is not converted into a charge.
            subtotal += (
                terms["execution_fee_nanos"]
                if (subtotal > 0 or not incremental(terms))
                and any(op["status"] == "observed" for op in operations)
                else 0
            )
            charge = final_charge(subtotal, root["maximum_nanos"])
            await self.post_ledger(
                conn,
                account=account,
                event_key=f"run:{root_id}:charge",
                kind="charge",
                amount=-charge,
                project_id=root["project_id"],
                run_id=root_id,
            )
            # Spend nonrefundable welcome credits first, then paid top-ups oldest-first.
            # A later welcome grant must not make previously spent cash refundable.
            from tin_lite.billing_contracts import NANOS_PER_CENT

            welcome_used = min(charge, account["welcome_remaining_nanos"])
            if welcome_used:
                await conn.execute(
                    """UPDATE billing_accounts
                       SET welcome_remaining_nanos=welcome_remaining_nanos-$2
                       WHERE workspace_id=$1""",
                    root["workspace_id"],
                    welcome_used,
                )
            remaining = (charge - welcome_used) // NANOS_PER_CENT
            payments = await conn.fetch(
                """SELECT * FROM billing_payments WHERE workspace_id=$1 AND status='paid'
                   ORDER BY created_at, id FOR UPDATE""",
                root["workspace_id"],
            )
            for payment in payments:
                available = max(
                    0,
                    payment["amount_cents"]
                    - payment["consumed_cents"]
                    - payment["refunded_cents"]
                    - payment["refund_reserved_cents"],
                )
                used = min(available, remaining)
                if used:
                    await conn.execute(
                        "UPDATE billing_payments SET consumed_cents=consumed_cents+$2 WHERE id=$1",
                        payment["id"],
                        used,
                    )
                    remaining -= used
                if not remaining:
                    break
            await conn.execute(
                """UPDATE billing_accounts SET reserved_nanos=reserved_nanos-$2
                   WHERE workspace_id=$1""",
                root["workspace_id"],
                liability(terms, root["committed_nanos"]),
            )
            await conn.execute(
                """UPDATE billing_operations SET status='absorbed'
                   WHERE root_run_id=$1 AND status='pending'""",
                root_id,
            )
            await conn.execute(
                """UPDATE billing_run_budgets SET status='settled',
                   charged_nanos=CASE WHEN run_id=$1 THEN $2::bigint ELSE 0 END,
                   settled_at=now() WHERE root_run_id=$1 AND status<>'settled'""",
                root_id,
                charge,
            )
            return charge

    async def post_ledger(
        self,
        conn,
        *,
        account,
        event_key,
        kind,
        amount,
        project_id=None,
        run_id=None,
        reference=None,
    ):
        """Caller holds account FOR UPDATE in the surrounding transaction."""
        prior = await conn.fetchrow("SELECT * FROM billing_ledger WHERE event_key=$1", event_key)
        if prior:
            if prior["workspace_id"] != account["workspace_id"] or prior["amount_nanos"] != amount:
                raise BillingError(
                    "ledger_conflict", "This financial event has different recorded terms."
                )
            return False
        balance = await conn.fetchval(
            """UPDATE billing_accounts SET balance_nanos=balance_nanos+$2
               WHERE workspace_id=$1 RETURNING balance_nanos""",
            account["workspace_id"],
            amount,
        )
        await conn.execute(
            """INSERT INTO billing_ledger(workspace_id, project_id, run_id, event_key, kind,
                   amount_nanos, balance_after_nanos, reference) VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
            account["workspace_id"],
            project_id,
            run_id,
            event_key,
            kind,
            amount,
            balance,
            reference,
        )
        return True

    async def reconcile(self):
        rows = await self.db.pool.fetch(
            """SELECT b.run_id FROM billing_run_budgets b
               WHERE b.run_id=b.root_run_id AND b.status<>'settled'
                 AND NOT EXISTS (SELECT 1 FROM billing_run_budgets child
                     JOIN workflow_runs r ON r.id=child.run_id
                     WHERE child.root_run_id=b.run_id
                       AND (r.status IN ('pending','running','paused')
                            OR (r.status='needs_input'
                                AND (child.terms->>'kind'='parent'
                                     OR r.executor IN ('project.task', 'ads.launch')))
                            OR (r.lease_active AND r.status<>'needs_input')))
               ORDER BY b.reconcile_by, b.created_at LIMIT 100"""
        )
        for row in rows:
            await self.settle(row["run_id"])

    async def run_charge(self, run_id, actor):
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT project_id, executor FROM workflow_runs WHERE id=$1", run_id
            )
            if not row:
                raise LookupError("run not found")
            await self.require_project(conn, row["project_id"], actor)
            budget = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1", run_id
            )
            if not budget:
                from tin_lite.free_workflows import included_execution

                included = await included_execution(self.db, run_id, conn=conn)
                if included:
                    return {
                        "run_id": str(run_id),
                        "root_run_id": included["root_run_id"],
                        "billing": "included",
                        "reason": included.get("reason", "onboarding"),
                        "charged_usd": "0.00",
                        "maximum_usd": "0.00",
                    }
                if row["executor"] == "outreach.email_campaign":
                    return {"run_id": str(run_id), "billing": "included", "charged_usd": "0.00"}
                return {"run_id": str(run_id), "billing": "not_enrolled", "charged_usd": None}
            root = await conn.fetchrow(
                "SELECT * FROM billing_run_budgets WHERE run_id=$1", budget["root_run_id"]
            )
            terms = object_value(root["terms"])
            return {
                "run_id": str(run_id),
                "root_run_id": str(root["run_id"]),
                "mode": "test",
                "status": "in_progress"
                if (incremental(terms) or session_funded(terms)) and root["status"] == "reserved"
                else root["status"],
                "estimated_usd": usd(
                    terms.get("estimate", {}).get("amount_nanos", root["maximum_nanos"])
                ),
                "usage_so_far_usd": usd(
                    sum(
                        row["observed_nanos"] or 0
                        for row in await conn.fetch(
                            "SELECT observed_nanos FROM billing_operations WHERE root_run_id=$1",
                            root["run_id"],
                        )
                    )
                ),
                "maximum_usd": usd(root["maximum_nanos"]),
                "charged_usd": usd(root["charged_nanos"])
                if root["charged_nanos"] is not None
                else None,
                "released_usd": usd(root["maximum_nanos"] - root["charged_nanos"])
                if root["status"] == "settled" and not (incremental(terms) or session_funded(terms))
                else None,
                "included_in_parent": run_id != root["run_id"],
                "rate_card": object_value(budget["terms"])["rate_card"],
            }

    async def overview(self, project_id, actor):
        async with (
            self.db.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            project = await self.require_project(conn, project_id, actor)
            account = await conn.fetchrow(
                "SELECT * FROM billing_accounts WHERE workspace_id=$1", project["workspace_id"]
            )
            if not account:
                return {"enabled": False, "mode": "disabled"}
            admin = account["admin_clerk_user_id"] == actor and await conn.fetchval(
                "SELECT true FROM workspace_memberships WHERE workspace_id=$1 AND clerk_user_id=$2",
                project["workspace_id"],
                actor,
            )
            policies = await conn.fetch(
                """SELECT p.id, CASE WHEN EXISTS (SELECT 1 FROM project_memberships m
                    WHERE m.project_id=p.id AND m.clerk_user_id=$2)
                    THEN p.name ELSE NULL END AS name,
                    b.revision, b.per_run_nanos, b.monthly_nanos,
                    b.concurrency, b.schedule_max_nanos
                   FROM projects p LEFT JOIN billing_project_policies b ON b.project_id=p.id
                   WHERE p.workspace_id=$1 AND p.deleted_at IS NULL AND ($3 OR p.id=$4)
                   ORDER BY p.created_at, p.id LIMIT 100""",
                project["workspace_id"],
                actor,
                bool(admin),
                project_id,
            )
            transactions = await conn.fetch(
                """SELECT id, kind, amount_nanos, balance_after_nanos, created_at,
                    CASE WHEN project_id=$2 THEN run_id ELSE NULL END AS run_id
                   FROM billing_ledger WHERE workspace_id=$1 AND ($3 OR project_id=$2)
                   ORDER BY id DESC LIMIT 100""",
                project["workspace_id"],
                project_id,
                bool(admin),
            )
            spent = await conn.fetchval(
                """SELECT COALESCE(-sum(amount_nanos),0) FROM billing_ledger WHERE project_id=$1
                   AND kind='charge' AND created_at>=date_trunc('month',
                       now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
                project_id,
            )
            result = {
                "enabled": True,
                "run_billing_enabled": account["run_billing_enabled"],
                "mode": "test",
                "currency": "USD",
                "is_admin": bool(admin),
                "workspace_id": str(project["workspace_id"]),
                "project_id": str(project_id),
                "spent_this_month_usd": usd(spent),
                "status": account["status"],
                "policies": [
                    {
                        **dict(p),
                        "id": str(p["id"]),
                        "name": p["name"] or f"Project {str(p['id'])[:8]}",
                        "per_run_usd": usd(p["per_run_nanos"])
                        if p["per_run_nanos"] is not None
                        else None,
                        "monthly_usd": usd(p["monthly_nanos"])
                        if p["monthly_nanos"] is not None
                        else None,
                        "schedule_max_usd": usd(p["schedule_max_nanos"])
                        if p["schedule_max_nanos"] is not None
                        else None,
                    }
                    for p in policies
                ],
                "transactions": [
                    {
                        "id": t["id"],
                        "kind": t["kind"],
                        "amount_usd": usd(t["amount_nanos"]),
                        "balance_after_usd": usd(t["balance_after_nanos"]) if admin else None,
                        "run_id": str(t["run_id"]) if t["run_id"] else None,
                        "created_at": t["created_at"].isoformat(),
                    }
                    for t in transactions
                ],
            }
            if admin:
                result.update(
                    available_usd=usd(account["balance_nanos"] - account["reserved_nanos"]),
                    reserved_usd=usd(account["reserved_nanos"]),
                    topup_min_cents=1000,
                    topup_max_cents=100000,
                )
            return result
