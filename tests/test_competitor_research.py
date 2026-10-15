"""content.plan 1.3.0 (content-editorial-v11): competitor research the plan can stand on.

Offline: the planning agent's portfolio is a fixture, including a plausible but unusable
competitor set (no labels, a drift rival given a comparison page, an evidence link that is not
a page). No model, sandbox or provider is called.
"""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

from test_content_plan_agent import context, document, opportunity, portfolio
from test_content_plan_editorial import pages

from tin_lite import content_plan_agent as agent
from tin_lite import content_plan_editorial as editorial
from tin_lite import content_plan_targets as targets

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "codex_procedures/content.plan_research/skills/content-portfolio/check_portfolio.py"


def research_context(**kwargs):
    data = context(**kwargs)
    data["competitor_research"] = {
        "file": agent.COMPETITORS_PATH,
        "channels": list(agent.CHANNELS),
        "fresh_days": agent.COMPETITORS_FRESH_DAYS,
    }
    return data


def researched(**fields):
    value = portfolio(3, **fields)
    value["competitors"] = [
        {
            "name": "Sendblue",
            "host": "https://www.sendblue.com/pricing",
            "label": "direct",
            "does": "Managed iMessage API for developers.",
            "buyer": "Developers building messaging agents",
            "evidence_url": "https://sendblue.com",
            "channels": ["roundups", "launches", "unknown"],
            "use": "comparison",
            "why": "Every iMessage API roundup lists it.",
        },
        {
            "name": "BlueBubbles",
            "host": "bluebubbles.app",
            "label": "diy",
            "evidence_url": "https://bluebubbles.app",
            "use": "comparison",
        },
        {"name": "Zapier", "host": "zapier.com", "label": "drift", "use": "none"},
        # Plausible but unusable: no label, and an evidence link that is not a page.
        {"name": "Somebody", "host": "somebody.io", "use": "alternative"},
        {"name": "Linq", "label": "adjacent", "evidence_url": "javascript:alert(1)"},
    ]
    value["competitor_research"] = {
        "started_from": "none",
        "channels": [
            {"channel": "roundups", "queries": ["best imessage api"], "found": ["Sendblue"]},
            {"channel": "launches", "queries": ["show hn imessage"], "found": ["Chert"]},
            {"channel": "nonsense", "queries": ["x"]},
        ],
    }
    return value


def test_site_comparison_pages_name_the_rivals_the_site_already_answers():
    site = [
        {"path": "/alternatives/n8n"},
        {"path": "/compare/claw-messenger-vs-sendblue"},
        {"path": "/blog/loopmessage-alternatives"},
        {"path": "/vs/bluebubbles/"},
        {"path": "/blog/airmessage-vs-claw-messenger"},
        {"path": "/compare/sendblue-vs-linq"},
        # Not rivals: two of the site's own plans, a topic page, a section index.
        {"path": "/blog/pro-vs-agency"},
        {"path": "/compare/imessage-api"},
        {"path": "/alternatives/best"},
        {"path": "/alternatives"},
        {"path": "/pricing"},
        {"path": "/blog/how-to-compare-apis"},
    ]
    found = agent.site_comparisons(site, "www.clawmessenger.com")
    assert [(row["name"], row["path"]) for row in found] == [
        ("N8n", "/alternatives/n8n"),
        ("Sendblue", "/compare/claw-messenger-vs-sendblue"),
        ("Loopmessage", "/blog/loopmessage-alternatives"),
        ("Bluebubbles", "/vs/bluebubbles"),
        ("Airmessage", "/blog/airmessage-vs-claw-messenger"),
        ("Linq", "/compare/sendblue-vs-linq"),
    ]


def test_leads_rank_the_sites_own_comparisons_and_checked_rivals_above_search_neighbours():
    earlier = {
        "competitors": [
            {"name": "Photon", "host": "photon.codes", "label": "direct", "use": "comparison"},
            {"name": "Zapier", "host": "zapier.com", "label": "drift", "use": "none"},
        ]
    }
    competitors, _ = agent.merge_competitors(
        host="clawmessenger.com",
        keyword_evidence={"competitors": ["digitaltrends.com", "airdroid.com", "zapier.com"]},
        site_pages=[{"path": "/compare/claw-messenger-vs-sendblue"}],
        earlier=earlier,
    )
    names = [row["name"] for row in competitors]
    assert names[:2] == ["Photon", "Sendblue"]
    # A rival the earlier research labelled drift ranks with the search neighbours.
    assert names.index("Zapier") > names.index("Sendblue")
    rows = {row["name"]: row for row in competitors}
    assert rows["Sendblue"]["detail"]["site_comparison_page"] == {
        "path": "/compare/claw-messenger-vs-sendblue"
    }
    assert rows["Zapier"]["detail"]["earlier_research"]["label"] == "drift"
    # Without the v11 inputs the ranking is v9's, by how many sources agree.
    plain, _ = agent.merge_competitors(
        host="clawmessenger.com", keyword_evidence={"competitors": ["a.com", "b.com"]}
    )
    assert [row["name"] for row in plain] == ["a.com", "b.com"]


def test_the_research_brief_presents_leads_to_check_and_the_earlier_set():
    data = research_context()
    competitors, cited = agent.merge_competitors(
        host="example.com",
        keyword_evidence={"competitors": ["fotor.com"]},
        site_pages=[{"path": "/alternatives/linear"}],
        audit={"ai_visibility": {"cited_domains": [{"domain": "docs.adobe.com", "answers": 3}]}},
    )
    earlier = agent.read_competitors(
        agent.competitors_document(
            researched(),
            run_id=uuid4(),
            host="example.com",
            today=date(2026, 10, 1),
            portfolio_path="reports/content-plan/x/PORTFOLIO.md",
        ),
        today=date(2026, 10, 15),
    )
    documents = agent.brief_documents(
        data,
        pages(),
        competitors=competitors,
        sources={"Earlier competitor research": agent.COMPETITORS_PATH},
        run_id=uuid4(),
        cited_sites=cited,
        earlier=earlier,
    )
    by_name = {path.rsplit("/", 1)[1]: content for path, content in documents.items()}
    brief = by_name["BRIEF.md"].decode()
    assert "## Competitor research" in brief and "not a checked list" in brief
    assert "- Linear: the site already has a comparison page for it (/alternatives/linear)" in brief
    assert "- fotor.com (fotor.com): ranks for the same searches" in brief
    assert "checked 2026-10-01 (14 days ago), 4 competitors with labels" in brief
    assert "It is recent: start from it" in brief
    assert "where answers come from, not competitors" in brief
    shown = json.loads(by_name["context.json"])
    assert shown["competitor_research"]["file"] == agent.COMPETITORS_PATH
    assert shown["earlier_competitor_research"]["age_days"] == 14
    # A stale set asks for every channel again.
    stale = agent.render_research_brief(
        {**json.loads(by_name["context.json"])["program"]},
        [],
        {},
        agent.brief_paths(uuid4()),
        earlier={**earlier, "age_days": 45},
    )
    assert "older than 30 days: run every channel again" in stale
    assert "None. Find them with the research channels." in stale
    # A v10 brief is unchanged.
    v10 = agent.brief_documents(
        context(), pages(), competitors=competitors, sources={}, run_id=uuid4()
    )
    old = next(v for k, v in v10.items() if k.endswith("BRIEF.md")).decode()
    assert "## Competitors earlier steps found" in old and "## Competitor research" not in old


def test_the_competitor_file_keeps_labelled_checked_rows_only():
    raw = agent.competitors_document(
        researched(),
        run_id="r1",
        host="www.clawmessenger.com",
        today=date(2026, 10, 15),
        portfolio_path="reports/content-plan/r1/PORTFOLIO.md",
    )
    value = json.loads(raw)
    assert value["schema"] == agent.COMPETITORS_SCHEMA and value["host"] == "clawmessenger.com"
    rows = {row["name"]: row for row in value["competitors"]}
    assert set(rows) == {"Sendblue", "BlueBubbles", "Zapier", "Linq"}
    assert rows["Sendblue"]["host"] == "sendblue.com"
    assert rows["Sendblue"]["channels"] == ["roundups", "launches"]
    assert "evidence_url" not in rows["Linq"]
    assert value["channels"] == [
        {"channel": "roundups", "queries": 1, "found": ["Sendblue"]},
        {"channel": "launches", "queries": 1, "found": ["Chert"]},
    ]
    assert len(raw) <= agent.COMPETITORS_MAX_BYTES
    # Nothing labelled: no file, so an earlier set stays as it was.
    assert (
        agent.competitors_document(
            portfolio(2), run_id="r", host="x.com", today=date.today(), portfolio_path="p"
        )
        is None
    )
    # Reading back checks the schema and counts the age.
    earlier = agent.read_competitors(raw, today=date(2026, 10, 20))
    assert earlier["age_days"] == 5 and len(earlier["competitors"]) == 4
    assert agent.read_competitors(b'{"schema": "other"}', today=date.today()) is None
    assert agent.read_competitors(b"not json", today=date.today()) is None


def test_a_drift_rival_gets_no_alternatives_or_comparison_page():
    value = researched()
    value["opportunities"] = [
        opportunity(1, title="Sendblue vs Claw", format="comparison", competitor="Sendblue"),
        opportunity(2, title="Zapier alternatives", format="alternative", competitor="zapier"),
        opportunity(3, title="Best messaging APIs", format="roundup", competitor="Zapier"),
    ]
    kept, left_out = agent.normalize(research_context(), pages(), value)
    assert [entry["item"]["title"] for entry in kept] == ["Sendblue vs Claw", "Best messaging APIs"]
    assert left_out == [
        {
            "title": "Zapier alternatives",
            "reason": "Zapier is labelled drift in the competitor research: it does another job, "
            "so it gets no alternatives or comparison page",
        }
    ]
    # A v10 plan keeps it: its portfolio has no labels to go on.
    kept, left_out = agent.normalize(context(), pages(), value)
    assert len(kept) == 3 and not left_out
    # The plan's coverage keeps the labelled set and what each channel searched.
    data = research_context()
    plan, backlog = agent.fill(data, kept)
    quality = agent.coverage(data, kept, [], backlog, value, plan)
    assert {row["name"] for row in quality["competitors"]} == {
        "Sendblue",
        "BlueBubbles",
        "Zapier",
        "Linq",
    }
    assert [row["channel"] for row in quality["competitor_channels"]] == ["roundups", "launches"]
    # No target row for a drift competitor.
    found = targets.targets(data, plan, competitors=quality["competitors"])["targets"]
    named = {row["label"] for row in found if row["kind"] == "competitor"}
    assert "Sendblue" in named and "Zapier" not in named


def test_v11_is_pinned_beside_v10():
    assert editorial.POLICY["version"] == "content-editorial-v11"
    assert editorial.POLICY["competitor_file"] == agent.COMPETITORS_PATH
    assert editorial.POLICY["competitor_channels"] == list(agent.CHANNELS)
    assert "competitor_file" not in editorial.V10_POLICY
    labels = agent.PORTFOLIO_JSON_SCHEMA_V2["properties"]["competitors"]["items"]["properties"]
    assert labels["label"] == {"enum": ["direct", "adjacent", "diy", "drift"]}


def run_check(tmp_path, value, *, research=True):
    brief = tmp_path / "reports/content-plan/run/brief"
    brief.mkdir(parents=True)
    data = research_context() if research else context()
    program = agent.brief_documents(
        data, pages(), competitors=[], sources={}, run_id="run", cited_sites=[]
    )
    for path, content in program.items():
        (brief / path.rsplit("/", 1)[1]).write_bytes(content)
    output = tmp_path / "PORTFOLIO.md"
    output.write_text(document(value))
    result = subprocess.run(  # noqa: S603 — the procedure's own check script, local files
        [sys.executable, "-I", str(CHECK), str(output), str(brief)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout


def test_the_procedure_check_asks_for_labels_evidence_and_every_channel(tmp_path):
    value = researched()
    value["opportunities"] = [
        opportunity(1, format="comparison", competitor="Zapier"),
        opportunity(2, format="alternative", competitor="Nobody"),
    ]
    code, out = run_check(tmp_path, value)
    assert code == 1
    assert "competitor 'Somebody': label must be one of" in out
    assert "competitor 'Linq': evidence_url must be the https page you opened" in out
    assert "#1: Zapier is labelled drift" in out
    assert "#2: 'Nobody' is not in competitors" in out
    assert "no queries for comparison_pages, job_and_stack" in out
    # A v10 brief runs none of these checks.
    code, out = run_check(tmp_path / "v10", value, research=False)
    assert "labelled drift" not in out and "competitor_research" not in out
