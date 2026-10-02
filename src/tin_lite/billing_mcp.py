"""The same billing service for agent callers; no separate privileged dispatch."""

from typing import Any
from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from tin_lite.billing import BillingService
from tin_lite.billing_contracts import BillingError, ProjectSpendingPolicy, usd_nanos
from tin_lite.billing_payments import StripePayments


def register_billing_tools(server, *, runtime, settings, caller):
    def service():
        return BillingService(database=runtime().database, settings=settings)

    async def result(call):
        try:
            return await call
        except (BillingError, LookupError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def get_project_spending(project_id: str) -> dict[str, Any]:
        """Read your project's spending, or workspace billing if you are its billing admin.

        spent_this_month_usd is this project's. A billing admin's transactions cover the
        whole workspace wallet; each has a scope: project, other_project or workspace.
        """
        token = await caller()
        return await result(service().overview(UUID(project_id), token.subject))

    async def cost_preview(
        project_id: str,
        workflow_id: str | None = None,
        project_workflow_id: str | None = None,
        inputs: dict | None = None,
        *,
        preview_only: bool,
    ) -> dict[str, Any]:
        from tin_lite.billing_api import QuoteRequest
        from tin_lite.mcp_server import _mcp_bound_inputs

        token = await caller()
        # Same contract as start_workflow: the tool call binds project_id, not inputs.
        inputs = _mcp_bound_inputs(inputs, project_id)
        if workflow_id:
            from tin_lite.mcp_server import _mcp_workflow

            async with runtime().database.pool.acquire() as conn:
                await service().require_project(conn, UUID(project_id), token.subject)
            workflow_id = str(
                _mcp_workflow(
                    await runtime().database.list_workflows(project_id=UUID(project_id)),
                    workflow_id,
                ).id
            )
        payload = QuoteRequest(
            workflow_id=workflow_id, project_workflow_id=project_workflow_id, inputs=inputs or {}
        )
        return await result(
            service().quote(
                runtime=runtime(),
                project_id=UUID(project_id),
                actor=token.subject,
                preview_only=preview_only,
                **payload.model_dump(),
            )
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def estimate_workflow_run(
        project_id: str,
        workflow_id: str | None = None,
        project_workflow_id: str | None = None,
        inputs: dict | None = None,
    ) -> dict[str, Any]:
        """Preview a configuration's reusable cost estimate without paid work or a quote.

        Optional when discussing setup/cost. Normal starts check funds automatically;
        do not call this or ask for billing approval before every run. Use one workflow
        key/UUID or saved configuration UUID. Actual verified usage determines the charge.
        """
        return await cost_preview(
            project_id, workflow_id, project_workflow_id, inputs, preview_only=True
        )

    @server.tool()
    async def quote_workflow_run(
        project_id: str,
        workflow_id: str | None = None,
        project_workflow_id: str | None = None,
        inputs: dict | None = None,
    ) -> dict[str, Any]:
        """Optional compatibility quote. Normal starts do NOT require billing_quote_id.

        Prefer estimate_workflow_run for a read-only setup preview. Do not add a quote
        approval loop to ordinary starts; the backend checks funds and project limits.
        """
        return await cost_preview(
            project_id, workflow_id, project_workflow_id, inputs, preview_only=False
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def get_run_charge(run_id: str) -> dict[str, Any]:
        """Read workflow spending so far or its final charge; no holds need managing."""
        token = await caller()
        return await result(service().run_charge(UUID(run_id), token.subject))

    @server.tool()
    async def set_project_spending_limits(
        project_id: str,
        per_run_usd: str,
        monthly_usd: str,
        expected_revision: int,
        schedule_max_usd: str | None = None,
    ) -> dict[str, Any]:
        """Billing admin only. Set dollar limits, e.g. per_run_usd="10.00".

        A schedule maximum establishes standing authority for future occurrences. Omit to disable.
        """
        token = await caller()
        policy = ProjectSpendingPolicy(
            per_run_nanos=usd_nanos(per_run_usd),
            monthly_nanos=usd_nanos(monthly_usd),
            expected_revision=expected_revision,
            schedule_max_nanos=usd_nanos(schedule_max_usd) if schedule_max_usd else None,
        )
        return await result(service().update_policy(UUID(project_id), token.subject, policy))

    @server.tool()
    async def create_billing_checkout(
        workspace_id: str, amount_cents: int, request_id: str
    ) -> dict[str, Any]:
        """Billing admin: open a hosted Stripe test checkout. Returning does not start a run."""
        token = await caller()
        payments = StripePayments(billing=service(), settings=settings)
        return await result(
            payments.checkout(
                workspace_id=UUID(workspace_id),
                actor=token.subject,
                amount_cents=amount_cents,
                request_id=UUID(request_id),
            )
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_billing_payments(workspace_id: str) -> dict[str, Any]:
        """Billing admin: read test payments, invoices and refundable balances."""
        token = await caller()
        payments = StripePayments(billing=service(), settings=settings)
        return {"payments": await result(payments.list_payments(UUID(workspace_id), token.subject))}

    @server.tool()
    async def enroll_billing_test(workspace_id: str) -> dict[str, Any]:
        """Billing administrator: enable test-only billing; no live charge.

        Hosted Tin enables billing automatically. This explicit operator/pilot control
        changes future admission only; configure limits and funds when not using hosted defaults.
        """
        token = await caller()
        return await result(service().enroll_test(UUID(workspace_id), token.subject))
