"""Checks the bounded review protocol, not an agent's claim that a check passed."""

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_codex_isolation import load_sandbox_module
from test_procedure_publication import PATH, activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.e2b_runtime import E2BRuntime


def test_pinned_detached_checkout_can_push_its_new_ephemeral_branch(tmp_path):
    remote = tmp_path / "ephemeral.git"
    workspace = tmp_path / "state"

    def git(*args):
        return subprocess.check_output(  # noqa: S603 — fixture-only Git arguments
            ["/usr/bin/git", *map(str, args)], text=True
        ).strip()

    git("init", "--bare", remote)
    git("init", workspace)
    git("-C", workspace, "config", "user.name", "Test")
    git("-C", workspace, "config", "user.email", "test@example.test")
    git("-C", workspace, "commit", "--allow-empty", "-m", "pinned source")
    git("-C", workspace, "checkout", "--detach", "HEAD")
    git("-C", workspace, "commit", "--allow-empty", "-m", "reviewed diagram")
    git("-C", workspace, "remote", "add", "ephemeral", remote)
    runner = (Path(__file__).parents[1] / "sandbox/run_procedure.sh").read_text()
    command = next(line.strip() for line in runner.splitlines() if "push ephemeral" in line)
    subprocess.run(  # noqa: S603 — exact Tin-owned runner line, fixture environment
        ["/bin/bash", "-c", command],
        check=True,
        env={
            **os.environ,
            "state_workspace": str(workspace),
            "TIN_EPHEMERAL_BRANCH": "procedures/test/1",
        },
    )
    assert git("--git-dir", remote, "rev-parse", "refs/heads/procedures/test/1") == git(
        "-C", workspace, "rev-parse", "HEAD"
    )


def report(sha="a", passed=True):
    return {
        "checker": "tin-diagram-check.v1",
        "source_sha256": sha,
        "source_bytes": 90,
        "renderer_sha256": "renderer",
        "browser_version": "pinned",
        "passed": passed,
        "issues": [] if passed else ["nodes overlap"],
        "previews": ["/preview/light.png", "/preview/dark.png"],
    }


def review(monkeypatch, reports):
    module = load_sandbox_module("diagram_review")
    loop = module.DiagramReview(Path("candidate.mmd"))
    stream = iter(reports)
    monkeypatch.setattr(loop, "check", lambda: next(stream))
    return loop


def test_faked_acceptance_cannot_skip_image_inspection(monkeypatch):
    loop = review(monkeypatch, [report(), report()])
    accepted = {"accepted": True, "inspected_sha256": "a"}
    inputs = loop.next_input(accepted)
    assert [item["type"] for item in inputs] == ["text", "localImage", "localImage"]
    assert loop.next_input(accepted) is None
    assert loop.report["candidates"] == 1


def test_bad_candidate_can_be_repaired_then_inspected(monkeypatch):
    loop = review(monkeypatch, [report(passed=False), report("b"), report("b")])
    assert "nodes overlap" in loop.next_input({})[0]["text"]
    assert loop.next_input({})[-1]["type"] == "localImage"
    assert loop.next_input({"accepted": True, "inspected_sha256": "b"}) is None
    assert loop.report["candidates"] == 2


def test_advisory_quality_reaches_inspection_without_skipping_the_hash_gate(monkeypatch):
    measured = report()
    measured["themes"] = [
        {"theme": "light", "issues": [], "quality": {"crossings": 12, "fitScale": 0.3}},
        {"theme": "dark", "issues": [], "quality": {"crossings": 12, "fitScale": 0.3}},
    ]
    loop = review(monkeypatch, [measured, measured])
    inputs = loop.next_input({"accepted": True, "inspected_sha256": "a"})
    assert '"crossings": 12' in inputs[0]["text"]
    assert "balanced equivalent branches" in inputs[0]["text"]
    assert "1200 by 800" in inputs[0]["text"]
    assert loop.report is None
    assert loop.next_input({"accepted": True, "inspected_sha256": "a"}) is None


def test_two_repairs_exhausted(monkeypatch):
    loop = review(monkeypatch, [report(passed=False)] * 3)
    loop.next_input({})
    loop.next_input({})
    with pytest.raises(RuntimeError, match="failed final visual"):
        loop.next_input({})


def test_missing_candidate_uses_same_two_repair_bound(monkeypatch):
    loop = review(monkeypatch, [report(None, passed=False)] * 3)
    loop.next_input({})
    loop.next_input({})
    with pytest.raises(RuntimeError, match="failed final visual"):
        loop.next_input({})


@pytest.mark.asyncio
@pytest.mark.parametrize("validator", ["tin-diagram.reviewed.v1", "tin-diagram.branded.v1"])
async def test_startup_timeout_does_not_mark_a_paid_attempt(monkeypatch, validator):
    import tin_lite.codex_api as api

    paid = AsyncMock()
    monkeypatch.setattr(api, "run_api_attempt", paid)
    prepare = AsyncMock(side_effect=TimeoutError("startup"))
    activities = object.__new__(TinActivities)
    activities._sandboxes = SimpleNamespace(prepare_diagram=prepare)
    run_input = SimpleNamespace(
        context={"output": {"validator": validator}},
        api_url="test",
    )
    with pytest.raises(TimeoutError):
        await activities._run_accounted_procedure(
            conn=None, run=None, sandbox_id="sandbox", run_input=run_input
        )
    paid.assert_not_awaited()


def test_source_change_requires_fresh_images(monkeypatch):
    loop = review(monkeypatch, [report("a"), report("b"), report("b")])
    loop.next_input({})
    assert loop.next_input({"accepted": True, "inspected_sha256": "a"}) is not None
    assert loop.next_input({"accepted": True, "inspected_sha256": "b"}) is None
    assert loop.report["source_sha256"] == "b"


def test_semantic_rejection_requires_repair_and_reinspection(monkeypatch):
    loop = review(monkeypatch, [report(), report(), report("b"), report("b")])
    loop.next_input({})
    assert "Repair the diagram" in loop.next_input({})[0]["text"]
    loop.next_input({"accepted": True, "inspected_sha256": "a"})
    assert loop.next_input({"accepted": True, "inspected_sha256": "b"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", [False, True])
@pytest.mark.parametrize(
    "tamper",
    [
        None,
        "source_sha256",
        "renderer_sha256",
        "themes",
        "issues",
        "theme_issue",
        "missing_theme",
        "duplicate_theme",
        "malformed_theme",
        "missing_theme_issues",
    ],
)
async def test_final_check_is_fresh_offline_and_hash_bound(monkeypatch, tamper, quality):
    import tin_lite.e2b_runtime as runtime_module

    source = 'graph LR\n a["Start"]:::step\n b["Done"]:::receipt\n a --> b\n'
    assets = Path(runtime_module.__file__).parent / "static"
    names = [
        "app.css",
        "diagram-renderer.js",
        "diagram-routing.wasm",
        "fonts/geist-sans-regular.woff2",
        "fonts/geist-sans-bold.woff2",
        "fonts/geist-mono-regular.woff2",
        "fonts/geist-mono-bold.woff2",
    ]
    proof = {
        **report(hashlib.sha256(source.encode()).hexdigest()),
        "source_bytes": len(source.encode()),
        "renderer_sha256": hashlib.sha256(
            b"".join((assets / name).read_bytes() for name in names)
        ).hexdigest(),
        "themes": [{"theme": "light", "issues": []}, {"theme": "dark", "issues": []}],
    }
    if quality:
        for theme in proof["themes"]:
            theme["quality"] = {"bends": 2, "crossings": 0, "fitScale": 0.75}
    if tamper == "theme_issue":
        proof["themes"][1]["issues"] = [{"kind": "node-overlap"}]
    elif tamper == "missing_theme":
        proof["themes"].pop()
    elif tamper == "duplicate_theme":
        proof["themes"][1]["theme"] = "light"
    elif tamper == "malformed_theme":
        proof["themes"][1] = None
    elif tamper == "missing_theme_issues":
        del proof["themes"][1]["issues"]
    elif tamper:
        proof[tamper] = "forged"
    sandbox = SimpleNamespace(
        files=SimpleNamespace(write=AsyncMock()),
        commands=SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(stdout=json.dumps(proof)))
        ),
    )
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr(runtime_module.AsyncSandbox, "create", create)
    runtime = E2BRuntime(
        api_key="test", template="test", timeout_seconds=300, egress_allow_hosts=()
    )
    runtime._observe_sandbox = AsyncMock()
    runtime._delete_sandbox = AsyncMock()
    if tamper:
        with pytest.raises(RuntimeError, match="independent visual validation"):
            await runtime.validate_diagram(content=source, run_id="run", revision="revision")
    else:
        result = await runtime.validate_diagram(content=source, run_id="run", revision="revision")
        assert result["revision"] == "revision"
    runtime._delete_sandbox.assert_awaited_once_with(sandbox)
    assert "allow_out" not in create.call_args.kwargs["network"]
    assert create.call_args.kwargs["network"]["deny_out"](SimpleNamespace(all_traffic="all")) == [
        "all"
    ]
    assert "--no-previews" in sandbox.commands.run.call_args.args[0]
    assert sandbox.files.write.call_args.args[1] == source.encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_recovered_diagram_cannot_skip_independent_check(publication_db, monkeypatch, valid):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)

    async def direct(awaitable, **_kwargs):
        return await awaitable

    monkeypatch.setattr(activities, "_await_with_heartbeats", direct)
    definition, spec = await activities._pinned_codex_procedure(run.id)
    spec = replace(
        spec, output_validator="tin-diagram.reviewed.v1", output_media_type="text/vnd.mermaid"
    )
    monkeypatch.setattr(
        activities, "_pinned_codex_procedure", AsyncMock(return_value=(definition, spec))
    )
    source = 'graph LR\n a["Start"]:::step\n b["Done"]:::receipt\n a --> b\n'
    storage.repo.trees[checkpoint.ephemeral_commit_sha][PATH] = ("100644", source.encode())
    check = AsyncMock(
        return_value={
            "checker": "tin-diagram-check.v1",
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "revision": checkpoint.ephemeral_commit_sha,
            "themes": ["light", "dark"],
        }
    )
    if not valid:
        check.side_effect = RuntimeError("visual check failed")
    monkeypatch.setattr(activities._sandboxes, "validate_diagram", check, raising=False)
    key = f"{run.id}:procedure_artifact_persist"
    await publication_db.pool.execute("DELETE FROM effect_receipts WHERE execution_key=$1", key)
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET retained_output=NULL WHERE id=$1", run.id
    )
    if not valid:
        with pytest.raises(RuntimeError, match="visual check failed"):
            await activities.persist_codex_procedure_artifact(str(run.id))
        assert storage.repo.writes == 0
    else:
        await activities.persist_codex_procedure_artifact(str(run.id))
        proof = await publication_db.get_effect(key)
        assert (
            proof.result["diagram_validation"]["source_sha256"]
            == hashlib.sha256(source.encode()).hexdigest()
        )
        await activities.commit_codex_procedure_artifact(str(run.id))
        assert storage.repo.writes == 1
        await activities.commit_codex_procedure_artifact(str(run.id))
        assert storage.repo.writes == 1
    check.assert_awaited_once_with(
        content=source.encode(), run_id=str(run.id), revision=checkpoint.ephemeral_commit_sha
    )
