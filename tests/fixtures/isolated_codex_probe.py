"""Run only inside a disposable E2B VM. All credentials/provider responses are synthetic."""

import base64
import hashlib
import importlib.machinery
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codex_web_evidence import WebEvidence

SCENARIO = os.environ.get("TIN_TEST_SCENARIO", "success")
requests = []
compactions = []
model_steps = 0
request_paths = []
wire_headers = {}
calls = [
    (
        "exec_command",
        {
            "cmd": (
                "id; test ! -r /home/user/.codex/auth.json && echo DENIED_AUTH; "
                'test -z "${TIN_SYNTHETIC_CANARY:-}" && echo CLEAN_ENV; '
                "test ! -w /usr/local/bin/python3 && echo RUNTIME_PROTECTED; "
                "touch /usr/local/bin/tin-probe; mkdir /usr/local/lib/tin-probe; "
                "sudo -n id; git config core.hooksPath /tmp/evil; "
                "mv .git .git-replaced; "
                "test ! -r /proc/1/environ && echo DENIED_PROC"
            ),
            "max_output_tokens": 1500,
        },
    ),
    (
        "apply_patch",
        "*** Begin Patch\n*** Add File: /home/user/.codex/probe-write\n+bad\n*** End Patch",
    ),
    ("exec_command", {"cmd": "id", "environment_id": "local"}),
    ("view_image", {"path": "/home/user/.codex/probe.png"}),
    ("apply_patch", "*** Begin Patch\n*** Add File: RESULT.md\n+valid artifact\n*** End Patch"),
    (
        "exec_command",
        {
            "cmd": "umask 077; mkdir -p reports/private; "
            "mv RESULT.md reports/private/RESULT.md; chmod 600 reports/private/RESULT.md; "
            "setsid sleep 90 </dev/null >/dev/null 2>&1 &",
            "yield_time_ms": 1000,
        },
    ),
]
if SCENARIO in {"api_context", "session_context", "bounded_context"}:
    calls += [("exec_command", {"cmd": "test -s reports/private/RESULT.md"})] * 4
if SCENARIO == "studio_voice":
    from codex_api_config import worker_shell_policy

    config = Path("/home/user/.codex/config.toml")
    config.write_text(
        config.read_text()
        + worker_shell_policy(
            {
                "TIN_PROCEDURE_STUDIO": "1",
                "TIN_RUN_TOOLS_URL": "https://tin.test/internal/run-tools/mcp",
                "TIN_RUN_TOOLS_GRANT": "synthetic-voice-grant",
                "FAL_KEY": "must-not-reach-worker",
            }
        )
    )
    calls[0][1]["cmd"] += (
        '; test "$TIN_RUN_TOOLS_URL" = "https://tin.test/internal/run-tools/mcp" '
        '&& test "$TIN_RUN_TOOLS_GRANT" = "synthetic-voice-grant" '
        '&& test -z "${FAL_KEY:-}${TIN_CODEX_API_GRANT:-}" && echo VOICE_CAPABILITY_OK'
    )
if SCENARIO == "companion":
    calls.append(
        (
            "apply_patch",
            "*** Begin Patch\n"
            "*** Add File: reports/private/RESULT.generation.md\n"
            "+internal generation notes\n*** End Patch",
        )
    )
if SCENARIO == "technical_verifier":
    calls.append(
        (
            "apply_patch",
            "*** Begin Patch\n*** Add File: index.html\n"
            "+<html><head><title>Example</title></head><body>Example</body></html>\n*** End Patch",
        )
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        global model_steps
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        request_paths.append(self.path)
        if len(requests) == 1:
            wire_headers.update(
                {
                    key: self.headers[key]
                    for key in ("x-codex-beta-features", "x-openai-internal-codex-responses-lite")
                    if key in self.headers
                }
            )
        compact = self.path.endswith("/compact") or (
            SCENARIO in {"api_context", "session_context", "bounded_context"}
            and "Any critical data, examples, or references needed to continue"
            in json.dumps(body.get("input"))
        )
        if compact:
            compactions.append(self.path)
        else:
            model_steps += 1
        number = model_steps
        if self.path.endswith("/compact"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "cmp_synthetic",
                        "object": "response.compaction",
                        "output": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Continue the test after the completed tool.",
                                    }
                                ],
                            }
                        ],
                        "usage": {
                            "input_tokens": 100000,
                            "output_tokens": 100,
                            "total_tokens": 100100,
                            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                        },
                    }
                ).encode()
            )
            return
        if number <= len(calls):
            name, args = calls[number - 1]
            code = (
                "try { text(await tools." + name + "(" + json.dumps(args) + ")); } "
                "catch (error) { text(String(error)); }"
            )
            if SCENARIO == "session_context" and number == 1:
                code += "\n// synthetic tool argument padding" * 1500
            output = [
                {
                    "type": "custom_tool_call",
                    "id": f"fc_{number}",
                    "call_id": f"call_{number}",
                    "name": "exec",
                    "input": code,
                }
            ]
        else:
            text = json.dumps({"summary": "Synthetic procedure", "message": "Done."})
            if SCENARIO == "invalid_result":
                text = "not JSON"
            output = [
                {
                    "type": "message",
                    "id": "msg",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        if (
            SCENARIO in {"hosted_search", "api_context", "session_context", "bounded_context"}
            and number == 1
            and not compact
        ):
            output.insert(
                0,
                {
                    "type": "web_search_call",
                    "id": "ws_synthetic",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "query": "ClawMessenger",
                        "sources": [{"type": "url", "url": "https://clawmessenger.com"}],
                    },
                    "results": [
                        {
                            "type": "text_result",
                            "url": "https://clawmessenger.com/docs",
                            "snippet": "SOURCE_HANDOFF_MARKER: POST /api/agent/send-message; "
                            "phone_number and text.",
                        }
                    ],
                },
            )
        if compact:
            output = [
                {
                    "type": "message",
                    "id": "msg_compact",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Checkpoint: continue the test; prior tool completed. "
                            "SOURCE_HANDOFF_MARKER: POST /api/agent/send-message; "
                            "phone_number and text; "
                            "source https://clawmessenger.com/docs.",
                        }
                    ],
                }
            ]
        input_tokens = 250_000 if SCENARIO == "token_limit" else 100
        if SCENARIO in {"api_context", "session_context", "bounded_context"} and len(requests) == 1:
            input_tokens = 100000
        if SCENARIO == "session_context" and not compact and number <= 3:
            input_tokens = 940_000  # Force real CLI compaction and exceed the old lifetime stop.
        if SCENARIO == "bounded_context" and not compact and number <= 3:
            input_tokens = 240_000  # Over v5's 200,000-token compaction threshold.
        response = {
            "id": f"resp_{number}",
            "object": "response",
            "status": "completed",
            "model": "gpt-6-sol",
            "output": output,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 10,
                "total_tokens": input_tokens + 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        evidence = WebEvidence()

        def emit(event):
            for adapted in evidence.events(event):
                self.wfile.write(("data: " + json.dumps(adapted) + "\n\n").encode())

        for index, item in enumerate(output):
            event = {"type": "response.output_item.done", "output_index": index, "item": item}
            emit(event)
        event = {"type": "response.completed", "response": response}
        emit(event)
        self.wfile.flush()


server = ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
context = {
    "workflow_key": "private.synthetic",
    "entry_skill": "test",
    "prompt": "Run the test.",
    "output": {"kind": "project.artifact", "path": "reports/private/RESULT.md"},
    "skill_files": {
        "test/SKILL.md": "---\nname: test\ndescription: Synthetic test\n---\nWrite RESULT.md."
    },
    "verification": {
        "commands": [
            "false"
            if SCENARIO == "verification_failure"
            else 'test "$(id -un)" = tin-work && test -z "${TIN_SYNTHETIC_CANARY:-}" '
            "&& test ! -r /home/user/.codex/auth.json && test -f reports/private/RESULT.md"
        ]
    },
}
if SCENARIO == "hosted_search":
    context["content_draft"] = {
        "frontmatter": {"brief_sha256": "pinned-brief-digest"},
        "item": {"id": "next-item"},
    }
if SCENARIO == "companion":
    context["output"].update(
        companion_path="reports/private/RESULT.generation.md", companion_max_bytes=24000
    )
if SCENARIO == "technical_verifier":
    before = b"<html><head></head><body>Example</body></html>\n"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        entry = tarfile.TarInfo("index.html")
        entry.size = len(before)
        archive.addfile(entry, io.BytesIO(before))
    archive_path = Path("/home/user/.tin-lite/procedure-workspace.tar.gz")
    archive_path.write_bytes(stream.getvalue())
    os.chown(archive_path, 1000, 1000)
    archive_path.chmod(0o600)
    snapshot = hashlib.sha256(
        json.dumps(
            [("index.html", hashlib.sha256(before).hexdigest())], separators=(",", ":")
        ).encode()
    ).hexdigest()
    context.update(
        workflow_key="organic.technical_fix",
        workspace={
            "technical_fix": {
                "originals": {"index.html": before.decode()},
                "selection": {"finding": {"check_id": "metadata.title_missing"}},
                "verification_profile": {
                    "kind": "static-html-v1",
                    "snapshot_sha256": snapshot,
                    "observations": [
                        {
                            "path": "index.html",
                            "values": {},
                            "sha256": hashlib.sha256(before).hexdigest(),
                        }
                    ],
                },
            }
        },
    )
    context["verification"]["commands"] += [
        "/opt/tin-lite/metadata-venv/bin/python -I /opt/tin-lite/verify-technical-metadata.py"
    ]
env = {
    **os.environ,
    "HTTP_PROXY": "http://127.0.0.1:1",
    "HTTPS_PROXY": "http://127.0.0.1:1",
    "http_proxy": "http://127.0.0.1:1",
    "https_proxy": "http://127.0.0.1:1",
    "NO_PROXY": "broker.test",
    "no_proxy": "broker.test",
    "TIN_PROCEDURE_ISOLATED": "1",
    "TIN_SYNTHETIC_CANARY": "synthetic-controller-secret",
    "TIN_PROCEDURE_CONTEXT_B64": base64.b64encode(json.dumps(context).encode()).decode(),
    "TIN_PROCEDURE_RESULT_PATH": "/home/user/.tin-lite/procedure-result.json",
}
if SCENARIO in {"api_context", "session_context", "bounded_context"}:
    env.update(
        TIN_CODEX_API_URL="https://tin.test/internal/codex-api/test/v1",
        TIN_CODEX_API_CONTRACT=json.dumps(
            {
                "protocol": {
                    "session_context": "tin-codex-api-v4",
                    "bounded_context": "tin-codex-api-v5",
                }.get(SCENARIO, "tin-codex-api-v3")
            }
        ),
    )
result = subprocess.run(
    ["/usr/sbin/runuser", "-u", "user", "--", "/opt/tin-lite/procedure-app-server"],
    env=env,
    capture_output=True,
    text=True,
    timeout=100,
)
usage = [
    json.loads(line.split("=", 1)[1])
    for line in result.stdout.splitlines()
    if line.startswith("TIN_CODEX_USAGE=")
]
tools = {
    item.get("call_id"): (
        item["output"] if isinstance(item["output"], str) else json.dumps(item["output"])
    )
    for request in requests
    for item in request.get("input", [])
    if item.get("type") in {"function_call_output", "custom_tool_call_output"}
}
sys.path.insert(0, "/opt/tin-lite")

helper = importlib.machinery.SourceFileLoader(
    "isolation", "/opt/tin-lite/isolated-procedure"
).load_module()
facts = {
    "source_survived_tool": any(
        "SOURCE_HANDOFF_MARKER" in json.dumps(r.get("input")) for r in requests[1:]
    ),
    "source_survived_second_tool": len(requests) > 2
    and "SOURCE_HANDOFF_MARKER" in json.dumps(requests[2].get("input")),
    "source_in_compaction": any(
        "SOURCE_HANDOFF_MARKER" in json.dumps(r.get("input"))
        for r in requests
        if "Any critical data, examples, or references needed to continue"
        in json.dumps(r.get("input"))
    ),
    "wire_request": requests[0] if requests else {},
    "wire_headers": wire_headers,
    "draft_context_visible": "pinned-brief-digest" in json.dumps(requests[0])
    if requests
    else False,
    "request_paths": request_paths,
    "last_inputs": [str(item.get("input", ""))[-300:] for item in requests],
    "compactions": compactions,
    "model_steps": model_steps,
    "advertised_tools": (
        requests[0].get("tools", [])
        + [
            tool
            for item in requests[0].get("input", [])
            if "tools" in item
            for tool in item["tools"]
        ]
    )
    if requests
    else [],
    "request_shape": [list(request) for request in requests],
    "request_tool_fields": {key: value for key, value in requests[0].items() if "tool" in key}
    if requests
    else {},
    "exit_code": result.returncode,
    "usage": usage,
    "tools": tools,
    "error": result.stderr[-1500:],
    "protected_write": Path("/home/user/.codex/probe-write").exists(),
    "project_mcp_leak": Path("/home/user/project/leak").exists(),
    "git_replaced": Path("/home/user/project/.git-replaced").exists(),
    "runtime_write": Path("/usr/local/bin/tin-probe").exists()
    or Path("/usr/local/lib/tin-probe").exists(),
    "artifact": Path("/home/user/project/reports/private/RESULT.md").read_text()
    if Path("/home/user/project/reports/private/RESULT.md").exists()
    else None,
    "worker_pids": helper.worker_pids(),
}
if SCENARIO == "companion":
    companion = Path("/home/user/project/reports/private/RESULT.generation.md")
    facts["companion"] = companion.read_text() if companion.exists() else None
    facts["companion_instruction"] = "internal generation notes" in json.dumps(requests[0]).lower()
if result.returncode == 0:
    read = subprocess.run(
        [
            "/usr/sbin/runuser",
            "-u",
            "user",
            "--",
            "/bin/cat",
            "/home/user/project/reports/private/RESULT.md",
        ],
        capture_output=True,
        text=True,
    )
    facts["controller_read"] = read.returncode == 0 and read.stdout == "valid artifact\n"
    commit = subprocess.run(  # noqa: S603 — both command variants are fixed synthetic fixtures
        [
            "/usr/sbin/runuser",
            "-u",
            "user",
            "--",
            "/bin/bash",
            "-c",
            "git -C /home/user/project add reports/private/RESULT.md "
            + ("reports/private/RESULT.generation.md " if SCENARIO == "companion" else "")
            + "&& "
            "git -C /home/user/project commit -m result",
        ],
        capture_output=True,
        text=True,
    )
    facts["commit_exit_code"] = commit.returncode
print(json.dumps(facts))
server.shutdown()
