"""The pages a grounded answer rested on, under their own addresses; what a grounded Gemini
answer searched; whether a company's site still opens."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from tin_lite import company_site, grounding_links
from tin_lite.model_providers import _gemini_grounding, _gemini_searches

LINK = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQH6nVKt7cvTeQ"
OTHER = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFx8yE_oCKk"


async def test_redirect_links_become_the_pages_they_point_to():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append((request.method, str(request.url)))
        if str(request.url) == LINK:
            page = "https://www.globex.test/news/northwind&quot;&gt;2022"
            return httpx.Response(302, headers={"location": page})
        return httpx.Response(500)

    parsed = {"reason": "Bought by Globex.", "sources": [LINK, OTHER, "https://kept.test/"]}
    transport = httpx.MockTransport(answer)
    found = await grounding_links.resolve(grounding_links.links_in(parsed), transport=transport)
    # Only Google's redirect host is asked, with HEAD; the page itself is never fetched.
    assert sorted(asked) == sorted([("HEAD", LINK), ("HEAD", OTHER)])
    assert found == {LINK: "https://www.globex.test/news/northwind"}
    assert grounding_links.replaced(parsed, found)["sources"] == [
        "https://www.globex.test/news/northwind",
        OTHER,
        "https://kept.test/",
    ]
    # Nothing to resolve asks nothing.
    assert await grounding_links.resolve(set(), transport=transport) == {}
    assert await grounding_links.resolved({"a": "b"}) == {"a": "b"}


def candidate(queries, chunks):
    web = [SimpleNamespace(web=SimpleNamespace(uri=uri, title=title)) for uri, title in chunks]
    metadata = SimpleNamespace(web_search_queries=queries, grounding_chunks=web)
    return [SimpleNamespace(grounding_metadata=metadata)]


def test_a_grounded_answer_says_what_it_searched_and_rested_on():
    found = candidate(["who owns northwind"], [(LINK, "globex.com")])
    assert _gemini_searches(found) == 1
    assert _gemini_grounding(found) == {
        "queries": ["who owns northwind"],
        "sources": [{"title": "globex.com", "uri": LINK}],
    }
    # Sources and no queries: it searched, so it counts one, not none (11-27's judge).
    assert _gemini_searches(candidate([], [(LINK, "globex.com")])) == 1
    assert _gemini_searches(candidate([], [])) == 0
    assert _gemini_searches([SimpleNamespace(grounding_metadata=None)]) == 0
    assert _gemini_grounding([SimpleNamespace(grounding_metadata=None)]) is None


async def test_a_site_on_a_private_address_is_never_contacted():
    checked = await company_site.check("localhost")
    assert checked == {"site": "localhost", "opens": False, "lands_on": None, "status": None}


def test_the_site_check_reads_as_a_sentence_and_in_a_message():
    moved = {"site": "northwind.test", "opens": True, "lands_on": "freight.globex.test"}
    gone = {"site": "northwind.test", "opens": False, "lands_on": None}
    fine = {"site": "northwind.test", "opens": True, "lands_on": None}
    assert company_site.said(moved) == "northwind.test redirects to freight.globex.test"
    assert company_site.said(gone).startswith("northwind.test doesn't open")
    assert company_site.said(None) is None
    assert company_site.shown(moved, "northwind.test") == "northwind.test → freight.globex.test"
    assert company_site.shown(gone, "northwind.test") == "northwind.test (doesn't open)"
    assert company_site.shown(fine, "northwind.test") == "northwind.test"
