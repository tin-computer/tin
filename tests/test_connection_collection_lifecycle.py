"""Continuing consent, encrypted reuse and unattended preparation, using synthetic accounts."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from test_connection_collection import (
    ACTOR,
    FRIEND,
    TOKEN,
    USER,
    cloud_fixture,
    session_envelope,
    source,
)
from test_connection_collection import collection_db as collection_db

from tin_lite.connection_collection import POLICY_V2, CollectionError
from tin_lite.connection_collection_activities import CollectionActivities
from tin_lite.connection_collection_cloud import step
from tin_lite.connection_collection_connection import CollectionConnection, purge_sessions
from tin_lite.connection_collection_store import document, record, token_hash
from tin_lite.linkedin_session import read_page, resolved_source

QUERY = "voyagerSearchDashClusters." + "a" * 32


def choice(mode="cloud_preferred"):
    return {"actor": ACTOR, "mode": mode, "consent_version": 1}


async def verified(*_):
    return None


async def prepared(db):
    f = await cloud_fixture(db)
    await db.pool.execute(
        "UPDATE connection_collection_jobs SET state='completed' WHERE run_id=$1", f.run.id
    )
    await db.pool.execute(
        "DELETE FROM connection_collection_account_leases WHERE run_id=$1", f.run.id
    )
    f.connection = CollectionConnection(f.store, f.cipher, verify=verified)
    await f.connection.preferences(f.project.id, USER, choice())
    envelope = session_envelope()
    envelope["cookies"][0]["expiration_date"] = (datetime.now(UTC) + timedelta(days=3)).timestamp()
    f.upload = {
        "actor_key": ACTOR["key"],
        "session": envelope,
        "query_id": QUERY,
        "expected_generation": None,
    }
    f.status = await f.connection.save_session(f.project.id, TOKEN, f.upload)
    return f


async def new_run(f, *, friends=None, mode="cloud_preferred"):
    identifier = uuid4()
    inputs = {"friends": friends or [FRIEND], "keywords": "", "execution": mode}
    await f.db.pool.execute(
        """INSERT INTO workflow_runs
        (id,project_id,workflow_id,executor,definition_commit_sha,temporal_workflow_id,thread_id,
         generation,fencing_token,status,started_by_clerk_user_id,input)
        SELECT $2,project_id,workflow_id,executor,definition_commit_sha,$3,$3,
          1,1,'running',started_by_clerk_user_id,$4::jsonb FROM workflow_runs WHERE id=$1""",
        f.run.id,
        identifier,
        "collection-" + str(identifier),
        json.dumps(inputs),
    )
    run = await f.db.get_run(identifier)
    return await f.store.prepare(run, POLICY_V2)


async def config(f):
    return await f.db.pool.fetchrow(
        "SELECT * FROM integration_connections WHERE project_id=$1 "
        "AND provider_key='network.linkedin'",
        f.project.id,
    )


async def test_setup_grant_binds_account_and_does_not_upgrade_old_permission(collection_db):
    f = await cloud_fixture(collection_db)
    assert not (await CollectionConnection(f.store).device_status(f.project.id, TOKEN))[
        "permission"
    ]
    grant = await f.store.grant(f.project.id, USER, choice())
    with pytest.raises(CollectionError, match="account_changed"):
        await f.store.pair(
            grant["grant"],
            token_hash("new-device"),
            {**ACTOR, "key": FRIEND, "profile_url": FRIEND},
        )
    with pytest.raises(CollectionError, match="project_unavailable"):
        await f.store.grant(f.project.id, "unrelated-member", choice())


async def test_reusable_session_retained_after_cleanup_and_next_run(collection_db):
    f = await prepared(collection_db)
    before = await config(f)
    assert "synthetic-li_at" not in json.dumps(f.status)
    assert "cookies" not in before["configuration"]
    sealed = json.loads(
        f.cipher.decrypt(
            before["credential_ciphertext"], context=f"credential:{f.project.id}:network.linkedin"
        )
    )
    assert sealed["version"] == 2 and "run_id" not in sealed
    assert (
        2.9
        < (datetime.fromisoformat(sealed["expires_at"]) - datetime.now(UTC)).total_seconds() / 86400
        <= 3
    )
    activity = CollectionActivities(database=f.db, storage=None, settings=f.store.settings)
    await activity.release(f.run.id)
    assert (await config(f))["credential_ciphertext"] == before["credential_ciphertext"]
    first = await new_run(f)
    assert first["state"] == "cloud_ready" and first["sources"] == {}
    assert first["credential_generation"] == sealed["generation"]
    await f.db.pool.execute(
        "UPDATE connection_collection_jobs SET state='completed' WHERE run_id=$1", first["run_id"]
    )
    await activity.release(first["run_id"])
    second = await new_run(f, friends=["https://www.linkedin.com/in/another-friend"])
    assert second["state"] == "cloud_ready" and second["sources"] == {}
    assert (await config(f))["credential_ciphertext"] == before["credential_ciphertext"]


async def test_consent_revocation_fences_pending_and_rejects_transfer(collection_db):
    f = await prepared(collection_db)
    job = await new_run(f)
    await f.connection.preferences(f.project.id, USER, choice("local_only"))
    assert (await config(f))["credential_ciphertext"] is None
    job = await f.db.pool.fetchrow(
        "SELECT * FROM connection_collection_jobs WHERE run_id=$1", job["run_id"]
    )
    assert job["state"] == "paused" and job["reason"] == "cloud_permission_required"
    with pytest.raises(CollectionError, match="cloud_permission_required"):
        await f.connection.save_session(f.project.id, TOKEN, f.upload)
    with pytest.raises(CollectionError, match="account_owner_required"):
        await f.db.grant_project_membership(
            project_id=f.project.id, clerk_user_id="user_othermember"
        )
        await f.connection.preferences(f.project.id, "user_othermember", choice())


async def test_session_refresh_cannot_overwrite_newer_capture_or_active_lease(collection_db):
    f = await prepared(collection_db)
    with pytest.raises(CollectionError, match="session_superseded"):
        await f.connection.save_session(f.project.id, TOKEN, f.upload)
    job = await new_run(f, mode="local_only")
    await f.store.claim(job["run_id"], f.project.id, TOKEN, ACTOR["key"])
    with pytest.raises(CollectionError, match="collection_active"):
        await f.connection.save_session(
            f.project.id, TOKEN, {**f.upload, "expected_generation": f.status["session_generation"]}
        )


@pytest.mark.parametrize("reason", ["expiry", "membership", "device"])
async def test_retained_sessions_are_erased_without_a_new_run(collection_db, reason):
    f = await prepared(collection_db)
    if reason == "expiry":
        await f.db.pool.execute(
            "UPDATE integration_connections SET configuration=jsonb_set(configuration,"
            "'{session_expires_at}',to_jsonb((now()-interval '1 minute')::text)) "
            "WHERE project_id=$1",
            f.project.id,
        )
    elif reason == "membership":
        await f.db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
    else:
        await f.db.pool.execute(
            "UPDATE connection_extension_devices SET revoked_at=now() WHERE project_id=$1",
            f.project.id,
        )
    await purge_sessions(f.db)
    assert (await config(f))["credential_ciphertext"] is None
    assert document((await config(f))["configuration"])["session_state"] == "reconnect"


async def test_waiting_does_not_spend_the_active_collection_time(collection_db):
    f = await prepared(collection_db)
    job = await new_run(f, mode="local_only")
    assert job["collection_started_at"] is None
    assert job["deadline"] - datetime.now(UTC) > timedelta(hours=23)
    claim = await f.store.claim(job["run_id"], f.project.id, TOKEN, ACTOR["key"])
    deadline = datetime.fromisoformat(claim["deadline"])
    assert timedelta(minutes=44) < deadline - datetime.now(UTC) <= timedelta(minutes=45)
    await f.db.pool.execute(
        "UPDATE connection_collection_jobs SET lease_expires_at=now()-interval '1 second' "
        "WHERE run_id=$1",
        job["run_id"],
    )
    again = await f.store.claim(job["run_id"], f.project.id, TOKEN, ACTOR["key"])
    assert again["deadline"] == claim["deadline"]


def profile_response():
    return {
        "included": [
            {
                "entityUrn": "urn:li:fsd_profile:member1",
                "publicIdentifier": "friend",
                "firstName": "Test",
                "lastName": "Friend",
                "*memberRelationship": "urn:relation:1",
                "connectionsUrl": source()["collection_url"],
            },
            {"entityUrn": "urn:relation:1", "distance": "DISTANCE_1"},
        ]
    }


def unresolved():
    return {"friend_url": FRIEND, "actor": ACTOR, "query_id": QUERY}


def test_profile_resolver_requires_evidence_linked_to_exact_friend():
    raw = profile_response()
    result = resolved_source(raw, unresolved(), "founder")
    assert "keywords=founder" in result["collection_url"] and result["first_degree"]
    hidden = profile_response()
    hidden["included"].append({"connectionsUrl": hidden["included"][0].pop("connectionsUrl")})
    with pytest.raises(CollectionError, match="browser_preparation_required"):
        resolved_source(hidden, unresolved(), "")
    raw["included"][0]["*memberRelationship"] = "urn:missing"
    with pytest.raises(CollectionError, match="browser_preparation_required"):
        resolved_source(raw, unresolved(), "")
    raw = profile_response()
    raw["included"][1]["distance"] = "DISTANCE_2"
    with pytest.raises(CollectionError, match="friend_not_connected"):
        resolved_source(raw, unresolved(), "")
    raw["included"][0]["publicIdentifier"] = "unrelated"
    with pytest.raises(CollectionError, match="browser_preparation_required"):
        resolved_source(raw, unresolved(), "")


async def test_cloud_prepares_new_friend_and_collects_without_browser(collection_db):
    f = await prepared(collection_db)
    job = await new_run(f)
    requests = []

    def respond(request):
        requests.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("/me"):
            return httpx.Response(
                200, json={"data": {"miniProfile": {"publicIdentifier": "owner"}}}
            )
        if request.url.path.endswith("/profiles"):
            return httpx.Response(200, json=profile_response())
        assert (
            "connectionOf" in request.url.params["variables"]
            and "List(S)" in request.url.params["variables"]
        )
        return httpx.Response(
            200,
            json={
                "included": [],
                "data": {
                    "data": {
                        "searchDashClustersByAll": {
                            "paging": {"start": 0, "count": 10, "total": 0},
                            "elements": [],
                        }
                    }
                },
            },
        )

    class Cloud:
        async def page(self, job, session, source):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await read_page(session, source, "", 1, client=client)
                return {**result, "observed_at": datetime.now(UTC).isoformat()}

    await step(f.store, Cloud(), f.cipher, job["run_id"])
    result = record(
        await f.db.pool.fetchrow(
            "SELECT * FROM connection_collection_jobs WHERE run_id=$1", job["run_id"]
        )
    )
    assert result["state"] == "completed"
    assert result["sources"]["0"]["friend_url"] == FRIEND
    assert requests == [
        "/voyager/api/me",
        "/voyager/api/identity/dash/profiles",
        "/voyager/api/graphql",
    ]


@pytest.mark.parametrize("query_present", [True, False])
async def test_unsupported_cloud_preparation_waits_for_browser_without_resetting_progress(
    collection_db,
    query_present,
):
    f = await prepared(collection_db)
    if not query_present:
        status = await f.connection.save_session(
            f.project.id,
            TOKEN,
            {**f.upload, "query_id": None, "expected_generation": f.status["session_generation"]},
        )
        assert status["session_available"]
    job = await new_run(f)
    calls = []

    class Cloud:
        async def page(self, *_):
            calls.append(True)
            return {"error": "browser_preparation_required"}

    await step(f.store, Cloud(), f.cipher, job["run_id"])
    assert len(calls) == int(query_present)
    pending = await f.store.pending(f.project.id, TOKEN)
    assert (
        pending["state"] == "waiting_browser"
        and pending["reason"] == "browser_preparation_required"
    )
    await f.store.activate_cloud(job["run_id"])
    assert (await f.store.pending(f.project.id, TOKEN))["state"] == "waiting_browser"
    claim = await f.store.claim(job["run_id"], f.project.id, TOKEN, ACTOR["key"])
    result = await f.store.cloud_source(
        job["run_id"],
        f.project.id,
        TOKEN,
        claim["generation"],
        claim["lease"],
        {**source(), "query_id": QUERY},
    )
    assert result["state"] == "cloud_ready" and result["friend_index"] == 0
    assert result["deadline"] == claim["deadline"]


async def test_reusable_session_rejects_wrong_account_and_expired_auth_cookie(collection_db):
    f = await prepared(collection_db)
    packet = {**f.upload, "expected_generation": f.status["session_generation"]}
    with pytest.raises(CollectionError, match="account_changed"):
        await f.connection.save_session(f.project.id, TOKEN, {**packet, "actor_key": FRIEND})
    packet["session"]["cookies"][0]["expiration_date"] = datetime.now(UTC).timestamp() - 60
    with pytest.raises(CollectionError, match="session_expired"):
        await f.connection.save_session(f.project.id, TOKEN, packet)


async def test_connection_endpoints_keep_user_device_and_project_authority_separate(
    collection_db, monkeypatch
):
    from types import SimpleNamespace

    from fastapi import FastAPI

    from tin_lite.auth import AuthContext
    from tin_lite.connection_collection_api import router
    from tin_lite.project_connections_api import setup_user

    f = await prepared(collection_db)
    monkeypatch.setattr("tin_lite.linkedin_session.check_account", verified)
    f.store.settings.linkedin_extension_ids = ["fixture-extension"]
    app = FastAPI()
    app.include_router(router)
    app.state.settings = f.store.settings
    app.state.runtime = SimpleNamespace(
        database=f.db, integrations=SimpleNamespace(_cipher=f.cipher)
    )
    app.dependency_overrides[setup_user] = lambda: AuthContext(
        clerk_user_id=USER,
        token_type="session_token",  # noqa: S106
    )
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "X-Tin-Extension-Id": "fixture-extension",
        "Origin": "chrome-extension://fixture-extension",
    }
    prefix = f"/api/projects/{f.project.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        for suffix in ("status", "session"):
            path = prefix + "/connection-extension/" + suffix
            request = client.get if suffix == "status" else client.post
            assert (await request(path)).status_code == 403
            assert (
                await request(path, headers={**headers, "Origin": "https://untrusted.test"})
            ).status_code == 403
            assert (
                await request(
                    path,
                    headers={**headers, "Authorization": "Bearer wrong"},
                    **({"json": f.upload} if suffix == "session" else {}),
                )
            ).status_code == 409
        response = await client.get(prefix + "/connection-extension/status", headers=headers)
        assert response.status_code == 200 and response.json()["session_available"]
        assert "cookies" not in response.text and "ciphertext" not in response.text
        response = await client.post(
            prefix + "/connection-extension/session",
            headers=headers,
            json={**f.upload, "expected_generation": f.status["session_generation"]},
        )
        assert response.status_code == 200 and response.json()["session_available"]
        response = await client.get(
            f"/api/projects/{uuid4()}/connection-extension/status", headers=headers
        )
        assert response.status_code == 409
        # Old installed versions send either no content type or an empty JSON body.
        for extra in ({}, {"Content-Type": "application/json"}):
            response = await client.post(prefix + "/connection-collection/pairing", headers=extra)
            assert response.status_code == 200
        response = await client.post(
            prefix + "/connection-collection/pairing",
            json={**choice(), "consent_version": True},
        )
        assert response.status_code == 409 and "invalid_setup" in response.text
        response = await client.put(
            prefix + "/connection-collection/preferences", json=choice("local_only")
        )
        assert response.status_code == 200 and not response.json()["session_available"]
        assert (await config(f))["credential_ciphertext"] is None


async def test_cloud_only_permission_does_not_silently_authorize_browser_backup(collection_db):
    f = await prepared(collection_db)
    await f.connection.preferences(f.project.id, USER, choice("cloud_only"))
    f.store.settings.linkedin_cloud_enabled = False
    job = await new_run(f)
    assert job["state"] == "paused" and job["reason"] == "local_permission_required"
    assert job["collection_started_at"] is None


async def test_explicit_refresh_resumes_cloud_only_without_resetting_checkpoint(collection_db):
    f = await prepared(collection_db)
    job = await new_run(f, mode="cloud_only")
    await f.db.pool.execute(
        "UPDATE connection_collection_jobs SET state='paused',reason='session_expired',"
        "execution_mode='cloud',next_page=2,collection_started_at=now(),"
        "deadline=now()+interval '10 minutes' WHERE run_id=$1",
        job["run_id"],
    )
    refreshed = await f.connection.save_session(
        f.project.id, TOKEN, {**f.upload, "expected_generation": f.status["session_generation"]}
    )
    await f.store.resume(job["run_id"], f.project.id, USER)
    current = record(
        await f.db.pool.fetchrow(
            "SELECT * FROM connection_collection_jobs WHERE run_id=$1", job["run_id"]
        )
    )
    assert current["state"] == "cloud_ready" and current["next_page"] == 2
    assert current["credential_generation"] == refreshed["session_generation"]
    assert current["deadline"] - datetime.now(UTC) < timedelta(minutes=10)


async def test_cloud_dispatch_rechecks_revocation_before_purchasing_compute(collection_db):
    from tin_lite.linkedin_cloud import LinkedInCloud

    f = await prepared(collection_db)
    job = await new_run(f)
    await f.db.pool.execute(
        "UPDATE connection_collection_jobs SET state='collecting',execution_mode='cloud',"
        "lease_expires_at=now()+interval '90 seconds' WHERE run_id=$1",
        job["run_id"],
    )
    runtime = LinkedInCloud(f.db, f.store.settings)
    await runtime.authorize(job)
    await f.connection.preferences(f.project.id, USER, choice("local_only"))
    # page checks authority before entering the receipt / E2B purchase path.
    with pytest.raises(CollectionError, match="cloud_permission_required"):
        await runtime.page(job, session_envelope(), source())


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"miniProfile": {"publicIdentifier": "owner"}}, None),
        (200, {"miniProfile": {"publicIdentifier": "someone-else"}}, "account_changed"),
        (200, {"included": []}, "unsupported_identity"),
        (401, {}, "session_expired"),
        (403, {}, "access_denied"),
        (429, {}, "rate_limited"),
        (302, {}, "challenge"),
    ],
)
async def test_setup_checks_the_transferred_account_without_following_redirects(
    status_code, payload, expected
):
    from tin_lite.linkedin_session import check_account, validate_session

    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/voyager/api/me"
        assert request.url.host == "www.linkedin.com"
        assert request.headers["x-li-track"] and request.headers["csrf-token"]
        return httpx.Response(
            status_code,
            json=payload,
            headers={"location": "https://www.linkedin.com/checkpoint/private-token"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if expected:
            with pytest.raises(CollectionError, match=expected):
                await check_account(validate_session(session_envelope()), ACTOR, client=client)
        else:
            await check_account(validate_session(session_envelope()), ACTOR, client=client)
    assert len(calls) == 1


@pytest.mark.parametrize("existing", [False, True])
async def test_failed_setup_check_cannot_mark_a_session_ready(collection_db, existing):
    f = await prepared(collection_db)
    if not existing:
        await f.connection.preferences(f.project.id, USER, choice("local_only"))
        await f.connection.preferences(f.project.id, USER, choice())
    before = await config(f)

    async def rejected(*_):
        raise CollectionError("account_changed")

    f.connection.verify = rejected
    with pytest.raises(CollectionError, match="account_changed"):
        await f.connection.save_session(
            f.project.id,
            TOKEN,
            {
                **f.upload,
                "expected_generation": f.status["session_generation"] if existing else None,
            },
        )
    after = await config(f)
    assert after["credential_ciphertext"] == before["credential_ciphertext"]
    assert after["configuration"] == before["configuration"]
