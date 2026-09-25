from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite.domain import RunStatus
from tin_lite.integrations import GitHubRepositoryBinding, IntegrationAuthorizationError
from tin_lite.organic_audit import (
    ARTIFACT_LIMITS,
    CHECKS,
    audit_paths,
    build_documents,
    canonical_json,
    digest,
    normalize_pages,
)
from tin_lite.technical_fix_sources import TechnicalFixError, TechnicalFixSources


def source_fixture(
    *, count=1, checks=None, crawl_status="completed", policy="organic-audit-v2", ai=None
):
    import json

    project = SimpleNamespace(id=uuid4(), state_repo_id="project/test")
    run = SimpleNamespace(
        id=uuid4(),
        project_id=project.id,
        executor="organic.audit",
        status=RunStatus.SUCCEEDED,
        canonical_commit_sha="a" * 40,
        definition_commit_sha="d" * 40,
        input={"site_url": "https://example.com/", "market": "US"},
    )
    pages = normalize_pages(
        [
            {
                "url": f"https://example.com/page-{index:03}",
                "resource_type": "html",
                "status_code": 200,
                "meta": {"title": ""},
                "checks": {"canonical": True, "no_title": True}
                if checks is None
                else checks[index]
                if isinstance(checks, list)
                else checks,
            }
            for index in range(count)
        ],
        "example.com",
        policy_version=policy,
    )
    docs = build_documents(
        run_id=str(run.id),
        project_id=str(project.id),
        definition_sha=run.definition_commit_sha,
        scope={
            "url": "https://example.com/",
            "host": "example.com",
            "market": "US",
            "language": "en",
            "started_at": "2026-09-09T00:00:00Z",
        },
        crawl={"status": crawl_status, "pages": pages},
        ai=ai if ai is not None else {"status": "partial", "summary": "Not measured."},
        spending={},
        policy_version=policy,
    )
    paths = audit_paths(str(run.id))
    evidence, inventory = (
        json.loads(docs[paths[name]]) for name in ("evidence.json", "findings.json")
    )
    receipt = SimpleNamespace(status="completed", result={})

    def seal(*, link_evidence=True):
        if link_evidence:
            inventory["evidence_sha256"] = digest(evidence)
        docs[paths["evidence.json"]] = canonical_json(evidence)
        docs[paths["findings.json"]] = canonical_json(inventory)
        receipt.result = {
            "canonical_commit_sha": run.canonical_commit_sha,
            "documents_sha256": digest({path: value.decode() for path, value in docs.items()}),
        }

    seal()
    database = SimpleNamespace(
        get_run=AsyncMock(return_value=run),
        get_project=AsyncMock(return_value=project),
        get_effect=AsyncMock(return_value=receipt),
        pool=SimpleNamespace(fetch=AsyncMock(return_value=[])),
    )
    storage = SimpleNamespace(
        read_canonical_artifact=AsyncMock(side_effect=lambda **kw: docs[kw["path"]])
    )
    binding = GitHubRepositoryBinding(uuid4(), 123, 456, "owner/site", "main", "b" * 40)
    integrations = SimpleNamespace(github_repository_binding=AsyncMock(return_value=binding))
    service = TechnicalFixSources(database=database, storage=storage, integrations=integrations)
    selection = {
        "project_id": project.id,
        "audit_run_id": run.id,
        "audit_revision": run.canonical_commit_sha,
        "finding_id": "oa_" + digest(["example.com", "metadata.title_missing"])[:20],
        "expected_repository": "owner/site",
        "repository_serves_site": True,
    }
    return SimpleNamespace(
        project=project,
        run=run,
        docs=docs,
        paths=paths,
        evidence=evidence,
        inventory=inventory,
        receipt=receipt,
        seal=seal,
        db=database,
        storage=storage,
        integrations=integrations,
        service=service,
        selection=selection,
    )


async def inspect(f):
    return await f.service.inspect(project_id=f.project.id, audit_run_id=f.run.id)


def content_source_fixture(*, checks=None, policy="organic-audit-v2"):
    from test_organic_audit_results import frozen_panel, scored

    from tin_lite.organic_audit_ai import summarize

    panel = frozen_panel()
    panel["questions"][-1]["job"] = "Compare support options"
    ai = summarize(
        panel,
        [scored(index) for index in range(len(panel["questions"]) * 2)],
        policy_version=policy,
    )
    return source_fixture(checks=checks if checks is not None else {}, ai=ai, policy=policy)


@pytest.mark.parametrize("policy", ["organic-audit-v1", "organic-audit-v2", "organic-audit-v8"])
async def test_content_findings_are_recognized_without_authorizing_technical_repair(policy):
    f = content_source_fixture(policy=policy)
    view = await inspect(f)
    assert view["findings"] == []
    assert view["repair_availability"] == {"available": False, "reason": "no_technical_findings"}
    assert len(view["excluded_findings"]) == 2
    assert {row["finding"]["id"] for row in view["excluded_findings"]} == {
        row["id"] for row in f.inventory["findings"]
    }
    for row in view["excluded_findings"]:
        assert row["finding"]["category"] == "content"
        assert row["source_eligible"] is False
        assert row["ineligible_reason"] == "content_finding"
        assert row["next_action"] == "content.plan"
        assert "does not establish a technical defect" in row["message"]
        f.selection["finding_id"] = row["finding"]["id"]
        with pytest.raises(TechnicalFixError) as error:
            await f.service.preflight(**f.selection)
        assert error.value.code == "content_finding"
        assert error.value.status_code == 409
        assert str(error.value) == row["message"]
    f.integrations.github_repository_binding.assert_not_awaited()


async def test_mixed_inventory_keeps_supported_technical_finding_selectable():
    f = content_source_fixture(checks={"no_title": True, "broken_links": True})
    view = await inspect(f)
    assert len(view["excluded_findings"]) == 2
    assert len(view["findings"]) == 2
    assert view["repair_availability"] == {"available": True, "reason": None}
    result = await f.service.preflight(**f.selection)
    assert result["selection"]["finding"]["check_id"] == "metadata.title_missing"
    f.integrations.github_repository_binding.assert_awaited_once()


@pytest.mark.parametrize("change", ["id", "duplicate", "technical_collision", "check"])
async def test_excluded_findings_require_valid_unambiguous_identifiers(change):
    f = content_source_fixture(checks={"no_title": True})
    content = next(row for row in f.inventory["findings"] if row["category"] == "content")
    if change == "id":
        content["id"] = None
    elif change == "duplicate":
        f.inventory["findings"].append(deepcopy(content))
    elif change == "technical_collision":
        content["id"] = f.selection["finding_id"]
    else:
        content["check_id"] = {"unexpected": "shape"}
    f.seal()
    with pytest.raises(TechnicalFixError, match="failed source verification"):
        await inspect(f)
    f.integrations.github_repository_binding.assert_not_awaited()


async def test_description_is_eligible_only_for_new_policy():
    from tin_lite import technical_fix

    f = source_fixture(checks={"no_description": True})
    view = await inspect(f)
    description = next(
        row
        for row in view["findings"]
        if row["finding"]["check_id"] == "metadata.description_missing"
    )
    assert description["source_eligible"] is True
    f.selection["finding_id"] = description["finding"]["id"]
    await f.service.preflight(**f.selection)
    f.service.supported_checks = technical_fix.supported_checks(technical_fix.LEGACY_POLICY)
    with pytest.raises(TechnicalFixError, match="does not support"):
        await f.service.preflight(**f.selection)


@pytest.mark.parametrize("policy", ["organic-audit-v1", "organic-audit-v2"])
async def test_source_verified_at_exact_revision_and_partial_ai_is_independent(policy):
    f = source_fixture(policy=policy)
    view = await inspect(f)
    assert view["findings"][0]["source_eligible"] is True
    assert view["execution_available"] is True
    assert view["source"]["audit_revision"] == f.run.canonical_commit_sha
    assert view["findings"][0]["affected_urls"] == ["https://example.com/page-000"]
    assert any(row["status"] == "unknown" for row in view["check_coverage"])
    assert len(f.storage.read_canonical_artifact.await_args_list) == 3
    for call in f.storage.read_canonical_artifact.await_args_list:
        assert call.kwargs["repo_id"] == f.project.state_repo_id
        assert call.kwargs["commit_sha"] == "a" * 40
        assert call.kwargs["path"] in f.paths.values()
    f.db.get_effect.assert_awaited_once_with(f"organic:{f.run.id}:publish")
    f.integrations.github_repository_binding.assert_not_awaited()
    assert "ai_visibility" not in view  # Raw model answers are not a technical-fix prompt.


@pytest.mark.parametrize(
    "change", ["cross_project", "missing", "executor", "status", "sha", "definition"]
)
async def test_invalid_run_rejected_before_storage_or_github(change):
    f = source_fixture()
    if change == "cross_project":
        f.run.project_id = uuid4()
    elif change == "missing":
        f.db.get_run.return_value = None
    elif change == "executor":
        f.run.executor = "content.plan"
    elif change == "status":
        f.run.status = RunStatus.FAILED
    elif change == "sha":
        f.run.canonical_commit_sha = None
    else:
        f.run.definition_commit_sha = "invalid"
    with pytest.raises(TechnicalFixError):
        await inspect(f)
    f.storage.read_canonical_artifact.assert_not_awaited()
    f.integrations.github_repository_binding.assert_not_awaited()


@pytest.mark.parametrize("change", ["missing", "pending", "revision", "digest", "text"])
async def test_receipt_and_all_document_bytes_must_match(change):
    f = source_fixture()
    if change == "missing":
        f.db.get_effect.return_value = None
    elif change == "pending":
        f.receipt.status = "started"
    elif change == "revision":
        f.receipt.result["canonical_commit_sha"] = "b" * 40
    elif change == "digest":
        f.receipt.result["documents_sha256"] = "b" * 64
    else:
        f.docs[f.paths["AUDIT.md"]] += b"\nchanged"
    with pytest.raises(TechnicalFixError, match="failed source verification"):
        await inspect(f)


@pytest.mark.parametrize("name", ARTIFACT_LIMITS)
@pytest.mark.parametrize("shape", ["empty", "oversized", "binary"])
async def test_each_artifact_has_a_bounded_utf8_contract(name, shape):
    f = source_fixture()
    f.docs[f.paths[name]] = {
        "empty": b"",
        "oversized": b"x" * (ARTIFACT_LIMITS[name] + 1),
        "binary": b"\xff",
    }[shape]
    with pytest.raises(TechnicalFixError):
        await inspect(f)


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "boolean_version",
        "owner",
        "run",
        "definition",
        "target",
        "input_target",
        "policy",
        "evidence_digest",
        "finding_id",
        "finding_flag",
        "finding_version",
        "finding_count",
        "finding_url",
        "duplicate_finding",
        "missing_finding",
        "coverage",
        "page_flag",
        "page_host",
        "duplicate_page",
        "page_order",
        "too_many_pages",
        "bad_page_status",
        "authority",
        "crawl_status",
    ],
)
async def test_valid_receipt_does_not_replace_evidence_and_inventory_validation(change):
    f = source_fixture(count=2)
    e, i = f.evidence, f.inventory
    row = i["findings"][0]
    if change == "version":
        e["schema_version"] = 2
    elif change == "boolean_version":
        i["schema_version"] = True
    elif change == "owner":
        e["project_id"] = str(uuid4())
    elif change == "run":
        i["run_id"] = str(uuid4())
    elif change == "definition":
        e["definition_commit_sha"] = "e" * 40
    elif change == "target":
        e["scope"]["host"] = "other.com"
    elif change == "input_target":
        f.run.input["site_url"] = "https://other.com/"
    elif change == "policy":
        e["policy"]["version"] = "future-policy"
    elif change == "evidence_digest":
        i["evidence_sha256"] = "0" * 64
    elif change == "finding_id":
        row["id"] = "oa_" + "0" * 20
    elif change == "finding_flag":
        row["affected_url_evidence"]["flag"] = "no_description"
    elif change == "finding_version":
        row["check_version"] = True
    elif change == "finding_count":
        row["affected_count"] = 1
    elif change == "finding_url":
        row["urls"] = ["https://other.com/"]
    elif change == "duplicate_finding":
        i["findings"].append(deepcopy(row))
    elif change == "missing_finding":
        i["findings"] = []
    elif change == "coverage":
        i["check_coverage"][0]["status"] = "observed"
    elif change == "page_flag":
        e["crawl"]["pages"][0]["checks"]["no_title"] = 1
    elif change == "page_host":
        e["crawl"]["pages"][0]["url"] = "https://other.com/"
    elif change == "duplicate_page":
        e["crawl"]["pages"].append(e["crawl"]["pages"][0])
    elif change == "page_order":
        e["crawl"]["pages"].reverse()
    elif change == "too_many_pages":
        e["crawl"]["pages"] *= 51
    elif change == "bad_page_status":
        e["crawl"]["pages"][0]["status_code"] = True
    elif change == "authority":
        i["downstream_authority"] = "may_edit"
    elif change == "crawl_status":
        e["crawl"]["status"] = "invented"
    f.seal(link_evidence=change != "evidence_digest")
    with pytest.raises(TechnicalFixError, match="failed source verification"):
        await inspect(f)


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b'{"schema_version":1,"schema_version":2}',
        b'{"x":NaN}',
        b'"text"',
        b"invalid",
    ],
)
async def test_malformed_json_is_a_safe_source_error(raw):
    f = source_fixture()
    f.docs[f.paths["evidence.json"]] = raw
    f.receipt.result["documents_sha256"] = digest({p: b.decode() for p, b in f.docs.items()})
    with pytest.raises(TechnicalFixError, match="failed source verification"):
        await inspect(f)


@pytest.mark.parametrize(
    "count,reason", [(5, None), (6, "affected_page_limit"), (12, "affected_page_limit")]
)
async def test_full_affected_list_is_recovered_not_silently_capped(count, reason):
    f = source_fixture(count=count)
    row = (await inspect(f))["findings"][0]
    assert len(row["affected_urls"]) == count
    assert len(row["finding"]["urls"]) == min(10, count)
    assert row["ineligible_reason"] == reason
    if reason:
        with pytest.raises(TechnicalFixError) as error:
            await f.service.preflight(**f.selection)
        assert error.value.code == reason
        f.integrations.github_repository_binding.assert_not_awaited()


async def test_affected_list_uses_the_pinned_audit_applicability():
    checks = [{"canonical": True, "no_title": True}]
    checks += [{"canonical": False, "no_title": True}] * 5 + [{"no_title": True}]
    f = source_fixture(count=len(checks), checks=checks, policy="organic-audit-v9")
    row = (await inspect(f))["findings"][0]
    assert row["finding"]["affected_count"] == 1
    assert row["affected_urls"] == ["https://example.com/page-000"]
    assert row["source_eligible"] is True


async def test_unknown_checks_and_empty_findings_do_not_invent_a_repair():
    f = source_fixture(checks={})
    view = await inspect(f)
    assert view["findings"] == []
    assert view["excluded_findings"] == []
    assert view["repair_availability"]["reason"] == "no_technical_findings"
    assert all(row["status"] == "unknown" for row in view["check_coverage"])
    with pytest.raises(TechnicalFixError) as error:
        await f.service.preflight(**f.selection)
    assert error.value.code == "finding_not_found"
    f.integrations.github_repository_binding.assert_not_awaited()


@pytest.mark.parametrize("crawl_status", ["partial", "unavailable"])
async def test_incomplete_crawl_keeps_evidence_but_does_not_enable_initial_repair(crawl_status):
    f = source_fixture(crawl_status=crawl_status)
    view = await inspect(f)
    assert view["findings"][0]["ineligible_reason"] == "crawl_incomplete"
    assert view["repair_availability"] == {"available": False, "reason": "no_eligible_findings"}


async def test_other_technical_findings_are_visible_but_not_supported():
    f = source_fixture(checks={"no_title": True, "broken_links": True})
    rows = (await inspect(f))["findings"]
    row = next(row for row in rows if row["finding"]["check_id"] == "links.broken")
    assert row["ineligible_reason"] == "check_not_supported"
    f.selection["finding_id"] = row["finding"]["id"]
    with pytest.raises(TechnicalFixError) as error:
        await f.service.preflight(**f.selection)
    assert error.value.code == "check_not_supported"
    assert "does not support" in str(error.value)
    assert "links.broken" in str(error.value)
    f.integrations.github_repository_binding.assert_not_awaited()


@pytest.mark.parametrize("flag,check", [(row[0], row[1]) for row in CHECKS])
async def test_every_audited_technical_check_is_found_and_classified(flag, check):
    from tin_lite.technical_metadata_rules import SUPPORTED_CHECKS

    f = source_fixture(checks={flag: True})
    view = await inspect(f)
    (row,) = view["findings"]
    assert row["finding"]["check_id"] == check
    f.selection["finding_id"] = row["finding"]["id"]
    if check in SUPPORTED_CHECKS:
        assert view["repair_availability"]["available"] is True
        assert (await f.service.preflight(**f.selection))["selection"] == row
    else:
        assert view["repair_availability"]["reason"] == "no_eligible_findings"
        with pytest.raises(TechnicalFixError) as error:
            await f.service.preflight(**f.selection)
        assert error.value.code == "check_not_supported"
        f.integrations.github_repository_binding.assert_not_awaited()


@pytest.mark.parametrize(
    "change,code",
    [
        ("revision", "source_changed"),
        ("finding", "finding_not_found"),
        ("format", "invalid_selection"),
        ("confirmation", "repository_confirmation_required"),
    ],
)
async def test_selection_rejected_before_github(change, code):
    f = source_fixture()
    if change == "revision":
        f.selection["audit_revision"] = "b" * 40
    elif change == "finding":
        f.selection["finding_id"] = "oa_" + "0" * 20
    elif change == "format":
        f.selection["finding_id"] = "whatever"
    else:
        f.selection["repository_serves_site"] = False
    with pytest.raises(TechnicalFixError) as error:
        await f.service.preflight(**f.selection)
    assert error.value.code == code
    f.integrations.github_repository_binding.assert_not_awaited()


async def test_preflight_is_an_exact_read_only_preview_not_execution_approval():
    f = source_fixture()
    result = await f.service.preflight(**f.selection)
    assert result["source"]["audit_revision"] == "a" * 40
    assert result["repository_binding"]["repository_id"] == 456
    assert result["repository_mapping"] == "member_asserted_not_verified"
    assert result["live_verification"] == "not_performed"
    assert result["execution_available"] is True
    f.integrations.github_repository_binding.assert_awaited_once_with(
        project_id=f.project.id,
        expected_repository="owner/site",
    )
    # The fixtures deliberately offer no run dispatch, paid provider, storage-write or PR method.


async def test_storage_errors_and_integration_errors_are_safe():
    f = source_fixture()
    f.storage.read_canonical_artifact.side_effect = RuntimeError("https://secret@storage.invalid")
    with pytest.raises(TechnicalFixError) as error:
        await inspect(f)
    assert error.value.status_code == 503
    assert "secret" not in str(error.value)
    f = source_fixture()
    f.integrations.github_repository_binding.side_effect = IntegrationAuthorizationError(
        "Choose GitHub"
    )
    with pytest.raises(TechnicalFixError) as error:
        await f.service.preflight(**f.selection)
    assert error.value.code == "github_binding_failed"


async def test_source_listing_is_bounded_project_scoped_postgres_only():
    f = source_fixture()
    f.db.pool.fetch.return_value = [{"id": uuid4()} for _ in range(51)]
    result = await f.service.list_sources(project_id=f.project.id, offset=50)
    assert len(result["sources"]) == 50 and result["next_offset"] == 100
    query, project, offset = f.db.pool.fetch.await_args.args
    assert "project_id = $1" in query and "executor = 'organic.audit'" in query
    assert "status = 'succeeded'" in query and "LIMIT 51 OFFSET $2" in query
    assert (project, offset) == (f.project.id, 50)
    f.storage.read_canonical_artifact.assert_not_awaited()
    f.integrations.github_repository_binding.assert_not_awaited()
    for offset in (-1, 10_001, True):
        with pytest.raises(TechnicalFixError) as error:
            await f.service.list_sources(project_id=f.project.id, offset=offset)
        assert error.value.status_code == 422
