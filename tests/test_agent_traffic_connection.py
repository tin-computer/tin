"""analytics.agent_traffic against a fake service that answers the API contract's examples.

Real migrations and SQL in a disposable schema (publication_db); the service is
tests/agent_traffic_fake.py.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from agent_traffic_fake import SERVICE_CREDENTIAL, FakeAgentTraffic, example
from pydantic import SecretStr
from test_integrations import USER_ID, settings
from test_procedure_publication import publication_db as publication_db

from tin_lite.agent_traffic_connection import (
    COMPARED,
    NOT_SET_UP,
    STILL_REMOVING,
    AnalyticsAccessLost,
    ServiceArgumentError,
    comparison,
    moved_hosts,
    project_ref,
    removal_wait,
    request_for,
    site_address,
)
from tin_lite.code_services import ARGUMENT_CHECKS
from tin_lite.code_services import OPERATIONS as GATEWAY_OPERATIONS
from tin_lite.connection_records import open_credential
from tin_lite.integrations import (
    AGENT_TRAFFIC_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationInputError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationRequirement,
    IntegrationService,
    IntegrationUpstreamError,
    ServiceCallRefused,
    ServiceResponseTooLarge,
    registered_integrations,
)
from tin_lite.workflow_services import SERVICE_CAPABILITIES

TIMEZONE = "Europe/Istanbul"


async def onboarding(db, project_id, url, *, timezone=TIMEZONE, status="succeeded"):
    """A Start here run that named the project's website address."""

    workflow_id = await db.pool.fetchval(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
               current_commit_sha, version_label, definition)
           VALUES ($1, 'growth.onboarding', 'Start here', 'growth.onboarding',
                   'registry/workflows', 'onboarding.json', $2, '1', '{}')
           ON CONFLICT DO NOTHING RETURNING id""",
        uuid4(),
        "d" * 40,
    ) or await db.pool.fetchval(
        "SELECT id FROM workflows WHERE key = 'growth.onboarding' AND project_id IS NULL"
    )
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
               temporal_workflow_id, thread_id, generation, fencing_token, status, input)
           VALUES ($1, $2, $3, 'growth.onboarding', $4, $5, $6, 1, 1, $7, $8::jsonb)""",
        run_id,
        project_id,
        workflow_id,
        "d" * 40,
        f"onboarding:{run_id}",
        str(run_id),
        status,
        json.dumps({"product_url": url, "timezone": timezone}),
    )
    return run_id


async def analytics(db, url="https://www.example.com", **overrides):
    """A pilot project (with Start here's address unless `url` is None) and its service."""

    project = await db.create_project(name="Example", state_repo_id=f"repo-{uuid4().hex[:12]}")
    if url is not None:
        await onboarding(db, project.id, url)
    api = FakeAgentTraffic()
    values = {
        "agent_traffic_service_token": SecretStr(SERVICE_CREDENTIAL),
        "agent_traffic_url": "https://edge.tin.test",
        "agent_traffic_projects": (str(project.id),),
        "deployment_environment": "production",
    }
    values.update(overrides)
    service = IntegrationService(
        database=db,
        settings=settings(**values),
        client=httpx.AsyncClient(transport=httpx.MockTransport(api)),
    )
    return service, api, project


async def age(db, project_id, seconds):
    """Move the stored status back in time, as if the card was last opened `seconds` ago."""

    await db.pool.execute(
        """UPDATE agent_traffic_sites SET
               status_checked_at = status_checked_at - make_interval(secs => $2),
               status_claimed_at = status_claimed_at - make_interval(secs => $2),
               status_retry_at = status_retry_at - make_interval(secs => $2)
           WHERE project_id = $1""",
        project_id,
        seconds,
    )


async def card(service, project_id):
    connection = await service._database.get_integration_connection(
        project_id=project_id, provider_key=AGENT_TRAFFIC_PROVIDER
    )
    return await service.agent_traffic.card(project_id, connection)


def test_registry_gateway_and_capabilities_are_explicit() -> None:
    definition = {d.key: d for d in registered_integrations()}[AGENT_TRAFFIC_PROVIDER]
    assert definition.name == "Analytics"
    assert definition.capabilities == ("status.read", "reports.read")
    assert SERVICE_CAPABILITIES[AGENT_TRAFFIC_PROVIDER] == {"status.read", "reports.read"}
    assert GATEWAY_OPERATIONS[(AGENT_TRAFFIC_PROVIDER, "site.status")] == (
        "status.read",
        frozenset(),
    )
    assert GATEWAY_OPERATIONS[(AGENT_TRAFFIC_PROVIDER, "report.read")] == (
        "reports.read",
        frozenset({"period", "date"}),
    )
    assert AGENT_TRAFFIC_PROVIDER in ARGUMENT_CHECKS


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("site.status", {"site_id": "st-other"}, "unsupported argument site_id"),
        ("report.read", {"period": "month"}, "period must be day or week"),
        ("report.read", {"date": "21-10-2026"}, "YYYY-MM-DD"),
        ("report.read", {"date": "2026-02-30"}, "YYYY-MM-DD"),
        ("report.read", {"date": "2020-01-01"}, "within the last 180 days"),
        ("report.read", {"tz": "UTC"}, "unsupported argument tz"),
        ("visits.list", {}, "unknown Analytics operation"),
    ],
)
def test_workflow_arguments_are_closed(operation, arguments, message) -> None:
    with pytest.raises(ServiceArgumentError, match=message):
        request_for(operation, arguments)


def test_addresses_are_checked_before_any_call() -> None:
    assert site_address("example.com") == "https://example.com"
    assert site_address(" https://WWW.Example.com/ ") == "https://www.example.com"
    assert site_address("https://shop.example.com/en") == "https://shop.example.com/en"
    assert site_address("https://bücher.de") == "https://xn--bcher-kva.de"
    for bad in (
        "",
        "ftp://example.com",
        "http://localhost:3000",
        "https://127.0.0.1",
        "https://[::1]",
        "https://printer.local",
        "https://api.internal",
        "https://intranet",
        "https://user:pass@example.com",
        "https://" + "a" * 2050 + ".com",
    ):
        with pytest.raises(IntegrationInputError):
            site_address(bad)


def test_moved_hosts_skip_previews_and_shared_hosting() -> None:
    status = {
        "uncounted_hosts": [
            {"host": "www.newname.com", "pages_7d": 40},
            {"host": "example-git-main-acme.vercel.app", "pages_7d": 3},
            {"host": "deploy-preview-12--example.netlify.app", "pages_7d": 1},
            {"host": "localhost", "pages_7d": 9},
        ]
    }
    assert moved_hosts(status) == [{"host": "www.newname.com", "pages_7d": 40}]


def test_project_references_follow_the_deployment() -> None:
    project = uuid4()
    for environment, prefix in (
        ("production", "tin"),
        ("staging", "tin-staging"),
        ("development", "tin-staging"),
    ):
        namespace = settings(deployment_environment=environment)
        assert project_ref(namespace, project) == f"{prefix}:{project}"


def test_removals_wait_longer_each_time_up_to_six_hours() -> None:
    assert [removal_wait(n) for n in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert removal_wait(40) == 6 * 3600
    assert removal_wait(1, retry_after=300) == 300


async def test_the_card_is_shown_only_to_pilot_projects(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    assert AGENT_TRAFFIC_PROVIDER in {d.key for d in service.definitions([], project_id=project.id)}
    assert AGENT_TRAFFIC_PROVIDER not in {d.key for d in service.definitions([], project_id=None)}
    unlisted, _, other = await analytics(publication_db, agent_traffic_projects=())
    assert AGENT_TRAFFIC_PROVIDER not in {
        d.key for d in unlisted.definitions([], project_id=other.id)
    }
    with pytest.raises(IntegrationNotConfiguredError, match="not available for this project"):
        await unlisted.agent_traffic.set_up(project_id=other.id, clerk_user_id=USER_ID)
    with pytest.raises(IntegrationNotConfiguredError):
        await unlisted.start_connect(
            project_id=other.id, provider_key=AGENT_TRAFFIC_PROVIDER, clerk_user_id=USER_ID
        )
    started = await service.start_connect(
        project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER, clerk_user_id=USER_ID
    )
    assert started.authorization_url == f"https://lite.tin.test/integrations?project={project.id}"
    assert api.calls == []


async def test_without_the_service_token_the_card_says_so(publication_db) -> None:
    service, api, project = await analytics(publication_db, agent_traffic_service_token=None)
    assert not service.is_configured(AGENT_TRAFFIC_PROVIDER)
    assert AGENT_TRAFFIC_PROVIDER in {d.key for d in service.definitions([], project_id=project.id)}
    assert await card(service, project.id) == {"state": "unavailable", "message": NOT_SET_UP}
    with pytest.raises(IntegrationNotConfiguredError, match="isn't set up on this deployment"):
        await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    assert api.calls == []


async def test_set_up_sends_the_setup_address_and_seals_the_read_token(
    publication_db, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    service, api, project = await analytics(publication_db)
    before = await card(service, project.id)
    assert before == {"state": "not_set_up", "address": "https://www.example.com"}
    connection = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    put = api.calls[-1]
    assert (put.method, put.url.path) == ("PUT", f"/v1/admin/projects/tin:{project.id}/site")
    assert put.headers["authorization"] == f"Bearer {SERVICE_CREDENTIAL}"
    assert json.loads(put.content) == {"url": "https://www.example.com"}
    token = api.sites[f"tin:{project.id}"]["read_token"]
    assert connection.status == "connected"
    assert connection.external_account_id == "st-fake0001"
    assert connection.external_account_label == "www.example.com"
    assert open_credential(service, connection) == token
    assert token.encode() not in connection.credential_ciphertext
    configuration = connection.configuration
    assert configuration["counted_hosts"] == ["example.com", "*.example.com"]
    assert configuration["script"].endswith('data-site="st-fake0001"></script>')
    assert configuration["csp"] == {
        "script-src": ["https://edge.tin.computer"],
        "connect-src": ["https://edge.tin.computer"],
    }
    assert [v["variant"] for v in configuration["reporter"]["variants"]] == [
        "nextjs",
        "cloudflare-worker",
    ]
    assert configuration["reporter"]["env_name"] == "TIN_REPORTER_TOKEN"
    assert "connected" not in configuration["reporter"]
    shown = await card(service, project.id)
    assert shown["state"] == "waiting" and shown["address"] == "https://www.example.com"
    # Neither token ever leaves Tin: not the configuration, the card, activity or logs.
    activity = await publication_db.pool.fetch(
        "SELECT summary, details FROM activity_events WHERE project_id = $1", project.id
    )
    for value in (configuration, shown, [dict(row) for row in activity], caplog.text):
        text = json.dumps(value, default=str)
        assert token not in text and SERVICE_CREDENTIAL not in text
    assert any(row["summary"] == "Analytics set up for www.example.com." for row in activity)


async def test_setting_up_again_keeps_the_site_and_rotates_only_the_token(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    first = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    first_token = open_credential(service, first)
    again = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    assert [r.method for r in api.calls] == ["PUT", "PUT"]
    assert again.id == first.id and again.external_account_id == first.external_account_id
    # A run pins the connection's id, account and configuration; all three are unchanged.
    assert again.configuration == first.configuration
    assert open_credential(service, again) not in {first_token, ""}


async def test_staging_sends_staging_references(publication_db) -> None:
    service, api, project = await analytics(publication_db, deployment_environment="staging")
    connection = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    assert api.calls[-1].url.path == f"/v1/admin/projects/tin-staging:{project.id}/site"
    assert connection.configuration["project_ref"] == f"tin-staging:{project.id}"
    other, other_api, second = await analytics(publication_db, deployment_environment="staging")
    other_api.refuse["PUT"] = [409]
    with pytest.raises(ServiceCallRefused, match="Staging already has 20") as refused:
        await other.agent_traffic.set_up(project_id=second.id, clerk_user_id=USER_ID)
    assert refused.value.code == "staging_limit"


async def test_a_project_without_setup_enters_one_address(publication_db) -> None:
    service, api, project = await analytics(publication_db, url=None)
    assert await card(service, project.id) == {"state": "not_set_up", "address": None}
    with pytest.raises(IntegrationInputError, match="Enter your website's address"):
        await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    with pytest.raises(IntegrationInputError, match="public address"):
        await service.agent_traffic.set_up(
            project_id=project.id, clerk_user_id=USER_ID, url="http://localhost:3000"
        )
    assert api.calls == []
    connection = await service.agent_traffic.set_up(
        project_id=project.id, clerk_user_id=USER_ID, url="example.vercel.app"
    )
    assert json.loads(api.calls[-1].content) == {"url": "https://example.vercel.app"}
    assert connection.configuration["counted_hosts"] == ["example.vercel.app"]
    site = await service.agent_traffic.site(project.id)
    assert site["address_source"] == "entered" and site["address_run_id"] is None


async def test_tins_credential_refused_is_logged_and_reads_as_not_set_up(
    publication_db, caplog
) -> None:
    service, api, project = await analytics(publication_db)
    for status in (401, 403):
        api.refuse["PUT"] = [status]
        with caplog.at_level(logging.ERROR, logger="tin_lite.agent_traffic_connection"):
            with pytest.raises(IntegrationNotConfiguredError, match=NOT_SET_UP):
                await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
        assert "refused Tin's service credential" in caplog.text
    assert SERVICE_CREDENTIAL not in caplog.text
    assert (
        await service._database.get_integration_connection(
            project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER
        )
        is None
    )
    api.refuse["PUT"] = [400]
    with pytest.raises(IntegrationInputError):
        await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.refuse["PUT"] = [503]
    with pytest.raises(IntegrationUpstreamError, match="unavailable"):
        await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)


async def test_the_card_moves_through_waiting_live_and_quiet(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    shown = await card(service, project.id)
    status_calls = [r for r in api.calls if r.url.path.endswith("/status")]
    assert len(status_calls) == 1 and shown["state"] == "waiting"
    assert status_calls[0].url.params["tz"] == TIMEZONE
    assert status_calls[0].headers["authorization"].startswith("Bearer read-token-st-fake0001")
    # While waiting, opening the card asks at most once every 30 seconds.
    await card(service, project.id)
    assert len([r for r in api.calls if r.url.path.endswith("/status")]) == 1
    await age(publication_db, project.id, 31)
    api.state = "live"
    shown = await card(service, project.id)
    assert shown["state"] == "live" and shown["pages_today"] == 37
    assert shown["reporter_connected"] is False and shown["requests_last_at"] is None
    # Live: the stored copy serves the card for ten minutes.
    await age(publication_db, project.id, 120)
    api.status_extra = {"pages_today": 90}
    assert (await card(service, project.id))["pages_today"] == 37
    await age(publication_db, project.id, 600)
    recent = (date.today() - timedelta(days=1)).isoformat() + "T10:00:00Z"
    api.status_extra = {"pages_today": 90, "requests_last_at": recent}
    shown = await card(service, project.id)
    assert shown["pages_today"] == 90 and shown["reporter_connected"] is True
    await age(publication_db, project.id, 601)
    api.state, api.status_extra = "quiet", {}
    assert (await card(service, project.id))["state"] == "quiet"


@pytest.mark.parametrize(
    ("status", "code"), [(401, "authentication_failed"), (404, "site_not_found")]
)
async def test_a_refused_read_needs_attention_until_set_up_again(
    publication_db, status, code
) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.refuse["status"] = [status]
    shown = await card(service, project.id)
    assert shown["state"] == "needs_attention"
    connection = await service._database.get_integration_connection(
        project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER
    )
    assert connection.status == "needs_attention" and connection.last_error_code == code
    with pytest.raises(IntegrationAuthorizationError, match="needs attention"):
        await service.ensure_requirements(
            project_id=project.id,
            requirements=(IntegrationRequirement(AGENT_TRAFFIC_PROVIDER, ("reports.read",)),),
        )
    # Set up again sends PUT and clears it.
    again = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    assert again.status == "connected" and api.calls[-1].method == "PUT"
    assert (await card(service, project.id))["state"] == "waiting"


async def test_rate_limits_and_outages_keep_the_stored_copy(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.state = "live"
    assert (await card(service, project.id))["pages_today"] == 37
    await age(publication_db, project.id, 601)
    api.refuse["status"] = [429]
    shown = await card(service, project.id)
    assert shown["state"] == "live" and shown["pages_today"] == 37 and shown["stale"] is True
    site = await service.agent_traffic.site(project.id)
    # The service's retry-after (30 seconds) is honored before asking again.
    calls = len(api.calls)
    await age(publication_db, project.id, 20)
    await card(service, project.id)
    assert len(api.calls) == calls
    assert site["status_failures"] == 1
    await age(publication_db, project.id, 600)
    api.refuse["status"] = [503]
    shown = await card(service, project.id)
    assert shown["state"] == "live" and shown["stale"] is True
    await age(publication_db, project.id, 3600)
    api.status_extra = {"pages_today": 41}
    shown = await card(service, project.id)
    assert shown["pages_today"] == 41 and shown["stale"] is False


async def test_a_newer_setup_with_another_host_sends_the_address_again(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    first = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    await card(service, project.id)
    # The same host in a newer setup changes nothing at the service.
    await onboarding(publication_db, project.id, "https://example.com/pricing")
    await age(publication_db, project.id, 31)
    await card(service, project.id)
    assert [r.method for r in api.calls].count("PUT") == 1
    await onboarding(publication_db, project.id, "https://www.newname.com")
    await age(publication_db, project.id, 31)
    shown = await card(service, project.id)
    puts = [r for r in api.calls if r.method == "PUT"]
    assert len(puts) == 2 and json.loads(puts[-1].content) == {"url": "https://www.newname.com"}
    assert shown["address"] == "https://www.newname.com"
    connection = await service._database.get_integration_connection(
        project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER
    )
    assert connection.id == first.id and connection.external_account_id == "st-fake0001"
    assert connection.configuration["counted_hosts"] == ["newname.com", "*.newname.com"]


async def test_the_card_offers_to_count_the_host_the_script_moved_to(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.state = "live"
    api.status_extra = {
        "uncounted_hosts": [
            {"host": "www.newname.com", "pages_7d": 40},
            {"host": "example-git-main.vercel.app", "pages_7d": 2},
        ]
    }
    shown = await card(service, project.id)
    assert shown["moved_hosts"] == [{"host": "www.newname.com", "pages_7d": 40}]
    with pytest.raises(IntegrationInputError, match="only an address the script ran on"):
        await service.agent_traffic.count_host(
            project_id=project.id, clerk_user_id=USER_ID, host="example-git-main.vercel.app"
        )
    with pytest.raises(IntegrationInputError):
        await service.agent_traffic.count_host(
            project_id=project.id, clerk_user_id=USER_ID, host="attacker.example"
        )
    connection = await service.agent_traffic.count_host(
        project_id=project.id, clerk_user_id=USER_ID, host="www.newname.com"
    )
    assert json.loads(api.calls[-1].content) == {"url": "https://www.newname.com"}
    assert connection.configuration["url"] == "https://www.newname.com"
    # Start here still names the old host, but no newer setup does: Tin keeps the new one.
    await age(publication_db, project.id, 601)
    api.status_extra = {}
    await card(service, project.id)
    assert [r.method for r in api.calls].count("PUT") == 2


async def test_stop_measuring_deletes_the_site_and_tins_copies(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.state = "live"
    await card(service, project.id)
    await service.agent_traffic.report(project.id, period="day")
    assert await service.disconnect(
        project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER, member=USER_ID
    )
    assert api.calls[-1].method == "DELETE"
    assert api.calls[-1].url.path == f"/v1/admin/projects/tin:{project.id}/site"
    db = publication_db
    assert (
        await db.get_integration_connection(
            project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER
        )
        is None
    )
    left = await db.pool.fetchval(
        """SELECT (SELECT count(*) FROM agent_traffic_sites WHERE project_id = $1)
                + (SELECT count(*) FROM agent_traffic_reports WHERE project_id = $1)""",
        project.id,
    )
    assert left == 0
    removal = await service.agent_traffic.removal(project.id)
    assert removal["state"] == "removing" and removal["finished_at"] is not None
    shown = await card(service, project.id)
    assert shown["state"] == "not_set_up"
    assert shown["removal"]["state"] == "removing"
    assert shown["removal"]["deleted_by"].startswith("2026-10-22T10:00:00")
    # Setting up again after the removal was accepted makes a new site.
    connection = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    assert connection.external_account_id == "st-fake0002"


@pytest.mark.parametrize("failure", [503, 401, 429, 403, "timeout"])
async def test_a_failed_delete_is_retried_with_backoff_until_accepted(
    publication_db, failure
) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    if failure == "timeout":
        real = api.__call__

        def timing_out(request):
            if request.method == "DELETE" and not getattr(api, "timed_out", False):
                api.timed_out = True
                raise httpx.ReadTimeout("timed out", request=request)
            return real(request)

        service._client = httpx.AsyncClient(transport=httpx.MockTransport(timing_out))
    else:
        api.refuse["DELETE"] = [failure, failure]
    await service.disconnect(project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER)
    db = publication_db
    removal = await service.agent_traffic.removal(project.id)
    assert removal["state"] == "pending" and removal["finished_at"] is None
    assert removal["attempts"] == 1 and removal["last_error"]
    wait = (removal["next_attempt_at"] - removal["requested_at"]).total_seconds()
    assert wait >= (30 if failure == 429 else 59)
    # Not due yet: the loop's pass sends nothing.
    deletes = len(api.paths("DELETE"))
    assert await service.agent_traffic.process_removals() == 0
    assert len(api.paths("DELETE")) == deletes
    # Setting up again now would let the pending DELETE take the new site, so it waits.
    await db.pool.execute("UPDATE agent_traffic_removals SET next_attempt_at = now()")
    if failure != "timeout":
        with pytest.raises(IntegrationUpstreamError, match=STILL_REMOVING):
            await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
        assert not [r for r in api.calls if r.method == "PUT"][1:]
        await db.pool.execute("UPDATE agent_traffic_removals SET next_attempt_at = now()")
    assert await service.agent_traffic.process_removals() == 1
    removal = await service.agent_traffic.removal(project.id)
    assert removal["state"] == "removing" and removal["finished_at"] is not None
    assert await service.agent_traffic.process_removals() == 0


async def test_a_404_ends_the_removal(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.sites.clear()
    await service.disconnect(project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER)
    removal = await service.agent_traffic.removal(project.id)
    assert removal["state"] == "not_found" and removal["finished_at"] is not None


async def test_removals_outlive_the_projects_purge(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.refuse["DELETE"] = [503]
    # Project deletion disconnects every provider, then purges the project's rows.
    await service.disconnect(project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER)
    async with publication_db.pool.acquire() as conn, conn.transaction():
        await publication_db.purge_project_data(conn, project_id=project.id)
    removal = await service.agent_traffic.removal(project.id)
    assert removal is not None and removal["state"] == "pending"
    await publication_db.pool.execute("UPDATE agent_traffic_removals SET next_attempt_at = now()")
    assert await service.agent_traffic.process_removals() == 1
    assert (await service.agent_traffic.removal(project.id))["state"] == "removing"


async def test_the_removal_loop_waits_without_the_service_token(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    service._settings.agent_traffic_service_token = None
    await service.disconnect(project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER)
    assert (await service.agent_traffic.removal(project.id))["state"] == "pending"
    await publication_db.pool.execute("UPDATE agent_traffic_removals SET next_attempt_at = now()")
    assert await service.agent_traffic.process_removals() == 0
    service._settings.agent_traffic_service_token = SecretStr(SERVICE_CREDENTIAL)
    assert await service.agent_traffic.process_removals() == 1


async def test_reports_are_stored_and_read_again_when_ten_minutes_old(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    day = date(2026, 10, 20)
    stored = await service.agent_traffic.report(project.id, period="day", day=day)
    report = stored["report"]
    assert report["summary"].startswith("48 visits and 91 page views, against 41 visits")
    assert report["previous"]["visits"] == 41 and report["totals"]["assistant_visits"] == 6
    assert stored["comparison"]["changes"]["visits"] == {"now": 48, "before": 41, "percent": 17}
    assert report["tz"] == TIMEZONE and report["start"] == "2026-10-20"
    assert report["kinds"]["automated_browser"]["named"] == [
        {"operator": "openai", "visits": 2, "evidence": "signature"}
    ]
    request = [r for r in api.calls if r.url.path.endswith("/report")][-1]
    assert dict(request.url.params) == {"period": "day", "date": "2026-10-20", "tz": TIMEZONE}
    # A complete report is final.
    again = await service.agent_traffic.report(project.id, period="day", day=day)
    assert again["report"] == report and again["comparison"] == stored["comparison"]
    assert len([r for r in api.calls if r.url.path.endswith("/report")]) == 1
    week = await service.agent_traffic.report(project.id, period="week", day=day)
    assert week["report"]["hours"] is None and len(week["report"]["days"]) == 7


async def test_workflow_reads_write_receipts_and_mark_attention(publication_db) -> None:
    service, api, project = await analytics(publication_db)
    connection = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    api.state = "live"

    def read(operation, arguments, key, *, bound=1_000_000):
        return service.agent_traffic.call(
            operation,
            arguments,
            connection=connection,
            run_id=None,
            execution_key=key,
            max_response_bytes=bound,
        )

    status = await read("site.status", {}, "run:status")
    assert status["state"] == "live" and status["pages_today"] == 37
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    week = await read("report.read", {"period": "week", "date": yesterday}, "run:week")
    assert week["period"] == "week" and len(week["days"]) == 7
    assert week["previous"]["start"] == "2026-10-07" and week["totals"]["assistant_visits"] == 38
    request = api.calls[-1]
    assert dict(request.url.params) == {"period": "week", "date": yesterday, "tz": TIMEZONE}
    with pytest.raises(ServiceResponseTooLarge):
        await read("report.read", {}, "run:small", bound=1024)
    api.refuse["report"] = [429]
    with pytest.raises(IntegrationRateLimitedError):
        await read("report.read", {"period": "day"}, "run:limited")
    api.refuse["report"] = [404]
    with pytest.raises(AnalyticsAccessLost) as lost:
        await read("report.read", {"period": "day"}, "run:lost")
    assert lost.value.code == "site_not_found"
    receipts = await publication_db.pool.fetch(
        """SELECT capability, status, error_code, response_summary
           FROM integration_call_receipts WHERE provider_key = $1 ORDER BY execution_key""",
        AGENT_TRAFFIC_PROVIDER,
    )
    assert [(r["capability"], r["status"], r["error_code"]) for r in receipts] == [
        ("reports.read", "failed", "rate_limited"),
        ("reports.read", "failed", "site_not_found"),
        ("reports.read", "completed", None),
        ("status.read", "completed", None),
        ("reports.read", "completed", None),
    ]
    token = api.sites[f"tin:{project.id}"]["read_token"]
    assert token not in json.dumps([dict(r) for r in receipts], default=str)
    marked = await publication_db.get_integration_connection(
        project_id=project.id, provider_key=AGENT_TRAFFIC_PROVIDER
    )
    assert marked.status == "needs_attention"


def test_contract_examples_are_complete_responses() -> None:
    put = example("put_site.json")
    assert set(put) >= {"site_id", "state", "script", "csp", "read_token", "reporter"}
    assert len(put["read_token"]) == 43
    for report in (example("report_day.json"), example("report_week.json")):
        totals, kinds = report["totals"], report["kinds"]
        assert sum(kind["visits"] for kind in kinds.values()) == totals["visits"]
        assistants = sum(row["visits"] for row in report["came_from"] if row["assistant"])
        assert totals["assistant_visits"] == assistants
        assert set(report["previous"]) == {"start", "end", "until"} | {n for n, _ in COMPARED}
    day, week = example("report_day.json"), example("report_week.json")
    assert set(week) - set(day) == {"days"} and week["hours"] is None
    assert sum(row["visits"] for row in week["days"]) == week["totals"]["visits"]


def test_each_number_compares_with_the_period_before() -> None:
    day = example("report_day.json")
    assert comparison(day) == {
        "start": "2026-10-19",
        "end": "2026-10-19",
        "until": None,
        "changes": {
            "visits": {"now": 48, "before": 41, "percent": 17},
            "page_views": {"now": 91, "before": 80, "percent": 14},
            # Below ten before, a percentage overstates the change; the page says "up from 4".
            "assistant_visits": {"now": 6, "before": 4},
            "no_automation_seen": {"now": 43, "before": 37, "percent": 16},
            "automated_browser": {"now": 3, "before": 2},
            "declared_crawler": {"now": 2, "before": 2},
        },
    }
    week = comparison(example("report_week.json"))
    assert week["start"] == "2026-10-07" and week["changes"]["visits"] == {
        "now": 270,
        "before": 248,
        "percent": 9,
    }
    # A withheld number (null) in either period has no change.
    withheld = example("report_day.json")
    withheld["totals"]["page_views"] = None
    withheld["previous"]["assistant_visits"] = None
    assert set(comparison(withheld)["changes"]) == {
        "visits",
        "no_automation_seen",
        "automated_browser",
        "declared_crawler",
    }
    # Today so far against the day before up to the same point, which follows built_at.
    today = example("report_day.json")
    today.update(complete=False, built_at="2026-10-21T07:50:00Z", start="2026-10-21")
    today["previous"].update(start="2026-10-20", end="2026-10-20", until="2026-10-20T07:50:00Z")
    assert comparison(today)["until"] == "2026-10-20T07:50:00Z"
    assert (
        comparison({**today, "previous": {**today["previous"], "until": "10:50"}})["until"] is None
    )
    # Past retention there is no period before; before the first visit it counts zero.
    assert comparison({**day, "previous": None}) is None
    assert comparison({**day, "previous": {**day["previous"], "visits": 0}})["changes"][
        "visits"
    ] == {"now": 48, "before": 0}


async def test_routes_set_up_show_the_card_and_never_send_a_token(publication_db) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI

    from tin_lite.api import router
    from tin_lite.auth import AuthContext, require_user

    service, api, project = await analytics(publication_db)
    await publication_db.grant_project_membership(project_id=project.id, clerk_user_id=USER_ID)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(integrations=service, database=publication_db)
    app.state.settings = service._settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=USER_ID,
        token_type="session_token",  # noqa: S106 — token category, not a credential
    )
    base = f"/api/projects/{project.id}/integrations"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as web:
        listed = await web.get(base)
        card_view = next(i for i in listed.json() if i["key"] == AGENT_TRAFFIC_PROVIDER)
        assert card_view["status"] == "available"
        assert card_view["analytics"] == {
            "state": "not_set_up",
            "address": "https://www.example.com",
        }
        malformed = await web.post(f"{base}/analytics.agent_traffic/setup", json={"token": "x"})
        assert malformed.status_code == 422
        refused = await web.post(
            f"{base}/analytics.agent_traffic/setup", json={"url": "http://localhost"}
        )
        assert refused.status_code == 422 and "public address" in refused.text
        saved = await web.post(f"{base}/analytics.agent_traffic/setup")
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "connected"
        assert saved.json()["analytics"]["state"] == "waiting"
        api.state = "live"
        api.status_extra = {"uncounted_hosts": [{"host": "newname.com", "pages_7d": 12}]}
        await age(publication_db, project.id, 31)
        refreshed = await web.post(f"{base}/analytics.agent_traffic/refresh")
        assert refreshed.json()["analytics"]["moved_hosts"] == [
            {"host": "newname.com", "pages_7d": 12}
        ]
        moved = await web.post(
            f"{base}/analytics.agent_traffic/address", json={"host": "newname.com"}
        )
        assert moved.status_code == 200 and moved.json()["configuration"]["url"] == (
            "https://newname.com"
        )
        bodies = [listed.text, saved.text, refreshed.text, moved.text]
        stopped = await web.delete(f"{base}/{AGENT_TRAFFIC_PROVIDER}")
        assert stopped.status_code == 204
        after = await web.get(base)
        shown = next(i for i in after.json() if i["key"] == AGENT_TRAFFIC_PROVIDER)
        assert shown["analytics"]["removal"]["state"] == "removing"
        foreign = await web.post(
            f"/api/projects/{uuid4()}/integrations/analytics.agent_traffic/setup"
        )
        assert foreign.status_code == 404
    tokens = [token for token in api.tokens] + [site["read_token"] for site in api.sites.values()]
    issued = [f"read-token-st-fake0001-{n:04d}-synthetic" for n in range(1, api.issued + 1)]
    for body in bodies + [after.text]:
        assert SERVICE_CREDENTIAL not in body
        assert not any(token in body for token in tokens + issued)


async def test_the_gateway_dispatches_to_the_connection(publication_db) -> None:
    from types import SimpleNamespace

    from tin_lite.code_services import CodeServices

    service, api, project = await analytics(publication_db)
    connection = await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=USER_ID)
    gateway = CodeServices(database=publication_db, integrations=service, authorize=None)
    run_id = await publication_db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE project_id = $1", project.id
    )
    run = SimpleNamespace(id=run_id, project_id=project.id)
    result = await gateway.adapter(
        AGENT_TRAFFIC_PROVIDER,
        "report.read",
        {"period": "week"},
        run,
        connection,
        f"{run.id}:code-service:week",
        max_response_bytes=1_000_000,
    )
    assert result["period"] == "week" and api.calls[-1].url.path.endswith("/report")
    ARGUMENT_CHECKS[AGENT_TRAFFIC_PROVIDER]("report.read", {"period": "week"})
    with pytest.raises(ServiceArgumentError):
        ARGUMENT_CHECKS[AGENT_TRAFFIC_PROVIDER]("report.read", {"site_id": "st-other"})
