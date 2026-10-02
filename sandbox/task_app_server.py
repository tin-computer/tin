#!/usr/bin/env python3
"""Small stdio bridge between Tin and Codex app-server.

Only normalized product facts and final messages leave this process. Raw reasoning, commands,
command output, credentials, and app-server notifications stay inside the E2B sandbox.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

WORKSPACE = "/home/user/project"
ISOLATED = os.environ.get("TIN_PROCEDURE_ISOLATED") == "1"
ISOLATION_HELPER = ["/usr/bin/sudo", "-n", "/opt/tin-lite/isolated-procedure"]
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "outcome": {"type": "string", "enum": ["completed", "needs_input"]},
        "summary": {"type": "string", "maxLength": 1000},
        "message": {"type": "string", "maxLength": 32000},
        "question": {"type": ["string", "null"], "maxLength": 4000},
    },
    "required": ["outcome", "summary", "message", "question"],
}


def _write(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _reader(stream: Any, output: queue.Queue[str | None]) -> None:
    for line in stream:
        output.put(line)
    output.put(None)


def _controls(output: queue.Queue[dict[str, Any]]) -> None:
    for line in sys.stdin:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            output.put(value)


def _decode_context() -> dict[str, Any]:
    raw = base64.b64decode(os.environ["TIN_TASK_CONTEXT_B64"], validate=True)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("task context must be an object")
    return value


def _request(
    proc: subprocess.Popen[str],
    incoming: queue.Queue[str | None],
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    _write(proc, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        line = incoming.get(timeout=60)
        if line is None:
            raise RuntimeError("Codex app-server stopped unexpectedly")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"Codex app-server rejected {method}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Codex app-server returned an invalid {method} result")
        return result


def _text_input(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def _emit_event(sequence: int, kind: str, message: str) -> int:
    payload = json.dumps(
        {"sequence": sequence, "kind": kind, "message": message},
        separators=(",", ":"),
    ).encode()
    print(f"TIN_TASK_EVENT={base64.b64encode(payload).decode()}", flush=True)
    return sequence + 1


def _normalized_item_event(method: str, item: dict[str, Any]) -> tuple[str, str] | None:
    item_type = str(item.get("type", ""))
    normalized = "".join(
        f"_{char.lower()}" if char.isupper() else char for char in item_type
    ).lstrip("_")
    if normalized in {"reasoning", "agent_message", "message"}:
        return None
    completed = method == "item/completed"
    if normalized in {"web_search", "web_search_call"}:
        return (
            ("web_search_completed", "Searched the web.")
            if completed
            else ("web_search_started", "Searching the web.")
        )
    if normalized in {"command_execution", "local_shell_call", "command"}:
        if not completed:
            return "project_command_started", "Working in the project workspace."
        failed = item.get("status") == "failed" or (
            isinstance(item.get("exitCode", item.get("exit_code")), int)
            and item.get("exitCode", item.get("exit_code")) != 0
        )
        return (
            ("project_command_failed", "A project command failed; Codex is adapting.")
            if failed
            else ("project_command_completed", "Completed a project command.")
        )
    if normalized == "file_change":
        if not completed:
            return "file_change_started", "Preparing project file changes."
        changes = item.get("changes")
        count = len(changes) if isinstance(changes, list) else 0
        noun = "file" if count == 1 else "files"
        return "file_change_completed", f"Updated {count or 'project'} {noun}."
    if normalized in {"mcp_tool_call", "dynamic_tool_call"}:
        return (
            ("project_tool_completed", "Finished using a project tool.")
            if completed
            else ("project_tool_started", "Using a project tool.")
        )
    if normalized in {"context_compaction", "context_compacted"} and completed:
        return "context_refreshed", "Refreshed the task context."
    if completed and normalized:
        return "task_step_completed", "Completed a task step."
    return None


def execute() -> int:
    context = _decode_context()
    instruction = str(context.get("instruction", "")).strip()
    transcript = context.get("transcript", [])
    if not instruction:
        raise RuntimeError("task instruction is missing")
    transcript_text = "\n".join(
        f"{str(item.get('source', 'unknown')).upper()}: {str(item.get('content', '')).strip()}"
        for item in transcript
        if isinstance(item, dict) and str(item.get("content", "")).strip()
    )
    prompt = (
        "You are executing one bounded Tin project task inside an isolated E2B workspace. "
        "Inspect the project before acting. You may use web search. Work only inside the current "
        "repository. If this checkout contains an earlier task checkpoint, compare it with the "
        f"latest origin/{os.environ['TIN_CANONICAL_BRANCH']} and reconcile the latest canonical "
        "project state before finalizing revised changes. Preserve the founder's reviewed intent. "
        "Do not expose secrets, hidden reasoning, raw terminal output, or sandbox "
        "details. If a material product choice is truly required, stop and ask one concise "
        "question. Otherwise complete the task. Your final structured message must summarize the "
        "outcome for the founder; Tin independently detects and reviews file changes before they "
        "reach canonical project state.\n\n"
        f"TASK:\n{instruction}"
    )
    if transcript_text:
        prompt += f"\n\nTASK TRANSCRIPT:\n{transcript_text}"

    command = ["/usr/local/bin/codex", "--search", "app-server", "--listen", "stdio://"]
    controller_cwd = "/home/user/.tin-lite/controller" if ISOLATED else WORKSPACE
    environment = dict(os.environ)
    if ISOLATED:
        for override in (
            'shell_environment_policy.inherit="none"',
            "features.hooks=false",
            "features.remote_plugin=false",
            "features.multi_agent=false",
            "agents.enabled=false",
            "features.apps=false",
        ):
            command[1:1] = ["-c", override]
        bypass = environment.get("NO_PROXY", "") + ",127.0.0.1,localhost"
        environment.update(NO_PROXY=bypass, no_proxy=bypass)
    proc = subprocess.Popen(  # noqa: S603 — fixed pinned Codex/controller configuration
        command,
        cwd=controller_cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env={
            key: value
            for key, value in environment.items()
            if key
            not in {
                "OPENAI_API_KEY",
                "CODEX_API_KEY",
                "TIN_LITE_LUNA_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "OPENROUTER_API_KEY",
                "FAL_KEY",
                "TIN_LITE_INTEGRATION_CREDENTIAL_KEY",
                "TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET",
                "TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH",
                "TIN_LITE_GITHUB_WEBHOOK_SECRET",
            }
        },
    )
    assert proc.stdout is not None
    incoming: queue.Queue[str | None] = queue.Queue()
    controls: queue.Queue[dict[str, Any]] = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, incoming), daemon=True).start()
    threading.Thread(target=_controls, args=(controls,), daemon=True).start()

    forced_outcome: str | None = None
    last_agent_message = ""
    pending_steers: dict[int, str] = {}
    event_sequence = 1
    environments = (
        {"environments": [{"environmentId": "tin-work", "cwd": WORKSPACE}]} if ISOLATED else {}
    )
    try:
        _request(
            proc,
            incoming,
            1,
            "initialize",
            {
                "clientInfo": {"name": "tin-lite", "title": "Tin", "version": "1.0.0"},
                "capabilities": {"experimentalApi": ISOLATED},
            },
        )
        _write(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        if ISOLATED:
            _request(
                proc,
                incoming,
                10,
                "environment/add",
                {
                    "environmentId": "tin-work",
                    "execServerUrl": "ws://127.0.0.1:8788",
                    "connectTimeoutMs": 10000,
                },
            )
            _request(proc, incoming, 11, "environment/info", {"environmentId": "tin-work"})
            ready = _request(
                proc, incoming, 12, "environment/status", {"environmentId": "tin-work"}
            )
            if ready.get("status") != "ready":
                raise RuntimeError("isolated task environment is not ready")
        thread_result = _request(
            proc,
            incoming,
            2,
            "thread/start",
            {
                "cwd": controller_cwd,
                **environments,
                "ephemeral": False,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "developerInstructions": (
                    "The E2B container is the external security boundary. Never access paths "
                    "outside the current project workspace."
                ),
            },
        )
        thread = thread_result.get("thread", thread_result)
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise RuntimeError("Codex app-server did not return a thread id")
        turn_result = _request(
            proc,
            incoming,
            3,
            "turn/start",
            {
                "threadId": thread_id,
                "input": _text_input(prompt),
                "cwd": controller_cwd,
                **environments,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"},
                "outputSchema": OUTPUT_SCHEMA,
            },
        )
        turn = turn_result.get("turn", turn_result)
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise RuntimeError("Codex app-server did not return a turn id")
        event_sequence = _emit_event(event_sequence, "turn_started", "Codex started the task turn.")

        while True:
            try:
                control = controls.get_nowait()
            except queue.Empty:
                control = None
            if control is not None:
                control_type = control.get("type")
                if control_type == "steer" and forced_outcome is None:
                    text = str(control.get("content", "")).strip()
                    if text:
                        steer_request_id = 1000 + int(control.get("sequence", 0))
                        entry_id = str(control.get("entry_id", ""))
                        if entry_id:
                            pending_steers[steer_request_id] = entry_id
                        _write(
                            proc,
                            {
                                "jsonrpc": "2.0",
                                "id": steer_request_id,
                                "method": "turn/steer",
                                "params": {
                                    "threadId": thread_id,
                                    "expectedTurnId": turn_id,
                                    "input": _text_input(text),
                                },
                            },
                        )
                elif control_type in {"pause", "stop"} and forced_outcome is None:
                    forced_outcome = str(control_type)
                    _write(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 9000,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        },
                    )

            try:
                line = incoming.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError(
                        "Codex app-server stopped before the task finished"
                    ) from None
                continue
            if line is None:
                raise RuntimeError("Codex app-server stopped before the task finished")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = message.get("id")
            if isinstance(response_id, int) and response_id in pending_steers:
                entry_id = pending_steers.pop(response_id)
                if "error" not in message:
                    print(f"TIN_TASK_STEERED={entry_id}", flush=True)
                continue
            if "id" in message and "method" in message:
                _write(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "interactive request unsupported"},
                    },
                )
                continue
            method = message.get("method")
            params = message.get("params", {})
            if method in {"item/started", "item/completed"} and isinstance(params, dict):
                item = params.get("item", {})
                if isinstance(item, dict):
                    event = _normalized_item_event(method, item)
                    if event is not None:
                        event_sequence = _emit_event(event_sequence, event[0], event[1])
            if method == "item/completed" and isinstance(params, dict):
                item = params.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    last_agent_message = str(item.get("text", ""))
            if method == "turn/completed" and isinstance(params, dict):
                completed_turn = params.get("turn", {})
                status = completed_turn.get("status") if isinstance(completed_turn, dict) else None
                if forced_outcome is None and status != "completed":
                    error = (
                        completed_turn.get("error") if isinstance(completed_turn, dict) else None
                    )
                    error_info = error.get("codexErrorInfo") if isinstance(error, dict) else None
                    category = (
                        json.dumps(error_info, separators=(",", ":"))[:160]
                        if error_info is not None
                        else "unknown"
                    )
                    raise RuntimeError(f"Codex task turn failed ({category})")
                event_sequence = _emit_event(
                    event_sequence,
                    "turn_completed",
                    "Finished the Codex turn. Saving the result.",
                )
                break

        if forced_outcome is not None:
            result = {
                "outcome": "paused" if forced_outcome == "pause" else "stopped",
                "summary": "Saved the current task checkpoint."
                if forced_outcome == "pause"
                else "Stopped by the founder.",
                "message": (
                    "The task is paused at a saved checkpoint."
                    if forced_outcome == "pause"
                    else "The task was stopped. No changes were applied."
                ),
                "question": None,
            }
        else:
            result = _task_result(json.loads(last_agent_message))
        with open(os.environ["TIN_TASK_RESULT_PATH"], "w", encoding="utf-8") as handle:
            json.dump(result, handle, separators=(",", ":"))
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _task_result(result):
    """Codex's final task message as Tin's result; a question-less pause asks a plain one."""
    if not isinstance(result, dict) or result.get("outcome") not in {"completed", "needs_input"}:
        raise RuntimeError("Codex returned an invalid task outcome")
    if result["outcome"] == "needs_input" and not (result.get("question") or "").strip():
        # Ask rather than lose the turn: the founder can still answer or stop.
        result["question"] = "How should I continue?"
    return result


def main() -> int:
    if not ISOLATED:
        return execute()
    if not os.environ.get("TIN_CODEX_API_URL"):
        raise RuntimeError("isolated tasks require API authentication")
    subprocess.run([*ISOLATION_HELPER, "prepare"], check=True)  # noqa: S603 — root-owned helper
    remote = subprocess.Popen(  # noqa: S603 — root-owned helper drops to author UID
        [*ISOLATION_HELPER, "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return execute()
    finally:
        subprocess.run([*ISOLATION_HELPER, "freeze"], check=True, timeout=20)  # noqa: S603
        try:
            remote.wait(timeout=5)
        except subprocess.TimeoutExpired:
            remote.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Bridge errors are deliberately authored above without model output, command output,
        # credentials, or request payloads, so the stage can be diagnosed without leaking data.
        print(
            f"task app-server bridge failed: {type(exc).__name__}: {str(exc)[:240]}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
