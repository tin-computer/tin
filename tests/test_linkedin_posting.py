"""Fixture-only LinkedIn posting connection through a synthetic Zernio."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from test_integrations import PROJECT_ID, USER_ID, FakeIntegrationDatabase, settings
from test_procedure_publication import publication_db as publication_db

from tin_lite.integrations import (
    LINKEDIN_POSTING_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationService,
    IntegrationUpstreamError,
    registered_integrations,
)
from tin_lite.linkedin_posting import may_post_as, member_view, owners
from tin_lite.settings import Settings
from tin_lite.zernio import Zernio, linkedin_account

KEY = "zernio_synthetic_key_value"
PROFILE = "6a" + "0" * 22
OTHER_PROFILE = "6b" + "0" * 22
PERSONAL = "6c" + "0" * 22
PAGE = "6d" + "0" * 22
FOREIGN = "6e" + "0" * 22
TEAMMATE_PROFILE = "6f" + "0" * 22
TEAMMATE = "user_Teammate789"
FOUNDER_URN = "urn:li:person:AZperson1"
TEAMMATE_URN = "urn:li:person:AZperson2"


EXPIRES = "2026-12-12T15:17:32.872Z"


def account(account_id, kind, *, profile=PROFILE, active=True, person="AZperson1", expires=EXPIRES):
    metadata = (
        {
            "accountType": "organization",
            "organizationInfo": {"urn": "urn:li:organization:108774412", "name": "Example Co"},
            "userProfile": {"id": "AZperson1"},
        }
        if kind == "organization"
        else {"accountType": "personal", "profileData": {"id": person}}
    )
    return {
        "_id": account_id,
        "platform": "linkedin",
        "displayName": "Example Co" if kind == "organization" else "Sam Founder",
        "isActive": active,
        "enabled": True,
        "metadata": metadata,
        "profileId": {"_id": profile, "name": "tin:x"},
        "tokenExpiresAt": expires,
    }


class Zernio_:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.profile_status = 201
        self.profiles: list[dict] = []
        self.accounts = [account(PERSONAL, "personal"), account(PAGE, "organization")]
        self.auth_url = "https://www.linkedin.com/oauth/v2/authorization?client_id=z"
        self.fail = None

    @staticmethod
    def reply(status, body=None, headers=None):
        return httpx.Response(
            status, stream=httpx.ByteStream(json.dumps(body or {}).encode()), headers=headers
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["user-agent"].startswith("Tin")
        if self.fail is not None:
            return self.fail(request)
        path, method = request.url.path, request.method
        if (method, path) == ("GET", "/api/v1/profiles"):
            name = request.url.params["name"]
            return self.reply(
                200, {"profiles": [item for item in self.profiles if item["name"] == name]}
            )
        if (method, path) == ("POST", "/api/v1/profiles"):
            body = json.loads(request.content)
            assert body["externalId"] == str(PROJECT_ID)
            assert request.headers["idempotency-key"] == f"tin-profile-{PROJECT_ID}"
            if self.profile_status == 409:
                return self.reply(
                    409,
                    {"code": "profile_name_conflict", "details": {"existingProfileId": PROFILE}},
                )
            profile = {"_id": PROFILE, "name": body["name"], "externalId": body["externalId"]}
            self.profiles.append(profile)
            if self.profile_status == 422:
                # Another request made it first.
                return self.reply(422, {"error": "Profile already exists"})
            return self.reply(201, {"profile": profile})
        if (method, path) == ("GET", "/api/v1/connect/linkedin"):
            return self.reply(200, {"authUrl": self.auth_url})
        if (method, path) == ("GET", "/api/v1/accounts"):
            assert request.url.params["platform"] == "linkedin"
            return self.reply(200, {"accounts": self.accounts})
        if method == "DELETE" and path.startswith(("/api/v1/accounts/", "/api/v1/profiles/")):
            return self.reply(200, {"message": "deleted"})
        raise AssertionError(f"unexpected Zernio route {method} {path}")

    def paths(self, method=None):
        return [(r.method, r.url.path) for r in self.calls if method in (None, r.method)]


def service(api=None, **overrides):
    api = api or Zernio_()
    db = FakeIntegrationDatabase()
    values = {
        "zernio_api_key": SecretStr(KEY),
        "linkedin_posting_projects": (str(PROJECT_ID),),
        "zernio_profile_map": {},
        "deployment_environment": "production",
    }
    values.update(overrides)
    integrations = IntegrationService(
        database=db,
        settings=settings(**values),
        client=httpx.AsyncClient(transport=httpx.MockTransport(api)),
    )
    return integrations, db, api


async def start(integrations, member=USER_ID):
    return await integrations.start_connect(
        project_id=PROJECT_ID, provider_key=LINKEDIN_POSTING_PROVIDER, clerk_user_id=member
    )


def state_from(api) -> str:
    connect = [r for r in api.calls if r.url.path == "/api/v1/connect/linkedin"][-1]
    redirect = urlsplit(connect.url.params["redirect_url"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == (
        "https://lite.tin.test/integrations/callback/linkedin"
    )
    assert connect.url.params["scopes"] == "posting,analytics,comments"
    return parse_qs(redirect.query)["state"][0]


def test_registry_declares_linkedin_posting_separately_from_collection() -> None:
    definitions = {item.key: item for item in registered_integrations()}
    posting = definitions[LINKEDIN_POSTING_PROVIDER]
    assert posting.capabilities == ("linkedin.posts.publish", "linkedin.media.upload")
    assert definitions["network.linkedin"].capabilities == ("connections.read",)


@pytest.mark.asyncio
async def test_the_card_is_shown_only_to_listed_projects_with_a_key() -> None:
    integrations, _, _ = service()
    keys = {d.key for d in integrations.definitions([], project_id=PROJECT_ID)}
    assert LINKEDIN_POSTING_PROVIDER in keys
    other = {d.key for d in integrations.definitions([], project_id=None)}
    assert LINKEDIN_POSTING_PROVIDER not in other
    unlisted, _, api = service(linkedin_posting_projects=())
    with pytest.raises(IntegrationNotConfiguredError):
        await start(unlisted)
    unkeyed, _, _ = service(zernio_api_key=None)
    assert not unkeyed.is_configured(LINKEDIN_POSTING_PROVIDER)
    with pytest.raises(IntegrationNotConfiguredError):
        await start(unkeyed)
    assert api.calls == []


@pytest.mark.asyncio
async def test_connect_makes_the_project_profile_and_records_its_accounts() -> None:
    integrations, db, api = service()
    started = await start(integrations)
    assert started.authorization_url == api.auth_url
    connection = await integrations.linkedin_posting.complete(
        state=state_from(api), clerk_user_id=USER_ID
    )
    assert connection.provider_key == LINKEDIN_POSTING_PROVIDER
    assert connection.external_account_id == PROFILE
    assert connection.credential_ciphertext is None
    assert connection.external_account_label == "Sam Founder, Example Co"
    assert connection.configuration["profile_mapped"] is False
    assert [(a["id"], a["kind"], a["urn"]) for a in connection.configuration["accounts"]] == [
        (PERSONAL, "personal", "urn:li:person:AZperson1"),
        (PAGE, "organization", "urn:li:organization:108774412"),
    ]
    assert db.activities[-1]["event_type"] == "integration_connected"
    # A repeated callback returns the same connection without reading again.
    reads = len(api.calls)
    again = await integrations.linkedin_posting.complete(
        state=state_from(api), clerk_user_id=USER_ID
    )
    assert again.id == connection.id and len(api.calls) == reads


@pytest.mark.asyncio
async def test_each_profile_belongs_to_the_member_who_signed_in_as_it() -> None:
    integrations, db, api = service()
    posting = integrations.linkedin_posting
    api.accounts = []
    await start(integrations)
    # The founder signs in as themselves and picks the page.
    api.accounts = [account(PERSONAL, "personal"), account(PAGE, "organization")]
    first = await posting.complete(state=state_from(api), clerk_user_id=USER_ID)
    assert first.configuration["owners"] == {FOUNDER_URN: USER_ID}
    # A teammate signs in later and adds their own profile.
    await start(integrations, TEAMMATE)
    api.accounts.append(account(TEAMMATE_PROFILE, "personal", person="AZperson2"))
    second = await posting.complete(state=state_from(api), clerk_user_id=TEAMMATE)
    assert owners(second) == {FOUNDER_URN: USER_ID, TEAMMATE_URN: TEAMMATE}
    founder, page, teammate = (
        {item["id"]: item for item in second.configuration["accounts"]}[key]
        for key in (PERSONAL, PAGE, TEAMMATE_PROFILE)
    )
    assert may_post_as(second, founder, USER_ID) and not may_post_as(second, founder, TEAMMATE)
    assert may_post_as(second, teammate, TEAMMATE) and not may_post_as(second, teammate, USER_ID)
    assert may_post_as(second, page, USER_ID) and may_post_as(second, page, TEAMMATE)
    # Check again keeps who owns what.
    checked = await posting.check(PROJECT_ID)
    assert owners(checked) == owners(second)
    # Each member is shown what they may post as, and no member IDs.
    for member, expected in ((USER_ID, [True, True, False]), (TEAMMATE, [False, True, True])):
        shown = member_view(second, member)
        assert "owners" not in shown
        assert [a["can_post"] for a in shown["accounts"]] == expected
        assert USER_ID not in json.dumps(shown) and TEAMMATE not in json.dumps(shown)


@pytest.mark.asyncio
async def test_a_profile_already_in_zernio_before_the_sign_in_has_no_owner() -> None:
    integrations, _, api = service()
    # Left from an earlier connection whose cleanup didn't finish.
    api.accounts = [account(PERSONAL, "personal")]
    await start(integrations)
    api.accounts.append(account(PAGE, "organization"))
    connection = await integrations.linkedin_posting.complete(
        state=state_from(api), clerk_user_id=USER_ID
    )
    assert connection.configuration["owners"] == {}
    founder = next(a for a in connection.configuration["accounts"] if a["id"] == PERSONAL)
    assert not may_post_as(connection, founder, USER_ID)


@pytest.mark.asyncio
async def test_members_connect_one_at_a_time() -> None:
    integrations, _, api = service()
    await start(integrations)
    await start(integrations)
    state = state_from(api)
    with pytest.raises(IntegrationAuthorizationError, match="Another member is connecting"):
        await start(integrations, TEAMMATE)
    await integrations.linkedin_posting.complete(state=state, clerk_user_id=USER_ID)
    # Connecting closed the founder's other tab, so the teammate may start.
    await start(integrations, TEAMMATE)


@pytest.mark.asyncio
async def test_a_connection_from_before_owners_belongs_to_whoever_connected_it() -> None:
    integrations, db, _ = service()
    accounts = [
        linkedin_account(account(PERSONAL, "personal")),
        linkedin_account(account(PAGE, "organization")),
    ]
    legacy = await db.upsert_integration_connection(
        project_id=PROJECT_ID,
        provider_key=LINKEDIN_POSTING_PROVIDER,
        external_account_id=PROFILE,
        external_account_label="Sam Founder, Example Co",
        configuration={"zernio_profile_id": PROFILE, "accounts": accounts},
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=USER_ID,
    )
    assert owners(legacy) == {FOUNDER_URN: USER_ID}
    checked = await integrations.linkedin_posting.check(PROJECT_ID)
    assert checked.configuration["owners"] == {FOUNDER_URN: USER_ID}


@pytest.mark.asyncio
async def test_an_existing_profile_is_reused_on_a_name_conflict() -> None:
    api = Zernio_()
    api.profile_status = 409
    integrations, _, _ = service(api)
    await start(integrations)
    assert (
        next(r for r in api.calls if r.url.path == "/api/v1/connect/linkedin").url.params[
            "profileId"
        ]
        == PROFILE
    )


@pytest.mark.asyncio
async def test_a_profile_made_earlier_is_found_before_creating_one() -> None:
    api = Zernio_()
    api.profiles = [{"_id": PROFILE, "name": f"tin:{PROJECT_ID}", "externalId": str(PROJECT_ID)}]
    integrations, _, _ = service(api)
    await start(integrations)
    assert ("POST", "/api/v1/profiles") not in api.paths()
    # Zernio refuses a profile another request made meanwhile; Tin finds that one.
    api = Zernio_()
    api.profile_status = 422
    integrations, _, _ = service(api)
    await start(integrations)
    assert api.paths("GET").count(("GET", "/api/v1/profiles")) == 2


@pytest.mark.asyncio
async def test_signing_in_again_as_a_profile_already_there_makes_it_yours() -> None:
    integrations, _, api = service()
    # The founder connected this profile outside Tin, before Tin recorded owners.
    api.accounts = [account(PERSONAL, "personal"), account(PAGE, "organization")]
    await start(integrations)
    renewed = "2026-12-14T01:20:00.000Z"
    api.accounts = [
        account(PERSONAL, "personal", expires=renewed),
        account(PAGE, "organization", expires=renewed),
    ]
    connection = await integrations.linkedin_posting.complete(
        state=state_from(api), clerk_user_id=USER_ID
    )
    assert connection.configuration["owners"] == {FOUNDER_URN: USER_ID}


@pytest.mark.asyncio
async def test_only_the_project_profiles_active_accounts_count() -> None:
    api = Zernio_()
    api.accounts = [
        account(FOREIGN, "organization", profile=OTHER_PROFILE),
        account(PAGE, "organization", active=False),
        {"_id": PERSONAL, "platform": "twitter", "metadata": {}},
    ]
    integrations, db, _ = service(api)
    await start(integrations)
    with pytest.raises(IntegrationAuthorizationError, match="No LinkedIn account"):
        await integrations.linkedin_posting.complete(state=state_from(api), clerk_user_id=USER_ID)
    assert db.connections == {}


@pytest.mark.asyncio
async def test_another_member_cannot_finish_the_attempt() -> None:
    integrations, _, api = service()
    await start(integrations)
    with pytest.raises(IntegrationAuthorizationError, match="expired"):
        await integrations.linkedin_posting.complete(
            state=state_from(api), clerk_user_id="user_Other456"
        )


@pytest.mark.asyncio
async def test_connect_refuses_a_redirect_off_linkedin_or_zernio() -> None:
    api = Zernio_()
    api.auth_url = "https://evil.example/oauth"
    integrations, _, _ = service(api)
    with pytest.raises(IntegrationUpstreamError):
        await start(integrations)


@pytest.mark.asyncio
async def test_disconnect_removes_the_accounts_and_profile_from_zernio() -> None:
    integrations, db, api = service()
    await start(integrations)
    await integrations.linkedin_posting.complete(state=state_from(api), clerk_user_id=USER_ID)
    assert await integrations.disconnect(
        project_id=PROJECT_ID, provider_key=LINKEDIN_POSTING_PROVIDER
    )
    assert api.paths("DELETE") == [
        ("DELETE", f"/api/v1/accounts/{PERSONAL}"),
        ("DELETE", f"/api/v1/accounts/{PAGE}"),
        ("DELETE", f"/api/v1/profiles/{PROFILE}"),
    ]
    assert db.connections == {}


@pytest.mark.asyncio
async def test_staging_uses_only_a_mapped_profile_and_never_changes_it() -> None:
    integrations, _, api = service(deployment_environment="staging")
    with pytest.raises(IntegrationNotConfiguredError, match="mapped"):
        await start(integrations)
    assert api.calls == []

    integrations, db, api = service(
        deployment_environment="staging", zernio_profile_map={str(PROJECT_ID): PROFILE}
    )
    started = await start(integrations)
    callback = urlsplit(started.authorization_url)
    assert callback.path == "/integrations/callback/linkedin"
    connection = await integrations.linkedin_posting.complete(
        state=parse_qs(callback.query)["state"][0], clerk_user_id=USER_ID
    )
    assert connection.configuration["profile_mapped"] is True
    # A mapped profile has no sign-in: the member who connects it owns its profiles.
    assert connection.configuration["owners"] == {FOUNDER_URN: USER_ID}
    assert ("POST", "/api/v1/profiles") not in api.paths()
    await integrations.disconnect(project_id=PROJECT_ID, provider_key=LINKEDIN_POSTING_PROVIDER)
    assert api.paths("DELETE") == []
    assert db.connections == {}


@pytest.mark.asyncio
async def test_check_again_reads_accounts_and_flags_a_profile_with_none() -> None:
    integrations, db, api = service()
    await start(integrations)
    await integrations.linkedin_posting.complete(state=state_from(api), clerk_user_id=USER_ID)
    api.accounts = [account(PAGE, "organization", active=False)]
    updated = await integrations.linkedin_posting.check(PROJECT_ID)
    assert updated.status == "needs_attention"
    assert updated.configuration["accounts"][0]["active"] is False


@pytest.mark.asyncio
async def test_provider_failures_name_tin_reasons_and_never_the_key() -> None:
    def status(code, headers=None):
        return lambda request: Zernio_.reply(code, {"error": f"echo {KEY}"}, headers)

    for code, error in [
        (500, IntegrationUpstreamError),
        (401, IntegrationUpstreamError),
        (429, IntegrationRateLimitedError),
    ]:
        api = Zernio_()
        api.fail = status(code, {"retry-after": "7"})
        integrations, _, _ = service(api)
        with pytest.raises(error) as raised:
            await start(integrations)
        assert KEY not in str(raised.value)
    api = Zernio_()
    api.fail = status(503)
    client = Zernio(
        httpx.AsyncClient(transport=httpx.MockTransport(api)),
        settings(zernio_api_key=SecretStr(KEY)),
    )
    with pytest.raises(IntegrationDeliveryUnknownError):
        await client.disconnect_account(PAGE)


def test_accounts_without_a_linkedin_identity_are_dropped() -> None:
    assert linkedin_account(account(PAGE, "organization"))["urn"] == (
        "urn:li:organization:108774412"
    )
    broken = account(PAGE, "organization")
    broken["metadata"]["organizationInfo"]["urn"] = "urn:li:person:x"
    assert linkedin_account(broken) is None
    assert linkedin_account({**account(PAGE, "personal"), "_id": "../profiles"}) is None


def test_profile_map_setting_is_validated(monkeypatch) -> None:
    from test_gak import settings_values

    mapped = f"{PROJECT_ID}={PROFILE}"
    good = Settings(
        _env_file=None, **settings_values(monkeypatch, TIN_LITE_ZERNIO_PROFILE_MAP=mapped)
    )
    assert good.zernio_profile_map == {str(PROJECT_ID): PROFILE}
    for bad in ("not-a-project=" + PROFILE, f"{PROJECT_ID}=short"):
        with pytest.raises(ValueError, match="ZERNIO_PROFILE_MAP"):
            Settings(
                _env_file=None, **settings_values(monkeypatch, TIN_LITE_ZERNIO_PROFILE_MAP=bad)
            )


@pytest.mark.asyncio
async def test_real_postgres_accepts_the_posting_connection(publication_db):
    from uuid import uuid4

    db = publication_db
    project = await db.create_project(name="LinkedIn posting", state_repo_id=f"projects/{uuid4()}")
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER_ID)
    wire = Zernio_()
    integrations = IntegrationService(
        database=db,
        settings=settings(
            zernio_api_key=SecretStr(KEY),
            linkedin_posting_projects=(str(project.id),),
            zernio_profile_map={str(project.id): PROFILE},
            deployment_environment="staging",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    started = await integrations.start_connect(
        project_id=project.id, provider_key=LINKEDIN_POSTING_PROVIDER, clerk_user_id=USER_ID
    )
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    teammate = {
        "project_id": project.id,
        "provider_key": LINKEDIN_POSTING_PROVIDER,
        "clerk_user_id": TEAMMATE,
    }
    with pytest.raises(IntegrationAuthorizationError, match="Another member"):
        await integrations.start_connect(**teammate)
    connection = await integrations.linkedin_posting.complete(state=state, clerk_user_id=USER_ID)
    stored = await db.get_integration_connection(
        project_id=project.id, provider_key=LINKEDIN_POSTING_PROVIDER
    )
    assert stored.id == connection.id and stored.credential_ciphertext is None
    assert len(stored.configuration["accounts"]) == 2
    assert stored.configuration["owners"] == {FOUNDER_URN: USER_ID}
    await integrations.start_connect(**teammate)
    assert await integrations.disconnect(
        project_id=project.id, provider_key=LINKEDIN_POSTING_PROVIDER
    )
    assert wire.paths("DELETE") == []
