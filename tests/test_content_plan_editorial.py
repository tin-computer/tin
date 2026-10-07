from copy import deepcopy

import jsonschema
import pytest
from test_content_plan import fixture_plan, item

from tin_lite import content_plan as legacy
from tin_lite import content_plan_editorial as editorial
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import canonical_json


def context():
    plan = fixture_plan()
    return {
        "mode": "initial",
        "plan": plan,
        "editable": [b["id"] for b in plan["batches"]],
        "capacity": 2,
        "instruction": "Create the initial roadmap.",
        "files": [],
        "research": {
            "scope": {"host": "example.com", "market": "US", "buyer_context": "API product"},
            "rows": [{"source_id": "keyword:k1", "data": {"keyword": "API setup"}}],
            "page_candidates": [{"url": "https://example.com/pricing"}],
            "excluded": [{"keyword_id": "k1", "reason": "Fallible model judgment"}],
        },
    }


def portfolio(count=1):
    return {
        "strategy": "Implementation first, then operations; distinct tasks, not synonyms.",
        "gaps": ["Need measured customer debugging questions before adding troubleshooting work."],
        "excluded": ["Business RCS API support is not established."],
        "opportunities": [
            {
                "id": f"topic_{i}",
                "title": f"Distinct buyer task {i}",
                "intent": f"Solve separate implementation problem {i}",
                "brief": f"Explain implementation problem {i}, with a tested example.",
                "action": "new_page",
                "page_id": "",
                "source_ids": ["s001"],
                "verification": ["Test examples against the current product."],
                "rationale": "The pricing page does not answer this implementation task.",
            }
            for i in range(count)
        ],
    }


def pages():
    return {
        "pages": [
            {
                "page_id": "p001",
                "url": "https://example.com/pricing",
                "status": "inspected",
                "text": "Pricing, billing and plans.",
            }
        ],
        "omitted_candidates": 0,
    }


@pytest.mark.parametrize("capacity", [1, 2, 3])
def test_all_portfolio_sizes_fit_calendar_and_keep_priority(capacity):
    data = context()
    data["capacity"] = capacity
    for count in range(26 * capacity + 1):
        plan, coverage = editorial.allocate(data, portfolio(count), pages(), {"s001": "keyword:k1"})
        assert all(len(b["items"]) <= capacity for b in plan["batches"])
        assert [i["id"] for b in plan["batches"] for i in b["items"]] == [
            f"topic_{i}" for i in range(count)
        ]
        assert coverage["unused_capacity"] == 26 * capacity - count
        if count >= 2:
            assert plan["batches"][-1]["items"]
        assert all(
            i["readiness"] == "needs_verification" for b in plan["batches"] for i in b["items"]
        )
    assert all(not b["items"] for b in data["plan"]["batches"])


@pytest.mark.parametrize("fault", ["source", "page", "new_page", "intent", "gap"])
def test_fail_closed_on_incoherent_portfolio(fault):
    proposal = portfolio(2)
    one, two = proposal["opportunities"]
    if fault == "source":
        one["source_ids"] = ["invented"]
    elif fault == "page":
        one.update(action="update_page", page_id="p_missing")
    elif fault == "new_page":
        one["page_id"] = "p001"
    elif fault == "intent":
        two["intent"] = one["intent"]
    else:
        proposal["gaps"] = []
    with pytest.raises(ValueError):
        editorial.allocate(context(), proposal, pages(), {"s001": "keyword:k1"})


def test_verified_destination_and_aliases_not_model_url_or_identity():
    proposal = portfolio()
    proposal["opportunities"][0].update(action="update_page", page_id="p001")
    data = context()
    model_data, aliases = editorial.model_context(data, pages())
    assert model_data["sources"][0]["source_id"] == "s001"
    assert model_data["research"]["excluded"] == data["research"]["excluded"]
    assert data["research"]["rows"][0]["source_id"] == "keyword:k1"
    schema = editorial.bound_schema(pages(), aliases)
    jsonschema.validate(proposal, schema)
    plan, coverage = editorial.allocate(data, proposal, pages(), aliases)
    result = plan["batches"][0]["items"][0]
    assert result["destination"] == "https://example.com/pricing"
    assert result["source_ids"] == ["keyword:k1"]
    assert "page_id" not in result and "rationale" not in result
    report = legacy.render_plan(plan, label="Content roadmap", editorial=coverage, pages=pages())
    assert "1 briefs" in report and "51 unused slots" in report
    assert "Page decision:" in report and "Live pages have not been rechecked" not in report
    assert "Live pages have not been rechecked" in legacy.render_plan(plan, label="Old plan")


def test_same_page_updates_consolidate_without_losing_sections_sources_or_checks():
    proposal = portfolio(2)
    for opportunity in proposal["opportunities"]:
        opportunity.update(action="update_page", page_id="p001")
    proposal["opportunities"][1]["verification"] = ["Check a separate inbound behavior."]
    original = deepcopy(proposal)
    plan, quality = editorial.allocate(context(), proposal, pages(), {"s001": "keyword:k1"})
    items = [i for b in plan["batches"] for i in b["items"]]
    assert len(items) == 1 and items[0]["id"] == "topic_0"
    for opportunity in original["opportunities"]:
        assert opportunity["brief"] in items[0]["brief"]
        assert set(opportunity["verification"]) <= set(items[0]["verification"])
    assert quality["consolidations"] == [
        {
            "kept_item_id": "topic_0",
            "merged_item_id": "topic_1",
            "destination": "https://example.com/pricing",
        }
    ]
    assert proposal == original
    proposal["opportunities"][0]["brief"] = "a" * 1500
    proposal["opportunities"][1]["brief"] = "b" * 1500
    with pytest.raises(ValueError):
        editorial.allocate(context(), proposal, pages(), {"s001": "keyword:k1"})


def test_same_page_consolidation_cannot_hide_overlap_with_unselected_work():
    data = context()
    data["plan"]["batches"][0]["items"] = [
        {
            **item("frozen"),
            "action": "update_page",
            "destination": "https://example.com/pricing",
        }
    ]
    data["editable"] = ["week_02", "week_03"]
    proposal = portfolio(2)
    for opportunity in proposal["opportunities"]:
        opportunity.update(action="update_page", page_id="p001")
    with pytest.raises(ValueError, match="Consolidate"):
        editorial.allocate(data, proposal, pages(), {"s001": "keyword:k1"})


def test_amendment_only_allocates_selected_batches_and_keeps_item_ids():
    data = context()
    data["mode"] = "revision"
    data["plan"]["batches"][0]["items"] = [item("prepared")]
    data["editable"] = ["week_02", "week_05"]
    proposal = portfolio(3)
    proposal["opportunities"][0]["id"] = "retained_item"
    plan, _ = editorial.allocate(data, proposal, pages(), {"s001": "keyword:k1"})
    for old, new in zip(data["plan"]["batches"], plan["batches"], strict=True):
        if old["id"] not in data["editable"]:
            assert old == new
    assert plan["batches"][1]["items"][0]["id"] == "retained_item"
    proposal["opportunities"][0]["id"] = "prepared"
    with pytest.raises(ValueError, match="unique"):
        editorial.allocate(data, proposal, pages(), {"s001": "keyword:k1"})


def test_amendment_does_not_reschedule_retained_items_when_adding_new_work():
    data = context()
    plan, _ = editorial.allocate(data, portfolio(3), pages(), {"s001": "keyword:k1"})
    data.update(mode="revision", plan=plan)
    before = {i["id"]: b["id"] for b in plan["batches"] for i in b["items"]}
    proposal = portfolio(5)
    proposal["opportunities"][1]["brief"] = "An amended brief without a requested date change."
    updated, _ = editorial.allocate(data, proposal, pages(), {"s001": "keyword:k1"})
    after = {i["id"]: b["id"] for b in updated["batches"] for i in b["items"]}
    assert all(before[id] == after[id] for id in before)
    assert len(after) == 5
    assert all(len(b["items"]) <= 2 for b in updated["batches"])


def test_schema_and_exact_legacy_and_current_policies():
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY)
    assert editorial.contract(definition).POLICY == editorial.POLICY
    old = deepcopy(definition)
    old.update(
        content_policy=legacy.POLICY,
        content_instructions=legacy.INSTRUCTIONS,
        content_schema=legacy.MODEL_SCHEMA,
    )
    old["model_route"]["key"] = legacy.ROUTE_KEY
    assert editorial.contract(old).POLICY == legacy.POLICY
    v2 = deepcopy(definition)
    v2.update(
        content_policy=editorial.V2_POLICY,
        content_instructions=editorial.V2_INSTRUCTIONS,
        content_schema=editorial.PORTFOLIO_SCHEMA,
    )
    assert editorial.contract(v2).POLICY == editorial.V2_POLICY
    definition["content_policy"]["max_pages"] += 1
    with pytest.raises(ValueError, match="pinned"):
        editorial.contract(definition)
    for schema in [editorial.V8_SCHEMA, *editorial.V8_SCHEMA["$defs"].values()]:
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_bounded_page_extraction_excludes_scripts_navigation_and_handles_unicode():
    observed = {
        "url": "https://example.com/guide",
        "observed_at": "2026-09-09T00:00:00Z",
        "sha256": "a" * 64,
        "html": "<nav>Not content</nav><script>steal()</script><h1>Real guide</h1>"
        "<style>hidden</style><p>" + "é好 " * 4000 + "</p>",
    }
    evidence = editorial.page_evidence(observed, observed["url"])
    assert evidence["text"].startswith("Real guide") and evidence["excerpt_truncated"]
    assert not any(s in evidence["text"] for s in ("Not content", "steal", "hidden"))
    assert len(evidence["text"].encode()) <= editorial.POLICY["page_text_bytes"]
    assert "html" not in evidence and evidence["html_sha256"] == "a" * 64


def test_page_selection_rejects_offsite_and_unsafe_urls_and_is_bounded():
    data = context()
    data["research"]["page_candidates"] = [
        {"url": url}
        for url in [
            "https://evil.example/x",
            "https://example.com/a?x=1",
            "https://example.com/a#x",
            "https://example.com/a\n",
            "https://example.com/\\evil",
            "http://example.com/a",
        ]
    ] + [{"url": f"https://example.com/page-{i}"} for i in range(100)]
    urls, omitted = editorial.page_candidates(data)
    assert len(urls) == 60 and omitted == 41
    assert urls[0] == "https://example.com/"
    assert all(editorial.clean_url(u, "example.com") for u in urls)


def test_javascript_shell_and_title_only_are_not_inspected_page_content():
    observation = {
        "url": "https://example.com/",
        "sha256": "a" * 64,
        "observed_at": "2026-09-09T00:00:00Z",
        "html": "<head><title>A very persuasive page title</title></head>"
        "<body><div id='root'></div><script>renderEverything()</script></body>",
    }
    page = editorial.page_evidence(observation, observation["url"])
    assert page["status"] == "unavailable" and page["text"] == ""


def test_readable_aliases_and_automatic_immutable_page_evidence():
    data = context()
    inventory = pages()
    inventory["pages"][0]["source_id"] = "page:" + "a" * 64
    model_input, aliases = editorial.model_context(data, inventory, readable_aliases=True)
    alias = next(iter(aliases))
    assert alias == "s001_api_setup"
    assert model_input["sources"][0]["source_id"] == alias
    assert aliases[alias] == "keyword:k1"
    schema = editorial.bound_schema(inventory, aliases)
    assert schema["$defs"]["Opportunity"]["properties"]["source_ids"]["maxItems"] == 11
    proposal = portfolio()
    proposal["opportunities"][0].update(action="update_page", page_id="p001", source_ids=[alias])
    plan, _ = editorial.allocate(data, proposal, inventory, aliases)
    assert plan["batches"][0]["items"][0]["source_ids"] == ["keyword:k1", "page:" + "a" * 64]


def test_page_excerpts_fit_real_input_budget_without_losing_sources_or_changing_evidence():
    data = context()
    data["files"] = [{"source_id": "file:notes.md", "content": "Important facts. " * 3700}]
    data["research"]["rows"] += [
        {
            "source_id": f"keyword:k{i}",
            "data": {
                "keyword": "Buyer question " + str(i),
                "observations": [{"note": "Evidence. " * 40}],
            },
        }
        for i in range(2, 301)
    ]
    inventory = {
        "pages": [
            {
                "page_id": f"p{i:03d}",
                "status": "inspected",
                "url": f"https://example.com/{i}",
                "text": 'Quoted "text" 好\\ ' * 120,
            }
            for i in range(60)
        ],
        "omitted_candidates": 0,
    }
    before = deepcopy(inventory)
    result, aliases = editorial.model_context(data, inventory)
    assert len(canonical_json(result)) <= editorial.POLICY["max_input_bytes"]
    assert len(aliases) == 301 and len(result["pages"]["pages"]) == 60
    assert result["sources"][-1]["content"] == data["files"][0]["content"]
    assert 80 <= result["model_page_text_limit"] < 2200
    assert inventory == before
    data["files"][0]["content"] = "x" * 240_000
    with pytest.raises(ValueError, match="smaller context"):
        editorial.model_context(data, inventory)
