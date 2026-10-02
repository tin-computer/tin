"""Codex procedures know their run ID, as code workflows do through ctx["run_id"].

content.blog_index, still a procedure, had no way to name its own run in a report or a
receipt. The brief now carries a RUN CONTEXT line, the sandbox controller gets TIN_RUN_ID,
and the isolated controller hands that one variable to the agent's commands.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from test_codex_isolation import load_sandbox_module
from test_rollouts import base_values

from tin_lite.e2b_runtime import E2BRuntime, SandboxProcedureInput
from tin_lite.procedures import PinnedCodexProcedure

RUN_ID = UUID("6f1c2b8e-3d4a-4e5f-9a0b-1c2d3e4f5a6b")


def spec() -> PinnedCodexProcedure:
    return PinnedCodexProcedure(
        workflow_key="content.blog_index",
        prompt="Build the blog index.",
        entry_skill="blog-index",
        skill_files={"blog-index/SKILL.md": b"---\nname: blog-index\n---\n"},
        output_path="reports/content/BLOG_INDEX.md",
    )


def test_the_brief_and_context_name_the_run():
    context = spec().sandbox_context(inputs={}, run_id=RUN_ID)
    assert context["run"] == {"id": str(RUN_ID)}
    assert f"RUN CONTEXT:\nThis run's Tin run ID is {RUN_ID}." in context["prompt"]
    assert "TIN_RUN_ID" in context["prompt"]
    assert context["prompt"].startswith("Build the blog index.")
    # A string ID is normalized; anything else is refused rather than passed to the agent.
    assert spec().sandbox_context(inputs={}, run_id=str(RUN_ID).upper())["run"] == {
        "id": str(RUN_ID)
    }
    with pytest.raises(ValueError):
        spec().sandbox_context(inputs={}, run_id="not-a-run")


def test_a_context_without_a_run_is_unchanged():
    context = spec().sandbox_context(inputs={})
    assert "run" not in context and context["prompt"] == "Build the blog index."


async def test_the_sandbox_controller_receives_tin_run_id(monkeypatch):
    commands = []
    payload = base64.b64encode(json.dumps({"summary": "done", "message": "ok"}).encode()).decode()
    output = f"TIN_PROCEDURE_COMMIT_SHA={'c' * 40}\nTIN_PROCEDURE_RESULT={payload}\n"

    async def command(cmd, **kwargs):
        commands.append((cmd, kwargs))
        if cmd.endswith(" check"):
            return SimpleNamespace(stdout="TIN_ISOLATION_READY_V1")
        if cmd.endswith("codex_api_config.py --check"):
            return SimpleNamespace(stdout="TIN_CODEX_API_READY_V1")
        return SimpleNamespace(wait=AsyncMock(return_value=SimpleNamespace(stdout=output)))

    sandbox = SimpleNamespace(
        commands=SimpleNamespace(run=command),
        files=SimpleNamespace(write=AsyncMock()),
        kill=AsyncMock(),
    )
    monkeypatch.setattr(
        "tin_lite.e2b_runtime.AsyncSandbox.connect", AsyncMock(return_value=sandbox)
    )
    runtime = E2BRuntime(
        api_key="synthetic", template="default", timeout_seconds=900, egress_allow_hosts=()
    )

    async def run(context):
        commands.clear()
        await runtime.run_procedure_and_kill(
            sandbox_id="sandbox",
            run_input=SandboxProcedureInput(
                **base_values(),
                context=context,
                output_path="report.md",
                output_max_bytes=1000,
                isolated=True,
                usage_sink=AsyncMock(),
                api_url="https://tin.test/relay",
                api_grant="synthetic-relay-grant",
            ),
        )
        (envs,) = [kw["envs"] for cmd, kw in commands if cmd == "/opt/tin-lite/run-procedure"]
        return envs

    envs = await run(spec().sandbox_context(inputs={}, run_id=RUN_ID))
    assert envs["TIN_RUN_ID"] == str(RUN_ID)
    # Contexts saved before this change have no run; the variable is simply absent.
    assert "TIN_RUN_ID" not in await run({})


def test_isolated_commands_get_only_a_valid_run_id(monkeypatch):
    bridge = load_sandbox_module("procedure_app_server")
    assert bridge._run_id_policy(str(RUN_ID)) == (
        f'shell_environment_policy.set.TIN_RUN_ID="{RUN_ID}"',
    )
    for value in ("", "x", f'{RUN_ID}"\nmodel="other"'):
        assert bridge._run_id_policy(value) == ()

    launched = {}

    class Stop(Exception):
        pass

    def popen(command, **kwargs):
        launched["command"], launched["env"] = command, kwargs["env"]
        raise Stop

    context = {
        "workflow_key": "content.blog_index",
        "prompt": "Build the blog index.",
        "entry_skill": "blog-index",
        "output": {"kind": "project.artifact", "path": "reports/content/BLOG_INDEX.md"},
        "inputs": {},
    }
    monkeypatch.setattr(bridge, "ISOLATED", True)
    monkeypatch.setattr(bridge, "_decode_context", lambda: context)
    monkeypatch.setattr(bridge, "_install_skills", lambda _context: None)
    monkeypatch.setattr(bridge, "_project_skill_names", lambda _context: [])
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setenv("TIN_RUN_ID", str(RUN_ID))
    with pytest.raises(Stop):
        bridge.execute()
    command = launched["command"]
    override = f'shell_environment_policy.set.TIN_RUN_ID="{RUN_ID}"'
    assert command[command.index(override) - 1] == "-c"
    assert 'shell_environment_policy.inherit="none"' in command
    assert launched["env"]["TIN_RUN_ID"] == str(RUN_ID)
