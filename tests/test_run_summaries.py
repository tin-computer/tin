"""Run summaries and their posts to Slack: the summary shape, what supports one, the Slack
connection's OAuth, channels and post outcomes, and the outbox that posts after a run succeeds.
No network: Slack is a synthetic transport, Postgres a disposable schema."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_gak import settings_values
from test_integrations import USER_ID, settings
from test_procedure_publication import publication_db as publication_db
from test_visitor_journeys import c as c  # noqa: F401 - fixtures for the journal report
from test_visitor_journeys import census as census  # noqa: F401
from test_visitor_journeys import j as j  # noqa: F401
from test_visitor_journeys import m as m  # noqa: F401
from test_visitor_journeys import people as people  # noqa: F401
from test_visitor_journeys import report

from tin_lite import run_summaries, slack_connection, summary_posts, workflow_code
from tin_lite.integrations import (
    SLACK_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
    IntegrationRateLimitedError,
    IntegrationService,
    IntegrationUpstreamError,
)
from tin_lite.workflow_code import (
    code_result_summary,
    validate_code_definition,
    validate_code_result,
)

SUMMARY = {
    "headline": "Yesterday: 80 unique visitors, 2 clicked Start trial.",
    "table": {
        "columns": ["", "Unique visitors", "Clicked Start trial"],
        "rows": [["Yesterday", "80", "2"], ["Last 7 days, daily average", "76.1", "3.4"]],
    },
    "sections": [
        {
            "heading": "Some visitors",
            "items": [
                {
                    "text": "Michael <Jones> & co: started a trial.",
                    "url": "https://us.posthog.com/project/1/replay/01a1",
                    "label": "Replay",
                }
            ],
        }
    ],
}


def test_a_summary_is_bounded_plain_data_and_a_broken_one_is_dropped():
    parsed = run_summaries.parse(SUMMARY)
    assert parsed["version"] == 1 and parsed["table"]["rows"][0] == ["Yesterday", "80", "2"]
    assert parsed["sections"][0]["items"][0]["label"] == "Replay"
    assert run_summaries.parse({"headline": ""}) is None
    assert run_summaries.parse("Yesterday: 80") is None
    long = run_summaries.parse(
        {
            "headline": "word " * 200,
            "table": {"columns": ["only one"], "rows": [["x"]]},
            "sections": [{"heading": "H", "items": [{"text": "t", "url": "javascript:alert(1)"}]}]
            + [{"heading": "More", "items": ["plain"]}] * 9,
        }
    )
    assert len(long["headline"]) <= run_summaries.HEADLINE and long["headline"].endswith("…")
    assert long["table"] is None  # one column is not a table
    assert long["sections"][0]["items"][0] == {"text": "t", "url": None, "label": None}
    assert len(long["sections"]) == run_summaries.SECTIONS
    assert "|  | Unique visitors | Clicked Start trial |" in run_summaries.markdown(parsed)


def test_a_visitor_journal_brief_becomes_the_runs_summary(j, c, m, people):
    text = report(
        j,
        c,
        m,
        people,
        brief=j.brief(
            [("ben@quanta.io", "Clicked pricing twice.")],
            [("Most visitors left after the homepage.", None)],
        ),
    )
    summary = run_summaries.from_output("visitor-journal.v1", text)
    assert summary["headline"] == "Yesterday: 4 unique visitors, 1 clicked pricing, 1 signed up."
    assert summary["table"]["columns"] == ["", "Unique visitors", "Clicked pricing", "Signed up"]
    assert summary["table"]["rows"][0] == ["Yesterday", "4", "1", "1"]
    visitors, observations = summary["sections"]
    assert visitors["items"][0]["text"].endswith("(ben@quanta.io): Clicked pricing twice.")
    assert visitors["items"][0]["label"] == "Replay" and visitors["items"][0]["url"]
    assert observations["items"] == [
        {"text": "Most visitors left after the homepage.", "url": None, "label": None}
    ]
    # Unsupported validators and unreadable content have no summary; nothing raises.
    assert run_summaries.from_output("analytics-brief.v1", text) is None
    assert run_summaries.from_output("visitor-journal.v1", "not a journal") is None


def test_new_users_lead_the_summary_with_where_they_work(j, c, m, people):
    from test_visitor_journeys import with_work

    text = report(
        j,
        c,
        m,
        people,
        named=with_work(j, m, people),
        brief=j.brief(
            [],
            [],
            [("ana@fieldnotes.dev", "Created an account."), ("ben@quanta.io", "Booked a call.")],
        ),
    )
    [new] = run_summaries.from_output("visitor-journal.v1", text)["sections"]
    assert new["heading"] == "New users"
    assert new["items"][0] == {
        "text": "Ana Souza (ana@fieldnotes.dev), Founder at Fieldnotes: Created an account.",
        "url": "https://www.linkedin.com/in/anasouza",
        "label": "LinkedIn",
    }
    # A run with lookups off posts no work, whatever the report says.
    [off] = run_summaries.from_output("visitor-journal.v1", text, {"look_up_new_people": False})[
        "sections"
    ]
    assert off["items"][0]["text"] == "Ana Souza (ana@fieldnotes.dev): Created an account."
    # Without a profile, the replay.
    assert new["items"][1]["text"] == "Ben Ode (ben@quanta.io): Booked a call."
    assert new["items"][1]["label"] == "Replay"


def code_definition(summary: bool):
    manifest = json.loads(
        (Path(workflow_code.__file__).with_name("code_example") / "workflow.json").read_text()
    )
    definition = manifest["definition"]
    if summary:
        definition["code"]["output"]["summary"] = True
    return definition


PATH = "reports/custom/ORDER_REPORT.md"


def test_code_workflows_declare_a_summary_and_return_it_beside_their_file():
    plain = validate_code_definition(code_definition(False))
    declared = validate_code_definition(code_definition(True))
    assert not plain.summary and declared.summary
    assert run_summaries.supported(code_definition(True))
    assert not run_summaries.supported(code_definition(False))
    raw = json.dumps({"path": PATH, "content": "# X\n", "summary": SUMMARY}).encode()
    assert validate_code_result(raw, declared) == b"# X\n"
    assert code_result_summary(raw, declared)["headline"] == SUMMARY["headline"]
    with pytest.raises(ValueError, match="declared path"):
        validate_code_result(raw, plain)  # undeclared: the envelope stays exactly path+content
    broken = json.dumps({"path": PATH, "content": "# X\n", "summary": 3}).encode()
    assert code_result_summary(broken, declared) is None  # dropped, never fails the run
    with pytest.raises(ValueError, match="summary must be true"):
        bad = code_definition(True)
        bad["code"]["output"]["summary"] = "yes"
        validate_code_definition(bad)


def test_the_slack_message_escapes_text_and_aligns_the_table():
    message = slack_connection.message(
        run_summaries.parse(SUMMARY),
        title="Daily visitor journeys · Claw",
        link="https://app.tin.test/document/1?project=2",
    )
    header, headline, table, section, context = message["blocks"]
    assert header["text"]["text"] == "Daily visitor journeys · Claw"
    assert headline["text"]["text"] == SUMMARY["headline"]
    assert table["text"]["text"].startswith("```") and "Unique visitors" in table["text"]["text"]
    lines = table["text"]["text"].strip("`").strip("\n").split("\n")
    assert len({len(line) for line in lines}) == 1  # right-aligned numbers line up
    assert "Michael &lt;Jones&gt; &amp; co" in section["text"]["text"]
    assert "<https://us.posthog.com/project/1/replay/01a1|Replay>" in section["text"]["text"]
    assert "Open the full result in Tin" in context["elements"][0]["text"]
    assert message["text"].startswith("Daily visitor journeys · Claw: Yesterday")
    wide = run_summaries.parse(
        {
            "headline": "h",
            "table": {"columns": ["`x`", *"abcde"], "rows": [["&" * 60] * 6] * 10},
            "sections": [{"heading": "S", "items": [{"text": "&" * 300}] * 8}],
        }
    )
    for block in slack_connection.message(wide, title="T", link=None)["blocks"][1:]:
        text = block["text"]["text"]
        assert len(text) <= 3000 and "&am" not in text.replace("&amp;", "")
    table = slack_connection.message(wide, title="T", link=None)["blocks"][2]["text"]["text"]
    assert table.startswith("```\n'x'") and table.endswith("\n```")
    ping = slack_connection.message(
        {**run_summaries.parse(SUMMARY), "headline": "<!channel> hi"}, title="T", link=None
    )
    assert "<!channel>" not in json.dumps(ping) and "&lt;!channel&gt; hi" in ping["text"]


class Slack:
    """Slack's Web API for one workspace."""

    def __init__(self):
        self.calls = []
        self.post = {"ok": True, "channel": "C1", "ts": "1729700000.000100"}
        self.post_status = 200
        self.revoked = False
        self.scope = "channels:read,groups:read,chat:write,chat:write.public"
        self.unreachable = False

    @staticmethod
    def reply(status, body, headers=None):
        return httpx.Response(
            status, stream=httpx.ByteStream(json.dumps(body).encode()), headers=headers
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        method = request.url.path.removeprefix("/api/")
        if method == "oauth.v2.access":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["code"] == "synthetic-code"
            assert form["redirect_uri"] == "https://lite.tin.test/integrations/callback/slack"
            assert request.headers["authorization"].startswith("Basic ")
            return self.reply(
                200,
                {
                    "ok": True,
                    "access_token": "xoxb-synthetic",
                    "token_type": "bot",
                    "scope": self.scope,
                    "bot_user_id": "U0BOT",
                    "app_id": "A0APP",
                    "team": {"id": "T0TEAM", "name": "Claw HQ"},
                },
            )
        assert request.headers["authorization"] == "Bearer xoxb-synthetic"
        if method == "conversations.list":
            if "cursor" not in request.url.params:
                return self.reply(
                    200,
                    {
                        "ok": True,
                        "channels": [
                            {"id": "C2", "name": "growth", "is_member": False},
                            {"id": "C9", "name": "old", "is_archived": True},
                            {"id": "G1", "name": "founders", "is_private": True, "is_member": True},
                            {"id": "G2", "name": "hidden", "is_private": True, "is_member": False},
                        ],
                        "response_metadata": {"next_cursor": "page2"},
                    },
                )
            return self.reply(
                200,
                {
                    "ok": True,
                    "channels": [{"id": "C1", "name": "general", "is_member": True}],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if method == "chat.postMessage":
            if self.unreachable:
                raise httpx.ConnectError("refused", request=request)
            body = json.loads(request.content)
            assert body["unfurl_links"] is False and body["blocks"]
            if self.post_status == 429:
                return self.reply(429, {"ok": False, "error": "ratelimited"}, {"retry-after": "7"})
            return self.reply(self.post_status, self.post)
        if method == "auth.revoke":
            self.revoked = True
            return self.reply(200, {"ok": True, "revoked": True})
        raise AssertionError(method)


def slack_service(db, wire):
    return IntegrationService(
        database=db,
        settings=settings(
            slack_oauth_client_id="1234.5678", slack_oauth_client_secret=SecretStr("secret")
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )


async def connect(integrations, project_id):
    start = await integrations.start_connect(
        project_id=project_id, provider_key=SLACK_PROVIDER, clerk_user_id=USER_ID
    )
    query = parse_qs(urlsplit(start.authorization_url).query)
    assert query["scope"] == ["channels:read,groups:read,chat:write,chat:write.public"]
    assert "code_challenge" not in query
    return await integrations.slack.complete(
        state=query["state"][0], code="synthetic-code", clerk_user_id=USER_ID
    )


@pytest.mark.asyncio
async def test_slack_connects_lists_channels_posts_and_disconnects(publication_db):
    db = publication_db
    project = await db.create_project(name="Claw", state_repo_id=f"projects/{uuid4()}")
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER_ID)
    wire = Slack()
    integrations = slack_service(db, wire)
    try:
        connection = await connect(integrations, project.id)
        assert connection.external_account_label == "Claw HQ"
        assert connection.configuration["granted_capabilities"] == [
            "slack.channels.read",
            "slack.messages.post",
        ]
        assert b"xoxb-synthetic" not in connection.credential_ciphertext
        channels = await integrations.slack.channels(project.id)
        assert [(c.id, c.label) for c in channels] == [
            ("G1", "#founders (private)"),
            ("C1", "#general"),
            ("C2", "#growth"),
        ]
        listed = wire.calls[-1].url.params
        assert listed["types"] == "public_channel,private_channel"

        body = slack_connection.message(run_summaries.parse(SUMMARY), title="T", link=None)
        post = integrations.slack.post
        assert await post(project.id, channel_id="C1", **body) == "1729700000.000100"
        wire.post = {"ok": False, "error": "channel_not_found"}
        with pytest.raises(IntegrationDeliveryRefusedError, match="no longer exists"):
            await post(project.id, channel_id="C1", **body)
        wire.post_status = 429
        with pytest.raises(IntegrationRateLimitedError) as limited:
            await post(project.id, channel_id="C1", **body)
        assert limited.value.retry_after == 7
        wire.post_status = 502
        with pytest.raises(IntegrationDeliveryUnknownError):
            await post(project.id, channel_id="C1", **body)
        wire.post_status, wire.post = 200, {"ok": False, "error": "internal_error"}
        with pytest.raises(IntegrationDeliveryUnknownError):  # it may have posted
            await post(project.id, channel_id="C1", **body)
        wire.post = {"ok": False, "error": "no_permission"}
        with pytest.raises(IntegrationDeliveryRefusedError):  # one channel, not the connection
            await post(project.id, channel_id="C1", **body)
        assert (
            await db.get_integration_connection(project_id=project.id, provider_key=SLACK_PROVIDER)
        ).status == "connected"
        wire.unreachable = True
        with pytest.raises(IntegrationUpstreamError) as unreachable:
            await post(project.id, channel_id="C1", **body)
        assert not isinstance(unreachable.value, IntegrationDeliveryUnknownError)
        wire.unreachable = False
        wire.post_status, wire.post = 200, {"ok": False, "error": "token_revoked"}
        with pytest.raises(IntegrationAuthorizationError):
            await post(project.id, channel_id="C1", **body)
        stored = await db.get_integration_connection(
            project_id=project.id, provider_key=SLACK_PROVIDER
        )
        assert stored.status == "needs_attention"

        await connect(integrations, project.id)  # Reconnect restores it.
        assert await integrations.disconnect(project_id=project.id, provider_key=SLACK_PROVIDER)
        assert wire.revoked
    finally:
        await integrations.close()


@pytest.mark.asyncio
async def test_slack_refuses_a_grant_without_its_scopes_or_with_rotating_tokens(publication_db):
    db = publication_db
    project = await db.create_project(name="Claw", state_repo_id=f"projects/{uuid4()}")
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER_ID)
    wire = Slack()
    wire.scope = "chat:write"
    integrations = slack_service(db, wire)
    try:
        with pytest.raises(IntegrationAuthorizationError, match="did not grant"):
            await connect(integrations, project.id)
        assert not await integrations.list_connections(project.id)
    finally:
        await integrations.close()


def test_the_slack_app_manifest_asks_for_what_tin_asks_for():
    manifest = json.loads(
        (Path(__file__).parents[1] / "infra" / "slack-app-manifest.json").read_text()
    )
    assert manifest["oauth_config"]["scopes"] == {"bot": list(slack_connection.SCOPES)}
    assert manifest["settings"]["token_rotation_enabled"] is False
    for url in manifest["oauth_config"]["redirect_urls"]:
        assert url.startswith("https://")
        assert url.endswith(f"/integrations/callback/{slack_connection.CALLBACK}")
    display = manifest["display_information"]
    assert len(display["name"]) <= 35 and len(display["description"]) <= 140
    assert 174 <= len(display["long_description"]) <= 4000


def test_slack_settings_come_in_pairs_and_need_the_credential_key(monkeypatch):
    from tin_lite.settings import Settings

    with pytest.raises(ValueError, match="configured together"):
        Settings(
            _env_file=None,
            **settings_values(monkeypatch, TIN_LITE_SLACK_OAUTH_CLIENT_ID="1234.5678"),
        )
    with pytest.raises(ValueError, match="CREDENTIAL_KEY is required when Slack"):
        Settings(
            _env_file=None,
            **settings_values(
                monkeypatch,
                TIN_LITE_SLACK_OAUTH_CLIENT_ID="1234.5678",
                TIN_LITE_SLACK_OAUTH_CLIENT_SECRET="secret",  # noqa: S106
            ),
        )


async def seeded_run(db, *, destinations, summary=SUMMARY):
    """A saved workflow that names where to post, and a run created from it."""
    project = await db.create_project(name="Claw", state_repo_id=f"projects/{uuid4()}")
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER_ID)
    workflow_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, $4, 'Visitor journeys', 'codex.procedure',
                   'registry/workflows', 'v.json', $2, '1.3.0', $3::jsonb)""",
        workflow_id,
        "d" * 40,
        json.dumps({"procedure": {"output": {"validator": "visitor-journal.v1"}}}),
        f"custom.journeys_{uuid4().hex[:8]}",
    )
    configured_id = await db.pool.fetchval(
        """INSERT INTO project_workflows (id, project_id, workflow_id, definition_commit_sha, name,
                inputs, input_schema, status, request_id, created_by_clerk_user_id)
           VALUES ($1, $2, $3, $4, 'Daily visitor journeys', '{}', '{}', 'active', $5, $6)
           RETURNING id""",
        uuid4(),
        project.id,
        workflow_id,
        "d" * 40,
        str(uuid4()),
        USER_ID,
    )
    configured = await db.set_summary_destinations(
        project_workflow_id=configured_id,
        project_id=project.id,
        destinations=destinations,
        clerk_user_id=USER_ID,
    )
    assert configured.summary_supported
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status,
            project_workflow_id, summary_destinations, run_summary)
           SELECT $1, $2, $3, 'codex.procedure', $4, $5, $6, 1, 1, 'running', $7,
                  configured.summary_destinations, $8::jsonb
           FROM project_workflows AS configured WHERE configured.id = $7""",
        run_id,
        project.id,
        workflow_id,
        "d" * 40,
        f"procedure:{run_id}",
        str(run_id),
        configured_id,
        json.dumps(run_summaries.parse(summary)) if summary else None,
    )
    return project, configured, run_id


async def succeed(db, run_id):
    async with db.pool.acquire() as conn:
        key = f"{run_id}:procedure_projection"
        await db.start_effect(conn, execution_key=key, operation="procedure_projection")
        await db.complete_procedure_projection(
            conn,
            execution_key=key,
            run_id=run_id,
            canonical_commit_sha="c" * 40,
            artifact_path="reports/visitors/x.md",
            artifact_ref="code.storage://x",
            details={},
            summary="Yesterday: 80 unique visitors.",
        )


GROWTH = {"provider_key": SLACK_PROVIDER, "channel_id": "C2", "channel_name": "growth"}


@pytest.mark.asyncio
async def test_a_run_that_succeeds_posts_its_summary_once_to_its_pinned_channels(publication_db):
    db = publication_db
    project, configured, run_id = await seeded_run(db, destinations=[GROWTH])
    # A later change to the saved workflow does not reach a run already started.
    await db.set_summary_destinations(
        project_workflow_id=configured.id,
        project_id=project.id,
        destinations=[],
        clerk_user_id=USER_ID,
    )
    await succeed(db, run_id)
    assert [p["channel_id"] for p in await db.list_summary_posts(run_id)] == ["C2"]

    sent = []

    class FakeSlack:
        async def post(self, project_id, *, channel_id, text, blocks):
            sent.append((project_id, channel_id, text))
            return "1729700000.000200"

    runtime = SimpleNamespace(database=db, integrations=SimpleNamespace(slack=FakeSlack()))
    assert (
        await summary_posts.post_due(runtime, SimpleNamespace(app_url="https://app.tin.test")) == 1
    )
    assert (
        await summary_posts.post_due(runtime, SimpleNamespace(app_url="https://app.tin.test")) == 0
    )
    assert sent == [(project.id, "C2", f"Daily visitor journeys · Claw: {SUMMARY['headline']}")]
    (post,) = await db.list_summary_posts(run_id)
    assert post["status"] == "sent" and post["message_ts"] == "1729700000.000200"
    events = await db.pool.fetch(
        "SELECT event_type, summary FROM activity_events WHERE run_id=$1 "
        "AND event_type LIKE 'run_summary%'",
        run_id,
    )
    assert [(e["event_type"], e["summary"]) for e in events] == [
        ("run_summary_posted", "Summary posted to #growth in Slack.")
    ]


@pytest.mark.asyncio
async def test_a_post_with_an_unknown_outcome_is_never_sent_again(publication_db):
    db = publication_db
    _, _, run_id = await seeded_run(db, destinations=[GROWTH])
    await succeed(db, run_id)
    (claimed,) = await db.claim_summary_posts()
    # Tin stopped after claiming: the post may be in the channel.
    await db.pool.execute(
        "UPDATE run_summary_posts SET claimed_at = now() - interval '1 hour' WHERE id=$1",
        claimed["id"],
    )
    assert await db.claim_summary_posts() == []
    assert await db.recover_summary_posts() == [claimed["id"]]
    (post,) = await db.list_summary_posts(run_id)
    assert post["status"] == "unknown" and "may or may not" in post["error_message"]
    assert await db.claim_summary_posts() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (IntegrationRateLimitedError("wait", retry_after=30), "pending", "rate_limited"),
        (IntegrationUpstreamError("Slack could not be reached"), "pending", "unreachable"),
        (
            IntegrationDeliveryRefusedError("Slack refused the post", status=200),
            "failed",
            "refused",
        ),
        (IntegrationDeliveryUnknownError("lost"), "unknown", "outcome_unknown"),
        (IntegrationAuthorizationError("Reconnect"), "failed", "reauthorization_required"),
        (RuntimeError("boom"), "unknown", "outcome_unknown"),
    ],
)
async def test_each_slack_outcome_settles_the_post_and_never_the_run(
    publication_db, error, status, code
):
    db = publication_db
    _, _, run_id = await seeded_run(db, destinations=[GROWTH])
    await succeed(db, run_id)

    class FakeSlack:
        async def post(self, *args, **kwargs):
            raise error

    runtime = SimpleNamespace(database=db, integrations=SimpleNamespace(slack=FakeSlack()))
    await summary_posts.post_due(runtime, SimpleNamespace(app_url="https://app.tin.test"))
    (post,) = await db.list_summary_posts(run_id)
    assert (post["status"], post["error_code"]) == (status, code)
    assert (await db.get_run(run_id)).status.value == "succeeded"


@pytest.mark.asyncio
async def test_a_run_without_a_summary_or_destinations_posts_nothing(publication_db):
    db = publication_db
    _, _, quiet = await seeded_run(db, destinations=[])
    _, _, empty = await seeded_run(db, destinations=[GROWTH], summary=None)
    for run_id in (quiet, empty):
        await succeed(db, run_id)
        assert await db.list_summary_posts(run_id) == []


@pytest.mark.asyncio
async def test_destinations_are_checked_against_the_workspace_and_the_workflow(
    publication_db, monkeypatch
):
    db = publication_db
    project, configured, _ = await seeded_run(db, destinations=[])

    class Channels:
        async def channels(self, project_id):
            return [SimpleNamespace(id="C2", label="#growth", detail=None)]

    workflow = await db.get_workflow(configured.workflow_id)

    async def pinned(**kwargs):
        return workflow

    monkeypatch.setattr("tin_lite.workflow_definitions.resolve_execution_contract", pinned)
    runtime = SimpleNamespace(database=db, integrations=SimpleNamespace(slack=Channels()))
    saved = await summary_posts.set_destinations(
        runtime, configured=configured, slack_channel_ids=["C2", "C2"], clerk_user_id=USER_ID
    )
    assert list(saved.summary_destinations) == [GROWTH]
    with pytest.raises(summary_posts.DestinationError, match="C7 is not one"):
        await summary_posts.set_destinations(
            runtime, configured=configured, slack_channel_ids=["C7"], clerk_user_id=USER_ID
        )
    with pytest.raises(summary_posts.DestinationError, match="at most 3"):
        await summary_posts.set_destinations(
            runtime,
            configured=configured,
            slack_channel_ids=["C1", "C2", "C3", "C4"],
            clerk_user_id=USER_ID,
        )
    unsupported = SimpleNamespace(definition={"procedure": {"output": {}}})

    async def plain(**kwargs):
        return unsupported

    monkeypatch.setattr("tin_lite.workflow_definitions.resolve_execution_contract", plain)
    with pytest.raises(summary_posts.DestinationError, match="does not write a summary"):
        await summary_posts.set_destinations(
            runtime, configured=configured, slack_channel_ids=["C2"], clerk_user_id=USER_ID
        )
    # Clearing never needs Slack or a summary.
    cleared = await summary_posts.set_destinations(
        runtime, configured=configured, slack_channel_ids=[], clerk_user_id=USER_ID
    )
    assert cleared.summary_destinations == ()
    assert project.id == cleared.project_id


@pytest.mark.asyncio
async def test_a_new_run_pins_where_its_summary_goes(publication_db):
    db = publication_db
    project, configured, _ = await seeded_run(db, destinations=[GROWTH])
    run, created = await db.create_run(
        project_id=project.id,
        workflow_id=configured.workflow_id,
        started_by_clerk_user_id=USER_ID,
        project_workflow_id=configured.id,
        definition_commit_sha=configured.definition_commit_sha,
    )
    assert created and run.summary_destinations == (GROWTH,)
    other, _ = await db.create_run(
        project_id=project.id,
        workflow_id=configured.workflow_id,
        started_by_clerk_user_id=USER_ID,
        definition_commit_sha=configured.definition_commit_sha,
    )
    assert other.summary_destinations == ()  # a run started outside a saved workflow


def test_a_long_section_continues_in_the_next_slack_block():
    items = [{"text": f"Person {n}: " + "did a lot this week " * 20} for n in range(20)]
    summary = run_summaries.parse(
        {"headline": "This week", "sections": [{"heading": "New users", "items": items}]}
    )
    blocks = slack_connection.message(summary, title="T", link=None)["blocks"][2:]
    assert len(blocks) > 1 and blocks[0]["text"]["text"].startswith("*New users*\n")
    texts = [block["text"]["text"] for block in blocks]
    assert all(len(text) <= slack_connection.BLOCK_TEXT for text in texts)
    assert sum(text.count("• Person ") for text in texts) == 20
