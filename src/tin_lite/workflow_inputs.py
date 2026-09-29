from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


class WorkflowInputError(ValueError):
    """A workflow input or published input schema is outside Tin's contract."""


def client_input_schema(definition: dict[str, Any]) -> dict[str, Any]:
    """The shared HTTP/MCP invocation schema excludes server-bound project identity."""
    schema = deepcopy(definition.get("input_schema", {}))
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("properties"), dict):
        schema["properties"].pop("project_id", None)
    if isinstance(schema.get("required"), list):
        schema["required"] = [key for key in schema["required"] if key != "project_id"]
    return schema


_SUPPORTED_UI_CONTROLS = {
    "string": {"text", "textarea", "select"},
    "boolean": {"segmented"},
    "integer": {"counter", "number"},
    "number": {"counter", "number"},
    "array": set(),
}


def validate_input_schema(schema: dict[str, Any]) -> None:
    """Validate the JSON Schema and the deliberately small UI-renderable subset."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise WorkflowInputError(f"workflow input schema is invalid: {exc.message}") from exc
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise WorkflowInputError(
            "workflow input schema must be a closed object with additionalProperties false"
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise WorkflowInputError("workflow input schema must define object properties")
    project = properties.get("project_id")
    if not isinstance(project, dict) or project.get("type") != "string":
        raise WorkflowInputError("workflow input schema must define the bound project_id")
    supported = {"string", "boolean", "integer", "number", "array"}
    for name, field in properties.items():
        if not isinstance(field, dict) or field.get("type") not in supported:
            raise WorkflowInputError(f"workflow input {name} uses an unsupported field shape")
        ui = field.get("x-tin-ui")
        if ui is not None:
            if not isinstance(ui, dict):
                raise WorkflowInputError(f"workflow input {name} x-tin-ui must be an object")
            unknown = set(ui) - {"control", "order"}
            if unknown:
                raise WorkflowInputError(
                    f"workflow input {name} x-tin-ui contains unsupported hints: "
                    f"{', '.join(sorted(unknown))}"
                )
            control = ui.get("control")
            if control is not None and control not in _SUPPORTED_UI_CONTROLS[field["type"]]:
                raise WorkflowInputError(
                    f"workflow input {name} cannot use the {control!r} control"
                )
            if control == "select" and not isinstance(field.get("enum"), list):
                raise WorkflowInputError(f"workflow input {name} select controls require an enum")
            order = ui.get("order")
            if order is not None and (not isinstance(order, int) or isinstance(order, bool)):
                raise WorkflowInputError(f"workflow input {name} x-tin-ui order must be an integer")
        if field.get("type") == "array":
            items = field.get("items")
            if not isinstance(items, dict) or items.get("type") != "string":
                raise WorkflowInputError(
                    f"workflow input {name} must be an array of strings for dashboard editing"
                )


def normalize_workflow_inputs(
    *,
    schema: dict[str, Any],
    project_id: UUID,
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind project identity, apply top-level defaults, and validate user inputs."""
    validate_input_schema(schema)
    normalized = deepcopy(inputs or {})
    if "project_id" in normalized:
        raise WorkflowInputError("project_id is bound by Tin and cannot be supplied as input")
    properties = schema["properties"]
    for name, field in properties.items():
        if name != "project_id" and name not in normalized and "default" in field:
            normalized[name] = deepcopy(field["default"])
    candidate = {"project_id": str(project_id), **normalized}
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(candidate)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path)
        label = f"workflow input {location}" if location else "workflow input"
        raise WorkflowInputError(f"{label}: {exc.message}") from exc
    return normalized


_TRUE = {"true", "yes", "y", "on", "1"}
_FALSE = {"false", "no", "n", "off", "0"}
_LIST_SEPARATOR = re.compile(r"[,\n]")
_MAX_LIST_TEXT = 20_000


def _json_list(text: str) -> list[Any] | None:
    """A list written as JSON: '["a", "b"]', or a JSON string that holds one."""
    value: Any = text
    for _ in range(2):  # A model sometimes encodes the list, then encodes that text again.
        if not isinstance(value, str):
            break
        body = value.strip()
        if not body.startswith(("[", '"')) or len(body) > _MAX_LIST_TEXT:
            return None
        try:
            value = json.loads(body)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def _text_list(text: str) -> list[Any]:
    """Read a list the plan wrote as text: JSON first, then comma or newline separated."""
    parsed = _json_list(text)
    if parsed is not None:
        return [
            item.strip() if isinstance(item, str) else item
            for item in parsed
            if not isinstance(item, str) or item.strip()
        ]
    body = text.strip()
    quoted = body.startswith("[") and body.endswith("]")
    if quoted:
        # Not valid JSON (single quotes, a stray comma): drop the brackets and item quotes.
        body = body[1:-1]
    parts = (part.strip() for part in _LIST_SEPARATOR.split(body))
    if quoted:
        parts = (part.strip("\"'").strip() for part in parts)
    return [part for part in parts if part]


def _number(value: Any, integer: bool) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace("_", "")
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer():
            value = int(value)
        elif integer:
            return None
    return value if isinstance(value, int | float) else None


def coerce_schema_inputs(
    schema: dict[str, Any], values: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Type textual values (e.g. model-written plan inputs) by their top-level schema.

    Converts integer, number and boolean strings, reads a string for an array of strings as a
    JSON list first (also a JSON string holding one) and otherwise splits it on commas and
    newlines, and clamps to minimum/maximum and maxItems. An unparseable value is
    dropped with a note so the schema default applies. Strings, enums and unknown fields are
    left to the caller and the schema validator. Pure: no I/O; the input is not mutated.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    fixed = dict(values)
    if not isinstance(properties, dict):
        return fixed, []
    notes: list[str] = []
    for name, value in values.items():
        prop = properties.get(name)
        kind = prop.get("type") if isinstance(prop, dict) else None
        if kind in {"integer", "number"}:
            number = _number(value, kind == "integer")
            if number is None:
                del fixed[name]
                notes.append(f"{name} dropped; {value!r} is not a {kind}")
                continue
            low, high = prop.get("minimum"), prop.get("maximum")
            if isinstance(low, int | float) and number < low:
                notes.append(f"{name} raised to its minimum {low}; the plan wrote {value!r}")
                number = low
            elif isinstance(high, int | float) and number > high:
                notes.append(f"{name} lowered to its maximum {high}; the plan wrote {value!r}")
                number = high
            fixed[name] = number
        elif kind == "boolean" and not isinstance(value, bool):
            text = str(value).strip().lower()
            if text in _TRUE or text in _FALSE:
                fixed[name] = text in _TRUE
            else:
                del fixed[name]
                notes.append(f"{name} dropped; {value!r} is not true or false")
        elif kind == "array":
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            if isinstance(value, str) and items.get("type") == "string":
                value = _text_list(value)
            if not isinstance(value, list):
                del fixed[name]
                notes.append(f"{name} dropped; {value!r} is not a list")
                continue
            limit = prop.get("maxItems")
            if isinstance(limit, int) and len(value) > limit:
                notes.append(f"{name} cut to its first {limit} items")
                value = value[:limit]
            fixed[name] = value
    return fixed, notes
