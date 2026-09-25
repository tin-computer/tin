"""One fixed recipe: plan, hold while the founder picks and connects, then set up."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tin_lite.domain import GROWTH_ONBOARDING_PLAN_PATH, GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME

KEY = "growth.onboarding"
STEPS = {"plan": GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME}
POLICY = {
    "version": "growth-onboarding-v2",
    "steps": STEPS,
    "hold": "level_and_connections_in_plan",
    "setup": "schedules_and_first_runs",
    "schedule": "manual_only",
}
PLAN_PATH = GROWTH_ONBOARDING_PLAN_PATH
PLAN_BLOCK = "tin-plan"
WORDS_BLOCK = "tin-words"

PRIORITY_DEFAULTS: dict[str, dict[str, str]] = {
    "main": {"founder_hours": "lots", "budget": "500_to_2000", "urgency": "weeks"},
    "side": {"founder_hours": "some", "budget": "under_500", "urgency": "two_months"},
    "fun": {"founder_hours": "min", "budget": "none", "urgency": "patient"},
}
FOUNDER_CHOICES: dict[str, dict[str, Any]] = {
    "priority": {
        "title": "How do you see this project",
        "description": (
            "Guess it from the codebase and session (commit cadence, pricing, waitlist); "
            "default side; say your guess in one line so the founder can correct it. "
            "main: my main project, I work on it "
            "every week, I can put some money behind it, I want results within a month. side: "
            "a serious side project, a few hours most weeks, money only where it clearly pays, "
            "results within a quarter are fine. fun: a fun project, I touch it when I feel like "
            "it, no budget, no deadline. Hours, budget and urgency default from it."
        ),
        "enum": ["unknown", "main", "side", "fun"],
    },
    "founder_hours": {
        "title": "Founder hours a week",
        "description": "Derived from priority unless known: 0-2 (min), 3-8 (some), 9+ (lots).",
        "enum": ["unknown", "min", "some", "lots"],
    },
    "budget": {
        "title": "Monthly budget outside Tin",
        "description": "Derived from priority unless known: none, under $500, $500-2,000, more.",
        "enum": ["unknown", "none", "under_500", "500_to_2000", "more"],
    },
    "urgency": {
        "title": "How soon results need to show",
        "description": "Derived from priority unless known: weeks, two months, or patient.",
        "enum": ["unknown", "weeks", "two_months", "patient"],
    },
    "outcome": {
        "title": "What would you most like to see happen in the next couple of months",
        "description": (
            "Guess it from the codebase and session; default signups; say your guess in one "
            "line so the founder can correct it (self-serve: paying customers; free tool: "
            "traffic or signups; pre-launch: a launch; services: a partner)."
        ),
        "enum": [
            "unknown",
            "paying_customers",
            "signups",
            "traffic",
            "launch",
            "partner",
            "other",
        ],
    },
}
HARD_NOS = [
    "no_paid_ads",
    "no_cold_email",
    "no_founder_posting",
    "no_discounting",
    "no_unbacked_claims",
]

SYSTEM_FIELDS: tuple[tuple[str, str, str], ...] = (
    (
        "system_hosting",
        "Website framework and hosting",
        "For example Next.js on Vercel, Webflow, WordPress on Kinsta.",
    ),
    (
        "system_repository",
        "Code repository",
        "Host and owner/repo, and which repository serves the site.",
    ),
    ("system_analytics", "Product analytics", "PostHog, GA4, Plausible, Mixpanel, none."),
    ("system_search_console", "Search Console", "Verified or not, and under which account."),
    (
        "system_mailbox",
        "Email and calendar",
        "Google Workspace, Gmail, Outlook; which mailbox would send outreach.",
    ),
    ("system_payments", "Payments and billing", "Stripe, Paddle, invoices, none."),
    ("system_crm", "CRM or lead list", "HubSpot, Attio, a spreadsheet, none."),
    ("system_support", "Support inbox or chat", "Where customers write when something breaks."),
    ("system_docs", "Documentation", "Where the docs live and whether they are in the repository."),
    ("system_newsletter", "Newsletter or lifecycle email", "Resend, Mailchimp, Loops, none."),
    ("system_ads", "Ad accounts", "Google, Meta, LinkedIn; anything running."),
)


def _input_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "project_id": {"type": "string", "format": "uuid"},
        "product_url": {
            "type": "string",
            "maxLength": 2000,
            "default": "",
            "title": "Product URL",
            "description": (
                "The landing page a stranger arrives at. Leave empty when there is no public "
                "site yet; the plan then works from the form and notes alone."
            ),
            "x-tin-ui": {"control": "text", "order": 10},
        },
    }
    for index, (key, title, description) in enumerate(SYSTEM_FIELDS):
        properties[key] = {
            "type": "string",
            "maxLength": 300,
            "default": "",
            "title": title,
            "description": f"{description} Never credentials.",
            "x-tin-ui": {"control": "text", "order": 20 + index},
        }
    properties["notes"] = {
        "type": "string",
        "maxLength": 2000,
        "default": "",
        "title": "Notes",
        "description": (
            "Anything else a stranger reading the site would not know. Never credentials."
        ),
        "x-tin-ui": {"control": "textarea", "order": 40},
    }
    properties["notes"]["maxLength"] = 4000
    properties["notes"]["title"] = "What you know"
    properties["notes"]["description"] = (
        "From the repository, its history and its configuration: what has been done for "
        "growth (pages, campaigns, emails, tools), what it produced, what was dropped and "
        "why, current customers; team size, how often it ships, whether the founder writes "
        "publicly. Never credentials."
    )
    for index, (key, choice) in enumerate(FOUNDER_CHOICES.items()):
        properties[key] = {
            "type": "string",
            "enum": list(choice["enum"]),
            "default": "unknown",
            "title": choice["title"],
            "description": choice["description"],
            "x-tin-ui": {"control": "select", "order": 50 + index},
        }
    properties["timezone"] = {
        "type": "string",
        "default": "UTC",
        "maxLength": 64,
        "title": "Timezone",
        "description": (
            "IANA timezone for schedules, from the founder's machine (America/New_York)."
        ),
        "x-tin-ui": {"control": "text", "order": 60},
    }
    properties["hard_nos"] = {
        "type": "array",
        "items": {"type": "string", "enum": list(HARD_NOS)},
        "default": [],
        "maxItems": len(HARD_NOS),
        "title": "Hard no's",
        "description": (
            "Ask the founder; suggest the ones the project's own rules imply (agent instruction "
            "files, README, house style)."
        ),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": ["project_id"],
    }


# The plan's contract, shared verbatim by the parent so one form serves both.
INPUT_SCHEMA = _input_schema()

SYSTEMS_HEADING = "what tin would run"
CONTROL_HEADING = "control"
CONNECTIONS_HEADING = "connections"
CONNECTION_DECISIONS = ("connected", "not_now")
SUGGESTED = "suggested"

_PICK = re.compile(r"^- \[(?P<mark>[ xX])\]\s+(?P<id>[a-z][a-z0-9_-]*)\b")
_MARK = re.compile(r"^(?P<lead>\s*- \[)[ xX](?P<tail>\].*)$")


def _sections(text: str) -> list[tuple[str, str]]:
    """Each line of the plan with the lower-cased `## ` heading it sits under ('' before any)."""
    out: list[tuple[str, str]] = []
    section = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
        out.append((section, line))
    return out


def _in(section: str, heading: str) -> bool:
    return section.startswith(heading)


def _ticked(mark: str) -> bool:
    return mark in {"x", "X"}


def plan_picks(text: str) -> tuple[list[str], list[str]]:
    """Return (ticked system ids, all system ids) from the plan's "What Tin would run" list."""
    picked: list[str] = []
    all_ids: list[str] = []
    for section, line in _sections(text):
        if not _in(section, SYSTEMS_HEADING):
            continue
        match = _PICK.match(line.strip())
        if match is None:
            continue
        all_ids.append(match.group("id"))
        if _ticked(match.group("mark")):
            picked.append(match.group("id"))
    return picked, all_ids


CONTROL_OPTIONS: dict[str, str] = {
    "pull_request": "Tin opens a pull request; nothing changes until you merge it.",
    "review_in_tin": (
        "Tin drafts; you approve each item in Decisions. Approved drafts stay in Tin unless "
        "GitHub pull-request delivery is configured. A pull request still needs your merge."
    ),
}
_CONTROL = re.compile(r"^- \[(?P<mark>[ xX])\]\s+control:\s*(?P<option>[a-z_]+)\b")


def plan_view(text: str) -> str:
    """The plan's "Tin's view" section, spoken to the founder by their agent."""
    match = re.search(r"^## Tin's view\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match:
        return ""
    lines = [" ".join(line.split()) for line in match.group(1).splitlines()]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def plan_control(text: str) -> str | None:
    """The founder's control choice from the plan's Control checklist, if ticked."""
    for section, line in _sections(text):
        if not _in(section, CONTROL_HEADING):
            continue
        match = _CONTROL.match(line.strip())
        if match and _ticked(match.group("mark")) and match.group("option") in CONTROL_OPTIONS:
            return match.group("option")
    return None


_CONNECTION = re.compile(r"^- \[(?P<mark>[ xX])\]\s+(?P<provider>[a-z]+\.[a-z_]+)\b(?P<rest>.*)$")


def _connection_state(mark: str, rest: str) -> tuple[str, str]:
    lowered = rest.lower()
    if _ticked(mark):
        return "connected", ""
    if "not now" in lowered:
        after = rest[lowered.index("not now") + len("not now") :]
        return "declined", after.lstrip(" :—-").strip()
    return "open", ""


def plan_connections(text: str) -> dict[str, dict[str, str]]:
    """Read the Connections checklist: provider key -> {state, note}.

    A ticked line is `connected`; an unticked line that says "not now" is `declined` with the
    text after it as the note; any other unticked line is `open`.
    """
    out: dict[str, dict[str, str]] = {}
    for section, line in _sections(text):
        if not _in(section, CONNECTIONS_HEADING):
            continue
        match = _CONNECTION.match(line.strip())
        if match is None:
            continue
        state, note = _connection_state(match.group("mark"), match.group("rest"))
        out[match.group("provider")] = {"state": state, "note": note}
    return out


def plan_readiness(text: str) -> dict[str, Any]:
    """What the plan records so far: the systems picked and offered, control, connections."""
    picked, offered = plan_picks(text)
    block = plan_block(text)
    return {
        "systems": chosen_systems(picked, offered),
        "offered": offered,
        "suggested": suggested_systems(block) if block else [],
        "control": plan_control(text),
        "connections": plan_connections(text),
    }


def _set_mark(line: str, ticked: bool) -> str:
    body = line.rstrip("\r\n")
    match = _MARK.match(body)
    assert match is not None
    return f"{match.group('lead')}{'x' if ticked else ' '}{match.group('tail')}{line[len(body) :]}"


def _connection_line(line: str, provider: str, rest: str, decision: str, reason: str) -> str:
    body = line.rstrip("\r\n")
    lead = body[: len(body) - len(body.lstrip())]
    lowered = rest.lower()
    head = rest[: lowered.index("not now")] if "not now" in lowered else rest
    head = head.rstrip(" :—-")
    if decision == "connected":
        return f"{lead}- [x] {provider}{head}{line[len(body) :]}"
    tail = f" — not now: {reason}" if reason else " — not now"
    return f"{lead}- [ ] {provider}{head}{tail}{line[len(body) :]}"


def apply_plan_picks(
    text: str, *, systems: list[str], control: str, connections: dict[str, tuple[str, str]]
) -> str:
    """Rewrite the plan's three checklists in place; every other byte of the plan is kept.

    The picked systems and the control are ticked and the others unticked. `systems` may be
    `["suggested"]`, which resolves to the systems the plan's block marks `suggested`. Each
    provider in `connections` maps to (decision, reason): `connected` ticks its line and drops
    any "not now" tail; `not_now` unticks it and writes " — not now: <reason>" so
    plan_connections reads the reason back. Providers not mentioned keep their line. Raises
    ValueError naming the valid ids when a pick does not match the plan; nothing changes then.
    """
    sections = _sections(text)
    offered = [
        m.group("id")
        for section, line in sections
        if _in(section, SYSTEMS_HEADING) and (m := _PICK.match(line.strip()))
    ]
    controls = [
        m.group("option")
        for section, line in sections
        if _in(section, CONTROL_HEADING)
        and (m := _CONTROL.match(line.strip()))
        and m.group("option") in CONTROL_OPTIONS
    ]
    providers = [
        m.group("provider")
        for section, line in sections
        if _in(section, CONNECTIONS_HEADING) and (m := _CONNECTION.match(line.strip()))
    ]
    if not offered:
        raise ValueError("The plan has no systems checklist to tick.")
    chosen = list(systems)
    if chosen == [SUGGESTED]:
        block = plan_block(text)
        chosen = suggested_systems(block) if block else []
        if not chosen:
            raise ValueError("The plan marks no system as suggested; name the systems.")
    if not chosen:
        raise ValueError(f"Pick at least one system; the plan offers {', '.join(offered)}.")
    unknown = [item for item in chosen if item not in offered]
    if unknown:
        raise ValueError(
            f"Unknown system {', '.join(repr(u) for u in unknown)}; the plan offers "
            f"{', '.join(offered)}."
        )
    if control not in CONTROL_OPTIONS or control not in controls:
        listed = ", ".join(controls) or "none"
        raise ValueError(f"Unknown control {control!r}; the plan offers {listed}.")
    for provider, (decision, _reason) in connections.items():
        if provider not in providers:
            listed = ", ".join(providers) or "none"
            raise ValueError(f"Unknown connection {provider!r}; the plan lists {listed}.")
        if decision not in CONNECTION_DECISIONS:
            raise ValueError(
                f"Connection decision for {provider} must be one of "
                f"{', '.join(CONNECTION_DECISIONS)}, not {decision!r}."
            )
    out: list[str] = []
    for section, line in sections:
        stripped = line.strip()
        if _in(section, SYSTEMS_HEADING) and (m := _PICK.match(stripped)):
            out.append(_set_mark(line, m.group("id") in chosen))
        elif _in(section, CONTROL_HEADING) and (m := _CONTROL.match(stripped)):
            out.append(_set_mark(line, m.group("option") == control))
        elif (
            _in(section, CONNECTIONS_HEADING)
            and (m := _CONNECTION.match(stripped))
            and m.group("provider") in connections
        ):
            decision, reason = connections[m.group("provider")]
            out.append(
                _connection_line(line, m.group("provider"), m.group("rest"), decision, reason)
            )
        else:
            out.append(line)
    return "".join(out)


async def current_plan_text(*, storage: Any, project: Any) -> tuple[str, str]:
    """The plan at the project's canonical HEAD, with that revision."""
    _paths, head = await storage.list_canonical_files(
        repo_id=project.state_repo_id, branch=project.canonical_branch
    )
    text = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id, commit_sha=head, path=PLAN_PATH
    )
    return text.decode("utf-8", "replace"), head


def plan_block(text: str) -> dict[str, Any] | None:
    """Return the machine block Tin sets up from, or None when the plan has none."""
    match = re.search(r"```" + PLAN_BLOCK + r"\s*\n(.*?)\n```", text, re.S)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("systems"), list) else None


async def system_facts(*, database: Any, project_id: UUID, run_id: UUID) -> dict[str, Any]:
    run = await database.get_run(run_id)
    if run is None or run.project_id != project_id or run.executor != KEY:
        raise LookupError("Growth onboarding run not found.")
    steps = []
    for step, key in STEPS.items():
        receipt = await database.get_effect(f"onboarding:{run_id}:step:{step}")
        value = receipt.result if receipt and receipt.status == "completed" else {}
        child = None
        if value.get("run_id"):
            child = await database.get_run(UUID(value["run_id"]))
            if child is None or child.project_id != project_id:
                raise ValueError("Onboarding child projection is unavailable.")
        steps.append(
            {
                "step": step,
                "workflow_key": key,
                "run_id": str(child.id) if child else None,
                "status": child.status.value if child else value.get("status", "not_started"),
                "artifact_path": child.artifact_path if child else None,
                "artifact_ref": child.artifact_ref if child else None,
                "canonical_commit_sha": child.canonical_commit_sha if child else None,
            }
        )
    return {
        "run_id": str(run.id),
        "project_id": str(project_id),
        "status": run.status.value,
        "steps": steps,
        "artifact_path": run.artifact_path,
        "artifact_ref": run.artifact_ref,
    }


WORKFLOW_EXPECTATIONS: dict[str, dict[str, str]] = {
    "visibility.audit": {
        "first": "about ten minutes",
        "lands": "Files, reports/AI_VISIBILITY.md",
        "watch": "whether AI answers start naming the product; expect movement in weeks, not days",
    },
    "organic.audit": {
        "first": "about fifteen minutes",
        "lands": "Files, the audit report and its findings",
        "watch": "the findings list; technical fixes and the keyword plan build on it",
    },
    "organic.keyword_plan": {
        "first": "about twenty minutes",
        "lands": "Files, the keyword inventory",
        "watch": "the content plan uses it within days",
    },
    "ads.assessment": {
        "first": "about fifteen minutes",
        "lands": "Files, reports/paid-ads/<run>/ASSESSMENT.md",
        "watch": "the verdict and the fix-before-spend list; nothing is spent on ads",
    },
    "ads.launch": {
        "first": "about ten minutes, then your approval",
        "lands": "Files, ads/google/<run>/PLAN.md and after approval RESULT.md",
        "watch": "the plan before approving; the campaign goes live in your Ads account",
    },
    "ads.monitor": {
        "first": "a few minutes on each scheduled day once a campaign is live",
        "lands": "Files, ads/google/<launch>/monitor/<run>.md",
        "watch": "proposals that need your approval; automatic changes are listed each day",
    },
    "content.plan": {
        "first": "the first weekly batch on its scheduled day",
        "lands": "My system, as an editable program",
        "watch": (
            "weekly batches contain briefs; content.generate is a separate "
            "run that drafts an article for review"
        ),
    },
    "content.generate": {
        "first": "after selecting an eligible brief from the program",
        "lands": "Decisions, as an article or editorial assessment",
        "watch": (
            "omit item_id to select the next eligible article; configured "
            "repository delivery follows approval and opens an unmerged PR"
        ),
    },
    "content.deliver": {
        "first": "after an approved source article and repository are selected",
        "lands": "your GitHub repository, as an unmerged pull request",
        "watch": "review and merge the PR; publication depends on your site",
    },
    "content.answer_page": {
        "first": "about ten minutes after a visibility audit",
        "lands": "Decisions, as a draft to review",
        "watch": "publish what you approve; search impressions follow in weeks",
    },
    "content.public_article": {
        "first": "about fifteen minutes",
        "lands": "Decisions, as a draft to review",
        "watch": "one article per run; cadence is yours",
    },
    "site.health_improve": {
        "first": "about an hour",
        "lands": "Files, plus an unmerged GitHub PR when a safe change is found",
        "watch": "review the PR or the no-change report; nothing deploys on its own",
    },
    "organic.technical_fix": {
        "first": "a pull request within the hour, after an audit",
        "lands": "your GitHub repository, unmerged",
        "watch": "one finding per run",
    },
    "outreach.email_shortlist": {
        "first": "about ten minutes",
        "lands": "Files, outreach/email/SHORTLIST.csv",
        "watch": "review the list before any campaign",
    },
    "outreach.email_campaign": {
        "first": "a snapshot in Decisions to approve",
        "lands": "sends after approval, replies in your mailbox",
        "watch": "replies over the following days; follow-ups are automatic",
    },
    "qa.signup_walkthrough": {
        "first": "about twenty minutes",
        "lands": "Files, reports/qa/signup",
        "watch": "every break in your signup, with evidence",
    },
    "qa.product_audit": {
        "first": "about an hour",
        "lands": "Files, reports/qa/audit",
        "watch": "what works, what is broken, what to improve",
    },
    "product.deep_dive": {
        "first": "about an hour",
        "lands": "project memory, the Feature map",
        "watch": "later runs know your product",
    },
    "product.code_map": {
        "first": "about twenty minutes",
        "lands": "project memory, the Code map",
        "watch": "later runs know your stack",
    },
    "project.weekly_brief": {
        "first": "on its scheduled day",
        "lands": "Files, and the chat",
        "watch": "what moved, what needs you",
    },
    "research.deep_dive": {
        "first": "about fifteen minutes",
        "lands": "Files, reports/RESEARCH_DEEP_DIVE.md",
        "watch": "one question answered with sources",
    },
    "creative.product_demo": {
        "first": "about thirty minutes",
        "lands": "Decisions, as a video to review",
        "watch": "post what you approve",
    },
    "creative.character": {
        "first": "a few minutes",
        "lands": "Decisions, as a character to review",
        "watch": "used by demo videos afterwards",
    },
    "project.task": {
        "first": "usually under an hour",
        "lands": "the dedicated task view, with approval before any project-file changes",
        "watch": "one task per run",
    },
    "competitor.watch": {
        "first": "about twenty minutes; the first run records a baseline",
        "lands": "Files, reports/competitor-watch/<run>.md",
        "watch": "most weeks say nothing changed; a report with changes names its response",
    },
    "qa.buyer_trust": {
        "first": "about fifteen minutes",
        "lands": "Files, reports/BUYER_TRUST.md",
        "watch": "the verdict and fixes; code fixes go to Improve site health",
    },
    "organic.error_surface": {
        "first": "about thirty minutes",
        "lands": "Files, reports/error-surface/<run>.md",
        "watch": "add it to the content plan as a context file",
    },
    "organic.mention_backlinks": {
        "first": "about fifteen minutes",
        "lands": "Files, reports/backlink-asks/<run>.md",
        "watch": "send the asks you like yourself; later weeks recheck for the link",
    },
    "outreach.paying_segment": {
        "first": "a few minutes",
        "lands": "Files, reports/outreach/PAYING_SEGMENT.md",
        "watch": "who keeps paying, and the inputs it hands to the shortlist and keyword plan",
    },
    "outreach.speaking_shortlist": {
        "first": "about twenty minutes",
        "lands": "Files, reports/outreach/speaking/<run>.md",
        "watch": "deadlines first; you submit the pitches",
    },
    "outreach.syllabus_placement": {
        "first": "about thirty minutes",
        "lands": "Files, reports/outreach/syllabus/<run>.md",
        "watch": "when each instructor next picks tools; you send the notes",
    },
    "outreach.marketplace_listings": {
        "first": "about twenty minutes, after the Code map",
        "lands": "Files, reports/outreach/marketplaces/<run>.md",
        "watch": "one to three filled-in listings to submit yourself",
    },
    "outreach.campus_events": {
        "first": "about twenty minutes",
        "lands": "Files, reports/outreach/campus-events/<run>.md",
        "watch": "one activation to pitch; nothing is booked or paid",
    },
    "content.release_announce": {
        "first": "a minute or two",
        "lands": "Files, reports/RELEASE_ANNOUNCE.md",
        "watch": "post and send the drafts you approve",
    },
    "growth.score_quiz": {
        "first": "a minute or two",
        "lands": "Files, reports/SCORE_QUIZ.md",
        "watch": "embed the widget you approve on your own site",
    },
    "product.analytics_brief": {
        "first": "about fifteen minutes",
        "lands": "Files, reports/analytics/<run>.md",
        "watch": "activation and trends on each scheduled day",
    },
}
DEFAULT_EXPECTATION = {
    "first": "within the hour",
    "lands": "Files",
    "watch": "the run in Activity",
}


def expectation(key: str) -> dict[str, str]:
    return WORKFLOW_EXPECTATIONS.get(key, DEFAULT_EXPECTATION)


def chosen_systems(picked: list[str], offered: list[str]) -> list[str]:
    """The ticked systems, in plan order."""
    return [item for item in offered if item in picked]


def suggested_systems(block: dict[str, Any]) -> list[str]:
    """The systems the plan marks as Tin's suggestion, in plan order."""
    return [
        str(item["id"])
        for item in block.get("systems", [])
        if isinstance(item, dict) and item.get(SUGGESTED) is True and item.get("id")
    ]


def picked_actions(block: dict[str, Any], systems: list[str]) -> list[dict[str, Any]]:
    """Flatten the picked systems' workflows into setup actions, in plan order, without repeats."""
    actions: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item_system in block.get("systems", []):
        if not isinstance(item_system, dict) or item_system.get("id") not in systems:
            continue
        workflows = item_system.get("workflows")
        if not isinstance(workflows, list):
            continue
        for item in workflows:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            weekdays = item.get("weekdays") or ([item["weekday"]] if item.get("weekday") else [])
            # Model-authored plans remain readable even when a field has the wrong shape.
            # The approval validator reports these rows instead of attempting setup.
            if not isinstance(weekdays, list) or not all(isinstance(day, str) for day in weekdays):
                continue
            signature = (
                item["key"],
                str(item.get("mode", "once")),
                tuple(weekdays),
                str(item.get("local_time", "09:00")),
            )
            if signature in seen:
                continue
            seen.add(signature)
            actions.append(
                {
                    "system": item_system.get("id"),
                    "system_name": str(item_system.get("name", item_system.get("id"))),
                    "key": item["key"],
                    "mode": str(item.get("mode", "once")),
                    "weekdays": weekdays,
                    "local_time": str(item.get("local_time", "09:00")),
                    "inputs": item.get("inputs") if isinstance(item.get("inputs"), dict) else {},
                }
            )
    return actions


def systems_details(block: dict[str, Any], systems: list[str]) -> dict[str, Any]:
    """Names, summaries and a merged outlook for the picked systems, with safe fallbacks."""
    names: list[str] = []
    summaries: list[str] = []
    outlook: dict[str, list[str]] = {"week": [], "month": [], "quarter": []}
    for item in block.get("systems", []):
        if not isinstance(item, dict) or item.get("id") not in systems:
            continue
        names.append(str(item.get("name", item.get("id"))))
        if str(item.get("summary", "")).strip():
            summaries.append(str(item["summary"]).strip())
        item_outlook = item.get("outlook") if isinstance(item.get("outlook"), dict) else {}
        for period in outlook:
            value = str(item_outlook.get(period, "")).strip()
            if value:
                outlook[period].append(value)
    return {
        "names": names,
        "summary": " ".join(summaries),
        "outlook": {period: " ".join(values) for period, values in outlook.items()},
    }


def ui_links(public_url: str, project_id: UUID) -> dict[str, str]:
    base = public_url.rstrip("/")
    return {
        "overview": f"{base}/chat?project={project_id}",
        "my_system": f"{base}/system?project={project_id}",
        "activity": f"{base}/activity?project={project_id}",
        "decisions": f"{base}/decisions?project={project_id}",
        "files": f"{base}/files?project={project_id}",
        "integrations": f"{base}/integrations?project={project_id}",
    }


WEEKDAY_NAMES = {
    "monday": "Monday",
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday": "Sunday",
}

CONTENT_DRAFT_KEYS = frozenset({"content.answer_page", "content.public_article"})


def _when(action: dict[str, Any]) -> str:
    days = [WEEKDAY_NAMES.get(str(d).lower(), str(d)) for d in action.get("weekdays") or []]
    time = str(action.get("local_time", "09:00"))
    if action.get("mode") == "daily":
        return f"every day at {time}"
    if days:
        return f"{_join(days)} at {time}"
    return "once, starting now"


def _local_date(value: Any, timezone: Any) -> str:
    """next_run_at is stored in UTC; the founder reads the date where the schedule runs.

    The onboarding timezone is free text, so a zone that does not resolve keeps the UTC date
    and a stored value that does not parse prints as stored; the report never fails on either.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:10]
    try:
        zone = ZoneInfo(str(timezone)) if timezone else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(zone).date().isoformat()


def _lands(action: dict[str, Any], delivery: dict[str, Any] | None) -> str:
    lands = expectation(action["key"])["lands"]
    if action["key"] in CONTENT_DRAFT_KEYS:
        mode = action.get("delivery_mode") or (delivery or {}).get("mode")
        where = (delivery or {}).get("repository") or "your repository"
        if mode == "github_pr":
            return f"Decisions, as a draft; your yes opens a pull request in {where}"
        if mode == "github_commit":
            return f"Decisions, as a draft; your yes publishes it to {where}"
        return "Decisions, as a draft; approved copy stays in Tin"
    return lands


def founder_words(setup: dict[str, Any], *, titles: dict[str, str]) -> dict[str, Any]:
    """What the founder hears once Tin is set up, in the two parts the agent treats apart.

    `quote` is Tin's own words, relayed as given: the win, each role as a benefit with its
    day and where it lands, and what is already under way. `relay` is the facts the agent
    tells the founder in its own words, one per item: the outlook, the control they kept,
    the two pages, what was left out and why, and the invitation to ask for more.
    """
    business = str(setup.get("business") or "Your project")
    actions = setup["actions"]
    links = setup["links"]
    details = setup.get("details") or {"names": [], "summary": "", "outlook": {}}
    delivery = setup.get("delivery")
    started = [a for a in actions if a.get("status") == "started"]
    scheduled = [a for a in actions if a.get("status") == "scheduled"]
    not_running = [a for a in actions if a.get("status") in {"declined", "blocked", "skipped"}]
    roles = len(scheduled)
    incomplete = [
        a
        for a in actions
        if a.get("status") in {"blocked", "skipped", "declined"}
        or a.get("first_run_status") == "blocked"
        or a.get("delivery_error")
    ]

    def title(action: dict[str, Any]) -> str:
        return titles.get(action["key"], action["key"])

    lines: list[str] = []
    if incomplete:
        lines.append(
            f"Setup is partial for {business}: {len(incomplete)} workflow(s) "
            "were left out or need attention."
        )
    if roles:
        lines.append(
            f"{business} now has a marketing system running: {roles} "
            f"{'role' if roles == 1 else 'roles'} on your calendar"
            + (f", {setup['timezone']} time." if setup.get("timezone") else ".")
        )
    elif started:
        lines.append(f"{business} has its first Tin runs under way.")
    else:
        lines.append(f"Tin could not start anything for {business} yet; here is why.")
    if details.get("summary") and not incomplete:
        lines.append(details["summary"])
    lines.append("")
    for a in scheduled:
        lines.append(
            f"- {_when(a)[0].upper()}{_when(a)[1:]}: {title(a)}. Lands in {_lands(a, delivery)}."
        )
        if a.get("first_run_status") == "blocked":
            lines.append(
                f"  The schedule is active, but its first run was not admitted: {a['reason']}"
            )
    already = [a for a in started]
    if already:
        lines.append("")
        lines.append(
            "Already under way: "
            + _join(
                [
                    f"{title(a).lower()} (first result in {expectation(a['key'])['first']})"
                    for a in already
                ]
            )
            + "."
        )
    quote = "\n".join(lines).strip()

    relay: list[str] = []
    outlook = details.get("outlook") or {}
    expect = [
        f"In a week: {outlook['week']}" if outlook.get("week") else "",
        f"In a month: {outlook['month']}" if outlook.get("month") else "",
        f"In three months: {outlook['quarter']}" if outlook.get("quarter") else "",
    ]
    expect = [item for item in expect if item]
    if expect and not incomplete:
        relay.append(" ".join(expect))
    control = setup.get("control")
    if control in CONTROL_OPTIONS:
        relay.append(f"Your control: {CONTROL_OPTIONS[control]}")
    relay.append(
        f"Two pages are yours: My system ({links['my_system']}), every workflow with its runs, "
        f"and Decisions ({links['decisions']}), anything waiting for your yes."
    )
    relay.append(
        f"Reports arrive in Files ({links['files']}). Open Tin to check results; "
        "email and Slack result notifications are not available."
    )
    for a in not_running:
        if a.get("status") == "declined":
            note = f" ({a['note']})" if a.get("note") else ""
            relay.append(
                f"Left out by your choice: {title(a)} needs {a.get('provider', 'an integration')} "
                f"you did not connect{note}."
            )
        else:
            relay.append(f"Waiting: {title(a)}. {a.get('reason') or 'See My system.'}")
    relay.append(
        "Tell me anything you do by hand for marketing and I will have Tin build it as a "
        "workflow; you will see it appear in My system. Once the first result is in, want to "
        f"talk through how Tin can help {business} grow?"
    )
    return {"quote": quote, "relay": relay}


def founder_message(setup: dict[str, Any], *, titles: dict[str, str]) -> str:
    """The handshake as one text, for the written report: the quote, then each relayed fact
    as a paragraph of its own."""
    words = founder_words(setup, titles=titles)
    return "\n\n".join([words["quote"], *words["relay"]]).strip()


def report_message(text: str) -> str:
    """The founder message at the top of a written report, up to its first `## ` heading."""
    body = text.split("\n", 1)[1] if text.startswith("# ") else text
    return body.split("\n## ", 1)[0].strip()


def report_words(text: str) -> dict[str, Any]:
    """The quote and relay a written report carries in its `tin-words` block.

    Reports written before the block existed carry the handshake as one text; it comes back
    as the quote, the way those clients were told to treat it.
    """
    match = re.search(r"```" + WORDS_BLOCK + r"\s*\n(.*?)\n```", text, re.S)
    if match is not None:
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("quote"), str):
            relay = value.get("relay")
            return {
                "quote": value["quote"],
                "relay": [str(item) for item in relay] if isinstance(relay, list) else [],
            }
    return {"quote": report_message(text), "relay": []}


def render_report(setup: dict[str, Any], *, titles: dict[str, str]) -> str:
    """The closing report: the founder message first, then the plain record."""
    actions = setup["actions"]
    details = setup.get("details") or {"names": [], "summary": "", "outlook": {}}
    names = details.get("names") or []
    scheduled = [a for a in actions if a.get("status") == "scheduled"]
    started = [a for a in actions if a.get("status") == "started"]
    delivery = setup.get("delivery")
    timezone = setup.get("timezone")

    def title(action: dict[str, Any]) -> str:
        return titles.get(action["key"], action["key"])

    heading = _join(names) if names else "your systems"
    partial = any(
        a.get("status") in {"blocked", "skipped", "declined"}
        or a.get("first_run_status") == "blocked"
        or a.get("delivery_error")
        for a in actions
    )
    label = "Tin setup needs attention" if partial else "Tin is set up"
    lines = [f"# {label}: {heading}", "", founder_message(setup, titles=titles), ""]
    lines += ["## What runs", ""]
    if not scheduled and not started:
        lines.append("Nothing could start; see above.")
    for a in scheduled:
        lines.append(
            f"- **{title(a)}**, {_when(a)}; next on {_local_date(a.get('next_run_at'), timezone)}. "
            f"Lands in {_lands(a, delivery)}."
        )
    for a in started:
        first = expectation(a["key"])["first"]
        lines.append(f"- **{title(a)}**, once, running now; first result in {first}.")
    if delivery:
        mode = delivery.get("mode")
        lines += ["", "## How drafts ship", ""]
        if mode == "github_pr":
            lines.append(
                f"Approved drafts open a pull request in {delivery.get('repository')} under "
                f"{delivery.get('path_pattern', 'content/blog/{slug}.md')}; you merge them."
            )
        elif mode == "github_commit":
            lines.append(f"Approved drafts publish straight to {delivery.get('repository')}.")
        elif mode == "partial":
            lines.append(
                "Pull-request delivery was saved for only some workflows. "
                "See each workflow's destination above; the others keep drafts in Tin."
            )
        else:
            lines.append(
                "Approved drafts stay in Tin. Connect the website repository and configure "
                "pull-request delivery to send approved copy to GitHub. "
                "Nothing merges automatically."
            )
    lines += ["", f"Plan revision: `{setup['plan_revision']}`.", ""]
    words = founder_words(setup, titles=titles)
    lines += [
        "```" + WORDS_BLOCK,
        json.dumps({"quote": words["quote"], "relay": words["relay"]}, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def apply_priority(inputs: dict[str, Any]) -> dict[str, Any]:
    """Fill hours, budget and urgency from the priority answer where they are unknown."""
    filled = dict(inputs)
    defaults = PRIORITY_DEFAULTS.get(str(filled.get("priority", "unknown")))
    if defaults:
        for key, value in defaults.items():
            if filled.get(key) in (None, "", "unknown"):
                filled[key] = value
    return filled
