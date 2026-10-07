from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from tin_lite.connection_collection import (
    KEY,
    POLICY,
    Actor,
    CollectionError,
    CollectionInputs,
    CollectionSource,
    PageBatch,
    failure_transition,
    profile_url,
)
from tin_lite.connection_collection_activities import artifacts
from tin_lite.connection_collection_store import CollectionStore, record, token_hash
from tin_lite.db import Database, apply_migrations

ACTOR = {
    "key": "https://www.linkedin.com/in/owner",
    "name": "Owner",
    "profile_url": "https://www.linkedin.com/in/owner",
}
FRIEND = "https://www.linkedin.com/in/friend"
TOKEN = "synthetic-device-token-for-tests-only-123456789"  # noqa: S105
USER = "user_collectiontest"


def source(page=1):
    return {
        "friend_url": FRIEND,
        "friend_name": "Friend",
        "actor": ACTOR,
        "first_degree": True,
        "collection_url": "https://www.linkedin.com/search/results/people/?connectionOf=%5B%22member1%22%5D&network=%5B%22S%22%5D&page="
        + str(page),
    }


def batch(claim, page=1, *, more=True, person="person", index=0):
    return {
        "generation": claim["generation"],
        "lease": claim["lease"],
        "friend_index": index,
        "page": page,
        "actor_key": ACTOR["key"],
        "source": source(page),
        "people": [
            {
                "profile_url": "https://www.linkedin.com/in/" + person,
                "name": "=Example",
                "degree": "2nd",
            }
        ],
        "next_page": more,
        "observed_at": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
async def collection_db():
    dsn = os.environ.get("TIN_LITE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set TIN_LITE_TEST_DATABASE_DSN to an isolated disposable Postgres")
    schema = "collection_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    db = Database(dsn)
    try:
        db._pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=10, server_settings={"search_path": schema}
        )
        await apply_migrations(
            dsn + ("? " if "?" not in dsn else "&").strip() + f"search_path={schema}",
            Path(__file__).parents[1] / "migrations",
        )
        yield db
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def seed(db):
    project = await db.create_project(
        name="Synthetic collection", state_repo_id="projects/" + uuid4().hex
    )
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER)
    settings = SimpleNamespace(connection_collection_projects=[str(project.id)])
    store = CollectionStore(db, settings)
    grant = await store.grant(project.id, USER)
    await store.pair(grant["grant"], token_hash(TOKEN), ACTOR)
    workflow = await db.pool.fetchval(
        """INSERT INTO workflows
        (id,key,title,executor,definition_repo_id,definition_path,current_commit_sha,version_label,definition)
        VALUES ($1,$2,'Connections',$2,'registry/workflows','connections.json',$3,'v1','{}')
        RETURNING id""",
        uuid4(),
        KEY,
        "d" * 40,
    )
    run_id = uuid4()
    inputs = {"project_id": str(project.id), "friends": [FRIEND]}
    await db.pool.execute(
        """INSERT INTO workflow_runs
        (id,project_id,workflow_id,executor,definition_commit_sha,temporal_workflow_id,thread_id,
        generation,fencing_token,status,started_by_clerk_user_id,input)
        VALUES ($1,$2,$3,$4,$5,$6,'collection',1,1,'running',$7,$8::jsonb)""",
        run_id,
        project.id,
        workflow,
        KEY,
        "d" * 40,
        "collection-" + str(run_id),
        USER,
        json.dumps(inputs),
    )
    run = await db.get_run(run_id)
    await store.prepare(run, POLICY)
    return SimpleNamespace(store=store, project=project, run=run, grant=grant, db=db)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.linkedin.com/in/a",
        "https://evil.test/in/a",
        "https://www.linkedin.com/in/a%2fb",
        "https://www.linkedin.com:443/in/a",
        "https://a@www.linkedin.com/in/a",
        "https://www.linkedin.com/in/%00",
    ],
)
def test_closed_profile_urls(url):
    with pytest.raises(ValueError):
        profile_url(url)


def test_filter_semantics_cover_linkedin_next_serialization():
    old = CollectionSource.model_validate(source())
    assert old.scope(keywords="") == "member1"
    new = source(2)
    new["collection_url"] = (
        new["collection_url"].replace("%5B%22member1%22%5D", "%22member1%22")
        + "&spellCorrectionEnabled=true&prioritizeMessage=false&origin=FACETED_SEARCH"
    )
    assert CollectionSource.model_validate(new).scope(keywords="") == "member1"
    for change in ["&network=%5B%22F%22%5D", "&currentCompany=x", "&keywords=another"]:
        with pytest.raises(CollectionError):
            CollectionSource.model_validate(
                {**source(), "collection_url": source()["collection_url"] + change}
            ).scope(keywords="")


def test_contract_rejects_unknown_scope_and_ambiguous_rows():
    with pytest.raises(ValidationError):
        CollectionInputs.model_validate(
            {"project_id": str(uuid4()), "friends": [FRIEND], "cookie": "secret"}
        )
    c = {"generation": 1, "lease": "x" * 40}
    b = batch(c)
    b["people"].append(b["people"][0])
    with pytest.raises(ValidationError):
        PageBatch.model_validate(b)
    with pytest.raises(ValidationError):
        Actor.model_validate({**ACTOR, "key": "random"})


def test_fallback_is_not_a_challenge_bypass():
    assert failure_transition("cloud_preferred", "cloud", "cloud_unavailable") == "handoff_pending"
    assert failure_transition("cloud_only", "cloud", "cloud_unavailable") == "failed"
    for reason in ["challenge", "rate_limited", "account_changed", "access_denied"]:
        assert failure_transition("cloud_preferred", "cloud", reason) == "paused"


async def test_pairing_idempotency_membership_and_device_scope(collection_db):
    f = await seed(collection_db)
    result = await f.store.pair(f.grant["grant"], token_hash(TOKEN), ACTOR)
    assert result["project_id"] == str(f.project.id)
    with pytest.raises(CollectionError, match="pairing_consumed"):
        await f.store.pair(f.grant["grant"], token_hash("another"), ACTOR)
    with pytest.raises(CollectionError):
        await f.store.pending(uuid4(), TOKEN)
    await f.db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
    with pytest.raises(CollectionError, match="project_unavailable"):
        await f.store.pending(f.project.id, TOKEN)


async def test_one_lease_fencing_and_final_page_lost_ack(collection_db):
    f = await seed(collection_db)

    async def claim():
        return await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])

    claims = await asyncio.gather(claim(), claim(), return_exceptions=True)
    assert sum(isinstance(c, dict) for c in claims) == 1
    current = next(c for c in claims if isinstance(c, dict))
    first = batch(current)
    accepted = await f.store.page(f.run.id, f.project.id, TOKEN, first)
    assert accepted["next_page"] == 2
    assert (await f.store.page(f.run.id, f.project.id, TOKEN, first))["next_page"] == 2
    await f.db.pool.execute(
        "UPDATE connection_collection_jobs SET lease_expires_at=now()-interval '1 second' "
        "WHERE run_id=$1",
        f.run.id,
    )
    renewed = await claim()
    assert renewed["next_page"] == 2 and renewed["generation"] == current["generation"] + 1
    with pytest.raises(CollectionError, match="stale_lease"):
        await f.store.page(
            f.run.id, f.project.id, TOKEN, batch(current, 2, more=False, person="second")
        )
    final = batch(renewed, 2, more=False, person="second")
    assert (await f.store.page(f.run.id, f.project.id, TOKEN, final))["state"] == "completed"
    assert (await f.store.page(f.run.id, f.project.id, TOKEN, final))["state"] == "completed"
    assert await f.db.pool.fetchval("SELECT count(*) FROM connection_collection_pages") == 2
    job = record(await f.db.pool.fetchrow("SELECT * FROM connection_collection_jobs"))
    pages = [
        record(r)
        for r in await f.db.pool.fetch("SELECT * FROM connection_collection_pages ORDER BY page")
    ]
    files = artifacts(job, pages)
    assert all(len(value) <= 64000 for key, value in files.items() if key.endswith(".json"))
    assert "'=Example" in next(
        value.decode() for key, value in files.items() if key.endswith(".csv")
    )
    manifest = json.loads(
        next(value for key, value in files.items() if key.endswith("manifest.json"))
    )
    assert manifest["people_count"] == 2 and manifest["path_count"] == 2


async def test_repeated_page_and_stopped_run_cannot_continue(collection_db):
    f = await seed(collection_db)
    c = await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])
    await f.store.page(f.run.id, f.project.id, TOKEN, batch(c))
    with pytest.raises(CollectionError, match="repeated_page"):
        await f.store.page(f.run.id, f.project.id, TOKEN, batch(c, 2))
    await f.db.pool.execute("UPDATE workflow_runs SET status='stopped' WHERE id=$1", f.run.id)
    with pytest.raises(CollectionError, match="collection_stopped"):
        await f.store.page(f.run.id, f.project.id, TOKEN, batch(c, 2, person="second"))


def session_envelope():
    return {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/151.0.0.0",
        "cookies": [
            {
                "name": name,
                "value": "synthetic-" + name,
                "domain": ".linkedin.com",
                "path": "/",
                "secure": True,
                "http_only": True,
                "same_site": "lax",
            }
            for name in ["li_at", "JSESSIONID"]
        ],
        "browser_context": {
            "accept_language": "en-US,en;q=0.9",
            "li_lang": "en_US",
            "li_track": {
                "clientVersion": "1.13.99",
                "osName": "web",
                "timezoneOffset": -7,
                "timezone": "America/Los_Angeles",
                "deviceFormFactor": "DESKTOP",
                "mpName": "voyager-web",
                "displayDensity": 2,
            },
        },
    }


async def cloud_fixture(db):
    import base64

    from tin_lite.integrations import CredentialCipher

    f = await seed(db)
    f.store.settings.linkedin_cloud_template = "isolated-test-image"
    from pydantic import SecretStr

    f.store.settings.linkedin_cloud_qualified = True
    f.store.settings.linkedin_e2b_api_key = SecretStr("synthetic-dedicated-key")
    f.store.settings.integration_credential_key = SecretStr("synthetic-cipher-key")
    inputs = {**f.run.input, "execution": "cloud_preferred", "keywords": ""}
    await db.pool.execute(
        "UPDATE connection_collection_jobs SET "
        "inputs=$2::jsonb,cloud_template='isolated-test-image',cloud_transport='http_v1' "
        "WHERE run_id=$1",
        f.run.id,
        json.dumps(inputs),
    )
    f.claim = await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])
    f.cipher = CredentialCipher(base64.urlsafe_b64encode(b"x" * 32).decode())
    await f.store.cloud_session(
        f.run.id,
        f.project.id,
        TOKEN,
        f.claim["generation"],
        f.claim["lease"],
        session_envelope(),
        f.cipher,
    )
    view = {**source(), "query_id": "voyagerSearchDashClusters." + "a" * 32}
    await f.store.cloud_source(
        f.run.id, f.project.id, TOKEN, f.claim["generation"], f.claim["lease"], view
    )
    return f


async def test_cloud_cleanup_then_local_handoff_preserves_pages_and_limits(collection_db):
    from tin_lite.connection_collection_cloud import step

    f = await cloud_fixture(collection_db)

    class Cloud:
        async def page(self, job, session, source):
            assert session["browser_context"]["li_track"]["displayDensity"] == 2
            await collection_db.pool.execute(
                "UPDATE connection_collection_jobs SET cloud_cleanup_confirmed=true WHERE "
                "run_id=$1",
                f.run.id,
            )
            if job["next_page"] == 1:
                return {
                    "people": [
                        {
                            "name": "Synthetic Person",
                            "profile_url": "https://www.linkedin.com/in/person",
                            "degree": "2nd",
                        }
                    ],
                    "next_page": True,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            return {"error": "cloud_failed"}

    original = await f.store.pending(f.project.id, TOKEN)
    await step(f.store, Cloud(), f.cipher, f.run.id)
    assert (await f.store.pending(f.project.id, TOKEN))["next_page"] == 2
    await step(f.store, Cloud(), f.cipher, f.run.id)
    handed = await f.store.pending(f.project.id, TOKEN)
    assert handed["state"] == "handoff_pending"
    claimed = await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])
    assert claimed["cloud_transport"] == "local_backup" and claimed["next_page"] == 2
    assert claimed["deadline"] == original["deadline"]
    assert (
        await f.store.page(
            f.run.id, f.project.id, TOKEN, batch(claimed, 2, more=False, person="second")
        )
    )["state"] == "completed"


@pytest.mark.parametrize(
    "reason",
    ["challenge", "rate_limited", "account_changed", "access_denied", "cloud_cleanup_pending"],
)
async def test_cloud_stops_do_not_bypass_to_local(collection_db, reason):
    from tin_lite.connection_collection_cloud import step

    f = await cloud_fixture(collection_db)

    class Cloud:
        async def page(self, job, session, source):
            await collection_db.pool.execute(
                "UPDATE connection_collection_jobs SET cloud_cleanup_confirmed=true WHERE "
                "run_id=$1",
                f.run.id,
            )
            raise CollectionError(reason)

    await step(f.store, Cloud(), f.cipher, f.run.id)
    assert (await f.store.pending(f.project.id, TOKEN))["state"] == "paused"
    with pytest.raises(CollectionError):
        await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])


async def test_session_cookie_context_validation_and_no_redirects():
    import httpx

    from tin_lite.linkedin_session import headers, read_page, validate_session

    captured = session_envelope()
    # Published capture preserves browser flags. A language preference may be
    # non-secure; the transport still sends every cookie only to its fixed HTTPS origin.
    captured["cookies"].append(
        {**captured["cookies"][0], "name": "lang", "value": "v=2&lang=en-us", "secure": False}
    )
    envelope = validate_session(captured)
    actual = headers(envelope)
    assert json.loads(actual["x-li-track"]) == session_envelope()["browser_context"]["li_track"]
    assert actual["referer"] == "https://www.linkedin.com/preload/?_bprMode=vanilla"
    assert '"macOS"' == actual["sec-ch-ua-platform"]
    seen = []

    async def respond(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://www.linkedin.com/checkpoint/test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CollectionError, match="challenge"):
            await read_page(envelope, source(), "", 1, client=client)
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path == "/voyager/api/me"
    bad = session_envelope()
    bad["cookies"][0]["value"] = "secret\r\nInjected: secret"
    with pytest.raises(CollectionError, match="invalid_session"):
        validate_session(bad)


def test_graphql_parser_excludes_unreferenced_people_and_checks_page_scope():
    from tin_lite.linkedin_session import search_page, search_request

    entity = {
        "entityUrn": "urn:person:1",
        "navigationUrl": "https://www.linkedin.com/in/one",
        "title": {"text": "One"},
        "entityCustomTrackingInfo": {"memberDistance": "DISTANCE_2"},
    }
    payload = {
        "included": [
            entity,
            {
                **entity,
                "entityUrn": "urn:recommendation:1",
                "navigationUrl": "https://www.linkedin.com/in/unrelated",
            },
        ],
        "data": {
            "data": {
                "searchDashClustersByAll": {
                    "paging": {"start": 10, "count": 10, "total": 11},
                    "elements": [{"items": [{"item": {"*entityResult": "urn:person:1"}}]}],
                }
            }
        },
    }
    parsed = search_page(payload, 2)
    assert len(parsed["people"]) == 1 and parsed["next_page"] is False
    with pytest.raises(CollectionError):
        search_page(payload, 1)
    request = search_request(
        {**source(2), "query_id": "voyagerSearchDashClusters." + "a" * 32}, "", 2
    )
    assert (
        "start:10" in request["variables"] and "(key:network,value:List(S))" in request["variables"]
    )
    entity["entityCustomTrackingInfo"]["memberDistance"] = "DISTANCE_1"
    with pytest.raises(CollectionError, match="filters_changed"):
        search_page(payload, 2)


async def test_dedicated_cloud_image_receipts_cleanup_and_secret_boundary(
    collection_db, monkeypatch
):
    from pydantic import SecretStr

    from tin_lite import linkedin_cloud
    from tin_lite.linkedin_session import validate_session

    f = await cloud_fixture(collection_db)
    f.store.settings.e2b_api_key = SecretStr("synthetic-provider-key")
    job = record(
        await collection_db.pool.fetchrow(
            "SELECT * FROM connection_collection_jobs WHERE run_id=$1", f.run.id
        )
    )
    calls = []

    class Sandbox:
        sandbox_id = "synthetic-cloud-sandbox"

        def __init__(self):
            self.files = self.commands = self
            self.input = None

        async def write(self, path, data, **kwargs):
            assert path == "/run/tin-collection/request.json" and kwargs["user"] == "root"
            self.input = json.loads(data)

        async def read(self, path, **kwargs):
            assert kwargs["user"] == "root"
            return json.dumps(
                {
                    "people": [
                        {
                            "name": "Fixture",
                            "profile_url": "https://www.linkedin.com/in/fixture",
                            "degree": "2nd",
                        }
                    ],
                    "next_page": False,
                }
            )

        async def run(self, cmd, **kwargs):
            assert "synthetic-li_at" not in cmd and kwargs["user"] == "root"
            calls.append("command")

        async def get_info(self):
            return SimpleNamespace(
                metadata={"run_id": str(f.run.id), "profile": "linkedin_http_v1"},
                started_at=datetime.now(UTC),
                cpu_count=1,
                memory_mb=1024,
            )

        async def kill(self):
            calls.append("kill")

    sandbox = Sandbox()

    class Provider:
        @staticmethod
        async def create(template, **kwargs):
            assert template == "isolated-test-image"
            assert kwargs["api_key"] == "synthetic-dedicated-key"
            assert kwargs["lifecycle"] == {"on_timeout": "kill", "auto_resume": False}
            assert kwargs["network"]["allow_out"] == ["www.linkedin.com"]
            assert kwargs["metadata"]["profile"] == "linkedin_http_v1"
            calls.append("create")
            return sandbox

        @staticmethod
        async def connect(identifier, **kwargs):
            assert identifier == sandbox.sandbox_id
            assert kwargs["api_key"] == "synthetic-dedicated-key"
            return sandbox

    monkeypatch.setattr(linkedin_cloud, "AsyncSandbox", Provider)
    runtime = linkedin_cloud.LinkedInCloud(collection_db, f.store.settings)
    session = validate_session(session_envelope())
    response = await runtime.page(job, session, source())
    again = await runtime.page(job, session, source())
    assert again == response and calls == ["create", "command", "kill"]
    assert sandbox.input["session"]["browser_context"] == session["browser_context"]
    assert "cookies" not in json.dumps(response) and "synthetic-li_at" not in json.dumps(response)
    usage = await collection_db.get_effect("sandbox-usage:" + sandbox.sandbox_id)
    assert usage.result["outcome"] == "deleted"
    assert usage.result["observed_wall_seconds"] is not None


def test_cloud_gate_requires_separate_credentials_and_qualification():
    from pydantic import SecretStr

    from tin_lite.connection_collection import cloud_ready

    settings = SimpleNamespace(
        linkedin_cloud_template="dedicated-v1",
        linkedin_cloud_qualified=True,
        integration_credential_key=SecretStr("synthetic-cipher"),
        e2b_api_key=SecretStr("ordinary-compute"),
    )
    assert not cloud_ready(settings)
    settings.linkedin_e2b_api_key = SecretStr("ordinary-compute")
    assert not cloud_ready(settings)
    settings.linkedin_e2b_api_key = SecretStr("separate-compute")
    assert cloud_ready(settings)
    settings.linkedin_cloud_qualified = False
    assert not cloud_ready(settings)


def test_page_byte_limit_is_independent_of_record_limit():
    value = batch({"generation": 1, "lease": "l" * 32})
    value["people"] = [
        {
            "name": "N" * 200,
            "profile_url": f"https://www.linkedin.com/in/person-{index}",
            "headline": "H" * 1000,
            "visible_text": "T" * 3000,
            "degree": "2nd",
        }
        for index in range(50)
    ]
    with pytest.raises(ValueError, match="page_too_large"):
        PageBatch.model_validate(value)


async def test_lost_source_ack_and_local_claim_while_cloud_active(collection_db):
    f = await cloud_fixture(collection_db)
    view = {**source(), "query_id": "voyagerSearchDashClusters." + "a" * 32}
    repeated = await f.store.cloud_source(
        f.run.id, f.project.id, TOKEN, f.claim["generation"], f.claim["lease"], view
    )
    assert repeated["state"] == "cloud_ready"
    await collection_db.pool.execute(
        "UPDATE connection_collection_jobs SET state='collecting',execution_mode='cloud',"
        "cloud_cleanup_confirmed=true WHERE run_id=$1",
        f.run.id,
    )
    with pytest.raises(CollectionError, match="cloud_active"):
        await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])


@pytest.mark.parametrize("state", ["completed", "partial", "failed"])
async def test_publication_retains_coverage_without_hiding_failure(
    collection_db, monkeypatch, state
):
    from temporalio.exceptions import ApplicationError

    from tin_lite import connection_collection_activities as activities

    f = await seed(collection_db)
    await collection_db.pool.execute(
        "UPDATE connection_collection_jobs SET state=$2 WHERE run_id=$1", f.run.id, state
    )

    async def publish(**kwargs):
        manifest = json.loads(kwargs["documents"][f"connections/{f.run.id}/manifest.json"])
        assert manifest["status"] == state
        await kwargs["validate_active"]()
        return "f" * 40

    monkeypatch.setattr(activities, "publish_artifacts", publish)
    implementation = activities.CollectionActivities(
        database=collection_db, storage=None, settings=f.store.settings
    )
    if state == "failed":
        with pytest.raises(ApplicationError, match="collection_failed"):
            await implementation.publish(str(f.run.id))
    else:
        await implementation.publish(str(f.run.id))
    projected = await collection_db.get_run(f.run.id)
    assert projected.status.value == ("failed" if state == "failed" else "succeeded")
    assert projected.artifact_path == f"connections/{f.run.id}/manifest.json"
    assert projected.artifact_ref == "f" * 40


async def test_http_extension_origin_and_secret_errors_are_closed(collection_db):
    import httpx
    from fastapi import FastAPI

    from tin_lite.connection_collection_api import router

    f = await seed(collection_db)
    app = FastAPI()
    app.include_router(router)
    f.store.settings.linkedin_extension_ids = ("syntheticextension",)
    app.state.settings = f.store.settings
    app.state.runtime = SimpleNamespace(database=collection_db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        path = f"/api/projects/{f.project.id}/connection-extension/pending"
        denied = await client.get(path, headers={"Authorization": "Bearer " + TOKEN})
        assert denied.status_code == 403
        headers = {"Authorization": "Bearer " + TOKEN, "X-Tin-Extension-Id": "syntheticextension"}
        assert (await client.get(path, headers=headers)).status_code == 200
        bad = await client.post(
            f"/api/projects/{f.project.id}/connection-extension/{f.run.id}/page",
            headers=headers,
            json={"cookies": "must-never-echo-this-secret"},
        )
        assert bad.status_code == 422 and "must-never-echo" not in bad.text
        bad = await client.post(
            f"/api/projects/{f.project.id}/connection-extension/{f.run.id}/heartbeat",
            headers=headers,
            json={"generation": 1, "lease": 5},
        )
        assert bad.status_code == 422
