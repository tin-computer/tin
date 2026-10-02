"""content.plan 0.8.0 knows every page on the site, not only the pages it inspects.

The saved plan of 1 October listed 60 inspected pages and counted 44 more without naming them;
/learn/ai-visibility-audit was among the 44, so the plan proposed a guide the site already had.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

from test_content_plan_editorial import context, pages
from test_content_programs import setup
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_plan as legacy
from tin_lite import content_plan_editorial as editorial
from tin_lite import content_plan_sources as sources
from tin_lite.keyword_plan import markdown_text
from tin_lite.model_providers import ModelUsage
from tin_lite.organic_audit import canonical_json

HOST = "https://example.com"
GAP = {"source_id": "audit:oa_gap", "data": {"id": "oa_gap", "check_id": editorial.ANSWER_CHECK}}
AUDIT = {
    "scope": {"host": "example.com", "market": "US"},
    "site": {
        "files": {
            "sitemaps": {
                "urls": [
                    {"loc": f"{HOST}/learn/ai-visibility-audit"},
                    {"loc": f"{HOST}/pricing/"},
                    {"loc": f"{HOST}/sitemap-blog.xml"},
                    {"loc": "https://other.example/learn"},
                ],
                "urls_capped": False,
            }
        },
        "pages": [
            {"url": f"{HOST}/docs", "fetch": "observed", "title": "Docs | Example"},
            {"url": f"{HOST}/gone", "fetch": "http_error"},
        ],
    },
    "search_console": {
        "value": {
            "pages": [
                {"url": f"{HOST}/pricing", "clicks": 3, "impressions": 120, "position": 6},
                {"url": f"{HOST}/never-seen", "clicks": 0, "impressions": 0, "position": 0},
            ]
        }
    },
    # The crawl sample: it holds neither /learn/ai-visibility-audit nor /docs.
    "crawl": {
        "pages": [
            {"url": f"{HOST}/", "status_code": 200, "title": "Example"},
            {"url": f"{HOST}/pricing", "status_code": 200, "title": "Pricing | Example"},
            {"url": f"{HOST}/broken", "status_code": 404, "title": "Not found"},
        ]
    },
}
KEYWORDS = {"keywords": [{"observations": [{"ranking_url": f"{HOST}/blog/ranked"}]}]}
PUBLISHED = [{"url": f"{HOST}/blog/answer", "title": "Which API sends iMessages?"}]


def site():
    return sources.site_pages(AUDIT, keywords=KEYWORDS, published=PUBLISHED)


def test_the_page_list_is_the_union_of_every_source_once_per_path():
    listed = site()
    by_path = {page["path"]: page for page in listed["pages"]}
    assert list(by_path) == [
        "/",
        "/blog/answer",
        "/blog/ranked",
        "/docs",
        "/learn/ai-visibility-audit",
        "/pricing",
    ]
    # A page the crawl sample missed is listed from the sitemap.
    assert by_path["/learn/ai-visibility-audit"]["sources"] == ["sitemap"]
    # One row per normalized path, with every source that lists it and the crawl's title.
    assert by_path["/pricing"] == {
        "path": "/pricing",
        "sources": ["sitemap", "search_console", "crawl"],
        "impressions": 120,
        "title": "Pricing | Example",
    }
    assert by_path["/docs"]["sources"] == ["crawl"]
    assert by_path["/blog/answer"]["sources"] == ["tin_published"]
    assert by_path["/blog/ranked"]["sources"] == ["keywords"]
    # Files, other sites, failed reads, broken pages and pages without impressions are not pages.
    assert listed["omitted"] == 0
    assert listed["by_source"] == {
        "sitemap": 2,
        "search_console": 1,
        "crawl": 3,
        "tin_published": 1,
        "keywords": 1,
    }


def test_the_page_list_keeps_its_bound_and_counts_the_rest(monkeypatch):
    monkeypatch.setattr(sources, "MAX_SITE_PAGES", 2)
    listed = site()
    # Most impressions first, then most sources: /pricing, then the homepage by path.
    assert [page["path"] for page in listed["pages"]] == ["/", "/pricing"]
    assert listed["omitted"] == 4
    monkeypatch.setattr(sources, "MAX_SITE_PAGES", 2000)
    monkeypatch.setattr(sources, "SITE_PAGES_BYTES", 120)
    assert len(site()["pages"]) == 1


def typed_context():
    data = context()
    data["research"]["site_pages"] = site()
    return data


def opportunity(id, title, kind="article"):
    return {
        "id": id,
        "title": title,
        "intent": f"Buyer task: {title}",
        "brief": f"Explain {title} with checked examples.",
        "action": "new_page",
        "page_id": "",
        "source_ids": ["s001"],
        "verification": ["Inspect the current site before drafting."],
        "rationale": "The inspected pages do not answer this task.",
        "kind": kind,
    }


def proposal(*opportunities):
    return {
        "strategy": "Audit guidance first, then tool choice.",
        "gaps": ["Search demand for the audit job is unmeasured."],
        "excluded": [],
        "opportunities": list(opportunities),
    }


def test_the_model_reads_the_whole_list_beside_the_inspected_pages():
    data, _ = editorial.model_context(typed_context(), pages())
    # Only /pricing was inspected; the list still names every known page and its sources.
    assert [p["url"] for p in data["pages"]["pages"]] == [f"{HOST}/pricing"]
    assert ["/learn/ai-visibility-audit", "s"] in data["site_pages"]["pages"]
    assert ["/pricing", "sgc"] in data["site_pages"]["pages"]
    assert data["site_pages"]["sources"]["s"] == "sitemap"
    assert "site_pages" not in data["research"]


def test_a_tight_model_input_shortens_the_list_before_failing(monkeypatch):
    data = typed_context()
    data["research"]["site_pages"]["pages"] = [
        {"path": f"/blog/post-{index:04d}-about-a-long-descriptive-topic", "sources": ["sitemap"]}
        for index in range(400)
    ]
    full, _ = editorial.model_context(data, pages())
    assert len(full["site_pages"]["pages"]) == 400 and full["site_pages"]["omitted"] == 0
    monkeypatch.setitem(editorial.POLICY, "max_input_bytes", len(canonical_json(full)) - 2_000)
    short, _ = editorial.model_context(data, pages())
    assert 0 < len(short["site_pages"]["pages"]) < 400
    assert short["site_pages"]["omitted"] == 400 - len(short["site_pages"]["pages"])


def test_new_pages_the_site_already_has_are_left_out_and_reported():
    data = typed_context()
    data["research"]["rows"].append(GAP)
    aliases = {"s001": "keyword:k1", "s002": GAP["source_id"]}
    plan, quality = editorial.allocate(
        data,
        proposal(
            # The 1 October plan item that run 1e474e10 found already covered.
            opportunity("audit", "How to audit AI visibility for a SaaS brand"),
            # Shares the audit's words but asks a different question: kept.
            opportunity("tools", "AI visibility tools: choose tracking or an actionable audit"),
            # An answer page matches by address or title only, not by topic words.
            {
                **opportunity("answer", "Is an AI visibility audit worth it?", "answer"),
                "source_ids": ["s002"],
            },
            {**opportunity("docs", "Docs", "answer"), "source_ids": ["s002"]},
        ),
        pages(),
        aliases,
        typed=True,
    )
    kept = [i["id"] for b in plan["batches"] for i in b["items"]]
    assert kept == ["tools", "answer"]
    assert quality["already_on_site"] == [
        {
            "item_id": "audit",
            "title": "How to audit AI visibility for a SaaS brand",
            "page": f"{HOST}/learn/ai-visibility-audit",
            "sources": ["sitemap"],
            "match": "topic",
        },
        {
            "item_id": "docs",
            "title": "Docs",
            "page": f"{HOST}/docs",
            "sources": ["crawl"],
            "match": "address",
        },
    ]
    assert quality["planned_items"] == 2
    report = legacy.render_plan(plan, label="Roadmap", editorial=quality, pages=pages())
    assert "### Already on the site" in report
    assert "How to audit AI visibility for a SaaS brand: left out, the site has" in report
    # An item the founder already kept in the plan is never dropped by a revision.
    data["plan"]["batches"][0]["items"] = [
        {**legacy_item("audit"), "title": "How to audit AI visibility for a SaaS brand"}
    ]
    data["mode"] = "revision"
    plan, quality = editorial.allocate(
        data,
        proposal(opportunity("audit", "How to audit AI visibility for a SaaS brand")),
        pages(),
        aliases,
        typed=True,
    )
    assert quality["already_on_site"] == []


def legacy_item(id):
    from test_content_plan import item

    return item(id)


def test_older_contracts_never_read_or_drop_against_the_list():
    data = typed_context()
    untyped = proposal(opportunity("audit", "How to audit AI visibility for a SaaS brand"))
    for entry in untyped["opportunities"]:
        entry.pop("kind")
    plan, quality = editorial.allocate(data, untyped, pages(), {"s001": "keyword:k1"})
    assert [i["id"] for b in plan["batches"] for i in b["items"]] == ["audit"]
    assert "already_on_site" not in quality


async def test_the_planner_saves_the_whole_list_and_plans_around_it(publication_db, monkeypatch):
    db, storage, project, configured, activities, _model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    # A page Tin published earlier and later found live, from its saved page URL record.
    earlier, _ = await db.create_run(
        project_id=project.id, workflow_id=configured.workflow_id, input_payload={}
    )
    await db.pool.execute(
        "INSERT INTO effect_receipts (execution_key, operation, status, result) "
        "VALUES ($1, 'page_url_projection_v1', 'completed', $2::jsonb)",
        f"page-url:{earlier.id}",
        json.dumps(
            {
                "base": {"url": f"{HOST}/blog/answer", "title": "Which API sends iMessages?"},
                "check": {"live": True, "url": f"{HOST}/blog/answer", "merged": True},
            }
        ),
    )
    seen = {}

    async def research(**kwargs):
        seen.update(kwargs)
        return {
            "scope": {"host": "example.com", "market": "US"},
            "sources": {"audit": {"revision": "c" * 40}},
            "rows": [{"source_id": "keyword:k1", "data": {"keyword": "ai visibility audit"}}],
            "site_pages": sources.site_pages(
                AUDIT, keywords=KEYWORDS, published=kwargs["published"]
            ),
        }

    class Model:
        inputs = []

        async def generate(self, key, request, *, timeout_seconds=None):
            data = json.loads(request.messages[0].content)
            self.inputs.append(data)
            alias = data["sources"][0]["source_id"]
            result = proposal(
                opportunity("audit", "How to audit AI visibility for a SaaS brand"),
                opportunity("tools", "AI visibility tools: choose tracking or an actionable audit"),
            )
            for entry in result["opportunities"]:
                entry["source_ids"] = [alias]
            return SimpleNamespace(parsed=result, usage=ModelUsage(), request_id="model-test")

    monkeypatch.setattr("tin_lite.content_plan_activities.research_sources", research)
    activities.router = Model()
    run = await create()
    await activities.content_plan_execute(str(run.id))
    saved = await db.get_run(run.id)
    assert saved.status.value == "succeeded"
    assert seen["published"] == [
        {"url": f"{HOST}/blog/answer", "title": "Which API sends iMessages?"}
    ]
    assert ["/learn/ai-visibility-audit", "s"] in Model.inputs[0]["site_pages"]["pages"]

    async def read(path):
        return await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=saved.canonical_commit_sha, path=path
        )

    listed = json.loads(await read(legacy.site_pages_path(str(run.id))))
    by_path = {page["path"]: page for page in listed["pages"]}
    assert by_path["/learn/ai-visibility-audit"]["sources"] == ["sitemap"]
    assert by_path["/blog/answer"]["sources"] == ["tin_published"]
    assert listed["sources"] == sources.SITE_SOURCES
    evidence = json.loads(await read(legacy.paths(str(run.id))["evidence.json"]))
    inventory = evidence["editorial"]["site_inventory"]
    assert inventory["path"] == legacy.site_pages_path(str(run.id))
    assert inventory["pages"] == len(listed["pages"]) and inventory["omitted"] == 0
    plan = legacy.parse_plan(await read(legacy.plan_path(configured.id)))
    assert [i["id"] for b in plan["batches"] for i in b["items"]] == ["tools"]
    report = (await read(legacy.paths(str(run.id))["PLAN.md"])).decode()
    assert "The site's page list holds 6 pages" in report
    assert markdown_text(f"{HOST}/learn/ai-visibility-audit") + " (topic match)" in report


async def test_published_pages_are_the_live_ones_in_this_project(publication_db, monkeypatch):
    db, _storage, project, configured, _activities, _model, _create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    for live, path in ((True, "/live"), (False, "/merged-only")):
        run, _ = await db.create_run(
            project_id=project.id, workflow_id=configured.workflow_id, input_payload={}
        )
        await db.pool.execute(
            "INSERT INTO effect_receipts (execution_key, operation, status, result) "
            "VALUES ($1, 'page_url_projection_v1', 'completed', $2::jsonb)",
            f"page-url:{run.id}",
            json.dumps({"base": {"title": path}, "check": {"live": live, "url": f"{HOST}{path}"}}),
        )
    assert await sources.published_pages(db, project) == [{"url": f"{HOST}/live", "title": "/live"}]
    other = SimpleNamespace(id=uuid4())
    assert await sources.published_pages(db, other) == []
