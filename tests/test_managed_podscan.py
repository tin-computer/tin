"""managed.podscan: podcast search, guest appearances and charts on Tin's Podscan key.

Provider answers are synthetic fixtures shaped like Podscan's v1 responses, served through an
httpx MockTransport. No test calls the live API.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from tin_lite import managed_services
from tin_lite.connection_records import ServiceArgumentError
from tin_lite.integrations import IntegrationRateLimitedError, ServiceCallRefused
from tin_lite.managed_services import PODSCAN_PROVIDER, ManagedServices, request_for

KEY = "fixture-podscan-key"
SETTINGS = SimpleNamespace(podscan_api_key=SecretStr(KEY))
TRANSCRIPT = "[00:00:00.000 --> 00:00:29.070] " + "long transcript text " * 2000


def podcast(**overrides):
    return {
        "podcast_id": "pd_example0001",
        "podcast_name": "The Example Builders Show",
        "podcast_url": "https://builders.example.com/podcast",
        "podcast_description": "<p>Interviews with people who <b>build</b> software.</p>"
        + "x" * 5000,
        "podcast_categories": [{"category_id": "ct_1", "category_name": "technology"}],
        "podcast_has_guests": True,
        "podcast_has_sponsors": True,
        "podcast_itunes_id": "1000000001",
        "podcast_reach_score": 71,
        "podcast_release_frequency": "Weekly",
        "publisher_name": "Example Media",
        "reach": {
            "itunes": {"itunes_rating_average": "4.7", "itunes_rating_count": "212"},
            "spotify": {"spotify_rating_average": "4.8", "spotify_rating_count": "96"},
            "audience_size": 6100,
            "social_links": [{"platform": "twitter", "url": "https://x.com/examplehost"}],
            "email": "host@noreply.example.com",
            "website": "https://builders.example.com",
        },
        "rss_url": "https://feeds.example.com/builders.rss",
        "is_active": True,
        "episode_count": 240,
        "language": "en",
        "region": "us",
        "last_posted_at": "2026-10-01T12:00:00+00:00",
        "avg_episode_duration": 3600,
        "podcast_summary": {"style": "Long-form interviews with practitioners."},
        **overrides,
    }


def episode(**overrides):
    return {
        "episode_id": "ep_example0001",
        "episode_title": "How a <em>founding engineer</em> runs agent fleets",
        "episode_url": "https://builders.example.com/ep/1",
        "episode_audio_url": "https://cdn.example.com/1.mp3",
        "episode_duration": 3300,
        "posted_at": "2026-09-20T10:00:00+00:00",
        "episode_transcript": TRANSCRIPT,
        "episode_description": "<p>We talk about agents.</p>",
        "metadata": {
            "hosts": [
                {"host_name": "Ada Host"},
                {"host_name": "SPEAKER_01"},
            ],
            "guests": [
                {
                    "guest_name": "Grace Guest",
                    "guest_company": "Smallco",
                    "guest_occupation": "Founding Engineer",
                    "guest_industry": "AI",
                },
                {"guest_name": "SPEAKER_02"},
            ],
            "sponsors": [{"sponsor_name": "Smallco", "sponsor_url": "https://smallco.example"}],
            "is_branded": True,
            "is_branded_confidence_score": 0.85,
            "summary_short": "Ada interviews Grace about agent orchestration.",
        },
        "_search_highlight": {"transcription": "we run <em>founding engineer</em> fleets"},
        "podcast": podcast(),
        **overrides,
    }


def answering(payload, *, status=200, seen=None):
    def answer(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    return ManagedServices(SETTINGS, transport=httpx.MockTransport(answer))


async def call(service, operation, arguments, *, max_response_bytes=32000):
    return await service.call(
        PODSCAN_PROVIDER,
        operation,
        arguments,
        execution_key="run:step",
        max_response_bytes=max_response_bytes,
    )


def test_podscan_is_a_free_managed_service_procedures_may_bind():
    assert managed_services.is_managed(PODSCAN_PROVIDER)
    assert not managed_services.paid(PODSCAN_PROVIDER)
    assert "Codex procedures" in managed_services.DEFINITIONS[PODSCAN_PROVIDER].unlocks
    assert managed_services.not_configured(PODSCAN_PROVIDER) == (
        "Podscan is not configured on this Tin deployment; "
        "an operator sets TIN_LITE_PODSCAN_API_KEY."
    )


def test_procedures_may_bind_podscan():
    from test_procedure_services import definition as procedure_definition
    from test_procedure_services import manifest as procedure_manifest

    from tin_lite.private_workflows import validate_private_definition

    value = procedure_manifest()
    value["definition"]["integration_requirements"] = [
        {"provider_key": PODSCAN_PROVIDER, "capabilities": ["podcasts.read"], "required": True}
    ]
    value["definition"]["procedure"]["services"] = {
        "podcasts": {"provider_key": PODSCAN_PROVIDER, "max_calls": 30, "max_response_bytes": 24000}
    }
    spec = validate_private_definition(procedure_definition(value))
    assert spec.services[0].provider_key == PODSCAN_PROVIDER


async def test_episode_search_keeps_people_and_sponsors_and_drops_transcripts():
    seen = []
    service = answering(
        {"episodes": [episode()], "pagination": {"total": 40, "last_page": 2}}, seen=seen
    )
    result = await call(
        service,
        "episodes.search",
        {"query": "founding engineer", "has_guests": True, "language": "en", "since": "2026-06-01"},
    )
    params = dict(seen[0].url.params)
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert seen[0].url.path == "/api/v1/episodes/search"
    assert params["has_guests"] == "true" and params["podcast_language"] == "en"
    assert params["since"] == "2026-06-01 00:00:00" and params["show_full_podcast"] == "true"
    record = result["records"][0]
    assert record["title"] == "How a founding engineer runs agent fleets"
    assert record["guests"] == [
        {
            "name": "Grace Guest",
            "company": "Smallco",
            "occupation": "Founding Engineer",
            "industry": "AI",
        }
    ]
    assert record["hosts"] == ["Ada Host"]
    assert record["sponsors"] == ["Smallco"]
    assert record["is_branded"] is True and record["branded_confidence"] == 0.85
    assert record["match"] == "we run founding engineer fleets"
    assert record["podcast"]["apple_ratings"] == 212 and record["podcast"]["reach_score"] == 71
    assert "transcript" not in json.dumps(result) and KEY not in json.dumps(result)
    assert result["total"] == 40 and result["next_page"] == 2
    assert len(json.dumps(result)) < 4000


async def test_a_full_podcast_carries_contacts_marked_unverified():
    result = await call(
        answering({"podcast": podcast()}), "podcasts.get", {"podcast_id": "pd_example0001"}
    )
    assert result["status"] == "ok"
    assert result["listed_email"] == "host@noreply.example.com"
    assert result["style"] == "Long-form interviews with practitioners."
    assert result["description"].startswith("Interviews with people who build software.")
    assert len(result["description"]) <= 600
    assert result["avg_episode_minutes"] == 60 and result["categories"] == ["technology"]


async def test_people_and_their_appearances():
    people = await call(
        answering(
            {
                "entities": [
                    {
                        "entity_id": "en_example0001",
                        "entity_name": "Grace Guest",
                        "entity_type": "person",
                        "company": "Smallco",
                        "occupation": "Founding Engineer",
                        "appearances": {"guests_count": 2, "hosts_count": 0},
                    }
                ],
                "pagination": {"total": 1, "last_page": 1},
            }
        ),
        "people.search",
        {"query": "founding engineer", "search_fields": ["occupation"]},
    )
    assert people["records"][0]["guest_appearances"] == 2 and people["next_page"] is None
    seen = []
    appearances = await call(
        answering(
            {
                "entity": {"entity_id": "en_example0001", "entity_name": "Grace Guest"},
                "appearances": [{"role": "guest", "episode": episode()}],
            },
            seen=seen,
        ),
        "people.appearances",
        {"entity_id": "en_example0001", "since": "2026-01-01"},
    )
    assert dict(seen[0].url.params)["role"] == "guest"
    assert dict(seen[0].url.params)["from"] == "2026-01-01"
    assert appearances["person"]["name"] == "Grace Guest"
    assert appearances["records"][0]["role"] == "guest"
    assert appearances["records"][0]["podcast"]["name"] == "The Example Builders Show"


async def test_charts_list_ranked_shows():
    result = await call(
        answering(
            {
                "podcasts": [
                    {
                        "rank": 1,
                        "name": "Örnek Teknoloji",
                        "publisher": "Örnek Medya",
                        "movement": "UP",
                        "podcast_id": "pd_example0002",
                    }
                ]
            }
        ),
        "charts.top",
        {"platform": "apple", "country": "tr", "category": "technology", "limit": 50},
    )
    assert result["records"] == [
        {
            "rank": 1,
            "name": "Örnek Teknoloji",
            "publisher": "Örnek Medya",
            "movement": "UP",
            "podcast_id": "pd_example0002",
        }
    ]
    assert result["country"] == "tr"


async def test_records_beyond_the_bound_are_cut_and_the_caller_asks_for_fewer():
    payload = {
        "episodes": [episode(episode_id=f"ep_example{n:04d}") for n in range(20)],
        "pagination": {"total": 200, "last_page": 10},
    }
    result = await call(
        answering(payload), "episodes.search", {"query": "agents"}, max_response_bytes=6000
    )
    assert result["truncated"] and 0 < len(result["records"]) < 20
    assert result["next_page"] is None
    assert len(json.dumps(result)) <= 6000


async def test_unknown_ids_are_answers_and_slow_reads_are_known_outcomes():
    missing = await call(
        answering({"message": "Not found"}, status=404),
        "podcasts.get",
        {"podcast_id": "pd_missing0001"},
    )
    assert missing["status"] == "not_found"

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    service = ManagedServices(SETTINGS, transport=httpx.MockTransport(slow))
    timed_out = await call(service, "podcasts.search", {"query": "agents"})
    assert timed_out["status"] == "unavailable" and timed_out["reason"] == "timed_out"


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (429, IntegrationRateLimitedError, None),
        (401, ServiceCallRefused, "provider_refused"),
        (422, ServiceCallRefused, "invalid_request"),
    ],
)
async def test_refusals_are_named_and_never_echo_the_key(status, error, code):
    with pytest.raises(error) as raised:
        await call(
            answering({"message": f"bad token {KEY}"}, status=status),
            "podcasts.search",
            {"query": "agents"},
        )
    if code:
        assert raised.value.code == code
    assert KEY not in str(raised.value)
    assert KEY not in json.dumps(raised.value.provider_error.__dict__, default=str)


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("episodes.search", {}),
        ("episodes.search", {"query": " "}),
        ("episodes.search", {"query": "x" * 201}),
        ("episodes.search", {"query": "agents", "per_page": 51}),
        ("episodes.search", {"query": "agents", "page": 21}),
        ("episodes.search", {"query": "agents", "since": "last week"}),
        ("episodes.search", {"query": "agents", "has_guests": "yes"}),
        ("episodes.search", {"query": "agents", "search_fields": ["audio"]}),
        ("episodes.search", {"query": "agents", "language": "english"}),
        ("episodes.search", {"query": "agents", "api_key": "attacker"}),
        ("podcasts.search", {"query": "agents", "order_by": "downloads"}),
        ("podcasts.get", {"podcast_id": "../../teams"}),
        ("podcasts.get", {"podcast_id": "en_example0001"}),
        ("people.appearances", {"entity_id": "en_example0001", "role": "producer"}),
        ("people.search", {"query": "x", "type": "place"}),
        ("charts.top", {"platform": "apple", "category": "technology"}),
        ("charts.top", {"platform": "spotify", "country": "tr", "limit": 51}),
        ("charts.top", {"platform": "apple", "country": "tr", "category": "../x"}),
        ("podcasts.audience_overlap", {"podcast_id": "pd_example0001"}),
    ],
)
def test_arguments_are_closed_and_bounded(operation, arguments):
    with pytest.raises(ServiceArgumentError):
        request_for(operation, arguments)


async def test_a_used_up_daily_allowance_says_to_continue_without_podscan():
    body = {"message": "Daily API limit exceeded.", "error": "daily_limit_exceeded"}
    with pytest.raises(ServiceCallRefused) as raised:
        await call(answering(body, status=429), "podcasts.search", {"query": "agents"})
    assert raised.value.code == "provider_quota"
    assert not isinstance(raised.value, IntegrationRateLimitedError)
    assert "continue without Podscan" in str(raised.value)
