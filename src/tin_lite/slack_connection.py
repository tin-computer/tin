"""Project-owned Slack connection: post run summaries to the channels a founder picks.

The founder adds Tin's Slack app to their workspace (OAuth v2, bot token). Tin asks only to list
channels and to post; it never reads messages. It can post to any public channel, and to a
private channel once someone adds the app to it: Slack shows a bot only the private channels it
is in. The bot token is stored encrypted and
never leaves the switchboard. Slack documents no idempotency key for chat.postMessage, so a
caller must record that it is sending before calling `post` (run_summary_posts) and must never
resend a post whose outcome is unknown.

Tin's Slack app must keep token rotation off: a bot token then lasts until it is revoked.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx

from tin_lite.connection_records import open_credential, read_bounded_json, seal_credential
from tin_lite.integrations import (
    SLACK_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
    IntegrationInputError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ProviderOption,
    ServiceCallRefused,
)

API = "https://slack.com/api"
AUTHORIZE = "https://slack.com/oauth/v2/authorize"
CALLBACK = "slack"
SCOPES = ("channels:read", "groups:read", "chat:write", "chat:write.public")
CAPABILITY_SCOPES = {
    "slack.channels.read": {"channels:read", "groups:read"},
    "slack.messages.post": {"chat:write", "chat:write.public"},
}
MAX_BYTES = 512_000
MAX_CHANNEL_PAGES = 10
BLOCK_TEXT = 3000
CHANNEL = re.compile(r"^[A-Z0-9]{1,32}$")
TEAM = re.compile(r"^[A-Z0-9]{1,32}$")
TS = re.compile(r"^[0-9]{1,12}\.[0-9]{1,8}$")
# Slack's answers that mean the token no longer works: reconnect.
REAUTHORIZE = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "missing_scope",
        "app_uninstalled",
    }
)
# Slack's answers that mean this message was not posted to this channel.
REFUSED = {
    "channel_not_found": "the channel no longer exists, or Tin's app was removed from it",
    "is_archived": "the channel is archived",
    "not_in_channel": "Tin's app is not in the channel; add it in Slack",
    "restricted_action": "the workspace does not let apps post there",
    "restricted_action_read_only_channel": "the channel is read-only",
    "restricted_action_thread_only_channel": "the channel allows replies only",
    "restricted_action_non_threadable_channel": "the channel does not allow this post",
    "ekm_access_denied": "the workspace's key management refused the post",
    "msg_too_long": "the message was too long",
    "msg_blocks_too_long": "the message was too long",
    "invalid_blocks": "Slack could not read the message",
    "invalid_blocks_format": "Slack could not read the message",
    "too_many_attachments": "Slack could not read the message",
    "no_text": "the message was empty",
    "duplicate_message_not_found": "Slack could not read the message",
    "no_permission": "Tin's app may not post in that channel",
    "team_access_not_granted": "Tin's app may not post in that channel",
}


def oauth_ready(settings: Any) -> bool:
    return bool(
        getattr(settings, "slack_oauth_client_id", None)
        and getattr(settings, "slack_oauth_client_secret", None)
    )


def authorization_url(settings: Any, *, state: str, redirect: str) -> str:
    query = urlencode(
        {
            "client_id": settings.slack_oauth_client_id,
            "scope": ",".join(SCOPES),
            "redirect_uri": redirect,
            "state": state,
        }
    )
    return f"{AUTHORIZE}?{query}"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit(lines: list[str], *, head: str, tail: str) -> str:
    """Whole lines that fit in one block's text; never a line cut in the middle of its markup.
    Empty when not even the first line fits."""
    kept: list[str] = []
    for line in lines:
        if len(head) + len("\n".join([*kept, line])) + len(tail) > BLOCK_TEXT:
            break
        kept.append(line)
    return head + "\n".join(kept) + tail if kept else ""


def message(summary: dict[str, Any], *, title: str, link: str | None) -> dict[str, Any]:
    """A run summary (run_summaries.parse) as Slack's `text` and Block Kit `blocks`."""
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _cut(title, 150)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _escape(summary["headline"])}},
    ]
    table = summary.get("table")
    if table:
        # Backticks in a cell would close the code block early.
        rows = [
            [cell.replace("`", "'") for cell in row] for row in [table["columns"], *table["rows"]]
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(len(table["columns"]))]
        lines = [
            _escape(
                "  ".join(
                    cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
                    for i, cell in enumerate(row)
                ).rstrip()
            )
            for row in rows
        ]
        # The rows start on their own line: Slack trims spaces right after the opening fence.
        text = _fit(lines, head="```\n", tail="\n```")
        if text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    for section in summary.get("sections") or []:
        items = [
            "• "
            + _escape(item["text"])
            + (f" <{item['url']}|{_escape(item['label'])}>" if item.get("url") else "")
            for item in section["items"]
        ]
        text = _fit(items, head=f"*{_escape(section['heading'])}*\n", tail="")
        if text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    if link:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{link}|Open the full result in Tin>"}],
            }
        )
    # The notification text is parsed like mrkdwn too: escaped, so a summary can never
    # mention @channel or link anywhere.
    return {"text": _cut(_escape(f"{title}: {summary['headline']}"), 3000), "blocks": blocks}


def _token(integrations: Any, connection: Any) -> str:
    try:
        token = open_credential(integrations, connection)
    except (ValueError, TypeError):
        raise IntegrationAuthorizationError("Reconnect Slack to restore its credential") from None
    if not isinstance(token, str) or not token.startswith("xoxb-"):
        raise IntegrationAuthorizationError("Reconnect Slack to restore its credential")
    return token


class SlackConnection:
    def __init__(self, integrations: Any) -> None:
        self.integrations = integrations
        self.db = integrations._database

    @property
    def _client(self) -> httpx.AsyncClient:
        return self.integrations._client

    async def _attempt(self, state: str, clerk_user_id: str, *, include_used: bool):
        if not isinstance(state, str) or not 1 <= len(state) <= 256:
            raise IntegrationAuthorizationError("Slack connection attempt has expired")
        attempt = await self.db.get_integration_auth_attempt(
            token_hash=hashlib.sha256(state.encode()).hexdigest(),
            provider_key=SLACK_PROVIDER,
            clerk_user_id=clerk_user_id,
            include_used=include_used,
        )
        if attempt is None:
            raise IntegrationAuthorizationError("Slack connection attempt has expired")
        return attempt

    async def pending_project(self, *, state: str, clerk_user_id: str) -> UUID:
        return (await self._attempt(state, clerk_user_id, include_used=True)).project_id

    async def _call(
        self,
        method: str,
        api_method: str,
        *,
        token: str | None = None,
        write: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        """One Web API call. Slack answers most refusals with HTTP 200 and `ok: false`; the
        caller reads `error`. A write whose answer is lost raises
        IntegrationDeliveryUnknownError: the message may be in the channel."""
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._client.stream(
                method,
                f"{API}/{api_method}",
                headers=headers,
                follow_redirects=False,
                timeout=20.0,
                **kwargs,
            ) as response:
                if response.status_code == 429:
                    # Slack refused before posting anything.
                    raw = response.headers.get("retry-after", "")
                    raise IntegrationRateLimitedError(
                        "Slack rate-limited the request",
                        retry_after=min(int(raw), 3600) if raw.isdigit() else 60,
                    )
                if response.status_code != 200:
                    if write:
                        raise IntegrationDeliveryUnknownError("Slack did not confirm the post")
                    raise IntegrationUpstreamError("Slack is temporarily unavailable")
                payload = await read_bounded_json(response, maximum=MAX_BYTES, provider="Slack")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The request never reached Slack, so nothing was posted.
            raise IntegrationUpstreamError("Slack could not be reached") from None
        except httpx.HTTPError:
            if write:
                raise IntegrationDeliveryUnknownError("Slack did not confirm the post") from None
            raise IntegrationUpstreamError("Slack is temporarily unavailable") from None
        except IntegrationRateLimitedError:
            raise
        except (IntegrationUpstreamError, ServiceCallRefused):
            # An unreadable or oversized answer: for a post, Slack may have shown it.
            if write:
                raise IntegrationDeliveryUnknownError("Slack did not confirm the post") from None
            raise
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            if write:
                raise IntegrationDeliveryUnknownError("Slack did not confirm the post")
            raise IntegrationUpstreamError("Slack returned an invalid response")
        return payload

    async def complete(self, *, state: str, code: str, clerk_user_id: str):
        self.integrations._require_configured(SLACK_PROVIDER)
        state_hash = hashlib.sha256((state or "").encode()).hexdigest()
        attempt = await self.db.consume_integration_auth_attempt(
            token_hash=state_hash, provider_key=SLACK_PROVIDER, clerk_user_id=clerk_user_id
        )
        if attempt is None:
            used = await self._attempt(state, clerk_user_id, include_used=True)
            existing = await self.db.get_integration_connection(
                project_id=used.project_id, provider_key=SLACK_PROVIDER
            )
            if existing is not None and existing.configuration.get("auth_attempt") == state_hash:
                return existing
            raise IntegrationAuthorizationError("Slack connection attempt has expired")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise IntegrationAuthorizationError("Slack did not return an authorization code")
        settings = self.integrations._settings
        payload = await self._call(
            "POST",
            "oauth.v2.access",
            data={"code": code, "redirect_uri": self.integrations._callback_url(CALLBACK)},
            auth=(
                settings.slack_oauth_client_id,
                settings.slack_oauth_client_secret.get_secret_value(),
            ),
        )
        if not payload["ok"]:
            raise IntegrationAuthorizationError("Add Tin to Slack again and allow its access")
        token = payload.get("access_token")
        team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
        scopes = {part for part in str(payload.get("scope") or "").split(",") if part}
        if payload.get("refresh_token") or payload.get("expires_in"):
            # Rotating tokens would need refresh Tin does not do for Slack; see the module note.
            raise IntegrationAuthorizationError(
                "Tin's Slack app has token rotation on; the operator must turn it off"
            )
        if (
            payload.get("token_type") != "bot"
            or not isinstance(token, str)
            or not token.startswith("xoxb-")
            or not isinstance(team.get("id"), str)
            or not TEAM.fullmatch(team["id"])
        ):
            raise IntegrationAuthorizationError("Slack did not return a workspace bot token")
        if not set(SCOPES) <= scopes:
            raise IntegrationAuthorizationError("Slack did not grant the access Tin asked for")
        name = str(team.get("name") or team["id"])[:100]
        async with self.db.integration_refresh_lock(
            project_id=attempt.project_id, provider_key=SLACK_PROVIDER
        ):
            ciphertext, version = seal_credential(
                self.integrations,
                project_id=attempt.project_id,
                provider_key=SLACK_PROVIDER,
                value=token,
            )
            connection = await self.db.upsert_integration_connection(
                project_id=attempt.project_id,
                provider_key=SLACK_PROVIDER,
                external_account_id=team["id"],
                external_account_label=name,
                configuration={
                    "team_id": team["id"],
                    "team_name": name,
                    "bot_user_id": str(payload.get("bot_user_id") or "")[:32],
                    "granted_scopes": sorted(scopes),
                    "granted_capabilities": [
                        key for key, needed in CAPABILITY_SCOPES.items() if needed <= scopes
                    ],
                    "oauth_client_id": settings.slack_oauth_client_id,
                    "auth_attempt": state_hash,
                },
                credential_ciphertext=ciphertext,
                credential_key_version=version,
                connected_by_clerk_user_id=clerk_user_id,
            )
        await self.integrations._record_activity(
            connection,
            "integration_connected",
            f"Slack workspace {name} connected.",
            suffix=str(uuid4()),
        )
        return connection

    async def connection(self, project_id: UUID):
        self.integrations._require_configured(SLACK_PROVIDER)
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=SLACK_PROVIDER
        )
        if (
            connection is None
            or connection.status != "connected"
            or connection.credential_ciphertext is None
        ):
            raise IntegrationAuthorizationError("Connect or reconnect Slack in this project")
        return connection

    async def _reauthorize(self, connection: Any, error: str) -> None:
        await self.db.mark_integration_attention(
            project_id=connection.project_id,
            provider_key=SLACK_PROVIDER,
            error_code="reauthorization_required",
        )
        raise IntegrationAuthorizationError(f"Slack no longer accepts Tin's access ({error})")

    async def channels(self, project_id: UUID) -> list[ProviderOption]:
        """The channels Tin's app can post to, by name: every public channel, and the private
        channels someone added the app to (Slack lists no others to a bot)."""
        connection = await self.connection(project_id)
        token = _token(self.integrations, connection)
        options: list[ProviderOption] = []
        cursor = ""
        for _page in range(MAX_CHANNEL_PAGES):
            params = {
                "types": "public_channel,private_channel",
                "exclude_archived": "true",
                "limit": "200",
            }
            if cursor:
                params["cursor"] = cursor
            payload = await self._call("GET", "conversations.list", token=token, params=params)
            if not payload["ok"]:
                error = str(payload.get("error") or "")[:64]
                if error in REAUTHORIZE:
                    await self._reauthorize(connection, error)
                raise IntegrationUpstreamError("Slack did not list the workspace's channels")
            for channel in payload.get("channels") or []:
                if (
                    isinstance(channel, dict)
                    and isinstance(channel.get("id"), str)
                    and CHANNEL.fullmatch(channel["id"])
                    and isinstance(channel.get("name"), str)
                    and not channel.get("is_archived")
                    and (channel.get("is_member") or not channel.get("is_private"))
                ):
                    private = bool(channel.get("is_private"))
                    options.append(
                        ProviderOption(
                            id=channel["id"],
                            label=f"#{channel['name'][:80]}" + (" (private)" if private else ""),
                            detail="Tin is in this channel" if channel.get("is_member") else None,
                        )
                    )
            metadata = payload.get("response_metadata")
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else ""
            if not cursor:
                break
        return sorted(options, key=lambda option: option.label)

    async def post(
        self, project_id: UUID, *, channel_id: str, text: str, blocks: list[dict[str, Any]]
    ) -> str:
        """Post one message and return its `ts`.

        Raises IntegrationDeliveryRefusedError when Slack refused (nothing posted),
        IntegrationRateLimitedError when Slack asks to wait (nothing posted),
        IntegrationAuthorizationError when the connection needs attention (nothing posted),
        and IntegrationDeliveryUnknownError when the message may have been posted."""
        if not CHANNEL.fullmatch(channel_id or ""):
            raise IntegrationInputError("Slack channel is invalid")
        connection = await self.connection(project_id)
        token = _token(self.integrations, connection)
        payload = await self._call(
            "POST",
            "chat.postMessage",
            token=token,
            write=True,
            json={
                "channel": channel_id,
                "text": text,
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        if payload["ok"]:
            ts = payload.get("ts")
            if not isinstance(ts, str) or not TS.fullmatch(ts):
                raise IntegrationDeliveryUnknownError("Slack did not confirm the post")
            return ts
        error = str(payload.get("error") or "")[:64]
        if error in REAUTHORIZE:
            await self._reauthorize(connection, error)
        if error == "ratelimited":
            raise IntegrationRateLimitedError("Slack rate-limited the post", retry_after=60)
        if error in REFUSED:
            raise IntegrationDeliveryRefusedError(
                f"Slack refused the post: {REFUSED[error]}", status=200
            )
        # Any other answer, internal_error and fatal_error among them, may follow a post that
        # went through: unknown, never sent again.
        raise IntegrationDeliveryUnknownError(f"Slack answered {error or 'an error'}")

    async def revoke(self, connection: Any) -> None:
        try:
            await self._call(
                "POST", "auth.revoke", token=_token(self.integrations, connection), data={}
            )
        except Exception:  # noqa: BLE001 - local disconnect remains authoritative
            return
