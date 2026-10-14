"""The warm-reach spike's probe against a fake LinkedIn: what it reads, what it reports."""

import json

import httpx
import pytest

from tin_lite.linkedin_spike_probe import probe

SESSION = {
    "cookies": [{"name": "li_at", "value": "x"}, {"name": "JSESSIONID", "value": '"ajax:1"'}],
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/140.0 Safari/537.36",
    "browser_context": {"accept_language": "en-US", "li_lang": "en_US", "li_track": {"a": 1}},
}
POST = "7508955117895766016"


def linkedin(calls, *, relationship="DISTANCE_2"):
    def handler(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/voyager/api/me":
            return httpx.Response(200, json={"included": [{"publicIdentifier": "founder"}]})
        if "/recent-activity/" in path:
            html = f"<div>urn:li:activity:{POST} urn:li:ugcPost:7508955117447147520</div>"
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if path == "/voyager/api/voyagerSocialDashReactions":
            people = [
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                    "publicIdentifier": "someone",
                    "firstName": "Secret",
                    "headline": "CTO",
                }
            ]
            return httpx.Response(200, json={"data": {"*elements": ["a"]}, "included": people})
        if path == "/voyager/api/feed/comments":
            return httpx.Response(200, json={"elements": []})
        if path == "/voyager/api/identity/dash/profiles":
            return httpx.Response(
                200,
                json={
                    "included": [
                        {
                            "publicIdentifier": "target",
                            "entityUrn": "urn:li:fsd_profile:ABC",
                            "memberRelationship": {"distance": relationship},
                        }
                    ]
                },
            )
        if path.startswith("/voyager/api/voyagerRelationshipsDashMemberRelationships"):
            return httpx.Response(201, json={}, headers={"x-restli-id": "inv"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_the_probe_reads_in_order_and_reports_no_names():
    calls = []
    plan = {"gap": 0, "profiles": ["https://www.linkedin.com/in/target"]}
    report = await probe(SESSION, plan, transport=linkedin(calls))
    assert report["me"]["verdict"] == "ok"
    assert report["activity"]["all"]["activities"] == 1
    assert report["post_day"] == "2026-09-24"
    assert report["reactions"]["tried"][0]["profiles_with_names"] == 1
    assert report["profile_1"]["exact_match"] is True
    assert report["profile_1"]["relationship_hints"] == ["DISTANCE_2"]
    # Nothing that could name a person, and nothing sent without the confirmation.
    text = json.dumps(report)
    assert "Secret" not in text and "someone" not in text and "founder" not in text
    assert "invite" not in report and all(method == "GET" for method, _ in calls)


@pytest.mark.asyncio
async def test_one_request_goes_only_when_confirmed_and_never_to_a_connection():
    target = "https://www.linkedin.com/in/target"
    calls = []
    plan = {"gap": 0, "invite": target, "invite_confirm": target}
    report = await probe(SESSION, plan, transport=linkedin(calls))
    assert report["invite"]["sent"] is True and report["invite"]["restli_id"] is True
    assert [method for method, _ in calls].count("POST") == 1
    calls = []
    report = await probe(SESSION, plan, transport=linkedin(calls, relationship="DISTANCE_1"))
    assert report["invite"] == {**report["invite"], "sent": False}
    assert all(method == "GET" for method, _ in calls)
