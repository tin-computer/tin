"""Qualification uses synthetic outputs and disposable SQL; no paid/live execution."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from test_billing import billed as billed
from test_billing import finish
from test_codex_api_billing import paid_relay, post
from test_private_workflows import ACTOR, app, fixture, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.community import validate_files
from tin_lite.private_workflows import PackageActivation, validate_private_definition
from tin_lite.run_usage import read_run_usage
from tin_lite.workflow_creator import creator_files
from tin_lite.workflow_qualification import (
    Case,
    Qualification,
    add_evidence,
    assess_output,
    check_package,
    inspect_candidate,
    qualification_path,
    read_json,
)
from tin_lite.workflow_qualification_cli import check_checkout, run_cases
from tin_lite.workflow_qualification_service import (
    EvaluationStart,
    QualificationError,
    QualificationSelection,
    WorkflowQualification,
)

ROOT = Path(__file__).parents[1]
KEY = "example.csv_summary"
PATH = f"workflow_packages/{KEY}/workflow.json"


def csv_files():
    return {
        f"workflow_packages/{KEY}/{p.name}": p.read_bytes()
        for p in (ROOT / "workflow_packages" / KEY).iterdir()
    }


def csv_contract():
    return Qualification.model_validate_json((ROOT / qualification_path(KEY)).read_bytes())


async def test_creator_and_examples_are_valid_without_registry_registration():
    from tin_lite.public_workflows import PUBLIC_WORKFLOWS

    files = {p: s.encode() for p, s in creator_files().items()}
    path = "workflow_packages/custom.workflow_create/workflow.json"
    definition, _ = await validate_files(files, definition_path=path)
    validate_private_definition(definition)
    contract = Qualification.model_validate_json(
        (ROOT / qualification_path("custom.workflow_create")).read_bytes()
    )
    report = await check_package(files, path, contract)
    assert report["cost"]["basis"] == "unmeasured"
    assert report["cost"]["expected_range_usd"] is None
    assert report["cost"]["configured_ceiling_usd"] == "10"
    assert report["safety"]["status"] == "review_required"
    assert all(not w.key.startswith(("example.", "custom.")) for w in PUBLIC_WORKFLOWS)


async def test_static_candidate_never_executes_source_or_trusts_author_results(tmp_path):
    files = csv_files()
    marker = tmp_path / "must-not-exist"
    files[f"workflow_packages/{KEY}/main.py"] = (
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def run(ctx, inputs): return {}\n"
    ).encode()
    candidate = {
        "format": "tin-workflow-candidate-v1",
        "files": {p: b.decode() for p, b in files.items()},
        "qualification": csv_contract().model_dump(),
    }
    inspected = await inspect_candidate(json.dumps(candidate).encode())
    assert not marker.exists()
    assert inspected["qualification"]["cost"]["expected_range_usd"] == ["0", "0"]
    assert inspected["qualification"]["evaluation"]["status"] == "not_run"
    assert inspected["changes"][-1]["path"] == qualification_path(KEY)
    candidate["qualification"]["measured_cost_usd"] = "0.01"
    with pytest.raises(ValidationError):
        await inspect_candidate(json.dumps(candidate).encode())


async def test_candidate_extra_files_and_case_input_override_are_rejected():
    files = csv_files()
    files["workflow_packages/other/main.py"] = b"pass\n"
    with pytest.raises(ValueError):
        await check_package(files, PATH, csv_contract())
    contract = csv_contract()
    contract.cases[0].inputs["project_id"] = str(uuid4())
    with pytest.raises(ValueError):
        await check_package(csv_files(), PATH, contract)


@pytest.mark.parametrize("raw", [b'{"version":1,"version":1}', b'{"number":NaN}'])
def test_noncanonical_json_rejected(raw):
    with pytest.raises(ValueError):
        read_json(raw)


@pytest.mark.parametrize(
    "schema",
    [{"$ref": "https://example.test/schema"}, {"type": "string", "pattern": "(a+)+$"}],
)
def test_assertions_cannot_fetch_remote_schemas_or_run_author_regex(schema):
    with pytest.raises(ValueError):
        Case.model_validate(
            {
                "id": "ordinary",
                "description": "bounded",
                "inputs": {},
                "expect": {"json_schema": schema},
            }
        )


def test_plausible_but_wrong_result_fails_numerical_assertion():
    case = Case.model_validate(
        {
            "id": "ordinary",
            "description": "The fixture has 3 conversions from 12 actors.",
            "inputs": {},
            "expect": {
                "json_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"rate": {"type": "number", "enum": [0.25]}},
                    "required": ["rate"],
                }
            },
        }
    )
    assert assess_output(case, status="succeeded", content=b'{"rate": 0.25}')["status"] == "passed"
    assert assess_output(case, status="succeeded", content=b'{"rate": 0.75}')["status"] == "failed"
    assert assess_output(case, status="failed", content=None)["status"] == "failed"


async def test_versioned_csv_cases_execute_reviewed_code_and_check_exact_results(tmp_path):
    # Only this checked-in fixture is imported by pytest. The qualification service never imports.
    source = tmp_path / "main.py"
    source.write_bytes((ROOT / Path(PATH).parent / "main.py").read_bytes())
    spec = importlib.util.spec_from_file_location("csv_example", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for case in csv_contract().cases:
        try:
            output = module.run(None, case.inputs)
            status, content = "succeeded", output["content"].encode()
        except ValueError:
            status, content = "failed", None
        assert assess_output(case, status=status, content=content)["status"] == "passed"
    report = await check_checkout(ROOT, PATH)
    assert report["evaluation"]["status"] == "not_run"


async def test_standing_analytics_candidate_keeps_full_job_and_honest_limits():
    raw = (ROOT / "tests/fixtures/workflow_creation/analytics_candidate.json").read_bytes()
    result = await inspect_candidate(raw)
    report = result["qualification"]
    assert report["workflow"] == "product.analytics_brief"
    assert report["cost"]["basis"] == "unmeasured"
    assert report["evaluation"]["quality_review"] == "pending"
    assert report["evaluation"]["status"] == "not_run"
    assert report["contract"]["integrations"][0]["provider_key"] == "custom.api.posthog"
    case = Qualification.model_validate(read_json(raw)["qualification"]).cases[0]
    narrow = b"Activation\nOne funnel has 3 of 12 conversions."
    assert assess_output(case, status="succeeded", content=narrow)["status"] == "failed"
    sections = b"Activation\nKey-event trends\nTraffic\nError signals\nBreakdown\n"
    complete = b"Status: complete\nOrdered funnel (projects): 12 -> 7 -> 3\n" + sections
    assert assess_output(case, status="succeeded", content=complete)["status"] == "passed"
    # Successful artifact delivery with every heading is not successful ordinary analysis.
    incomplete = b"Status: incomplete\nBoth funnel queries failed.\n" + sections
    assert assess_output(case, status="succeeded", content=incomplete)["status"] == "failed"
    # A plausible completed report must also match the controlled fixture's known answer.
    wrong_counts = complete.replace(b"12 -> 7 -> 3", b"12 -> 12 -> 12")
    assert assess_output(case, status="succeeded", content=wrong_counts)["status"] == "failed"
    assert result["author_limitations"]  # No claim this synthetic candidate is publication-ready.


async def test_measured_ranges_are_per_case_and_price_contract_including_failed_runs():
    contract = csv_contract()
    report = await check_package(csv_files(), PATH, contract)

    def sample(amount, card="v1", status="succeeded"):
        return {
            "case_id": "ordinary",
            "pricing_digest": card,
            "model_cost_usd": amount,
            "status": status,
            "assessment": {"status": "passed"},
        }

    measured = add_evidence(
        deepcopy(report), contract, [sample("0.004"), sample("0.006", status="failed")]
    )
    cost = measured["evaluation"]["cases"][0]["cost"]
    assert cost["expected_range_usd"] == ["0.004", "0.006"]
    assert cost["sample_count"] == 2 and cost["observed_failures"] == 1
    for samples in ([sample("0.004"), sample(None)], [sample("0.004"), sample("0.006", "v2")]):
        mixed = add_evidence(deepcopy(report), contract, samples)
        assert mixed["evaluation"]["cases"][0]["cost"]["expected_range_usd"] is None


async def prepared(db):
    f = await fixture(db)
    key = "custom.csv_summary"
    f.path = f"workflow_packages/{key}/workflow.json"
    files = {
        p.replace(KEY, key): b.replace(KEY.encode(), key.encode()) for p, b in csv_files().items()
    }
    f.contract = csv_contract()
    files[qualification_path(key)] = f.contract.model_dump_json().encode()
    f.revision = f.storage.repo.edit(files)
    f.qualifier = WorkflowQualification(database=f.db, storage=f.storage, settings=f.settings)
    activated = await f.service.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path=f.path, revision=f.revision, request_id=uuid4(), expected_revision=None
        ),
    )
    f.workflow_id = UUID(activated["workflow_id"])
    return f


def chosen(f, **changes):
    return EvaluationStart(
        path=f.path,
        revision=f.revision,
        workflow_id=f.workflow_id,
        case_id="ordinary",
        request_id=uuid4(),
        maximum_usd="0",
        **changes,
    )


async def start_case(f, selection):
    return await f.qualifier.start_case(
        runtime=f.runtime, project_id=f.project.id, actor=ACTOR, client_id=None, selection=selection
    )


async def test_http_mcp_share_checks_and_do_not_read_other_projects(publication_db, monkeypatch):
    f = await prepared(publication_db)
    selection = {"path": f.path, "revision": f.revision}
    server = mcp(f, monkeypatch)
    mcp_report = structured(
        await server.call_tool(
            "qualify_workflow_package", {"project_id": str(f.project.id), **selection}
        )
    )
    base = f"/api/projects/{f.project.id}/workflow-packages"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(base + "/qualify", json=selection)
        assert response.status_code == 200
        assert response.json() == mcp_report
    f.storage.read_workflow_resource = AsyncMock(side_effect=AssertionError("must authorize first"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "outsider")), base_url="https://tin.test"
    ) as client:
        response = await client.post(base + "/qualify", json=selection)
        assert response.status_code == 404
    f.storage.read_workflow_resource.assert_not_called()
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_saved_creator_output_becomes_reviewable_files_then_pinned_qualification(
    publication_db,
):
    from tin_lite.run_service import start_workflow_run

    f = await fixture(publication_db)
    revision = f.storage.repo.edit({p: s.encode() for p, s in creator_files().items()})
    activated = await f.service.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path="workflow_packages/custom.workflow_create/workflow.json",
            revision=revision,
            request_id=uuid4(),
            expected_revision=None,
        ),
    )
    workflow = await f.db.get_workflow(UUID(activated["workflow_id"]))
    run = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={
            "brief": "Summarize integer CSV amounts.",
            "workflow_key": KEY,
            "scope": "public",
        },
    )
    # Substitute only the author/model output, then exercise the real transport and validator.
    candidate = json.dumps(
        {
            "format": "tin-workflow-candidate-v1",
            "files": {p: b.decode() for p, b in csv_files().items()},
            "qualification": csv_contract().model_dump(),
        }
    ).encode()
    output_path = "reports/WORKFLOW_CANDIDATE.json"
    output_revision = f.storage.repo.edit({output_path: candidate})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
        "artifact_path=$3 WHERE id=$1",
        run.id,
        output_revision,
        output_path,
    )
    base = f"/api/projects/{f.project.id}/workflow-packages"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(base + "/candidate", json={"run_id": str(run.id)})
        assert response.status_code == 200
        changes = response.json()["changes"]
        assert len(changes) == 3
        assert f.storage.repo.writes == 0  # Inspection does not commit or activate candidate files.
        reviewed = f.storage.repo.edit({c["path"]: c["content"].encode() for c in changes})
        checked = await client.post(base + "/qualify", json={"path": PATH, "revision": reviewed})
        assert checked.status_code == 200
        assert checked.json()["shape"]["status"] == "passed"
        assert checked.json()["evaluation"]["status"] == "not_run"
        assert [
            w.key for w in await f.db.list_workflows(project_id=f.project.id) if w.project_id
        ] == ["custom.workflow_create"]


async def test_http_and_mcp_evaluation_preserve_caller_bound_idempotency(
    publication_db, monkeypatch
):
    from mcp.server.mcpserver.exceptions import ToolError

    f = await prepared(publication_db)
    selection = chosen(f).model_dump(mode="json", exclude={"runs"})
    base = f"/api/projects/{f.project.id}/workflow-packages"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(base + "/evaluate", json=selection)
        assert response.status_code == 200
        replay = await client.post(base + "/evaluate", json=selection)
        assert replay.json()["run_id"] == response.json()["run_id"]
    server = mcp(f, monkeypatch)
    with pytest.raises(ToolError, match="different OAuth client"):
        await server.call_tool(
            "evaluate_workflow_case", {"project_id": str(f.project.id), **selection}
        )
    selection["request_id"] = str(uuid4())
    recovered = structured(
        await server.call_tool(
            "evaluate_workflow_case", {"project_id": str(f.project.id), **selection}
        )
    )
    replay = structured(
        await server.call_tool(
            "evaluate_workflow_case", {"project_id": str(f.project.id), **selection}
        )
    )
    assert recovered["run_id"] == replay["run_id"]
    assert len(await f.db.list_runs(project_id=f.project.id)) == 2


async def test_live_start_is_idempotent_and_preserves_old_pin_after_activation(publication_db):
    f = await prepared(publication_db)
    selection = chosen(f)
    first = await start_case(f, selection)
    new_source = f.storage.repo.trees[f.revision][f.path][1].replace(b'"1.0.0"', b'"1.0.1"')
    newer = f.storage.repo.edit({f.path: new_source})
    await f.service.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path=f.path, revision=newer, request_id=uuid4(), expected_revision=f.revision
        ),
    )
    replay = await start_case(f, selection)
    assert replay["run_id"] == first["run_id"]
    assert replay["definition_revision"] == f.revision
    starts = f.runtime.temporal.start_workflow.await_args_list
    assert len(starts) == 2
    assert starts[0].kwargs["id"] == starts[1].kwargs["id"]
    with pytest.raises(QualificationError, match="exact reviewed"):
        await start_case(f, chosen(f))
    assert len(await f.db.list_runs(project_id=f.project.id)) == 1


async def test_qualification_matches_run_inputs_source_and_canonical_output(publication_db):
    f = await prepared(publication_db)
    started = await start_case(f, chosen(f))
    run_id = UUID(started["run_id"])
    output_path = "reports/CSV_SUMMARY.md"
    output_revision = f.storage.repo.edit({output_path: b"Rows: 2\nTotal amount (cents): 3500\n"})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
        "artifact_path=$3 WHERE id=$1",
        run_id,
        output_revision,
        output_path,
    )
    # A later project edit must not alter the sample.
    f.storage.repo.edit({output_path: b"Wrong later output"})
    selection = QualificationSelection(
        path=f.path, revision=f.revision, runs=[{"case_id": "ordinary", "run_id": run_id}]
    )
    report = await f.qualifier.qualify(project_id=f.project.id, actor=ACTOR, selection=selection)
    sample = report["evaluation"]["cases"][0]["samples"][0]
    assert sample["assessment"]["status"] == "passed"
    assert sample["artifact"]["revision"] == output_revision
    assert sample["model_cost_usd"] == "0"
    selection.runs[0].case_id = "zero"
    with pytest.raises(QualificationError, match="inputs differ"):
        await f.qualifier.qualify(project_id=f.project.id, actor=ACTOR, selection=selection)
    other = await f.db.create_project(name="Other", state_repo_id="projects/other")
    await f.db.pool.execute("UPDATE workflow_runs SET project_id=$2 WHERE id=$1", run_id, other.id)
    with pytest.raises(LookupError):
        await f.qualifier.qualify(project_id=f.project.id, actor=ACTOR, selection=selection)


async def test_private_execution_allowlist_still_required(publication_db):
    from tin_lite.private_workflows import PrivateWorkflowError

    f = await prepared(publication_db)
    f.settings.private_workflow_projects = set()
    with pytest.raises(PrivateWorkflowError):
        await start_case(f, chosen(f))
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_paid_case_checks_caller_limit_and_requires_enforced_billing(publication_db):
    from test_private_workflows import PATH as PROCEDURE_PATH
    from test_private_workflows import activate

    f = await fixture(publication_db)
    activated = await activate(f)
    contract = Qualification.model_validate(
        {
            "version": 1,
            "cases": [
                {
                    "id": "ordinary",
                    "description": "A short report.",
                    "inputs": {"brief": "Explain the public docs"},
                    "expect": {"contains": ["#"]},
                }
            ],
        }
    )
    revision = f.storage.repo.edit(
        {qualification_path("custom.research_digest"): contract.model_dump_json().encode()}
    )
    f.qualifier = WorkflowQualification(database=f.db, storage=f.storage, settings=f.settings)
    selection = EvaluationStart(
        path=PROCEDURE_PATH,
        revision=revision,
        workflow_id=activated["workflow_id"],
        case_id="ordinary",
        request_id=uuid4(),
        maximum_usd="4",
    )
    with pytest.raises(
        QualificationError,
        match=r"needs maximum_usd of at least \$10\.00 \(you authorized \$4\.00\)",
    ):
        await start_case(f, selection)
    selection.maximum_usd = "10"
    with pytest.raises(QualificationError, match="enforced run budgets"):
        await start_case(f, selection)
    assert await f.db.list_runs(project_id=f.project.id) == []
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_fixture_checks_do_not_become_live_measurements(tmp_path):
    files = csv_files()
    for path, raw in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    target = tmp_path / qualification_path(KEY)
    target.parent.mkdir(parents=True)
    target.write_text(csv_contract().model_dump_json())
    report = await check_checkout(
        tmp_path,
        PATH,
        fixtures=json.dumps(
            {
                "ordinary": {
                    "status": "succeeded",
                    "content": "Rows: 2; Total amount (cents): 3500",
                },
                "zero": {"status": "succeeded", "content": "Rows: 1; Total amount (cents): 0"},
                "invalid_amount": {"status": "failed", "content": None},
            }
        ).encode(),
    )
    assert report["evaluation"]["status"] == "fixture_assertions_passed"
    assert "samples" not in str(report)
    assert report["safety"]["status"] == "review_required"


async def test_live_runner_stops_for_human_review_without_approving(tmp_path):
    report = await check_package(csv_files(), PATH, csv_contract())
    requests = []

    def wire(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/qualify"):
            return httpx.Response(200, json=report)
        if request.url.path.endswith("/evaluate"):
            return httpx.Response(200, json={"run_id": str(uuid4())})
        return httpx.Response(200, json={"status": "needs_input"})

    async with httpx.AsyncClient(
        base_url="https://tin.test", transport=httpx.MockTransport(wire)
    ) as client:
        with pytest.raises(ValueError, match="human review"):
            await run_cases(
                client,
                project_id=str(uuid4()),
                workflow_id=str(uuid4()),
                path=PATH,
                revision="a" * 40,
                maximum_usd="0",
                directory=tmp_path / "eval",
            )
    assert len(requests) == 3  # read cases, explicitly start one, read status; no approval.


async def test_unrounded_model_usage_and_actual_settlement_remain_separate(billed):
    f = billed
    # gpt-6-sol: 500 x $2/M + 300 x $10/M = $0.004, below the one-cent settlement unit.
    run, _, application, _ = await paid_relay(f, provider_usage=(500, 300, 0, 0))
    response = await post(application, run)
    assert response.status_code == 200
    await application.aclose()
    await finish(f, run)
    assert await f.billing.settle(run.id) == 0
    run = await f.db.get_run(run.id)
    qualifier = WorkflowQualification(database=f.db, storage=f.storage, settings=f.settings)
    cost = await qualifier._cost(
        run, await read_run_usage(database=f.db, run=run), model_free=False
    )
    assert cost["model_cost_usd"] == "0.004"
    assert cost["actual_charge_usd"] == "0"
    assert cost["enforced_ceiling_usd"] == "5"
    assert cost["cost_evidence"] == "verified_ledger_usage"


async def test_missing_usage_is_not_free_or_measured(billed):
    f = billed
    run, _, application, _ = await paid_relay(f, missing_usage=True)
    await post(application, run)
    await application.aclose()
    qualifier = WorkflowQualification(database=f.db, storage=f.storage, settings=f.settings)
    cost = await qualifier._cost(
        run, await read_run_usage(database=f.db, run=run), model_free=False
    )
    assert cost["model_cost_usd"] is None
    assert cost["cost_evidence"] == "unavailable"


async def test_live_runner_reuses_request_after_lost_start_ack_and_checks_total(tmp_path):
    report = await check_package(csv_files(), PATH, csv_contract())
    report["cost"]["configured_ceiling_usd"] = "0.02"
    calls, run_ids = [], {}
    lose = True

    def wire(request):
        nonlocal lose
        data = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/qualify"):
            return httpx.Response(200, json=report)
        if request.url.path.endswith("/evaluate"):
            calls.append(data["request_id"])
            run_ids.setdefault(data["request_id"], str(uuid4()))
            if lose:
                lose = False
                raise httpx.ReadError("lost acknowledgement")
            return httpx.Response(200, json={"run_id": run_ids[data["request_id"]]})
        return httpx.Response(200, json={"status": "succeeded"})

    async with httpx.AsyncClient(
        base_url="https://tin.test", transport=httpx.MockTransport(wire)
    ) as client:
        arguments = dict(
            project_id=str(uuid4()),
            workflow_id=str(uuid4()),
            path=PATH,
            revision="a" * 40,
            directory=tmp_path / "evaluation",
            maximum_usd="0.06",
        )
        with pytest.raises(ValueError, match="sum of case ceilings"):
            await run_cases(client, **{**arguments, "maximum_usd": "0.05"})
        assert calls == []
        with pytest.raises(httpx.ReadError):
            await run_cases(client, **arguments)
        await run_cases(client, **arguments)
        assert calls[0] == calls[1]
        assert len(run_ids) == 3
        assert (arguments["directory"] / "qualification.json").exists()
        with pytest.raises(ValueError, match="different candidate"):
            await run_cases(client, **{**arguments, "revision": "b" * 40})
