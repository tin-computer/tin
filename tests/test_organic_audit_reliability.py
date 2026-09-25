"""Real-shaped www crawl and long-answer regressions, without paid resampling."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_organic_audit import activities_fixture, page_fixture, panel_fixture, response
from test_procedure_publication import HistoryStorage
from test_technical_fix_sources import source_fixture

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V4_AUDIT_POLICY,
    V7_AUDIT_POLICY,
    audit_paths,
    build_documents,
    canonical_json,
    normalize_pages,
    technical_findings,
)
from tin_lite.organic_audit_ai import AuditValidationError, classify_absent_target, read_response
from tin_lite.organic_audit_panel import grounded_panel
from tin_lite.organic_audit_publication import publish_audit
from tin_lite.organic_audit_scope import audit_hosts, resolve_site_identity
from tin_lite.publication import PublicationPendingError
from tin_lite.technical_fix import verified_page_host
from tin_lite.technical_fix_sources import TechnicalFixError


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [V7_AUDIT_POLICY, AUDIT_POLICY])
async def test_only_new_buyer_answers_get_longer_transport_timeout(policy):
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    db.effects[activities.key(run_id, "scope")].result["policy_version"] = policy["version"]
    activities.responses = type("Responses", (), {"create": AsyncMock(return_value=response())})()
    request = {"model": policy["model"], "input": "A buyer question"}
    for stage in ("answer:0", "judge:0", "brand:0", "panel_research"):
        assert (await activities._model(run_id, stage, request, search=True))[
            "status"
        ] == "completed"
        sent = activities.responses.create.await_args.args[0]
        assert sent.get("timeout") == (
            180 if stage == "answer:0" and policy == AUDIT_POLICY else None
        )
    assert "timeout" not in request


def scope(host="example.com", peer="www.example.com"):
    return {
        "host": host,
        "url": f"https://{host}/",
        "market": "US",
        "language": "en",
        "started_at": "2026-09-15",
        "policy_version": AUDIT_POLICY["version"],
        "site_identity": {
            "redirects": [
                {"from": f"https://{host}/", "to": f"https://{peer}/", "status_code": 308}
            ]
        },
    }


@pytest.mark.parametrize(
    "host,peer", [("example.com", "www.example.com"), ("www.example.com", "example.com")]
)
@pytest.mark.asyncio
async def test_redirect_is_observed_over_dns_pinned_https_and_no_body_read(host, peer):
    calls = []

    def handle(request):
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == request.headers["Host"]
        calls.append(request.headers["Host"])
        return (
            httpx.Response(308, headers={"location": f"https://{peer}/"})
            if len(calls) == 1
            else httpx.Response(200)
        )

    resolver = AsyncMock(return_value=[(None, None, None, None, ("93.184.216.34", 443))])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        identity = await resolve_site_identity(f"https://{host}/", client=client, resolver=resolver)
    saved = {**scope(host, peer), "site_identity": identity}
    assert calls == [host, peer] and audit_hosts(saved) == (host, peer)
    assert identity["status"] == "observed"
    assert audit_hosts({**saved, "policy_version": V4_AUDIT_POLICY["version"]}) == (host,)


@pytest.mark.parametrize(
    "destination",
    [
        "https://other.com/",
        "https://blog.example.com/",
        "https://example.com.attacker.com/",
        "http://www.example.com/",
        "https://user:secret@www.example.com/",
        "https://www.example.com:8443/",
        "https://127.0.0.1/",
    ],
)
@pytest.mark.asyncio
async def test_unrelated_or_unsafe_redirect_never_broadens_scope(destination):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(301, headers={"location": destination})

    resolver = AsyncMock(return_value=[(None, None, None, None, ("93.184.216.34", 443))])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        identity = await resolve_site_identity(
            "https://example.com/", client=client, resolver=resolver
        )
    assert len(calls) == 1 and identity["status"] == "outside_site_redirect"
    assert audit_hosts({**scope(), "site_identity": identity}) == ("example.com",)


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.216.34", "2606:4700:4700::1111"],
        ["2606:4700:4700::1111", "93.184.216.34"],
    ],
)
@pytest.mark.asyncio
async def test_site_identity_keeps_resolver_preference_and_checks_every_address(addresses):
    calls = []

    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200)

    rows = [(None, None, None, None, (ip, 443)) for ip in addresses]
    resolver = AsyncMock(return_value=rows)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        identity = await resolve_site_identity(
            "https://example.com/", client=client, resolver=resolver
        )
        assert identity["status"] == "observed" and calls == [addresses[0]]

        rows.append((None, None, None, None, ("127.0.0.1", 443)))
        identity = await resolve_site_identity(
            "https://example.com/", client=client, resolver=resolver
        )
    assert identity["status"] == "non_public_address" and len(calls) == 1


@pytest.mark.asyncio
async def test_private_redirect_destination_is_not_requested_or_recorded():
    resolver = AsyncMock(
        side_effect=[
            [(None, None, None, None, ("93.184.216.34", 443))],
            [(None, None, None, None, ("127.0.0.1", 443))],
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(308, headers={"location": "https://www.example.com/"})
        )
    ) as client:
        identity = await resolve_site_identity(
            "https://example.com/", client=client, resolver=resolver
        )
    assert identity["status"] == "non_public_address" and not identity["redirects"]


@pytest.mark.asyncio
async def test_all_55_www_html_pages_survive_and_duplicate_poll_does_not_recollect():
    activities, db, _, provider = await activities_fixture()
    run_id = str(db.run.id)
    await activities._save(
        run_id, "crawl_submit", {"status": "completed", "value": {"task_id": str(uuid4())}}
    )
    original = await activities._result(run_id, "scope")
    # The fixture has already prepared; model the persisted, observed redirect.
    original.update(site_identity=scope()["site_identity"])
    provider.pages.return_value = [
        page_fixture(url=f"https://www.example.com/page-{index:02}") for index in range(55)
    ]
    for _ in range(2):
        assert await activities.organic_poll_crawl(run_id)
    crawl = await activities._result(run_id, "crawl")
    assert crawl["status"] == "completed" and len(crawl["pages"]) == 55
    assert crawl["collection"] == {
        "provider_html_pages": 55,
        "retained_html_pages": 55,
        "excluded_html_pages": 0,
        "provider_resources": 55,
        "retained_resources": 55,
    }
    assert provider.pages.await_count == 1
    assert not normalize_pages(
        provider.pages.return_value, "example.com"
    )  # Legacy exact-host contract.


def test_verified_www_research_and_citations_flow_into_the_frozen_panel():
    proposed = panel_fixture()
    proposed["host"] = "www.example.com"
    for question in proposed["questions"]:
        question["source_url"] = "https://www.example.com/"
    research = {
        "text": "Acme supports team planning.",
        "sources": ["https://www.example.com/"],
        "citations": [],
    }
    panel = grounded_panel(
        {"text": json.dumps(proposed)}, research, "example.com", aliases=audit_hosts(scope())[1:]
    )
    assert panel["host"] == "example.com"
    assert classify_absent_target(
        {"text": "Consider Rival Brand.", "citations": ["https://www.example.com/"]}, panel
    )["owned_domain_cited"]
    assert not classify_absent_target(
        {"text": "Consider Rival Brand.", "citations": ["https://blog.example.com/"]}, panel
    )["owned_domain_cited"]
    with pytest.raises(AuditValidationError):
        grounded_panel({"text": json.dumps(proposed)}, research, "example.com")


@pytest.mark.asyncio
async def test_long_completed_answer_is_saved_and_scored_losslessly_without_resampling():
    text = "A useful answer about the supported buying problem. " * 500
    raw = response(text)
    with pytest.raises(AuditValidationError, match="sources exceeded"):
        read_response(raw, search=True, policy_version=V4_AUDIT_POLICY["version"])
    parsed = read_response(raw, search=True, policy_version=AUDIT_POLICY["version"])
    assert parsed["text"] == text
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    panel = {**panel_fixture(), "status": "completed", "planned_observations": 8}
    await activities._save(run_id, "panel", panel)
    activities.responses = type("Responses", (), {"create": AsyncMock(return_value=raw)})()
    for _ in range(2):
        await activities.organic_observe({"run_id": run_id, "index": 0})
    observed = await activities._result(run_id, "observation:0")
    assert observed["status"] == "completed" and observed["answer"]["value"]["text"] == text
    assert activities.responses.create.await_count == 1


def large_documents(run_id):
    # Exercise all 24 observations near the per-observation budget, plus the
    # six preparation records, two probes and full crawl budget. No shrunken panel.
    answer = {
        "text": "x" * 32_000,
        "sources": ["https://example.com/" + "a" * 750 + str(i) for i in range(40)],
        "citations": [],
    }
    assert len(canonical_json(answer)) < AUDIT_POLICY["max_response_bytes"]
    ai = {
        "status": "completed",
        "summary": "24/24 scored.",
        "observations": [
            {"answer": deepcopy(answer), "classification": {"quote": "q" * 6000}} for _ in range(24)
        ],
        "preparation_receipts": {str(i): answer for i in range(6)},
        "brand_checks": {"observations": [answer, answer]},
    }
    return build_documents(
        run_id=run_id,
        project_id=str(uuid4()),
        definition_sha="d" * 40,
        scope=scope(),
        crawl={"status": "completed", "pages": [], "padding": "c" * 240_000},
        ai=ai,
        spending={},
    )


@pytest.mark.asyncio
async def test_full_panel_evidence_fits_publication_and_lost_response_recovery():
    run_id = str(uuid4())
    documents = large_documents(run_id)
    evidence = documents[audit_paths(run_id)["evidence.json"]]
    assert 1_000_000 < len(evidence) < AUDIT_POLICY["max_evidence_bytes"]
    storage = HistoryStorage()
    storage.repo.lose_response = True
    saved = {}

    async def save(intent):
        saved.update(intent)

    args = dict(
        storage=storage,
        repo_id=storage.repo.id,
        branch="main",
        run_id=run_id,
        documents=documents,
        save_intent=save,
        validate_active=AsyncMock(),
    )
    with pytest.raises(PublicationPendingError):
        await publish_audit(**args, intent=None)
    revision = await publish_audit(**args, intent=saved)
    assert storage.repo.writes == 1
    assert storage.repo.trees[revision][audit_paths(run_id)["evidence.json"]][1] == evidence


@pytest.mark.asyncio
async def test_www_findings_remain_usable_by_technical_fix_without_cross_site_authority():
    fixture = source_fixture(policy=AUDIT_POLICY["version"])
    fixture.evidence["scope"].update(scope())
    fixture.evidence["crawl"]["pages"][0]["url"] = "https://www.example.com/"
    findings, coverage = technical_findings(
        fixture.evidence["crawl"]["pages"], "example.com", policy_version=AUDIT_POLICY["version"]
    )
    fixture.inventory.update(findings=findings, check_coverage=coverage)
    fixture.seal()
    inspected = await fixture.service.inspect(
        project_id=fixture.project.id, audit_run_id=fixture.run.id
    )
    assert inspected["findings"][0]["source_eligible"]
    assert verified_page_host("https://www.example.com/", inspected["target"]) == "www.example.com"
    with pytest.raises(ValueError):
        verified_page_host("https://other.com/", inspected["target"])
    fixture.evidence["scope"]["site_identity"]["redirects"][0]["to"] = "https://other.com/"
    fixture.seal()
    with pytest.raises(TechnicalFixError):
        await fixture.service.inspect(project_id=fixture.project.id, audit_run_id=fixture.run.id)
