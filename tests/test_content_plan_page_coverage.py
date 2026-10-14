"""content.plan 1.1.0 (content-editorial-v10): the planner reads every page the site lists.

On 10/8 a production plan held 65 site addresses but the text of 15, and planned new pages
that its docs and agency guides already answered. Offline: pages are fixtures, no site, model or
sandbox is called.
"""

from types import SimpleNamespace

from test_content_plan_agent import context, opportunity, portfolio

from tin_lite import content_plan_activities
from tin_lite import content_plan_agent as agent
from tin_lite import content_plan_editorial as editorial
from tin_lite.content_plan_activities import ContentPlanActivities


def observed(url, html):
    return {"url": url, "html": html, "observed_at": "2026-10-14T00:00:00Z", "sha256": "a" * 64}


DOCS = (
    "<html><head><title>API Docs - Acme</title>"
    '<meta name="description" content="Send and receive messages from your agent."></head>'
    "<body><header><h1>Connect your agent</h1><nav><h2>Menu</h2></nav></header>"
    "<p>Prove one text thread in six steps. " + "Setup words. " * 400 + "</p>"
    "<h2>Receiving Messages</h2><p>Inbound SMS replies arrive on the WebSocket.</p>"
    "<h3>Message sync</h3><footer><h2>Footer links</h2></footer></body></html>"
)


def page(path, *, title="", outline=(), text="", source_id=None, status="inspected"):
    return {
        "url": f"https://example.com{path}",
        "status": status,
        "title": title,
        "outline": list(outline),
        "text": text,
        "source_id": source_id or f"page:{path.strip('/').replace('/', '-') or 'home'}",
    }


SITE = [
    page("/", title="Acme agent messaging API", text="SMS iMessage RCS replies agency pricing"),
    page("/about", title="About Acme", text="SMS agency replies pricing API"),
    page(
        "/docs",
        title="API docs",
        outline=["h2: Receiving messages", "h2: Delivery status", "h3: Register a number"],
        text="Acme API: send a message, then receive the inbound reply.",
    ),
    page(
        "/agency",
        title="Numbers for agencies",
        outline=["h2: One line per client subaccount", "h2: Billing per line"],
        text="Acme agency lines for each client.",
    ),
    page("/blog/acme-pricing", title="Acme pricing", text="Acme plans and monthly price."),
    page("/privacy", title="Privacy", text="Acme receive SMS replies subaccount data."),
    # A redirect alias of /docs.
    {**page("/docs", title="API docs", outline=["h2: Receiving messages"]), "source_id": "dup"},
]


def test_the_outline_keeps_title_description_and_headings_but_never_navigation():
    url = "https://example.com/docs"
    evidence = editorial.page_evidence(observed(url, DOCS), url, text_limit=1500, outline=True)
    assert evidence["title"] == "API Docs - Acme"
    assert evidence["description"] == "Send and receive messages from your agent."
    # The H1 in the page's own header counts; navigation and footer headings never do. The
    # outline reaches past the 1,500-byte opening.
    assert evidence["outline"] == [
        "h1: Connect your agent",
        "h2: Receiving Messages",
        "h3: Message sync",
    ]
    assert len(evidence["text"].encode()) <= 1500 and "Receiving" not in evidence["text"]


def test_earlier_contracts_keep_their_exact_page_evidence():
    url = "https://example.com/docs"
    evidence = editorial.page_evidence(observed(url, DOCS), url, text_limit=6000)
    assert not {"title", "description", "outline"} & set(evidence)
    assert editorial.V9_POLICY["version"] == "content-editorial-v9"
    assert editorial.POLICY["known_page_reads"] == 150


def site_context(paths, *, coverage=True):
    data = context()
    data["research"]["site_pages"]["pages"] = [
        {"path": path, "sources": ["sitemap"], "title": path, "impressions": impressions}
        for path, impressions in paths
    ]
    if coverage:
        data["page_coverage"] = {"reads": 3, "text_bytes": 1500, "concurrency": 2, "nearby": 3}
    return data


def test_known_pages_follow_the_site_order_after_the_pages_already_read():
    data = site_context(
        [("/docs", 5), ("/agency", 40), ("/", 900), ("/blog/a", 0), ("/blog/b", 0), ("/x", 1)]
    )
    urls, omitted = editorial.known_pages(data, ["https://example.com/"], limit=3)
    assert urls == [
        "https://example.com/agency",
        "https://example.com/docs",
        "https://example.com/x",
    ]
    assert omitted == 2


class InMemory(ContentPlanActivities):
    """page_inventory's receipts in memory: no database."""

    def __init__(self):
        self.receipts = {}

    async def saved(self, run_id, stage):
        return self.receipts.get(stage)

    async def save(self, run_id, stage, value):
        return self.receipts.setdefault(stage, value)

    async def active(self, run_id, *, conn=None):
        return None


async def test_every_listed_page_is_read_with_its_opening_and_outline(monkeypatch):
    fetched = []

    async def fetch(url, *, host):
        fetched.append(url)
        return observed(url, f"<title>{url}</title><h2>Section</h2><p>{'word ' * 2000}</p>")

    monkeypatch.setattr(content_plan_activities, "fetch_page", fetch)
    data = site_context([("/docs", 5), ("/agency", 4), ("/a", 3), ("/b", 2), ("/c", 1)])
    data["research"]["scope"] = {"host": "example.com"}
    data["plan"]["host"] = "example.com"
    data["page_text_limit"] = 6000
    activities = InMemory()
    pages = await activities.page_inventory(SimpleNamespace(id="run"), data, bind_sources=True)
    by_url = {p["url"]: p for p in pages["pages"]}
    # The home page is a candidate with the longer excerpt; three listed pages follow at 1,500
    # bytes, and the two the runaway guard leaves out are counted.
    assert list(by_url) == [
        "https://example.com/",
        "https://example.com/docs",
        "https://example.com/agency",
        "https://example.com/a",
    ]
    assert len(by_url["https://example.com/"]["text"].encode()) > 1500
    assert len(by_url["https://example.com/docs"]["text"].encode()) <= 1500
    assert all(p["outline"] == ["h2: Section"] and p["source_id"] for p in pages["pages"])
    assert pages["omitted_known_pages"] == 2
    # A retry reuses the saved inventory.
    assert await activities.page_inventory(SimpleNamespace(id="run"), data) == pages
    assert len(fetched) == 4
    # v9 reads only its candidates.
    activities = InMemory()
    v9 = site_context([("/docs", 5)], coverage=False)
    v9["page_text_limit"] = 6000
    pages = await activities.page_inventory(SimpleNamespace(id="run"), v9)
    assert [p["url"] for p in pages["pages"]] == ["https://example.com/"]
    assert "omitted_known_pages" not in pages and "outline" not in pages["pages"][0]


def test_nearby_pages_match_rare_shared_words_and_skip_pages_that_answer_nothing():
    paths = [
        editorial.page_path(p["url"])
        for p in agent.nearby_pages(
            "Receive SMS replies in an AI agent", "sms receive api", SITE, limit=3
        )
    ]
    # "Acme" is on every page, so it counts for nothing; the home, about and privacy pages and
    # the redirect alias never come back.
    assert paths == ["/docs"]
    near = agent.nearby_pages("SMS lines for agency client subaccounts", "", SITE, limit=3)
    assert [editorial.page_path(p["url"]) for p in near] == ["/agency"]
    assert agent.nearby_pages("Acme vs Linear", "", SITE, limit=3) == []
    assert agent.nearby_pages("Receive SMS replies", "", [], limit=3) == []


def coverage_context():
    data = context()
    data["page_coverage"] = {"reads": 150, "text_bytes": 1500, "concurrency": 8, "nearby": 3}
    return data


def test_a_new_item_carries_its_nearest_pages_as_evidence_and_first_check():
    value = portfolio(0)
    value["opportunities"] = [
        opportunity(1, format="guide", title="Receive SMS replies in an AI agent"),
        opportunity(2, format="alternative", title="Acme vs Linear"),
    ]
    pages = {"pages": SITE, "omitted_candidates": 0}
    kept, _ = agent.normalize(coverage_context(), pages, value)
    first, second = (entry["item"] for entry in kept)
    assert first["source_ids"] == ["keyword:k1", "page:docs"]
    assert first["verification"][0].startswith("Read /docs first:")
    assert "already_covered" in first["verification"][0]
    assert kept[0]["rationale"]["nearby"] == "/docs"
    assert second["source_ids"] == ["keyword:k1"] and "nearby" not in kept[1]["rationale"]
    # A plausible but unusable read: every page unavailable. The item stays, with no evidence.
    unread = {"pages": [{**p, "status": "unavailable"} for p in SITE], "omitted_candidates": 0}
    kept, left_out = agent.normalize(coverage_context(), unread, value)
    assert len(kept) == 2 and not left_out
    assert kept[0]["item"]["source_ids"] == ["keyword:k1"]
    # v9 plans name no nearby pages.
    kept, _ = agent.normalize(context(), pages, value)
    assert kept[0]["item"]["source_ids"] == ["keyword:k1"]
    assert not kept[0]["item"]["verification"][0].startswith("Read ")


def test_a_refresh_that_needs_a_table_changed_is_planned_as_an_update():
    data = coverage_context()
    value = portfolio(0)
    value["opportunities"] = [
        opportunity(
            1,
            format="refresh",
            action="update_page",
            destination="/pricing",
            brief="Answer the cost question first, then a compact choice table and a link.",
        ),
        opportunity(
            2,
            format="refresh",
            action="update_page",
            destination="/blog/old-guide",
            title="Old guide refresh",
            brief="Rewrite the title and opening to meet the main search.",
        ),
    ]
    kept, _ = agent.normalize(data, {"pages": [], "omitted_candidates": 0}, value)
    update, refresh = (entry["item"] for entry in kept)
    assert update["format"] == "update" and "kind" not in update
    assert "cannot" in kept[0]["rationale"]["format"]
    assert refresh["format"] == "refresh" and refresh["kind"] == "refresh"
    # v9 keeps the agent's format.
    kept, _ = agent.normalize(context(), {"pages": [], "omitted_candidates": 0}, value)
    assert kept[0]["item"]["format"] == "refresh"


def test_the_brief_shows_each_page_outline_and_says_what_a_refresh_can_change():
    pages = {
        "pages": [{**SITE[2], "page_id": "p001"}],
        "omitted_candidates": 0,
        "omitted_known_pages": 4,
    }
    documents = agent.brief_documents(
        context(), pages, competitors=[], sources={}, run_id="r1", cited_sites=()
    )
    view = next(v for k, v in documents.items() if k.endswith("pages.json"))
    assert view == documents[agent.brief_paths("r1")["pages.json"]]
    assert b'"outline":["h2: Receiving messages"' in view and b'"omitted_known_pages":4' in view
    formats = next(v for k, v in documents.items() if k.endswith("context.json"))
    assert b"never a table, list, link, section or FAQ" in formats


def test_nearby_pages_never_push_a_draft_past_its_evidence_bound():
    big = {**SITE[2], "text": SITE[2]["text"] + " filler" * 5000}
    value = portfolio(0)
    value["opportunities"] = [opportunity(1, format="guide", title="Receive SMS replies")]
    pages = {"pages": [big, *SITE[3:6]], "omitted_candidates": 0}
    kept, _ = agent.normalize(coverage_context(), pages, value)
    item = kept[0]["item"]
    assert item["source_ids"] == ["keyword:k1"]
    assert not item["verification"][0].startswith("Read ")


def test_comparisons_and_translated_pages_get_no_nearby_pages():
    value = portfolio(0)
    value["opportunities"] = [
        opportunity(1, format="comparison", title="Acme vs Linear for SMS replies"),
    ]
    pages = {"pages": SITE, "omitted_candidates": 0}
    kept, _ = agent.normalize(coverage_context(), pages, value)
    assert kept[0]["item"]["source_ids"] == ["keyword:k1"]
    translated = [{**SITE[2], "url": "https://example.com/blog/zh/docs", "source_id": "zh"}]
    assert agent.nearby_pages("Receive SMS replies", "", translated, limit=3) == []
