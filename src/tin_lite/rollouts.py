"""Codex rollout transcripts captured from sandboxes for operator debugging.

A rollout is the JSONL file Codex writes per thread under
``~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl``. The trusted
switchboard pulls these files out of a sandbox before killing it, redacts every secret
it handed the sandbox, and stores them in Postgres. They never enter code.storage,
Temporal history, Luna, MCP responses, or project memory.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tin_lite.redaction import redact_text, redact_token_shapes

ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"(?P<thread>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.jsonl$"
)


@dataclass(frozen=True)
class RolloutFile:
    path: str
    filename: str
    thread_id: str | None
    content: bytes
    size_bytes: int
    truncated: bool = False
    redactions: int = 0


@dataclass(frozen=True)
class RolloutCapture:
    sandbox_id: str
    files: tuple[RolloutFile, ...]
    skipped: int = 0
    error: str | None = None


def parse_rollout_filename(name: str) -> tuple[str, str] | None:
    """Return ``(timestamp, thread_id)`` for a Codex rollout filename, else ``None``."""
    match = ROLLOUT_FILENAME_RE.match(name)
    if match is None:
        return None
    return match.group("timestamp"), match.group("thread").lower()


def sandbox_secrets(
    *,
    canonical_auth_header: str,
    ephemeral_auth_header: str,
    proxy_url: str | None,
    run_tools_grant: str | None = None,
    extra: Iterable[str] = (),
) -> tuple[str, ...]:
    """Every secret the switchboard handed a sandbox, in the forms it could be echoed."""
    values: set[str] = set()
    for header in (canonical_auth_header, ephemeral_auth_header):
        values.add(header)
        blob = header.rpartition(" ")[2]
        values.add(blob)
        try:
            decoded = base64.b64decode(blob, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        values.add(decoded)
        values.add(decoded.partition(":")[2])
    if proxy_url:
        values.add(proxy_url)
        parts = urlsplit(proxy_url)
        if parts.password:
            values.add(parts.password)
            values.add(f"{parts.username or ''}:{parts.password}")
    if run_tools_grant:
        values.add(run_tools_grant)
    values.update(extra)
    return tuple(sorted((value for value in values if len(value) >= 4), key=len, reverse=True))


def redact_rollout(content: bytes, secrets: Sequence[str]) -> tuple[bytes, int]:
    """Redact a rollout: exact secrets first, then token shapes. Always returns UTF-8."""
    text = content.decode("utf-8", errors="replace")
    text, count = redact_text(text, secrets)
    text, shapes = redact_token_shapes(text)
    return text.encode("utf-8"), count + shapes


def render_rollout_trace(content: bytes, *, max_output_chars: int = 2000) -> Iterator[str]:
    """Render a rollout as a readable transcript, tolerating malformed lines."""
    malformed = 0
    for raw_line in content.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(entry, dict):
            malformed += 1
            continue
        stamp = _clock(entry.get("timestamp"))
        kind = str(entry.get("type", ""))
        payload = entry.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        for line in _render_entry(kind, payload, max_output_chars=max_output_chars):
            yield f"{stamp} {line}"
    if malformed:
        yield f"# skipped {malformed} malformed line(s)"


def _render_entry(kind: str, payload: dict[str, Any], *, max_output_chars: int) -> list[str]:
    if kind == "session_meta":
        lines = [
            "# session "
            f"{payload.get('id') or payload.get('session_id')} cwd={payload.get('cwd')} "
            f"cli={payload.get('cli_version')} originator={payload.get('originator')}"
        ]
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            lines.extend(_block("DEVELOPER (instructions)", instructions, max_output_chars))
        return lines
    if kind == "turn_context":
        sandbox = payload.get("sandbox_policy")
        sandbox_summary = (
            sandbox.get("mode") or sandbox.get("type") if isinstance(sandbox, dict) else sandbox
        )
        return [
            f"# turn model={payload.get('model')} approval={payload.get('approval_policy')} "
            f"sandbox={sandbox_summary} cwd={payload.get('cwd')}"
        ]
    if kind == "response_item":
        return _render_response_item(payload, max_output_chars=max_output_chars)
    if kind == "event_msg":
        event = str(payload.get("type", ""))
        if event in {"task_started", "task_complete", "agent_message", "error", "turn_aborted"}:
            message = payload.get("message") or payload.get("last_agent_message") or ""
            return _block(f"EVENT {event}", str(message), max_output_chars)
        return []
    if kind == "compacted":
        return ["# context compacted"]
    return [f"# {kind}/{payload.get('type')}"]


def _render_response_item(payload: dict[str, Any], *, max_output_chars: int) -> list[str]:
    item_type = str(payload.get("type", ""))
    if item_type == "message":
        role = str(payload.get("role", "unknown")).upper()
        return _block(role, _joined_text(payload.get("content")), max_output_chars)
    if item_type == "reasoning":
        text = _joined_text(payload.get("summary"))
        return _block("REASONING", text, max_output_chars) if text else []
    if item_type == "function_call":
        name = payload.get("name")
        label = f"CALL {name}#{payload.get('call_id')}"
        arguments = payload.get("arguments")
        return _block(label, _shell_arguments(name, arguments), max_output_chars)
    if item_type == "custom_tool_call":
        label = f"TOOL {payload.get('name')}#{payload.get('call_id')}"
        return _block(label, _stringify(payload.get("input")), max_output_chars)
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        label = ("OUTPUT" if item_type == "function_call_output" else "TOOL OUTPUT") + (
            f" #{payload.get('call_id')}"
        )
        output = payload.get("output")
        if isinstance(output, dict) and "output" in output:
            output = output["output"]
        return _block(label, _stringify(output), max_output_chars)
    if item_type == "local_shell_call":
        action = payload.get("action")
        command = action.get("command") if isinstance(action, dict) else None
        return _block("SHELL", _stringify(command), max_output_chars)
    if item_type == "web_search_call":
        action = payload.get("action")
        query = action.get("query") if isinstance(action, dict) else None
        return _block("SEARCH", _stringify(query), max_output_chars)
    return [f"# response_item/{item_type}"]


def _shell_arguments(name: Any, arguments: Any) -> str:
    if isinstance(arguments, str) and name in {"shell", "exec_command", "container.exec"}:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        if isinstance(parsed, dict):
            command = parsed.get("cmd") or parsed.get("command")
            if isinstance(command, list):
                return " ".join(str(part) for part in command)
            if isinstance(command, str):
                return command
    return _stringify(arguments)


def _joined_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _block(label: str, text: str, max_output_chars: int) -> list[str]:
    text = text.strip()
    if len(text) > max_output_chars:
        text = f"{text[:max_output_chars]}… [truncated {len(text) - max_output_chars} chars]"
    if not text:
        return [f"{label}:"]
    # Keep the first line on the label line so a grep over labels still shows content;
    # the rest follows indented.
    first, _, rest = text.partition("\n")
    lines = [f"{label}: {first}"]
    if rest:
        lines.extend(f"    {line}" for line in rest.splitlines())
    return lines


def _clock(timestamp: Any) -> str:
    if isinstance(timestamp, str) and "T" in timestamp:
        return timestamp.split("T", 1)[1][:8]
    return "--:--:--"
