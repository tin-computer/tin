"""Post run summaries to the places their saved workflows name, after each run succeeds.

The run's success and its pending posts commit together (db.enqueue_summary_posts). This loop
claims due posts, marking each `sending` before anything is sent, then settles it:

- `sent` with Slack's message `ts`;
- `failed` when Slack refused (channel gone, app removed): nothing was posted;
- `pending` again after a rate limit, when Slack posted nothing and asked to wait;
- `unknown` when Slack's answer was lost, or Tin stopped mid-send: the message may be in the
  channel, so it is never sent again.

A post never changes its run, which already succeeded. Activity says what happened.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
    IntegrationError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
)

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 10
MAX_ATTEMPTS = 5
MAX_DESTINATIONS = 3


class DestinationError(ValueError):
    """The founder's choice of where to post cannot be saved; the message says why."""


async def set_destinations(
    runtime: Any, *, configured: Any, slack_channel_ids: list[str], clerk_user_id: str
):
    """Save where a saved workflow's run summaries are posted: up to three Slack channels
    Tin's app can post to, or none. Later runs only; a run keeps the list it started with."""
    from tin_lite import run_summaries
    from tin_lite.integrations import SLACK_PROVIDER
    from tin_lite.workflow_definitions import resolve_execution_contract

    ids = list(dict.fromkeys(slack_channel_ids))
    if len(ids) > MAX_DESTINATIONS:
        raise DestinationError(f"Pick at most {MAX_DESTINATIONS} Slack channels")
    destinations = []
    if ids:
        workflow = await runtime.database.get_workflow(configured.workflow_id)
        if workflow is None:
            raise DestinationError("This workflow is no longer available")
        workflow = await resolve_execution_contract(
            storage=getattr(runtime, "storage", None),
            workflow=workflow,
            project_id=configured.project_id,
            revision=configured.definition_commit_sha,
            input_schema=configured.input_schema,
        )
        if not run_summaries.supported(workflow.definition):
            raise DestinationError(
                "This workflow does not write a summary, so there is nothing to post"
            )
        channels = {
            option.id: option
            for option in await runtime.integrations.slack.channels(configured.project_id)
        }
        unknown = [channel for channel in ids if channel not in channels]
        if unknown:
            raise DestinationError(
                "Pick channels Tin can post to in the connected Slack workspace "
                "(for a private channel, add Tin's app to it first); "
                + ", ".join(unknown[:3])
                + " is not one"
            )
        destinations = [
            {
                "provider_key": SLACK_PROVIDER,
                "channel_id": channel,
                "channel_name": channels[channel]
                .label.removeprefix("#")
                .removesuffix(" (private)")[:100],
            }
            for channel in ids
        ]
    return await runtime.database.set_summary_destinations(
        project_workflow_id=configured.id,
        project_id=configured.project_id,
        destinations=destinations,
        clerk_user_id=clerk_user_id,
    )


def _link(settings: Any, post: dict[str, Any]) -> str | None:
    if not post.get("artifact_path"):
        return None
    from tin_lite.product_urls import dashboard_url

    return f"{dashboard_url(settings)}/document/{post['run_id']}?project={post['project_id']}"


async def _again(database, post, code: str, message: str, retry_after: int | None) -> None:
    """Nothing was posted: back to pending, until the attempts run out."""
    if post["attempts"] >= MAX_ATTEMPTS:
        await database.finish_summary_post(
            post_id=post["id"], status="failed", error_code=code, error_message=message
        )
    else:
        await database.finish_summary_post(
            post_id=post["id"],
            status="pending",
            error_code=code,
            retry_after_seconds=retry_after or 60,
        )


async def send(runtime: Any, settings: Any, post: dict[str, Any]) -> None:
    """Send one claimed post and settle it. Never raises for a provider outcome."""
    from tin_lite import slack_connection

    database = runtime.database
    summary = post.get("run_summary")
    if summary is None or post.get("run_status") != "succeeded":
        await database.finish_summary_post(
            post_id=post["id"],
            status="failed",
            error_code="no_summary",
            error_message="the run has no summary to post",
        )
        return
    title = f"{post.get('configured_name') or post['workflow_title']} · {post['project_name']}"
    body = slack_connection.message(summary, title=title, link=_link(settings, post))
    try:
        ts = await runtime.integrations.slack.post(
            post["project_id"],
            channel_id=post["channel_id"],
            text=body["text"],
            blocks=body["blocks"],
        )
    except IntegrationRateLimitedError as exc:
        await _again(
            database, post, "rate_limited", "Slack kept asking Tin to wait", exc.retry_after
        )
        return
    except IntegrationDeliveryUnknownError:
        await database.finish_summary_post(
            post_id=post["id"],
            status="unknown",
            error_code="outcome_unknown",
            error_message="Slack did not confirm the post, so it may or may not be in the channel",
        )
        return
    except IntegrationAuthorizationError as exc:
        await database.finish_summary_post(
            post_id=post["id"],
            status="failed",
            error_code="reauthorization_required",
            error_message=f"{exc}. Reconnect Slack in Integrations",
        )
        return
    except IntegrationUpstreamError:
        # Slack was never reached (no connection), so nothing was posted: try again later.
        await _again(database, post, "unreachable", "Tin could not reach Slack", 120)
        return
    except IntegrationDeliveryRefusedError as exc:
        await database.finish_summary_post(
            post_id=post["id"], status="failed", error_code="refused", error_message=str(exc)
        )
        return
    except IntegrationError as exc:
        # Refused before sending (not configured, invalid channel): nothing was posted.
        await database.finish_summary_post(
            post_id=post["id"], status="failed", error_code="not_sent", error_message=str(exc)
        )
        return
    except Exception:  # noqa: BLE001 - an unexpected error after claiming may follow a send
        logger.warning("A run summary post ended without a known outcome.")
        await database.finish_summary_post(
            post_id=post["id"],
            status="unknown",
            error_code="outcome_unknown",
            error_message="Tin could not tell whether Slack showed the post",
        )
        return
    await database.finish_summary_post(post_id=post["id"], status="sent", message_ts=ts)


async def post_due(runtime: Any, settings: Any, *, limit: int = 10) -> int:
    """Claim and send one post at a time, so a deploy or an error can interrupt only the post
    in flight, never a batch of claimed posts that were not attempted."""
    await runtime.database.recover_summary_posts()
    done = 0
    for _ in range(limit):
        claimed = await runtime.database.claim_summary_posts(limit=1)
        if not claimed:
            break
        await send(runtime, settings, claimed[0])
        done += 1
    return done


async def summary_posts_loop(runtime: Any, settings: Any) -> None:
    while True:
        try:
            await post_due(runtime, settings)
        except Exception:  # noqa: BLE001 - the next pass retries; nothing is resent
            logger.warning("Run summary posting is pending; it will retry.")
        await asyncio.sleep(INTERVAL_SECONDS)
