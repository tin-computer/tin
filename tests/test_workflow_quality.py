"""Behavioral boundaries from the ClawMessenger review, without paid providers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite.activities import TinActivities
from tin_lite.billing_contracts import BillingError
from tin_lite.visibility import VisibilityProtocolError, _validate_candidate_bank
from tin_lite.workflow_evidence import integration_inventory


async def test_integration_inventory_never_serializes_credentials_or_configuration():
    db = SimpleNamespace(
        list_integration_connections=AsyncMock(
            return_value=[
                SimpleNamespace(
                    provider_key="custom.api.posthog",
                    status="connected",
                    configuration={"secret": "do-not-copy"},
                    credential_ciphertext="private",
                )
            ]
        )
    )
    result = await integration_inventory(db, uuid4())
    assert result["connections"] == [{"provider": "custom.api.posthog", "status": "connected"}]
    assert "private" not in str(result) and "do-not-copy" not in str(result)


async def test_native_schedule_billing_block_projects_existing_pause_path(monkeypatch):
    configured = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        workflow_id=uuid4(),
        status="active",
        schedule={"cadence": "daily", "local_time": "09:00", "timezone": "UTC"},
        definition_commit_sha="a" * 40,
        input_schema={},
        inputs={},
        settings_revision=1,
    )
    workflow = SimpleNamespace(
        key="content.answer_page", executor="content.answer_page", definition={}
    )
    db = SimpleNamespace(
        get_project_workflow=AsyncMock(return_value=configured),
        get_workflow=AsyncMock(return_value=workflow),
        consume_project_workflow_skip=AsyncMock(return_value=False),
        create_run=AsyncMock(
            side_effect=BillingError(
                "insufficient_limit", "This schedule has no sufficient standing spending limit."
            )
        ),
    )
    common = object.__new__(TinActivities)
    common._db, common._storage = db, SimpleNamespace()
    monkeypatch.setattr(
        "tin_lite.workflow_definitions.resolve_execution_contract", AsyncMock(return_value=workflow)
    )
    monkeypatch.setattr("tin_lite.workflow_definitions.ensure_schedule_allowed", lambda *args: None)
    monkeypatch.setattr(
        "tin_lite.activities.evaluate_prerequisites",
        AsyncMock(return_value=SimpleNamespace(results=[])),
    )
    pause = AsyncMock()
    monkeypatch.setattr("tin_lite.code_schedules.pause_for_issue", pause)
    result = await common.dispatch_scheduled_workflow(
        {
            "project_workflow_id": str(configured.id),
            "occurrence_id": "one",
            "scheduled_for": datetime.now(UTC).isoformat(),
        }
    )
    assert result == {} and db.create_run.await_count == 1
    pause.assert_awaited_once_with(
        common, configured, "This schedule has no sufficient standing spending limit."
    )


def test_candidate_bank_rejects_branded_or_unfrozen_measurement_questions():
    intents = [
        {
            "intent": f"intent{i}",
            "questions": [f"How can I solve buyer task {i} with approach {j}?" for j in range(3)],
        }
        for i in range(5)
    ]
    panel = {
        "target": {"name": "Acme", "domain": "acme.example", "aliases": []},
        "candidate_intents": intents,
        "questions": [{"text": item["questions"][0]} for item in intents],
    }
    _validate_candidate_bank(panel)
    panel["questions"][0]["text"] = "What tools should I use instead?"
    with pytest.raises(VisibilityProtocolError, match="frozen"):
        _validate_candidate_bank(panel)
    panel["questions"][0]["text"] = intents[0]["questions"][0]
    intents[0]["questions"][1] = "Should I use Acme for this task?"
    with pytest.raises(VisibilityProtocolError, match="branded"):
        _validate_candidate_bank(panel)


def test_native_instruction_pins_retain_prechange_suites_and_reject_other_workflows():
    from tin_lite.catalog import BUILTIN_WORKFLOWS
    from tin_lite.native_skill_pins import pinned_suite, suite_for_workflow

    key = "content.answer_page"
    definition, _ = next(
        w for w in BUILTIN_WORKFLOWS if w.key == key
    ).definition_and_resource_files()
    assert "ANSWER_PLAN_V1" in pinned_suite(definition, key)
    assert "ANSWER_PLAN_V1" not in pinned_suite({"key": key}, key)
    assert pinned_suite({"key": key}, key) == suite_for_workflow(key, legacy=True)
    with pytest.raises(ValueError, match="another workflow"):
        pinned_suite({"key": "visibility.audit"}, key)
    hidden, _ = next(
        w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article"
    ).definition_and_resource_files()
    assert hidden["public_discovery"] is False
