"""content.plan 1.0.0 (content-editorial-v9): a planning agent proposes, Tin fills every week.

Offline: the agent's run is faked; its portfolio is a fixture, including a plausible but
unusable one. No model, sandbox or provider is called.
"""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_content_plan import fixture_plan
from test_content_plan_editorial import pages
from test_content_programs import Storage, setup
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_plan as legacy
from tin_lite import content_plan_agent as agent
from tin_lite import content_plan_editorial as editorial
from tin_lite.catalog import BUILTIN_WORKFLOWS, RETIRED_BUILTIN_WORKFLOW_IDS
from tin_lite.organic_audit import canonical_json
from tin_lite.procedures import (
    validate_codex_procedure_definition,
    validate_procedure_artifact,
)
from tin_lite.service_pricing import service_terms


def context(*, capacity=2, decisions=None):
    plan = fixture_plan()
    return {
        "mode": "initial",
        "plan": plan,
        "editable": [b["id"] for b in plan["batches"]],
        "capacity": capacity,
        "instruction": "Create the initial roadmap.",
        "files": [],
        "source_revision": "c" * 40,
        "research": {
            "scope": {"host": "example.com", "market": "US", "buyer_context": "API product"},
            "rows": [
                {"source_id": "keyword:k1", "data": {"keyword": "linear alternative"}},
                {"source_id": "group:g1", "data": {"title": "comparisons"}},
            ],
            "excluded": [],
            "site_pages": {
                "pages": [
                    {"path": "/pricing", "sources": ["sitemap"], "title": "Pricing"},
                    {"path": "/blog/old-guide", "sources": ["sitemap"], "title": "Old guide"},
                    {"path": "/integrations/slack", "sources": ["crawl"], "title": "Slack"},
                ],
                "omitted": 0,
                "by_source": {"sitemap": 2, "crawl": 1},
            },
            "site_signals": {"page_decisions": decisions or {"status": "not read"}},
        },
    }


def opportunity(index, **fields):
    return {
        "id": f"op{index:03d}",
        "format": "alternative",
        "title": f"Distinct page {index}",
        "target_query": f"query {index}",
        "intent": f"Buyer intent {index}",
        "action": "new_page",
        "destination": "",
        "brief": f"Who reads page {index}, the angle, the sections and what not to claim.",
        "sources": ["keyword:k1"],
        "evidence_strength": "measured",
        "why_this": "It beat the other comparisons on measured demand.",
        "win_case": "The top results are thin directory listings.",
        "metric": "Signups from comparison searches.",
        "rejected": ["A generic best-tools list"],
        "verification": ["Confirm each product claim in brand/BRAND.md."],
        **fields,
    }


def portfolio(count=3, **fields):
    return {
        "schema": agent.SCHEMA,
        "strategy": "Comparison pages first for the competitors buyers name, then answers.",
        "competitors": [{"name": "Linear", "host": "linear.app", "use": "alternative"}],
        "families": [{"id": "integrations", "name": "Integrations", "members": ["Slack"]}],
        "opportunities": [opportunity(i, **fields) for i in range(1, count + 1)],
        "gaps": ["A keyword lookup for integration searches would unlock more family pages."],
        "excluded": ["No security pages: the files make no compliance claims."],
    }


def document(value):
    return f"# Portfolio\n\nSummary.\n\n{agent.START}\n{json.dumps(value)}\n{agent.END}\n"


def test_the_catalog_pins_the_agent_contract_and_its_hidden_planner():
    plan = next(w for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY)
    assert plan.version_label == "1.0.0"
    pinned = editorial.contract(plan.definition)
    assert pinned.AGENT and pinned.TYPED
    assert pinned.POLICY["version"] == "content-editorial-v9"
    assert pinned.POLICY["planner"] == agent.RESEARCH_KEY
    research = next(w for w in BUILTIN_WORKFLOWS if w.key == agent.RESEARCH_KEY)
    assert research.id not in RETIRED_BUILTIN_WORKFLOW_IDS
    definition, files = research.definition_and_resource_files()
    assert definition["public_discovery"] is False
    assert definition["procedure"]["output"]["validator"] == agent.VALIDATOR
    assert definition["procedure"]["output"]["path_template"] == agent.PATH_TEMPLATE
    assert any(path.endswith("check_portfolio.py") for path in files)


def test_the_portfolio_check_needs_only_the_shape_tin_reads():
    value = agent.validate(document(portfolio(2)))
    assert len(value["opportunities"]) == 2
    for bad in (
        "no markers",
        document({**portfolio(1), "schema": "other/1"}),
        document({**portfolio(1), "opportunities": []}),
        document({**portfolio(1), "strategy": " "}),
        document({**portfolio(1), "opportunities": [{"title": "x"}]}),
        f"{agent.START}\nnot json\n{agent.END}",
    ):
        with pytest.raises(ValueError):
            agent.validate(bad)
    research = next(w for w in BUILTIN_WORKFLOWS if w.key == agent.RESEARCH_KEY)
    parsed = validate_codex_procedure_definition(research.definition)
    assert parsed.output_validator == agent.VALIDATOR
    spec = SimpleNamespace(
        result_kind="project.artifact",
        output_max_bytes=agent.MAX_PORTFOLIO_BYTES,
        documents=None,
        output_validator=agent.VALIDATOR,
        binary_output=False,
    )
    validate_procedure_artifact(document(portfolio(1)).encode(), spec=spec)
    with pytest.raises(ValueError):
        validate_procedure_artifact(b"# nothing", spec=spec)


def test_every_week_is_filled_in_priority_order_and_the_rest_is_backlog():
    data = context()
    kept, left_out = agent.normalize(data, pages(), portfolio(60))
    assert not left_out
    plan, backlog = agent.fill(data, kept)
    plan = legacy.validate_change(data["plan"], plan, editable=set(data["editable"]))
    ids = [i["id"] for b in plan["batches"] for i in b["items"]]
    assert ids == [f"op{i:03d}" for i in range(1, 53)]
    assert all(len(b["items"]) == 2 for b in plan["batches"])
    assert [i["id"] for i in backlog] == [f"op{i:03d}" for i in range(53, 61)]
    # A short portfolio fills the first weeks, not a spread over the horizon.
    kept, _ = agent.normalize(data, pages(), portfolio(5))
    plan, backlog = agent.fill(data, kept)
    assert [len(b["items"]) for b in plan["batches"][:4]] == [2, 2, 1, 0] and not backlog
    quality = agent.coverage(data, kept, [], backlog, portfolio(5), plan)
    assert quality["planned_items"] == 5 and quality["first_empty_week"] == "week_04"


def test_items_keep_their_format_strength_and_kind():
    data = context()
    value = portfolio(0)
    value["opportunities"] = [
        opportunity(1),
        opportunity(2, format="answer", title="Which tool sends invoices from Slack?"),
        opportunity(
            3,
            format="refresh",
            action="update_page",
            destination="/pricing",
            evidence_strength="measured",
            sources=["https://competitor.example/pricing"],
        ),
        opportunity(4, format="family_page", title="Slack approvals", evidence_strength="bet"),
        opportunity(5, format="comparison", action="update_page", destination="/blog/old-guide"),
    ]
    kept, left_out = agent.normalize(data, pages(), value)
    assert not left_out
    items = {entry["item"]["id"]: entry["item"] for entry in kept}
    assert items["op001"]["format"] == "alternative" and "kind" not in items["op001"]
    assert items["op002"]["kind"] == "answer" and items["op002"]["destination"] == ""
    assert items["op003"]["kind"] == "refresh"
    assert items["op003"]["destination"] == "https://example.com/pricing"
    # Measured needs a measured source; a competitor page is inferred evidence.
    assert items["op003"]["evidence_strength"] == "inferred"
    assert items["op004"]["evidence_strength"] == "bet"
    # A comparison that changes an existing page is an update of that page.
    assert items["op005"]["format"] == "update" and "kind" not in items["op005"]
    plan, _ = agent.fill(data, kept)
    parsed = legacy.parse_plan(
        canonical_json(legacy.validate_change(data["plan"], plan, editable=set(data["editable"])))
    )
    assert parsed["batches"][0]["items"][0]["format"] == "alternative"
    report = legacy.render_plan(
        parsed,
        label="Content roadmap",
        editorial=agent.coverage(data, kept, [], [], value, plan),
        pages=pages(),
    )
    assert "Format: alternatives page · evidence measured" in report
    assert "Why it can win: The top results are thin directory listings" in report
    assert "## Coverage" in report and "Capacity is not a quota" not in report


def test_unusable_proposals_are_left_out_with_a_reason_not_a_failure():
    data = context(
        decisions={
            "status": "used",
            "generated": "2026-10-01",
            "keep": ["/integrations/slack"],
            "cut": {},
            "refresh": {},
            "rewrite": {},
        }
    )
    value = portfolio(0)
    value["opportunities"] = [
        opportunity(1, title="Pricing"),  # the site already has this page
        opportunity(2, format="update", action="update_page", destination="/missing"),
        opportunity(3, format="update", action="update_page", destination="https://other.com/x"),
        opportunity(4, format="update", action="update_page", destination="/integrations/slack"),
        opportunity(5, title="Distinct page 6"),
        opportunity(6),  # repeats op005's title
        opportunity(7, brief=""),
        "not an object",
    ]
    kept, left_out = agent.normalize(data, pages(), value)
    assert [entry["item"]["id"] for entry in kept] == ["op005"]
    reasons = " | ".join(entry["reason"] for entry in left_out)
    for expected in (
        "the site already has /pricing",
        "/missing is not a page Tin knows",
        "an update needs a page on the site",
        "Page decisions keeps /integrations/slack",
        "repeats another item's title",
        "no title or brief",
    ):
        assert expected in reasons
    # A plausible but wholly unusable portfolio still yields a plan with named reasons.
    value["opportunities"] = value["opportunities"][:4]
    kept, left_out = agent.normalize(data, pages(), value)
    plan, backlog = agent.fill(data, kept)
    quality = agent.coverage(data, kept, left_out, backlog, value, plan)
    assert quality["planned_items"] == 0 and len(quality["left_out"]) == 4
    strategy = agent.strategy(value, quality)
    assert "Tin left out 4 proposal(s)" in strategy and "0 of 52 slots" in strategy


def test_sources_are_research_ids_small_project_files_or_web_pages():
    data = context()
    value = portfolio(1)
    value["opportunities"][0]["sources"] = [
        "keyword:k1",
        "keyword:unknown",
        "file:brand/BRAND.md",
        "reports/keyword-plan/x/keywords.json",
        "https://linear.app/pricing",
        "http://insecure.example",
    ]
    kept, _ = agent.normalize(
        data,
        pages(),
        value,
        file_sizes={"brand/BRAND.md": 4_000, "reports/keyword-plan/x/keywords.json": 90_000},
    )
    item = kept[0]["item"]
    assert item["source_ids"] == [
        "keyword:k1",
        "file:brand/BRAND.md",
        "https://linear.app/pricing",
    ]
    assert kept[0]["rationale"]["files"] == "reports/keyword-plan/x/keywords.json"


def test_page_decisions_refreshes_the_agent_left_follow_its_items():
    data = context(
        decisions={
            "status": "used",
            "generated": "2026-10-01",
            "keep": [],
            "cut": {},
            "rewrite": {},
            "refresh": {"/blog/old-guide": {"rule": "low_ctr", "reason": "Seen, rarely clicked."}},
        }
    )
    from tin_lite.content_plan_sources import EFFICACY_SOURCE_PREFIX
    from tin_lite.organic_audit import digest

    data["research"]["rows"].append(
        {"source_id": EFFICACY_SOURCE_PREFIX + digest("/blog/old-guide")[:20], "data": {}}
    )
    kept, _ = agent.normalize(data, pages(), portfolio(3))
    kept = agent.with_decision_refreshes(data, kept)
    plan, _ = agent.fill(data, kept)
    # The agent's own order stands; the refresh it left unanswered takes the next open slot.
    added = plan["batches"][1]["items"][1]
    assert [i["id"] for i in plan["batches"][0]["items"]] == ["op001", "op002"]
    assert added["kind"] == "refresh" and added["source"] == legacy.CONTENT_EFFICACY
    assert added["format"] == "refresh"
    # One the agent planned is not added again.
    value = portfolio(1)
    value["opportunities"][0].update(
        format="refresh", action="update_page", destination="/blog/old-guide"
    )
    kept, _ = agent.normalize(data, pages(), value)
    assert len(agent.with_decision_refreshes(data, kept)) == 1
    # Nor is one Page decisions also keeps.
    data["research"]["site_signals"]["page_decisions"]["keep"] = ["/blog/old-guide"]
    kept, _ = agent.normalize(data, pages(), portfolio(1))
    assert len(agent.with_decision_refreshes(data, kept)) == 1


def test_a_revision_keeps_retained_items_in_their_week():
    data = context()
    kept, _ = agent.normalize(data, pages(), portfolio(6))
    plan, _ = agent.fill(data, kept)
    revision = {**data, "mode": "revision", "plan": plan, "editable": ["week_02", "week_03"]}
    value = portfolio(0)
    value["opportunities"] = [
        opportunity(6, id="op006", title="Distinct page 6 revised"),
        opportunity(9, title="A new page"),
    ]
    kept, left_out = agent.normalize(revision, pages(), value)
    assert not left_out
    revised, _ = agent.fill(revision, kept)
    assert [i["id"] for i in revised["batches"][2]["items"]] == ["op006"]
    assert [i["id"] for i in revised["batches"][1]["items"]] == ["op009"]
    assert revised["batches"][0] == plan["batches"][0]


def test_competitors_merge_earlier_steps_and_keep_cited_sites_apart():
    audit = {
        "ai_visibility": {
            "panel": {"competitor_names": ["Canva", "YouTube"]},
            "cited_domains": [
                {"domain": "www.canva.com", "answers": 6},
                {"domain": "helpx.adobe.com", "answers": 4},
                {"domain": "docs.adobe.com", "answers": 1},
                {"domain": "reddit.com", "answers": 3},
            ],
        }
    }
    keyword = {"competitors": ["fotor.com", "facebook.com", {"host": "canva.com"}]}
    watch_report = (
        "Status: quiet\n\n```tin-competitor-watch\n"
        + json.dumps({"competitors": [{"id": "fotor.com", "name": "Fotor", "pages": []}]})
        + "\n```\n"
    )
    competitors, cited = agent.merge_competitors(
        host="example.com", audit=audit, keyword_evidence=keyword, watch_report=watch_report
    )
    rows = {row["name"]: row for row in competitors}
    assert set(rows) == {"Canva", "Fotor"}
    assert rows["Canva"]["host"] == "canva.com" and rows["Canva"]["corroborated"]
    assert set(rows["Canva"]["sources"]) == {
        "audit_buyer_panel",
        "keyword_search_competitor",
        "audit_cited_in_ai_answers",
    }
    assert rows["Fotor"]["sources"] == ["keyword_search_competitor", "competitor_watch"]
    assert cited == [{"domain": "adobe.com", "answers": 5}]


def test_the_brief_holds_the_program_competitors_and_sources():
    data = context()
    data["research"]["rows"].append({"source_id": "page:abc", "data": {}})
    documents = agent.brief_documents(
        data,
        pages(),
        competitors=[
            {"name": "Linear", "host": "linear.app", "sources": ["a", "b"], "corroborated": True}
        ],
        sources={"Brand guide": "brand/BRAND.md"},
        run_id=uuid4(),
        cited_sites=[{"domain": "adobe.com", "answers": 5}],
    )
    by_name = {path.rsplit("/", 1)[1]: content for path, content in documents.items()}
    assert set(by_name) == set(agent.BRIEF_FILES)
    program = json.loads(by_name["context.json"])["program"]
    assert program["slots"] == 52 and len(program["weeks"]) == 26
    brief = by_name["BRIEF.md"].decode()
    assert "Linear (linear.app): a, b · corroborated" in brief
    assert "brand/BRAND.md" in brief and "adobe.com (5 answers)" in brief


def test_the_agent_plan_is_a_spending_parent_of_its_ceiling():
    plan = next(w for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY).definition
    terms = service_terms(plan)
    assert terms["kind"] == "parent" and terms["operations"] == []
    assert terms["maximum_nanos"] == 6_000_000_000
    v8 = {**deepcopy(plan), "content_policy": editorial.V8_POLICY}
    assert service_terms(v8)["kind"] == "metered_workflow"
    # One call of at most $0.088 (2026-10-08 calibration: $0.25 rather than the $2 default).
    assert service_terms(v8)["maximum_nanos"] == 250_000_000


class AgentStorage(Storage):
    def __init__(self):
        super().__init__()
        self.research_definition = next(
            w for w in BUILTIN_WORKFLOWS if w.key == agent.RESEARCH_KEY
        ).definition

    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        if repo_id == "registry/workflows" and path.endswith(f"{agent.RESEARCH_KEY}.json"):
            return canonical_json(self.research_definition)
        return await super().read_canonical_artifact(
            repo_id=repo_id, commit_sha=commit_sha, path=path
        )

    async def list_canonical_files_at(self, *, repo_id, revision):
        return sorted(self.repo.trees[revision])


async def test_a_plan_starts_its_agent_then_fills_the_weeks_from_its_portfolio(
    publication_db, monkeypatch
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    # Pin the current (agent) contract on the saved workflow.
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY)
    agent_storage = AgentStorage()
    agent_storage.repo = storage.repo
    agent_storage.definition = deepcopy(spec.definition)
    activities.storage = agent_storage
    activities.programs.storage = agent_storage
    research = next(w for w in BUILTIN_WORKFLOWS if w.key == agent.RESEARCH_KEY)
    await db.upsert_registry_workflow(
        workflow_id=research.id,
        key=research.key,
        title=research.title,
        description=research.description,
        executor=research.executor,
        definition_repo_id="registry/workflows",
        definition_path=research.definition_path,
        current_commit_sha="d" * 40,
        version_label=research.version_label,
        definition=research.definition,
    )
    started = {}

    async def start(**kwargs):
        started.update(kwargs)
        child, _ = await db.create_run(
            project_id=kwargs["project_id"],
            workflow_id=research.id,
            input_payload=kwargs["input_payload"],
            start_idempotency_key=kwargs["start_idempotency_key"],
            definition_commit_sha=kwargs["definition_commit_sha"],
        )
        return SimpleNamespace(
            id=child.id, executor="codex.procedure", temporal_workflow_id=f"wf-{child.id}"
        )

    monkeypatch.setattr("tin_lite.run_service.start_workflow_run", start)
    run = await create()
    child = await activities.content_plan_research(str(run.id))
    assert child["executor"] == "codex.procedure" and model.calls == 0
    assert started["start_idempotency_key"] == f"content:{run.id}:research"
    assert started["input_payload"]["slots"] == 52
    # Retries reuse the saved child; the brief was published once.
    assert await activities.content_plan_research(str(run.id)) == child
    head = agent_storage.repo.head
    brief = agent.brief_paths(run.id)
    assert all(path in agent_storage.repo.trees[head] for path in brief.values())
    # The agent writes its portfolio; content_plan_execute turns it into the plan.
    output = agent.path_template(child["run_id"])
    revision = agent_storage.repo.edit({output: document(portfolio(8)).encode()})
    await db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, artifact_path=$3, "
        "finished_at=now(), lease_active=false WHERE id=$1",
        __import__("uuid").UUID(child["run_id"]),
        revision,
        output,
    )
    await activities.content_plan_execute(str(run.id))
    saved = await db.get_run(run.id)
    assert saved.status.value == "succeeded" and model.calls == 0
    plan = legacy.parse_plan(
        await agent_storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=legacy.plan_path(configured.id),
        )
    )
    assert [len(b["items"]) for b in plan["batches"][:5]] == [2, 2, 2, 2, 0]
    evidence = json.loads(
        await agent_storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=legacy.paths(str(run.id))["evidence.json"],
        )
    )
    assert evidence["editorial"]["portfolio"]["run_id"] == child["run_id"]
    assert evidence["policy_version"] == "content-editorial-v9"


async def test_a_plan_whose_agent_failed_names_why(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY)
    storage.definition = deepcopy(spec.definition)
    run = await create()
    with pytest.raises(Exception, match="could not validate"):
        await activities.content_plan_execute(str(run.id))
    failure = await activities.saved(run.id, "failure")
    assert failure["summary"] == "The planning agent never started; start the plan again."
