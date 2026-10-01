"""analytics.posthog: CIMD client, OAuth/PKCE, region, projects, refresh, guard and reads."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from connection_fakes import FIXTURES, FakePostHogConnection, posthog_objects
from test_billing import billed as billed
from test_integrations import PROJECT_ID, RUN_ID, USER_ID, FakeIntegrationDatabase, settings
from test_procedure_publication import publication_db as publication_db

from tin_lite import posthog_connection as ph
from tin_lite.code_services import OPERATIONS as GATEWAY_OPERATIONS
from tin_lite.code_services import CodeServiceError
from tin_lite.connection_records import ServiceArgumentError, json_size
from tin_lite.integrations import (
    POSTHOG_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationRequirement,
    IntegrationService,
    IntegrationUpstreamError,
    ServiceCallRefused,
    registered_integrations,
)
from tin_lite.workflow_services import service_bindings

OBJECTS = posthog_objects()
ACCESS, REFRESH = "pha_" + "a" * 40, "phr_" + "r" * 40
SIGNUPS = (
    "SELECT toDate(timestamp) AS day, count() AS signups FROM events "
    "WHERE event = 'signed_up' AND timestamp >= now() - INTERVAL 7 DAY "
    "GROUP BY day ORDER BY day LIMIT 30"
)


def posthog_settings(**overrides):
    return settings(posthog_oauth_enabled=True, **overrides)


class PostHog:
    """A PostHog stand-in: OAuth token endpoint, both regions, and the reviewed reads."""

    def __init__(self, *, region="eu", token_extra=None, api_status=None):
        self.region, self.requests = region, []
        self.token_extra = {} if token_extra is None else token_extra
        self.api_status: dict[str, int] = api_status or {}
        self.valid_access = {ACCESS}
        self.refresh_tokens = {REFRESH}
        self.refresh_status: int | None = None
        self.rotate = True
        self.issued = 0
        self.challenge: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, host = request.url.path, request.url.host
        if path == "/oauth/token/":
            return self.token(request)
        if path == "/oauth/revoke/":
            return httpx.Response(200, json={})
        if host != f"{self.region}.posthog.com":
            return self.reply(401, {"detail": "wrong region"})
        if request.headers["authorization"].removeprefix("Bearer ") not in self.valid_access:
            return self.reply(401, {"detail": "expired"})
        status = self.api_status.get(path)
        if status == 429:
            return self.reply(429, {"code": "api_queries_budget_exceeded"}, {"Retry-After": "120"})
        if status == 400:
            return self.reply(400, {"detail": f"Unable to resolve field\n{'x' * 900}"})
        if status:
            return self.reply(status, {"detail": "no"})
        if path == "/api/users/@me/":
            return self.reply(200, OBJECTS["user"])
        if path == "/api/projects/":
            return self.reply(200, {"results": OBJECTS["projects"], "next": None})
        if path.startswith("/api/organizations/"):
            return self.reply(200, {"results": OBJECTS["projects"][:1], "next": None})
        fake = FakePostHogConnection(OBJECTS)
        parts = path.strip("/").split("/")
        if len(parts) == 3:
            project = next((p for p in OBJECTS["projects"] if str(p["id"]) == parts[2]), None)
            return self.reply(200, project) if project else self.reply(404, {})
        if parts[3] == "query":
            body = json.loads(request.content)
            return self.reply(200, fake.query(body, body["name"].removeprefix("tin: ")))
        params = dict(request.url.params)
        params["limit"], params["offset"] = int(params["limit"]), int(params["offset"])
        return self.reply(200, fake.page(parts[3], params), {"x-request-id": "req_ph"})

    def token(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert "client_secret" not in form  # a public client: PKCE only
        if form["grant_type"] == "authorization_code":
            assert request.url.host == "oauth.posthog.com"
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenge
            assert form["client_id"] == "https://lite.tin.test/integrations/posthog/client.json"
            if form["code"] != "good-code":
                return self.reply(400, {"error": "invalid_grant"})
            body = {
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "token_type": "Bearer",
                "expires_in": 604800,
                "scope": " ".join(ph.REQUESTED_SCOPES),
                "scoped_teams": [101],
                "scoped_organizations": [],
                "posthog_region": self.region,
                "posthog_base_url": f"https://{self.region}.posthog.com",
            }
            return self.reply(200, {**body, **self.token_extra})
        assert form["grant_type"] == "refresh_token"
        assert request.url.host == f"{self.region}.posthog.com"  # the grant's own region
        assert form["client_id"] == "https://lite.tin.test/integrations/posthog/client.json"
        if self.refresh_status or form["refresh_token"] not in self.refresh_tokens:
            return self.reply(self.refresh_status or 400, {"error": "invalid_grant"})
        self.issued += 1
        access = f"pha_renewed_{self.issued}"
        self.valid_access = {access}
        body = {"access_token": access, "expires_in": 604800, "scope": "project:read"}
        if self.rotate:
            self.refresh_tokens = {f"phr_rotated_{self.issued}"}
            body["refresh_token"] = f"phr_rotated_{self.issued}"
        return self.reply(200, body)

    @staticmethod
    def reply(code, body, headers=None):
        return httpx.Response(
            code, stream=httpx.ByteStream(json.dumps(body).encode()), headers=headers or {}
        )


class LockingDatabase(FakeIntegrationDatabase):
    """The fake database with a real single-flight refresh lock."""

    def __init__(self):
        super().__init__()
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def integration_refresh_lock(self, **values):
        del values
        async with self.lock:
            yield


async def service_for(api, database=None, **overrides):
    database = database or LockingDatabase()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    service = IntegrationService(
        database=database, settings=posthog_settings(**overrides), client=client
    )
    return service, database, client


async def connect(service, api, *, code="good-code", user=USER_ID):
    started = await service.start_connect(
        project_id=PROJECT_ID, provider_key=POSTHOG_PROVIDER, clerk_user_id=USER_ID
    )
    query = parse_qs(urlsplit(started.authorization_url).query)
    api.challenge = query["code_challenge"][0]
    connection = await service.posthog.complete(
        state=query["state"][0], code=code, clerk_user_id=user
    )
    return connection, query


def tokens_of(service, connection):
    return json.loads(
        service._cipher.decrypt(
            connection.credential_ciphertext, context=f"credential:{PROJECT_ID}:{POSTHOG_PROVIDER}"
        )
    )


# ---------------------------------------------------------------- registry and client identity


def test_registry_contracts_and_bindings_are_read_only_and_explicit() -> None:
    definition = next(d for d in registered_integrations() if d.key == POSTHOG_PROVIDER)
    assert definition.capabilities == ("query.read", "definitions.read", "insights.read")
    for name, op in ph.OPERATIONS.items():
        assert GATEWAY_OPERATIONS[(POSTHOG_PROVIDER, name)] == (op.capability, op.arguments)
        assert "project_id" not in op.arguments and "host" not in op.arguments
    requirement = [
        {"provider_key": POSTHOG_PROVIDER, "capabilities": ["query.read"], "required": True}
    ]
    bound = service_bindings(
        {"ph": {"provider_key": POSTHOG_PROVIDER, "max_calls": 8, "max_response_bytes": 64000}},
        requirement,
    )
    assert bound[0].capabilities == ("query.read",)
    # Every requested scope is a read; the required set stays minimal.
    assert all(scope.endswith(":read") for scope in ph.REQUESTED_SCOPES)
    assert ph.REQUIRED_SCOPES == {"project:read"} and ph.REQUIRED_SCOPES <= set(ph.REQUESTED_SCOPES)
    assert ph.capabilities_for({"project:read", "insight:read", "event_definition:read"}) == [
        "insights.read"
    ]


def test_client_metadata_document_derives_from_the_public_url_only() -> None:
    config = posthog_settings(posthog_oauth_verification_token="phvt_" + "v" * 20)
    document = ph.client_metadata(config)
    assert document["client_id"] == "https://lite.tin.test/integrations/posthog/client.json"
    assert document["redirect_uris"] == ["https://lite.tin.test/integrations/callback/posthog"]
    assert document["token_endpoint_auth_method"] == "none"  # noqa: S105 — a public client
    assert document["com.posthog"] == {
        "scopes": list(ph.REQUESTED_SCOPES),
        "verification_token": "phvt_" + "v" * 20,
    }
    assert len(json.dumps(document).encode()) < 5 * 1024  # PostHog's document limit
    assert ph.oauth_ready(config)
    assert not ph.oauth_ready(settings())  # disabled by default
    assert not ph.oauth_ready(posthog_settings(switchboard_public_url="http://127.0.0.1:8000"))
    assert not ph.oauth_ready(posthog_settings(switchboard_public_url="https://localhost"))


async def test_start_connect_uses_pkce_and_asks_for_one_project() -> None:
    api = PostHog()
    service, database, client = await service_for(api)
    async with client:
        started = await service.start_connect(
            project_id=PROJECT_ID, provider_key=POSTHOG_PROVIDER, clerk_user_id=USER_ID
        )
        disabled = IntegrationService(
            database=database, settings=settings(), client=client
        )  # not configured on this deployment
        with pytest.raises(IntegrationNotConfiguredError):
            await disabled.start_connect(
                project_id=PROJECT_ID, provider_key=POSTHOG_PROVIDER, clerk_user_id=USER_ID
            )
    url = urlsplit(started.authorization_url)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://oauth.posthog.com/oauth/authorize/"
    assert query["client_id"] == "https://lite.tin.test/integrations/posthog/client.json"
    assert query["redirect_uri"] == "https://lite.tin.test/integrations/callback/posthog"
    assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
    assert query["required_access_level"] == "project"
    assert set(query["scope"].split()) == set(ph.REQUESTED_SCOPES)
    attempt = next(iter(database.attempts.values()))
    assert attempt.token_hash == hashlib.sha256(query["state"].encode()).hexdigest()
    assert attempt.pkce_verifier_ciphertext and query["state"] not in json.dumps(
        attempt.__dict__, default=str
    )
    assert api.requests == []


# ---------------------------------------------------------------- authorization completion


async def test_complete_stores_encrypted_tokens_region_and_the_consented_project() -> None:
    api = PostHog(region="eu")
    service, database, client = await service_for(api)
    async with client:
        connection, query = await connect(service, api)
        # A repeated callback returns the connection it made, without a second exchange.
        exchanges = sum(r.url.path == "/oauth/token/" for r in api.requests)
        again = await service.posthog.complete(
            state=query["state"][0], code="good-code", clerk_user_id=USER_ID
        )
        assert again.id == connection.id
        assert sum(r.url.path == "/oauth/token/" for r in api.requests) == exchanges == 1
        with pytest.raises(IntegrationAuthorizationError, match="expired"):
            await service.posthog.complete(
                state=query["state"][0], code="good-code", clerk_user_id="user_Other"
            )
    config = connection.configuration
    assert config["region"] == "eu" and config["api_host"] == "https://eu.posthog.com"
    assert config["selected_project_id"] == "101"
    assert config["selected_project_name"] == "Fixture App"
    assert config["oauth_client_id"] == "https://lite.tin.test/integrations/posthog/client.json"
    assert config["granted_capabilities"] == ["query.read", "definitions.read", "insights.read"]
    assert connection.external_account_id == "eu:0192f0a0-0000-7000-8000-0000000000aa"
    assert connection.external_account_label == "Fixture App · EU Cloud"
    stored = tokens_of(service, connection)
    assert stored["access_token"] == ACCESS and stored["refresh_token"] == REFRESH
    assert stored["expires_at"] > time.time() + 600_000
    visible = json.dumps([config, database.activities, database.calls], default=str)
    assert ACCESS not in visible and REFRESH not in visible and "phc_" not in visible
    # API reads went only to the region the token response named.
    assert {r.url.host for r in api.requests if r.url.path.startswith("/api/")} == {
        "eu.posthog.com"
    }


async def test_region_falls_back_to_a_probe_and_never_guesses() -> None:
    api = PostHog(region="eu", token_extra={"posthog_region": None, "posthog_base_url": None})
    service, _, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
    assert connection.configuration["region"] == "eu"
    probes = [r.url.host for r in api.requests if r.url.path == "/api/users/@me/"]
    assert "us.posthog.com" in probes and "eu.posthog.com" in probes
    # An unknown base URL is never used as an API host, and no answer means no connection.
    api = PostHog(region="eu", token_extra={"posthog_region": "ap", "posthog_base_url": "x"})
    api.valid_access = set()
    service, database, client = await service_for(api)
    async with client:
        with pytest.raises(IntegrationUpstreamError, match="US or EU"):
            await connect(service, api)
    assert database.connections == {}


async def test_complete_refuses_bad_codes_and_grants_without_project_read() -> None:
    api = PostHog()
    service, database, client = await service_for(api)
    async with client:
        with pytest.raises(IntegrationAuthorizationError, match="did not accept"):
            await connect(service, api, code="bad-code")
        api.token_extra = {"scope": "query:read"}
        with pytest.raises(IntegrationAuthorizationError, match="project read"):
            await connect(service, api)
        api.token_extra = {"refresh_token": None}
        with pytest.raises(IntegrationAuthorizationError, match="durable access"):
            await connect(service, api)
    assert database.connections == {}


# ---------------------------------------------------------------- projects


async def test_projects_follow_the_grant_and_selection_is_checked() -> None:
    api = PostHog(token_extra={"scoped_teams": [], "scoped_organizations": []})
    service, database, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        assert connection.configuration["selected_project_id"] is None
        with pytest.raises(IntegrationAuthorizationError, match="Choose a PostHog project"):
            await service.ensure_requirements(
                project_id=PROJECT_ID,
                requirements=(IntegrationRequirement(POSTHOG_PROVIDER, ("query.read",)),),
            )
        options = await service.posthog.projects(project_id=PROJECT_ID)
        assert [(o.id, o.label) for o in options] == [
            ("101", "Fixture App"),
            ("102", "Fixture Staging"),
        ]
        assert options[0].detail == "EU Cloud · id 101"
        with pytest.raises(IntegrationAuthorizationError, match="not available"):
            await service.select_option(
                project_id=PROJECT_ID, provider_key=POSTHOG_PROVIDER, option_id="999"
            )
        selected = await service.select_option(
            project_id=PROJECT_ID, provider_key=POSTHOG_PROVIDER, option_id="102"
        )
        assert selected.configuration["selected_project_id"] == "102"
        assert selected.external_account_label == "Fixture Staging · EU Cloud"
        await service.ensure_requirements(
            project_id=PROJECT_ID,
            requirements=(IntegrationRequirement(POSTHOG_PROVIDER, POSTHOG_ALL),),
        )
        # An organization-scoped grant lists that organization's projects only.
        org = "0192f0a0-0000-7000-8000-0000000000ff"
        database.connections[(PROJECT_ID, POSTHOG_PROVIDER)].configuration[
            "scoped_organizations"
        ] = [org]
        await service.posthog.projects(project_id=PROJECT_ID)
    assert api.requests[-1].url.path == f"/api/organizations/{org}/projects/"
    assert [c["capability"] for c in database.calls] == ["projects.list"] * 4


POSTHOG_ALL = ("query.read", "definitions.read", "insights.read")


async def test_missing_capability_scopes_fail_before_a_run() -> None:
    api = PostHog(token_extra={"scope": "project:read insight:read"})
    service, _, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        assert connection.configuration["granted_capabilities"] == ["insights.read"]
        with pytest.raises(IntegrationAuthorizationError, match="query.read"):
            await service.ensure_requirements(
                project_id=PROJECT_ID,
                requirements=(IntegrationRequirement(POSTHOG_PROVIDER, POSTHOG_ALL),),
            )
        with pytest.raises(IntegrationAuthorizationError):
            await service.posthog.call(
                "query.hogql",
                {"query": SIGNUPS},
                connection=connection,
                run_id=RUN_ID,
                execution_key="run:code-service:scope",
                max_response_bytes=8000,
            )


# ---------------------------------------------------------------- refresh


async def expire(database, service):
    connection = database.connections[(PROJECT_ID, POSTHOG_PROVIDER)]
    tokens = {**tokens_of(service, connection), "expires_at": int(time.time()) - 5}
    ciphertext, _ = ph._seal(service, PROJECT_ID, tokens)
    await database.update_integration_credential(
        project_id=PROJECT_ID,
        provider_key=POSTHOG_PROVIDER,
        connection_id=connection.id,
        credential_ciphertext=ciphertext,
        credential_key_version=service._cipher.version,
    )
    return database.connections[(PROJECT_ID, POSTHOG_PROVIDER)]


async def test_refresh_is_single_flight_and_persists_the_rotated_token() -> None:
    api = PostHog()
    service, database, client = await service_for(api)
    async with client:
        await connect(service, api)
        stale = await expire(database, service)
        tokens = await asyncio.gather(*(service.posthog.access_token(stale) for _ in range(5)))
        assert set(tokens) == {"pha_renewed_1"} and api.issued == 1
        stored = tokens_of(service, database.connections[(PROJECT_ID, POSTHOG_PROVIDER)])
        assert stored["refresh_token"] == "phr_rotated_1"  # noqa: S105 — fixture token
        # Configuration is untouched, so a running workflow's binding survives rotation.
        assert database.connections[(PROJECT_ID, POSTHOG_PROVIDER)].configuration == (
            stale.configuration
        )
        # A non-rotating refresh keeps the refresh token it presented.
        api.rotate = False
        stale = await expire(database, service)
        assert await service.posthog.access_token(stale) == "pha_renewed_2"
        stored = tokens_of(service, database.connections[(PROJECT_ID, POSTHOG_PROVIDER)])
        assert stored["refresh_token"] == "phr_rotated_1"  # noqa: S105 — fixture token


async def test_a_rejected_access_token_refreshes_once_and_a_dead_grant_needs_attention() -> None:
    api = PostHog()
    service, database, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        api.valid_access = {"pha_other"}  # the stored token was revoked upstream
        api.refresh_tokens = {REFRESH}
        result = await service.posthog.call(
            "insights.list",
            {},
            connection=connection,
            run_id=RUN_ID,
            execution_key="run:code-service:retry",
            max_response_bytes=64000,
        )
        assert len(result["records"]) == 4 and api.issued == 1
        api.valid_access, api.refresh_status = set(), 400
        with pytest.raises(ph.PostHogUnauthorized) as refused:
            await service.posthog.call(
                "insights.list",
                {},
                connection=database.connections[(PROJECT_ID, POSTHOG_PROVIDER)],
                run_id=RUN_ID,
                execution_key="run:code-service:dead",
                max_response_bytes=64000,
            )
    assert refused.value.code == "reauthorization_required"
    stored = database.connections[(PROJECT_ID, POSTHOG_PROVIDER)]
    assert stored.status == "needs_attention"
    assert stored.last_error_code == "reauthorization_required"
    assert database.calls[-1]["error_code"] == "reauthorization_required"


# ---------------------------------------------------------------- HogQL guard and arguments


@pytest.mark.parametrize(
    "query",
    [
        SIGNUPS,
        "select event from events limit 1",
        "WITH s AS (SELECT distinct_id FROM events LIMIT 5000) SELECT count() FROM s LIMIT 1;",
        "SELECT properties.plan, count() FROM events -- limit 100000\nGROUP BY 1 LIMIT 1000",
        "SELECT 'a;b OFFSET 3 UNION', `offset` FROM events /* LIMIT 9 */ LIMIT 10",
        "SELECT * EXCEPT (properties) FROM events WHERE timestamp < '2026-09-01' LIMIT 50",
        "SELECT distinct_id FROM events LIMIT 1 BY distinct_id LIMIT 100",
    ],
)
def test_hogql_guard_accepts_one_bounded_select(query) -> None:
    assert ph.check_hogql(query) == query.strip().rstrip(";").rstrip()


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("SELECT event FROM events", "explicit LIMIT"),
        ("SELECT event FROM events LIMIT 1001", "from 1 to 1000"),
        ("SELECT event FROM events LIMIT 0", "from 1 to 1000"),
        ("SELECT event FROM events LIMIT 10 OFFSET 10", "OFFSET"),
        ("SELECT event FROM events LIMIT 10, 10", "LIMIT"),
        ("SELECT * FROM (SELECT 1 OFFSET 5) LIMIT 1", "OFFSET"),
        ("SELECT 1 LIMIT 1; SELECT 2 LIMIT 1", "single statement"),
        ("SELECT 1 LIMIT 1;;", "single statement"),
        ("SELECT a FROM x UNION ALL SELECT a FROM y LIMIT 5", "UNION"),
        ("SELECT a FROM x EXCEPT SELECT a FROM y LIMIT 5", "UNION"),
        ("INSERT INTO t VALUES (1) LIMIT 1", "SELECT or WITH"),
        ("SELECT event FROM events # LIMIT 5", "# comments"),
        ("SELECT event FROM events WHERE x = 'open LIMIT 5", "unterminated"),
        ("SELECT (event FROM events LIMIT 5", "parentheses"),
        ("SELECT event FROM events /* LIMIT 5", "unterminated comment"),
        ("SELECT event FROM events LIMIT 5 SETTINGS max_threads=1", "explicit LIMIT"),
        ("SELECT event FROM events WHERE x = 'LIMIT 5'", "explicit LIMIT"),
        ("SELECT '" + "x" * 8100 + "' LIMIT 1", "8000 bytes"),
        ("SELECT 1\x00 LIMIT 1", "control"),
        ("", "HogQL SELECT"),
        (7, "HogQL SELECT"),
    ],
)
def test_hogql_guard_refuses_exports_and_multi_statements(query, message) -> None:
    with pytest.raises(ServiceArgumentError, match=message):
        ph.check_hogql(query)


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("query.hogql", {}, "query is required"),
        ("query.hogql", {"query": SIGNUPS, "project_id": 1}, "unsupported argument project_id"),
        ("query.hogql", {"query": SIGNUPS, "name": "x" * 80}, "name must be"),
        ("query.hogql", {"query": SIGNUPS, "refresh": "force_blocking"}, "unsupported"),
        ("event_definitions.list", {"limit": 101}, "limit must be"),
        ("event_definitions.list", {"cursor": "10001"}, "cursor must be"),
        ("event_definitions.list", {"cursor": -1}, "cursor must be"),
        ("event_definitions.list", {"search": ""}, "search must be"),
        ("insights.list", {"search": "a\nb"}, "search must be"),
        ("property_definitions.list", {"event_names": []}, "event_names must be"),
        ("property_definitions.list", {"event_names": ["a", "a"]}, "event_names must be"),
        ("property_definitions.list", {"event_names": [""]}, "event_names must be"),
        ("insights.delete", {}, "unknown PostHog operation"),
        ("insights.list", ["x"], "arguments must be an object"),
    ],
)
def test_arguments_are_closed_and_validated(operation, arguments, message) -> None:
    with pytest.raises(ServiceArgumentError, match=message):
        ph.request_for(operation, arguments)


def test_arguments_map_to_posthog_requests() -> None:
    _, request = ph.request_for("query.hogql", {"query": SIGNUPS + ";", "name": "signups"})
    assert request["body"] == {
        "query": {"kind": "HogQLQuery", "query": SIGNUPS},
        "refresh": "blocking",
        "name": "tin: signups",
    }
    _, request = ph.request_for(
        "property_definitions.list", {"event_names": ["signed_up"], "cursor": "40", "limit": 20}
    )
    assert request["params"] == {
        "limit": 20,
        "offset": 40,
        "type": "event",
        "event_names": '["signed_up"]',
        "filter_by_event_names": "true",
    }
    _, request = ph.request_for("insights.list", {"search": "funnel"})
    assert request["params"] == {
        "limit": 100,
        "offset": 0,
        "search": "funnel",
        "basic": "true",
        "saved": "true",
    }


# ---------------------------------------------------------------- reads, projection and fit


async def test_hogql_call_posts_to_the_selected_project_and_fits_rows() -> None:
    api = PostHog()
    service, database, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        api.requests.clear()
        query = "SELECT timestamp, distinct_id, plan FROM events LIMIT 20"
        result = await service.posthog.call(
            "query.hogql",
            {"query": query, "name": "recent_signups"},
            connection=connection,
            run_id=RUN_ID,
            execution_key="run:code-service:q",
            max_response_bytes=900,
        )
    [request] = api.requests
    assert (request.method, request.url.host, request.url.path) == (
        "POST",
        "eu.posthog.com",
        "/api/projects/101/query/",
    )
    assert request.headers["authorization"] == f"Bearer {ACCESS}"
    assert json.loads(request.content)["query"] == {"kind": "HogQLQuery", "query": query}
    assert result["columns"] == ["timestamp", "distinct_id", "plan"]
    assert result["types"] == ["DateTime64(6, 'UTC')", "String", "Nullable(String)"]
    assert result["truncated"] is True and result["has_more"] is True
    assert 0 < len(result["rows"]) < 20 and json_size(result) <= 900
    call = database.calls[-1]
    assert call["capability"] == "query.read" and call["status"] == "completed"
    assert call["response_summary"] == {
        "record_count": 20,
        "returned_count": len(result["rows"]),
        "has_more": True,
        "truncated": True,
    }
    assert ACCESS not in json.dumps(database.calls, default=str)


async def test_query_refusals_are_named_and_bounded() -> None:
    api = PostHog(api_status={"/api/projects/101/query/": 429})
    service, database, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        common = {
            "connection": connection,
            "run_id": RUN_ID,
            "max_response_bytes": 8000,
        }
        with pytest.raises(IntegrationRateLimitedError) as limited:
            await service.posthog.call(
                "query.hogql", {"query": SIGNUPS}, execution_key="k:429", **common
            )
        assert limited.value.retry_after == 120 and limited.value.code == "rate_limited"
        assert limited.value.reason == "api_queries_budget_exceeded"
        assert "hourly query budget" in str(limited.value)
        api.api_status["/api/projects/101/query/"] = 400
        with pytest.raises(ServiceCallRefused) as rejected:
            await service.posthog.call(
                "query.hogql", {"query": SIGNUPS}, execution_key="k:400", **common
            )
        assert rejected.value.code == "query_error"
        assert str(rejected.value) == "PostHog rejected the query (HTTP 400)."
        said = rejected.value.provider_error
        assert (said.provider, said.status) == ("PostHog", 400)
        assert said.message.startswith("Unable to resolve field xxx")
        assert "\n" not in said.message and len(said.message) <= 1500
        api.api_status["/api/projects/101/insights/"] = 403
        with pytest.raises(ph.PostHogPermissionDenied):
            await service.posthog.call("insights.list", {}, execution_key="k:403", **common)
    assert [c["error_code"] for c in database.calls[-3:]] == [
        "rate_limited",
        "query_error",
        "permission_denied",
    ]
    assert database.connections[(PROJECT_ID, POSTHOG_PROVIDER)].status == "connected"


async def test_reader_only_sends_reviewed_requests_to_known_hosts() -> None:
    api = PostHog()
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        reader = ph.PostHogReader(client, "https://eu.posthog.com", ACCESS)
        for method, path in [
            ("DELETE", "/api/projects/101/insights/"),
            ("POST", "/api/projects/101/insights/"),
            ("PATCH", "/api/projects/101/"),
            ("GET", "/api/projects/101/persons/"),
            ("GET", "/api/projects/101/query/"),
            ("GET", "/api/projects/../"),
        ]:
            with pytest.raises(ph.PostHogRequestRefused):
                await reader.request(method, path)
        with pytest.raises(ph.PostHogRequestRefused):
            ph.PostHogReader(client, "https://evil.example", ACCESS)
    assert api.requests == []


async def test_list_pages_fit_and_resume_from_the_position_cursor() -> None:
    api = PostHog()
    service, _, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        common = {"connection": connection, "run_id": RUN_ID, "max_response_bytes": 450}
        names, cursor, pages, truncated = [], None, 0, 0
        while True:
            arguments = {"limit": 5, **({"cursor": cursor} if cursor else {})}
            page = await service.posthog.call(
                "event_definitions.list", arguments, execution_key=f"k:{pages}", **common
            )
            assert json_size(page) <= 450
            truncated += page["truncated"]
            names += [r["name"] for r in page["records"]]
            pages += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            assert cursor == str(len(names))
        assert names == [e["name"] for e in OBJECTS["event_definitions"]]
        assert pages >= 3 and truncated


# ---------------------------------------------------------------- offline fake


async def test_recorded_projections_match_the_fixture_objects() -> None:
    recorded = json.loads((FIXTURES / "posthog_projected.json").read_text())
    fake = FakePostHogConnection(OBJECTS)
    for operation, arguments in recorded["_arguments"].items():
        result = await fake.call(
            service="posthog", step=operation, operation=operation, arguments=arguments
        )
        assert result == recorded[operation], operation
    assert [p["name"] for p in recorded["property_definitions.list"]["records"]] == [
        "$browser",
        "plan",
        "signup_source",
    ]
    insights = recorded["insights.list"]["records"]
    assert insights[1]["name"] == "Pageview count"  # derived when unnamed
    assert insights[0]["query_kind"] == "FunnelsQuery"
    text = json.dumps(recorded)
    # Owners, cached results, examples and project API tokens never pass the projection.
    assert "owner@fixture.example" not in text and "hidden@" not in text and "phc_" not in text


async def test_fake_rehearses_limits_refusals_and_gating() -> None:
    fake = FakePostHogConnection(
        OBJECTS, capabilities=("query.read",), max_calls=3, refuse={"late": "budget_exceeded"}
    )
    query = "SELECT timestamp, distinct_id, plan FROM events LIMIT 5"
    first = await fake.call(
        service="posthog",
        step="recent",
        operation="query.hogql",
        arguments={"name": "recent_signups", "query": query},
    )
    assert len(first["rows"]) == 5 and first["has_more"] is True
    with pytest.raises(CodeServiceError, match="contract: query must not use OFFSET"):
        await fake.call(
            service="posthog",
            step="bad",
            operation="query.hogql",
            arguments={"name": "recent_signups", "query": query.replace("LIMIT", "OFFSET 5 LIMIT")},
        )
    with pytest.raises(CodeServiceError, match="declared contract"):
        await fake.call(service="posthog", step="ins", operation="insights.list", arguments={})
    with pytest.raises(CodeServiceError, match="hourly query budget"):
        await fake.call(
            service="posthog",
            step="late",
            operation="query.hogql",
            arguments={"name": "signups_by_day", "query": SIGNUPS},
        )
    with pytest.raises(CodeServiceError, match="hourly query budget"):  # the step replays
        await fake.call(
            service="posthog",
            step="late",
            operation="query.hogql",
            arguments={"name": "signups_by_day", "query": SIGNUPS},
        )
    with pytest.raises(LookupError, match="no canned result"):
        await fake.call(
            service="posthog",
            step="unknown",
            operation="query.hogql",
            arguments={"name": "nothing", "query": "SELECT 1 LIMIT 1"},
        )
    with pytest.raises(CodeServiceError, match="service-call limit"):
        await fake.call(
            service="posthog",
            step="fourth",
            operation="query.hogql",
            arguments={"name": "signups_by_day", "query": SIGNUPS},
        )


# ---------------------------------------------------------------- HTTP surface


async def test_api_serves_the_client_document_and_completes_the_callback() -> None:
    from fastapi import FastAPI

    from tin_lite.api import router
    from tin_lite.auth import AuthContext, require_user

    class AccessDatabase(LockingDatabase):
        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == PROJECT_ID and clerk_user_id == USER_ID

        async def get_project(self, project_id):
            return SimpleNamespace(id=project_id, name="PostHog proof")

    api = PostHog()
    service, _, client = await service_for(api, AccessDatabase())
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(integrations=service, database=service._database)
    app.state.settings = service._settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=USER_ID,
        token_type="session_token",  # noqa: S106 — token category, not a credential
    )
    async with (
        client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as web,
    ):
        document = await web.get("/integrations/posthog/client.json")
        assert document.status_code == 200
        assert document.json() == ph.client_metadata(service._settings)
        assert document.headers["cache-control"] == "public, max-age=3600"
        started = await web.post(
            f"/api/projects/{PROJECT_ID}/integrations/{POSTHOG_PROVIDER}/connect"
        )
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        api.challenge = query["code_challenge"][0]
        body = {"state": query["state"][0], "code": "good-code"}
        done = await web.post("/api/integrations/posthog/complete", json=body)
        assert done.status_code == 200, done.text
        assert done.json()["configuration"]["selected_project_id"] == "101"
        assert ACCESS not in done.text and REFRESH not in done.text
        repeat = await web.post("/api/integrations/posthog/complete", json=body)
        assert (
            repeat.status_code == 200
            and repeat.json()["connection_id"] == (done.json()["connection_id"])
        )
        options = await web.get(
            f"/api/projects/{PROJECT_ID}/integrations/{POSTHOG_PROVIDER}/options"
        )
        assert [o["id"] for o in options.json()] == ["101"]
        api.api_status["/api/projects/101/"] = 429
        limited = await web.get(
            f"/api/projects/{PROJECT_ID}/integrations/{POSTHOG_PROVIDER}/options"
        )
        assert limited.status_code == 429
        service._settings.posthog_oauth_enabled = False
        assert (await web.get("/integrations/posthog/client.json")).status_code == 404


# ---------------------------------------------------------------- gateway with Postgres


async def test_gateway_replays_posthog_steps_and_settles_refusals(billed, monkeypatch):
    """Real Postgres: OAuth attempts, token rotation, per-step replay and a budget 429."""
    from dataclasses import replace

    from temporalio.testing import ActivityEnvironment
    from test_private_workflows import ACTOR
    from test_project_connections import definition, prepared, public_dns

    from tin_lite.workflow_code import validate_code_definition

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))
        )
    ) as crm:
        code.services.client, code.services.resolver = crm, public_dns
        with pytest.raises(RuntimeError, match="worker loss"):
            await ActivityEnvironment().run(code.execute, run_id)
    service._settings.posthog_oauth_enabled = True
    service._settings.switchboard_public_url = "https://lite.tin.test"
    api = PostHog(region="us")
    original = service._client
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    try:
        started = await service.start_connect(
            project_id=f.project.id, provider_key=POSTHOG_PROVIDER, clerk_user_id=ACTOR
        )
        query = parse_qs(urlsplit(started.authorization_url).query)
        api.challenge = query["code_challenge"][0]
        connection = await service.posthog.complete(
            state=query["state"][0], code="good-code", clerk_user_id=ACTOR
        )
        repeat = await service.posthog.complete(
            state=query["state"][0], code="good-code", clerk_user_id=ACTOR
        )
        assert repeat.id == connection.id
        run, workflow, _, _, _ = await code.selected(run_id)
        body = definition()
        body["integration_requirements"] = [
            {
                "provider_key": POSTHOG_PROVIDER,
                "capabilities": ["query.read", "definitions.read"],
                "required": True,
            }
        ]
        body["code"]["services"] = {
            "ph": {"provider_key": POSTHOG_PROVIDER, "max_calls": 4, "max_response_bytes": 4000}
        }
        changed, spec = replace(workflow, definition=body), validate_code_definition(body)

        def call(conn, step, operation, arguments):
            return code.services.call(
                conn=conn,
                run=run,
                workflow=changed,
                spec=spec,
                payload={
                    "service": "ph",
                    "step": step,
                    "operation": operation,
                    "arguments": arguments,
                },
            )

        signups = {"name": "signups_by_day", "query": SIGNUPS}
        async with f.db.pool.acquire() as conn:
            api.requests.clear()
            first = await call(conn, "signups", "query.hogql", signups)
            assert len(first["rows"]) == 7 and len(api.requests) == 1
            assert await call(conn, "signups", "query.hogql", signups) == first
            assert len(api.requests) == 1
            with pytest.raises(CodeServiceError, match="contract: query must end"):
                await call(conn, "bad", "query.hogql", {"query": "SELECT 1"})
            with pytest.raises(CodeServiceError, match="declared contract"):
                await call(conn, "bad", "insights.list", {})  # not a declared capability
            # The stored access token expires: the next step refreshes and rotates it in
            # place, and the pinned binding still matches.
            stored = await f.db.get_integration_connection(
                project_id=f.project.id, provider_key=POSTHOG_PROVIDER
            )
            tokens = {**tokens_of_db(service, stored, f.project.id), "expires_at": 0}
            ciphertext, version = ph._seal(service, f.project.id, tokens)
            assert await f.db.update_integration_credential(
                project_id=f.project.id,
                provider_key=POSTHOG_PROVIDER,
                connection_id=stored.id,
                credential_ciphertext=ciphertext,
                credential_key_version=version,
            )
            events = await call(conn, "events", "event_definitions.list", {"limit": 3})
            assert events["next_cursor"] == "3" and api.issued == 1
            rotated = await f.db.get_integration_connection(
                project_id=f.project.id, provider_key=POSTHOG_PROVIDER
            )
            rotated_refresh = tokens_of_db(service, rotated, f.project.id)["refresh_token"]
            assert rotated_refresh == "phr_rotated_1"  # noqa: S105 — fixture token
            api.api_status["/api/projects/101/query/"] = 429
            with pytest.raises(CodeServiceError, match="hourly query budget"):
                await call(conn, "again", "query.hogql", signups)
            with pytest.raises(CodeServiceError, match="hourly query budget"):
                await call(conn, "again", "query.hogql", signups)
            api.api_status.clear()
            retried = await call(conn, "again_later", "query.hogql", signups)
            assert retried == first
        rows = await f.db.pool.fetch(
            """SELECT operation, status, result FROM effect_receipts
               WHERE operation IN ('code_service_call_v1', 'external_usage_v1')"""
        )
        assert rows and all(row["status"] == "completed" for row in rows)
        dumped = json.dumps([dict(r) for r in rows], default=str)
        assert ACCESS not in dumped and "phr_" not in dumped and "pha_" not in dumped
        receipts = await f.db.pool.fetch(
            "SELECT status, error_code FROM integration_call_receipts WHERE provider_key=$1",
            POSTHOG_PROVIDER,
        )
        assert sorted((r["status"], r["error_code"] or "") for r in receipts) == [
            ("completed", ""),
            ("completed", ""),
            ("completed", ""),
            ("failed", "rate_limited"),
        ]
    finally:
        await service._client.aclose()
        service._client = original
        await service.close()


def tokens_of_db(service, connection, project_id):
    return json.loads(
        service._cipher.decrypt(
            connection.credential_ciphertext,
            context=f"credential:{project_id}:{POSTHOG_PROVIDER}",
        )
    )
