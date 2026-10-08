"""Calibrated cost estimates: what a run usually costs, separate from its spending ceiling.

An estimate is the 90th percentile of what runs of a workflow actually cost, authored here
as a constant. It is what admission checks against credits and project limits and what a
run in progress sets aside. The ceiling (maximum_nanos) stays the hard stop per run: every
paid call still reserves against it. Estimates are never learned at runtime and never read
from another user's runs. Amounts are USD decimal strings; an estimate is never above its
run's ceiling and never free for a paid run.
"""

from decimal import Decimal

from tin_lite.billing_contracts import NANOS_PER_CENT, NANOS_PER_DOLLAR

POLICY = "calibrated-p90-v1"
BASIS = "calibrated_p90"

# Each workflow's p90 cost per run in USD, at today's rates, over the 60 days to 2026-10-08
# (billed runs plus priced unbilled ones; earlier Codex runs repriced), rounded up to a
# friendly figure. Workflows with fewer than five runs are judged, not measured. A workflow
# without an entry uses its family default below. Change POLICY when the numbers change
# materially, so issued estimate ids change too.
ESTIMATES_USD: dict[str, str] = {
    # Organic traffic system children. organic.audit is by policy: AUDIT_V15_USD below.
    "organic.audit": "0.55",
    "organic.keyword_plan": "0.50",
    "organic.content_efficacy": "0.01",
    # content.plan before 1.0.0: one model call. 1.0.0 (the agent planner) buys nothing itself
    # and is estimated at its planning agent, content.plan_research.
    "content.plan": "0.02",
    "content.plan_research": "1.50",
    "content.generate": "1.25",
    "content.refresh": "0.20",
    "website.change": "0.60",
    # Retired; kept for pinned runs, retries and saved schedules.
    "content.deliver": "0.75",
    "content.blog_index": "0.75",
    "organic.technical_fix": "0.30",
    # Codex procedures.
    "product.code_map": "2.00",
    "product.deep_dive": "2.50",
    "qa.product_audit": "2.50",
    "project.task": "1.00",
    "research.deep_dive": "1.25",
    "content.public_article": "1.20",
    "outreach.awesome_lists": "1.75",
    "outreach.email_shortlist": "1.00",
    "product.analytics_brief": "0.50",
    "organic.error_surface": "2.25",
    "organic.mention_backlinks": "1.25",
    "content.diagram": "1.20",
    "creative.product_demo": "1.50",
    "qa.signup_walkthrough": "1.00",
    "outreach.newsletter_placements": "1.00",
    "outreach.syllabus_placement": "1.00",
    "outreach.podcast_guest": "1.00",
    "growth.free_tool": "1.50",
    "brand.capture": "0.75",
    "outreach.community_threads": "0.75",
    "outreach.speaking_shortlist": "0.75",
    "competitor.watch": "0.50",
    "site.health_improve": "0.55",
    "outreach.marketplace_listings": "0.50",
    "outreach.campus_events": "0.50",
    "competitor.sunset_rescue": "0.50",
    "growth.signup_source": "0.60",
    "content.design_md": "0.60",
    "qa.buyer_trust": "0.30",
    # Five hosted runs of its private copy cost $0.52 to $0.59.
    "qa.feedback_to_fix": "0.75",
    "growth.framework_starter": "0.30",
    # Native workflows.
    "creative.character": "0.40",
    "style.capture": "0.15",
    "social.x_style": "0.03",
    "social.x_revise": "0.02",
    "content.answer_page": "0.04",
    "project.memory": "0.02",
    "project.weekly_brief": "0.01",
    "scan.report": "0.01",
    "visibility.audit": "0.16",
    "ads.assessment": "0.70",
    "ads.launch": "0.50",
    "ads.monitor": "0.15",
    "revenue.payment_recovery": "0.06",
    # Measured as a whole (a captured voice skips its style step), not as its children's sum.
    "social.x_draft": "0.03",
    # Included as Start here; this is only what it would cost otherwise.
    "growth.onboarding_plan": "0.65",
    # workflow.code packages with observed costs.
    "social.x_compose": "0.03",
    "social.content_plan": "0.02",
    "social.post_batch": "0.02",
}
# organic-audit-v15 asks sixteen questions, not twelve (judged; earlier versions measured).
AUDIT_V15_USD = "0.75"
# website.change by its `source` input. A source without an entry uses its overall p90.
WEBSITE_CHANGE_SOURCE_USD: dict[str, str] = {
    "content_draft": "0.60",
    "audit": "0.80",
}
# organic-traffic-v8's own children: audit, planning agent, Page decisions, first draft and
# first refresh. The keyword plan is added at its estimate, capped by the founder's keyword
# limit, and delivery and technical fixes at WEBSITE_CHANGE_SOURCE_USD. It is below the sum of
# those children's p90s ($3.71): p90s do not add, and whole runs before v8 had a p90 of $2.06.
TRAFFIC_SYSTEM_V8_BASE_USD = "3.00"
# Family defaults for workflows without their own entry.
CODEX_PROCEDURE_USD = "1.00"  # Codex procedures, including private custom.* procedures.
NATIVE_MODEL_USD = "0.25"  # Native model workflows.
# workflow.code packages: a share of the bound derived from their declared routes and services.
CODE_SHARE = Decimal("0.30")


def _nanos(usd: str) -> int:
    return int(Decimal(usd) * NANOS_PER_DOLLAR)


def _audit_v15(definition: dict) -> bool:
    version = (definition.get("audit_policy") or {}).get("version") or ""
    number = version.rsplit("v", 1)[-1]
    return number.isdigit() and int(number) >= 15


def _key_nanos(key: str, inputs: dict | None = None) -> int | None:
    if key == "website.change" and (inputs or {}).get("source") in WEBSITE_CHANGE_SOURCE_USD:
        return _nanos(WEBSITE_CHANGE_SOURCE_USD[inputs["source"]])
    return _nanos(ESTIMATES_USD[key]) if key in ESTIMATES_USD else None


def _step(key: str, executor: str, inputs: dict | None = None) -> int:
    """A child step's estimate by its key, else its family's default."""
    amount = _key_nanos(key, inputs)
    if amount is not None:
        return amount
    return _nanos(CODEX_PROCEDURE_USD if executor == "codex.procedure" else NATIVE_MODEL_USD)


def _organic_system(definition: dict, inputs: dict) -> int:
    """The traffic system's own run, as service_pricing composes its ceiling.

    organic-traffic-v8 is measured as a whole (TRAFFIC_SYSTEM_V8_BASE_USD); earlier versions
    sum the children their run starts. The traffic snapshot makes no paid call. Later weekly
    runs are ordinary scheduled runs with their own estimates, not part of this one.
    """
    from tin_lite.service_pricing import amount_nanos

    version = (definition.get("organic_system_policy") or {}).get("version")
    number = int(version.rsplit("v", 1)[1]) if version and version[-1].isdigit() else 1
    keywords = _step("organic.keyword_plan", "organic.keyword_plan")
    keyword_maximum = amount_nanos(inputs.get("keyword_max_cost_usd", 2))
    if keyword_maximum is not None:
        keywords = min(keywords, keyword_maximum)
    writer = "website.change" if number >= 6 else None
    if number >= 8:
        total = _nanos(TRAFFIC_SYSTEM_V8_BASE_USD) + keywords
    else:
        total = _step("organic.audit", "organic.audit") + keywords
        total += _step("content.plan", "content.plan")
        if number >= 7:
            total += _step("organic.content_efficacy", "workflow.code")
        if number >= 2:
            total += _step("content.generate", "codex.procedure")
        if number >= 5:
            total += _step("content.refresh", "codex.procedure")
    if inputs.get("technical_fix"):
        total += (
            _step(writer, "codex.procedure", {"source": "audit"})
            if writer
            else _step("organic.technical_fix", "codex.procedure")
        )
    if number >= 2 and inputs.get("content_delivery", "auto") == "auto":
        total += (
            _step(writer, "codex.procedure", {"source": "content_draft"})
            if writer
            else _step("content.deliver", "codex.procedure")
        )
    return total


def estimate_nanos(definition: dict, inputs: dict | None, maximum: int) -> int:
    """A run's calibrated estimate, at most its ceiling and at least a cent (or the ceiling,
    when that is smaller): a paid run is never estimated as free."""
    from tin_lite.service_pricing import agent_planner

    inputs = inputs or {}
    key, executor = definition.get("key") or "", definition.get("executor")
    if executor == "organic.traffic_system":
        amount = _organic_system(definition, inputs)
    elif agent_planner(definition):
        amount = _step("content.plan_research", "codex.procedure")
    elif executor == "organic.audit" and _audit_v15(definition):
        amount = _nanos(AUDIT_V15_USD)
    elif (direct := _key_nanos(key, inputs)) is not None:
        amount = direct
    elif executor == "social.x_draft":
        from tin_lite.x_draft import STEPS

        amount = sum(_step(child, "workflow.code") for child in STEPS.values())
    elif executor == "growth.onboarding":
        # Included when it is Start here; otherwise its fixed child's usual cost.
        amount = _step("growth.onboarding_plan", "growth.onboarding_plan")
    elif executor == "workflow.code":
        # Rounded up to the cent below, so any paid route is at least $0.01.
        amount = int(Decimal(maximum) * CODE_SHARE)
    else:
        amount = _step(key, executor)
    # Whole cents, as the founder sees them; never above the ceiling.
    amount = -(-amount // NANOS_PER_CENT) * NANOS_PER_CENT
    return min(max(amount, NANOS_PER_CENT), maximum)
