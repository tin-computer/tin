from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from e2b import (
    AsyncCommandHandle,
    AsyncSandbox,
    FileType,
    NotFoundException,
    SandboxNotFoundException,
    SandboxQuery,
)

from tin_lite.procedures import SandboxProfile
from tin_lite.proxy_grants import proxy_grant
from tin_lite.rollouts import (
    RolloutCapture,
    RolloutFile,
    parse_rollout_filename,
    redact_rollout,
    redact_text,
    sandbox_secrets,
)
from tin_lite.usage_capture import observe_sandbox

logger = logging.getLogger(__name__)

CONTEXT_PATH = "/home/user/.tin-lite/procedure-context.json"
CONTEXT_ENV_MAX = 96 * 1024
"""Largest base64 context still passed as a variable, under the 128 KiB per-string cap."""

RolloutSink = Callable[[RolloutCapture], Awaitable[None]]
CodexUsageSink = Callable[[dict[str, Any]], Awaitable[None]]

# Codex writes one rollout per thread here; CODEX_HOME is unset in every runner, so the
# sandbox user's default home applies. The switchboard reads this tree before killing the
# sandbox, on success and failure alike, within the bounds below so capture can never
# hold a sandbox open for long or fail a run.
CODEX_SESSIONS_DIR = "/home/user/.codex/sessions"

# Only executed after the root-owned isolation helper has frozen the author.
# Compare to the checkout itself so an unchanged old report is not a new draft.
_INTERRUPTED_OUTPUT_READER = r"""
import base64, os, stat, subprocess, sys
from pathlib import Path
relative, limit, *roots = sys.argv[1:]
limit = int(limit)
for root in roots:
    path = Path(root) / relative
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
            continue
        with open(path, 'rb') as stream:
            content = stream.read(limit + 1)
        content.decode('utf-8')
        if len(content) > limit:
            continue
        head = subprocess.run(['git', '-C', root, 'rev-parse', '--verify', 'HEAD'],
                              capture_output=True, timeout=3)
        if head.returncode:
            continue
        previous = subprocess.run(['git', '-C', root, 'rev-parse', '--verify', 'HEAD:' + relative],
                                  capture_output=True, timeout=3)
        if previous.returncode == 0:
            current = subprocess.run(['git', '-C', root, 'hash-object', '--stdin'], input=content,
                                     capture_output=True, timeout=3, check=True)
            if previous.stdout == current.stdout:
                continue
        print(base64.b64encode(content).decode())
        break
    except (OSError, UnicodeError, subprocess.SubprocessError):
        continue
"""
ROLLOUT_FILE_MAX_BYTES = 16 * 1024 * 1024
ROLLOUT_TOTAL_MAX_BYTES = 48 * 1024 * 1024
ROLLOUT_MAX_FILES = 32
ROLLOUT_MAX_DIR_LISTS = 32
ROLLOUT_MAX_DEPTH = 5
ROLLOUT_LIST_TIMEOUT_SECONDS = 20.0
ROLLOUT_READ_TIMEOUT_SECONDS = 45.0
ROLLOUT_CAPTURE_BUDGET_SECONDS = 90.0
# Controller narration frames; the dashboard stores a shorter projection.
PROGRESS_MAX_CHARS = 1000


@dataclass(frozen=True, kw_only=True)
class SandboxRunInput:
    execution_key: str
    canonical_url: str
    canonical_auth_header: str
    canonical_branch: str
    ephemeral_url: str
    ephemeral_auth_header: str
    ephemeral_branch: str
    proxy_url: str | None
    no_proxy: str
    redact: tuple[str, ...] = ()
    rollout_sink: RolloutSink | None = None


@dataclass(frozen=True)
class SandboxTaskInput(SandboxRunInput):
    run_id: str
    context: dict[str, Any]
    context_delivery_ids: tuple[str, ...] = ()
    isolated: bool = False
    timeout_seconds: int = 900
    api_url: str | None = None
    api_contract: dict | None = None
    api_grant: str | None = None
    usage_sink: CodexUsageSink | None = None


@dataclass(frozen=True)
class SandboxProcedureInput(SandboxRunInput):
    context: dict[str, Any]
    output_path: str
    output_max_bytes: int
    run_tools_url: str | None = None
    run_tools_grant: str | None = None
    workspace_archive: bytes | None = None
    workspace_evidence: bytes | None = None
    browser: bool = False
    studio: bool = False
    timeout_seconds: int | None = None
    result_kind: str = "project.artifact"
    isolated: bool = False
    usage_sink: CodexUsageSink | None = None
    api_url: str | None = None
    api_contract: dict | None = None
    api_grant: str | None = None
    project_revision: str | None = None
    failure_sink: Callable[[BaseException], Awaitable[None]] | None = None
    interrupted_output_sink: Callable[[bytes], Awaitable[None]] | None = None
    progress_sink: Callable[[str], Awaitable[None]] | None = None


@dataclass(frozen=True)
class SandboxTaskResult:
    outcome: str
    summary: str
    message: str
    question: str | None
    has_changes: bool
    delivered_entry_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SandboxTaskEvent:
    sequence: int
    kind: str
    message: str


@dataclass(frozen=True)
class SandboxProcedureResult:
    ephemeral_commit_sha: str
    summary: str
    message: str
    diagram_review: dict | None = None


class E2BRuntime:
    def __init__(
        self,
        *,
        api_key: str,
        template: str,
        timeout_seconds: int,
        egress_allow_hosts: tuple[str, ...],
        browser_template: str | None = None,
        studio_template: str | None = None,
        isolated_template: str | None = None,
        browser_api_template: str = "tin-lite-codex-browser-api",
        studio_api_template: str = "tin-lite-codex-studio-api",
        usage_database=None,
        proxy_grant_dir: Path | None = None,
    ) -> None:
        self._proxy_grant_dir = proxy_grant_dir
        self._api_key = api_key
        self._usage_database = usage_database
        self._studio_template = studio_template
        self._isolated_template = isolated_template
        self._template = template
        self._browser_template = browser_template
        self._browser_api_template = browser_api_template
        self._studio_api_template = studio_api_template
        self._timeout_seconds = timeout_seconds
        self._egress_allow_hosts = egress_allow_hosts
        self._task_handles: dict[str, AsyncCommandHandle] = {}

    def template_for(self, profile: SandboxProfile | None) -> str:
        if profile is not None and profile.profile == "studio_api":
            return self._studio_api_template
        if profile is not None and profile.profile == "browser_api":
            return self._browser_api_template
        if profile is not None and profile.isolated:
            if not self._isolated_template:
                raise RuntimeError("isolated sandbox profile is not configured")
            return self._isolated_template
        if profile is not None and profile.studio:
            if not self._studio_template:
                raise RuntimeError("studio sandbox profile requires a studio template alias")
            return self._studio_template
        if profile is None or not profile.browser:
            return self._template
        if not self._browser_template:
            raise RuntimeError("browser sandbox profile requires a browser template alias")
        return self._browser_template

    async def create(
        self,
        *,
        execution_key: str,
        run_id: str,
        profile: SandboxProfile | None = None,
        code_only: bool = False,
    ) -> str:
        template = self.template_for(profile)
        open_egress = profile is not None and profile.open_egress
        if (
            profile is not None
            and profile.isolated
            and not profile.browser
            and not profile.studio
            and (open_egress or (not code_only and not self._egress_allow_hosts))
        ):
            raise RuntimeError("isolated sandbox profile requires an explicit egress fence")
        existing = await self._find(execution_key, template=template)
        if existing is not None:
            return existing
        timeout = profile.timeout_seconds if profile is not None else self._timeout_seconds
        network = None
        if self._egress_allow_hosts and not open_egress:
            network = {
                "allow_out": list(self._egress_allow_hosts),
                "deny_out": lambda context: [context.all_traffic],
            }
        if code_only:
            if profile is None or not profile.isolated or not 1 <= timeout <= 60:
                raise ValueError("code execution requires a bounded isolated profile")
            network = {"deny_out": lambda context: [context.all_traffic]}
        metadata = {"execution_key": execution_key, "run_id": run_id, "template": template}
        if profile is not None:
            metadata["profile"] = profile.profile
        sandbox = await AsyncSandbox.create(
            template,
            timeout=timeout,
            metadata=metadata,
            lifecycle={"on_timeout": "pause", "auto_resume": False},
            network=network,
            api_key=self._api_key,
        )
        try:
            await self._observe_sandbox(sandbox)
        except asyncio.CancelledError:
            await sandbox.kill()
            raise
        return sandbox.sandbox_id

    async def run_code_and_kill(self, *, sandbox_id: str, packet: dict, model_call=None) -> bytes:
        from pathlib import Path

        from tin_lite.code_models import CodeModelError
        from tin_lite.code_services import CodeServiceError

        sandbox = None
        try:
            sandbox = await AsyncSandbox.connect(
                sandbox_id, timeout=packet["timeout_seconds"] + 30, api_key=self._api_key
            )
            await sandbox.commands.run("mkdir -m 700 -p /root/tin-code", user="root", timeout=10)
            await sandbox.files.write(
                "/root/tin-code/runner.py",
                Path(__file__).with_name("code_runner.py").read_bytes(),
                user="root",
            )
            await sandbox.files.write("/root/tin-code/packet.json", json.dumps(packet), user="root")
            buffer = ""

            async def model_notice(chunk):
                nonlocal buffer
                buffer += chunk
                if len(buffer) > 128:
                    raise RuntimeError("Invalid code control notification.")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line != "TIN_MODEL_REQUEST" or model_call is None:
                        raise RuntimeError("Invalid code control notification.")
                    raw = await sandbox.files.read(
                        "/root/tin-code/request.json", format="bytes", user="root"
                    )
                    if len(raw) > 128_000:
                        raise CodeModelError("invalid_model_request")
                    try:
                        response = {"result": await model_call(json.loads(raw))}
                    except CodeModelError as exc:
                        if exc.code != "model_output_invalid":
                            raise
                        # A schema rejection may be handled by authored code. A corrective
                        # request needs a different step and another declared allowance.
                        response = {"error": exc.code}
                    await sandbox.files.write(
                        "/root/tin-code/response.tmp",
                        json.dumps(response, ensure_ascii=False),
                        user="root",
                    )
                    await sandbox.files.rename(
                        "/root/tin-code/response.tmp", "/root/tin-code/response.json", user="root"
                    )

            await sandbox.commands.run(
                "/opt/tin-lite/metadata-venv/bin/python -I -S /root/tin-code/runner.py",
                user="root",
                timeout=packet["timeout_seconds"] + 20,
                on_stdout=model_notice if model_call is not None else None,
            )
            raw = await sandbox.files.read(
                "/root/tin-code/result.json", format="bytes", user="root"
            )
            return bytes(raw)
        except asyncio.CancelledError:
            raise
        except (CodeModelError, CodeServiceError):
            raise
        except Exception:
            raise RuntimeError("Code workflow failed or exceeded its execution limits.") from None
        finally:
            if sandbox is not None:
                await self._delete_sandbox(sandbox)
            else:
                await self.kill(sandbox_id)

    @asynccontextmanager
    async def _proxy_input(self, *, sandbox_id: str, run_input):
        if self._proxy_grant_dir is None or run_input.proxy_url is None:
            yield run_input
            return
        timeout = getattr(run_input, "timeout_seconds", None) or self._timeout_seconds
        with ExitStack() as grants:
            try:
                url = grants.enter_context(
                    proxy_grant(
                        directory=self._proxy_grant_dir,
                        proxy_url=run_input.proxy_url,
                        execution_key=run_input.execution_key,
                        sandbox_id=sandbox_id,
                        ttl_seconds=timeout + 60,
                    )
                )
            except BaseException:
                # No command has started, but the activity already created its
                # sandbox. A grant/transport configuration failure must still kill it.
                await self.kill(sandbox_id)
                raise
            scoped = replace(run_input, proxy_url=url)
            try:
                yield scoped
            except Exception as exc:
                # SDK command errors can include echoed environment values. The
                # activity cannot redact a credential created only in this scope.
                message = _redact(str(exc), _run_secrets(scoped))
                if message != str(exc):
                    raise RuntimeError(f"{type(exc).__name__}: {message}") from None
                raise

    async def run_and_kill(self, *, sandbox_id: str, run_input: SandboxRunInput) -> str:
        await self.kill(sandbox_id)
        raise ValueError("The legacy pooled OAuth design runner is disabled")

    async def run_task_and_kill(
        self,
        *,
        sandbox_id: str,
        run_input: SandboxTaskInput,
        on_event: Callable[[SandboxTaskEvent], Awaitable[None]] | None = None,
    ) -> SandboxTaskResult:
        if not run_input.isolated or not run_input.api_url or not run_input.api_grant:
            await self.kill(sandbox_id)
            raise ValueError("Tasks require protected Codex API execution")
        async with self._proxy_input(sandbox_id=sandbox_id, run_input=run_input) as scoped:
            return await self._run_task_and_kill(
                sandbox_id=sandbox_id, run_input=scoped, on_event=on_event
            )

    async def run_procedure_and_kill(
        self, *, sandbox_id: str, run_input: SandboxProcedureInput
    ) -> SandboxProcedureResult:
        if not run_input.isolated or not run_input.api_url or not run_input.api_grant:
            await self.kill(sandbox_id)
            raise ValueError("Codex procedures require a protected controller")
        async with self._proxy_input(sandbox_id=sandbox_id, run_input=run_input) as scoped:
            return await self._run_procedure_and_kill(sandbox_id=sandbox_id, run_input=scoped)

    async def _run_task_and_kill(
        self,
        *,
        sandbox_id: str,
        run_input: SandboxTaskInput,
        on_event: Callable[[SandboxTaskEvent], Awaitable[None]] | None = None,
    ) -> SandboxTaskResult:
        sandbox = await AsyncSandbox.connect(
            sandbox_id,
            timeout=self._timeout_seconds,
            api_key=self._api_key,
        )
        envs = self._run_env(sandbox_id=sandbox_id, run_input=run_input)
        context = json.dumps(run_input.context, separators=(",", ":")).encode()
        envs["TIN_TASK_CONTEXT_B64"] = base64.b64encode(context).decode()
        if run_input.isolated:
            envs["TIN_PROCEDURE_ISOLATED"] = "1"
        secrets = _run_secrets(run_input)
        stdout_buffer = ""

        async def receive_stdout(chunk: str) -> None:
            nonlocal stdout_buffer
            stdout_buffer += chunk
            while "\n" in stdout_buffer:
                line, stdout_buffer = stdout_buffer.split("\n", 1)
                event = _task_event(line)
                if event is not None and on_event is not None:
                    await on_event(event)

        try:
            if run_input.isolated:
                if not run_input.api_url or not run_input.api_grant:
                    raise ValueError("isolated tasks require their API contract")
                ready = await sandbox.commands.run(
                    "/usr/bin/sudo -n /opt/tin-lite/isolated-procedure check", timeout=45
                )
                if ready.stdout.strip() != "TIN_ISOLATION_READY_V1":
                    raise RuntimeError("isolated task runtime is unavailable")
                ready = await sandbox.commands.run("/opt/tin-lite/run-task --check-api", timeout=15)
                if ready.stdout.strip() != "TIN_TASK_API_READY_V1":
                    raise RuntimeError("sandbox lacks API task support")
            handle = await sandbox.commands.run(
                "/opt/tin-lite/run-task",
                background=True,
                stdin=True,
                envs=envs,
                timeout=self._timeout_seconds,
                on_stdout=receive_stdout,
            )
            self._task_handles[run_input.run_id] = handle
            result = await handle.wait()
            payload: dict[str, Any] | None = None
            has_changes: bool | None = None
            delivered_entry_ids: list[str] = []
            for line in result.stdout.splitlines():
                if line.startswith("TIN_TASK_RESULT="):
                    raw = base64.b64decode(line.partition("=")[2], validate=True)
                    decoded = json.loads(raw)
                    if isinstance(decoded, dict):
                        payload = decoded
                elif line == "TIN_TASK_HAS_CHANGES=1":
                    has_changes = True
                elif line == "TIN_TASK_HAS_CHANGES=0":
                    has_changes = False
                elif line.startswith("TIN_TASK_STEERED="):
                    entry_id = line.partition("=")[2]
                    if entry_id:
                        delivered_entry_ids.append(entry_id)
            if payload is None or has_changes is None:
                raise RuntimeError("sandbox completed without a valid task result")
            outcome = str(payload.get("outcome", ""))
            if outcome == "completed" and has_changes:
                outcome = "review"
            if outcome not in {"completed", "needs_input", "paused", "stopped", "review"}:
                raise RuntimeError("sandbox returned an invalid task outcome")
            return SandboxTaskResult(
                outcome=outcome,
                summary=_redact(str(payload.get("summary", "")).strip()[:1000], secrets),
                message=_redact(str(payload.get("message", "")).strip()[:32000], secrets),
                question=(
                    _redact(str(payload["question"]).strip()[:4000], secrets)
                    if payload.get("question") is not None
                    else None
                ),
                has_changes=has_changes,
                delivered_entry_ids=tuple(delivered_entry_ids),
            )
        finally:
            self._task_handles.pop(run_input.run_id, None)
            try:
                await self._capture_rollouts(sandbox, sandbox_id=sandbox_id, run_input=run_input)
            finally:
                await self._delete_sandbox(sandbox)

    async def _run_procedure_and_kill(
        self,
        *,
        sandbox_id: str,
        run_input: SandboxProcedureInput,
    ) -> SandboxProcedureResult:
        timeout = run_input.timeout_seconds or self._timeout_seconds
        sandbox = await AsyncSandbox.connect(
            sandbox_id,
            timeout=timeout,
            api_key=self._api_key,
        )
        envs = self._run_env(sandbox_id=sandbox_id, run_input=run_input)
        context = json.dumps(run_input.context, separators=(",", ":")).encode()
        # The context goes to a file in the sandbox: Linux caps one environment string at
        # 128 KiB (MAX_ARG_STRLEN), which a procedure package plus tin_state exceeds. The
        # variable is still set while it fits, so images built before the file reader work.
        encoded = base64.b64encode(context).decode()
        if len(encoded) <= CONTEXT_ENV_MAX:
            envs["TIN_PROCEDURE_CONTEXT_B64"] = encoded
        envs["TIN_PROCEDURE_CONTEXT_PATH"] = CONTEXT_PATH
        envs["TIN_PROCEDURE_OUTPUT_PATH"] = run_input.output_path
        companion = run_input.context.get("output", {}).get("companion_path")
        if companion:
            envs["TIN_PROCEDURE_COMPANION_PATH"] = companion
            if run_input.context["output"].get("reviewed_documents"):
                envs["TIN_PROCEDURE_DOCUMENT_PAIR"] = "1"
                envs["TIN_PROCEDURE_COMPANION_MAX_BYTES"] = str(
                    run_input.context["output"]["companion_max_bytes"]
                )
        envs["TIN_PROCEDURE_OUTPUT_MAX_BYTES"] = str(run_input.output_max_bytes)
        envs["TIN_PROCEDURE_RESULT_KIND"] = run_input.result_kind
        if run_input.project_revision is not None:
            envs["TIN_PROCEDURE_PROJECT_REVISION"] = run_input.project_revision
        if run_input.isolated:
            envs["TIN_PROCEDURE_ISOLATED"] = "1"
        # With a repository snapshot the project-state checkout moves aside; the artifact
        # branch of the runner and the bridge must both address it explicitly.
        envs["TIN_PROCEDURE_STATE_DIR"] = (
            "/home/user/state" if run_input.workspace_archive is not None else "/home/user/project"
        )
        if run_input.browser:
            envs["TIN_PROCEDURE_BROWSER"] = "1"
        if run_input.studio:
            envs["TIN_PROCEDURE_STUDIO"] = "1"
        if (run_input.run_tools_url is None) != (run_input.run_tools_grant is None):
            raise ValueError("procedure run tools require both a URL and grant")
        if run_input.run_tools_url is not None and run_input.run_tools_grant is not None:
            envs["TIN_RUN_TOOLS_URL"] = run_input.run_tools_url
            envs["TIN_RUN_TOOLS_GRANT"] = run_input.run_tools_grant

        stdout_buffer = ""

        async def receive_stdout(chunk: str) -> None:
            nonlocal stdout_buffer
            # Only the isolated controller can write this stream. Legacy sandbox output
            # and worker-authored rollouts are never admitted as trusted accounting.
            if not run_input.isolated:
                return
            stdout_buffer += chunk
            if len(stdout_buffer) > 128_000:
                raise RuntimeError("isolated controller stream exceeded its bound")
            while "\n" in stdout_buffer:
                line, stdout_buffer = stdout_buffer.split("\n", 1)
                if line.startswith("TIN_CODEX_USAGE="):
                    value = json.loads(line.partition("=")[2])
                    if (
                        not isinstance(value, dict)
                        or value.get("source") != "isolated_codex_controller"
                    ):
                        raise RuntimeError("invalid isolated controller observation")
                    assert run_input.usage_sink is not None
                    await run_input.usage_sink(value)
                elif line.startswith("TIN_CODEX_PROGRESS="):
                    await _deliver_progress(line.partition("=")[2], run_input)

        try:
            if run_input.context.get("output", {}).get("validator") == "content-draft.v3":
                ready = await sandbox.commands.run(
                    "/opt/tin-lite/run-procedure --check-editorial", timeout=15
                )
                if ready.stdout.strip() != "TIN_PROCEDURE_EDITORIAL_V1":
                    raise RuntimeError("Sandbox image lacks editorial assessment support")
            if run_input.context.get("output", {}).get("reviewed_documents"):
                ready = await sandbox.commands.run(
                    "/opt/tin-lite/run-procedure --check-reviewed-documents", timeout=15
                )
                if ready.stdout.strip() != "TIN_PROCEDURE_DOCUMENTS_V1":
                    raise RuntimeError("Sandbox image lacks reviewed document support")
            if companion:
                ready = await sandbox.commands.run(
                    "/opt/tin-lite/run-procedure --check-companion", timeout=15
                )
                if ready.stdout.strip() != "TIN_PROCEDURE_COMPANION_V1":
                    raise RuntimeError("Sandbox image lacks companion document support")
            if run_input.isolated:
                if run_input.usage_sink is None:
                    raise ValueError("isolated procedures require their own profile and usage sink")
                if (run_input.browser or run_input.studio) and run_input.api_url is None:
                    raise ValueError("isolated browser execution requires the API controller")
                # Before any broker credential is fetched, fail closed on an old/missing
                # image. Merely passing an env flag to the legacy runner is not isolation.
                ready = await sandbox.commands.run(
                    "/usr/bin/sudo -n /opt/tin-lite/isolated-procedure check", timeout=45
                )
                if ready.stdout.strip() != "TIN_ISOLATION_READY_V1":
                    raise RuntimeError("isolated sandbox did not confirm its runtime protocol")
                if run_input.api_url is not None:
                    protocol = (run_input.api_contract or {}).get("protocol")
                    version = {
                        "tin-codex-api-v2": 2,
                        "tin-codex-api-v3": 3,
                        "tin-codex-api-v4": 4,
                    }.get(protocol, 1)
                    ready = await sandbox.commands.run(
                        "python3 /opt/tin-lite/codex_api_config.py "
                        + (f"--check-v{version}" if version > 1 else "--check"),
                        timeout=15,
                    )
                    expected = f"TIN_CODEX_API_READY_V{version}"
                    if ready.stdout.strip() != expected:
                        raise RuntimeError("isolated sandbox lacks the Codex API protocol")
            await sandbox.files.write(CONTEXT_PATH, context)
            if run_input.workspace_archive is not None:
                await sandbox.files.write(
                    "/home/user/.tin-lite/procedure-workspace.tar.gz",
                    run_input.workspace_archive,
                )
                envs["TIN_PROCEDURE_WORKSPACE_ARCHIVE"] = (
                    "/home/user/.tin-lite/procedure-workspace.tar.gz"
                )
            if run_input.workspace_evidence is not None:
                await sandbox.files.write(
                    "/home/user/.tin-lite/open-pull-requests.json",
                    run_input.workspace_evidence,
                )
                envs["TIN_PROCEDURE_WORKSPACE_EVIDENCE"] = (
                    "/home/user/.tin-lite/open-pull-requests.json"
                )
            handle = await sandbox.commands.run(
                "/opt/tin-lite/run-procedure",
                background=True,
                envs=envs,
                timeout=timeout,
                on_stdout=receive_stdout,
            )
            result = await handle.wait()
            commit_sha: str | None = None
            payload: dict[str, Any] | None = None
            for line in result.stdout.splitlines():
                if line.startswith("TIN_PROCEDURE_COMMIT_SHA="):
                    commit_sha = line.partition("=")[2]
                elif line.startswith("TIN_PROCEDURE_RESULT="):
                    raw = base64.b64decode(line.partition("=")[2], validate=True)
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        payload = value
            if not commit_sha or payload is None:
                raise RuntimeError("sandbox completed without a valid procedure result")
            summary = payload.get("summary")
            message = payload.get("message")
            if not isinstance(summary, str) or not summary.strip():
                raise RuntimeError("sandbox procedure result has no summary")
            if not isinstance(message, str) or not message.strip():
                raise RuntimeError("sandbox procedure result has no message")
            secrets = _run_secrets(run_input)
            return SandboxProcedureResult(
                ephemeral_commit_sha=commit_sha,
                summary=_redact(summary.strip()[:1000], secrets),
                message=_redact(message.strip()[:32000], secrets),
                diagram_review=(
                    payload.get("diagram_review")
                    if run_input.context.get("output", {}).get("validator")
                    in {"tin-diagram.reviewed.v1", "tin-diagram.branded.v1"}
                    else None
                ),
            )
        except BaseException as exc:
            # Close the paid capability first, then salvage only a frozen file.
            # Neither failed retention nor cleanup may replace the original cause.
            try:
                if run_input.failure_sink is not None:
                    await asyncio.wait_for(run_input.failure_sink(exc), timeout=5)
                if run_input.interrupted_output_sink is not None and run_input.isolated:
                    await asyncio.wait_for(
                        self._capture_interrupted_output(sandbox, run_input), timeout=45
                    )
            except BaseException:  # noqa: S110 — best effort; original failure remains authoritative
                pass
            raise
        finally:
            try:
                await self._capture_rollouts(sandbox, sandbox_id=sandbox_id, run_input=run_input)
            finally:
                await self._delete_sandbox(sandbox)

    async def _capture_interrupted_output(self, sandbox, run_input):
        from tin_lite.project_files import safe_project_file_path

        if not safe_project_file_path(run_input.output_path):
            return
        # This installed root-owned helper kills the author and rejects symlinks,
        # hardlinks and special files before giving files to the controller.
        await sandbox.commands.run(
            "/usr/bin/sudo -n /opt/tin-lite/isolated-procedure freeze", timeout=20
        )
        roots = ["/home/user/project"]
        if run_input.workspace_archive is not None:
            roots.insert(0, "/home/user/state")
        result = await sandbox.commands.run(
            "python3 -c "
            + shlex.quote(_INTERRUPTED_OUTPUT_READER)
            + " "
            + shlex.join(
                [run_input.output_path, str(min(run_input.output_max_bytes, 1_000_000)), *roots]
            ),
            timeout=15,
        )
        if not result.stdout.strip() or len(result.stdout) > 1_400_000:
            return
        content = base64.b64decode(result.stdout.strip(), validate=True)
        if not 0 < len(content) <= min(run_input.output_max_bytes, 1_000_000):
            return
        content.decode("utf-8")
        if any(secret.encode() in content for secret in _run_secrets(run_input) if secret):
            return
        await run_input.interrupted_output_sink(content)

    async def prepare_diagram(self, *, sandbox_id: str, branded: bool = False) -> None:
        # Read-only startup checks precede the paid-attempt receipt. A transient
        # envd stream timeout must remain retryable without implying a model call.
        sandbox = await AsyncSandbox.connect(
            sandbox_id,
            timeout=self._timeout_seconds,
            api_key=self._api_key,
        )
        ready = await sandbox.commands.run(
            "node /opt/tin-lite/diagram/scripts/check_diagram.mjs --version",
            timeout=45,
            request_timeout=30,
        )
        if ready.stdout.strip() != "tin-diagram-check.v1":
            raise RuntimeError("sandbox lacks the offline diagram checker")
        if branded:
            support = await sandbox.commands.run(
                "node /opt/tin-lite/diagram/scripts/check_diagram.mjs --brand-version",
                timeout=45,
                request_timeout=30,
            )
            if support.stdout.strip() != "tin-diagram.branded.v1":
                raise RuntimeError("sandbox lacks the branded diagram checker; rebuild its image")
        await self._diagram_product_styles(sandbox)

    async def validate_diagram(self, *, content: str | bytes, run_id: str, revision: str) -> dict:
        """Re-render exact checkpoint bytes in a clean, credential-free offline sandbox.

        Both normal execution and checkpoint recovery use this boundary. No agent
        process, checkout scripts, or supplied report can influence its result.
        """
        import hashlib
        from pathlib import Path

        from tin_lite.brand_diagrams import parse_diagram

        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not 0 < len(raw) <= 64_000:
            raise ValueError("diagram source exceeds its byte bound")
        parse_diagram(raw.decode("utf-8"))
        source_sha = hashlib.sha256(raw).hexdigest()
        assets = Path(__file__).parent / "static"
        names = [
            "app.css",
            "diagram-renderer.js",
            "diagram-routing.wasm",
            "fonts/geist-sans-regular.woff2",
            "fonts/geist-sans-bold.woff2",
            "fonts/geist-mono-regular.woff2",
            "fonts/geist-mono-bold.woff2",
        ]
        renderer_sha = hashlib.sha256(
            b"".join((assets / name).read_bytes() for name in names)
        ).hexdigest()
        sandbox = await AsyncSandbox.create(
            self._isolated_template or self._template,
            timeout=120,
            metadata={"run_id": run_id, "execution_key": f"{run_id}:diagram_check:{source_sha}"},
            network={"deny_out": lambda context: [context.all_traffic]},
            api_key=self._api_key,
        )
        try:
            await self._observe_sandbox(sandbox)
            await self._diagram_product_styles(sandbox)
            await sandbox.files.write("/tmp/tin-candidate.mmd", raw)  # noqa: S108 — fresh private sandbox
            result = await sandbox.commands.run(
                "node /opt/tin-lite/diagram/scripts/check_diagram.mjs check "
                "/tmp/tin-candidate.mmd --out /tmp/tin-final-check --no-previews",
                timeout=90,
            )
            if len(result.stdout) > 64000:
                raise RuntimeError("diagram check diagnostics exceed their bound")
            report = json.loads(result.stdout)
            themes = report.get("themes")
            # Layout quality is advisory metadata. Keep both theme proofs mandatory
            # without treating additive diagnostics as a failed visual audit.
            themes_passed = (
                isinstance(themes, list)
                and len(themes) == 2
                and all(
                    isinstance(theme, dict)
                    and theme.get("theme") == name
                    and theme.get("issues") == []
                    for theme, name in zip(themes, ("light", "dark"), strict=True)
                )
            )
            if (
                report.get("checker") != "tin-diagram-check.v1"
                or report.get("source_sha256") != source_sha
                or report.get("source_bytes") != len(raw)
                or report.get("renderer_sha256") != renderer_sha
                or report.get("passed") is not True
                or report.get("issues") != []
                or not themes_passed
            ):
                raise RuntimeError("The saved diagram failed independent visual validation")
            return {
                "checker": report["checker"],
                "source_sha256": source_sha,
                "source_bytes": len(raw),
                "renderer_sha256": renderer_sha,
                "revision": revision,
                "themes": ["light", "dark"],
                "browser_version": report["browser_version"],
            }
        finally:
            await self._delete_sandbox(sandbox)

    @staticmethod
    async def _diagram_product_styles(sandbox) -> None:
        from pathlib import Path

        # Ordinary UI color/CSS changes must not require rebuilding the compute
        # image. Only trusted packaged product CSS is refreshed, before any author
        # starts. Fonts, renderer, checker and browser remain template-pinned.
        await sandbox.files.write(
            "/opt/tin-lite/diagram/src/tin_lite/static/app.css",
            (Path(__file__).parent / "static" / "app.css").read_bytes(),
            user="root",
        )

    async def _capture_rollouts(
        self, sandbox: Any, *, sandbox_id: str, run_input: SandboxRunInput
    ) -> None:
        """Pull Codex rollouts out of a live sandbox and hand them to the run's sink.

        Runs inside the runner's ``finally`` before ``kill()``, so it sees the transcript
        of failed runs too. Every failure here is logged with identifiers only and
        swallowed: capture is diagnostics, never part of the run's effect.
        """
        sink = run_input.rollout_sink
        if sink is None:
            return
        try:
            async with asyncio.timeout(ROLLOUT_CAPTURE_BUDGET_SECONDS):
                capture = await collect_rollouts(
                    sandbox.files,
                    sandbox_id=sandbox_id,
                    redact=_run_secrets(run_input),
                )
                await sink(capture)
        except Exception:
            logger.warning(
                "rollout capture failed",
                extra={"sandbox_id": sandbox_id, "execution_key": run_input.execution_key},
                exc_info=True,
            )

    async def control_task(self, *, run_id: str, control: dict[str, Any]) -> bool:
        handle = self._task_handles.get(run_id)
        if handle is None:
            return False
        await handle.send_stdin(json.dumps(control, separators=(",", ":")) + "\n")
        return True

    def _run_env(self, *, sandbox_id: str, run_input: SandboxRunInput) -> dict[str, str]:
        envs = {
            "TIN_EXECUTION_KEY": run_input.execution_key,
            "TIN_SANDBOX_ID": sandbox_id,
            "TIN_CANONICAL_URL": run_input.canonical_url,
            "TIN_CANONICAL_AUTH_HEADER": run_input.canonical_auth_header,
            "TIN_CANONICAL_BRANCH": run_input.canonical_branch,
            "TIN_EPHEMERAL_URL": run_input.ephemeral_url,
            "TIN_EPHEMERAL_AUTH_HEADER": run_input.ephemeral_auth_header,
            "TIN_EPHEMERAL_BRANCH": run_input.ephemeral_branch,
            "NO_PROXY": run_input.no_proxy,
            "no_proxy": run_input.no_proxy,
        }
        if run_input.proxy_url:
            envs.update(
                {
                    "HTTP_PROXY": run_input.proxy_url,
                    "HTTPS_PROXY": run_input.proxy_url,
                    "http_proxy": run_input.proxy_url,
                    "https_proxy": run_input.proxy_url,
                }
            )
        if isinstance(run_input, (SandboxProcedureInput, SandboxTaskInput)) and (
            run_input.api_url is not None or run_input.api_grant is not None
        ):
            if not run_input.isolated or not run_input.api_url or not run_input.api_grant:
                raise ValueError("Codex API execution requires an isolated, run-bound grant")
            envs.update(
                TIN_CODEX_API_URL=run_input.api_url, TIN_CODEX_API_GRANT=run_input.api_grant
            )
            if run_input.api_contract is not None:
                envs["TIN_CODEX_API_CONTRACT"] = json.dumps(run_input.api_contract)
        return envs

    async def is_running(self, sandbox_id: str) -> bool:
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
            return await sandbox.is_running()
        except SandboxNotFoundException:
            return False

    async def kill(self, sandbox_id: str) -> None:
        """Idempotently remove a sandbox left behind by worker or activity failure."""
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
            await self._delete_sandbox(sandbox)
        except SandboxNotFoundException:
            await self._observe_sandbox(None, sandbox_id=sandbox_id, absent=True)
            return

    async def _observe_sandbox(self, sandbox, *, sandbox_id=None, ended=False, absent=False):
        if self._usage_database is None:
            return
        try:
            async with asyncio.timeout(3):
                info = await sandbox.get_info() if sandbox is not None else None
                await observe_sandbox(
                    self._usage_database,
                    sandbox.sandbox_id if sandbox is not None else sandbox_id,
                    info=info,
                    ended=ended,
                    absent=absent,
                )
        except Exception:
            # Observability cannot strand a paid sandbox or undo successful deletion.
            # Missing observations are a visible coverage gap, never invented zero usage.
            logger.warning("Sandbox usage observation unavailable")

    async def _delete_sandbox(self, sandbox):
        try:
            await self._observe_sandbox(sandbox)
        finally:
            killed = await sandbox.kill()
        if self._usage_database is not None:
            await self._observe_sandbox(
                None, sandbox_id=sandbox.sandbox_id, ended=killed is True, absent=killed is False
            )

    async def _find(self, execution_key: str, *, template: str | None = None) -> str | None:
        paginator = AsyncSandbox.list(
            SandboxQuery(metadata={"execution_key": execution_key}),
            limit=10,
            api_key=self._api_key,
        )
        items = await paginator.next_items(api_key=self._api_key)
        for item in items:
            try:
                sandbox = await AsyncSandbox.connect(item.sandbox_id, api_key=self._api_key)
                if not await sandbox.is_running():
                    continue
            except SandboxNotFoundException:
                continue
            recorded = (getattr(item, "metadata", None) or {}).get("template")
            if template is not None and recorded is not None and recorded != template:
                raise RuntimeError("a running sandbox for this execution uses a different template")
            return item.sandbox_id
        return None


async def collect_rollouts(
    files: Any, *, sandbox_id: str, redact: tuple[str, ...]
) -> RolloutCapture:
    """Bounded walk of the Codex sessions tree, returning redacted rollout files.

    The tree is ``sessions/YYYY/MM/DD/rollout-*.jsonl``; a breadth-first walk with
    ``depth=1`` listings keeps every step small and stubbable. Files beyond the caps are
    recorded as metadata only, and a failing read never hides the other files.
    """
    error: str | None = None
    entries: list[Any] = []
    pending: list[tuple[str, int]] = [(CODEX_SESSIONS_DIR, 0)]
    lists = 0
    while pending and lists < ROLLOUT_MAX_DIR_LISTS:
        directory, depth = pending.pop(0)
        lists += 1
        try:
            listed = await files.list(
                directory, depth=1, request_timeout=ROLLOUT_LIST_TIMEOUT_SECONDS
            )
        except NotFoundException:
            if depth == 0:
                return RolloutCapture(sandbox_id=sandbox_id, files=())
            continue
        for entry in listed:
            if entry.type == FileType.DIR:
                if depth + 1 < ROLLOUT_MAX_DEPTH:
                    pending.append((entry.path, depth + 1))
            elif entry.type == FileType.FILE and parse_rollout_filename(entry.name):
                entries.append(entry)
    entries.sort(key=lambda entry: entry.name)
    skipped = max(0, len(entries) - ROLLOUT_MAX_FILES)
    collected: list[RolloutFile] = []
    total = 0
    for entry in entries[:ROLLOUT_MAX_FILES]:
        parsed = parse_rollout_filename(entry.name)
        thread_id = parsed[1] if parsed else None
        size = int(entry.size or 0)
        if size > ROLLOUT_FILE_MAX_BYTES or total + size > ROLLOUT_TOTAL_MAX_BYTES:
            if size <= ROLLOUT_FILE_MAX_BYTES:
                skipped += 1
            collected.append(
                RolloutFile(
                    path=entry.path,
                    filename=entry.name,
                    thread_id=thread_id,
                    content=b"",
                    size_bytes=size,
                    truncated=True,
                )
            )
            continue
        try:
            raw = await files.read(
                entry.path, format="bytes", request_timeout=ROLLOUT_READ_TIMEOUT_SECONDS
            )
        except Exception as exc:
            error = type(exc).__name__
            continue
        content = bytes(raw)
        truncated = len(content) > ROLLOUT_FILE_MAX_BYTES
        if truncated:
            content = content[:ROLLOUT_FILE_MAX_BYTES]
        total += len(content)
        redacted, redactions = redact_rollout(content, redact)
        collected.append(
            RolloutFile(
                path=entry.path,
                filename=entry.name,
                thread_id=thread_id,
                content=redacted,
                size_bytes=max(size, len(content)),
                truncated=truncated,
                redactions=redactions,
            )
        )
    return RolloutCapture(
        sandbox_id=sandbox_id, files=tuple(collected), skipped=skipped, error=error
    )


def _run_secrets(run_input: SandboxRunInput) -> tuple[str, ...]:
    values = sandbox_secrets(
        canonical_auth_header=run_input.canonical_auth_header,
        ephemeral_auth_header=run_input.ephemeral_auth_header,
        proxy_url=run_input.proxy_url,
        run_tools_grant=getattr(run_input, "run_tools_grant", None),
        extra=run_input.redact
        + ((run_input.api_grant,) if getattr(run_input, "api_grant", None) else ()),
    )

    # Explicit secrets include short payment security codes.
    return tuple(dict.fromkeys((*values, *(value for value in run_input.redact if value))))


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    return redact_text(value, secrets)[0]


async def _deliver_progress(encoded: str, run_input: SandboxProcedureInput) -> None:
    """Narration is display text, not accounting: drop malformed frames, never fail."""
    sink = getattr(run_input, "progress_sink", None)
    if sink is None:
        return
    try:
        value = json.loads(encoded)
    except ValueError:
        return
    text = value.get("text") if isinstance(value, dict) else None
    if not isinstance(text, str) or not text.strip():
        return
    try:
        await sink(_redact(text[:PROGRESS_MAX_CHARS], _run_secrets(run_input)))
    except Exception:
        logger.warning(
            "procedure progress update failed",
            extra={"execution_key": run_input.execution_key},
            exc_info=True,
        )


def _task_event(line: str) -> SandboxTaskEvent | None:
    if not line.startswith("TIN_TASK_EVENT="):
        return None
    encoded = line.partition("=")[2]
    try:
        raw = base64.b64decode(encoded, validate=True)
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    sequence = payload.get("sequence")
    kind = payload.get("kind")
    message = payload.get("message")
    if (
        not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(kind, str)
        or not kind
        or not isinstance(message, str)
        or not message.strip()
    ):
        return None
    return SandboxTaskEvent(
        sequence=sequence,
        kind=kind[:80],
        message=message.strip()[:1000],
    )
