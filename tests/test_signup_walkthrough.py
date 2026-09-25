from __future__ import annotations

import ast
import base64
import importlib.util
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from tin_lite.activities import TinActivities
from tin_lite.domain import ProjectTestIdentity
from tin_lite.e2b_runtime import E2BRuntime, SandboxProcedureInput
from tin_lite.integrations import IntegrationAuthorizationError, IntegrationService
from tin_lite.procedures import (
    SIGNUP_WALKTHROUGH_VALIDATOR,
    PinnedCodexProcedure,
    SandboxProfile,
    TestIdentityPolicy,
    artifact_host,
    artifact_timestamp,
    signup_walkthrough_activation,
    validate_codex_procedure_definition,
    validate_procedure_artifact,
)

ROOT = Path(__file__).parents[1]


def _credential_key() -> str:
    return base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")


def _walkthrough_spec(**overrides) -> PinnedCodexProcedure:
    values = {
        "workflow_key": "qa.signup_walkthrough",
        "prompt": "Walk the signup.",
        "entry_skill": "signup-walkthrough",
        "skill_files": {"signup-walkthrough/SKILL.md": b"---\nname: signup-walkthrough\n---\n"},
        "output_path": "reports/qa/SIGNUP_WALKTHROUGH.md",
        "output_media_type": "text/markdown",
        "output_validator": SIGNUP_WALKTHROUGH_VALIDATOR,
        "sandbox": SandboxProfile(profile="browser", timeout_seconds=1800, egress="open"),
        "identity": TestIdentityPolicy(create=True),
    }
    values.update(overrides)
    return PinnedCodexProcedure(**values)


def _definition(**procedure_overrides) -> dict:
    procedure = {
        "prompt_path": "procedures/qa.signup_walkthrough/PROMPT.md",
        "skills_path": "procedures/qa.signup_walkthrough/skills",
        "skill_files": ["procedures/qa.signup_walkthrough/skills/signup-walkthrough/SKILL.md"],
        "entry_skill": "signup-walkthrough",
        "workspace": {"kind": "project.state"},
        "output": {
            "kind": "project.artifact",
            "path_template": "reports/qa/signup/{host}/{started_at}.md",
            "media_type": "text/markdown",
            "validator": SIGNUP_WALKTHROUGH_VALIDATOR,
            "max_bytes": 200_000,
        },
        "verification": {"commands": []},
        "project_skills": [],
        "sandbox": {"profile": "browser", "timeout_seconds": 1800, "egress": "open"},
        "identity": {"create": True},
    }
    procedure.update(procedure_overrides)
    return {"key": "qa.signup_walkthrough", "executor": "codex.procedure", "procedure": procedure}


def test_sandbox_profile_is_validated_and_defaults_stay_fenced() -> None:
    spec = validate_codex_procedure_definition(_definition())
    assert spec.output_path is None
    assert spec.output_path_template == "reports/qa/signup/{host}/{started_at}.md"
    assert spec.sandbox == SandboxProfile("browser", 1800, "open")
    assert spec.sandbox.browser and spec.sandbox.open_egress
    # A pinned definition from before identity reuse existed still loads as a creating policy.
    assert spec.identity == TestIdentityPolicy(create=True, reuse="none")
    assert spec.identity.enabled and bool(spec.identity)

    legacy = _definition()
    del legacy["procedure"]["sandbox"]
    del legacy["procedure"]["identity"]
    assert validate_codex_procedure_definition(legacy).sandbox == SandboxProfile()
    assert validate_codex_procedure_definition(legacy).identity == TestIdentityPolicy()
    assert not validate_codex_procedure_definition(legacy).identity.enabled
    reusing = validate_codex_procedure_definition(
        _definition(identity={"create": False, "reuse": "active"})
    ).identity
    assert reusing == TestIdentityPolicy(create=False, reuse="active") and reusing.enabled
    with pytest.raises(ValueError, match="identity reuse mode is unsupported"):
        validate_codex_procedure_definition(_definition(identity={"create": True, "reuse": "any"}))
    with pytest.raises(ValueError, match="identity contract is invalid"):
        validate_codex_procedure_definition(_definition(identity={"reuse": "active"}))

    with pytest.raises(ValueError, match="sandbox profile is unsupported"):
        validate_codex_procedure_definition(
            _definition(sandbox={"profile": "gpu", "timeout_seconds": 60, "egress": "open"})
        )
    with pytest.raises(ValueError, match="sandbox timeout"):
        validate_codex_procedure_definition(
            _definition(sandbox={"profile": "browser", "timeout_seconds": 3601, "egress": "open"})
        )
    with pytest.raises(ValueError, match="egress mode is unsupported"):
        validate_codex_procedure_definition(
            _definition(sandbox={"profile": "browser", "timeout_seconds": 60, "egress": "any"})
        )
    with pytest.raises(ValueError, match="identity contract is invalid"):
        validate_codex_procedure_definition(_definition(identity={"create": "yes"}))
    with pytest.raises(ValueError, match="validation requires a Markdown output"):
        validate_codex_procedure_definition(
            _definition(
                output={
                    "kind": "project.artifact",
                    "path": "reports/qa/walk.txt",
                    "media_type": "text/plain",
                    "validator": SIGNUP_WALKTHROUGH_VALIDATOR,
                    "max_bytes": 1000,
                }
            )
        )
    for template, reason in (
        ("reports/qa/signup/{host}.md", "output path template is invalid"),
        ("reports/qa/signup/{slug}.md", "use {host} and {started_at}"),
        ("../{host}/{started_at}.md", "output path template is unsafe"),
    ):
        with pytest.raises(ValueError, match=reason):
            validate_codex_procedure_definition(
                _definition(
                    output={
                        "kind": "project.artifact",
                        "path_template": template,
                        "media_type": "text/markdown",
                        "validator": SIGNUP_WALKTHROUGH_VALIDATOR,
                        "max_bytes": 1000,
                    }
                )
            )


def test_walkthrough_output_path_is_one_report_per_host_and_run() -> None:
    template = _walkthrough_spec(
        output_path=None, output_path_template="reports/qa/signup/{host}/{started_at}.md"
    )
    started = datetime(2026, 9, 4, 14, 5, 19, 935988, tzinfo=UTC)
    inputs = {"product_url": "https://WWW.Example.com/start?ref=1"}
    resolved = template.resolve_inputs(inputs, started_at=started)
    assert resolved.output_path == "reports/qa/signup/www.example.com/2026-09-04T14-05-19Z.md"
    assert resolved.output_path_template is None
    # A retried attempt resolves the identical path, and the run clock is normalized to UTC.
    local = started.astimezone(timezone(timedelta(hours=-7)))
    assert template.resolve_inputs(inputs, started_at=local).output_path == resolved.output_path
    assert artifact_host("https://tin.computer/") == "tin.computer"
    assert artifact_timestamp(started) == "2026-09-04T14-05-19Z"

    with pytest.raises(ValueError, match="requires a product_url"):
        template.resolve_inputs({}, started_at=started)
    with pytest.raises(ValueError, match="output host is invalid"):
        template.resolve_inputs({"product_url": "https://bad_host/"}, started_at=started)
    with pytest.raises(ValueError, match="requires the run creation time"):
        template.resolve_inputs(inputs)
    with pytest.raises(ValueError, match="timezone-aware"):
        template.resolve_inputs(inputs, started_at=started.replace(tzinfo=None))


def test_walkthrough_report_requires_activation_frontmatter() -> None:
    spec = _walkthrough_spec()
    good = b"---\nactivation_reached: true\nwalked_at: 2026-09-03\n---\n# Walk\n\nReached.\n"
    validate_procedure_artifact(good, spec=spec)
    assert signup_walkthrough_activation(good.decode()) is True
    crlf = b"---\r\nactivation_reached: false\r\n---\r\n# Walk\r\nBlocked at CAPTCHA.\r\n"
    assert signup_walkthrough_activation(crlf.decode()) is False

    for bad, reason in (
        (b"# Walk\n", "start with a frontmatter"),
        (b"---\nactivation_reached: true\n# Walk\n", "not closed"),
        (b"---\nreached: true\n---\n# Walk\n", "must declare activation_reached"),
        (b"---\nactivation_reached: maybe\n---\n# Walk\n", "must be true or false"),
        (b"---\nactivation_reached: true\nactivation_reached: false\n---\n# Walk\n", "duplicate"),
        (b"---\nactivation_reached: true\n---\n\n", "body is empty"),
    ):
        with pytest.raises(ValueError, match=reason):
            validate_procedure_artifact(bad, spec=spec)


def test_sandbox_context_carries_profile_and_identity_outside_inputs() -> None:
    spec = _walkthrough_spec()
    context = spec.sandbox_context(
        inputs={"project_id": "p", "product_url": "https://app.example.com/"},
        identity={"identity_id": uuid4(), "email": "founder+tin-abc@ex.com", "password": "pw"},
    )
    assert context["sandbox"] == {"profile": "browser", "timeout_seconds": 1800, "egress": "open"}
    assert context["identity"]["email"] == "founder+tin-abc@ex.com"
    assert context["identity"]["mode"] == "created"
    assert "identity" not in context["inputs"]
    assert context["inputs"] == {"product_url": "https://app.example.com/"}
    reused = spec.sandbox_context(
        inputs={"project_id": "p"},
        identity={"identity_id": "i", "email": "e@x", "password": "p", "mode": "reused"},
    )
    assert reused["identity"]["mode"] == "reused"
    assert "phone" not in reused["identity"]
    with_phone = spec.sandbox_context(
        inputs={"project_id": "p"},
        identity={"identity_id": "i", "email": "e@x", "password": "p", "phone": "+15596662364"},
    )
    assert with_phone["identity"]["phone"] == "+15596662364"
    with pytest.raises(ValueError, match="phone is not E.164"):
        spec.sandbox_context(
            inputs={"project_id": "p"},
            identity={"identity_id": "i", "email": "e@x", "password": "p", "phone": "559-666"},
        )

    with pytest.raises(ValueError, match="requires a test identity"):
        spec.sandbox_context(inputs={})
    with pytest.raises(ValueError, match="does not declare a test identity"):
        _walkthrough_spec(identity=TestIdentityPolicy()).sandbox_context(
            inputs={}, identity={"identity_id": "i", "email": "e", "password": "p"}
        )
    with pytest.raises(ValueError, match="identity mode is invalid"):
        spec.sandbox_context(
            inputs={}, identity={"identity_id": "i", "email": "e", "password": "p", "mode": "x"}
        )


@pytest.mark.asyncio
async def test_browser_profile_selects_template_timeout_and_open_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict = {}

    class Paginator:
        async def next_items(self, **_kwargs):
            return []

    async def create(template, **kwargs):
        created["template"] = template
        created.update(kwargs)
        return SimpleNamespace(sandbox_id="sbx-browser")

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.list", lambda *a, **k: Paginator())
    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.create", create)
    runtime = E2BRuntime(
        api_key="e2b-test",
        template="tin-lite-codex",
        browser_template="tin-lite-codex-browser",
        timeout_seconds=900,
        egress_allow_hosts=("api.tin.test",),
    )
    profile = SandboxProfile("browser", 1800, "open")
    assert await runtime.create(execution_key="run:create", run_id="run", profile=profile) == (
        "sbx-browser"
    )
    assert created["template"] == "tin-lite-codex-browser"
    assert created["timeout"] == 1800
    assert created["network"] is None
    assert created["metadata"]["profile"] == "browser"

    await runtime.create(execution_key="run:create", run_id="run")
    assert created["template"] == "tin-lite-codex"
    assert created["timeout"] == 900
    assert created["network"]["allow_out"] == ["api.tin.test"]

    with pytest.raises(RuntimeError, match="browser template"):
        await E2BRuntime(
            api_key="k", template="tin-lite-codex", timeout_seconds=9, egress_allow_hosts=()
        ).create(execution_key="x", run_id="r", profile=profile)


@pytest.mark.asyncio
async def test_runtime_recovery_refuses_a_sandbox_built_from_another_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Paginator:
        async def next_items(self, **_kwargs):
            return [SimpleNamespace(sandbox_id="old", metadata={"template": "tin-lite-codex"})]

    class Sandbox:
        async def is_running(self):
            return True

    async def connect(*_args, **_kwargs):
        return Sandbox()

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.list", lambda *a, **k: Paginator())
    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    runtime = E2BRuntime(
        api_key="k",
        template="tin-lite-codex",
        browser_template="tin-lite-codex-browser",
        timeout_seconds=9,
        egress_allow_hosts=(),
    )
    assert await runtime._find("run:create") == "old"
    with pytest.raises(RuntimeError, match="different template"):
        await runtime.create(
            execution_key="run:create", run_id="r", profile=SandboxProfile("browser", 60, "open")
        )


@pytest.mark.asyncio
async def test_procedure_run_exports_browser_flag_timeout_and_redacts_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = base64.b64encode(
        json.dumps({"summary": "Signed up with pw-secret-123.", "message": "ok"}).encode()
    ).decode()
    seen: dict = {}

    class Handle:
        async def wait(self):
            return SimpleNamespace(
                stdout=f"TIN_PROCEDURE_COMMIT_SHA={'c' * 40}\nTIN_PROCEDURE_RESULT={payload}\n"
            )

    class Commands:
        async def run(self, *args, **kwargs):
            if args[0].endswith("isolated-procedure check"):
                return SimpleNamespace(stdout="TIN_ISOLATION_READY_V1")
            if args[0].endswith("codex_api_config.py --check"):
                return SimpleNamespace(stdout="TIN_CODEX_API_READY_V1")
            seen.update(kwargs)
            return Handle()

    class Sandbox:
        commands = Commands()
        killed = False

        class Files:
            async def write(self, path, data):
                pass

        files = Files()

        async def kill(self):
            self.killed = True

    sandbox = Sandbox()

    async def connect(_sandbox_id, **kwargs):
        seen["connect_timeout"] = kwargs["timeout"]
        return sandbox

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    runtime = E2BRuntime(
        api_key="k", template="tin-lite-codex", timeout_seconds=900, egress_allow_hosts=()
    )
    result = await runtime.run_procedure_and_kill(
        sandbox_id="sbx",
        run_input=SandboxProcedureInput(
            execution_key="run:persist",
            isolated=True,
            usage_sink=AsyncMock(),
            api_url="https://tin.test/relay",
            api_grant="synthetic",
            canonical_url="https://storage/canonical",
            canonical_auth_header="Authorization: Bearer c",
            canonical_branch="main",
            ephemeral_url="https://storage/ephemeral",
            ephemeral_auth_header="Authorization: Bearer e",
            ephemeral_branch="procedures/run/1",
            proxy_url=None,
            no_proxy="tin.test",
            context={"identity": {"password": "pw-secret-123"}},
            output_path="reports/qa/SIGNUP_WALKTHROUGH.md",
            output_max_bytes=200_000,
            browser=True,
            timeout_seconds=1800,
            redact=("pw-secret-123",),
        ),
    )
    assert seen["connect_timeout"] == 1800
    assert seen["timeout"] == 1800
    assert seen["envs"]["TIN_PROCEDURE_BROWSER"] == "1"
    assert result.summary == "Signed up with [redacted]."
    assert sandbox.killed is True


def test_browser_template_pins_camoufox_warp_and_the_browser_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "tin_lite_sandbox_template_browser", ROOT / "sandbox" / "template.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[tuple[str, object]] = []

    class Builder:
        def __init__(self, *, file_context_path):
            assert file_context_path == ROOT

        def __getattr__(self, name):
            def record(*args, **kwargs):
                calls.append((name, args))
                return self

            return record

    monkeypatch.setattr(module, "Template", Builder)
    module.browser_template()
    assert calls[:2] == [("from_template", ("codex",)), ("pip_install", ("uv==0.5.21",))]
    copied = {args[:2] for name, args in calls if name == "copy"}
    assert ("sandbox/warp_up.sh", "/opt/tin-lite/warp-up") in copied
    assert ("sandbox/camoufox_mcp.py", "/opt/tin-lite/camoufox-mcp") in copied
    commands = "\n".join(str(args[0]) for name, args in calls if name == "run_cmd")
    assert "cloudflare-warp=2026.7.1377.0" in commands
    assert "'camoufox[geoip]==0.5.5' 'mcp==2.1.1'" in commands
    assert "camoufox fetch official/152.0.4-beta.29" in commands
    assert "download_mmdb()" in commands
    assert "camoufox version" in commands
    assert "/opt/tin-lite/camoufox-mcp --check" in commands
    everything = str(calls)
    assert "npm_install" not in everything
    assert "google-chrome" not in everything and "chrome-devtools" not in everything
    assert module.TEMPLATES["tin-lite-codex-browser"][2] > module.TEMPLATES["tin-lite-codex"][2]


def test_sandbox_scripts_wire_the_camoufox_mcp_and_guard_the_identity_secret() -> None:
    runner = (ROOT / "sandbox" / "run_procedure.sh").read_text()
    bridge = (ROOT / "sandbox" / "procedure_app_server.py").read_text()
    assert 'TIN_PROCEDURE_BROWSER:-}" == "1"' in runner
    assert "/opt/tin-lite/warp-up /home/user/.tin-lite/browser" in runner
    assert "codex mcp add camoufox" in runner
    assert "--env TIN_BROWSER_PROFILE_DIR=/home/user/.tin-lite/browser/profile" in runner
    assert "cd /home/user/.tin-lite/browser && exec" in runner
    assert "/opt/tin-lite/cfx-venv/bin/python /opt/tin-lite/camoufox-mcp" in runner
    # Codex's default 10-second MCP handshake limit dropped the server in the first live run.
    assert "startup_timeout_sec = 60" in runner and "tool_timeout_sec = 180" in runner
    assert "chrome" not in runner
    assert "outside the procedure output contract:" in runner
    # A new output directory must be listed file by file, or `reports/qa/` masks the artifact.
    assert "status --porcelain --untracked-files=all | cut -c4-" in runner
    assert "procedure output contains the test identity secret" in runner
    assert "TIN_PROCEDURE_BROWSER" in bridge
    assert "mounted the `camoufox` MCP server" in bridge
    assert "only browser: use `navigate`" in bridge
    assert "Do not write helper scripts" in bridge
    # The shared harness permits the capture package's images; the signup procedure
    # retains its own restriction. A global ban silently prevented live brand evidence.
    assert "Do not take screenshots" not in bridge
    assert "`set_viewport` and `screenshot` when the procedure permits it" in bridge
    assert "chrome" not in bridge
    skill = (
        ROOT
        / "codex_procedures"
        / "qa.signup_walkthrough"
        / "skills"
        / "signup-walkthrough"
        / "SKILL.md"
    ).read_text()
    assert "`camoufox.navigate(url)`" in skill
    assert "`camoufox.click_turnstile()`" in skill
    assert "Never write helper scripts" in skill
    assert "never reload or re-navigate" in skill
    assert "python" not in skill.lower()
    assert "TEST IDENTITY (Tin-owned, created for this run)" in bridge
    assert "TEST IDENTITY (Tin-owned, registered on this product by an earlier Tin run)" in bridge
    assert "do not sign up again" in bridge
    assert "password into the report or any other file" in bridge
    assert "record_test_identity_status" in bridge
    assert "`tin-run.search_sms`" in bridge and "receives SMS only" in bridge
    assert "never write this one" in bridge
    assert "`tin-run.search_sms`" in skill and "never write the number into the report" in skill
    assert "TIN_PROCEDURE_STATE_DIR" in bridge and "TIN_PROCEDURE_STATE_DIR" in runner
    assert 'TIN_PROCEDURE_RESULT_KIND="${result_kind}"' in runner


def test_warp_up_script_registers_then_connects() -> None:
    script = (ROOT / "sandbox" / "warp_up.sh").read_text()
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert "warp-cli --accept-tos registration new" in script
    assert "warp-cli --accept-tos mode proxy" in script
    assert "warp-cli --accept-tos connect" in script
    assert "grep -q Connected" in script
    assert script.index("registration new") < script.index("mode proxy") < script.index("connect")
    assert "exit 69" in script and "exit 71" in script
    assert script.rstrip().endswith('echo "WARP_OK=true"')


def test_camoufox_mcp_exposes_bounded_browser_tools_without_file_access() -> None:
    source = (ROOT / "sandbox" / "camoufox_mcp.py").read_text()
    tree = ast.parse(source)
    declared: tuple[str, ...] = ()
    registered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TOOL_NAMES" for target in node.targets
        ):
            declared = tuple(ast.literal_eval(node.value))
        if isinstance(node, ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                ):
                    registered.add(node.name)
    assert registered == set(declared) and len(declared) == len(set(declared))
    assert {"set_viewport", "screenshot"} <= registered
    assert not {name for name in registered if "download" in name}
    assert not {name for name in registered if "write" in name or "save" in name}
    assert "MAX_TEXT_CHARS = 20_000" in source
    assert '"persistent_context": True' in source
    assert "async def _launch()" in source and "_LAUNCH_LOCK" in source
    assert '"exclude_addons": [DefaultAddons.UBO]' in source
    assert 'DEFAULT_PROXY = "socks5://127.0.0.1:40000"' in source
    assert 'if parts.scheme not in {"http", "https"}' in source


def test_test_identity_password_is_sealed_under_its_own_context() -> None:
    settings = SimpleNamespace(integration_credential_key=SecretStr(_credential_key()))
    service = IntegrationService(database=object(), settings=settings, client=object())
    project_id = uuid4()
    identity_id = uuid4()
    ciphertext, version = service.seal_test_identity_password(
        project_id=project_id,
        identity_id=identity_id,
        password="pw-secret",  # noqa: S106
    )
    assert version == "v1"
    assert b"pw-secret" not in ciphertext

    def identity(**overrides) -> ProjectTestIdentity:
        values = dict(
            id=identity_id,
            project_id=project_id,
            created_by_run_id=uuid4(),
            target_host="app.example.com",
            label="walk",
            email="founder+tin-abc@example.com",
            auth_kind="password",
            username=None,
            password_ciphertext=ciphertext,
            credential_key_version=version,
            status="pending",
            status_note=None,
            verified_at=None,
            last_used_run_id=None,
            last_used_at=None,
            notes=None,
            created_at=None,
            updated_at=None,
        )
        values.update(overrides)
        return ProjectTestIdentity(**values)

    assert service.open_test_identity_password(identity()) == "pw-secret"
    with pytest.raises(IntegrationAuthorizationError):
        service.open_test_identity_password(identity(id=uuid4()))


@pytest.mark.asyncio
async def test_identity_is_minted_once_per_run_with_a_plus_alias() -> None:
    run_id = uuid4()
    project_id = uuid4()
    settings = SimpleNamespace(integration_credential_key=SecretStr(_credential_key()))
    integrations = IntegrationService(database=object(), settings=settings, client=object())
    created: list[dict] = []

    class Database:
        rows: dict = {}

        async def get_test_identity_for_run(self, *, run_id):
            return self.rows.get(run_id)

        async def get_test_identity_use_mode(self, *, run_id):
            return "created" if run_id in self.rows else None

        async def get_integration_connection(self, *, project_id, provider_key):
            assert provider_key == "workspace.google"
            return SimpleNamespace(
                id=uuid4(),
                status="connected",
                external_account_id="sub-1",
                configuration={
                    "email": "founder@example.com",
                    "granted_capabilities": ["gmail.messages.read"],
                },
            )

        async def create_test_identity(self, **values):
            created.append(values)
            row = ProjectTestIdentity(
                id=values["identity_id"],
                project_id=values["project_id"],
                created_by_run_id=values["run_id"],
                target_host=values["target_host"],
                label=values["label"],
                email=values["email"],
                auth_kind=values["auth_kind"],
                username=None,
                password_ciphertext=values["password_ciphertext"],
                credential_key_version=values["credential_key_version"],
                status="pending",
                status_note=None,
                verified_at=None,
                last_used_run_id=None,
                last_used_at=None,
                notes=None,
                created_at=None,
                updated_at=None,
            )
            self.rows[values["run_id"]] = row
            return row

    database = Database()
    activities = TinActivities(
        database=database,
        storage=object(),
        sandboxes=object(),
        settings=SimpleNamespace(),
        integrations=integrations,
    )
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        input={"product_url": "https://App.Example.com/start?ref=1"},
    )
    procedure = _walkthrough_spec()

    first, first_mode = await activities._resolve_test_identity(run=run, procedure=procedure)
    second, second_mode = await activities._resolve_test_identity(run=run, procedure=procedure)

    assert first is second
    assert (first_mode, second_mode) == ("created", "created")
    assert len(created) == 1
    assert first.email == f"founder+tin-{first.id.hex[:8]}@example.com"
    assert first.target_host == "app.example.com"
    password = integrations.open_test_identity_password(first)
    assert len(password) >= 20 and password not in first.email

    with pytest.raises(RuntimeError, match="contains the test identity secret"):
        await activities._reject_identity_leak(
            run_id=run_id, content=f"---\nactivation_reached: true\n---\n{password}\n".encode()
        )
    await activities._reject_identity_leak(run_id=run_id, content=b"clean report")
