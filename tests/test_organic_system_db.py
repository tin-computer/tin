"""Disposable Postgres schemas: pinned child preparation and no-change receipts."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db
from test_technical_fix_sources import content_source_fixture, source_fixture
from test_technical_title_repair import AFTER, BEFORE, archive, manifest, spec

from tin_lite import organic_system
from tin_lite import technical_fix as technical_contract
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.db import SideEffectConflictError
from tin_lite.organic_system_activities import OrganicSystemActivities
from tin_lite.organic_system_control import stop_system
from tin_lite.run_reports import publish_run_report
from tin_lite.technical_fix_execution import TechnicalFixExecution, prepared_result
from tin_lite.workflow_inputs import normalize_workflow_inputs


async def technical_fixture(
    db, monkeypatch, *, html=BEFORE, source_html=BEFORE, overlap=False, source=None
):
    activities, storage, original, _ = await activity_fixture(db)
    source = source or source_fixture()
    source.project.id = source.run.project_id = original.project_id
    source.selection["project_id"] = original.project_id
    source.evidence["project_id"] = str(original.project_id)
    source.seal()
    selection = await source.service.preflight(**source.selection)
    inputs = {
        key: str(value) if key.endswith("run_id") else value
        for key, value in source.selection.items()
        if key != "project_id"
    }
    await db.pool.execute(
        "UPDATE workflows SET key='organic.technical_fix' WHERE id=$1", original.workflow_id
    )
    await db.pool.execute(
        "UPDATE workflow_runs SET input=$2::jsonb, lease_active=false WHERE id=$1",
        original.id,
        json.dumps(inputs),
    )
    monkeypatch.setattr(db, "has_project_access", AsyncMock(return_value=True))
    preview = AsyncMock(return_value=selection)
    monkeypatch.setattr("tin_lite.technical_fix_execution.TechnicalFixSources.preflight", preview)
    fetch = AsyncMock(
        return_value={
            "url": "https://example.com/page-000",
            "redirects": [],
            "status_code": 200,
            "observed_at": "2026-09-09T00:00:00Z",
            "html": html,
            "has_title": html == AFTER,
            "sha256": hashlib.sha256(html.encode()).hexdigest(),
        }
    )
    integrations = SimpleNamespace(
        github_repository_bundle=AsyncMock(
            return_value=SimpleNamespace(archive=archive({"index.html": source_html}))
        ),
        github_open_pull_requests=AsyncMock(
            return_value=SimpleNamespace(
                truncated=False, changed_paths=("index.html",) if overlap else ()
            )
        ),
        github_create_pull_request=AsyncMock(
            side_effect=AssertionError("No PR during preparation")
        ),
    )
    execution = TechnicalFixExecution(
        database=db, storage=storage, integrations=integrations, fetch=fetch
    )
    return SimpleNamespace(
        db=db,
        storage=storage,
        run=await db.get_run(original.id),
        execution=execution,
        fetch=fetch,
        integrations=integrations,
        preview=preview,
        activities=activities,
    )


@pytest.mark.parametrize(
    "html,source_html,overlap,reason",
    [
        (AFTER, BEFORE, False, "already_resolved"),
        (BEFORE, AFTER, False, "unsupported_source"),
        (BEFORE, BEFORE, True, "open_pr_overlap"),
    ],
)
async def test_preparation_no_change_is_durable_and_idempotent(
    publication_db, monkeypatch, html, source_html, overlap, reason
):
    f = await technical_fixture(
        publication_db, monkeypatch, html=html, source_html=source_html, overlap=overlap
    )
    assert await f.execution.prepare(f.run) is True
    assert await f.execution.prepare(f.run) is True
    saved = await prepared_result(f.db, f.run.id)
    assert saved["reason"] == reason
    run = await f.db.get_run(f.run.id)
    assert run.status.value == "succeeded"
    assert run.artifact_path == f"reports/technical-fix/{run.id}/RESULT.md"
    assert f.storage.repo.writes == 1
    assert f.preview.await_count == f.fetch.await_count == 1
    assert f.integrations.github_create_pull_request.await_count == 0
    if reason == "already_resolved":
        f.integrations.github_repository_bundle.assert_not_awaited()


async def test_description_preparation_and_delivery_recheck(publication_db, monkeypatch):
    from test_technical_metadata_repair import AFTER as DESCRIPTION_AFTER

    f = await technical_fixture(publication_db, monkeypatch)
    selection = f.preview.return_value
    selection["selection"]["finding"]["check_id"] = "metadata.description_missing"
    assert await f.execution.prepare(f.run, policy=technical_contract.POLICY) is False
    saved = await prepared_result(f.db, f.run.id)
    assert saved["policy"] == technical_contract.POLICY
    assert saved["verification_profile"]["kind"] == "static-html-v1"
    result = manifest()
    result["files"][0]["content"] = DESCRIPTION_AFTER
    await f.execution.validate_delivery(f.run, result, saved)
    f.fetch.return_value = {**f.fetch.return_value, "html": DESCRIPTION_AFTER}
    with pytest.raises(ValueError, match="website changed"):
        await f.execution.validate_delivery(f.run, result, saved)


async def test_technical_preparation_and_execution_read_the_same_repository_snapshot(
    publication_db, monkeypatch
):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *_: None)
    f = await technical_fixture(publication_db, monkeypatch)
    assert await f.execution.prepare(f.run) is False
    prepared_call = f.integrations.github_repository_bundle.call_args.kwargs
    f.activities._integrations = f.integrations
    await f.activities._github_procedure_bundle(
        project_id=f.run.project_id,
        run_id=f.run.id,
        sandbox_id="sandbox-test",
        expected_binding=prepared_call["expected_binding"],
    )
    execution_call = f.integrations.github_repository_bundle.call_args.kwargs
    assert execution_call == prepared_call


async def test_an_unreadable_repository_ends_the_run_failed_with_the_reason(
    publication_db, monkeypatch
):
    """Runs 43b99efd and 721f6a8d said "Tin couldn't read the repository" and succeeded."""
    f = await technical_fixture(publication_db, monkeypatch)
    bundle = f.integrations.github_repository_bundle.return_value
    bundle.complete = False
    bundle.missing = ({"path": "src/data/posts.json", "size": 2_400_000, "reason": "too_large"},)
    assert await f.execution.prepare(f.run, policy=technical_contract.POLICY) is True
    assert await f.execution.prepare(f.run, policy=technical_contract.POLICY) is True
    saved = await prepared_result(f.db, f.run.id)
    assert saved["reason"] == "repository_incomplete"
    assert saved["repository_missing"] == [dict(item) for item in bundle.missing]
    f.integrations.github_open_pull_requests.assert_not_awaited()
    run = await f.db.get_run(f.run.id)
    assert run.status.value == "failed"
    assert run.error_message == (
        "Tin couldn't read every file in the repository: src/data/posts.json (2.4 MB, over "
        "the 2 MB limit for files Tin reads). No change proposed."
    )
    assert run.artifact_path == f"reports/technical-fix/{run.id}/RESULT.md"
    assert f.storage.repo.writes == 1
    events = await f.db.pool.fetch(
        "SELECT event_type FROM activity_events WHERE run_id=$1 AND event_type LIKE 'technical%'",
        run.id,
    )
    assert [row["event_type"] for row in events] == ["technical_fix_failed"]


async def test_partial_coverage_is_explicit_and_only_matched_pages_rechecked(
    publication_db, monkeypatch
):
    from test_technical_metadata_repair import AFTER as DESCRIPTION_AFTER

    f = await technical_fixture(publication_db, monkeypatch)
    selection = f.preview.return_value["selection"]
    selection["finding"]["check_id"] = "metadata.description_missing"
    docs_url = "https://example.com/docs"
    selection["affected_urls"].append(docs_url)
    original_page = f.fetch.return_value

    async def fetch(url, **kwargs):
        html = (
            "<html><head></head><body>Generated API docs</body></html>"
            if url == docs_url
            else BEFORE
        )
        return {
            **original_page,
            "url": url,
            "html": html,
            "sha256": hashlib.sha256(html.encode()).hexdigest(),
        }

    f.fetch.side_effect = fetch
    assert await f.execution.prepare(f.run, policy=technical_contract.POLICY) is False
    saved = await prepared_result(f.db, f.run.id)
    assert saved["unsupported_pages"][0]["url"] == docs_url
    result = manifest()
    result["files"][0]["content"] = DESCRIPTION_AFTER
    await f.execution.validate_delivery(f.run, result, saved)
    assert f.fetch.await_count == 3  # Two initial observations, one matched-page delivery recheck.
    report = technical_contract.report(saved, reason="no_safe_patch").decode()
    assert "not a repair of the whole finding" in report and docs_url in report


@pytest.mark.parametrize("policy", [technical_contract.LEGACY_POLICY, technical_contract.POLICY])
async def test_parent_selection_uses_pinned_child_policy(monkeypatch, policy):
    facts = {"steps": [{"step": "audit", "status": "succeeded", "run_id": str(uuid4())}]}
    monkeypatch.setattr(
        "tin_lite.organic_system_activities.system_facts", AsyncMock(return_value=facts)
    )
    seen = []

    async def inspect(service, **kwargs):
        seen.append(service.supported_checks)
        return {"findings": []}

    monkeypatch.setattr("tin_lite.organic_system_activities.TechnicalFixSources.inspect", inspect)
    activities = OrganicSystemActivities(
        database=None, storage=None, settings=None, integrations=None
    )
    monkeypatch.setattr(
        activities,
        "saved",
        AsyncMock(
            return_value={
                "definitions": {"technical": {"procedure": {"output": {"repair_policy": policy}}}}
            }
        ),
    )
    run = SimpleNamespace(id=uuid4(), project_id=uuid4(), input={"technical_fix": True})
    assert await activities._child_inputs(run, "technical") == (None, "no_eligible_findings")
    assert seen == [technical_contract.supported_checks(policy)]


async def test_patch_preparation_pins_binding_and_allows_no_effect_yet(publication_db, monkeypatch):
    f = await technical_fixture(publication_db, monkeypatch)
    assert await f.execution.prepare(f.run) is False
    assert await f.execution.prepare(f.run) is False
    saved = await prepared_result(f.db, f.run.id)
    assert saved["originals"] == {"index.html": BEFORE}
    assert "html" not in saved["pages"][0]
    assert saved["repository_binding"]["head_sha"] == "b" * 40
    assert f.storage.repo.writes == 0
    assert f.integrations.github_repository_bundle.await_count == 1
    assert f.integrations.github_create_pull_request.await_count == 0


async def test_failed_fresh_request_is_not_success_or_free_pass(publication_db, monkeypatch):
    f = await technical_fixture(publication_db, monkeypatch)
    f.fetch.side_effect = ValueError("HTML unavailable")
    with pytest.raises(ValueError, match="unavailable"):
        await f.execution.prepare(f.run)
    assert (await f.db.get_run(f.run.id)).status.value == "running"
    assert f.storage.repo.writes == 0
    f.integrations.github_repository_bundle.assert_not_awaited()


async def test_technical_stop_fences_lease_and_refuses_inflight_commit(publication_db, monkeypatch):
    f = await technical_fixture(publication_db, monkeypatch)
    key = f"{f.run.id}:procedure_canonical_commit"
    async with f.db.effect_lock(key, "procedure_canonical_commit"):
        with pytest.raises(SideEffectConflictError, match="in progress"):
            await f.db.stop_technical_fix(
                run_id=f.run.id, project_id=f.run.project_id, actor="tester"
            )
    stopped = await f.db.stop_technical_fix(
        run_id=f.run.id, project_id=f.run.project_id, actor="tester"
    )
    assert stopped.status.value == "stopped" and not stopped.lease_active
    with pytest.raises(ValueError, match="no longer active"):
        await f.execution.prepare(f.run)
    f.fetch.assert_not_awaited()


async def parent_fixture(db, monkeypatch):
    _, storage, run, _ = await activity_fixture(db)
    definitions = {row.key: row.definition for row in BUILTIN_WORKFLOWS}
    inputs = normalize_workflow_inputs(
        schema=organic_system.INPUT_SCHEMA,
        project_id=run.project_id,
        inputs={
            "site_url": "https://example.com/",
            "market": "US",
            "buyer_context": "A useful product for founders planning their marketing.",
            "start_date": "2026-09-14",
        },
    )
    await db.pool.execute(
        "UPDATE workflows SET key=$2, executor=$2, definition=$3::jsonb WHERE id=$1",
        run.workflow_id,
        organic_system.KEY,
        json.dumps(definitions[organic_system.KEY]),
    )
    await db.pool.execute(
        "UPDATE workflow_runs SET executor=$2, input=$3::jsonb, lease_active=false, "
        "started_by_clerk_user_id='user_tester' WHERE id=$1",
        run.id,
        organic_system.KEY,
        json.dumps(inputs),
    )
    for workflow_key in organic_system.STEPS.values():
        definition = definitions[workflow_key]
        await db.pool.execute(
            "INSERT INTO workflows (id,key,title,executor,definition_repo_id,definition_path,"
            "current_commit_sha,version_label,definition) "
            "VALUES ($1,$2,$2,$3,'registry/workflows',$4,$5,'1',$6::jsonb)",
            uuid4(),
            workflow_key,
            definition["executor"],
            f"workflows/{workflow_key}.json",
            "e" * 40,
            json.dumps(definition),
        )
    monkeypatch.setattr(db, "has_project_access", AsyncMock(return_value=True))

    async def read(**kwargs):
        assert kwargs["commit_sha"] == run.definition_commit_sha
        return json.dumps(definitions[kwargs["path"][10:-5]]).encode()

    monkeypatch.setattr(storage, "read_canonical_artifact", read)
    settings = SimpleNamespace(
        dataforseo_login="fixture",
        dataforseo_password="fixture",  # noqa: S106 - synthetic local-test credential
        luna_api_key="fixture",
        keyword_plan_max_cost_usd=9,
        organic_audit_max_cost_usd=7,
        content_plan_max_cost_usd=1,
        temporal_task_queue="fixture",
    )
    activities = OrganicSystemActivities(
        database=db, storage=storage, settings=settings, integrations=SimpleNamespace()
    )
    return SimpleNamespace(
        db=db, storage=storage, run=await db.get_run(run.id), activities=activities
    )


async def test_parent_prepares_one_pinned_run_per_step_without_temporal_dispatch(
    publication_db, monkeypatch
):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    for step in ("audit", "keywords"):
        payload = {"run_id": str(f.run.id), "step": step}
        first = await f.activities.organic_system_step(payload)
        second = await f.activities.organic_system_step(payload)
        assert first == second
        child = await f.db.get_run(UUID(first["run_id"]))
        assert child.definition_commit_sha == f.run.definition_commit_sha  # not latest eeeee...
        assert child.project_id == f.run.project_id
        assert child.input["site_url"] == f.run.input["site_url"]
    technical = await f.activities.organic_system_step(
        {"run_id": str(f.run.id), "step": "technical"}
    )
    assert technical == {"status": "skipped", "reason": "not_requested"}
    content = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "content"})
    assert content == {"status": "blocked", "reason": "research_unavailable"}
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 3


@pytest.mark.parametrize("failed", [False, True])
async def test_summary_report_publishes_once_with_honest_status(
    publication_db, monkeypatch, failed
):
    f = await parent_fixture(publication_db, monkeypatch)
    for _ in range(2):
        await publish_run_report(
            database=f.db,
            storage=f.storage,
            run_id=f.run.id,
            workflow_key=organic_system.KEY,
            prefix="traffic",
            path=f"reports/organic-system/{f.run.id}/RESULT.md",
            content=b"# System result\n",
            summary="System result",
            failed=failed,
        )
    run = await f.db.get_run(f.run.id)
    assert run.status.value == ("failed" if failed else "succeeded")
    assert f.storage.repo.writes == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE dedupe_key LIKE 'traffic:%'"
        )
        == 1
    )


@pytest.mark.parametrize("child_status", ["failed", "succeeded"])
async def test_parent_retry_links_only_its_failed_keyword_child(
    publication_db, monkeypatch, child_status
):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    result = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "keywords"})
    old_child_id = UUID(result["run_id"])
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status=$2, lease_active=false, finished_at=now() WHERE id=$1",
        old_child_id,
        child_status,
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='failed', lease_active=false, finished_at=now() "
        "WHERE id=$1",
        f.run.id,
    )
    template = await f.db.get_workflow(f.run.workflow_id)
    retried, _ = await f.db.create_run(
        project_id=f.run.project_id,
        workflow_id=f.run.workflow_id,
        started_by_clerk_user_id=f.run.started_by_clerk_user_id,
        input_payload=f.run.input,
        pinned_definition=template.definition,
        definition_commit_sha=f.run.definition_commit_sha,
        retry_of_run_id=f.run.id,
    )
    await f.activities.organic_system_prepare(str(retried.id))
    result = await f.activities.organic_system_step({"run_id": str(retried.id), "step": "keywords"})
    new_child = await f.db.get_run(UUID(result["run_id"]))
    assert new_child.id != old_child_id
    assert new_child.retry_of_run_id == (old_child_id if child_status == "failed" else None)
    assert new_child.definition_commit_sha == f.run.definition_commit_sha


async def test_parent_stop_fences_future_child_creation(publication_db, monkeypatch):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    handle = SimpleNamespace(cancel=AsyncMock())
    runtime = SimpleNamespace(
        database=f.db, temporal=SimpleNamespace(get_workflow_handle=lambda _: handle)
    )
    result = await stop_system(runtime=runtime, run_id=f.run.id, actor="tester")
    assert result["status"] == "stopped"
    handle.cancel.assert_awaited_once()
    with pytest.raises(Exception, match="no longer active"):
        await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "audit"})
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1


async def test_content_child_uses_own_research_and_creates_one_manual_program(
    publication_db, monkeypatch
):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    sources = {}
    for step in ("audit", "keywords"):
        child = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": step})
        sources[step] = child["run_id"]
        # A succeeded research run always carries its publication revision; the content
        # child's declared prerequisites resolve exactly those runs.
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
            "finished_at=now() WHERE id=$1",
            UUID(child["run_id"]),
            "d" * 40,
        )
    first = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "content"})
    assert (
        await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "content"})
        == first
    )
    content = await f.db.get_run(UUID(first["run_id"]))
    assert content.input["audit_run_id"] == sources["audit"]
    assert content.input["keyword_run_id"] == sources["keywords"]
    assert content.definition_commit_sha == f.run.definition_commit_sha
    configured = await f.db.get_project_workflow(content.project_workflow_id)
    assert configured.schedule is None
    assert configured.definition_commit_sha == f.run.definition_commit_sha
    assert await f.db.pool.fetchval("SELECT count(*) FROM project_workflows") == 1


async def test_stopped_repair_cannot_reacquire_sandbox_lease(publication_db, monkeypatch):
    f = await technical_fixture(publication_db, monkeypatch)
    await f.db.stop_technical_fix(run_id=f.run.id, project_id=f.run.project_id, actor="user_tester")
    with pytest.raises(RuntimeError):
        await f.db.attach_sandbox(
            run_id=f.run.id,
            sandbox_id="new-sandbox",
            lease_owner=f.run.lease_owner,
            expected_head_sha="a" * 40,
            ephemeral_branch=f.run.ephemeral_branch,
            require_active=True,
        )
    assert not (await f.db.get_run(f.run.id)).lease_active


@pytest.mark.parametrize("no_change", [False, True])
@pytest.mark.parametrize("flag", ["no_title", "no_description"])
async def test_technical_commit_uses_immutable_verified_output_and_one_receipt(
    publication_db, monkeypatch, no_change, flag
):
    from test_technical_metadata_repair import AFTER as DESCRIPTION_AFTER

    source = content_source_fixture(checks={flag: True})
    selected = next(row for row in source.inventory["findings"] if row["category"] == "technical")
    source.selection["finding_id"] = selected["id"]
    real_preflight = source.service.preflight
    f = await technical_fixture(publication_db, monkeypatch, source=source)
    # Exercise the actual immutable audit reader and selection inside execution,
    # including unrelated content recommendations in the same inventory.
    f.preview.side_effect = real_preflight
    await f.execution.prepare(f.run, policy=technical_contract.POLICY)
    await f.execution.prepare(f.run, policy=technical_contract.POLICY)
    f.preview.assert_awaited_once()
    await f.db.pool.execute("UPDATE workflow_runs SET lease_active=true WHERE id=$1", f.run.id)
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        f"{f.run.id}:procedure_artifact_persist",
        json.dumps({"ephemeral_commit_sha": "e" * 40, "summary": "untrusted model summary"}),
    )
    contract = replace(spec(), receipt_path_template="reports/technical-fix/{run_id}/RESULT.md")
    monkeypatch.setattr(
        f.activities,
        "_pinned_codex_procedure",
        AsyncMock(
            return_value=(
                SimpleNamespace(key="organic.technical_fix", title="Technical fix"),
                contract,
            )
        ),
    )
    monkeypatch.setattr(
        f.storage,
        "read_ephemeral_artifact",
        AsyncMock(side_effect=AssertionError("mutable branch")),
    )
    proposed = manifest(no_change=no_change)
    after = AFTER if flag == "no_title" else DESCRIPTION_AFTER
    if not no_change:
        proposed["files"][0]["content"] = after
    immutable = AsyncMock(return_value=json.dumps(proposed).encode())
    monkeypatch.setattr(f.storage, "read_procedure_checkpoint", immutable)
    publisher = AsyncMock(return_value=("f" * 40, True))
    monkeypatch.setattr(f.storage, "publish_state_document", publisher)
    monkeypatch.setattr(
        "tin_lite.technical_fix_execution.TechnicalFixExecution", lambda **kwargs: f.execution
    )
    create_pr = AsyncMock(
        return_value=SimpleNamespace(
            url="https://github.com/owner/site/pull/1",
            number=1,
            repository="owner/site",
            branch="tin/fixture",
        )
    )
    f.integrations.github_create_pull_request = create_pr
    f.activities._integrations = f.integrations
    await f.activities.commit_codex_procedure_artifact(str(f.run.id))
    await f.activities.commit_codex_procedure_artifact(str(f.run.id))
    assert publisher.await_count == immutable.await_count == 1
    assert immutable.await_args.kwargs["revision"] == "e" * 40
    assert create_pr.await_count == (0 if no_change else 1)
    if not no_change:
        assert create_pr.await_args.kwargs["expected_binding"].head_sha == "b" * 40
        assert create_pr.await_args.kwargs["files"][0].content == after
    saved = await f.db.get_effect(f"{f.run.id}:procedure_canonical_commit")
    assert saved.status == "completed"
    assert saved.result["outcome"] == ("no_change" if no_change else "pull_request")
    assert saved.result["summary"] != "untrusted model summary"
    assert not (await f.db.get_run(f.run.id)).lease_active
