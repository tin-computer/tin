from __future__ import annotations

from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.workflow_inputs import (
    WORKFLOW_FORMAT_CHECKER,
    WorkflowInputError,
    normalize_workflow_inputs,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "product_url"],
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "product_url": {"type": "string", "format": "uri", "maxLength": 2000},
        "notes": {"type": "string", "maxLength": 500},
        "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
}


def _normalize(**inputs):
    return normalize_workflow_inputs(schema=SCHEMA, project_id=uuid4(), inputs=inputs)


@pytest.mark.parametrize(
    "inputs",
    [
        {"product_url": "https://example.com/", "notes": "a\x00b"},
        {"product_url": "https://example.com/", "topics": ["ok", "bad\x00"]},
        {"product_url": "https://example.com/", "notes\x00": "key"},
    ],
)
def test_nul_characters_are_rejected_before_they_reach_postgres(inputs) -> None:
    with pytest.raises(WorkflowInputError, match="NUL"):
        _normalize(**inputs)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "ftp://example.com/",
        "https://",
        "https:///path",
        "example.com",
        "https://exa mple.com/",
        "https://example.com/\n",
        "",
    ],
)
def test_uri_inputs_must_be_http_urls_with_a_host(url) -> None:
    with pytest.raises(WorkflowInputError, match="product_url"):
        _normalize(product_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://a.example/",
        "http://example.test/landing?utm_source=x#top",
        "https://www.stackradar.dev/tools/shipyard/?utm_source=x",
        "https://user:pw@shop.example/",
        "https://[2001:db8::1]:8443/x",
        "https://café.example/",
    ],
)
def test_uri_inputs_keep_accepting_ordinary_urls(url) -> None:
    assert _normalize(product_url=url)["product_url"] == url


def test_builtin_uri_fields_still_accept_their_documented_https_origins() -> None:
    checked = 0
    for workflow in BUILTIN_WORKFLOWS:
        properties = (workflow.input_schema or {}).get("properties") or {}
        for name, field in properties.items():
            if field.get("format") == "uri":
                checked += 1
                validator = Draft202012Validator(field, format_checker=WORKFLOW_FORMAT_CHECKER)
                assert validator.is_valid("https://example.com"), (workflow.key, name)
                assert not validator.is_valid("javascript:alert(1)"), (workflow.key, name)
    assert checked >= 5
