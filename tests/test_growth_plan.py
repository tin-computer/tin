"""Start here plan as an LLM flow: offline fixtures, including unusable model results."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from test_procedure_publication import publication_db as publication_db

from tin_lite import growth_plan as plan
from tin_lite import growth_plan_activities as plan_activities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.code_storage import CodeStorage
from tin_lite.domain import GROWTH_ONBOARDING_PLAN_PATH, GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME
from tin_lite.growth_onboarding import plan_block, plan_picks, plan_view
from tin_lite.growth_plan_assets import score as scorer
from tin_lite.growth_plan_site import _get, evidence_text, read_site
from tin_lite.public_workflows import PUBLIC_WORKFLOWS

ROOT = Path(__file__).resolve().parents[1]

TODAY = "2026-09-18"
NOTES = (
    "Acme Forms is a form builder for clinics. We sell to practice managers. "
    "Pricing starts at $29 a month. Do not run discounts. I want one article this week."
)


def workflow(key, title, *, required=(), modes=("on_demand", "weekly"), integrations=(), **extra):
    return {
        "key": key,
        "title": title,
        "kind": "workflow",
        "description": f"{title} for the project.",
        "required_inputs": list(required),
        "optional_inputs": [],
        "schedule_modes": list(modes),
        "requires_integrations": list(integrations),
        "runnable": not integrations,
        "unblock": {"kind": "connect_integration"} if integrations else None,
        **extra,
    }


def tin_state():
    return {
        "integrations": [
            {"provider_key": "infra.github", "name": "GitHub", "connected": False},
            {"provider_key": "workspace.google", "name": "Google Workspace", "connected": False},
        ],
        "running": [],
        "recent_runs": [],
        "workflows": [
            workflow("visibility.audit", "Audit AI visibility", required=("target",)),
            workflow("content.answer_page", "Draft answer page"),
            workflow(
                "organic.audit",
                "Audit organic search",
                required=("site_url", "market"),
                modes=("on_demand",),
            ),
            workflow(
                "organic.keyword_plan",
                "Plan keywords",
                required=("site_url", "market", "buyer_context"),
                modes=("on_demand",),
            ),
            workflow("content.public_article", "Write public article", required=("brief",)),
            workflow("research.deep_dive", "Research a question", required=("question",)),
            workflow(
                "site.health_improve",
                "Improve site health",
                required=("site_url",),
                integrations=("infra.github",),
            ),
            workflow(
                "outreach.email_shortlist",
                "Shortlist contacts",
                required=("objective",),
                integrations=("workspace.google",),
            ),
            workflow(
                "content.plan",
                "Plan content",
                required=("audit_run_id",),
                unblock={"kind": "prior_runs"},
                runnable=False,
            ),
            workflow("project.weekly_brief", "Weekly brief"),
            {**workflow("project.task", "One-off task", required=("instruction",)), "kind": "task"},
        ],
    }


def inputs(**overrides):
    return {
        "product_url": "https://acmeforms.example",
        "notes": NOTES,
        "founder_hours": "some",
        "budget": "under_500",
        "urgency": "two_months",
        "outcome": "paying_customers",
        "hard_nos": [],
        "timezone": "UTC",
        "system_repository": "GitHub acme/forms",
        "tin_state": tin_state(),
        **overrides,
    }


SITE = {"verdict": "ok", "pages": [], "readable": ["https://acmeforms.example"], "sitemap": None}
SITE_TEXT = (
    "Direct fetch verdict: ok.\n\n[home] https://acmeforms.example\nA form builder for clinics."
)


class FakeModel:
    """Answers each step from its schema and prompt, the way a well-behaved model would."""

    def __init__(self, *, overrides=None, unusable=()):
        self.calls, self.overrides, self.unusable = [], overrides or {}, list(unusable)

    async def __call__(self, step, system, user, schema, max_out, effort):
        self.calls.append(step)
        if step in self.unusable:
            self.unusable.remove(step)
            raise plan.UnusableModelResult("response was truncated")
        base = step.removesuffix(":retry")
        result = getattr(self, "_" + base.split(":")[0])(base, user, schema)
        return self.overrides.get(base, lambda value, user: value)(result, user)

    def _facts(self, step, user, schema):
        return {
            "business_name": "Acme Forms",
            "facts": [
                {
                    "topic": "what_is_sold",
                    "statement": "Acme Forms sells a form builder for clinics.",
                    "evidence": "https://acmeforms.example",
                    "confidence": "seen",
                }
            ],
            "assumptions": ["Buyer geography is unknown."],
            "corrections": [],
            "own_workflows": [],
            "founder_requests": ["I want one article this week"],
            "founder_limits": ["Do not run discounts."],
            "ruled_out_systems": [
                {"system": "pricing-and-packaging", "founder_words": "Do not run discounts."}
            ],
            "code_on_github": True,
            "hosted_site_builder": False,
            "mailbox_on_google": True,
            "search_market": "US",
            "search_market_basis": "nothing says otherwise",
        }

    def _profile(self, step, user, schema):
        profile = dict.fromkeys(schema["properties"]["profile"]["properties"])
        profile.update(businessType="b2b_saas", priceBand="mid", face="yes")
        return {
            "profile": profile,
            "basis": [
                {
                    "param": "businessType",
                    "quote": "form builder for clinics",
                    "source": "your agent's notes",
                },
                {
                    "param": "priceBand",
                    "quote": "Pricing starts at $29 a month",
                    "source": "your agent's notes",
                },
                {
                    "param": "face",
                    "quote": "the founder loves being on camera",
                    "source": "invented",
                },
            ],
            "tried": [],
            "current_status": [{"system": sid, "status": "nothing"} for sid in plan.SYSTEM_IDS],
        }

    def _scope(self, step, user, schema):
        candidates = schema["properties"]["suggested"]["items"]["properties"]["id"]["enum"]
        return {
            "bottleneck": "Growth breaks at discovery. Nobody finds the product yet.",
            "key_unknowns": ["Which clinics buy first?"],
            "suggested": [{"id": c, "reason": "fits", "items_per_week": 2} for c in candidates[:3]],
            "left_out": [],
            "founder_actions": ["Record signups every week.", "Tin will draft pages."],
            "measure": "Weekly signups from the product database.",
        }

    def _system(self, step, user, schema):
        spec = json.loads(user.split("THE ONLY WORKFLOWS IT MAY USE:\n", 1)[1])
        spec.sort(key=lambda item: item["key"] != "visibility.audit")  # a measured start first
        chosen = []
        for item in spec[:2]:
            weekly = "weekly" in item["schedule_modes"]
            chosen.append(
                {
                    "key": item["key"],
                    "mode": "weekly" if weekly else "once",
                    "weekdays": ["monday", "wednesday", "friday"] if weekly else [],
                    "local_time": "09:00",
                    "inputs": [
                        {"name": n, "value": "US and Canada" if n == "market" else f"value for {n}"}
                        for n in item["required_inputs"]
                    ],
                }
            )
        return {
            "role_line": "Tin will draft one page every Monday; drafts land in Decisions.",
            "summary": "Tin drafts pages for you.",
            "outlook": {
                "week": "A first draft will likely exist.",
                "month": "Several drafts will likely exist.",
                "quarter": "Published pages may earn visits.",
            },
            "workflows": chosen,
        }

    def _rewrite(self, step, user, schema):
        given = json.loads(user)
        return {
            "role_line": given["role_line"],
            "summary": given["summary"],
            "outlook": given["outlook"],
        }

    def _table(self, step, user, schema):
        return {
            "rows": [
                {"system": sid, "availability": "drafts yes", "job": "Drafts one page weekly."}
                for sid in plan.SYSTEM_IDS
            ]
        }

    def _view(self, step, user, schema):
        return {
            "picture": "Acme Forms has a live site. Clinics can find it through search.",
            "first_deliverable": [
                "The first audit lands in Files within ten minutes.",
                "It shows where AI answers name you, so you can choose the first page.",
            ],
            "first_phase": ["drafting one page every Monday"],
            "next_phase": [
                "open pull requests once GitHub is connected",
                "send outreach once a mailbox is connected",
            ],
            "systems_to_enable": ["Organic traffic system: Tin will draft one page every Monday."],
            "additional_roles": [],
            "own_workflows": [],
            "missing_pieces": [],
            "left_out_notes": [],
            "requests": [
                {
                    "index": 0,
                    "answer": "Delivered by Organic traffic system: one article each Monday.",
                }
            ],
            "channels": [],
        }

    def _repair(self, step, user, schema):
        return {"fixes": [{"slot": p["slot"], "text": "Short sentence."} for p in json.loads(user)]}


def block_of(text):
    return plan_block(text)["systems"]


async def test_fixture_run_produces_a_plan_setup_can_read():
    model = FakeModel()
    result = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, model)
    text = result["plan"]

    plan.validate_plan(text, tin_state())
    systems = block_of(text)
    _picked, offered = plan_picks(text)
    assert offered == [item["id"] for item in systems]
    assert 0 < len(plan_view(text)) <= 1100
    assert text.startswith("# Growth plan for Acme Forms\n\n2026-09-18")
    assert len([line for line in text.splitlines() if re.match(r"^\| \d+ \|", line)]) == 14
    assert "What you asked for" in text and "Delivered by Organic traffic system" in text
    # Judgment steps use the stronger route; everything else the cheaper one.
    assert plan.route_for("scope") is plan.JUDGMENT_ROUTE
    assert plan.route_for("system:organic-traffic") is plan.DRAFTING_ROUTE
    assert {"facts", "profile", "scope", "table", "view"} <= set(model.calls)


async def test_code_not_the_model_decides_what_reaches_the_block():
    result = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, FakeModel())
    workflows = [w for item in block_of(result["plan"]) for w in item["workflows"]]
    keys = {w["key"] for w in workflows}

    # Housekeeping, one-off tasks and workflows that start from an earlier run never enter setup.
    assert not keys & {"project.weekly_brief", "project.task", "content.plan"}
    # The market is an enum the workflow accepts, whatever prose the model offered.
    assert {w["inputs"]["market"] for w in workflows if "market" in w["inputs"]} <= {"US"}
    # The organic traffic system's audit grades AI visibility, so Start here no longer adds
    # the separate AI visibility audit beside it.
    assert "visibility.audit" not in keys
    # One configuration per workflow, so setup never doubles a schedule.
    configs = {}
    for w in workflows:
        configs.setdefault(w["key"], set()).add((w["mode"], tuple(w.get("weekdays", []))))
    assert all(len(v) == 1 for v in configs.values())
    # Three weekdays were offered; the review allowance trims what the founder is asked to read.
    assert all(len(w.get("weekdays", [])) <= 2 for w in workflows)
    # A scorer parameter without a verbatim basis in the evidence is dropped before ranking.
    assert "face" in result["report"]["dropped_parameters"]
    assert "face" not in result["report"]["profile_parameters"]
    # "Tin will ..." is not something only the founder can do.
    assert "Tin will draft pages." not in result["plan"]


async def test_plan_says_what_arrives_first_and_never_promises_publication():
    text = (await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, FakeModel()))["plan"]
    run = text.split("## What Tin would run\n", 1)[1]

    # The first useful deliverable comes before the systems list, and where it arrives is said.
    assert run.startswith("The first audit lands in Files within ten minutes.")
    assert run.index("so you can choose the first page") < run.index("- [ ] ")
    # Approval is not publication, and nothing promises a notification.
    assert "approved drafts stay in Tin unless GitHub pull-request delivery is configured" in text
    assert "publishes when GitHub is connected" not in text
    assert not re.search(r"(?i)\b(slack|email you|notify)", text)
    # A blank repository field does not hide GitHub; it is conditional on confirming the repository.
    connections = text.split("## Connections\n", 1)[1].split("```tin-plan", 1)[0]
    assert "analytics.gsc" in connections and "actual queries and impressions" in connections
    assert "product analytics" in connections and "reads PostHog directly" in connections
    # The mailbox is requested only when the selected work needs it.
    needed = {i for item in block_of(text) for i in item["integrations"]}
    assert ("workspace.google" in connections) == ("workspace.google" in needed)


class ChannelModel(FakeModel):
    def _view(self, step, user, schema):
        return {
            **super()._view(step, user, schema),
            "channels": [
                {"platform": "linkedin", "why": "clinic managers read it at work."},
                {"platform": "x", "why": "health-tech founders trade notes there"},
                {"platform": "linkedin", "why": "named twice"},
                {"platform": "reddit", "why": "practice owners ask for software there"},
                {"platform": "instagram", "why": "a fourth is never shown"},
            ],
        }


async def test_plan_suggests_social_channels_and_says_what_tin_does_on_each_today():
    quiet = (await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, FakeModel()))["plan"]
    assert "Social channels" not in quiet
    text = (await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, ChannelModel()))["plan"]
    line = next(x for x in text.splitlines() if x.startswith("Social channels"))
    assert line == (
        "Social channels that likely fit your buyers: LinkedIn (clinic managers read it at work); "
        "X (health-tech founders trade notes there); Reddit (practice owners ask for software "
        "there)."
    )
    # Code, not the model, says what Tin does on each: X today, LinkedIn drafts, the rest not yet.
    assert "ask your agent to run Draft for X when you're ready" in text
    assert "Tin can draft LinkedIn posts from an article or a weekly social plan" in text
    pieces = text.split("## Missing pieces\n", 1)[1].split("\n\n", 1)[0]
    assert "- Tin doesn't prepare Reddit posts yet." in pieces
    assert not re.search(r"(?i)coming soon", text)


def test_workflow_inputs_are_held_to_the_input_schema():
    spec = workflow(
        "project.brief",
        "Weekly brief",
        required=("focus",),
        input_schema={
            "properties": {
                "focus": {"type": "string", "maxLength": 240},
                "depth": {"type": "string", "enum": ["light", "deep"]},
            }
        },
    )
    spec["optional_inputs"] = ["depth"]
    avail = {"briefs": {"workflows": [dict(spec, includable=True)]}}
    item = {
        "id": "briefs",
        "workflows": [
            {
                "key": "project.brief",
                "mode": "weekly",
                "weekdays": ["monday"],
                "local_time": "09:00",
                "inputs": [
                    {"name": "focus", "value": "signups from clinics " * 20},
                    {"name": "depth", "value": "as deep as the evidence allows"},
                ],
            }
        ],
    }

    kept, notes = plan.validate_system(item, avail, inputs())

    # Prose never reaches an enum, and a string never exceeds its limit.
    assert "depth" not in kept[0]["inputs"]
    assert 0 < len(kept[0]["inputs"]["focus"]) <= 240
    assert any("depth" in n for n in notes) and any("240" in n for n in notes)


def package_schema(key):
    path = ROOT / "workflow_packages" / key / "workflow.json"
    return json.loads(path.read_text())["definition"]["input_schema"]


def test_numbers_and_lists_are_typed_before_the_founder_sees_them():
    """The model writes every input as text; the tin-plan block must carry schema types."""
    mentions = workflow("organic.mention_backlinks", "Mention backlinks")
    mentions.update(
        optional_inputs=["max_mentions", "recency_days"],
        input_schema=package_schema("organic.mention_backlinks"),
    )
    watch = workflow("competitor.watch", "Competitor watch")
    watch.update(
        optional_inputs=["competitor_urls", "max_competitors"],
        input_schema=package_schema("competitor.watch"),
    )
    avail = {
        "outreach": {"workflows": [dict(mentions, includable=True), dict(watch, includable=True)]}
    }
    item = {
        "id": "outreach",
        "workflows": [
            {
                "key": "organic.mention_backlinks",
                "mode": "once",
                "weekdays": [],
                "local_time": "",
                "inputs": [
                    {"name": "max_mentions", "value": "10"},
                    {"name": "recency_days", "value": "lots"},
                ],
            },
            {
                "key": "competitor.watch",
                "mode": "once",
                "weekdays": [],
                "local_time": "",
                "inputs": [
                    {"name": "max_competitors", "value": "9"},
                    {"name": "competitor_urls", "value": "https://a.example, https://b.example"},
                ],
            },
        ],
    }

    kept, notes = plan.validate_system(item, avail, inputs())

    assert kept[0]["inputs"] == {"max_mentions": 10}  # "lots" is dropped; the default applies.
    assert kept[1]["inputs"] == {
        "max_competitors": 5,
        "competitor_urls": ["https://a.example", "https://b.example"],
    }
    assert any("recency_days dropped" in n for n in notes)
    assert any("max_competitors lowered to its maximum 5" in n for n in notes)


def test_schema_coercion_is_pure_and_bounded():
    from tin_lite.workflow_inputs import coerce_schema_inputs

    schema = {
        "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 5},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "name": {"type": "string"},
        }
    }
    values = {"count": "0", "ratio": "2.5", "flag": "Yes", "tags": "a\nb, c", "name": "7"}
    fixed, notes = coerce_schema_inputs(schema, values)
    assert fixed == {"count": 1, "ratio": 2.5, "flag": True, "tags": ["a", "b"], "name": "7"}
    assert values["count"] == "0"  # The caller's dict is not mutated.
    assert len(notes) == 2
    fixed, notes = coerce_schema_inputs(schema, {"count": "2.5", "flag": "maybe", "ratio": "nan"})
    assert fixed == {} and len(notes) == 3
    assert coerce_schema_inputs(schema, {"count": 3, "flag": False}) == (
        {"count": 3, "flag": False},
        [],
    )


def test_schema_coercion_reads_json_lists_before_splitting():
    from tin_lite.workflow_inputs import coerce_schema_inputs

    strings = {"type": "array", "items": {"type": "string"}}
    schema = {
        "properties": {
            "seed_phrases": strings,
            "website_hosts": strings,
            "exclude_domains": {**strings, "maxItems": 2},
        }
    }
    fixed, notes = coerce_schema_inputs(
        schema,
        {
            # A plan wrote these lists as JSON text, the second one encoded twice.
            "seed_phrases": '["crm for dentists", "dental, practice software"]',
            "website_hosts": json.dumps(json.dumps(["a.example", " b.example "])),
            "exclude_domains": '["x.example", "y.example", "z.example"]',
        },
    )
    assert fixed == {
        "seed_phrases": ["crm for dentists", "dental, practice software"],
        "website_hosts": ["a.example", "b.example"],
        "exclude_domains": ["x.example", "y.example"],
    }
    assert notes == ["exclude_domains cut to its first 2 items"]
    # Not JSON: a bracketed list loses its brackets and quotes; plain text still splits.
    fixed, _ = coerce_schema_inputs(
        schema, {"seed_phrases": "['a b', \"c\",]", "website_hosts": "a.example, b.example"}
    )
    assert fixed == {"seed_phrases": ["a b", "c"], "website_hosts": ["a.example", "b.example"]}
    # A JSON value that is not a list, or a quoted word, falls back to splitting as before.
    fixed, _ = coerce_schema_inputs(schema, {"seed_phrases": '"solo"', "website_hosts": "[1"})
    assert fixed == {"seed_phrases": ['"solo"'], "website_hosts": ["[1"]}


async def test_hard_nos_and_founder_rulings_are_enforced_by_code():
    result = await plan.build_plan(
        inputs(hard_nos=["no_cold_email"]), SITE, SITE_TEXT, TODAY, FakeModel()
    )
    text = result["plan"]
    systems = block_of(text)

    assert "cold-outbound" not in {item["id"] for item in systems}
    assert not any(w["key"].startswith("outreach.") for item in systems for w in item["workflows"])
    assert "outreach sends from your mailbox" not in text
    # "Do not run discounts." is an imperative prohibition: the system moves last, unoffered.
    rows = [
        line.split("|")[2].strip() for line in text.splitlines() if re.match(r"^\| \d+ \|", line)
    ]
    assert rows[-1] in {"Pricing and packaging", "Cold outbound email"}
    assert "pricing-and-packaging" not in {item["id"] for item in systems}


async def test_a_condition_or_a_status_report_is_not_a_ruling():
    def conditional(value, user):
        value["ruled_out_systems"] = [
            {
                "system": "technical-seo",
                "founder_words": "Do not touch the site without telling me.",
            },
            {"system": "owned-audience", "founder_words": "No blog, no email."},
        ]
        return value

    notes = NOTES + " Do not touch the site without telling me. No blog, no email."
    result = await plan.build_plan(
        inputs(notes=notes), SITE, SITE_TEXT, TODAY, FakeModel(overrides={"facts": conditional})
    )
    rows = [
        line.split("|")[2].strip()
        for line in result["plan"].splitlines()
        if re.match(r"^\| \d+ \|", line)
    ]
    assert rows[-1] not in {"Technical SEO", "Owned audience"}


async def test_unsupported_market_excludes_the_audits_instead_of_faking_one():
    def elsewhere(value, user):
        return {**value, "search_market": "other"}

    result = await plan.build_plan(
        inputs(), SITE, SITE_TEXT, TODAY, FakeModel(overrides={"facts": elsewhere})
    )
    keys = {w["key"] for item in block_of(result["plan"]) for w in item["workflows"]}
    assert not keys & {"organic.audit", "organic.keyword_plan"}


async def test_no_live_site_excludes_site_bound_workflows():
    site = {"verdict": "unreachable", "pages": [], "sitemap": None}
    result = await plan.build_plan(
        inputs(), site, "Direct fetch verdict: unreachable.", TODAY, FakeModel()
    )
    keys = {w["key"] for item in block_of(result["plan"]) for w in item["workflows"]}
    assert not keys & {
        "visibility.audit",
        "organic.audit",
        "organic.keyword_plan",
        "site.health_improve",
    }


def with_research_run(state, *recent):
    state["workflows"].insert(
        0,
        workflow(
            "organic.traffic_system",
            "Run the organic traffic system",
            required=("site_url", "market", "buyer_context", "start_date"),
            modes=("on_demand",),
        ),
    )
    state["recent_runs"] = list(recent)
    return state


def leaves_out_the_default(value, user):
    """A scope step that ranks the default below other systems and leaves it out."""
    rest = [x for x in value["suggested"] if x["id"] != plan.DEFAULT_SYSTEM]
    others = ["technical-seo", "research-and-positioning", "owned-audience"]
    candidates = [c for c in others if c in user] or [x["id"] for x in rest]
    return {
        **value,
        "suggested": [{"id": c, "reason": "fits", "items_per_week": 2} for c in candidates[:3]],
        "left_out": [{"id": plan.DEFAULT_SYSTEM, "reason": "the scope preferred other work"}],
    }


async def test_the_organic_traffic_system_leads_and_starts_its_research_once():
    state = with_research_run(tin_state())
    model = FakeModel(overrides={"scope": leaves_out_the_default})
    result = await plan.build_plan(inputs(tin_state=state), SITE, SITE_TEXT, TODAY, model)
    text = result["plan"]
    plan.validate_plan(text, state)

    systems = block_of(text)
    # Code puts the default first and makes it Tin's suggestion, whatever the scope step said.
    assert systems[0]["id"] == plan.DEFAULT_SYSTEM and systems[0]["suggested"]
    assert result["report"]["suggested"][0] == plan.DEFAULT_SYSTEM
    assert (
        f"code made {plan.DEFAULT_SYSTEM} the first suggestion" in result["report"]["code_repairs"]
    )
    assert plan_picks(text)[1][0] == plan.DEFAULT_SYSTEM
    # Its research run starts once, with the site, market and start date set by code.
    run = systems[0]["workflows"][0]
    assert run["key"] == plan.DEFAULT_RUN and run["mode"] == "once"
    assert run["inputs"]["site_url"] == "https://acmeforms.example"
    assert run["inputs"]["market"] == "US" and run["inputs"]["start_date"] == TODAY
    assert len(run["inputs"]["buyer_context"]) >= 20
    # The research run already audits and researches keywords; nothing buys them a second time.
    once = {w["key"] for item in systems for w in item["workflows"] if w["mode"] == "once"}
    assert not once & plan.DEFAULT_RUN_CHILDREN


async def test_code_adds_the_research_run_when_the_model_leaves_it_out():
    def answer_pages_only(value, user):
        return {
            **value,
            "workflows": [
                {
                    "key": "content.answer_page",
                    "mode": "weekly",
                    "weekdays": ["tuesday"],
                    "local_time": "10:00",
                    "inputs": [],
                }
            ],
        }

    state = with_research_run(tin_state())
    model = FakeModel(overrides={f"system:{plan.DEFAULT_SYSTEM}": answer_pages_only})
    result = await plan.build_plan(inputs(tin_state=state), SITE, SITE_TEXT, TODAY, model)
    plan.validate_plan(result["plan"], state)

    lead = block_of(result["plan"])[0]
    assert [w["key"] for w in lead["workflows"]] == [plan.DEFAULT_RUN, "content.answer_page"]
    # Without a model-written buyer context, code takes it from the facts it can cite.
    assert lead["workflows"][0]["inputs"]["buyer_context"] == (
        "Acme Forms sells a form builder for clinics."
    )
    # Code changed the setup, so the role's text is rewritten to match it.
    assert f"rewrite:{plan.DEFAULT_SYSTEM}" in model.calls


async def test_the_default_needs_a_live_site_a_supported_market_and_no_earlier_run():
    def elsewhere(value, user):
        return {**value, "search_market": "other"}

    state = with_research_run(tin_state())
    result = await plan.build_plan(
        inputs(tin_state=state), SITE, SITE_TEXT, TODAY, FakeModel(overrides={"facts": elsewhere})
    )
    keys = {w["key"] for item in block_of(result["plan"]) for w in item["workflows"]}
    assert plan.DEFAULT_RUN not in keys

    state = with_research_run(tin_state())
    site = {"verdict": "unreachable", "pages": [], "sitemap": None}
    result = await plan.build_plan(
        inputs(tin_state=state), site, "Direct fetch verdict: unreachable.", TODAY, FakeModel()
    )
    keys = {w["key"] for item in block_of(result["plan"]) for w in item["workflows"]}
    assert plan.DEFAULT_RUN not in keys

    # A project whose research already ran keeps the lead but never buys the research again.
    state = with_research_run(tin_state(), {"key": plan.DEFAULT_RUN, "status": "succeeded"})
    model = FakeModel(overrides={"scope": leaves_out_the_default})
    result = await plan.build_plan(inputs(tin_state=state), SITE, SITE_TEXT, TODAY, model)
    systems = block_of(result["plan"])
    assert systems[0]["id"] == plan.DEFAULT_SYSTEM
    assert plan.DEFAULT_RUN not in {w["key"] for item in systems for w in item["workflows"]}


async def test_a_founder_ruling_still_beats_the_default():
    def no_seo(value, user):
        value["ruled_out_systems"] = [
            {"system": plan.DEFAULT_SYSTEM, "founder_words": "Do not write SEO articles."}
        ]
        return value

    state = with_research_run(tin_state())
    result = await plan.build_plan(
        inputs(notes=NOTES + " Do not write SEO articles.", tin_state=state),
        SITE,
        SITE_TEXT,
        TODAY,
        FakeModel(overrides={"facts": no_seo}),
    )
    systems = block_of(result["plan"])
    assert plan.DEFAULT_SYSTEM not in {item["id"] for item in systems}
    assert plan.DEFAULT_RUN not in {w["key"] for item in systems for w in item["workflows"]}


NO_SITE = {"verdict": "none", "pages": [], "seconds": 0}


def declines_everything():
    """What production saw for founders without a site: no suggestion, no workflow anywhere."""

    def no_suggestion(value, user):
        return {**value, "suggested": []}

    def no_workflows(value, user):
        return {**value, "role_line": "Tin will wait for a live site.", "workflows": []}

    overrides = {f"system:{sid}": no_workflows for sid in plan.SYSTEM_IDS}
    return FakeModel(overrides={**overrides, "scope": no_suggestion})


async def test_a_plan_without_a_site_is_never_empty_when_every_step_declines():
    model = declines_everything()
    result = await plan.build_plan(
        inputs(product_url=""), NO_SITE, evidence_text(NO_SITE), TODAY, model
    )
    text = result["plan"]
    plan.validate_plan(text, tin_state())

    systems = block_of(text)
    fallback = result["report"]["fallback"]
    assert [item["id"] for item in systems] == [fallback["system"]]
    assert systems[0]["suggested"] and result["report"]["suggested"] == [fallback["system"]]
    # The kept workflow runs without a site and needs no input the model would have written.
    avail, _ = plan.availability(tin_state(), False, True)
    states = {w["key"]: w for w in avail[fallback["system"]]["workflows"]}
    assert fallback["workflows"] == [w["key"] for w in systems[0]["workflows"]]
    assert all(states[key]["state"] == "runnable now" for key in fallback["workflows"])
    # The checklist offers it as Tin's suggestion, and its text is rewritten to match the setup.
    _picked, offered = plan_picks(text)
    assert offered == [fallback["system"]] and "(Tin's suggestion)" in text
    assert f"rewrite:{fallback['system']}" in model.calls
    assert plan_view(text)
    # A plan whose systems kept their workflows reports no fallback.
    ordinary = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, FakeModel())
    assert ordinary["report"]["fallback"] is None


def test_the_fallback_respects_hard_nos_and_founder_rulings():
    state = tin_state()
    for w in state["workflows"]:
        if w["key"] in ("research.deep_dive", "outreach.email_shortlist"):
            w["required_inputs"] = []
    state["integrations"][1]["connected"] = True  # the mailbox is there; only the hard no stops it
    ranking = plan.score({}, {})[1]["ranking"]

    # Without the hard no the mailbox shortlist comes first in its system.
    open_avail, _ = plan.availability(state, False, False)
    sid, kept = plan.fallback_system(["founder-led-sales"], open_avail, inputs())
    assert sid == "founder-led-sales"
    assert kept == [
        {
            "key": "outreach.email_shortlist",
            "mode": "weekly",
            "weekdays": ["monday"],
            "local_time": "09:00",
            "inputs": {},
        }
    ]

    avail, _ = plan.availability(state, False, False, ["no_cold_email"])
    _fit, candidates, banned, _weight = plan.order(
        ranking, avail, ["no_cold_email"], ["founder-led-sales"]
    )
    assert "cold-outbound" in banned and "cold-outbound" not in candidates
    assert "founder-led-sales" not in candidates
    sid, kept = plan.fallback_system(candidates, avail, inputs())
    assert sid in candidates and kept
    assert not any(w["key"].startswith("outreach.") for w in kept)
    # Within an allowed system the hard no still removes the forbidden workflow.
    assert plan.fallback_system(["founder-led-sales"], avail, inputs())[1][0]["key"] == (
        "research.deep_dive"
    )
    # Nothing code can configure alone means no fallback, not an invented input.
    plain, _ = plan.availability(tin_state(), False, False)
    assert plan.fallback_system(["cold-outbound"], plain, inputs()) == (None, [])


async def test_an_unusable_result_gets_one_replacement_under_its_own_step():
    model = FakeModel(unusable=["scope"])
    result = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, model)
    assert "scope" in model.calls and "scope:retry" in model.calls
    assert result["report"]["retried_steps"] == ["scope: response was truncated"]


async def test_a_view_that_skips_a_request_is_asked_again_under_its_own_step():
    model = FakeModel(overrides={"view": lambda value, user: dict(value, requests=[])})
    receipts = {}

    async def receipted(step, *args):
        # The activity replays a completed step's receipt instead of buying it again.
        if step not in receipts:
            receipts[step] = await model(step, *args)
        return receipts[step]

    result = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, receipted)
    assert model.calls.count("view") == 1 and "view:requests" in model.calls
    assert result["report"]["unanswered_requests"] == []
    assert "Delivered by Organic traffic system" in result["plan"]


async def test_an_unusable_request_re_ask_keeps_the_original_view():
    model = FakeModel(
        overrides={"view": lambda value, user: dict(value, requests=[])},
        unusable=["view:requests", "view:requests:retry"],
    )
    asked = {}

    async def recording(step, system, user, *args):
        asked[step] = user
        return await model(step, system, user, *args)

    result = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, recording)
    # The re-ask is best-effort: one extra request at most, and the valid first view stands.
    assert model.calls.count("view:requests") == 1 and "view:requests:retry" not in model.calls
    assert "UNANSWERED REQUESTS" in asked["view:requests"] and "[0]" in asked["view:requests"]
    assert result["report"]["unanswered_requests"] == [0]
    assert result["report"]["retried_steps"] == []


async def test_two_unusable_results_fail_the_run_and_save_nothing():
    with pytest.raises(plan.UnusableModelResult):
        await plan.build_plan(
            inputs(), SITE, SITE_TEXT, TODAY, FakeModel(unusable=["scope", "scope:retry"])
        )


async def test_plausible_but_off_contract_results_never_reach_setup():
    def invents(value, user):
        value["workflows"].append(
            {
                "key": value["workflows"][0]["key"],
                "mode": "hourly",
                "weekdays": [],
                "local_time": "25:99",
                "inputs": [],
            }
        )
        value["workflows"][0]["inputs"] = []  # a required input left out
        return value

    overrides = {f"system:{sid}": invents for sid in plan.SYSTEM_IDS}
    result = await plan.build_plan(inputs(), SITE, SITE_TEXT, TODAY, FakeModel(overrides=overrides))
    plan.validate_plan(result["plan"], tin_state())
    assert any("dropped" in note for note in result["report"]["code_repairs"])


def test_validate_plan_refuses_what_setup_cannot_use():
    good = (
        "# P\n\n## Tin's view\nA view.\n\n## What Tin would run\n"
        "- [ ] organic-traffic **Organic traffic system** — x\n\n"
    )
    block = {
        "systems": [
            {
                "id": "organic-traffic",
                "workflows": [
                    {"key": "visibility.audit", "mode": "weekly", "inputs": {"target": "a.example"}}
                ],
            }
        ]
    }
    text = good + "```tin-plan\n" + json.dumps(block) + "\n```\n"
    plan.validate_plan(text, tin_state())

    for mutate in (
        lambda b: b["systems"][0]["workflows"][0].update(key="invented.workflow"),
        lambda b: b["systems"][0]["workflows"][0].update(mode="daily"),
        lambda b: b["systems"][0]["workflows"][0].update(inputs={}),
        lambda b: b["systems"][0].update(id="project-progress"),
        lambda b: b["systems"][0]["workflows"][0].update(key="project.weekly_brief"),
    ):
        broken = json.loads(json.dumps(block))
        mutate(broken)
        with pytest.raises(ValueError):
            plan.validate_plan(good + "```tin-plan\n" + json.dumps(broken) + "\n```\n", tin_state())


def test_definition_pins_the_contract_and_the_assets_stay_consistent():
    """Moved from the procedure contract test when the plan stopped being a Codex procedure."""
    registry = {item.key: item for item in BUILTIN_WORKFLOWS}
    onboarding_keys = {GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME, "growth.onboarding"}
    public_keys = {item.key for item in PUBLIC_WORKFLOWS}
    definition, files = registry[
        GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME
    ].definition_and_resource_files()

    assert definition["executor"] == plan.KEY and "procedure" not in definition and not files
    assert definition["system"] == "start-here" and definition["agent_only"] is True
    assert definition["schedule_modes"] == ["on_demand"] and "human_review" not in definition
    assert definition["output_path"] == "reports/GROWTH_ONBOARDING_PLAN.md"
    assert definition["plan_policy"] == plan.POLICY
    assert definition["plan_contract_sha256"] == plan.contract_digest()
    assert [(r["provider"], r["model"]) for r in definition["plan_routes"]] == [
        ("openai", "gpt-6-sol"),
        ("openai", "gpt-6-luna"),
    ]
    properties = definition["input_schema"]["properties"]
    assert definition["input_schema"]["required"] == ["project_id"]
    assert {
        "notes",
        "timezone",
        "founder_hours",
        "budget",
        "urgency",
        "outcome",
        "hard_nos",
    } <= set(properties)

    programs = plan.PROGRAMS["programs"]
    providers = {"infra.github", "workspace.google", "analytics.gsc", "ads.google"}
    assert len(programs) == 14 and len({row["id"] for row in programs}) == 14
    for row in programs:
        assert row["tin"]["coverage"] in {"full", "partial", "none"}
        assert 0 <= row["tin"]["impact"] <= 1 and row["tin"]["impact_note"]
        assert row["needs"]["founder_hours"] in {"min", "some", "lots"}
        assert set(row["tin"]["workflows"]) <= set(registry) | set(public_keys), row["id"]
        assert not set(row["tin"]["workflows"]) & onboarding_keys
        assert set(row["tin"]["integrations"]) <= providers, row["id"]
    system_fields = {entry["input"] for entry in plan.PROGRAMS["systems_checklist"]}
    assert len(system_fields) == 11 and system_fields <= set(properties)
    # A checklist system that names a Tin integration names a registered, labelled one.
    from tin_lite.integrations import registered_integrations

    named = {e["tin_integration"] for e in plan.PROGRAMS["systems_checklist"]} - {None}
    assert named <= {d.key for d in registered_integrations()} and named <= set(plan.PROVIDERS)
    assert {"payments.stripe", "analytics.posthog"} <= named
    titles = plan.PROGRAMS["workflow_titles"]
    public = {
        item.key: json.loads((ROOT / "workflow_packages" / item.key / "workflow.json").read_text())[
            "definition"
        ]["title"]
        for item in PUBLIC_WORKFLOWS
    }
    # Workflows hidden from the organic system and discovery stay registered for saved
    # configurations, but the plan never names them.
    hidden = {item.key for item in PUBLIC_WORKFLOWS if not item.public_discovery}
    hidden.add("visibility.audit")
    assert titles == {
        key: title
        for key, title in (
            {item.key: item.title for item in BUILTIN_WORKFLOWS if item.key not in onboarding_keys}
            | public
        ).items()
        if key not in hidden
    }
    assert set(plan.PROGRAMS["workflow_scope"]) == set(titles)
    assert {system["id"] for system in plan.RUBRIC["systems"]} == {row["id"] for row in programs}
    known = {param["id"] for param in plan.RUBRIC["params"]}
    for system in plan.RUBRIC["systems"]:
        assert set(system["weights"]) <= known, system["id"]
        assert system["plays"] and system["out"]
    assert len({play for system in plan.RUBRIC["systems"] for play in system["plays"]}) == 115
    # Every rules section the prompts quote must still exist in the packaged rules.
    for section in (
        plan.WRITING,
        plan.UNDERSTAND,
        plan.PROFILE_RULES,
        plan.TABLE_RULES,
        plan.SCOPE_RULES,
        plan.RUN_RULES,
        plan.VIEW_RULES,
    ):
        assert len(section) > 200


def test_scorer_ranks_all_systems_and_penalises_fair_failures():
    base = {
        "businessType": "dev_tool",
        "priceBand": "low",
        "searchDemand": "proven",
        "buyersBuy": ["search", "community"],
        "funnelBreak": "conversion",
        "hours": "some",
        "budget": "none",
    }
    _profile, first = plan.score(dict(base), {})
    assert [row["rank"] for row in first["ranking"]] == list(range(1, 15))
    top = first["ranking"][0]["id"]
    _profile, second = plan.score(dict(base), {top: 2})
    moved = next(row for row in second["ranking"] if row["id"] == top)
    assert moved["rank"] > 1 and any(c["param"] == "tried" for c in moved["against"])
    assert "businessType (required)" in scorer.describe()
    # An illegal value is dropped, never guessed.
    profile, _ = plan.score({**base, "priceBand": "astronomical"}, {})
    assert "priceBand" not in profile


def test_every_form_budget_reaches_the_scorer():
    # Each budget the onboarding form offers maps to a rubric value; "unknown" stays unset.
    expected = {
        "none": "none",
        "under_500": "small",
        "500_to_2000": "real",
        "more": "real",
        "unknown": None,
    }
    for budget, value in expected.items():
        assert plan.founder_profile({"budget": budget}).get("budget") == value, budget
    assert plan.founder_profile({"priority": "main"})["budget"] == "real"


def resolver_for(table):
    async def resolve(host, port, **_kwargs):
        return [(None, None, None, None, (table[host], port))]

    return resolve


async def test_site_reader_refuses_non_public_addresses_and_outside_redirects():
    def handler(request):
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        return httpx.Response(
            200,
            text="<html><title>x</title><p>hello</p></html>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        private = await read_site(
            "http://internal.example",
            client=client,
            resolver=resolver_for({"internal.example": "10.0.0.5"}),
        )
        assert (
            private["verdict"] == "unreachable"
            and private["pages"][0]["error"] == "non_public_address"
        )
        metadata = await read_site(
            "http://169.254.169.254/latest",
            client=client,
            resolver=resolver_for({"169.254.169.254": "169.254.169.254"}),
        )
        assert metadata["pages"][0]["error"] == "non_public_address"
        outside = await read_site(
            "https://shop.example/moved",
            client=client,
            resolver=resolver_for(
                {"shop.example": "93.184.216.34", "evil.example": "93.184.216.35"}
            ),
        )
        assert outside["pages"][0]["error"] == "outside_site_redirect"


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.216.34", "2606:4700:4700::1111"],
        ["2606:4700:4700::1111", "93.184.216.34"],
    ],
)
async def test_site_reader_keeps_resolver_preference_and_checks_every_address(addresses):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})

    async def resolve(host, port, **_kwargs):
        return [(None, None, None, None, (ip, port)) for ip in addresses]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await read_site("https://acme.example", client=client, resolver=resolve)
        assert seen and set(seen) == {addresses[0]}

        addresses.append("127.0.0.1")
        private = await read_site("https://acme.example", client=client, resolver=resolve)
    assert private["pages"][0]["error"] == "non_public_address"


async def test_site_reader_reads_pages_the_sitemap_and_reports_a_js_shell():
    body = "<html><title>Acme</title><meta name='description' content='Forms for clinics'>" + (
        "<p>" + "Acme builds forms for clinics. " * 80 + "</p><a href='/pricing'>Pricing</a></html>"
    )
    sitemap = (
        "<urlset>"
        + "".join(f"<url><loc>https://acme.example/blog/{i}</loc></url>" for i in range(40))
        + "</urlset>"
    )

    def handler(request):
        seen.append((request.url.host, request.headers["host"]))
        if request.headers["host"] == "shell.example":
            return httpx.Response(
                200, text="<html><div id=root></div></html>", headers={"content-type": "text/html"}
            )
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap, headers={"content-type": "application/xml"})
        if request.url.path in {"/", "/pricing"}:
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})

    seen = []
    resolve = resolver_for({"acme.example": "93.184.216.34", "shell.example": "93.184.216.36"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        site = await read_site("https://acme.example", client=client, resolver=resolve)
        shell = await read_site("https://shell.example", client=client, resolver=resolve)

    assert site["verdict"] == "ok" and site["sitemap"]["urls"] == 40
    assert site["sitemap"]["top_sections"][0] == ("blog", 40)
    # The connection goes to the vetted address; the site's name travels only in the Host header.
    assert ("93.184.216.34", "acme.example") in seen
    text = evidence_text(site)
    assert (
        "Forms for clinics" in text
        and "lists 40 URLs" in text
        and "none found after one fetch" in text
    )
    assert shell["verdict"] == "thin"
    assert evidence_text({"verdict": "none", "pages": []}).startswith("No product_url")


async def test_site_reader_keeps_nul_bytes_and_error_bodies_out_of_the_receipt():
    # A live site served a binary 404 for /llms.txt; its NUL bytes made Postgres refuse the receipt.
    def handler(request):
        if request.url.path == "/llms.txt":
            return httpx.Response(
                404, content=b"\x00\x01PK\x00binary", headers={"content-type": "text/plain"}
            )
        if request.url.path == "/docs.txt":
            return httpx.Response(
                200, content=b"<html>app shell</html>", headers={"content-type": "text/html"}
            )
        return httpx.Response(
            200,
            content=b"<html><title>Loc\x00In</title><p>" + b"Local SEO. " * 200 + b"</p></html>",
            headers={"content-type": "text/html"},
        )

    resolve = resolver_for({"locin.example": "93.184.216.34"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        site = await read_site("https://locin.example", client=client, resolver=resolve)
        shell = await _get(client, "https://locin.example/docs.txt", resolve)

    assert "\x00" not in json.dumps(site)
    assert site["pages"][0]["title"] == "LocIn"
    llms = next(p for p in site["pages"] if p["kind"] == "llms")
    assert llms["status"] == 404 and llms["text"] == ""
    assert shell["status"] == 200 and shell["text"] == ""


# ---------------------------------------------------------------- activities (database-backed)


def step_of(schema_name):
    """The activity names each schema after its step; recover the step for the fake model."""
    name = schema_name.removeprefix("growth_plan_")
    retry = name.endswith("_retry")
    name = name.removesuffix("_retry")
    for prefix in ("system", "rewrite", "repair", "view"):
        if name.startswith(prefix + "_"):
            name = f"{prefix}:{name[len(prefix) + 1 :]}"
    return name + (":retry" if retry else "")


async def activity_fixture(db, monkeypatch, *, model=None):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_private_workflows import fixture as project_fixture

    from tin_lite.growth_plan_activities import GrowthPlanActivities
    from tin_lite.model_providers import ModelProviderError, ModelResult, ModelUsage, ProviderName

    f = await project_fixture(db)
    await db.upsert_workflow_system(system_id="start-here", name="Start here", display_order=0)
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == plan.KEY)
    revision = "e" * 40
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=plan.KEY,
        title=builtin.title,
        description=builtin.description,
        executor=plan.KEY,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=revision,
        version_label=builtin.version_label,
        definition=builtin.definition,
    )
    f.workflow = await db.get_workflow(builtin.id)
    f.settings.luna_api_key = "test-native-key"
    f.definition = json.loads(json.dumps(builtin.definition))
    original_read = f.storage.read_canonical_artifact

    async def read(**kw):
        if kw["repo_id"] == "registry/workflows":
            return json.dumps(f.definition).encode()
        return await original_read(**kw)

    async def stage(**kw):
        # The real storage refuses an output its executor did not declare; keep the fake as strict.
        assert (kw["executor"], kw["path"]) == (plan.KEY, GROWTH_ONBOARDING_PLAN_PATH)
        head = f.storage.repo.head
        saved = f.storage.repo.edit({kw["path"]: kw["content"]})
        f.storage.repo.head = head
        return saved

    f.storage.read_canonical_artifact = read
    f.storage.stage_native_output = AsyncMock(side_effect=stage)
    f.model = model or FakeModel()

    async def generate(route_key, request, *, timeout_seconds=None):
        step = step_of(request.output_schema_name)
        assert route_key == plan.route_for(step).key
        # Every plan step waits longer than the model client's 90-second default.
        assert timeout_seconds == plan_activities.MODEL_TIMEOUT_SECONDS > 90
        try:
            parsed = await f.model(
                step, request.system, request.messages[0].content, request.output_schema, 0, ""
            )
        except plan.UnusableModelResult as exc:
            raise ModelProviderError(str(exc)) from None
        return ModelResult(
            provider=ProviderName.OPENAI,
            model="synthetic",
            text="",
            parsed=parsed,
            request_id=f"request-{step}",
            usage=ModelUsage(input_tokens=10, output_tokens=5),
        )

    f.router = SimpleNamespace(generate=AsyncMock(side_effect=generate))

    async def state(**_kwargs):
        return tin_state()

    monkeypatch.setattr("tin_lite.onboarding.onboarding_tin_state", state)
    f.site_reader = AsyncMock(return_value={**SITE, "seconds": 0.1})
    f.activities = GrowthPlanActivities(
        database=db,
        storage=f.storage,
        settings=f.settings,
        router=f.router,
        responses=None,
        site_reader=f.site_reader,
    )
    monkeypatch.setattr("tin_lite.growth_plan_activities.activity.heartbeat", lambda *_a: None)
    return f


async def start_plan(f):
    from uuid import uuid4

    from test_private_workflows import ACTOR

    from tin_lite.run_service import start_workflow_run

    payload = {k: v for k, v in inputs().items() if k != "tin_state"}
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=str(uuid4()),
        input_payload=payload,
    )


async def test_activities_receipt_every_step_and_a_retry_buys_nothing(publication_db, monkeypatch):
    from tin_lite.domain import GROWTH_ONBOARDING_PLAN_PATH
    from tin_lite.publication import read_run_output

    f = await activity_fixture(publication_db, monkeypatch)
    run = await start_plan(f)
    for _ in range(2):
        await f.activities.prepare(str(run.id))
    assert f.site_reader.await_count == 1
    await f.activities.write(str(run.id))
    bought = f.router.generate.await_count
    assert bought >= 5
    # A retried activity replays every completed step from its receipt.
    await publication_db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"{run.id}:plan_document"
    )
    await f.activities.write(str(run.id))
    assert f.router.generate.await_count == bought
    for _ in range(2):
        await f.activities.publish(str(run.id))
    done = await f.db.get_run(run.id)
    assert done.status.value == "succeeded" and done.artifact_path == GROWTH_ONBOARDING_PLAN_PATH
    assert done.retained_output is None and not done.review_required
    assert f.storage.stage_native_output.await_count == 1
    saved = await read_run_output(storage=f.storage, run=done, repo_id=f.project.state_repo_id)
    plan.validate_plan(saved.content.decode(), tin_state())
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='onboarding_plan_ready'",
            run.id,
        )
        == 1
    )


async def test_an_unconfirmed_model_step_stops_the_run_instead_of_buying_again(
    publication_db, monkeypatch
):
    from temporalio.exceptions import ApplicationError

    f = await activity_fixture(publication_db, monkeypatch)
    run = await start_plan(f)
    await f.activities.prepare(str(run.id))
    key = f"{run.id}:plan:facts"
    async with publication_db.effect_lock(key, plan.KEY) as (conn, _saved):
        await publication_db.start_effect(conn, execution_key=key, operation=plan.KEY)
    with pytest.raises(ApplicationError, match="no replacement was purchased"):
        await f.activities.write(str(run.id))
    assert not any(
        step_of(call.args[1].output_schema_name) == "facts"
        for call in f.router.generate.await_args_list
    )


async def test_unusable_results_are_receipted_and_fail_without_saving(publication_db, monkeypatch):
    from temporalio.exceptions import ApplicationError

    model = FakeModel(unusable=["scope", "scope:retry"])
    f = await activity_fixture(publication_db, monkeypatch, model=model)
    run = await start_plan(f)
    await f.activities.prepare(str(run.id))
    with pytest.raises(ApplicationError, match="unusable"):
        await f.activities.write(str(run.id))
    bought = f.router.generate.await_count
    # The unusable outcomes are receipted too: a retry replays them and buys nothing.
    with pytest.raises(ApplicationError, match="unusable"):
        await f.activities.write(str(run.id))
    assert f.router.generate.await_count == bought
    assert await f.db.get_effect(f"{run.id}:plan_document") is None
    assert f.storage.stage_native_output.await_count == 0


async def test_a_worker_serving_another_contract_refuses_the_run(publication_db, monkeypatch):
    from temporalio.exceptions import ApplicationError

    f = await activity_fixture(publication_db, monkeypatch)
    run = await start_plan(f)
    f.definition["plan_contract_sha256"] = "0" * 64
    with pytest.raises(ApplicationError, match="could not be prepared"):
        await f.activities.prepare(str(run.id))
    assert f.router.generate.await_count == 0


async def test_storage_stages_the_plan_file_and_nothing_else_for_this_executor():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    # Production refused every plan at the save step: the staging contract knew only the
    # writing-style file. This runs the real contract, which the activity tests replace.
    storage = object.__new__(CodeStorage)
    storage.procedure_checkpoint_revision = AsyncMock(return_value=None)
    builder = SimpleNamespace(send=AsyncMock(return_value={"commit_sha": "b" * 40}))
    builder.add_file = lambda *args: builder
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return builder

    storage.get_repo = AsyncMock(return_value=SimpleNamespace(create_commit=create))
    args = dict(
        repo_id="project",
        branch="main",
        run_id=str(uuid4()),
        generation=0,
        path=GROWTH_ONBOARDING_PLAN_PATH,
        content=b"# Growth plan\n" * 2000,
        executor=plan.KEY,
    )
    assert len(args["content"]) > 24_000
    assert await storage.stage_native_output(**args) == "b" * 40
    assert calls[0]["ephemeral"] is True
    assert calls[0]["target_branch"] == f"native-plan/{args['run_id']}/0"

    for bad in (
        {"executor": "style.capture"},
        {"path": "reports/OTHER.md"},
        {"content": b"x" * (plan.POLICY["output_max_bytes"] + 1)},
        {"content": b""},
    ):
        with pytest.raises(ValueError, match="invalid native output checkpoint"):
            await storage.stage_native_output(**{**args, **bad})
    assert len(calls) == 1


class ReceiptsOnly:
    """Just enough of the database for the write and failure activities, without Postgres."""

    def __init__(self, run):
        from contextlib import asynccontextmanager

        from tin_lite.domain import EffectReceipt

        self.run, self.receipts, self.failures = run, {}, []
        self.Receipt = EffectReceipt

        @asynccontextmanager
        async def effect_lock(key, operation):
            yield None, self.receipts.get(key)

        self.effect_lock = effect_lock

    async def get_run(self, run_id, conn=None):
        return self.run

    async def get_effect(self, key):
        return self.receipts.get(key)

    async def project_run_progress(self, **_kwargs):
        return None

    async def start_effect(self, conn, *, execution_key, operation):
        self.receipts.setdefault(
            execution_key, self.Receipt(execution_key, operation, "started", None)
        )

    async def fail_effect(self, conn, *, execution_key, error_message):
        old = self.receipts[execution_key]
        self.receipts[execution_key] = self.Receipt(
            execution_key, old.operation, "failed", None, error_message
        )

    async def complete_effect(self, conn, *, execution_key, result):
        self.receipts[execution_key] = self.Receipt(execution_key, plan.KEY, "completed", result)

    async def project_failure(self, *, run_id, error_message):
        self.failures.append(error_message)


def receipts_fixture(monkeypatch, model):
    from types import SimpleNamespace
    from uuid import uuid4

    from tin_lite.domain import EffectReceipt
    from tin_lite.growth_plan_activities import GrowthPlanActivities

    run = SimpleNamespace(
        id=uuid4(),
        executor=plan.KEY,
        status=SimpleNamespace(value="running"),
        input={k: v for k, v in inputs(product_url="").items() if k != "tin_state"},
    )
    db = ReceiptsOnly(run)
    context = {"tin_state": tin_state(), "site": NO_SITE, "memory": "", "today": TODAY}
    db.receipts[f"{run.id}:plan_context"] = EffectReceipt(
        f"{run.id}:plan_context", plan.KEY, "completed", context
    )
    activities = GrowthPlanActivities(database=db, storage=None, settings=None, router=None)

    async def generate(self, run, step, system, user, schema, max_out, effort):
        return await model(step, system, user, schema, max_out, effort)

    monkeypatch.setattr(GrowthPlanActivities, "_generate", generate)
    monkeypatch.setattr("tin_lite.growth_plan_activities.activity.heartbeat", lambda *_a: None)
    return db, activities, run


async def test_a_site_less_write_saves_a_plan_when_every_step_declines(monkeypatch):
    db, activities, run = receipts_fixture(monkeypatch, declines_everything())
    await activities.write(str(run.id))
    document = db.receipts[f"{run.id}:plan_document"].result
    plan.validate_plan(document["text"], tin_state())
    assert document["report"]["fallback"]["system"] in plan.SYSTEM_IDS
    assert f"{run.id}:plan_write_failed" not in db.receipts


async def test_a_refused_plan_says_why_and_never_points_to_files(monkeypatch):
    from temporalio.exceptions import ApplicationError

    # Nothing in this project is configurable without the model, so even the fallback is empty.
    db, activities, run = receipts_fixture(monkeypatch, declines_everything())
    for w in db.receipts[f"{run.id}:plan_context"].result["tin_state"]["workflows"]:
        if w["key"] == "content.answer_page":
            w["required_inputs"] = ["question"]
    with pytest.raises(ApplicationError, match="final check: the plan offers no system") as exc:
        await activities.write(str(run.id))
    assert "unusable" not in str(exc.value)
    assert f"{run.id}:plan_document" not in db.receipts

    await activities.failure(str(run.id))
    assert db.failures == [str(exc.value)]
    assert "Nothing was saved" in db.failures[0] and "Check Files" not in db.failures[0]


async def test_unusable_model_results_keep_their_own_message(monkeypatch):
    from temporalio.exceptions import ApplicationError

    model = FakeModel(unusable=["scope", "scope:retry"])
    db, activities, run = receipts_fixture(monkeypatch, model)
    with pytest.raises(ApplicationError, match="model results were unusable"):
        await activities.write(str(run.id))
    await activities.failure(str(run.id))
    assert db.failures == ["The plan's model results were unusable. Nothing was saved; try again."]


async def test_failure_before_anything_was_written_does_not_send_the_founder_to_files(
    monkeypatch,
):
    from tin_lite.domain import EffectReceipt

    db, activities, run = receipts_fixture(monkeypatch, FakeModel())
    await activities.failure(str(run.id))
    assert db.failures[-1] == "The growth plan could not be written. Nothing was saved; try again."
    # Once the save step has started, a file may exist: the founder is told to check Files.
    key = f"{run.id}:plan_artifact_persist"
    db.receipts[key] = EffectReceipt(key, plan.KEY, "started", None)
    await activities.failure(str(run.id))
    assert "Check Files for a saved result" in db.failures[-1]


def test_onboarding_offers_no_social_channel():
    # Which social channel a founder uses is decided after onboarding, by the founder, not
    # chosen as a setup first run for them.
    offered = {key for row in plan.PROGRAMS["programs"] for key in row["tin"]["workflows"]}
    assert not {key for key in offered if key.startswith("social.")}
