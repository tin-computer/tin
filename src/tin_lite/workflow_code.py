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
# An output file name may carry the run's date and a slug the package picks, so repeated
# runs keep separate, readable files. Nothing else is substituted.
OUTPUT_PLACEHOLDERS = {"{date}": r"\d{4}-\d{2}-\d{2}", "{slug}": r"[a-z0-9]+(?:-[a-z0-9]+)*"}
MAX_OUTPUT_SLUG = 80


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


@dataclass(frozen=True)
class EvidenceSlot:
    name: str
    input: str
    workflow_key: str
    max_bytes: int
    required: bool


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
    evidence: tuple[EvidenceSlot, ...] = ()

    @property
    def paid_services(self) -> tuple[ServiceBinding, ...]:
        """Bindings to services Tin buys per call (managed_services.CALL_CEILING_USD)."""
        from tin_lite.managed_services import paid

        return tuple(service for service in self.services if paid(service.provider_key))

    @property
    def metered(self) -> bool:
        """Model steps or paid managed reads: the run is funded per operation from credits."""
        return bool(self.model_routes or self.paid_services)

    @property
    def policy(self):
        return MODEL_POLICY if self.metered else POLICY


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
        or set(modes) - {"on_demand", "daily", "weekly", "monthly"}
    ):
        raise ValueError(
            "code schedules require on_demand and optional daily, weekly or monthly modes"
        )
    validate_package_input_schema(definition.get("input_schema", {}))
    code = definition.get("code")
    if (
        not isinstance(code, dict)
        or set(code) - {"model_routes", "services", "approved_article", "evidence"}
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
        or not safe_project_file_path(_sample_output_path(path))
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
    evidence = evidence_specs(definition)
    if article_input is not None and any(slot.input == article_input for slot in evidence):
        raise ValueError("approved article and evidence slots must use different inputs")
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
        evidence,
    )


def approved_article_input(definition):
    """Legacy reviewed content.generate input; preserve existing definition semantics."""
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


def evidence_specs(definition) -> tuple[EvidenceSlot, ...]:
    """Legacy approved-output slots; optionality comes from the pinned input schema."""
    value = definition.get("code", {}).get("evidence")
    if value is None:
        return ()
    if (
        definition.get("executor") != EXECUTOR
        or definition.get("schedule_modes") != ["on_demand"]
        or not isinstance(value, dict)
        or not 1 <= len(value) <= 4
    ):
        raise ValueError("code evidence requires 1-4 named on-demand source slots")
    schema = definition.get("input_schema", {})
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    slots = []
    used_inputs = set()
    for name, raw in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name)
            or name == "approved_article"
            or not isinstance(raw, dict)
            or set(raw) != {"kind", "input", "workflow_key", "max_bytes"}
            or raw["kind"] != "approved_output"
        ):
            raise ValueError("invalid code evidence slot")
        input_name = raw["input"]
        field = properties.get(input_name) if isinstance(input_name, str) else None
        if (
            not isinstance(input_name, str)
            or input_name == "project_id"
            or input_name in used_inputs
            or not isinstance(field, dict)
            or field.get("type") != "string"
            or field.get("format") != "uuid"
            or not isinstance(raw["workflow_key"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", raw["workflow_key"])
            or type(raw["max_bytes"]) is not int
            or not 1 <= raw["max_bytes"] <= 64_000
        ):
            raise ValueError("evidence slots need distinct UUID inputs and bounded producers")
        used_inputs.add(input_name)
        slots.append(
            EvidenceSlot(
                name, input_name, raw["workflow_key"], raw["max_bytes"], input_name in required
            )
        )
    return tuple(slots)


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


def _sample_output_path(template: str) -> str:
    """Check a declared output path; placeholders may appear once each, in the file name."""
    head, _, name = template.rpartition("/")
    for placeholder in OUTPUT_PLACEHOLDERS:
        if template.count(placeholder) > 1 or placeholder in head:
            raise ValueError("code output placeholders belong once in the file name")
    if re.search(r"[{}]", re.sub("|".join(map(re.escape, OUTPUT_PLACEHOLDERS)), "", name)):
        raise ValueError("code output supports only {date} and {slug} placeholders")
    return template.replace("{date}", "2026-01-01").replace("{slug}", "sample")


def output_path_allowed(spec: CodeSpec, path: object, created_at=None) -> bool:
    """Whether one concrete output path is what this run's declared output allows."""
    if not isinstance(path, str):
        return False
    template = spec.output_path
    if not any(placeholder in template for placeholder in OUTPUT_PLACEHOLDERS):
        return path == template
    if "{date}" in template and created_at is None:
        return False
    pattern = re.escape(template)
    for placeholder, expression in OUTPUT_PLACEHOLDERS.items():
        pattern = pattern.replace(re.escape(placeholder), f"(?P<{placeholder[1:-1]}>{expression})")
    match = re.fullmatch(pattern, path)
    return bool(
        match
        and ("{date}" not in template or match["date"] == created_at.date().isoformat())
        and ("{slug}" not in template or len(match["slug"]) <= MAX_OUTPUT_SLUG)
        and safe_project_file_path(path)
    )


def code_result_path(raw: bytes) -> str:
    """The concrete path a result names; validate_code_result checks it against the contract."""
    return json.loads(raw)["path"]


def validate_code_result(raw: bytes, spec: CodeSpec, *, created_at=None) -> bytes:
    if len(raw) > spec.max_bytes * 6 + 2048:
        raise ValueError("code result envelope exceeds its bound")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "content"}
        or not output_path_allowed(spec, value["path"], created_at)
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
