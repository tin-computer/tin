from __future__ import annotations

# The order workflows appear in within their group on the dashboard and in list_workflows.
# Within a group, listed keys come first in this order, then the rest by key. It is
# presentation only: not part of any workflow definition, so changing it pins nothing.
WORKFLOW_DISPLAY_ORDER: tuple[str, ...] = (
    # Organic traffic, in the order the steps feed each other. The traffic system runs
    # the audit, keyword plan, content plan and drafting for you, so it leads.
    "organic.traffic_system",
    "organic.audit",
    "organic.prompt_panel",
    "organic.keyword_plan",
    "organic.traffic_snapshot",
    "organic.content_efficacy",
    "organic.site_architecture",
    "content.blog_index",
    "style.capture",
    "content.plan",
    "content.generate",
    "content.deliver",
)
