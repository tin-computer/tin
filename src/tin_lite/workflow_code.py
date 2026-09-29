"""Bounded code contract. Customer source is data until it reaches E2B."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass

from tin_lite.project_files import safe_project_file_path
from tin_lite.workflow_packages import relative_path, validate_package_input_schema
from tin_lite.workflow_services import ServiceBinding, service_bindings

EXECUTOR = "workflow.code"
RUNTIME = "python3.12.8-stdlib-v1"
POLICY = "bounded-code-v1"
MODEL_POLICY = "managed-code-model-v1"
# Explicit supported routes, using the existing trusted adapters and price card.
# A configured provider credential alone never admits an unpriced model.
MODEL_TARGETS = frozenset({("openai", "gpt-6-luna"), ("openai", "gpt-6-sol")})
ROUTE_KEYS = ("provider", "model", "max_calls", "max_input_bytes", "max_output_tokens")
MAX_FILE_BYTES = 64_000
MAX_PACKAGE_BYTES = 256_000
MAX_OUTPUT_BYTES = 64_000


@dataclass(frozen=True)
class CodeModelRoute:
    name: str
    provider: str
    model: str
    max_calls: int
    max_input_bytes: int
    max_output_tokens: int

    @property
    def router_key(self):
        return f"workflow.code:{self.provider}:{self.model}"


def supported_models() -> str:
    return ", ".join(f"{provider}/{model}" for provider, model in sorted(MODEL_TARGETS))


def model_routes(value):
    if not isinstance(value, dict) or len(value) > 4:
        raise ValueError("model_routes must declare at most four named routes")
    routes = []
    for name, route in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name)
            or not isinstance(route, dict)
        ):
            raise ValueError("invalid code model route")
        if set(route) != set(ROUTE_KEYS):
            raise ValueError(
                f"code model route {name!r} keys must be exactly {', '.join(ROUTE_KEYS)}"
            )
        if (
            not isinstance(route["provider"], str)
            or not isinstance(route["model"], str)
            or (route["provider"], route["model"]) not in MODEL_TARGETS
        ):
            raise ValueError(
                f"code model route {name!r} uses an unsupported or unpriced model; "
                f"supported provider/model pairs: {supported_models()}"
            )
        for field, lower, upper in (
            ("max_calls", 1, 4),
            ("max_input_bytes", 1024, 32_000),
            ("max_output_tokens", 64, 4096),
        ):
            if type(route[field]) is not int or not lower <= route[field] <= upper:
                raise ValueError(f"model {field} must be {lower}-{upper}")
        routes.append(CodeModelRoute(name=name, **route))
    if sum(r.max_calls for r in routes) > 8:
        raise ValueError("code workflows allow at most eight managed model calls")
    return tuple(routes)


@dataclass(frozen=True)
class CodeSpec:
    entrypoint: str
    files: tuple[str, ...]
    timeout_seconds: int
    output_path: str
    media_type: str
    max_bytes: int
    model_routes: tuple[CodeModelRoute, ...] = ()
    services: tuple[ServiceBinding, ...] = ()
    approved_article_input: str | None = None

    @property
    def policy(self):
        return MODEL_POLICY if self.model_routes else POLICY


def validate_code_definition(definition) -> CodeSpec:
    if definition.get("executor") != EXECUTOR or set(definition) - {
        "key",
        "title",
        "description",
        "version",
        "executor",
        "kind",
        "input_schema",
        "code",
        "human_review",
        "schedule_modes",
        "system",
        "prerequisites",
        "integration_requirements",
    }:
        raise ValueError("unsupported code workflow fields or capabilities")
    if definition.get("kind", "workflow") != "workflow":
        raise ValueError("code packages define workflows")
    modes = definition.get("schedule_modes")
    if (
        not isinstance(modes, list)
        or not all(isinstance(mode, str) for mode in modes)
        or "on_demand" not in modes
        or len(modes) != len(set(modes))
        or set(modes) - {"on_demand", "daily", "weekly"}
    ):
        raise ValueError("code schedules require on_demand and optional daily/weekly modes")
    validate_package_input_schema(definition.get("input_schema", {}))
    code = definition.get("code")
    if (
        not isinstance(code, dict)
        or set(code) - {"model_routes", "services", "approved_article"}
        != {"runtime", "entrypoint", "files", "timeout_seconds", "output"}
        or code["runtime"] != RUNTIME
    ):
        raise ValueError("unsupported code runtime contract")
    files = code["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise ValueError("code packages require 1-32 declared files")
    for path in files:
        relative_path(path)
        if not safe_project_file_path(path) or path == "workflow.json":
            raise ValueError("unsafe code resource path")
    if len(set(files)) != len(files):
        raise ValueError("duplicate code resource")
    entrypoint = relative_path(code["entrypoint"])
    if entrypoint not in files or not entrypoint.endswith(".py"):
        raise ValueError("entrypoint must be a declared Python file")
    timeout = code["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("code timeout must be 1-60 seconds")
    output = code["output"]
    if not isinstance(output, dict) or set(output) != {"kind", "path", "media_type", "max_bytes"}:
        raise ValueError("code output must declare one bounded project artifact")
    path = relative_path(output["path"])
    if (
        output["kind"] != "project.artifact"
        or not safe_project_file_path(path)
        or path == "wiki/INDEX.md"
        or path.split("/")[0] in {".tin-lite", "procedures", "registry", "workflow_packages"}
        or output["media_type"]
        not in {"text/markdown", "text/plain", "text/csv", "application/json"}
    ):
        raise ValueError("code output must be an ordinary project text artifact")
    maximum = output["max_bytes"]
    if type(maximum) is not int or not 1 <= maximum <= MAX_OUTPUT_BYTES:
        raise ValueError("code output exceeds its byte limit")
    article_input = approved_article_input(definition)
    return CodeSpec(
        entrypoint,
        tuple(files),
        timeout,
        path,
        output["media_type"],
        maximum,
        model_routes(code.get("model_routes", {})),
        service_bindings(code.get("services", {}), definition.get("integration_requirements")),
        article_input,
    )


def approved_article_input(definition):
    """One reviewed content.generate source; no arbitrary project-file reader."""
    value = definition.get("code", {}).get("approved_article")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"input"}:
        raise ValueError("approved_article must name one required UUID input")
    name = value["input"]
    schema = definition.get("input_schema", {})
    field = schema.get("properties", {}).get(name) if isinstance(name, str) else None
    if (
        definition.get("executor") != EXECUTOR
        or name == "project_id"
        or name not in schema.get("required", [])
        or not isinstance(field, dict)
        or field.get("type") != "string"
        or field.get("format") != "uuid"
        or definition.get("schedule_modes") != ["on_demand"]
    ):
        raise ValueError("approved_article requires a required UUID input and on-demand execution")
    return name


def validate_code_resources(spec, files):
    if set(files) != set(spec.files) or sum(map(len, files.values())) > MAX_PACKAGE_BYTES:
        raise ValueError("code package resources exceed their contract")
    for path, raw in files.items():
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise ValueError("code resource exceeds its byte limit")
        text = raw.decode("utf-8")
        if path.endswith(".py"):
            # Parse only. Never import, evaluate or execute uploaded source here.
            try:
                ast.parse(text, filename=path)
            except SyntaxError:
                raise ValueError(f"invalid Python syntax in {path}") from None


async def load_code_package(*, storage, repo_id, commit_sha, definition_path):
    from tin_lite.workflow_packages import load_workflow_source

    source = await load_workflow_source(
        storage=storage, repo_id=repo_id, commit_sha=commit_sha, definition_path=definition_path
    )
    if source.package_format is None:
        raise ValueError("code workflows require the versioned package layout")
    spec = validate_code_definition(source.definition)
    files = {
        path: await storage.read_workflow_resource(
            repo_id=repo_id, commit_sha=commit_sha, path=source.resource_paths[path]
        )
        for path in spec.files
    }
    validate_code_resources(spec, files)
    return source.definition, spec, files


def validate_code_result(raw: bytes, spec: CodeSpec) -> bytes:
    if len(raw) > spec.max_bytes * 6 + 2048:
        raise ValueError("code result envelope exceeds its bound")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "content"}
        or value["path"] != spec.output_path
        or not isinstance(value["content"], str)
    ):
        raise ValueError("code result must contain the declared path and text content")
    content = value["content"].encode("utf-8")
    if not content.strip() or len(content) > spec.max_bytes or b"\x00" in content:
        raise ValueError("code artifact is empty, invalid, or exceeds its byte limit")
    if spec.media_type == "application/json":
        json.loads(content)
    return content


def example_files(key="custom.order_report", *, model_steps=False, connections=False):
    """The shipped Tin-owned example and its private copy use the identical contract."""
    from pathlib import Path

    root = Path(__file__).with_name(
        "code_connection_example"
        if connections
        else "code_model_example"
        if model_steps
        else "code_example"
    )
    manifest = json.loads((root / "workflow.json").read_bytes())
    manifest["definition"]["key"] = key
    spec = validate_code_definition(manifest["definition"])
    prefix = f"workflow_packages/{key}/"
    return {
        prefix + "workflow.json": json.dumps(manifest, indent=2) + "\n",
        **{prefix + path: (root / path).read_text() for path in spec.files},
    }
