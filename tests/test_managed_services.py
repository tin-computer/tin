"""Services Tin holds the key for: PageSpeed Insights, CrUX and DataForSEO live reads.

Provider answers are fixtures shaped like the providers' own responses (Google PSI v5, CrUX
queryRecord, DataForSEO v3 live tasks), served through an httpx MockTransport. No test calls a
live API. Gateway tests use a real disposable Postgres schema and synthetic compute.
"""

import asyncio
import base64
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_billing import fund
from test_private_workflows import ACTOR, structured
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import setup, start

from tin_lite import managed_services
from tin_lite.code_activities import CodeActivities
from tin_lite.code_models import model_terms
from tin_lite.code_services import OPERATION, SPENDING_STOPPED, CodeServiceError
from tin_lite.connection_records import ServiceArgumentError
from tin_lite.domain import RunStatus
from tin_lite.integrations import (
    IntegrationRateLimitedError,
    IntegrationService,
    IntegrationUpstreamError,
    ServiceCallRefused,
    parse_integration_requirements,
)
from tin_lite.managed_services import (
    DATAFORSEO_PROVIDER,
    PAGESPEED_PROVIDER,
    ManagedServices,
    request_for,
)
from tin_lite.run_usage import read_run_usage
from tin_lite.workflow_code import example_files, validate_code_definition

FIXTURES = Path(__file__).parent / "fixtures" / "managed_services"
KEY = "custom.order_report"
PATH = f"workflow_packages/{KEY}/workflow.json"
OUTPUT = "reports/custom/ORDER_REPORT.md"
GOOGLE_KEY = "fixture-google-api-key"
SETTINGS = SimpleNamespace(
    pagespeed_api_key=SecretStr(GOOGLE_KEY),
    dataforseo_login=SecretStr("fixture-login"),
    dataforseo_password=SecretStr("fixture-password"),
)


def load(name):
    return json.loads((FIXTURES / name).read_text())


def dataforseo(name, *, status=20000, task_status=20000, cost=None, http_status=200):
    """A DataForSEO live answer that echoes the request, as the real envelope does."""
    fixture = load(name)

    def answer(request):
        if http_status != 200:
            return httpx.Response(http_status, json={"status_code": 40100})
        body = json.loads(request.content)
        assert isinstance(body, list) and len(body) == 1
        path = request.url.path.removeprefix("/v3/")
        charged = fixture["cost"] if cost is None else cost
        task = {
            "id": "09301810-1535-0139-0000-9f1c0bd2f1f1",
            "status_code": task_status,
            "status_message": "Ok." if task_status == 20000 else "Access denied.",
            "time": "0.9124 sec.",
            "cost": charged if task_status == 20000 else 0,
            "result_count": 1,
            "path": path.split("/"),
            "data": {"api": path.split("/")[0], "function": "live", **body[0]},
            "result": fixture["result"] if task_status == 20000 else None,
        }
        return httpx.Response(
            200,
            json={
                "version": "0.1.20260915",
                "status_code": status,
                "status_message": "Ok.",
                "time": "1.0112 sec.",
                "cost": task["cost"] if status == 20000 else 0,
                "tasks_count": 1,
                "tasks_error": 0 if task_status == 20000 else 1,
                "tasks": [task] if status == 20000 else None,
            },
        )

    return answer


class Provider:
    """Answers by host and path; records every request it saw."""

    def __init__(self, **routes):
        self.routes, self.requests = routes, []

    async def __call__(self, request):
        self.requests.append(request)
        answer = self.routes[request.url.path.rsplit("/", 1)[-1]]
        result = answer(request)
        return await result if asyncio.iscoroutine(result) else result


def managed(provider, settings=SETTINGS):
    return ManagedServices(settings, transport=httpx.MockTransport(provider))


async def call(provider, operation, arguments, *, maximum=64_000, key="run:code-service:x"):
    service = managed(provider)
    owner = PAGESPEED_PROVIDER if operation.split(".")[0] in {"pagespeed", "crux"} else None
    return await service.call(
        owner or DATAFORSEO_PROVIDER,
        operation,
        arguments,
        execution_key=key,
        max_response_bytes=maximum,
    )


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


# ---------------------------------------------------------------- PageSpeed Insights and CrUX


async def test_pagespeed_returns_scores_and_key_metrics_not_the_lighthouse_report():
    provider = Provider(
        runPagespeed=lambda r: httpx.Response(200, json=load("pagespeed_mobile.json"))
    )
    result = await call(
        provider,
        "pagespeed.run",
        {"url": "https://example.com/", "categories": ["seo", "performance"]},
    )
    assert result == {
        "url": "https://example.com/",
        "strategy": "mobile",
        "status": "observed",
        "final_url": "https://example.com/",
        "fetch_time": "2026-09-30T18:02:11.412Z",
        "lighthouse_version": "12.8.2",
        "scores": {"performance": 68, "seo": 92},
        "lab": {
            "lcp_ms": 3912.4,
            "cls": 0.0312,
            "tbt_ms": 318.5,
            "fcp_ms": 1811.2,
            "speed_index_ms": 4120.9,
        },
        "field_status": "observed",
        "field": {
            "scope": "url",
            "overall": "AVERAGE",
            "lcp_ms": 2710.0,
            "inp_ms": 180.0,
            "cls": 0.07,
            "fcp_ms": 1750.0,
            "ttfb_ms": 910.0,
        },
    }
    assert (FIXTURES / "pagespeed_mobile.json").stat().st_size > 60_000 > 50 * size(result)
    (request,) = provider.requests
    # The key travels in a header, never the URL that logs and receipts might keep.
    assert request.headers["x-goog-api-key"] == GOOGLE_KEY
    assert GOOGLE_KEY not in str(request.url)
    assert request.url.params.get_list("category") == ["performance", "seo"]
    assert request.url.params["strategy"] == "mobile"


async def test_pagespeed_desktop_on_a_small_site_says_there_is_no_field_data():
    provider = Provider(
        runPagespeed=lambda r: httpx.Response(200, json=load("pagespeed_no_field_data.json"))
    )
    result = await call(
        provider, "pagespeed.run", {"url": "https://tiny-startup.example/", "strategy": "desktop"}
    )
    assert result["field_status"] == "no_field_data" and result["field"] is None
    assert result["scores"] == {"performance": 97}
    # A lab CLS of 0 is a real measurement; only missing field data is withheld.
    assert result["lab"]["cls"] == 0.0 and result["strategy"] == "desktop"
    assert provider.requests[0].url.params["strategy"] == "desktop"


async def test_a_slow_lighthouse_run_comes_back_as_timed_out(monkeypatch):
    monkeypatch.setattr(managed_services, "GOOGLE_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=load("pagespeed_mobile.json"))

    result = await call(
        Provider(runPagespeed=slow), "pagespeed.run", {"url": "https://example.com/"}
    )
    assert result == {
        "url": "https://example.com/",
        "strategy": "mobile",
        "status": "timed_out",
        "seconds": 0.05,
    }


async def test_lighthouse_errors_and_google_refusals_are_named():
    error = Provider(
        runPagespeed=lambda r: httpx.Response(500, json=load("pagespeed_lighthouse_error.json"))
    )
    result = await call(error, "pagespeed.run", {"url": "https://example.com/"})
    assert result["status"] == "lighthouse_error" and result["code"] == "NO_FCP"
    limited = Provider(runPagespeed=lambda r: httpx.Response(429, json={}))
    with pytest.raises(IntegrationRateLimitedError, match="new step"):
        await call(limited, "pagespeed.run", {"url": "https://example.com/"})
    refused = Provider(**{"records:queryRecord": lambda r: httpx.Response(403, json={})})
    with pytest.raises(ServiceCallRefused, match="TIN_LITE_PAGESPEED_API_KEY") as caught:
        await call(refused, "crux.query", {"origin": "https://example.com"})
    assert caught.value.code == "provider_refused"


async def test_crux_origin_field_data_keeps_p75_and_the_good_share():
    provider = Provider(
        **{"records:queryRecord": lambda r: httpx.Response(200, json=load("crux_origin.json"))}
    )
    result = await call(provider, "crux.query", {"origin": "https://example.com/"})
    assert result["status"] == "observed" and result["origin"] == "https://example.com"
    assert result["form_factor"] == "all"
    assert result["collection_period"] == {"first_date": "2026-09-01", "last_date": "2026-09-28"}
    assert result["metrics"]["lcp_ms"] == {
        "p75": 2807.0,
        "good": 0.69,
        "needs_improvement": 0.2,
        "poor": 0.11,
    }
    assert result["metrics"]["cls"]["p75"] == 0.05 and result["metrics"]["inp_ms"]["p75"] == 176.0
    body = json.loads(provider.requests[0].content)
    assert body == {"origin": "https://example.com", "metrics": list(managed_services.CRUX_METRICS)}
    assert provider.requests[0].headers["x-goog-api-key"] == GOOGLE_KEY


async def test_crux_missing_metrics_stay_null_and_unknown_sites_have_no_field_data():
    partial = Provider(
        **{"records:queryRecord": lambda r: httpx.Response(200, json=load("crux_partial.json"))}
    )
    result = await call(
        partial,
        "crux.query",
        {"url": "https://example.com/pricing?ref=x", "form_factor": "phone"},
    )
    assert result["normalized_url"] == "https://example.com/pricing"
    assert result["metrics"]["inp_ms"] is None and result["metrics"]["fcp_ms"] is None
    assert result["metrics"]["lcp_ms"]["p75"] == 3620.0 and result["form_factor"] == "phone"
    assert json.loads(partial.requests[0].content)["formFactor"] == "PHONE"
    unknown = Provider(
        **{"records:queryRecord": lambda r: httpx.Response(404, json=load("crux_not_found.json"))}
    )
    result = await call(unknown, "crux.query", {"origin": "https://tiny-startup.example"})
    # Never zeros: a site without enough Chrome traffic has no metrics at all.
    assert result == {
        "origin": "https://tiny-startup.example",
        "form_factor": "all",
        "status": "no_field_data",
    }


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("pagespeed.run", {}),
        ("pagespeed.run", {"url": "http://localhost:8080/"}),
        ("pagespeed.run", {"url": "https://user:pass@example.com/"}),
        ("pagespeed.run", {"url": "ftp://example.com/"}),
        ("pagespeed.run", {"url": "https://example.com/", "strategy": "tablet"}),
        ("pagespeed.run", {"url": "https://example.com/", "categories": ["pwa"]}),
        ("pagespeed.run", {"url": "https://example.com/", "key": "attacker"}),
        ("crux.query", {"origin": "https://example.com", "url": "https://example.com/"}),
        ("crux.query", {"origin": "https://example.com/pricing"}),
        ("crux.query", {"origin": "https://example.com", "form_factor": "watch"}),
        ("serp.organic", {"keyword": ""}),
        ("serp.organic", {"keyword": "crm", "depth": 101}),
        ("serp.organic", {"keyword": "crm", "location_code": "2840"}),
        ("serp.organic", {"keyword": "crm", "language_code": "English"}),
        ("keywords.ideas", {"keywords": ["x"] * 21}),
        ("keywords.ideas", {"keywords": ["crm"], "limit": 0}),
        ("keywords.overview", {"keywords": "crm"}),
        ("backlinks.summary", {"target": "http://10.0.0.1/"}),
        ("backlinks.summary", {"target": "example.com", "include_subdomains": "yes"}),
        ("backlinks.referring_domains", {"target": "example.com", "offset": 10_001}),
        ("backlinks.anchors", {"target": "example.com"}),
    ],
)
def test_arguments_are_closed_and_bounded(operation, arguments):
    with pytest.raises(ServiceArgumentError):
        request_for(operation, arguments)


# ---------------------------------------------------------------- DataForSEO live reads


async def test_serp_keeps_organic_results_and_fits_the_binding():
    provider = Provider(advanced=dataforseo("dataforseo_serp.json"))
    result = await call(provider, "serp.organic", {"keyword": "project management software"})
    assert len(result["records"]) == 10 and result["truncated"] is False
    assert result["has_more"] is False and "next_offset" not in result
    assert result["records"][0] == {
        "rank_group": 1,
        "rank_absolute": 1,
        "domain": "site1.example",
        "url": "https://site1.example/guide/1",
        "title": result["records"][0]["title"],
        "description": result["records"][0]["description"],
    }
    assert len(result["records"][0]["title"]) <= 200
    assert len(result["records"][0]["description"]) == 300
    assert result["serp_features"] == ["featured_snippet", "people_also_ask"]
    assert result["cost_usd"] == "0.0024" and result["se_results_count"] == 1840000000
    (request,) = provider.requests
    assert request.url.path == "/v3/serp/google/organic/live/advanced"
    sent = json.loads(request.content)[0]
    assert sent["location_code"] == 2840 and sent["language_code"] == "en"
    assert sent["depth"] == 10 and sent["tag"].startswith("tin-svc-")
    login = base64.b64encode(b"fixture-login:fixture-password").decode()
    assert request.headers["authorization"] == f"Basic {login}"

    trimmed = await call(provider, "serp.organic", {"keyword": "crm", "depth": 20}, maximum=2500)
    assert trimmed["truncated"] is True and trimmed["has_more"] is True
    assert 0 < len(trimmed["records"]) < 10 and size(trimmed) <= 2500
    assert "next_offset" not in trimmed


async def test_keyword_ideas_page_with_offsets_and_overview_keeps_a_year_of_months():
    provider = Provider(
        live=lambda r: dataforseo(
            "dataforseo_keyword_ideas.json"
            if "keyword_ideas" in r.url.path
            else "dataforseo_keyword_overview.json"
        )(r)
    )
    ideas = await call(provider, "keywords.ideas", {"keywords": ["project management"]})
    assert ideas["total_count"] == 5412 and len(ideas["records"]) == 20
    assert ideas["has_more"] is True and ideas["next_offset"] == 20
    assert ideas["records"][1] == {
        "keyword": "project management idea 1",
        "search_volume": 9600,
        "keyword_difficulty": 31,
        "cpc": 4.3,
        "competition": 0.4,
        "intent": "commercial",
    }
    small = await call(provider, "keywords.ideas", {"keywords": ["pm"], "offset": 40}, maximum=1500)
    assert small["truncated"] is True and small["next_offset"] == 40 + len(small["records"])
    overview = await call(
        provider, "keywords.overview", {"keywords": ["project management software", "kanban board"]}
    )
    first = overview["records"][0]
    assert first["search_volume"] == 90500 and first["keyword_difficulty"] == 78
    assert len(first["monthly"]) == 12 and first["monthly"][0] == ["2026-08", 91300]
    assert overview["cost_usd"] == "0.0102"


async def test_backlinks_summary_and_referring_domains():
    provider = Provider(
        live=lambda r: dataforseo(
            "dataforseo_backlinks_summary.json"
            if "summary" in r.url.path
            else "dataforseo_referring_domains.json"
        )(r)
    )
    summary = await call(provider, "backlinks.summary", {"target": "https://www.example.com/"})
    assert summary["target"] == "https://www.example.com/"
    assert summary["referring_domains"] == 1873 and summary["backlinks"] == 18234
    assert summary["cost_usd"] == "0.02004" and "referring_links_countries" not in summary
    domains = await call(
        provider,
        "backlinks.referring_domains",
        {"target": "www.example.com", "limit": 20, "include_subdomains": False},
    )
    assert domains["total_count"] == 1873 and domains["next_offset"] == 20
    assert domains["records"][0] == {
        "domain": "referrer0.example",
        "rank": 900,
        "backlinks": 400,
        "backlinks_spam_score": 0,
        "first_seen": "2021-05-02 10:00:00 +00:00",
        "lost_date": None,
    }
    sent = json.loads(provider.requests[1].content)[0]
    assert sent["target"] == "example.com" and sent["order_by"] == ["rank,desc"]
    assert sent["include_subdomains"] is False


@pytest.mark.parametrize(
    ("answer", "error", "code"),
    [
        (
            dataforseo("dataforseo_serp.json", task_status=40204),
            ServiceCallRefused,
            "provider_refused",
        ),
        (
            dataforseo("dataforseo_serp.json", task_status=40501),
            ServiceCallRefused,
            "invalid_request",
        ),
        (dataforseo("dataforseo_serp.json", status=40210), ServiceCallRefused, "provider_refused"),
        (
            dataforseo("dataforseo_serp.json", http_status=429),
            IntegrationRateLimitedError,
            "rate_limited",
        ),
    ],
)
async def test_dataforseo_refusals_are_known_outcomes(answer, error, code):
    with pytest.raises(error) as caught:
        await call(Provider(advanced=answer), "serp.organic", {"keyword": "crm"})
    assert caught.value.code == code and "fixture" not in str(caught.value)


def another_tag(request):
    """A complete answer for a different request: DataForSEO echoed another task's tag."""
    answer = json.loads(dataforseo("dataforseo_serp.json")(request).content)
    answer["tasks"][0]["data"]["tag"] = "tin-svc-someone-else"
    return httpx.Response(200, json=answer)


@pytest.mark.parametrize(
    "answer",
    [
        lambda r: httpx.Response(503, json={"status_code": 50000}),
        lambda r: httpx.Response(200, content=b"[" + b" " * 4_000_001 + b"]"),
        lambda r: httpx.Response(200, json={"status_code": 20000, "tasks": None}),
        another_tag,
    ],
    ids=["server_error", "oversized", "invalid_envelope", "another_tag"],
)
async def test_an_unconfirmed_dataforseo_answer_is_an_upstream_error(answer):
    with pytest.raises(IntegrationUpstreamError) as caught:
        await call(Provider(advanced=answer), "serp.organic", {"keyword": "crm"})
    assert "fixture" not in str(caught.value) and "someone" not in str(caught.value)


# ---------------------------------------------------------------- declaration and funding


def definition(*services):
    manifest = json.loads(example_files(KEY)[PATH])
    body = manifest["definition"]
    body["integration_requirements"] = [
        {
            "provider_key": provider,
            "capabilities": list(managed_services.CAPABILITIES[provider]),
            "required": True,
        }
        for provider, _ in services
    ]
    body["code"]["services"] = {
        provider.split(".")[1]: {
            "provider_key": provider,
            "max_calls": calls,
            "max_response_bytes": 32_000,
        }
        for provider, calls in services
    }
    body["code"]["timeout_seconds"] = 60
    return manifest


def test_bindings_declare_managed_services_and_only_paid_ones_meter_the_run():
    free = validate_code_definition(definition((PAGESPEED_PROVIDER, 2))["definition"])
    assert free.services[0].capabilities == ("pagespeed.read", "crux.read")
    assert not free.metered and free.policy == "bounded-code-v1"
    body = definition((PAGESPEED_PROVIDER, 2), (DATAFORSEO_PROVIDER, 4))["definition"]
    spec = validate_code_definition(body)
    assert spec.metered and spec.policy == "managed-code-model-v1"
    terms = model_terms(body)
    # Four DataForSEO reads reserve $0.05 each; PageSpeed adds nothing.
    assert terms["maximum_nanos"] == 200_000_000
    assert terms["operations"] == ["native_model", "tool"]
    assert terms["service_pricing"]["tools"]["dataforseo"] == "provider_reported_task_cost_usd"
    wrong = deepcopy(body)
    wrong["integration_requirements"][0]["capabilities"] = ["http.read"]
    with pytest.raises(ValueError):
        validate_code_definition(wrong)
    assert parse_integration_requirements(body["integration_requirements"])[1].required


def test_procedures_may_read_pagespeed_but_not_buy_dataforseo():
    from test_procedure_services import definition as procedure_definition
    from test_procedure_services import manifest as procedure_manifest

    from tin_lite.private_workflows import validate_private_definition

    for provider, name in ((PAGESPEED_PROVIDER, "speed"), (DATAFORSEO_PROVIDER, "seo")):
        value = procedure_manifest()
        value["definition"]["integration_requirements"] = [
            {
                "provider_key": provider,
                "capabilities": list(managed_services.CAPABILITIES[provider]),
                "required": True,
            }
        ]
        value["definition"]["procedure"]["services"] = {
            name: {"provider_key": provider, "max_calls": 2, "max_response_bytes": 8000}
        }
        if provider == PAGESPEED_PROVIDER:
            spec = validate_private_definition(procedure_definition(value))
            assert spec.services[0].name == "speed"
        else:
            with pytest.raises(ValueError, match="not procedures"):
                validate_private_definition(procedure_definition(value))


async def test_there_is_nothing_for_a_founder_to_connect():
    service = IntegrationService(
        database=None, settings=SimpleNamespace(integration_credential_key=None)
    )
    with pytest.raises(Exception, match="nothing to connect"):
        await service.start_connect(
            project_id=uuid4(), provider_key=PAGESPEED_PROVIDER, clerk_user_id=ACTOR
        )
    assert not service.is_configured(DATAFORSEO_PROVIDER)
    assert managed_services.not_configured(DATAFORSEO_PROVIDER) == (
        "DataForSEO is not configured on this Tin deployment; "
        "an operator sets DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD."
    )


# ---------------------------------------------------------------- through the gateway


class ServiceCompute:
    """Synthetic sandbox: the package's service calls, in order, then a fixed report."""

    def __init__(self, calls):
        self.calls, self.results, self.creates = calls, [], 0

    async def create(self, **kwargs):
        self.creates += 1
        return f"synthetic-managed-{self.creates}"

    async def run_code_and_kill(self, *, packet, model_call, **kwargs):
        assert packet["model_client"] is True
        for service, step, operation, arguments in self.calls:
            payload = {
                "service": service,
                "step": step,
                "operation": operation,
                "arguments": arguments,
            }
            try:
                self.results.append(await model_call({"kind": "service", "payload": payload}))
            except CodeServiceError as exc:
                self.results.append({"error": str(exc)})
        return json.dumps({"path": OUTPUT, "content": "# Managed reads\n"}).encode()

    async def kill(self, sandbox_id):
        pass


async def prepare(f, monkeypatch, manifest, calls, provider, *, settings=SETTINGS, policy):
    compute = ServiceCompute(calls)
    server, common, _ = await setup(f, monkeypatch, compute=compute)
    for name, value in vars(settings).items():
        setattr(f.settings, name, value)
    f.settings.integration_credential_key = None  # Managed services store no credential.
    f.runtime.integrations = IntegrationService(database=f.db, settings=f.settings)
    files = example_files(KEY)
    files[PATH] = json.dumps(manifest)
    f.revision = f.storage.repo.edit({p: raw.encode() for p, raw in files.items()})
    selection = {"project_id": str(f.project.id), "path": PATH, "revision": f.revision}
    validated = structured(await server.call_tool("validate_workflow_package", selection))
    assert validated["valid"] and validated["policy"] == policy, validated
    active = structured(
        await server.call_tool(
            "activate_workflow_package",
            {**selection, "expected_revision": None, "request_id": str(uuid4())},
        )
    )
    code = CodeActivities(common=common)
    code.services.managed = managed(provider, f.settings)
    return server, code, active, compute


async def finish(code, run_id):
    env = ActivityEnvironment()
    await env.run(code.execute, run_id)
    await env.run(code.publish, run_id)
    await code.project(run_id)


async def test_a_dataforseo_read_reserves_its_ceiling_and_charges_the_reported_cost(
    billed, monkeypatch
):
    f = billed
    await fund(f)
    provider = Provider(
        advanced=dataforseo("dataforseo_serp.json", cost=0.0156),
        runPagespeed=lambda r: httpx.Response(200, json=load("pagespeed_mobile.json")),
    )
    calls = [
        ("dataforseo", "serp", "serp.organic", {"keyword": "project management software"}),
        ("pagespeed", "speed", "pagespeed.run", {"url": "https://example.com/"}),
        ("dataforseo", "serp", "serp.organic", {"keyword": "project management software"}),
    ]
    manifest = definition((PAGESPEED_PROVIDER, 2), (DATAFORSEO_PROVIDER, 2))
    server, code, active, compute = await prepare(
        f, monkeypatch, manifest, calls, provider, policy="managed-code-model-v1"
    )
    estimate = structured(
        await server.call_tool(
            "estimate_workflow_run",
            {"project_id": str(f.project.id), "workflow_id": active["workflow_id"], "inputs": {}},
        )
    )
    assert estimate["maximum_usd"] == "0.10"
    run_id = (await start(f, server, active))["id"]
    await finish(code, run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.SUCCEEDED
    serp, speed, replay = compute.results
    assert serp["cost_usd"] == "0.0156" and replay == serp
    assert speed["scores"] == {"performance": 68}
    # The replayed step returned its saved response: one DataForSEO request, one charge.
    assert sum(r.url.host == "api.dataforseo.com" for r in provider.requests) == 1
    operation = await f.db.pool.fetchrow("SELECT * FROM billing_operations")
    assert operation["kind"] == "tool" and operation["status"] in {"observed", "settled"}
    assert operation["maximum_nanos"] == 50_000_000
    assert operation["observed_nanos"] == 15_600_000
    assert json.loads(operation["observation"])["basis"] == "provider_reported_cost"
    await f.billing.settle(run.id)
    await f.billing.settle(run.id)
    charge = await f.billing.run_charge(run.id, ACTOR)
    assert charge["charged_usd"] == "0.02"
    usage = await read_run_usage(database=f.db, run=run)
    providers = sorted(
        item["provider"] for item in usage["own"]["observations"] if item["kind"] == "tool"
    )
    assert providers == ["dataforseo", "pagespeed"]
    free = next(item for item in usage["own"]["observations"] if item["provider"] == "pagespeed")
    assert free["provider_reported_cost_usd"] == "0"


async def test_pagespeed_alone_is_included_and_a_timeout_does_not_block_later_reads(
    billed, monkeypatch
):
    f = billed
    monkeypatch.setattr(managed_services, "GOOGLE_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=load("pagespeed_mobile.json"))

    provider = Provider(
        runPagespeed=slow,
        **{"records:queryRecord": lambda r: httpx.Response(404, json=load("crux_not_found.json"))},
    )
    calls = [
        ("pagespeed", "lab", "pagespeed.run", {"url": "https://example.com/"}),
        ("pagespeed", "field", "crux.query", {"origin": "https://example.com"}),
        ("pagespeed", "lab", "pagespeed.run", {"url": "https://example.com/"}),
    ]
    manifest = definition((PAGESPEED_PROVIDER, 2))
    server, code, active, compute = await prepare(
        f, monkeypatch, manifest, calls, provider, policy="bounded-code-v1"
    )
    run_id = (await start(f, server, active))["id"]
    await finish(code, run_id)
    lab, field, replay = compute.results
    assert lab["status"] == "timed_out" and replay == lab
    assert field["status"] == "no_field_data"
    assert len(provider.requests) == 2
    receipts = await f.db.pool.fetch(
        "SELECT status, result FROM effect_receipts WHERE operation=$1", OPERATION
    )
    assert {r["status"] for r in receipts} == {"completed"} and len(receipts) == 2
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
    charge = await f.billing.run_charge(UUID(run_id), ACTOR)
    assert charge["charged_usd"] == "0.00" and charge["reason"] == "bounded-code-v1"


async def test_an_unset_key_refuses_the_start_and_a_removed_key_refuses_the_call(
    billed, monkeypatch
):
    f = billed
    provider = Provider(runPagespeed=lambda r: httpx.Response(200, json={}))
    calls = [("pagespeed", "lab", "pagespeed.run", {"url": "https://example.com/"})]
    manifest = definition((PAGESPEED_PROVIDER, 1))
    server, code, active, compute = await prepare(
        f,
        monkeypatch,
        manifest,
        calls,
        provider,
        settings=SimpleNamespace(pagespeed_api_key=None),
        policy="bounded-code-v1",
    )
    with pytest.raises(ToolError, match="TIN_LITE_PAGESPEED_API_KEY"):
        await start(f, server, active)
    f.settings.pagespeed_api_key = SecretStr(GOOGLE_KEY)
    run_id = (await start(f, server, active))["id"]
    f.settings.pagespeed_api_key = None
    await finish(code, run_id)
    assert compute.results == [
        {
            "error": "PageSpeed Insights and CrUX is not configured on this Tin deployment; "
            "an operator sets TIN_LITE_PAGESPEED_API_KEY."
        }
    ]
    assert not provider.requests
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM effect_receipts WHERE operation=$1", OPERATION
    )


async def test_a_refused_dataforseo_read_settles_and_a_later_step_still_runs(billed, monkeypatch):
    f = billed
    await fund(f)
    provider = Provider(
        live=lambda r: (
            dataforseo("dataforseo_backlinks_summary.json", task_status=40204)(r)
            if "backlinks" in r.url.path
            else dataforseo("dataforseo_keyword_overview.json")(r)
        )
    )
    calls = [
        ("dataforseo", "links", "backlinks.summary", {"target": "example.com"}),
        ("dataforseo", "volumes", "keywords.overview", {"keywords": ["kanban board"]}),
    ]
    manifest = definition((DATAFORSEO_PROVIDER, 2))
    server, code, active, compute = await prepare(
        f, monkeypatch, manifest, calls, provider, policy="managed-code-model-v1"
    )
    run_id = (await start(f, server, active))["id"]
    await finish(code, run_id)
    refused, volumes = compute.results
    assert "status 40204" in refused["error"] and "operator" in refused["error"]
    assert volumes["records"][0]["keyword"] == "project management software"
    # The refusal settled at DataForSEO's reported $0; the later read settled its own cost.
    rows = await f.db.pool.fetch(
        "SELECT status, observed_nanos FROM billing_operations ORDER BY observed_nanos"
    )
    assert [(r["status"], r["observed_nanos"]) for r in rows] == [
        ("observed", 0),
        ("observed", 10_200_000),
    ]


async def test_an_unconfirmed_read_stays_unconfirmed_and_is_not_repeated(billed, monkeypatch):
    f = billed
    await fund(f)
    provider = Provider(live=lambda r: httpx.Response(503, json={"status_code": 50000}))
    calls = [
        ("dataforseo", "links", "backlinks.summary", {"target": "example.com"}),
        ("dataforseo", "links", "backlinks.summary", {"target": "example.com"}),
    ]
    manifest = definition((DATAFORSEO_PROVIDER, 2))
    server, code, active, compute = await prepare(
        f, monkeypatch, manifest, calls, provider, policy="managed-code-model-v1"
    )
    run_id = (await start(f, server, active))["id"]
    await finish(code, run_id)
    lost, replay = compute.results
    assert "will not be repeated" in lost["error"]
    assert "unconfirmed result" in replay["error"]
    assert len(provider.requests) == 1
    # Sent and unconfirmed: the receipt and its reservation stay open, never settled as free.
    receipt = await f.db.pool.fetchrow(
        "SELECT status FROM effect_receipts WHERE operation=$1", OPERATION
    )
    assert receipt["status"] != "completed"
    operation = await f.db.pool.fetchrow("SELECT status, observed_nanos FROM billing_operations")
    assert operation["status"] not in {"observed", "settled"}
    assert operation["observed_nanos"] is None


async def test_a_replayed_spending_stop_keeps_its_reason(billed, monkeypatch):
    f = billed
    await fund(f)
    provider = Provider(live=dataforseo("dataforseo_keyword_overview.json"))
    calls = [
        ("dataforseo", "volumes", "keywords.overview", {"keywords": ["kanban board"]}),
        ("dataforseo", "volumes", "keywords.overview", {"keywords": ["kanban board"]}),
    ]
    manifest = definition((DATAFORSEO_PROVIDER, 2))
    server, code, active, compute = await prepare(
        f, monkeypatch, manifest, calls, provider, policy="managed-code-model-v1"
    )
    run_id = (await start(f, server, active))["id"]
    from tin_lite.billing_contracts import BillingError

    async def refuse(*args, **kwargs):
        raise BillingError("budget_exhausted", "The run budget is spent.")

    monkeypatch.setattr(f.db.billing, "begin_operation", refuse)
    await finish(code, run_id)
    assert compute.results == [{"error": SPENDING_STOPPED}] * 2
    assert not provider.requests
