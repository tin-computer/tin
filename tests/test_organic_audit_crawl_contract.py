"""The v10 DataForSEO crawl contract: a pinned page cap and priority URLs; offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from tin_lite.dataforseo import DataForSEO, DataForSEORefused

HOST = "example.com"
BASE = f"https://{HOST}"


def test_crawl_request_defaults_reproduce_pinned_requests_exactly():
    assert DataForSEO.crawl_request(host=HOST, tag="t", respect_sitemap=True) == {
        "target": HOST,
        "start_url": f"{BASE}/",
        "tag": "t",
        "max_crawl_pages": 100,
        "allow_subdomains": False,
        "enable_www_redirect_check": False,
        "respect_sitemap": True,
        "load_resources": False,
        "enable_javascript": False,
        "enable_browser_rendering": False,
        "enable_content_parsing": False,
        "store_raw_html": False,
        "crawl_delay": 2000,
    }
    steered = DataForSEO.crawl_request(
        host=HOST, tag="t", max_pages=250, priority_urls=[f"{BASE}/a", f"{BASE}/b"]
    )
    assert steered["max_crawl_pages"] == 250
    assert steered["priority_urls"] == [f"{BASE}/a", f"{BASE}/b"]


@pytest.mark.parametrize(
    "changes",
    [
        {"max_pages": 301},
        {"max_pages": 0},
        {"priority_urls": ["https://www.example.com/a"]},
        {"priority_urls": ["http://example.com/a"]},
        {"priority_urls": [f"{BASE}/a", f"{BASE}/a"]},
        {"priority_urls": [f"{BASE}/{index}" for index in range(21)]},
        {"priority_urls": [f"{BASE}/a#top"]},
    ],
)
def test_crawl_request_refuses_caps_and_priority_urls_outside_the_contract(changes):
    with pytest.raises(ValueError):
        DataForSEO.crawl_request(host=HOST, tag="t", **changes)


@pytest.mark.asyncio
async def test_pages_limit_and_recovery_follow_the_pinned_request():
    seen = []
    task_id = str(uuid4())
    request = DataForSEO.crawl_request(
        host=HOST, tag="unique", max_pages=150, priority_urls=[f"{BASE}/a"]
    )

    def handle(message):
        body = json.loads(message.content)
        seen.append((message.url.path, body))
        if message.url.path.endswith("/pages"):
            result = [{"items": [{"url": f"{BASE}/{i}"} for i in range(150)]}]
        else:
            echoed = {key: value for key, value in request.items() if key != "priority_urls"}
            result = [{"id": task_id, "metadata": echoed, "cost": 0.02}]
        return httpx.Response(
            200,
            json={"status_code": 20000, "tasks": [{"status_code": 20000, "result": result}]},
        )

    provider = DataForSEO("login", "password", transport=httpx.MockTransport(handle))
    items = await provider.pages(task_id, include_broken=True, limit=150)
    assert len(items) == 150 and seen[0][1][0]["limit"] == 150
    # A provider that does not echo priority URLs still matches on the unique tag and settings.
    found = await provider.recover(request=request, submitted_at="2026-09-29T00:00:00+00:00")
    assert found == {"task_id": task_id, "reported_cost_usd": "0.02"}


def test_a_www_site_with_priority_urls_targets_its_bare_domain():
    # The provider refuses priority URLs under a www. target (40501, checked live 10/16) and
    # accepts them under the bare domain while crawling from the www start URL.
    www = "https://www.example.com"
    steered = DataForSEO.crawl_request(host="www.example.com", tag="t", priority_urls=[f"{www}/a"])
    assert steered["target"] == "example.com" and steered["start_url"] == f"{www}/"
    assert steered["priority_urls"] == [f"{www}/a"]
    # Without priority URLs a www request stays exactly as earlier runs pinned it.
    assert DataForSEO.crawl_request(host="www.example.com", tag="t")["target"] == "www.example.com"


@pytest.mark.asyncio
async def test_submit_accepts_the_bare_target_of_a_www_site():
    task_id = str(uuid4())

    def handle(message):
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [{"id": task_id, "status_code": 20100, "cost": 0.00015}],
            },
        )

    provider = DataForSEO("login", "password", transport=httpx.MockTransport(handle))
    request = DataForSEO.crawl_request(
        host="www.example.com", tag="t", priority_urls=["https://www.example.com/a"]
    )
    assert (await provider.submit(request))["task_id"] == task_id


@pytest.mark.asyncio
async def test_a_refused_task_is_a_definite_refusal_not_an_uncertain_submission():
    def handle(message):
        task = {"id": str(uuid4()), "status_code": 40501, "cost": 0}
        return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})

    provider = DataForSEO("login", "password", transport=httpx.MockTransport(handle))
    with pytest.raises(DataForSEORefused) as exc:
        await provider.submit(DataForSEO.crawl_request(host=HOST, tag="t"))
    assert exc.value.code == 40501 and "Invalid" not in str(exc.value)


@pytest.mark.asyncio
async def test_recovery_never_lists_a_window_that_ends_in_the_future():
    seen = []

    def handle(message):
        seen.append(json.loads(message.content)[0])
        return httpx.Response(
            200, json={"status_code": 20000, "tasks": [{"status_code": 20000, "result": []}]}
        )

    provider = DataForSEO("login", "password", transport=httpx.MockTransport(handle))
    now = datetime.now(UTC)
    request = DataForSEO.crawl_request(host=HOST, tag="t")
    assert await provider.recover(request=request, submitted_at=now.isoformat()) is None
    window_end = datetime.strptime(seen[0]["datetime_to"], "%Y-%m-%d %H:%M:%S %z")
    assert window_end < now
