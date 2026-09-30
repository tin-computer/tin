"""One X drafting recipe, using the existing voice and composition workflows."""

from uuid import UUID

from tin_lite import x_style

KEY = "social.x_draft"
WORKFLOW_ID = UUID("13a31bac-6a89-4789-9d66-c0ab03a5da0d")
STEPS = {"style": x_style.KEY, "compose": "social.x_compose"}
POLICY = {"version": "x-draft-v1", "steps": STEPS, "missing_voice": "project_context"}
COMPOSE_FIELDS = ("direction", "post_count", "notes", "evidence_paths", "plan_path", "asset_paths")
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "direction": {
            "type": "string",
            "minLength": 8,
            "maxLength": 4000,
            "title": "What should this post say?",
            "x-tin-ui": {"control": "textarea"},
        },
        "post_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 6,
            "default": 1,
            "title": "Number of posts",
        },
        "notes": {
            "type": "string",
            "maxLength": 8000,
            "default": "",
            "title": "Current facts or preferences",
            "x-tin-ui": {"control": "textarea"},
        },
        "evidence_paths": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 512},
            "title": "Relevant project files",
        },
        "plan_path": {"type": "string", "maxLength": 512, "title": "Optional social plan"},
        "asset_paths": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 512},
            "title": "Existing images or video",
        },
        "supplied_samples": {
            "type": "string",
            "maxLength": 32000,
            "default": "",
            "title": "Your writing samples",
            "x-tin-ui": {"control": "textarea"},
        },
        "source_path": {
            "type": "string",
            "maxLength": 512,
            "default": "",
            "title": "Samples in project Files",
        },
        "preferences": {
            "type": "string",
            "maxLength": 4000,
            "default": "",
            "title": "Writing preferences",
            "x-tin-ui": {"control": "textarea"},
        },
    },
    "required": ["project_id", "direction"],
}


def check_inputs(inputs):
    x_style.validate_inputs(
        {name: inputs.get(name, "") for name in ("supplied_samples", "source_path", "preferences")}
    )


def matching_guide(guide, account):
    owner = x_style.guide_account(guide)
    return bool(owner and (owner == account if account else True))


async def facts(database, run):
    """The same small Postgres projection for HTTP and MCP; never reads Temporal."""
    prepared = await database.get_effect(f"x-draft:{run.id}:prepare")
    source = prepared.result if prepared and prepared.status == "completed" else {}
    steps = []
    for step, workflow_key in STEPS.items():
        receipt = await database.get_effect(f"x-draft:{run.id}:{step}")
        value = receipt.result if receipt and receipt.status == "completed" else {}
        child = await database.get_run(UUID(value["run_id"])) if value.get("run_id") else None
        template = await database.get_registry_workflow(workflow_key) if child else None
        if child and (
            child.project_id != run.project_id or not template or child.workflow_id != template.id
        ):
            raise ValueError("X drafting child does not belong to this recipe")
        steps.append(
            {
                "step": step,
                "workflow_key": workflow_key,
                "run_id": str(child.id) if child else None,
                "status": child.status.value if child else value.get("status", "not_started"),
                "artifact_path": child.artifact_path if child else None,
            }
        )
    return {"voice": source.get("voice"), "steps": steps}
