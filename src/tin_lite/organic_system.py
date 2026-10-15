"""One fixed recipe, not executable category metadata or a user-authored graph."""

from datetime import date

from tin_lite.content_plan import DURATIONS
from tin_lite.organic_audit import MARKETS, public_site
from tin_lite.schedules import WEEKDAYS

KEY = "organic.traffic_system"
WEBSITE_KEY = "website.change"
# The recipe's two writer steps go through website.change, the one workflow that edits the
# site: the technical step with the latest audit's fixes (`source: audit`), the delivery step
# with the approved draft (`source: content_draft`).
STEPS = {
    "audit": "organic.audit",
    "keywords": "organic.keyword_plan",
    "technical": WEBSITE_KEY,
    "content": "content.plan",
    "draft": "content.generate",
    "delivery": WEBSITE_KEY,
}
# v6 (Emre, 10/1) is the oldest recipe that still runs; runs pinned to v1-v5 started workflows
# that left the codebase and fail at preparation. It drafts and schedules weekly articles,
# falls back to the latest saved plan, and refreshes existing pages weekly.
WEBSITE_POLICY = {
    "version": "organic-traffic-v6",
    "steps": STEPS,
    "max_technical_findings": 1,
    "schedule": "weekly_articles",
    "content_fallback": "latest_saved_plan",
    "refresh": "weekly_refresh",
    "site_writer": WEBSITE_KEY,
}
# v7 also measures the site each week. After the audit it runs the traffic snapshot and then
# Page decisions as child runs, before the content plan reads both, and saves each as a weekly
# schedule that runs ahead of the weekly page refresh: snapshot, then decisions, then refresh.
# Onboarding no longer installs the two on their own. Everything else is v6's.
SNAPSHOT_KEY = "organic.traffic_snapshot"
DECISIONS_KEY = "organic.content_efficacy"
MEASURE_STEPS = {"snapshot": SNAPSHOT_KEY, "decisions": DECISIONS_KEY}
MEASUREMENT_POLICY = {
    **WEBSITE_POLICY,
    "version": "organic-traffic-v7",
    "measurement": "weekly_measurement",
}
# v8: the content step's plan runs its planning agent (content.plan 1.0.0), so the plan's share
# of the system's spending pool grows to the agent's ceiling (service_pricing). The steps and
# their order are v7's.
POLICY = {**MEASUREMENT_POLICY, "version": "organic-traffic-v8", "content_planner": "agent"}
POLICIES = (WEBSITE_POLICY, MEASUREMENT_POLICY, POLICY)
REFRESH_KEY = "content.refresh"
# The executor each child must have; any other step's executor is its own key.
CHILD_EXECUTORS = {
    "content.generate": "codex.procedure",
    WEBSITE_KEY: "codex.procedure",
    SNAPSHOT_KEY: "workflow.code",
    DECISIONS_KEY: "workflow.code",
}


def policy_steps(policy):
    if policy in POLICIES:
        return STEPS
    raise ValueError("Unsupported organic system policy.")


def child_executor(workflow_key):
    return CHILD_EXECUTORS.get(workflow_key, workflow_key)


def runs_recipe(policy):
    """Whether a run pinned to this policy drafts, schedules and delivers with this code."""
    return policy in POLICIES


def measures_pages(policy):
    """Whether this recipe runs and schedules the traffic snapshot and Page decisions (v7)."""
    return policy in (MEASUREMENT_POLICY, POLICY)


INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "site_url": {
            "type": "string",
            "title": "Public website",
            "format": "uri",
            "pattern": "^https://",
            "maxLength": 500,
        },
        "market": {"type": "string", "title": "Buyer market", "enum": list(MARKETS)},
        "buyer_context": {
            "type": "string",
            "title": "Product and buyers",
            "minLength": 20,
            "maxLength": 2000,
        },
        "start_date": {
            "type": "string",
            "title": "Content plan starts",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
        },
        "duration": {
            "type": "string",
            "title": "Content plan duration",
            "enum": list(DURATIONS),
            "default": "6_months",
        },
        "keyword_max_cost_usd": {
            "type": "number",
            "title": "Keyword research limit (USD)",
            "minimum": 2,
            "maximum": 25,
            "default": 2,
        },
        "technical_fix": {
            "type": "boolean",
            "title": "Fix what the audit found",
            "default": True,
            "description": "Opens one pull request with the audit's fixes in the repository "
            "selected on GitHub, or the one named below; repository CI may run. Skipped "
            "without GitHub.",
        },
        "content_delivery": {
            "type": "string",
            "title": "Article delivery",
            "enum": ["auto", "draft_only"],
            "default": "auto",
            "description": "With GitHub connected, open a PR after draft approval. "
            "Otherwise keep the Markdown draft in Tin. Nothing is merged or published.",
        },
        "expected_repository": {
            "type": "string",
            "title": "GitHub owner/repository",
            "default": "",
            "maxLength": 140,
            "description": "Leave empty to use the repository selected on GitHub.",
        },
        "repository_serves_site": {
            "type": "boolean",
            "default": False,
            "title": "This repository serves the audited website",
            "description": "Needed only with a repository named here: the repository "
            "selected on GitHub is the one the founder chose for the site.",
        },
        "article_weekdays": {
            "type": "array",
            "title": "Draft the next article on",
            "items": {"type": "string", "enum": list(WEEKDAYS)},
            "uniqueItems": True,
            "maxItems": 7,
            "default": ["tuesday"],
            "description": "After the first article, Tin drafts the next planned article on "
            "these days, one review at a time. Leave empty to keep drafting on demand.",
        },
        "article_local_time": {
            "type": "string",
            "title": "Drafting time",
            "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$",
            "default": "10:00",
        },
    },
    "required": ["project_id", "site_url", "market", "buyer_context", "start_date"],
}


def check_inputs(inputs):
    public_site(inputs["site_url"])
    date.fromisoformat(inputs["start_date"])
    if inputs["market"] not in MARKETS or inputs.get("duration", "6_months") not in DURATIONS:
        raise ValueError("Choose a supported market and content-plan duration.")
    weekdays = inputs.get("article_weekdays", [])
    if len(set(weekdays)) != len(weekdays) or any(day not in WEEKDAYS for day in weekdays):
        raise ValueError("Choose each drafting weekday once, by its lowercase English name.")


async def system_facts(*, database, project_id, run_id):
    run = await database.get_run(run_id)
    if run is None or run.project_id != project_id or run.executor != KEY:
        raise LookupError("Organic traffic system run not found.")
    prepared = await database.get_effect(f"traffic:{run_id}:prepare")
    policy = (prepared.result or {}).get("policy") if prepared else None
    # A run keeps the steps its pinned policy recorded, including retired recipes; before
    # preparation it shows the current recipe's steps.
    selected_steps = (policy or {}).get("steps") or ({} if prepared else STEPS)
    steps = []
    for step, key in selected_steps.items():
        receipt = await database.get_effect(f"traffic:{run_id}:step:{step}")
        value = receipt.result if receipt and receipt.status == "completed" else {}
        child = None
        if value.get("run_id"):
            from uuid import UUID

            child = await database.get_run(UUID(value["run_id"]))
            if child is None or child.project_id != project_id:
                raise ValueError("System child projection is unavailable.")
            if step == "draft":
                # A revision replaces the reviewable copy, not the program item. Follow
                # the same durable lineage used by Decisions, including while it runs.
                current = await database.pool.fetchval(
                    "SELECT id FROM workflow_runs WHERE project_id=$1 AND workflow_id=$2 "
                    "AND (id=$3 OR review_root_run_id=$3) "
                    "ORDER BY review_version DESC LIMIT 1",
                    project_id,
                    child.workflow_id,
                    child.id,
                )
                if current:
                    child = await database.get_run(current)
        steps.append(
            {
                "step": step,
                "workflow_key": key,
                "run_id": str(child.id) if child else None,
                "status": child.status.value if child else value.get("status", "not_started"),
                "reason": value.get("reason"),
                "artifact_path": child.artifact_path if child else None,
                "artifact_ref": child.artifact_ref if child else None,
                "canonical_commit_sha": child.canonical_commit_sha if child else None,
                "project_workflow_id": str(child.project_workflow_id)
                if child and child.project_workflow_id
                else None,
            }
        )
    weekly = await database.get_effect(f"traffic:{run_id}:weekly")
    measurement = {}
    for step in MEASURE_STEPS:
        receipt = await database.get_effect(f"traffic:{run_id}:measure:{step}")
        if receipt and receipt.status == "completed" and receipt.result:
            value = dict(receipt.result)
            if value.get("run_id"):
                from uuid import UUID

                child = await database.get_run(UUID(value["run_id"]))
                if child is not None and child.project_id == project_id:
                    value["run_status"] = child.status.value
                    value["artifact_path"] = child.artifact_path
            measurement[step] = value
    return {
        "run_id": str(run.id),
        "project_id": str(project_id),
        "status": run.status.value,
        "steps": steps,
        # The saved weekly drafting configuration, when this recipe includes one.
        "weekly_articles": weekly.result if weekly and weekly.status == "completed" else None,
        # v7's weekly traffic snapshot and Page decisions: each saved schedule and its run now.
        "measurement": measurement or None,
        "artifact_path": run.artifact_path,
        "artifact_ref": run.artifact_ref,
    }
