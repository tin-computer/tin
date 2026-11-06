from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from tin_lite.billing_contracts import receipt_charge
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.codex_api_pricing import api_terms
from tin_lite.domain import (
    STUDIO_PROVIDER,
    STUDIO_REVIEW_CAPABILITY,
    STUDIO_VOICE_CAPABILITY,
    RunToolGrant,
)
from tin_lite.film_review import ROUTE, FilmReview, FilmReviewError
from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelCapabilityError,
    ModelImage,
    ModelMessage,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    OpenAIModelProvider,
    ProviderName,
)
from tin_lite.run_tools import create_run_tools_app

RUN_ID = UUID("00000000-0000-4000-8000-0000000000cc")
PROJECT_ID = UUID("00000000-0000-4000-8000-0000000000aa")
GRANT = "opaque-film-grant"  # noqa: S105
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(8) + b"IHDR" + bytes(16)).decode()
STILL = {"media_type": "image/png", "data_base64": PNG}


def _request(*images: ModelImage, role: MessageRole = MessageRole.USER) -> ModelRequest:
    return ModelRequest(messages=(ModelMessage(role, "Look at this film.", images=images),))


@pytest.mark.asyncio
async def test_openai_reads_images_beside_the_text_of_a_user_message() -> None:
    class Responses:
        parameters = None

        async def create(self, **parameters):
            self.parameters = parameters
            return SimpleNamespace(output_text="fine", model="gpt-test", id="r", usage=None)

    client = SimpleNamespace(responses=Responses())
    provider = OpenAIModelProvider(api_key="test", client=client)  # noqa: S106
    request = _request(ModelImage("image/png", PNG))
    assert ModelCapability.IMAGE_INPUT in request.required_capabilities()
    await provider.generate(model="gpt-test", request=request)
    assert client.responses.parameters["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Look at this film."},
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{PNG}",
                    "detail": "high",
                },
            ],
        }
    ]
    await provider.generate(model="gpt-test", request=_request())
    assert client.responses.parameters["input"][0]["content"] == "Look at this film."


@pytest.mark.asyncio
async def test_images_need_a_route_that_reads_them_and_a_user_message() -> None:
    provider = SimpleNamespace(
        name=ProviderName.OPENAI, capabilities=OpenAIModelProvider.capabilities
    )
    text_only = ModelRoute(
        key="text", provider=ProviderName.OPENAI, model="m", capabilities=frozenset({"text"})
    )
    router = ModelRouter(providers={ProviderName.OPENAI: provider}, routes=(text_only,))
    with pytest.raises(ModelCapabilityError, match="image_input"):
        await router.generate("text", _request(ModelImage("image/png", PNG)))
    reader = ModelRouter(providers={ProviderName.OPENAI: provider}, routes=(ROUTE,))
    for request, message in (
        (_request(ModelImage("image/png", PNG), role=MessageRole.ASSISTANT), "user message"),
        (_request(ModelImage("image/gif", PNG)), "PNG or JPEG"),
        (_request(*[ModelImage("image/png", PNG)] * 17), "at most 16"),
    ):
        with pytest.raises(ValueError, match=message):
            await reader.generate(ROUTE.key, request)


class ReviewDatabase:
    def __init__(self, *, used: int = 0) -> None:
        self.receipts: dict[str, dict] = {}
        self.statuses: dict[str, str] = {}
        self.failed: list[str] = []
        self.used = used

    @asynccontextmanager
    async def effect_lock(self, execution_key: str, operation: str, *, conn=None):
        async def fetchval(query, *args):
            return self.used + len(self.receipts)

        yield SimpleNamespace(transaction=nullcontext, fetchval=fetchval), None

    async def get_effect(self, key, *, conn=None):
        if key not in self.receipts:
            return None
        return SimpleNamespace(status=self.statuses[key], result=self.receipts[key])

    async def start_effect(self, conn, *, execution_key: str, operation: str) -> None:
        self.receipts.setdefault(execution_key, {})
        self.statuses[execution_key] = "started"

    async def save_effect_progress(self, conn, *, execution_key, result):
        self.receipts[execution_key] = result

    async def complete_effect(self, conn, *, execution_key: str, result: dict) -> None:
        self.receipts[execution_key] = result
        self.statuses[execution_key] = "completed"

    async def fail_effect(self, conn, *, execution_key: str, error_message: str) -> None:
        self.statuses[execution_key] = "failed"
        self.failed.append(error_message)


class Router:
    def __init__(self, parsed) -> None:
        self.parsed = parsed
        self.requests: list[ModelRequest] = []

    async def generate(self, route_key, request):
        assert route_key == ROUTE.key
        self.requests.append(request)
        return SimpleNamespace(parsed=self.parsed)


FINDINGS = {
    "verdict": "fix",
    "summary": "The poster shows a spinner.",
    "findings": [
        {"where": "poster", "what": "A spinner covers the inbox.", "fix": "Hold 1 s first."}
    ],
}


@pytest.mark.asyncio
async def test_a_review_reads_the_stills_once_and_replays_its_findings() -> None:
    database, router = ReviewDatabase(), Router(FINDINGS)
    review = FilmReview(database=database, router=router)
    result = await review.review(
        run_id=RUN_ID, request_id="r1", spec='{"beats": []}', checks="", images=[STILL, STILL]
    )
    assert result["verdict"] == "fix" and not result["replayed"]
    assert result["findings"][0]["where"] == "poster"
    sent = router.requests[0]
    assert len(sent.messages[0].images) == 2 and sent.output_schema is not None
    again = await review.review(
        run_id=RUN_ID, request_id="r1", spec='{"beats": []}', checks="", images=[STILL, STILL]
    )
    assert again["replayed"] and len(router.requests) == 1
    with pytest.raises(FilmReviewError, match="different review"):
        await review.review(
            run_id=RUN_ID, request_id="r1", spec='{"beats": [1]}', checks="", images=[STILL]
        )


@pytest.mark.asyncio
async def test_a_review_holds_its_stills_to_their_storyboard_and_the_example() -> None:
    router = Router(FINDINGS)
    review = FilmReview(database=ReviewDatabase(), router=router)
    board = "# Ranked\n\n## 1. Ranked\n- Mark: poster\n- Caption: Tin ranks them.\n"
    await review.review(
        run_id=RUN_ID,
        request_id="r1",
        spec='{"beats": []}',
        checks="",
        images=[STILL, STILL],
        storyboard=board,
        example=STILL,
    )
    message = router.requests[0].messages[0]
    content = json.loads(message.content)
    assert content["storyboard"] == board
    assert content["images"].endswith("then the worked example's contact sheet")
    assert len(message.images) == 3
    assert "the bar" in router.requests[0].system


@pytest.mark.asyncio
async def test_a_review_is_refused_before_paying_when_it_cannot_be_read() -> None:
    review = FilmReview(database=ReviewDatabase(), router=Router(FINDINGS))
    for images, message in (
        ([], "1-12 stills"),
        ([STILL] * 13, "1-12 stills"),
        ([{"media_type": "image/gif", "data_base64": PNG}], "PNG or JPEG"),
        ([{"media_type": "image/png", "data_base64": "not base64!"}], "base64"),
    ):
        with pytest.raises(FilmReviewError, match=message):
            await review.review(run_id=RUN_ID, request_id="r", spec="{}", checks="", images=images)
    exhausted = FilmReview(database=ReviewDatabase(used=4), router=Router(FINDINGS))
    with pytest.raises(FilmReviewError, match="all of its reviews"):
        await exhausted.review(run_id=RUN_ID, request_id="r", spec="{}", checks="", images=[STILL])
    with pytest.raises(FilmReviewError, match="not configured"):
        await FilmReview(database=ReviewDatabase(), router=None).review(
            run_id=RUN_ID, request_id="r", spec="{}", checks="", images=[STILL]
        )


@pytest.mark.asyncio
async def test_a_review_the_model_answers_without_findings_fails_without_a_second_purchase() -> (
    None
):
    database = ReviewDatabase()
    review = FilmReview(database=database, router=Router(None))
    with pytest.raises(FilmReviewError, match="usable review"):
        await review.review(run_id=RUN_ID, request_id="r1", spec="{}", checks="", images=[STILL])
    assert database.statuses[f"{RUN_ID}:studio-review:r1"] == "failed"
    with pytest.raises(FilmReviewError, match="unresolved"):
        await review.review(run_id=RUN_ID, request_id="r1", spec="{}", checks="", images=[STILL])
    # Findings without a verdict are shaped to fit; an empty list means ready.
    ready = FilmReview(database=ReviewDatabase(), router=Router({"findings": []}))
    result = await ready.review(
        run_id=RUN_ID, request_id="r2", spec="{}", checks="", images=[STILL]
    )
    assert result["verdict"] == "ready" and result["findings"] == []


class GrantDatabase:
    def __init__(self, capabilities: tuple[str, ...]) -> None:
        self.capabilities = capabilities

    async def authorize_run_tool_grant(self, *, token: str, capability: str | None = None):
        if token != GRANT or (capability is not None and capability not in self.capabilities):
            return None
        return RunToolGrant(
            project_id=PROJECT_ID,
            run_id=RUN_ID,
            connection_id=None,
            external_account_id="",
            sandbox_id="sbx-film",
            provider_key=STUDIO_PROVIDER,
            capabilities=self.capabilities,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


class FakeReview:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def review(self, **values):
        self.calls.append(values)
        if values["request_id"] == "boom":
            raise FilmReviewError("this run has used all of its reviews")
        return {"verdict": "ready", "summary": "", "findings": [], "replayed": False}


@pytest.mark.asyncio
async def test_the_review_route_needs_the_film_grant() -> None:
    film_review = FakeReview()
    body = {"request_id": "r1", "spec": "{}", "checks": "", "images": [STILL]}
    voice_only = GrantDatabase((STUDIO_VOICE_CAPABILITY,))
    for database, status in ((voice_only, 403), (GrantDatabase((STUDIO_REVIEW_CAPABILITY,)), 200)):
        _server, app = create_run_tools_app(
            settings=SimpleNamespace(switchboard_public_url="https://tin.test"),
            runtime=lambda database=database: SimpleNamespace(
                database=database, film_review=film_review
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
        ) as client:
            assert (await client.post("/studio/review", json=body)).status_code == 401
            answer = await client.post(
                "/studio/review", json=body, headers={"Authorization": f"Bearer {GRANT}"}
            )
            assert answer.status_code == status
            if status == 200:
                assert answer.json()["verdict"] == "ready"
                assert film_review.calls[0]["run_id"] == RUN_ID
                assert film_review.calls[0]["storyboard"] == ""
                assert film_review.calls[0]["example"] is None
                refused = await client.post(
                    "/studio/review",
                    json={**body, "request_id": "boom"},
                    headers={"Authorization": f"Bearer {GRANT}"},
                )
                assert refused.status_code == 422 and "reviews" in refused.json()["error"]


def test_a_film_run_may_buy_a_review_and_its_codex_requests_keep_their_rates() -> None:
    film = next(w for w in BUILTIN_WORKFLOWS if w.key == "creative.feature_film").definition
    demo = next(w for w in BUILTIN_WORKFLOWS if w.key == "creative.product_demo").definition
    terms = api_terms(film)
    assert terms["operations"] == ["codex_api", "tool", "native_model"]
    assert ROUTE.model in terms["service_pricing"]["models"]
    assert "service_pricing" not in api_terms(demo)
    assert api_terms(demo)["operations"] == ["codex_api", "tool"]
    card = terms["pricing"]
    record = {
        "pricing": card,
        "provider": card["provider"],
        "model": card["model"],
        "service_tier": card["service_tier"],
        "endpoint": "responses",
        "outcome": "response_received",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "web_search_calls": 0,
        },
    }
    plain = {key: value for key, value in terms.items() if key != "service_pricing"}
    charged = receipt_charge(terms, "codex_api", record)
    assert charged is not None and charged == receipt_charge(plain, "codex_api", record)
