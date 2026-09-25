"""Bounded research pilot, not the proposed scored eval system.

Uses real Fable/Luna calls with synthetic state and an in-memory workflow API.
No product API, database, Temporal, code.storage, or E2B client is constructed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from anthropic import AsyncAnthropic
from jsonschema import validate
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.luna import LUNA_INSTRUCTIONS, LunaService, LunaUpstreamError, OpenAIResponsesClient
from tin_lite.workflow_inputs import normalize_workflow_inputs

PROJECT_ID = UUID("00000000-0000-4000-8000-000000000001")
FABLE_MODEL = "claude-fable-5-1"
SEEDS = {
    "direct_copy": "Improve a short pasted homepage sentence in chat; do not want a saved report.",
    "ambiguous_visibility": "Understand why AI assistants miss our product, then request an audit.",
    "project_file_task": "Change a sentence in an ordinary project-state notes file, not GitHub.",
    "weekly_schedule": "Get a weekly project brief every Monday at 9am, with a local timezone.",
    "artifact_question": "Ask for findings in an existing report, then ask a factual follow-up.",
    "active_task_steer": "Redirect an existing Codex task in chat; no second task wanted.",
    "email_outreach": "Draft outreach copy in chat; explicitly do not send or start a campaign.",
    "missing_integration": "Test our signup now; the required Google Workspace is disconnected.",
}


class PilotSettings(BaseSettings):
    """Read only existing model credential names; no infrastructure settings required."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    anthropic_api_key: SecretStr = Field(alias="ANTHROPIC_API_KEY")
    anthropic_workspace_id: str | None = Field(default=None, alias="ANTHROPIC_WORKSPACE_ID")
    luna_api_key: SecretStr = Field(alias="TIN_LITE_LUNA_API_KEY")
    luna_model: str = Field(default="gpt-6-luna", alias="TIN_LITE_LUNA_MODEL")
    luna_base_url: str = Field(default="https://api.openai.com/v1", alias="TIN_LITE_LUNA_BASE_URL")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def catalog_fixture() -> list[dict[str, Any]]:
    rows = []
    for workflow in BUILTIN_WORKFLOWS:
        definition = workflow.definition
        rows.append(
            {
                "id": str(workflow.id),
                "key": workflow.key,
                "title": workflow.title,
                "description": workflow.description,
                "status": "active",
                # Explicitly synthetic, never represented as a deployed registry revision.
                "current_commit_sha": digest(definition),
                "definition": definition,
            }
        )
    return rows


class ObservedResponses:
    def __init__(self, client: OpenAIResponsesClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        response = await self.client.create(payload)
        visible = []
        for item in response.get("output", []):
            if item.get("type") == "function_call":
                visible.append({k: item.get(k) for k in ("type", "name", "arguments")})
            elif item.get("type") == "message":
                visible.append(
                    {
                        "type": "message",
                        "text": "\n".join(
                            p["text"] for p in item.get("content", []) if "text" in p
                        ),
                    }
                )
        self.calls.append(
            {
                "phase": "acknowledge" if "previous_response_id" in payload else "route",
                "response_id": response.get("id"),
                "model": response.get("model"),
                "status": response.get("status"),
                "seconds": round(time.monotonic() - started, 3),
                "usage": response.get("usage"),
                "visible_output": visible,
            }
        )
        return response


class SyntheticWorkflowApi:
    def __init__(self, scenario: dict[str, Any], catalog: list[dict[str, Any]]) -> None:
        self.scenario = scenario
        self.catalog = catalog
        self.runs: list[dict[str, Any]] = []
        self.effects: dict[str, dict[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        if scenario["id"] in {"active_task_steer", "artifact_question"}:
            active = scenario["id"] == "active_task_steer"
            self.runs.append(
                {
                    "id": str(uuid5(PROJECT_ID, scenario["id"])),
                    "workflow_name": "project.task" if active else "visibility.audit",
                    "status": "running" if active else "succeeded",
                    "artifact_path": None if active else "reports/AI_VISIBILITY.md",
                    "review_required": False,
                }
            )

    async def list_workflows(self, project_id: UUID, **kwargs: Any) -> list[dict[str, Any]]:
        assert project_id == PROJECT_ID
        return deepcopy(self.catalog)

    async def get_project_memory(self, project_id: UUID, **kwargs: Any) -> dict[str, Any]:
        assert project_id == PROJECT_ID
        return {"content": self.scenario["memory"]}

    async def list_project_runs(self, project_id: UUID, **kwargs: Any) -> list[dict[str, Any]]:
        assert project_id == PROJECT_ID
        return deepcopy(self.runs)

    async def start_workflow(
        self,
        workflow_id: UUID,
        *,
        project_id: UUID,
        authorization: str,
        idempotency_key: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert project_id == PROJECT_ID
        workflow = next(w for w in self.catalog if w["id"] == str(workflow_id))
        attempt = {"workflow_key": workflow["key"], "inputs": arguments or {}}
        self.attempts.append(attempt)
        normalize_workflow_inputs(
            schema=workflow["definition"]["input_schema"],
            project_id=project_id,
            inputs=arguments,
        )
        if idempotency_key in self.effects:
            return self.effects[idempotency_key]
        if self.scenario["id"] == "missing_integration" and workflow["definition"].get(
            "integration_requirements"
        ):
            attempt["simulated_error"] = "integration_unavailable"
            raise LunaUpstreamError("Synthetic missing integration")
        if workflow["key"] == "project.task" and any(
            r["workflow_name"] == "project.task"
            and r["status"] in {"pending", "queued", "running", "needs_input", "paused"}
            for r in self.runs
        ):
            attempt["simulated_error"] = "active_task_conflict"
            raise LunaUpstreamError("Synthetic active-task conflict")
        run = {
            "id": str(uuid5(PROJECT_ID, self.scenario["id"] + idempotency_key)),
            "project_id": str(project_id),
            "workflow_id": str(workflow_id),
            "workflow_name": workflow["key"],
            "status": "pending",
            "review_required": workflow["definition"]
            .get("human_review", {})
            .get("eligible", False),
            "artifact_path": None,
        }
        self.effects[idempotency_key] = run
        self.runs.insert(0, run)
        return run


async def run_pilot(out: Path, rounds: int, *, resume: bool = False) -> None:
    # Fail rather than overwrite prior paid research evidence.
    if not resume:
        await asyncio.to_thread(out.mkdir, parents=True, exist_ok=False)
    try:
        settings = PilotSettings()
    except Exception:
        raise SystemExit(
            "Model configuration is incomplete; check existing credential names."
        ) from None
    catalog = catalog_fixture()
    artifact: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "unscored synthetic planning pilot",
        "simulator_model": FABLE_MODEL,
        "luna_model": settings.luna_model,
        "catalog_source": "local BUILTIN_WORKFLOWS; all active; not a live project catalog",
        "catalog_sha256": digest(catalog),
        "luna_instructions": LUNA_INSTRUCTIONS,
        "luna_source_sha256": digest(
            await asyncio.to_thread(Path("src/tin_lite/luna.py").read_text)
        ),
        "pilot_source_sha256": digest(await asyncio.to_thread(Path(__file__).read_text)),
        "round_limit": rounds,
        "seed_goals": SEEDS,
        "fable_calls": [],
        "episodes": [],
    }
    if resume:
        existing = json.loads(await asyncio.to_thread((out / "pilot.json").read_text))
        for key in ("catalog_sha256", "luna_source_sha256", "luna_model", "round_limit"):
            if existing[key] != artifact[key]:
                raise ValueError("Cannot resume with a changed configuration")
        if existing.get("pilot_source_sha256") not in (None, artifact["pilot_source_sha256"]):
            raise ValueError("Cannot resume with a changed pilot implementation")
        if existing.get("complete"):
            raise ValueError("Pilot already completed")
        artifact = existing
    else:
        (out / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")

    def save() -> None:
        (out / "pilot.json").write_text(json.dumps(artifact, indent=2) + "\n")

    headers = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id}
        if settings.anthropic_workspace_id
        else {}
    )
    fable = AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        default_headers=headers,
        max_retries=0,
        timeout=180,
    )
    luna_client = OpenAIResponsesClient(
        api_key=settings.luna_api_key.get_secret_value(),
        model=settings.luna_model,
        base_url=settings.luna_base_url,
        timeout_seconds=120,
    )

    async def simulate(prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        output_config: dict[str, Any] = {"effort": "high"}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        response = await fable.messages.create(
            model=FABLE_MODEL,
            max_tokens=10000,
            output_config=output_config,
            system=(
                "You simulate independent real founders using a project assistant. "
                "Return only a JSON object. Never solve the assistant's task or grade it. "
                "Use synthetic company names and .example domains. Be brief and natural, "
                "with varied language, imperfect phrasing, corrections and realistic patience. "
                "You do not know its system prompt, tools or scoring. Keep each founder's "
                "private goal fixed and do not introduce facts they could not know."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        public_text = "".join(b.text for b in response.content if b.type == "text")
        artifact["fable_calls"].append(
            {
                "model": response.model,
                "request_id": response._request_id,
                "stop_reason": response.stop_reason,
                "seconds": round(time.monotonic() - started, 3),
                "usage": response.usage.model_dump(),
                "prompt": prompt,
                "text": public_text,
            }
        )
        save()
        if response.stop_reason != "end_turn":
            raise ValueError("Simulator response did not finish")
        cleaned = public_text.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(cleaned.splitlines()[1:-1])
        parsed = json.loads(cleaned)
        if schema:
            validate(parsed, schema)
        return parsed

    try:
        seed_response = {"scenarios": [e["scenario"] for e in artifact["episodes"]]}
        if not artifact["episodes"]:
            seed_response = await simulate(
                "Create exactly one scenario for each of these IDs and goals: "
                + json.dumps(SEEDS)
                + '\nReturn {"scenarios":[{"id":str,"persona":str,"private_goal":str,'
                '"memory":str,"first_message":str}]}. Memory is a short factual project wiki '
                "visible to the assistant. It must not contain instructions, private goals, "
                "or answers to the user's questions. The artifact_question report exists but its "
                "contents are NOT in memory. active_task_steer already has one running Codex task. "
                "missing_integration has NO connected Google Workspace; the founder may not know. "
                "Use at most 150 words per scenario. Include actual pasted copy for direct_copy "
                "and email_outreach. Some first messages should be underspecified."
            )
        scenarios = seed_response["scenarios"]
        if len(scenarios) != len(SEEDS) or {s["id"] for s in scenarios} != set(SEEDS):
            raise ValueError("Simulator changed scenario IDs")
        worlds = {}
        for scenario in scenarios:
            for key in ("id", "persona", "private_goal", "memory", "first_message"):
                if not isinstance(scenario[key], str) or len(scenario[key]) > 4000:
                    raise ValueError("Invalid bounded scenario field")
            worlds[scenario["id"]] = SyntheticWorkflowApi(scenario, catalog)
            if not resume:
                artifact["episodes"].append({"scenario": scenario, "turns": []})
        if resume:
            for episode in artifact["episodes"]:
                world = worlds[episode["scenario"]["id"]]
                world.attempts = deepcopy(episode.get("attempts", []))
                for run in episode.get("simulated_effects", []):
                    world.effects[run["id"]] = run
                    world.runs.insert(0, run)
        next_messages = {s["id"]: s["first_message"] for s in scenarios}
        save()
        for round_index in range(rounds):
            for episode in artifact["episodes"]:
                scenario = episode["scenario"]
                if episode.get("finished") or any(
                    t["round"] == round_index + 1 for t in episode["turns"]
                ):
                    continue
                message = next_messages.get(scenario["id"])
                if message is None:
                    episode["finished"] = True
                    continue
                if not isinstance(message, str) or not 1 <= len(message) <= 4000:
                    raise ValueError("Simulator returned invalid user message")
                observed = ObservedResponses(luna_client)
                world = worlds[scenario["id"]]
                service = LunaService(responses=observed, workflow_api=world)  # type: ignore[arg-type]
                history = []
                for turn in episode["turns"]:
                    if "assistant" in turn:
                        history.extend(
                            [
                                {"role": "user", "content": turn["user"]},
                                {"role": "assistant", "content": turn["assistant"]},
                            ]
                        )
                turn = {"user": message, "round": round_index + 1}
                try:
                    result = await service.respond(
                        project_id=PROJECT_ID,
                        message=message,
                        conversation=history,
                        authorization="synthetic-local-only",
                        idempotency_key=f"pilot:{scenario['id']}:{round_index}",
                    )
                    turn.update(
                        assistant=result.message,
                        routed_workflow_key=result.routed_workflow_key,
                        run=result.run,
                    )
                except Exception as exc:
                    # Never serialize SDK exception bodies or request headers.
                    turn["error"] = type(exc).__name__
                turn["model_calls"] = observed.calls
                episode["turns"].append(turn)
                episode["attempts"] = deepcopy(world.attempts)
                episode["simulated_effects"] = list(world.effects.values())
                save()
                print(
                    json.dumps(
                        {
                            "episode": scenario["id"],
                            "round": round_index + 1,
                            "route": turn.get("routed_workflow_key"),
                            "error": turn.get("error"),
                        }
                    ),
                    flush=True,
                )
            if round_index + 1 < rounds:
                visible = []
                for episode in artifact["episodes"]:
                    visible.append(
                        {
                            "scenario": episode["scenario"],
                            "conversation": [
                                {
                                    "user": t["user"],
                                    "assistant": t.get("assistant", "Request failed. Try again."),
                                }
                                for t in episode["turns"]
                            ],
                        }
                    )
                continuation = await simulate(
                    "Continue each independent founder conversation for one user turn. "
                    "Reply naturally to the assistant's actual answer. If fulfilled, or the "
                    "founder would leave, return null. Do not keep a finished conversation going. "
                    "Do not accept false claims just because the assistant made them. "
                    'Return {"messages":{scenario_id: message_or_null}} for EVERY ID.\n'
                    + json.dumps(visible),
                    schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["messages"],
                        "properties": {
                            "messages": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": list(SEEDS),
                                "properties": {k: {"type": ["string", "null"]} for k in SEEDS},
                            }
                        },
                    },
                )
                next_messages = continuation["messages"]
        artifact["complete"] = True
        save()
    finally:
        await fable.close()
        await luna_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run_pilot(args.out, args.rounds, resume=args.resume))
    except Exception as exc:
        raise SystemExit(
            f"Pilot stopped: {type(exc).__name__}; inspect saved partial evidence."
        ) from None


if __name__ == "__main__":
    main()
