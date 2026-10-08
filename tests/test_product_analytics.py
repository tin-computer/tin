"""Reviewed analytics resources, synthetic data only; no provider/model calls in CI."""

import json
import math
import re
from pathlib import Path

import pytest
from connection_fakes import FakePostHogConnection
from test_procedure_publication import publication_db as publication_db

from tin_lite.posthog_connection import check_hogql

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/product.analytics_brief"
RESOURCES = PACKAGE / "skills/product-analytics"


def resource(name):
    path = RESOURCES / name
    blocks = re.findall(r"```python\n(.*?)\n```", path.read_text(), re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only fixed repository resources execute here, never uploaded author code.
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture
def statistics():
    return resource("STATISTICS.md")


def test_exact_test_matches_published_reference_and_is_symmetric(statistics):
    fisher = statistics["fisher_exact_two_sided"]
    assert fisher(6, 2, 1, 4) == pytest.approx(0.10256410256410256)
    assert fisher(1, 4, 6, 2) == pytest.approx(fisher(6, 2, 1, 4))
    assert fisher(2, 6, 4, 1) == pytest.approx(fisher(6, 2, 1, 4))
    assert fisher(0, 20, 0, 20) == pytest.approx(1)
    assert fisher(10, 0, 0, 10) == pytest.approx(2 / math.comb(20, 10))
    assert fisher(0, 0, 0, 0) is None
    assert fisher(0, 0, 4, 8) is None


@pytest.mark.parametrize("invalid", [-1, True, 1.5, float("nan"), "4"])
def test_stats_reject_invalid_counts(statistics, invalid):
    with pytest.raises(ValueError):
        statistics["fisher_exact_two_sided"](invalid, 2, 3, 4)


def test_screen_corrects_whole_family_and_rejects_small_samples(statistics):
    assert statistics["holm"]([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert statistics["holm"]([]) == []
    rows = statistics["screen_comparisons"]([(40, 10, 5, 45), (0, 0, 3, 7), (10, 0, 0, 10)])
    assert rows[0]["supported"] is True
    assert rows[0]["difference_pp"] == pytest.approx(70)
    # Unavailable and small-sample comparisons still belong to the correction family.
    assert rows[0]["adjusted_p"] == pytest.approx(3 * rows[0]["p"])
    assert rows[1]["p"] is None and rows[1]["supported"] is False
    assert rows[2]["p"] < 0.05 and rows[2]["supported"] is False
    assert statistics["fisher_exact_two_sided"](10001, 10001, 10001, 10001) is None
    assert statistics["fisher_exact_two_sided"](1, 1000000, 1, 1000000) is None


@pytest.mark.parametrize("p", [float("nan"), float("inf"), -0.1, 1.01, True])
def test_holm_refuses_invalid_values(statistics, p):
    with pytest.raises(ValueError):
        statistics["holm"]([p])


def analytics():
    namespace = resource("STATISTICS.md")
    path = RESOURCES / "CALCULATIONS.md"
    block = re.findall(r"```python\n(.*?)\n```", path.read_text(), re.S)[0]
    exec(compile(block, str(path), "exec"), namespace)  # noqa: S102
    return namespace


def fixture(name):
    import json

    return json.loads((ROOT / f"tests/fixtures/product_analytics/{name}.json").read_text())


def plan(name="accounts"):
    data = fixture(name)
    profile = data["profile"]
    return {
        "version": "reusable-v2",
        "actor_key": profile["actor"].replace("properties.", "event:"),
        "actor_label": profile["unit"],
        "chain_key": profile["attempt"].replace("properties.", "event:"),
        "chain_label": "sessions" if name == "website" else "attempts",
        "steps": profile["events"],
        "labels": profile["labels"],
        "key_events": [profile["events"][0], profile["events"][-1]],
        "error_events": ["job_failed"],
        "pageview_event": "$pageview" if name == "website" else "",
        "path_property": "$pathname" if name == "website" else "",
        "source_property": "$referring_domain" if name == "website" else "",
        "paths": ["/start"] if name == "website" else [],
        "category_property": "$device_type" if name == "website" else "",
        "categories": ["Desktop", "Mobile"] if name == "website" else [],
        "exclusions": [],
        "semantic_evidence": ["Synthetic documented event and identity semantics."],
        "traffic_actor_key": "distinct_id",
        "traffic_chain_key": "event:$session_id",
    }


def windows():
    return analytics()["settings"]({"as_of_utc": "2026-01-12"})[1]


@pytest.mark.parametrize("name", ["accounts", "website"])
def test_ordered_population_excludes_ties_cross_attempts_and_later_only(name):
    from datetime import datetime

    a, p, data = analytics(), plan(name), fixture(name)
    a["validate_plan"](p)

    def field(row, key):
        return row["distinct_id"] if key == "distinct_id" else row["properties"].get(key[6:])

    rows = [
        (
            "current",
            field(r, p["actor_key"]),
            field(r, p["chain_key"]),
            r["event"],
            datetime.fromisoformat(r["timestamp"]).timestamp(),
        )
        for r in data["events"]
    ]
    result = a["reference_funnel"](rows, p["steps"])
    assert result["current"]["counts"] == data["expected_ordered"]
    assert result["current"]["medians"] == [float(i) for i in range(1, len(p["steps"]))]


def test_inputs_freeze_complete_utc_windows_and_separate_semantics_binding():
    from datetime import UTC, datetime

    a = analytics()
    inputs = {}
    _, w, binding = a["settings"](inputs, datetime(2026, 1, 12, 11, tzinfo=UTC))
    assert [x.isoformat() for x in w] == [
        "2025-12-29T00:00:00+00:00",
        "2026-01-05T00:00:00+00:00",
        "2026-01-12T00:00:00+00:00",
    ]
    assert a["settings"]({**inputs, "as_of_utc": "2026-01-19"})[2] == binding
    assert a["settings"]({**inputs, "event_mapping": "Changed meaning"})[2] != binding
    # Structured exclusions are part of the binding, but leaving them empty changes nothing.
    assert a["settings"]({"exclude_email_domains": [], "internal_flag_property": ""})[2] == binding
    domains = a["settings"]({"exclude_email_domains": ["example.test"]})[2]
    flag = a["settings"]({"internal_flag_property": "person:is_internal"})[2]
    assert len({binding, domains, flag}) == 3
    for change in [
        # The PostHog project is the one selected in Integrations, never an input.
        {"posthog_project_id": "101"},
        {"website_hosts": ["example.com/../admin"]},
        {"reporting_days": True},
        {"reporting_days": 32},
        {"as_of_utc": "yesterday"},
        {"project_id": "cannot override"},
        {"exclude_email_domains": ["Example.test"]},
        {"exclude_email_domains": ["@example.test"]},
        {"exclude_email_domains": ["example.test", "example.test"]},
        {"exclude_email_domains": [f"d{i}.example.test" for i in range(11)]},
        {"exclude_email_domains": ["a" * 60 + ".test"]},
        {"exclude_email_domains": "example.test"},
        {"internal_flag_property": "is internal"},
        {"internal_flag_property": "distinct_id"},
        {"internal_flag_property": "person:"},
        {"internal_flag_property": True},
    ]:
        with pytest.raises(ValueError):
            a["settings"]({**inputs, **change})


STEPS = ["inventory", "coverage", "trends", "funnel", "dimensions", "traffic", "breakdown"]
HOSTS = [f"{name}.example.com" for name in ("www", "app", "docs", "blog", "shop")]


def largest_plan(window=False):
    """Every count at its maximum: six steps, eight labels per dimension, six prose exclusions
    plus ten 63-character email domains and a flag, five website hosts."""
    a = analytics()
    c = a["settings"](
        {
            "website_hosts": HOSTS,
            "exclude_email_domains": [f"{i}{'d' * 41}.example-company.test" for i in range(10)],
            "internal_flag_property": "person:is_internal_team_member_flag_" + "x" * 35,
        }
    )[0]
    assert all(len(d) == 63 for d in c["exclude_email_domains"])
    p = plan("website")
    p["steps"] = [f"event_number_{i}_long_name" for i in range(6)]
    p["labels"] = list(p["steps"])
    p["key_events"] = p["steps"][:4]
    p["error_events"] = ["job_failed_badly", "job_errored_again"]
    p["categories"] = [f"Category name {i}" for i in range(8)]
    p["paths"] = [f"/some/long/path/{i}" for i in range(8)]
    p["exclusions"] = a["input_exclusions"](c) + [
        {"property": f"person:email_{i}", "op": "eq", "value": "a@example.test"} for i in range(6)
    ]
    p["actor_key"], p["chain_key"] = (
        ("person_id", "window:168")
        if window
        else ("event:account_identifier", "event:attempt_identifier")
    )
    return p, c


@pytest.mark.parametrize("largest", [None, "session", "window", "split"])
def test_queries_are_single_bounded_selects_tins_hogql_guard_accepts(largest):
    a = analytics()
    p, c = largest_plan(largest != "session") if largest else (plan("website"), {})
    if largest == "split":
        # A pageview step with person/window identities while traffic keeps sessions.
        p["steps"][0] = "$pageview"
        p["labels"][0] = "Visited the site"
    for step in STEPS:
        request = a["request"](c, step, p, windows())
        assert set(request) == {"service", "step", "operation", "arguments"}
        assert request["operation"] == "query.hogql" and request["service"] == "analytics"
        query = request["arguments"]["query"]
        # No project id or host: Tin injects the project selected in Integrations.
        assert "/api/projects" not in json.dumps(request)
        assert len(query.encode()) <= 8000 and "UNION" not in query
        assert check_hogql(query) == query
    if largest == "split":
        columns = a["query_columns"]("coverage", p)[0]
        assert columns[-2:] == ["funnel_actors", "funnel_eligible_actors"]
    p = plan("website")
    p["steps"] = [f"step {i}" for i in range(6)]
    p["labels"] = list(p["steps"])
    query = a["request"]({}, "funnel", p, windows())["arguments"]["query"]
    assert "n6" in query and "median_d1_6" in query
    assert a["literal"]("I'm here") == "'I\\'m here'"
    for key in ["email;DELETE", "person:email", "event:bad.key", None, "window:24"]:
        with pytest.raises(ValueError):
            a["identity"](key)
    assert "person_id" in a["identity"]("person_id")
    assert a["window_hours"]("window:24") == 24 and a["window_hours"]("event:x") is None
    for key in ["window:0", "window:169", "window:1.5", "window:"]:
        with pytest.raises(ValueError):
            a["chain"](key)


def test_exclusions_preserve_types_null_inclusion_and_do_not_publish_values():
    a, p = analytics(), plan()
    rules = [
        {"property": "person:email", "op": "eq", "value": "team@example.test"},
        {"property": "is_test", "op": "eq", "value": True},
    ]
    assert a["exclusions"]([{"property": "tier", "op": "eq", "value": 1}]) != a["exclusions"](
        [{"property": "tier", "op": "eq", "value": "1"}]
    )
    assert "NOT coalesce" in a["exclusions"](rules)
    assert "person.properties" in a["exclusions"](rules)
    p["exclusions"] = rules
    pin = a["plan_state"](None, "binding", p, {})["pin"]
    public = a["public_pin"](pin)
    assert "team@example.test" not in str(public)
    assert a["restore_pin"](public, "binding", rules) == pin
    with pytest.raises(ValueError, match="binding"):
        a["restore_pin"](public, "another", rules)
    with pytest.raises(ValueError, match="exclusions"):
        a["restore_pin"](public, "binding", [])
    query = a["request"]({}, "inventory", p, windows())
    assert "team@example.test" not in a["safe_sql"](query["arguments"]["query"], rules)
    # A rule without an op is the old six-rule shape: it no longer compiles.
    with pytest.raises(ValueError):
        a["exclusions"]([{"property": "is_test", "value": True}])


def test_domain_and_flag_exclusions_are_compiled_from_saved_inputs():
    a, p = analytics(), plan()
    c = a["settings"](
        {
            "exclude_email_domains": ["example.test", "team-mail.test"],
            "internal_flag_property": "is_internal",
        }
    )[0]
    rules = a["input_exclusions"](c)
    assert rules == [
        {"property": "person:email", "op": "suffix", "value": ["example.test", "team-mail.test"]},
        {"property": "is_internal", "op": "truthy", "value": True},
    ]
    domain, flag = (a["exclusion_clause"](rule) for rule in rules)
    # Suffix: @domain or .domain, case-insensitive, on the current person email.
    assert "lower(toString(" in domain and "person.properties" in domain
    assert "'(?:^|[@.])(?:example\\\\.test|team\\\\-mail\\\\.test)$'" in domain
    # Truthy: boolean true, the string "true" in any case, or 1. Missing stays included.
    assert "lower(JSONExtractRaw(properties,'is_internal')) IN ('true','\"true\"','1')" in flag
    assert domain.startswith("NOT coalesce(") and flag.startswith("NOT coalesce(")
    # The builder refuses a plan that dropped the compiled rules.
    with pytest.raises(ValueError, match="input_exclusions"):
        a["request"](c, "inventory", {"exclusions": []}, windows())
    with pytest.raises(ValueError, match="input_exclusions"):
        a["request"](c, "coverage", p, windows())
    p["exclusions"] = rules + [{"property": "is_test", "op": "eq", "value": True}]
    query = a["request"](c, "coverage", p, windows())["arguments"]["query"]
    public = a["public_pin"](a["plan_state"](None, "binding", p, {})["pin"])
    safe = a["safe_sql"](query, p["exclusions"])
    for text in (json.dumps(public), safe):
        assert "example" not in text and "team-mail" not in text
    assert a["restore_pin"](public, "binding", p["exclusions"])["plan"] == p
    for bad in [
        {"property": "person:email", "op": "suffix", "value": []},
        {"property": "person:email", "op": "suffix", "value": "example.test"},
        {"property": "person:email", "op": "suffix", "value": ["EXAMPLE.test"]},
        {"property": "distinct_id", "op": "suffix", "value": ["example.test"]},
        {"property": "is_internal", "op": "truthy", "value": "true"},
        {"property": "distinct_id", "op": "truthy", "value": True},
        {"property": "email", "op": "contains", "value": "test"},
    ]:
        with pytest.raises(ValueError):
            a["exclusion_clause"](bad)
    with pytest.raises(ValueError):
        a["exclusions"]([{"property": f"p{i}", "op": "eq", "value": i} for i in range(9)])


def test_repeat_keeps_labels_and_saved_input_edits_explicitly_reconfigure():
    from copy import deepcopy

    a, p = analytics(), plan()
    first = a["plan_state"](None, "binding", p, {"event": "string"})
    assert first["state"] == "provisional"
    previous = deepcopy(first["pin"])
    assert a["plan_state"](previous, "binding", p, {"event": "string"})["state"] == "pinned"
    proposed = deepcopy(p)
    proposed["labels"][0] = "Different meaning"
    changed = a["plan_state"](previous, "binding", proposed, {"event": "string"})
    assert changed["state"] == "schema changed" and changed["pin"] == previous
    assert changed["changes"] == ["plan.labels"]
    assert a["plan_state"](previous, "new binding", proposed, {})["state"] == "reconfigured"


def result(**changes):
    """A query.hogql result as Tin's gateway returns it."""
    return {
        "columns": ["n"],
        "types": ["UInt64"],
        "rows": [[1]],
        "has_more": False,
        "truncated": False,
        **changes,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"has_more": True},
        {"truncated": True},
        {"has_more": None},
        {"columns": ["unexpected"]},
        {"rows": None},
        {"rows": [[1, 2]]},
        {"rows": [[1]] * 2},
    ],
)
def test_plausible_but_unusable_provider_result_is_rejected(bad):
    with pytest.raises(ValueError):
        analytics()["table"](result(**bad), ["n"], 2)
    # PostHog's own response shape is not what the gateway returns; it is never read directly.
    with pytest.raises(ValueError):
        analytics()["table"]({"columns": ["n"], "results": [[1]], "hasMore": False}, ["n"], 2)


def test_complete_results_are_usable_and_zero_is_not_failure():
    a = analytics()
    assert a["table"](result(rows=[]), ["n"], 2) == []
    assert a["ratio"](0, 0) is None
    assert a["change"](5, 0) == {"current": 5, "prior": 0, "delta": 5, "relative_pct": None}
    totals, rows = a["reconcile_funnel"]([], plan(), windows(), {})
    assert totals == {"prior": [0, 0, 0], "current": [0, 0, 0]}
    assert len(rows) == 14 and all(r["median_d1_3"] is None for r in rows)


def test_funnel_checks_reject_monotone_but_wrong_counts():
    a, p = analytics(), plan()
    row = {
        "period": "current",
        "day": "2026-01-05",
        "n1": 5,
        "n2": 3,
        "n3": 2,
        "median_d1_2": 1,
        "median_d1_3": 2,
        "median_d2_3": 1,
        "order_violations": 0,
        "duplicate_choices": 0,
    }
    cov = {
        ("current", event): {"eligible_actors": n}
        for event, n in zip(p["steps"], [6, 7, 2], strict=True)
    }
    with pytest.raises(ValueError, match="coverage mismatch"):
        a["reconcile_funnel"]([row], p, windows(), cov)
    for changes in [
        {"n2": 6},
        {"median_d1_3": float("nan")},
        {"order_violations": 1},
        {"duplicate_choices": 1},
        {"day": "2026-01-12"},
        {"n1": True},
    ]:
        with pytest.raises(ValueError):
            a["validate_funnel"]([{**row, **changes}], p, windows())


def test_missing_breakdown_categories_stay_in_declared_test_family():
    a, p = analytics(), plan("website")
    result = a["screen"]([{"category": "Desktop", "total": 40, "converted": 20}], p)
    assert result["family_size"] == 2
    assert result["results"][1]["category"] == "Mobile"
    assert result["results"][1]["p"] is None
    assert not any(x["supported"] for x in result["results"])


def test_website_keys_are_independent_from_account_funnel():
    a, p = analytics(), plan()
    p.update(
        {
            k: plan("website")[k]
            for k in ["pageview_event", "path_property", "source_property", "paths"]
        }
    )
    a["validate_plan"](p)
    sql = a["request"]({}, "traffic", p, windows())["arguments"]["query"]
    assert "account_id" not in sql and "job_id" not in sql
    assert "$session_id" in sql and "distinct_id" in sql
    assert "funnel_actors" not in a["request"]({}, "coverage", p, windows())["arguments"]["query"]
    # A pageview step with another identity is counted under both: sessions for traffic and
    # the activation identity in funnel_* columns, which reconcile_funnel then reads.
    p["steps"][0] = "$pageview"
    a["validate_plan"](p)
    sql = a["request"]({}, "coverage", p, windows())["arguments"]["query"]
    assert "AS funnel_actors" in sql and "AS funnel_eligible_actors" in sql
    assert "account_id" in sql and "$session_id" in sql
    columns = a["query_columns"]("coverage", p)[0]
    assert columns[-2:] == ["funnel_actors", "funnel_eligible_actors"]
    # Traffic keys stay identities: a window is never a session.
    with pytest.raises(ValueError):
        a["validate_plan"]({**p, "traffic_chain_key": "window:24"})


async def test_registered_package_publishes_pinned_resources_and_run_owned_reports():
    from uuid import uuid4

    from test_public_workflows import PublishedSnapshots
    from test_registry_recipe_publication import WIKI, catalog_database

    from tin_lite import catalog, public_workflows
    from tin_lite.procedures import load_pinned_codex_procedure

    entry = next(w for w in public_workflows.PUBLIC_WORKFLOWS if w.key == "product.analytics_brief")
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    assert row.definition["schedule_modes"] == ["on_demand", "daily", "weekly"]
    procedure = await load_pinned_codex_procedure(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=row.current_commit_sha,
        definition_path=row.definition_path,
    )
    run_id = uuid4()
    pinned = procedure.resolve_inputs(inputs={}, run_id=run_id)
    assert pinned.output_path == f"reports/analytics/{run_id}.md"
    assert pinned.resolve_inputs(inputs={}, run_id=run_id).output_path == pinned.output_path
    assert procedure.resolve_inputs(inputs={}, run_id=uuid4()).output_path != pinned.output_path
    assert procedure.services[0].provider_key == "analytics.posthog"
    assert set(procedure.services[0].capabilities) == {"query.read", "definitions.read"}
    assert procedure.services[0].max_calls == 8
    assert procedure.sandbox.egress == "fenced"
    # Tin reads the report before publishing: a brief that measured nothing fails its run.
    assert procedure.output_validator == "analytics-brief.v1"
    assert row.definition["version"] == "2.1.0"
    assert (
        procedure.skill_files["product-analytics/CALCULATIONS.md"]
        == (RESOURCES / "CALCULATIONS.md").read_bytes()
    )
    first = row.current_commit_sha
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert db.rows[entry.id].current_commit_sha == first


@pytest.mark.parametrize(
    "path,media_type",
    [
        ("content/drafts/{run_id}.md", "text/markdown"),
        ("reports/../wiki/{run_id}.md", "text/markdown"),
        ("reports/analytics/{run_id}.json", "application/json"),
        ("reports/analytics/{slug}.md", "text/markdown"),
        ("reports/analytics/{run_id}.md", "application/json"),
        # The analytics-brief.v1 check reads only the analytics report path.
        ("reports/insights/{run_id}.md", "text/markdown"),
    ],
)
def test_generic_report_paths_do_not_bypass_existing_output_contracts(path, media_type):
    import json

    from tin_lite.procedures import validate_codex_procedure_definition

    definition = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    definition["procedure"]["output"].update(path_template=path, media_type=media_type)
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(definition)


async def test_package_uses_shared_qualification_and_diagnostic_fails_normal_case():
    from tin_lite.workflow_qualification import Qualification, assess_output, check_package

    contract = Qualification.model_validate_json(
        (ROOT / "workflow_evals/product.analytics_brief/qualification.json").read_bytes()
    )
    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in PACKAGE.rglob("*") if p.is_file()}
    checked = await check_package(
        files, "workflow_packages/product.analytics_brief/workflow.json", contract
    )
    assert checked["cost"]["basis"] == "unmeasured"
    assert checked["cost"]["expected_range_usd"] is None
    # Its own $3 session ceiling (2026-10-08 calibration), by workflow key.
    assert checked["cost"]["configured_ceiling_usd"] == "3"
    assert checked["safety"]["status"] == "review_required"
    # API below is deliberately asserted through its public existing contract.
    case = next(c for c in contract.cases if c.id == "ordinary_web")
    text = "\n".join(case.expect.contains).replace("Status: complete", "Status: incomplete")
    result = assess_output(case, status="succeeded", content=text.encode())
    assert result["status"] == "failed"


async def replay(recorded, p, inputs=None):
    """Send each generated request through Tin's offline PostHog binding, which applies the
    HogQL guard and projects the recorded PostHog response exactly as the gateway does."""
    import hashlib

    a = analytics()
    c = a["settings"](inputs)[0] if inputs else {}
    queries = {f"analytics brief {step}": saved["data"] for step, saved in recorded.items()}
    posthog = FakePostHogConnection({}, service="analytics", queries=queries)
    rows = {}
    for step, saved in recorded.items():
        request = a["request"](c, step, p, windows())
        query = request["arguments"]["query"]
        assert hashlib.sha256(query.encode()).hexdigest() == saved["query_sha256"]
        data = await posthog.call(
            service=request["service"],
            step=request["step"],
            operation=request["operation"],
            arguments=request["arguments"],
        )
        rows[step] = a["table"](data, *a["query_columns"](step, p))
    return rows


@pytest.mark.parametrize("name", ["accounts", "website"])
async def test_provider_fixtures_reconcile_exact_generated_queries(name):
    a, p = analytics(), plan(name)
    rows = await replay(fixture("provider_results")["cases"][name], p)
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    a["reconcile_coverage"](cov, rows["inventory"], p, windows())
    totals, days = a["reconcile_funnel"](rows["funnel"], p, windows(), cov)
    assert totals["current"] == fixture(name)["expected_ordered"]
    display = a["funnel_display"](days, p)
    day = [r for r in display if r["selected_start_day"] == "2026-01-05"]
    assert day[1]["of_previous_pct"] == 50
    assert day[2]["of_first_pct"] == pytest.approx(100 / 3)
    assert day[2]["median_from_first_seconds"] == 2
    _, comparisons = a["validate_trends"](rows["trends"], p, windows(), cov)
    assert comparisons[p["key_events"][0]]["current"] == 7  # raw events, not six actors
    if name == "website":
        assert a["validate_traffic"](rows["traffic"], p, cov)["current"] == [6, 7]
        assert a["validate_breakdown"](rows["breakdown"], p, totals["current"]) == (6, 2)
        # search.example is not a known engine: it is a named Referral domain.
        summary = a["traffic_summary"](rows["traffic"])["current"]
        assert summary["channels"] == {"Referral": [6, 7]}
        assert summary["referrers"] == {"search.example": [6, 7]}
        assert a["validate_dimensions"](rows["dimensions"], p) == {
            "categories": ["Desktop"],
            "paths": ["/start"],
        }


def discovery_rows(total=205):
    return [
        {
            "event": f"unrelated_{i}",
            "observed": 10,
            "prior": 0,
            "current": 10,
            "first_seen": "2026-01-05T00:00:00Z",
            "last_seen": "2026-01-05T00:00:01Z",
            "total_event_types": total,
        }
        for i in range(min(total, 200))
    ]


def test_large_catalog_does_not_turn_undiscovered_events_into_zero_counts():
    from tin_lite.posthog_connection import project_query

    a, p = analytics(), plan("accounts")
    inventory = discovery_rows()
    a["validate_inventory"](inventory, windows())
    assert a["inventory_scope"](inventory) == {
        "returned_event_types": 200,
        "total_event_types": 205,
        "complete": False,
    }
    data = project_query(
        fixture("provider_results")["cases"]["accounts"]["coverage"]["data"],
        max_response_bytes=64_000,
    )
    rows = a["table"](data, *a["query_columns"]("coverage", p))
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows, a["event_list"](p), windows(), keys)
    assert a["reconcile_coverage"](cov, inventory, p, windows()) == cov
    # A reported selected-event count still has to match the independent coverage query.
    inventory[0]["event"] = p["steps"][0]
    with pytest.raises(ValueError, match="inventory/coverage mismatch"):
        a["reconcile_coverage"](cov, inventory, p, windows())
    # Complete discovery can establish absence, so the same nonzero coverage is inconsistent.
    with pytest.raises(ValueError, match="inventory/coverage mismatch"):
        a["reconcile_coverage"](cov, discovery_rows(200), p, windows())


@pytest.mark.parametrize(
    "change", ["missing_row", "inconsistent_total", "boolean_total", "extra_row"]
)
def test_discovery_rejects_unexpected_truncation_and_invalid_catalog_counts(change):
    a, rows = analytics(), discovery_rows()
    if change == "missing_row":
        rows.pop()
    elif change == "inconsistent_total":
        rows[0]["total_event_types"] += 1
    elif change == "boolean_total":
        rows[0]["total_event_types"] = True
    else:
        rows.append({**rows[-1], "event": "extra"})
    with pytest.raises(ValueError):
        a["validate_inventory"](rows, windows())


def test_empty_discovery_is_complete_and_small_discovery_stays_exact():
    a = analytics()
    assert a["inventory_scope"]([]) == {
        "returned_event_types": 0,
        "total_event_types": 0,
        "complete": True,
    }
    rows = discovery_rows(3)
    a["validate_inventory"](rows, windows())
    assert a["inventory_scope"](rows)["complete"] is True
    rows.pop()
    with pytest.raises(ValueError):
        a["validate_inventory"](rows, windows())


@pytest.mark.parametrize("name", ["six", "empty", "exclusions", "mixed"])
async def test_provider_boundary_fixtures(name):
    a = analytics()
    case = fixture("provider_edges")["cases"][name]
    p = case["plan"]
    rows = await replay(case["responses"], p)
    if name in {"six", "empty"}:
        totals = a["validate_funnel"](rows["funnel"], p, windows())
        assert totals["current"] == ([1] * 6 if name == "six" else [0] * 3)
        return
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    if name == "exclusions":
        a["reconcile_coverage"](cov, rows["inventory"], p, windows())
        row = cov[("current", p["steps"][0])]
        # Only boolean true excluded: false, string "true", null and integer 1 remain.
        assert row["raw"] == 4 and row["missing_p0"] == 1
    else:
        assert cov[("current", p["steps"][0])]["eligible_actors"] == 6
        assert a["validate_traffic"](rows["traffic"], p, cov)["current"] == [6, 7]


@pytest.mark.parametrize(
    "value",
    [
        "customer@example.test",
        "/users/123456",
        "Other",
        "https://private.test/",
        "01234567-abcd",
        None,
    ],
)
def test_configured_dimensions_have_the_same_safe_labels_as_discovery(value):
    a, p = analytics(), plan("website")
    p["categories"] = [value]
    with pytest.raises(ValueError):
        a["validate_plan"](p)
    with pytest.raises(ValueError):
        a["validate_dimensions"](
            [{"kind": "categories", "value": value, "volume": 2}], plan("website")
        )


def test_host_scope_is_bound_and_applies_to_inventory_and_traffic():
    a = analytics()
    c, w, binding = a["settings"](
        {"as_of_utc": "2026-09-22", "website_hosts": ["example.com", "www.example.com"]}
    )
    _, _, unscoped = a["settings"]({"as_of_utc": "2026-09-22"})
    assert binding != unscoped
    request = a["request"](c, "inventory", {"exclusions": []}, w)
    sql = request["arguments"]["query"]
    assert "event!='$pageview' OR" in sql and "'www.example.com'" in sql
    assert "$host" in sql
    for bad in [["example.com/path"], ["example.com", "example.com"], ["x'); DROP TABLE events"]]:
        with pytest.raises(ValueError):
            a["settings"]({"website_hosts": bad})


def website_properties():
    """Property definitions for the website fixture's events, as PostHog lists them."""
    events = ["$pageview", "signup_started", "signup_completed", "first_export"]
    rows = [
        ("$session_id", "String", events),
        ("$pathname", "String", ["$pageview"]),
        ("$referring_domain", "String", ["$pageview"]),
        ("$device_type", "String", events),
        ("plan_seats", "Numeric", ["signup_completed"]),
    ]
    return {
        "property_definitions": [
            {
                "id": f"p{i}",
                "name": name,
                "property_type": kind,
                "is_numerical": kind == "Numeric",
                "_fixture_events": used,
            }
            for i, (name, kind, used) in enumerate(rows)
        ]
    }


async def test_property_definitions_check_declared_types_through_the_binding():
    a, p = analytics(), plan("website")
    request = a["properties_request"](a["event_list"](p))
    assert request["operation"] == "property_definitions.list"
    posthog = FakePostHogConnection(website_properties(), service="analytics")
    page = await posthog.call(**request)
    found = a["property_types"](page)
    assert found["complete"] and found["types"]["$pathname"] == "String"
    checked = a["check_property_types"](found, p)
    assert checked == {"conflicts": [], "unverified": [], "complete": True}
    p["category_property"] = "plan_seats"
    p["categories"] = []
    checked = a["check_property_types"](found, p)
    assert checked["conflicts"] == [
        {"property": "plan_seats", "type": "Numeric", "expected": "String"}
    ]
    # A property missing from the listing is unverified, never proof it is absent.
    p["chain_key"] = "event:job_id"
    assert "job_id" in a["check_property_types"](found, p)["unverified"]
    for events in [[], ["x"] * 2, [f"e{i}" for i in range(21)], [None]]:
        with pytest.raises(ValueError):
            a["properties_request"](events)
    for bad in [{"records": None}, {**page, "has_more": None}, {**page, "records": [{"x": 1}]}]:
        with pytest.raises(ValueError):
            a["property_types"](bad)


async def test_domain_and_string_flag_exclusions_through_the_provider():
    a = analytics()
    case = fixture("provider_edges")["cases"]["input_exclusions"]
    p = case["plan"]
    c = a["settings"](case["inputs"])[0]
    assert p["exclusions"] == a["input_exclusions"](c)
    rows = await replay(case["responses"], p, case["inputs"])
    _, keys = a["coverage"](p, windows())
    assert keys == ["is_internal", "person:email"]
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    a["reconcile_coverage"](cov, rows["inventory"], p, windows())
    row = cov[("current", p["steps"][0])]
    # Of 14 rows: the domain rule drops @example.test, a subdomain and an upper-case address;
    # the flag drops true, "true", "TRUE" and 1. False, "false", 0, "1", a look-alike domain,
    # a lookalike suffix and a missing email stay, and the missing values are counted.
    assert row["raw"] == 7
    assert (row["missing_p0"], row["missing_p1"]) == (3, 1)


def traffic_case(name):
    a = analytics()
    case = fixture("provider_edges")["cases"][name]
    return a, case, case["plan"]


async def test_traffic_channels_are_classified_by_the_builder():
    a, case, p = traffic_case("channels")
    rows = await replay(case["responses"], p, case["inputs"])
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    totals = a["validate_traffic"](rows["traffic"], p, cov)
    # Every session with both keys is counted once; the out-of-scope host is not.
    assert totals == {"prior": [1, 1], "current": [24, 26]}
    summary = a["traffic_summary"](rows["traffic"])
    assert {k: summary["current"][k] for k in ("channels", "referrers")} == case["expected_current"]
    assert summary["prior"]["channels"] == {"Direct": [1, 1]}
    assert summary["current"]["paths"] == {"/start": [22, 24], "Other": [1, 1], "Unknown": [1, 1]}
    # Rows that break the channel contract are refused.
    good = rows["traffic"]
    referral = next(r for r in good if r["source"] == "github.com")
    direct = next(r for r in good if r["channel"] == "Direct")
    for bad in [
        [*good, {**direct, "source": "github.com"}],
        [*good, {**referral, "channel": "Organic"}],
        [*good, {**referral, "source": "$direct"}],
        [*good, {**referral, "source": "Unknown"}],
        [r for r in good if r is not referral],
    ]:
        with pytest.raises(ValueError):
            a["validate_traffic"](bad, p, cov)
    named = [
        {**referral, "source": f"site{i}.test", "sessions": 0, "pageviews": 0} for i in range(9)
    ]
    with pytest.raises(ValueError, match="named"):
        a["validate_traffic"]([r for r in good if r["channel"] != "Referral"] + named, p, cov)


def test_channel_rules_lowercase_strip_www_and_use_website_hosts():
    a = analytics()
    p = plan("website")
    c = a["settings"]({"website_hosts": ["www.example.com", "docs.example.com"]})[0]
    sql = a["request"](c, "traffic", p, windows())["arguments"]["query"]
    assert "dom IN ('docs.example.com','example.com')" in sql and "dom=own" in sql
    assert "replaceRegexpOne(lower(toString(" in sql and "'^www\\\\.'" in sql
    assert "'AI assistants'" in sql and "chatgpt\\\\.com" in sql and "dom IN ('','$direct')" in sql
    order = [sql.index(f"'{name}'") for name in ("Paid", "Email", "Direct", "Internal")]
    assert order == sorted(order)
    assert sql.index("'AI assistants'") < sql.index("'Search'") < sql.rindex("'Social'")
    assert "search.example" not in sql  # referrers are named in-query, never pinned


def funnel_rows(events, p, window_seconds=None):
    """Oracle rows for reference_funnel: the plan's identities, missing keys as None."""
    from datetime import datetime

    a = analytics()
    _, boundary, _ = windows()

    def key(row, name):
        if name == "person_id":
            value = row.get("person_id")
            return None if value in (None, "", a["NO_PERSON"]) else value
        if name == "distinct_id":
            return row["distinct_id"]
        return row["properties"].get(name[6:])

    rows = []
    for r in events:
        t = datetime.fromisoformat(r["timestamp"])
        rows.append(
            (
                "prior" if t < boundary else "current",
                key(r, p["actor_key"]),
                None if window_seconds else key(r, p["chain_key"]),
                r["event"],
                t.timestamp(),
            )
        )
    return rows


async def test_server_signup_joins_anonymous_pageviews_by_person_within_the_window():
    a, case, p = traffic_case("identified")
    assert (p["actor_key"], p["chain_key"]) == ("person_id", "window:24")
    events = case["events"]
    # Independent oracle: a later step counts within 24 hours of any step-1 event.
    oracle = a["reference_funnel"](funnel_rows(events, p, 86400), p["steps"], 86400)
    assert {k: v["counts"] for k, v in oracle.items()} == case["expected_ordered"]
    assert oracle["current"]["medians"] == [2700.0]
    rows = await replay(case["responses"], p)
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    pageviews = cov[("current", "$pageview")]
    # Traffic keeps browser sessions; the funnel counts people (one pageview has no person).
    assert (pageviews["chains"], pageviews["funnel_eligible_actors"]) == (7, 5)
    totals, days = a["reconcile_funnel"](rows["funnel"], p, windows(), cov)
    assert totals == case["expected_ordered"]
    joined = [r for r in days if r["n2"]]
    assert [(r["day"], r["median_d1_2"]) for r in joined] == [
        ("2026-01-01", 3600),
        ("2026-01-05", 1800),
        ("2026-01-08", 3600),
    ]
    assert a["validate_traffic"](rows["traffic"], p, cov)["current"] == [7, 8]


async def test_session_keys_cannot_join_a_server_signup_and_trip_the_guard():
    a, case, p = traffic_case("identified_sessions")
    assert (p["actor_key"], p["chain_key"]) == ("distinct_id", "event:$session_id")
    oracle = a["reference_funnel"](funnel_rows(case["events"], p), p["steps"])
    assert {k: v["counts"] for k, v in oracle.items()} == case["expected_ordered"]
    rows = await replay(case["responses"], p)
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    signups = cov[("current", "signup_completed")]
    assert (signups["actors"], signups["eligible_actors"]) == (5, 0)
    # The rows themselves are consistent, so only the new guard stops a 0% conversion.
    assert a["validate_funnel"](rows["funnel"], p, windows())["current"] == [6, 0]
    with pytest.raises(ValueError, match="unjoinable identity"):
        a["reconcile_funnel"](rows["funnel"], p, windows(), cov)


def test_reusable_v1_pins_fail_and_disclose_continuity():
    from copy import deepcopy

    a = analytics()
    old = deepcopy(plan("website"))
    old.update(version="reusable-v1", sources=["search.example"])
    old["exclusions"] = [{"property": "is_test", "value": True}]
    with pytest.raises(ValueError, match="unsupported plan shape/version"):
        a["validate_plan"](old)
    pin = {"binding": "binding", "version": "reusable-v1", "plan": old, "schema_signature": {}}
    with pytest.raises(ValueError):
        a["plan_state"](pin, "binding", plan("website"), {})
    with pytest.raises(ValueError):
        a["restore_pin"](a["public_pin"](pin), "binding", old["exclusions"])


REPORT_HEAD = """# Product analytics brief

Status: {status}
Generated: 2026-01-12T09:00:00Z

## Activation funnel
"""


def report(status="complete", reason="", *, requests=None, scope=None, coverage=None, **extra):
    a = analytics()
    requests = [ok("inventory"), ok("coverage")] if requests is None else requests
    fields = {
        "binding": "binding",
        "generated_at": "2026-01-12T09:00:00Z",
        "windows": None,
        "state": None,
        "inventory_scope": {"returned_event_types": 4, "total_event_types": 4, "complete": True}
        if scope is None
        else scope,
        "requests": requests,
        "derived": {"coverage": [{"raw": 7}] if coverage is None else coverage},
        "limitations": [],
        **extra,
    }
    return REPORT_HEAD.format(status=status) + "\n" + a["render_evidence"](status, reason, fields)


def refused(step, message="Tin refused the call"):
    q = {"step": step, "operation": "query.hogql", "arguments": {"query": "SELECT 1"}}
    return analytics()["request_record"](q, [], "refused", message)


def ok(step):
    q = {"step": step, "operation": "query.hogql", "arguments": {"query": "SELECT 1"}}
    return analytics()["request_record"](q, [], "ok")


def test_analytics_brief_validator_reads_status_and_what_was_measured():
    from tin_lite import analytics_brief
    from tin_lite.procedures import PinnedCodexProcedure, validate_procedure_artifact

    # The package's renderer and Tin's check share one contract.
    assert set(analytics()["STATUSES"]) == analytics_brief.STATUSES
    assert analytics()["EVIDENCE_MARKER"] == analytics_brief.MARKER
    complete = analytics_brief.read(report())
    assert (complete.status, complete.reason, complete.measured_nothing) == ("complete", "", None)
    assert analytics_brief.summary(complete) is None
    incomplete = analytics_brief.read(
        report(
            "incomplete",
            "The funnel query was refused by PostHog's hourly query budget.",
            requests=[ok("inventory"), ok("coverage"), refused("funnel")],
        )
    )
    assert incomplete.measured_nothing is None
    assert analytics_brief.summary(incomplete) == (
        "Analytics brief incomplete: The funnel query was refused by PostHog's hourly query budget."
    )
    zero = analytics_brief.read(
        report(
            "incomplete",
            "PostHog has no events yet.",
            scope={"returned_event_types": 0, "total_event_types": 0, "complete": True},
        )
    )
    assert zero.measured_nothing == "PostHog returned no events in the 90-day lookback"
    all_refused = analytics_brief.read(
        report(
            "incomplete",
            "Every query was refused.",
            requests=[refused(s) for s in ("inventory", "coverage", "trends")],
            scope=None,
        )
    )
    assert all_refused.measured_nothing.startswith("every analytics query was refused")
    none_selected = analytics_brief.read(report("incomplete", "No selected events.", coverage=[]))
    assert none_selected.measured_nothing.startswith("none of the selected events")
    # A saved-input diagnostic makes no queries: incomplete, but not a failed run.
    invalid = analytics_brief.read(
        report(
            "invalid configuration", "website_hosts has an invalid host.", requests=[], scope=None
        )
    )
    assert invalid.measured_nothing is None
    assert analytics_brief.summary(invalid) == (
        "Analytics brief incomplete (invalid configuration): website_hosts has an invalid host."
    )
    # A report cannot claim completion without one successful query.
    assert analytics_brief.read(report(requests=[], scope=None)).measured_nothing
    spec = PinnedCodexProcedure(
        workflow_key="product.analytics_brief",
        prompt="",
        entry_skill="product-analytics",
        skill_files={},
        output_path="reports/analytics/run.md",
        output_media_type="text/markdown",
        output_validator="analytics-brief.v1",
        output_max_bytes=192000,
    )
    validate_procedure_artifact(report().encode(), spec=spec)
    good = report()
    marker = analytics()["EVIDENCE_MARKER"]
    for bad in [
        good.replace("Status: complete", "Status: done"),
        good.replace("Status: complete", "Status: complete\nStatus: complete"),
        good.split(marker)[0],
        good + marker,
        good.replace('"status":"complete"', '"status":"incomplete"'),
        good.replace('"status_reason":""', '"status_reason":"extra"'),
        good.replace('"requests":', '"calls":'),
        good.replace('"outcome":"ok"', '"outcome":"maybe"', 1),
        good.replace('{"raw":7}', '{"raw":"7"}'),
        good.replace("```json", "```"),
        good.replace('"binding":"binding"', '"binding":'),
    ]:
        with pytest.raises(ValueError):
            analytics_brief.read(bad)
        with pytest.raises(ValueError):
            validate_procedure_artifact(bad.encode(), spec=spec)
    with pytest.raises(ValueError):
        report("complete", "no reason for a complete brief")
    with pytest.raises(ValueError):
        report("incomplete", "")
    with pytest.raises(ValueError):
        report(extra_field=1)


async def analytics_activity(database, content):
    """A persisted analytics-brief run at the publication step, on real Postgres."""
    from dataclasses import replace
    from types import SimpleNamespace

    from test_procedure_publication import HistoryStorage, run_fixture, saved_checkpoint

    from tin_lite.activities import TinActivities
    from tin_lite.procedures import PinnedCodexProcedure

    storage = HistoryStorage()
    run = run_fixture()
    project = await database.create_project(name="Analytics proof", state_repo_id=storage.repo.id)
    run = replace(run, project_id=project.id)
    path = f"reports/analytics/{run.id}.md"
    await database.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'product.analytics_brief', 'Product analytics brief', 'codex.procedure',
                   'registry/workflows', 'product.analytics_brief.json', $2, '2.1.0', '{}')""",
        run.workflow_id,
        run.definition_commit_sha,
    )
    await database.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            lease_owner, sandbox_id, expected_head_sha, ephemeral_branch)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
        run.id,
        run.project_id,
        run.workflow_id,
        run.executor,
        run.definition_commit_sha,
        run.temporal_workflow_id,
        run.thread_id,
        run.generation,
        run.fencing_token,
        run.status.value,
        run.lease_active,
        run.lease_owner,
        run.sandbox_id,
        run.expected_head_sha,
        run.ephemeral_branch,
    )
    checkpoint = saved_checkpoint(storage, run, path=path, content=content)
    spec = PinnedCodexProcedure(
        workflow_key="product.analytics_brief",
        prompt="",
        entry_skill="product-analytics",
        skill_files={},
        output_path=path,
        output_media_type="text/markdown",
        output_validator="analytics-brief.v1",
        output_max_bytes=192000,
    )

    class Activities(TinActivities):
        async def _pinned_codex_procedure(self, requested_id):
            assert requested_id == run.id
            workflow = SimpleNamespace(
                key="product.analytics_brief", title="Product analytics brief", project_id=None
            )
            return workflow, spec

    class Sandboxes:
        async def kill(self, sandbox_id):
            pass

    activities = Activities(
        database=database, storage=storage, sandboxes=Sandboxes(), settings=SimpleNamespace()
    )
    key = f"{run.id}:procedure_artifact_persist"
    async with database.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await database.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await database.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={
                "checkpoint": checkpoint.to_dict(),
                "summary": "Product analytics brief produced its result.",
                "sandbox_killed": True,
            },
            sandbox_killed=True,
        )
    return activities, storage, run


async def test_brief_that_measured_nothing_fails_its_run_and_keeps_the_diagnostic(
    publication_db,
):
    from temporalio.exceptions import ApplicationError

    from tin_lite.domain import RunStatus
    from tin_lite.publication import read_run_output

    content = report(
        "incomplete",
        "PostHog has no events yet.",
        scope={"returned_event_types": 0, "total_event_types": 0, "complete": True},
    ).encode()
    activities, storage, run = await analytics_activity(publication_db, content)
    with pytest.raises(ApplicationError) as failure:
        await activities.commit_codex_procedure_artifact(str(run.id))
    assert failure.value.non_retryable and failure.value.type == "AnalyticsBriefMeasuredNothing"
    await activities.project_codex_procedure_failure(
        {"run_id": str(run.id), "reason": "workflow failed"}
    )
    failed = await publication_db.get_run(run.id)
    assert failed.status == RunStatus.FAILED and failed.canonical_commit_sha is None
    assert failed.error_message == (
        "The analytics brief measured nothing: PostHog returned no events in the 90-day "
        "lookback. Its diagnostic is saved with this run and was not added to Files."
    )
    assert failed.retained_output["reason"] == "not_published"
    assert storage.repo.writes == 0 and not failed.lease_active
    saved = await read_run_output(
        storage=storage, run=failed, repo_id=storage.repo.id, source="retained"
    )
    assert saved.content == content


async def test_incomplete_brief_publishes_with_a_summary_that_says_why(publication_db):
    from tin_lite.domain import RunStatus

    content = report(
        "incomplete",
        "The funnel query was refused by PostHog's hourly query budget.",
        requests=[ok("inventory"), ok("coverage"), refused("funnel")],
    ).encode()
    activities, storage, run = await analytics_activity(publication_db, content)
    await activities.commit_codex_procedure_artifact(str(run.id))
    await activities.project_codex_procedure_result(str(run.id))
    done = await publication_db.get_run(run.id)
    assert done.status == RunStatus.SUCCEEDED and storage.repo.writes == 1
    assert done.progress_summary == (
        "Analytics brief incomplete: The funnel query was refused by PostHog's hourly query budget."
    )
    assert done.retained_output is None


def test_an_interrupted_brief_is_still_retained_as_partial_text():
    from tin_lite import interrupted_procedure
    from tin_lite.procedures import (
        PinnedCodexProcedure,
        SandboxProfile,
        validate_procedure_artifact,
    )

    spec = PinnedCodexProcedure(
        workflow_key="product.analytics_brief",
        prompt="",
        entry_skill="product-analytics",
        skill_files={},
        output_path="reports/analytics/run.md",
        output_media_type="text/markdown",
        output_validator="analytics-brief.v1",
        output_max_bytes=192000,
        sandbox=SandboxProfile(profile="isolated"),
    )
    partial = b"# Product analytics brief\n\nStatus: incomplete\n\n## Activation funnel\n"
    assert interrupted_procedure.eligible(spec)
    with pytest.raises(ValueError):
        validate_procedure_artifact(partial, spec=spec)
    validate_procedure_artifact(partial, spec=interrupted_procedure._partial(spec))
