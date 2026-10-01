"""One recipient's failure stays with that recipient; the rest of the campaign sends."""

from __future__ import annotations

import asyncio
import shutil
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import EmailCampaignWorkflow, EmailRecipientWorkflow

QUEUE = "email-campaign-local-test"


@pytest.mark.asyncio
async def test_a_failed_recipient_does_not_stop_the_others_or_the_campaign():
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI is required")
    run_id = str(uuid4())
    recipients = [str(uuid4()) for _ in range(3)]
    refused = recipients[0]
    calls: list[tuple[str, object]] = []

    def stub(name, result=None):
        @activity.defn(name=name)
        async def implementation(value):
            calls.append((name, value))
            if name == "send_email_campaign_recipient" and value["recipient_id"] == refused:
                raise ApplicationError("Gmail refused the message", non_retryable=True)
            return result

        return implementation

    activities = [
        stub("prepare_email_campaign"),
        stub("request_email_campaign_review"),
        stub("record_email_campaign_approval"),
        stub("list_email_campaign_recipients", recipients),
        stub("reserve_email_campaign_delivery", 0),
        stub("send_email_campaign_recipient"),
        stub("email_campaign_follow_up_delay", 0),
        stub("check_email_campaign_reply", False),
        stub("complete_email_campaign_recipient"),
        stub("fail_email_campaign_recipient"),
        stub("complete_email_campaign"),
        stub("project_email_campaign_failure"),
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[EmailCampaignWorkflow, EmailRecipientWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                EmailCampaignWorkflow.run,
                run_id,
                id=f"outreach.email_campaign:{run_id}",
                task_queue=QUEUE,
            )
            await handle.signal("approve")
            await asyncio.wait_for(handle.result(), timeout=60)
            history = await handle.fetch_history()
            statuses = {
                recipient: (
                    await env.client.get_workflow_handle(f"email-recipient:{recipient}").describe()
                ).status
                for recipient in recipients
            }
        await Replayer(workflows=[EmailCampaignWorkflow]).replay_workflow(history)

    names = [name for name, _ in calls]
    sent = [
        value["recipient_id"] for name, value in calls if name == "send_email_campaign_recipient"
    ]
    assert sorted(sent) == sorted(recipients)
    assert statuses[refused] == WorkflowExecutionStatus.FAILED
    assert all(
        statuses[recipient] == WorkflowExecutionStatus.COMPLETED for recipient in recipients[1:]
    )
    completed = [value for name, value in calls if name == "complete_email_campaign_recipient"]
    assert sorted(completed) == sorted(recipients[1:])
    failed = [
        value["recipient_id"] for name, value in calls if name == "fail_email_campaign_recipient"
    ]
    assert failed == [refused]
    assert names.count("complete_email_campaign") == 1
    assert "project_email_campaign_failure" not in names
