"""A synthetic site shaped like tin.computer's own marketing site, with Search Console rows.

Numbers are invented to reproduce the defects an earlier audit missed: overlapping
/compare/{x}-alternatives and /alternatives/{x} pages, near-page-one searches, an indexable
sign-in page ranking for the brand, /nl/ pages declaring English, indexable ad landing pages
in the sitemap, and search pages missing from the sitemap.
"""

from __future__ import annotations

from organic_site_stub import SyntheticSite, html_page, sitemap

HOST = "tin.computer"
BASE = f"https://{HOST}"
TOPICS = [
    "semrush",
    "moz",
    "ahrefs",
    "ubersuggest",
    "similarweb",
    "spyfu",
    "serpstat",
    "mangools",
    "seranking",
    "surfer",
    "clearscope",
    "marketmuse",
    "frase",
    "jasper",
    "copyai",
    "writesonic",
    "hubspot",
]
COMPARE_ALTERNATIVES = [f"/compare/{topic}-alternatives" for topic in TOPICS]
ALTERNATIVES = [f"/alternatives/{topic}" for topic in TOPICS[:16]]
TOP_LEVEL = ["/pricing", "/workflows", "/agents", "/opensource", "/blog"]
COMPARE_OTHER = ["/compare/ai-tools-for-startups", "/compare/best-seo-tools"]
DUTCH = ["/nl/"] + [
    f"/nl/{name}"
    for name in (
        "pricing",
        "workflows",
        "agents",
        "opensource",
        "seo",
        "content",
        "ads",
        "email",
        "social",
        "analytics",
        "about-us",
    )
]
OFFERS = [f"/offer/{name}" for name in ("launch", "founders", "agencies", "trial", "audit", "fall")]
BLOG = [f"/blog/post-{index:03}" for index in range(78)]
# Blog first, as a build tool might order it; a crawl in sitemap order reaches it first.
SITEMAP_PATHS = ["/", *BLOG, *TOP_LEVEL, *COMPARE_ALTERNATIVES, *ALTERNATIVES, *COMPARE_OTHER]
SITEMAP_PATHS += [*DUTCH, *OFFERS]
LASTMOD = "2026-09-20T00:00:00+00:00"
ROBOTS = """User-agent: *
Disallow: /api/
Disallow: /app/

User-agent: GPTBot
Allow: /

Sitemap: https://tin.computer/sitemap.xml
"""

# Page rows: 33 overlapping pattern pages hold 5,300 of 10,000 impressions with no clicks.
PATTERN_IMPRESSIONS = {
    "/compare/semrush-alternatives": 400,
    "/alternatives/semrush": 300,
    "/compare/moz-alternatives": 250,
    "/alternatives/moz": 350,
}
PAGE_ROWS: dict[str, tuple[int, int, float]] = {}
_remaining = 5300 - sum(PATTERN_IMPRESSIONS.values())
_others = [p for p in COMPARE_ALTERNATIVES + ALTERNATIVES if p not in PATTERN_IMPRESSIONS]
for _index, _path in enumerate(_others):
    _share = _remaining // len(_others) + (1 if _index < _remaining % len(_others) else 0)
    PAGE_ROWS[_path] = (0, _share, 11.0 + (_index % 4))
for _path, _impressions in PATTERN_IMPRESSIONS.items():
    PAGE_ROWS[_path] = (0, _impressions, {"/alternatives/moz": 8.5}.get(_path, 12.0))
PAGE_ROWS.update(
    {
        "/": (120, 1500, 2.1),
        "/sign-in": (10, 600, 1.5),
        "/about": (5, 250, 7.0),
        "/compare/ai-tools-for-startups": (0, 310, 5.8),
        "/pricing": (20, 400, 6.5),
        "/nl/": (1, 90, 14.0),
        "/offer/launch": (2, 150, 9.5),
    }
)
for _index in range(20):
    PAGE_ROWS[f"/blog/post-{_index:03}"] = (1, 70, 18.0)
assert sum(row[1] for row in PAGE_ROWS.values()) == 10_000

QUERY_ROWS = [
    ("semrush alternative", "/compare/semrush-alternatives", 0, 200, 9.2),
    ("semrush alternative", "/alternatives/semrush", 0, 150, 11.4),
    ("semrush alternatives", "/compare/semrush-alternatives", 0, 150, 8.8),
    ("semrush alternatives", "/alternatives/semrush", 0, 120, 12.0),
    ("moz alternative", "/alternatives/moz", 0, 300, 8.5),
    ("moz alternative", "/compare/moz-alternatives", 0, 200, 13.0),
    ("ai tools for startups", "/compare/ai-tools-for-startups", 0, 280, 5.8),
    ("tin computer", "/sign-in", 10, 520, 1.5),
    ("tin computer", "/", 30, 300, 2.4),
    ("tin.computer", "/", 80, 900, 1.2),
    ("about tin computer", "/about", 5, 200, 7.0),
    ("tin pricing", "/pricing", 20, 300, 6.5),
    *(
        (f"{topic} alternative", path, 0, 60, 12.5)
        for topic in TOPICS[2:16]
        for path in (f"/compare/{topic}-alternatives", f"/alternatives/{topic}")
    ),
]


def gsc_response(dimensions: tuple[str, ...]) -> dict:
    if dimensions == ("page",):
        return {
            "rows": [
                {
                    "keys": [BASE + path],
                    "clicks": clicks,
                    "impressions": impressions,
                    "ctr": clicks / impressions,
                    "position": position,
                }
                for path, (clicks, impressions, position) in PAGE_ROWS.items()
            ]
        }
    assert dimensions == ("query", "page")
    return {
        "rows": [
            {
                "keys": [query, BASE + path],
                "clicks": clicks,
                "impressions": impressions,
                "ctr": clicks / impressions,
                "position": position,
            }
            for query, path, clicks, impressions, position in QUERY_ROWS
        ]
    }


def site() -> SyntheticSite:
    result = SyntheticSite()
    result.text(f"{BASE}/robots.txt", ROBOTS)
    result.text(
        f"{BASE}/sitemap.xml",
        sitemap([BASE + path for path in SITEMAP_PATHS], lastmod=LASTMOD),
        content_type="application/xml",
    )
    for path in SITEMAP_PATHS:
        title = f"{path.strip('/').replace('/', ' ').replace('-', ' ').title() or 'Home'} | Tin"
        page = html_page(title=title, canonical=BASE + path)
        if path == "/":
            page = html_page(
                title="Tin — marketing that runs itself",
                canonical=BASE + "/",
                json_ld='{"@context":"https://schema.org","@type":"Organization","name":"Tin"}',
            )
        result.page(BASE + path, page)
    result.page(
        f"{BASE}/sign-in",
        html_page(title="Sign in – Iteration Machine", h1=0, canonical=f"{BASE}/"),
    )
    result.page(f"{BASE}/about", html_page(title="About Tin", canonical=f"{BASE}/about"))
    return result


def provider_pages(priority_urls: list[str], limit: int) -> list[dict]:
    """What the crawler returns: priority URLs first, then the sitemap in order."""
    order = list(dict.fromkeys([*priority_urls, *(BASE + p for p in SITEMAP_PATHS)]))[:limit]
    return [
        {
            "url": url,
            "resource_type": "html",
            "status_code": 200,
            "meta": {"title": "Crawled page"},
            "checks": {"canonical": True, "no_title": False, "no_description": False},
            "duplicate_title": False,
            "duplicate_description": False,
            "broken_links": False,
        }
        for url in sorted(order)
    ]
