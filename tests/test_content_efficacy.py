"""organic.content_efficacy offline: a synthetic snapshot, audit and judgment, frozen clock."""

import json
import re

from loop_workflow_fakes import context, definition, gsc, load

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "organic.content_efficacy"
SITE = "tallyfox.example"
CHANNELS = [
    "Paid",
    "Email",
    "Social",
    "Direct",
    "Internal",
    "AI assistants",
    "Search",
    "Referral",
    "Unknown",
    "Ambiguous",
]


def row(path, now, before, position=1.0, queries=(), paid=0, first_seen="2026-01-01"):
    visits = [0] * len(CHANNELS)
    visits[0] = paid
    return {
        "page": f"{SITE}{path}",
        "first_seen": first_seen,
        "first_seen_exact": True,
        "search": {
            "current": [now[0], now[1], None, position],
            "prior": [before[0], before[1], None, position],
            "queries": [[q, 0, 500, 1, False] for q in queries],
        },
        "visits": {"current": {"sessions": sum(visits), "by_channel": visits}},
        "signups": {"first_touch": [0, 0], "activated": [0, 0]},
        "audit": [],
    }


def snapshot(**extra):
    pages = [
        row("/sign-in", (48, 1100), (50, 1000), queries=["tallyfox login"]),
        row("/", (100, 900), (95, 850), queries=["tallyfox", "tallyfox app"]),
        row("/compare/quickbooks-alternatives", (4, 380), (5, 410)),
        row("/alternatives/quickbooks", (31, 2100), (34, 2250)),
        row("/blog/invoice-late-fee-template", (22, 4950), (34, 5100)),
        row("/blog/mileage-log-template", (18, 5000), (19, 4800), position=4.1),
        row("/blog/new-post", (0, 20), (0, 0), first_seen="2026-09-01"),
        row("/offer/spring-sale", (0, 40), (0, 30)),
        row("/blog/earning-post", (60, 900), (58, 880), position=3.0),
    ]
    value = {
        "schema": "tin.traffic_snapshot/1",
        "generated_at": "2026-09-28T13:00:00Z",
        "status": "complete",
        "definitions": {"website_hosts": [SITE], "channels": CHANNELS, "short_columns": []},
        "windows": {"current": ["2026-08-30", "2026-09-26"], "prior": ["2026-08-02", "2026-08-29"]},
        "totals": {"visits": {"current": {"sessions": 300, "by_channel": [0] * 10}}},
        "pages": pages,
        "rest": {"visits": {"paths": 0}},
        "trimmed": {"short_rows_dropped": 0},
    }
    value.update(extra)
    return value


JUDGMENT = {
    "pairs": [],
    "queries": [{"url": "/", "verdict": "answers"}, {"url": "/sign-in", "verdict": "unclear"}],
    "sections": [],
}
FILES = {
    "analytics/traffic-snapshot.json": snapshot(),
    "brand/BRAND.md": "# Tallyfox\n\nInvoicing for small studios.",
}


async def run(monkeypatch, files=FILES, model=JUDGMENT, services=None, inputs=None):
    module = load(KEY, monkeypatch)
    ctx = context(files=files, services=services or {}, models={"judgment": model})
    result = await module.run(ctx, dict(inputs or {}))
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition(KEY)))
    return result["content"], ctx


def block(content):
    return json.loads(
        re.search(r"## Decisions block\s*```json\s*(\{.*?\})\s*```", content, re.S)[1]
    )


def decisions(content):
    return {r["url"]: r for r in block(content)["decisions"]}


def test_manifest_fits_the_code_contract():
    spec = validate_code_definition(definition(KEY))
    assert spec.output_path == "content/efficacy.md"
    assert [route.name for route in spec.model_routes] == ["judgment"]


async def test_ordinary_week_decides_every_page(monkeypatch):
    content, ctx = await run(monkeypatch)
    rows = decisions(content)
    assert (rows["/sign-in"]["decision"], rows["/sign-in"]["action"]) == ("retire", "noindex")
    merge = rows["/compare/quickbooks-alternatives"]
    assert (merge["decision"], merge["action"], merge["target"]) == (
        "merge",
        "301",
        "/alternatives/quickbooks",
    )
    assert rows["/alternatives/quickbooks"]["rule"] == "merge_survivor"
    assert rows["/blog/invoice-late-fee-template"]["rule"] == "decline"
    mileage = rows["/blog/mileage-log-template"]
    assert (mileage["action"], mileage["owner"]) == ("title_description", "content.refresh")
    assert rows["/blog/new-post"]["action"] == "wait"
    assert (rows["/offer/spring-sale"]["decision"], rows["/offer/spring-sale"]["action"]) == (
        "retire",
        "noindex",
    )
    assert rows["/blog/earning-post"]["decision"] == "keep"
    changes = block(content)["url_changes"]
    assert {(c["from"], c["kind"], c["owner"]) for c in changes} == {
        ("/sign-in", "noindex", "organic.technical_fix"),
        ("/compare/quickbooks-alternatives", "301", "organic.technical_fix"),
        ("/offer/spring-sale", "noindex", "organic.technical_fix"),
    }
    assert "appear in the next organic.technical_fix as questions" in content
    assert "approved" not in content.lower()  # no approval to hand-edit in this file
    assert [c["step"] for c in ctx.services.calls] == []  # a fresh snapshot needs no GSC reads
    assert "Tallyfox" in json.dumps(ctx.models.calls[0]["data"]["sources"])


async def test_a_repeat_proposal_is_marked_confirmed(monkeypatch):
    first, _ = await run(monkeypatch)
    files = {**FILES, "content/efficacy.md": first}
    second, _ = await run(monkeypatch, files=files)
    assert decisions(second)["/sign-in"]["confirmed"] is True
    assert "Proposed two weeks running." in second


async def test_paid_visits_block_a_merge(monkeypatch):
    pages = snapshot()["pages"]
    pages[2] = row("/compare/quickbooks-alternatives", (4, 380), (5, 410), paid=3)
    files = {**FILES, "analytics/traffic-snapshot.json": snapshot(pages=pages)}
    content, _ = await run(monkeypatch, files=files)
    row_ = decisions(content)["/compare/quickbooks-alternatives"]
    assert row_["decision"] == "keep" and "paid visits recorded" in row_["reason"]


async def test_unusable_judgment_changes_no_decision(monkeypatch):
    """Plausible but unusable: verdicts on pages we never asked about and a made-up quote."""
    unusable = {
        "pairs": [{"a": "/blog/earning-post", "b": "/", "verdict": "same_intent"}],
        "queries": [{"url": "/blog/earning-post", "verdict": "does_not_answer"}],
        "sections": [{"section": "/blog", "verdict": "off_positioning", "quote": "we sell boats"}],
    }
    content, _ = await run(monkeypatch, model=unusable)
    rows = decisions(content)
    assert rows["/blog/earning-post"]["decision"] == "keep"
    assert not any(r["decision"] == "rewrite" for r in rows.values())


async def test_a_malformed_judgment_is_retried_then_skipped(monkeypatch):
    content, ctx = await run(monkeypatch, model={"pairs": "none"})
    assert [c["step"] for c in ctx.models.calls] == ["judgment", "judgment_retry"]
    assert "Model: skipped." in content
    assert decisions(content)["/sign-in"]["action"] == "noindex"  # code rules still apply


def search_console():
    pages = [
        {"keys": [f"https://{SITE}/"], "clicks": 100, "impressions": 900, "position": 1.0},
        {"keys": [f"https://{SITE}/sign-in"], "clicks": 48, "impressions": 1100, "position": 1.0},
        {"keys": [f"https://{SITE}/blog/old-launch-notes"], "clicks": 0, "impressions": 5},
    ]
    return {
        "gsc_pages_current": gsc(pages),
        "gsc_pages_prior": gsc(pages),
        "gsc_low_query": gsc([]),
    }


async def test_without_a_snapshot_no_url_changes_are_written(monkeypatch):
    content, ctx = await run(monkeypatch, files={}, services=search_console())
    assert "No fresh snapshot; 3 direct Search Console calls." in content
    assert block(content)["url_changes"] == []
    assert "no merge or retirement was written" in content


CASES = {
    "ordinary": (FILES, JUDGMENT, None),
    "no_snapshot": ({}, JUDGMENT, search_console()),
    "unusable_judgment": (FILES, {"pairs": "none"}, None),
}


async def test_qualification_cases_pass_on_their_fixtures(monkeypatch):
    raw = (REPOSITORY_ROOT / "workflow_evals" / KEY / "qualification.json").read_text()
    qualification = Qualification.model_validate_json(raw)
    assert {case.id for case in qualification.cases} == set(CASES)
    for case in qualification.cases:
        files, model, services = CASES[case.id]
        content, _ = await run(
            monkeypatch, files=files, model=model, services=services, inputs=case.inputs
        )
        verdict = assess_output(case, status="succeeded", content=content.encode())
        assert verdict["status"] == "passed", (case.id, verdict["checks"])
