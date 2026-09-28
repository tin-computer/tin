"""Tin-owned E2B launcher, uploaded from the installed worker; Python stdlib only.

Only --worker imports customer code, after network isolation and a UID drop.
No customer output or exception is a controller command or product log.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import inspect
import json
import os
import pwd
import resource
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/user/project")
CONTROL = Path("/root/tin-code")
PYTHON = "/opt/tin-lite/metadata-venv/bin/python"
MAX_RESULT = 64_000 * 6 + 2048
MAX_RPC = 128_000
SOCKET = "/run/tin-code-model.sock"
SERVICE_ERROR_EXIT = 3
"""Exit status when authored code let a service error from the bridge escape.

Only that fact crosses back, so the host may report its own stored error for the run; any
other failure keeps the generic status. Must match e2b_runtime.SERVICE_ERROR_EXIT.
"""


class ServiceError(ValueError):
    """A service error Tin handed to authored code, as the documented ValueError."""


class ServiceErrorEscaped(Exception):
    """Authored code let a ServiceError escape; the runner exits with SERVICE_ERROR_EXIT."""


class Models:
    async def generate(self, *, route, step, instructions, data, output_schema=None):
        return await asyncio.to_thread(
            self._call,
            {
                "route": route,
                "step": step,
                "instructions": instructions,
                "data": data,
                "output_schema": output_schema,
            },
        )

    def _call(self, request):
        raw = json.dumps(request, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(raw) > MAX_RPC:
            raise ValueError("Model request exceeds its bound.")
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(60)
            client.connect(SOCKET)
            client.sendall(raw)
            response = json.loads(client.makefile("rb").readline(MAX_RPC + 1))
        if response.get("error"):
            error = ServiceError if request.get("kind") == "service" else ValueError
            raise error(response["error"])
        return response["result"]


class Context(dict):
    models = Models()

    class Services(Models):
        async def call(self, *, service, step, operation, arguments=None):
            return await asyncio.to_thread(
                self._call,
                {
                    "kind": "service",
                    "payload": {
                        "service": service,
                        "step": step,
                        "operation": operation,
                        "arguments": arguments or {},
                    },
                },
            )

        async def request(self, *, service, step, path, method="GET", params=None, body=None):
            return await self.call(
                service=service,
                step=step,
                operation="http.request",
                arguments={
                    "method": method,
                    "path": path,
                    "params": params or {},
                    "body": body,
                },
            )

    services = Services()


def managed_process(command, timeout):
    """Bridge bounded local IPC through the protected E2B control channel.

    The author UID can ask for model calls, but never gets an E2B/Tin/provider key.
    Only a fixed notification reaches stdout; payloads stay in protected files.
    """
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(SOCKET)
        os.chown(SOCKET, 0, pwd.getpwnam("tin-work").pw_gid)
        os.chmod(SOCKET, 0o660)
        server.listen(4)
        server.settimeout(0.1)
        with subprocess.Popen(  # noqa: S603 — trusted UID-drop command
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as process:
            calls = 0
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("code execution timed out")
                    try:
                        client, _ = server.accept()
                    except TimeoutError:
                        continue
                    with client:
                        client.settimeout(min(2, max(0.01, deadline - time.monotonic())))
                        raw = client.makefile("rb").readline(MAX_RPC + 1)
                        calls += 1
                        if len(raw) > MAX_RPC or not raw.endswith(b"\n") or calls > 32:
                            raise ValueError("invalid model request envelope")
                        request = json.loads(raw)
                        (CONTROL / "request.json").write_text(json.dumps(request), encoding="utf-8")
                        print("TIN_MODEL_REQUEST", flush=True)
                        reply = CONTROL / "response.json"
                        while not reply.exists():
                            if time.monotonic() >= deadline:
                                raise TimeoutError("model request timed out")
                            time.sleep(0.02)
                        response = reply.read_bytes()
                        reply.unlink()
                        if len(response) > MAX_RPC:
                            raise ValueError("model response exceeds its bound")
                        client.sendall(response + b"\n")
                if process.returncode == SERVICE_ERROR_EXIT:
                    raise ServiceErrorEscaped
                if process.returncode:
                    raise RuntimeError("code worker failed")
            finally:
                if process.poll() is None:
                    process.kill()


def worker():
    # -I -S excludes user sites and installed dependencies; only stdlib and package files.
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64,) * 2)
    resource.setrlimit(resource.RLIMIT_NPROC, (16,) * 2)
    context = json.loads(Path("/run/tin-code-context.json").read_bytes())
    timeout = context["timeout_seconds"]
    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    namespace = runpy.run_path(str(ROOT / context["entrypoint"]))
    ctx = Context(context["context"]) if context.get("model_client") else context["context"]
    try:
        result = namespace["run"](ctx, context["inputs"])
        if inspect.isawaitable(result):
            result = asyncio.run(result)
    except ServiceError:
        raise ServiceErrorEscaped from None
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_RESULT:
        raise ValueError("result too large")
    (ROOT / "_tin_result.json").write_bytes(raw)


def controller():
    if os.geteuid() != 0 or sys.version_info[:3] != (3, 12, 8):
        raise RuntimeError("code runtime version or identity mismatch")
    # The existing image's protected helper owns UID separation and whole-UID cleanup.
    loader = importlib.machinery.SourceFileLoader(
        "tin_isolation", "/opt/tin-lite/isolated-procedure"
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    isolation = importlib.util.module_from_spec(spec)
    loader.exec_module(isolation)
    isolation.protect_runtime()
    packet = json.loads((CONTROL / "packet.json").read_bytes())
    ROOT.mkdir(exist_ok=True)
    # A bounded scratch filesystem prevents an untrusted loop filling the sandbox disk.
    subprocess.run(  # noqa: S603 — fixed trusted launcher, isolated author UID
        ["/usr/bin/mount", "-t", "tmpfs", "-o", "size=16m,nosuid,nodev", "tmpfs", str(ROOT)],
        check=True,
        capture_output=True,
    )
    try:
        for name, content in packet["files"].items():
            destination = ROOT / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        context = {
            key: packet[key] for key in ("entrypoint", "timeout_seconds", "context", "inputs")
        }
        context["model_client"] = packet.get("model_client", False)
        Path("/run/tin-code-context.json").write_text(json.dumps(context), encoding="utf-8")
        Path("/run/tin-code-context.json").chmod(0o444)
        # Worker must be able to read this trusted launcher, but cannot replace it.
        script = Path("/run/tin-code-runner.py")
        script.write_bytes(Path(__file__).read_bytes())
        script.chmod(0o444)
        isolation.prepare()
        try:
            command = [
                "/usr/bin/unshare",
                "--net",
                *isolation.worker_command(PYTHON, "-I", "-S", str(script), "--worker"),
            ]
            if context["model_client"]:
                managed_process(command, packet["timeout_seconds"])
            else:
                subprocess.run(  # noqa: S603 — fixed trusted launcher, isolated author UID
                    command,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=packet["timeout_seconds"],
                    check=True,
                )
        finally:
            isolation.freeze()
        result = ROOT / "_tin_result.json"
        if result.stat().st_size > MAX_RESULT:
            raise ValueError("result too large")
        (CONTROL / "result.json").write_bytes(result.read_bytes())
    finally:
        subprocess.run(  # noqa: S603 — fixed trusted mount point
            ["/usr/bin/umount", str(ROOT)], check=True, capture_output=True
        )


if __name__ == "__main__":
    try:
        worker() if sys.argv[1:] == ["--worker"] else controller()
    except ServiceErrorEscaped:
        raise SystemExit(SERVICE_ERROR_EXIT) from None
    except BaseException:
        # Do not propagate customer exceptions/source/terminal output to trusted logs.
        raise SystemExit("Code workflow failed or exceeded its execution limits.") from None
