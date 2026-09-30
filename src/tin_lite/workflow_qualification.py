"""Shared, non-executing qualification contract for authored and contributed packages.

An author proposes cases and a rubric. Only trusted run evidence supplies measurements.
This report is evidence for review, never an activation, publication or billing decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tin_lite.billing_contracts import digest
from tin_lite.code_models import model_terms, validate_output_schema
from tin_lite.codex_api_pricing import api_terms
from tin_lite.community import validate_files
from tin_lite.workflow_code import validate_code_definition
from tin_lite.workflow_inputs import normalize_workflow_inputs
from tin_lite.workflow_packages import relative_path

MAX_BYTES = 512_000
CASE_PROJECT = UUID("00000000-0000-4000-8000-000000000001")
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Expectations(Closed):
    contains: list[Text] = Field(default_factory=list, max_length=20)
    excludes: list[Text] = Field(default_factory=list, max_length=20)
    json_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def schema_is_bounded(self):
        if self.json_schema is not None:
            validate_output_schema(self.json_schema)
            Draft202012Validator.check_schema(self.json_schema)
        return self


class Criterion(Closed):
    id: Identifier
    question: Text


class Case(Closed):
    id: Identifier
    description: Text
    inputs: dict[str, Any]
    expected_status: Literal["succeeded", "failed"] = "succeeded"
    expect: Expectations = Field(default_factory=Expectations)


class Qualification(Closed):
    version: Literal[1]
    assumptions: list[Text] = Field(default_factory=list, max_length=12)
    cost_drivers: list[Text] = Field(default_factory=list, max_length=12)
    effects: list[Text] = Field(default_factory=list, max_length=12)
    cases: list[Case] = Field(min_length=1, max_length=12)
    rubric: list[Criterion] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def identifiable_checks(self):
        for entries in (self.cases, self.rubric):
            if len({item.id for item in entries}) != len(entries):
                raise ValueError("case and rubric IDs must be unique")
        for case in self.cases:
            if case.expected_status == "succeeded" and not (
                case.expect.contains
                or case.expect.excludes
                or case.expect.json_schema is not None
                or self.rubric
            ):
                raise ValueError("each successful case needs assertions or a quality rubric")
        return self


class Candidate(Closed):
    format: Literal["tin-workflow-candidate-v1"]
    files: dict[str, str] = Field(min_length=1, max_length=64)
    qualification: Qualification
    limitations: list[Text] = Field(default_factory=list, max_length=12)


def read_json(raw: bytes):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("qualification input exceeds its byte limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def qualification_path(key):
    return relative_path(f"workflow_evals/{key}/qualification.json")


async def inspect_candidate(raw: bytes):
    candidate = Candidate.model_validate(read_json(raw))
    files = {relative_path(path): text.encode("utf-8") for path, text in candidate.files.items()}
    manifests = [path for path in files if path.endswith("/workflow.json")]
    if len(manifests) != 1:
        raise ValueError("candidate must contain exactly one workflow package")
    report = await check_package(files, manifests[0], candidate.qualification)
    # These are proposed edits, not writes. Existing commit/revision checks still apply.
    changes = [{"operation": "upsert", "path": p, "content": c} for p, c in candidate.files.items()]
    changes.append(
        {
            "operation": "upsert",
            "path": qualification_path(report["workflow"]),
            "content": candidate.qualification.model_dump_json(indent=2) + "\n",
        }
    )
    return {
        "changes": changes,
        "qualification": report,
        "author_limitations": candidate.limitations,
    }


async def check_package(files, definition_path, qualification: Qualification):
    definition, fingerprint = await validate_files(files, definition_path=definition_path)
    for case in qualification.cases:
        normalize_workflow_inputs(
            schema=definition["input_schema"], project_id=CASE_PROJECT, inputs=case.inputs
        )
    executor = definition["executor"]
    code = validate_code_definition(definition) if executor == "workflow.code" else None
    terms = model_terms(definition) if code else api_terms(definition)
    model_free = bool(code is not None and not code.metered)
    report = {
        "format": "tin-workflow-qualification-v1",
        "workflow": definition["key"],
        "package_digest": fingerprint,
        "qualification_digest": digest(qualification.model_dump()),
        "shape": {
            "status": "passed",
            "executor": executor,
            "output": definition.get("code", definition.get("procedure"))["output"],
        },
        "contract": {
            "inputs": definition["input_schema"],
            "prerequisites": definition.get("prerequisites", []),
            "schedule_modes": definition["schedule_modes"],
            "human_review": definition.get("human_review"),
            "integrations": definition.get("integration_requirements", []),
            "services": definition.get("code", definition.get("procedure")).get("services", {}),
            "model_routes": [asdict(route) for route in code.model_routes] if code else [],
        },
        "cost": {
            "scope": "llm_and_agent_usage",
            "basis": "computed_zero" if model_free else "unmeasured",
            "expected_range_usd": ["0", "0"] if model_free else None,
            "configured_ceiling_usd": str(Decimal(terms["maximum_nanos"]) / 1_000_000_000),
            "rate_card": terms["rate_card"],
            "author_cost_drivers": qualification.cost_drivers,
            "note": (
                "Configured bounds are not measured estimates. Live admission and "
                "each run's pinned budget remain authoritative. Connected-provider "
                "and infrastructure costs are outside this estimate."
            ),
        },
        "safety": {
            "status": "review_required",
            "author_declared_effects": qualification.effects,
            "review": [
                "Compare source and declared effects; a valid manifest is not a security audit.",
                "Check integration scopes, idempotency and approvals before live evaluation.",
                "Review eligibility after execution cannot prevent an earlier external effect.",
            ],
        },
        "evaluation": {
            "status": "not_run",
            "cases": [{"id": case.id, "status": "not_run"} for case in qualification.cases],
            "rubric": [item.model_dump() for item in qualification.rubric],
            "quality_review": "pending" if qualification.rubric else "not_requested",
        },
        "assumptions": qualification.assumptions,
        "limitations": [
            "Static checks do not execute candidate code or establish output quality.",
            (
                "Author-proposed cases and rubric require review; passing them is "
                "not proof of general correctness."
            ),
            "This report never activates, publishes, reprices or updates a workflow.",
        ],
    }
    return report


def assess_output(case: Case, *, status: str, content: bytes | None):
    checks = [{"check": "run_status", "passed": status == case.expected_status}]
    if case.expected_status == "succeeded":
        checks.append({"check": "artifact_available", "passed": content is not None})
    if content is not None:
        if len(content) > 1_000_000:
            raise ValueError("evaluation artifact exceeds its byte limit")
        text = content.decode("utf-8")
        for kind, entries in (
            ("contains", case.expect.contains),
            ("excludes", case.expect.excludes),
        ):
            for index, phrase in enumerate(entries):
                checks.append(
                    {"check": f"{kind}:{index}", "passed": (phrase in text) == (kind == "contains")}
                )
        if case.expect.json_schema is not None:
            try:
                value = read_json(content)
                valid = Draft202012Validator(case.expect.json_schema).is_valid(value)
            except (ValueError, RecursionError):
                valid = False
            checks.append({"check": "json_schema", "passed": valid})
    elif case.expect.contains or case.expect.excludes or case.expect.json_schema is not None:
        checks.append({"check": "assertions_have_output", "passed": False})
    return {"status": "passed" if all(c["passed"] for c in checks) else "failed", "checks": checks}


def add_evidence(report, qualification, samples):
    """Samples are assembled by the trusted service, never supplied by an author."""
    if not samples:
        return report
    cases = []
    for case in qualification.cases:
        selected = [sample for sample in samples if sample["case_id"] == case.id]
        prices = [Decimal(s["model_cost_usd"]) for s in selected if s["model_cost_usd"] is not None]
        # Never pool different models/price contracts into one apparent estimate.
        cards = {s["pricing_digest"] for s in selected}
        comparable = len(cards) == 1 and len(prices) == len(selected) and bool(prices)
        cases.append(
            {
                "id": case.id,
                "description": case.description,
                "status": "not_run"
                if not selected
                else "failed"
                if any(s["assessment"]["status"] == "failed" for s in selected)
                else "passed",
                "samples": selected,
                "cost": {
                    "basis": "measured_sample_range"
                    if comparable
                    else "unmeasured_or_incomparable",
                    "sample_count": len(selected),
                    "priced_sample_count": len(prices),
                    "expected_range_usd": [str(min(prices)), str(max(prices))]
                    if comparable
                    else None,
                    "observed_failures": sum(s["status"] != "succeeded" for s in selected),
                    "note": (
                        "Observed range for these exact case inputs, not a "
                        "confidence interval or a guarantee for other inputs. "
                        "Failures and cache behavior affect cost."
                    ),
                },
            }
        )
    report["evaluation"].update(
        status="failed"
        if any(c["status"] == "failed" for c in cases)
        else "incomplete"
        if any(c["status"] == "not_run" for c in cases)
        else "assertions_passed",
        cases=cases,
    )
    if samples and report["cost"]["basis"] != "computed_zero":
        report["cost"]["basis"] = "see_per_case_evidence"
    return report


def content_digest(raw):
    return hashlib.sha256(raw).hexdigest()
