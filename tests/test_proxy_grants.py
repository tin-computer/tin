from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from test_recovery import sandbox_input

from tin_lite.e2b_runtime import E2BRuntime, SandboxProcedureInput, SandboxTaskInput, _run_secrets
from tin_lite.proxy_grants import authorized, proxy_grant, validate_proxy_url
from tin_lite.settings import Settings


def protected_input(method):
    values = {**asdict(sandbox_input()), "proxy_url": "https://proxy.test:8888"}
    values.update(
        isolated=True, api_url="https://tin.test/relay", api_grant="synthetic", context={}
    )
    if method == "run_task_and_kill":
        return SandboxTaskInput(**values, run_id="run")
    return SandboxProcedureInput(**values, output_path="report.md", output_max_bytes=1000)


@pytest.mark.parametrize(
    "url",
    [
        "http://proxy.test:8888",
        "https://user:password@proxy.test:8888",
        "https://proxy.test/path",
        "https://proxy.test?password=secret",
        "https://proxy.test#fragment",
        "https://proxy.test:bad",
        "https:///",
    ],
)
def test_plaintext_and_credential_bearing_proxy_origins_are_rejected(url):
    with pytest.raises(ValueError):
        validate_proxy_url(url)


@pytest.mark.parametrize("directory", [None, "relative/grants", "/var/lib/tin-lite-proxy-grants"])
def test_settings_require_a_grant_directory_with_the_tls_proxy(monkeypatch, directory):
    # Never load the developer's .env or real credentials into a validation error.
    for field in Settings.model_fields.values():
        monkeypatch.delenv(field.alias, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    values = {
        field.alias: 5432 if name.endswith("port") else "synthetic"
        for name, field in Settings.model_fields.items()
        if field.is_required()
    }
    values["CLERK_PUBLISHABLE_KEY"] = "pk_test_synthetic"
    values.update(TIN_LITE_PROXY_URL="https://proxy.test:8888", TIN_LITE_PROXY_GRANT_DIR=directory)
    if directory is None or not directory.startswith("/"):
        with pytest.raises(ValueError, match="TIN_LITE_PROXY_GRANT_DIR"):
            Settings(_env_file=None, **values)
    else:
        assert Settings(_env_file=None, **values).proxy_grant_dir == Path(directory)


def test_grant_is_hashed_scoped_expires_and_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("tin_lite.proxy_grants.time.time", lambda: 1000)
    with proxy_grant(
        directory=tmp_path,
        proxy_url="https://proxy.test:8888",
        execution_key="run-1:turn-2",
        sandbox_id="sandbox-3",
        ttl_seconds=60,
    ) as url:
        parsed = urlsplit(url)
        assert authorized(tmp_path, parsed.username, parsed.password)
        paths = list(tmp_path.iterdir())
        assert len(paths) == 1
        assert paths[0].stat().st_mode & 0o777 == 0o640
        assert parsed.password not in paths[0].read_text()
        assert json.loads(paths[0].read_text()) == {
            "execution_key": "run-1:turn-2",
            "sandbox_id": "sandbox-3",
            "expires_at": 1060,
        }
        assert not authorized(tmp_path, "tinlite", parsed.password)
        assert not authorized(tmp_path, parsed.username, "../" + parsed.password)
        monkeypatch.setattr("tin_lite.proxy_grants.time.time", lambda: 1060)
        assert not authorized(tmp_path, parsed.username, parsed.password)
    assert not list(tmp_path.iterdir())
    assert not authorized(tmp_path, parsed.username, parsed.password)


def test_grant_covers_the_longest_procedure_sandbox(tmp_path):
    from tin_lite.procedures import MAX_SANDBOX_TIMEOUT_SECONDS
    from tin_lite.proxy_grants import MAX_TTL_SECONDS

    # The runtime asks for the sandbox timeout plus 60 seconds.
    assert MAX_TTL_SECONDS == MAX_SANDBOX_TIMEOUT_SECONDS + 60
    with proxy_grant(
        directory=tmp_path,
        proxy_url="https://proxy.test:8888",
        execution_key="run-1:long",
        sandbox_id="sandbox-1",
        ttl_seconds=MAX_TTL_SECONDS,
    ) as url:
        parsed = urlsplit(url)
        assert authorized(tmp_path, parsed.username, parsed.password)
    with (
        pytest.raises(ValueError, match="lifetime"),
        proxy_grant(
            directory=tmp_path,
            proxy_url="https://proxy.test:8888",
            execution_key="run-1:too-long",
            sandbox_id="sandbox-1",
            ttl_seconds=MAX_TTL_SECONDS + 1,
        ),
    ):
        pass


def test_helper_protocol_never_echoes_credentials(tmp_path):
    with proxy_grant(
        directory=tmp_path,
        proxy_url="https://proxy.test:8888",
        execution_key="run-1:stage",
        sandbox_id="sandbox-1",
        ttl_seconds=60,
    ) as url:
        parsed = urlsplit(url)
        result = subprocess.run(  # noqa: S603 — local helper and synthetic credentials
            [sys.executable, "src/tin_lite/proxy_grants.py", str(tmp_path)],
            input=f"{parsed.username} {parsed.password}\ninvalid\ntinlite old-password\n",
            capture_output=True,
            text=True,
            check=True,
        )
    assert result.stdout == "OK\nERR\nERR\n"
    assert not result.stderr


@pytest.mark.parametrize("method", ["run_task_and_kill", "run_procedure_and_kill"])
@pytest.mark.parametrize("failure", [None, RuntimeError("failed"), asyncio.CancelledError()])
async def test_every_runner_revokes_grants_on_success_failure_and_cancel(
    tmp_path, monkeypatch, method, failure
):
    runtime = E2BRuntime(
        api_key="synthetic",
        template="test",
        timeout_seconds=60,  # noqa: S106
        egress_allow_hosts=(),
        proxy_grant_dir=tmp_path,
    )
    original = protected_input(method)
    observed = []

    async def execute(*, sandbox_id, run_input, **kwargs):
        parsed = urlsplit(run_input.proxy_url)
        assert authorized(tmp_path, parsed.username, parsed.password)
        assert parsed.password in _run_secrets(run_input)
        assert runtime._run_env(sandbox_id=sandbox_id, run_input=run_input)["HTTPS_PROXY"] == (
            run_input.proxy_url
        )
        observed.append(parsed)
        if failure:
            raise failure
        return "result"

    monkeypatch.setattr(runtime, "_" + method, execute)
    if failure:
        with pytest.raises(type(failure)):
            await getattr(runtime, method)(sandbox_id="sandbox-1", run_input=original)
    else:
        assert (
            await getattr(runtime, method)(sandbox_id="sandbox-1", run_input=original) == "result"
        )
    assert len(observed) == 1
    assert not authorized(tmp_path, observed[0].username, observed[0].password)
    assert not list(tmp_path.iterdir())
    assert original.proxy_url == "https://proxy.test:8888"


@pytest.mark.parametrize("method", ["run_task_and_kill", "run_procedure_and_kill"])
async def test_grant_creation_failure_still_kills_the_sandbox(tmp_path, monkeypatch, method):
    runtime = E2BRuntime(
        api_key="synthetic",
        template="test",
        timeout_seconds=60,  # noqa: S106
        egress_allow_hosts=(),
        proxy_grant_dir=tmp_path / "missing",
    )
    killed = []

    async def kill(sandbox_id):
        killed.append(sandbox_id)

    monkeypatch.setattr(runtime, "kill", kill)
    with pytest.raises(FileNotFoundError):
        await getattr(runtime, method)(
            sandbox_id="sandbox-1",
            run_input=protected_input(method),
        )
    assert killed == ["sandbox-1"]


async def test_command_errors_cannot_expose_the_scoped_proxy_credential(tmp_path, monkeypatch):
    runtime = E2BRuntime(
        api_key="synthetic",
        template="test",
        timeout_seconds=60,  # noqa: S106
        egress_allow_hosts=(),
        proxy_grant_dir=tmp_path,
    )
    credentials = []

    async def execute(*, sandbox_id, run_input):
        credentials.append(urlsplit(run_input.proxy_url).password)
        raise RuntimeError(f"command echoed {run_input.proxy_url} and {credentials[0]}")

    method = "run_procedure_and_kill"
    monkeypatch.setattr(runtime, "_run_procedure_and_kill", execute)
    with pytest.raises(RuntimeError) as caught:
        await runtime.run_procedure_and_kill(
            sandbox_id="sandbox-1",
            run_input=protected_input(method),
        )
    assert "[redacted]" in str(caught.value)
    assert credentials[0] not in str(caught.value)
    assert caught.value.__suppress_context__
    assert caught.value.__cause__ is None
    failure = Failure()
    DefaultFailureConverter().to_failure(caught.value, DefaultPayloadConverter(), failure)
    assert credentials[0] not in str(failure)
    assert not list(tmp_path.iterdir())


def test_installer_fails_closed_and_only_replaces_its_firewall_table():
    installer = Path("infra/install_switchboard.sh").read_text()
    assert installer.index("systemctl disable --now tinyproxy") < installer.index("apt-get update")
    assert "systemctl mask squid.service" in installer
    assert "TIN_LITE_PROXY_URL=https://" in installer
    assert "TIN_LITE_PROXY_URL=http://" not in installer
    assert "ExecStartPre=/usr/bin/find /var/lib/tin-lite-proxy-grants -type f -delete" in installer
    service = Path("infra/install_forward_proxy.sh").read_text()
    assert "Requires=tin-lite-proxy-firewall.service" in service
    assert "User=proxy\nGroup=proxy" in service
    assert "CapabilityBoundingSet=" in service
    assert "flush ruleset" not in service
