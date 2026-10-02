from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_billing import billed as billed
from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import IntegrationConnection, Workflow
from tin_lite.onboarding import onboarding_tin_state, tin_state


def _workflow(builtin) -> Workflow:
    now = datetime.now(UTC)
    definition = builtin.definition
    return Workflow(
        id=builtin.id,
        project_id=None,
        key=builtin.key,
        title=builtin.title,
        description=builtin.description,
        executor=builtin.executor,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha="a" * 40,
        version_label=builtin.version_label,
        definition=definition,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _connection(provider_key: str, status: str = "connected") -> IntegrationConnection:
    now = datetime.now(UTC)
    return IntegrationConnection(
        id=uuid4(),
        project_id=uuid4(),
        provider_key=provider_key,
        status=status,
        external_account_id="acct",
        external_account_label="acct",
        configuration={},
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id="user",
        last_checked_at=None,
        last_error_code=None,
        created_at=now,
        updated_at=now,
    )


@dataclass
class _Settings:
    dataforseo_login: str | None = None
    dataforseo_password: str | None = None
    luna_api_key: str | None = None
    organic_audit_max_cost_usd: float = 0
    keyword_plan_max_cost_usd: float = 0
    content_plan_max_cost_usd: float = 0


WORKFLOWS = [_workflow(item) for item in BUILTIN_WORKFLOWS]


def _rows(state):
    return {row["key"]: row for row in state["workflows"]}


def test_tin_state_mirrors_the_start_gates_when_nothing_is_configured() -> None:
    state = tin_state(settings=_Settings(), workflows=WORKFLOWS, connections=[])
    rows = _rows(state)

    assert "growth.onboarding" not in rows
    assert "growth.onboarding_plan" not in rows
    # The AI visibility audit is folded into the organic audit and left out of discovery.
    assert "visibility.audit" not in rows
    assert rows["project.task"]["runnable"] is True and rows["project.task"]["kind"] == "task"
    assert rows["organic.audit"]["runnable"] is False
    assert "DataForSEO" in rows["organic.audit"]["reason"]
    assert rows["organic.audit"]["unblock"]["kind"] == "tin_operator"
    assert rows["content.plan"]["unblock"] is None
    assert rows["website.change"]["unblock"]["kind"] == "connect_integration"
    # Site health is folded into the technical fix, and the technical fix into website.change;
    # both are left out of discovery.
    assert "site.health_improve" not in rows
    assert "organic.technical_fix" not in rows
    audit_prerequisites = rows["qa.product_audit"]["prerequisites"]
    assert any(
        item["level"] == "required" and item.get("producer") == "qa.signup_walkthrough"
        for item in audit_prerequisites
    )
    assert all(
        item["level"] in {"required", "recommended"}
        for item in rows["content.refresh"]["prerequisites"]
    )
    assert rows["organic.keyword_plan"]["runnable"] is False
    assert rows["organic.traffic_system"]["runnable"] is False
    assert rows["content.plan"]["reason"] is None
    assert rows["website.change"]["runnable"] is False
    assert rows["website.change"]["reason"] == "Connect infra.github first."
    assert rows["website.change"]["requires_integrations"] == ["infra.github"]
    assert rows["outreach.email_campaign"]["reason"] == "Connect workspace.google first."
    assert rows["organic.audit"]["required_inputs"] == ["site_url", "market"]
    assert rows["research.deep_dive"]["schedule_modes"] == ["on_demand", "daily", "weekly"]
    assert rows["organic.audit"]["schedule_modes"] == ["on_demand"]
    assert "notes" in rows["qa.signup_walkthrough"]["optional_inputs"]
    assert {item["provider_key"]: item["connected"] for item in state["integrations"]} == {
        "analytics.gsc": False,
        "infra.github": False,
        "workspace.google": False,
        "ads.google": False,
        "payments.stripe": False,
        "analytics.posthog": False,
        "social.x": False,
        "infra.github_user": False,
    }
    assert state["running"] == [] and state["recent_runs"] == []


def test_tin_state_lists_what_already_runs() -> None:
    audit = next(item for item in WORKFLOWS if item.key == "visibility.audit")
    saved = SimpleNamespace(
        workflow_id=audit.id,
        workflow_key="visibility.audit",
        name="Audit AI visibility — onboarding L2",
        schedule={"cadence": "weekly", "weekdays": ["monday"], "local_time": "09:00"},
        next_run_at=datetime(2026, 9, 14, 16, tzinfo=UTC),
        status="active",
    )
    archived = SimpleNamespace(**{**saved.__dict__, "status": "archived"})
    run = SimpleNamespace(
        workflow_id=audit.id,
        status="succeeded",
        artifact_path="reports/AI_VISIBILITY.md",
        finished_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
    )
    onboarding = next(item for item in WORKFLOWS if item.key == "growth.onboarding")
    own = SimpleNamespace(workflow_id=onboarding.id, status="succeeded", artifact_path=None)
    state = tin_state(
        settings=_Settings(),
        workflows=WORKFLOWS,
        connections=[],
        project_workflows=[saved, archived],
        recent_runs=[run, own],
    )
    assert state["running"] == [
        {
            "key": "visibility.audit",
            "name": "Audit AI visibility — onboarding L2",
            "cadence": "weekly",
            "weekdays": ["monday"],
            "local_time": "09:00",
            "next_run_at": "2026-09-14T16:00:00+00:00",
            "status": "active",
        }
    ]
    assert state["recent_runs"] == [
        {
            "key": "visibility.audit",
            "status": "succeeded",
            "artifact_path": "reports/AI_VISIBILITY.md",
            "finished_at": "2026-09-11T12:00:00+00:00",
        }
    ]


def test_tin_state_opens_doors_as_settings_and_connections_arrive() -> None:
    settings = _Settings(
        dataforseo_login="login",
        dataforseo_password="secret",  # noqa: S106
        luna_api_key="key",
        organic_audit_max_cost_usd=1,
        keyword_plan_max_cost_usd=9,
        content_plan_max_cost_usd=1,
    )
    connections = [_connection("infra.github"), _connection("workspace.google", status="attention")]
    state = tin_state(settings=settings, workflows=WORKFLOWS, connections=connections)
    rows = _rows(state)

    assert rows["organic.audit"]["runnable"] is True
    assert rows["organic.keyword_plan"]["runnable"] is True
    assert rows["organic.traffic_system"]["runnable"] is True
    assert rows["website.change"]["runnable"] is True
    # A connection that needs attention is not connected.
    assert rows["outreach.email_campaign"]["runnable"] is False
    assert {item["provider_key"]: item["connected"] for item in state["integrations"]} == {
        "analytics.gsc": False,
        "infra.github": True,
        "workspace.google": False,
        "ads.google": False,
        "payments.stripe": False,
        "analytics.posthog": False,
        "social.x": False,
        "infra.github_user": False,
    }


def test_tin_state_orders_by_system_then_key_and_skips_private_workflows() -> None:
    ordered = []
    for item in WORKFLOWS:
        ordered.append(
            Workflow(
                **{
                    **item.__dict__,
                    "system_order": {"organic-traffic": 1, "cold-outreach": 2, "paid-ads": 3}.get(
                        item.definition.get("system")
                    ),
                }
            )
        )
    private = Workflow(**{**WORKFLOWS[0].__dict__, "project_id": uuid4(), "key": "custom.mine"})
    state = tin_state(settings=_Settings(), workflows=[private, *ordered], connections=[])
    keys = [row["key"] for row in state["workflows"]]
    assert "custom.mine" not in keys
    systems = [row["system"] for row in state["workflows"]]
    first_unassigned = systems.index(None) if None in systems else len(systems)
    assert all(system is not None for system in systems[:first_unassigned])
    assert keys[:first_unassigned] == sorted(
        keys[:first_unassigned],
        key=lambda key: (
            {"organic-traffic": 1, "cold-outreach": 2, "paid-ads": 3}[_rows(state)[key]["system"]],
            key,
        ),
    )


@pytest.mark.asyncio
async def test_onboarding_tin_state_reads_the_project_scope() -> None:
    seen = {}

    class _Database:
        async def list_prerequisite_runs(self, **kwargs):
            return []

        async def list_test_identities(self, **kwargs):
            return []

        async def list_workflows(self, *, project_id):
            seen["workflows"] = project_id
            return WORKFLOWS

        async def list_integration_connections(self, project_id):
            seen["connections"] = project_id
            return [_connection("workspace.google")]

        async def list_project_workflows(self, *, project_id):
            seen["project_workflows"] = project_id
            return []

        async def list_runs(self, *, project_id, limit):
            seen["runs"] = (project_id, limit)
            return []

    project_id = uuid4()
    state = await onboarding_tin_state(
        database=_Database(), settings=SimpleNamespace(), project_id=project_id
    )
    assert seen == {
        "workflows": project_id,
        "connections": project_id,
        "project_workflows": project_id,
        "runs": (project_id, 20),
    }
    assert _rows(state)["outreach.email_campaign"]["runnable"] is False
    assert _rows(state)["outreach.email_campaign"]["readiness"]["state"] == "blocked"


async def test_content_plan_readiness_uses_durable_prerequisites():
    from unittest.mock import AsyncMock

    plan = next(w for w in WORKFLOWS if w.key == "content.plan")
    database = SimpleNamespace(
        list_workflows=AsyncMock(return_value=[plan]),
        list_integration_connections=AsyncMock(return_value=[]),
        list_project_workflows=AsyncMock(return_value=[]),
        list_runs=AsyncMock(return_value=[]),
        list_prerequisite_runs=AsyncMock(return_value=[]),
    )
    pid = uuid4()
    before = await onboarding_tin_state(database=database, settings=_Settings(), project_id=pid)
    assert not _rows(before)["content.plan"]["runnable"]
    database.list_prerequisite_runs.return_value = [
        ("organic.audit", object()),
        ("organic.keyword_plan", object()),
    ]
    after = await onboarding_tin_state(database=database, settings=_Settings(), project_id=pid)
    assert _rows(after)["content.plan"]["runnable"]
    assert _rows(after)["content.plan"]["readiness"]["state"] == "ready"
    assert _rows(after)["content.plan"]["prerequisites"][0]["via_input"] == "audit_run_id"


async def test_onboarding_guidance_is_free_even_in_an_enrolled_workspace(billed, monkeypatch):
    from unittest.mock import AsyncMock

    from test_private_workflows import mcp, structured

    from tin_lite.onboarding import billing_restrictions

    f = billed
    onboarding = next(w for w in WORKFLOWS if w.key == "growth.onboarding")
    blocked = await billing_restrictions(
        database=f.db, project_id=f.project.id, workflows=[onboarding]
    )
    assert blocked == {}
    monkeypatch.setattr(f.db, "get_registry_workflow", AsyncMock(return_value=onboarding))
    server = mcp(f, monkeypatch)
    result = structured(await server.call_tool("get_started", {"project_id": str(f.project.id)}))
    assert result["first_workflow"]["available"] is True
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0
    f.settings.codex_api_projects = {f.project.id}
    assert (
        await billing_restrictions(database=f.db, project_id=f.project.id, workflows=[onboarding])
        == {}
    )
    await f.db.pool.execute(
        "DELETE FROM billing_project_policies WHERE workspace_id=$1", f.project.workspace_id
    )
    await f.db.pool.execute(
        "DELETE FROM billing_accounts WHERE workspace_id=$1", f.project.workspace_id
    )
    result = structured(await server.call_tool("get_started", {"project_id": str(f.project.id)}))
    assert result["first_workflow"]["available"] is True


def test_public_article_is_hidden_from_new_recommendations_but_retains_its_contract():
    workflow = next(w for w in WORKFLOWS if w.key == "content.public_article")
    assert workflow.definition["procedure"]
    state = tin_state(settings=_Settings(), workflows=WORKFLOWS, connections=[])
    assert workflow.key not in _rows(state)
    assert next(w for w in BUILTIN_WORKFLOWS if w.id == workflow.id).executor == workflow.executor
