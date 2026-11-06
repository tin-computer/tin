"""DataForSEO AI answer measurement with fake provider responses. No live calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from tin_lite.ai_answers import (
    API_MODEL_ANSWER,
    CONSUMER_APP_ANSWER,
    DEFAULT_ENGINES,
    ENGINES,
    LIMITS,
    AIAnswersClient,
    AIAnswersRequest,
    Brand,
    CostCeilingExceeded,
    MemoryLedger,
    detect_brand,
    estimate_cost,
    measure,
    trim,
)

BRAND = {"name": "Acme Forms", "domain": "https://www.acmeforms.io/", "aliases": ["Acme"]}
PROMPTS = ["best form builder for startups", "typeform alternatives with logic"]
ALL = list(DEFAULT_ENGINES)


def inputs(**overrides):
    return {
        "prompts": PROMPTS,
        "engines": ALL,
        "brand": BRAND,
        "max_cost_usd": "1",
        **overrides,
    }


class Clock:
    """A clock that only moves when the measurement sleeps."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 30, 12, tzinfo=UTC)
        self.sleeps = 0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps += 1
        self.now += timedelta(seconds=seconds)


def overview(markdown, urls):
    return {
        "type": "ai_overview",
        "markdown": markdown,
        "references": [{"type": "ai_overview_reference", "url": u} for u in urls[:1]],
        "items": [
            {
                "type": "ai_overview_element",
                "markdown": markdown,
                "references": [{"type": "ai_overview_reference", "url": u} for u in urls[1:]],
            }
        ],
    }


def answer_result(engine, text, urls):
    if engine in {"chatgpt", "gemini"}:
        return {
            "model": f"{engine}-consumer",
            "markdown": text,
            "sources": [{"type": "source", "url": u, "domain": "x"} for u in urls[:1]],
            "search_results": [{"url": "https://retrieved-not-cited.example/"}],
            "items": [
                {
                    "type": f"{engine}_text",
                    "markdown": text,
                    "sources": [{"url": u} for u in urls[1:]],
                }
            ],
        }
    if engine in {"google_ai_mode", "google_ai_overview"}:
        return {"type": "organic", "items": [{"type": "organic"}, overview(text, urls)]}
    return {
        "model_name": f"{engine}-model-2026",
        "money_spent": 0.02,
        "items": [
            {"type": "reasoning", "sections": [{"type": "summary_text", "text": "thinking"}]},
            {
                "type": "message",
                "sections": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [{"title": "t", "url": u} for u in urls],
                    }
                ],
            },
        ],
    }


class FakeDataForSEO:
    """Answers the endpoints ai_answers calls. Script it per engine and task."""

    def __init__(self, *, answers=None, pending_rounds=1) -> None:
        self.answers = answers or {}
        self.pending_rounds = pending_rounds
        self.requests: list[tuple[str, str, object]] = []
        self.tasks: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.reject_post: set[str] = set()
        self.task_status: dict[tuple[str, int], int] = {}
        self.live_error: set[str] = set()
        self.post_answer: dict[str, httpx.Response] = {}
        self.never_ready = False

    def answer(self, engine, index):
        default = (
            f"1. **Acme Forms** is the pick for {engine}.\n2. Formly is another option.",
            ["https://www.acmeforms.io/pricing", "https://review.example/best"],
        )
        return self.answers.get((engine, index), default)

    def engine_for(self, path):
        for key, spec in ENGINES.items():
            if path.endswith(spec.post) or (spec.collect and spec.collect in path):
                return key
        raise AssertionError(path)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v3/")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        engine = self.engine_for(path)
        spec = ENGINES[engine]
        if request.method == "POST" and engine in self.post_answer:
            return self.post_answer[engine]
        if request.method == "POST" and spec.mode == "task":
            if engine in self.reject_post:
                return httpx.Response(
                    200, json={"status_code": 40200, "cost": 0, "tasks": [], "tasks_count": 0}
                )
            tasks = []
            for task in body:
                task_id = str(uuid4())
                index = int(task["tag"].rsplit(":", 1)[1])
                self.tasks[task_id] = {"engine": engine, "index": index}
                status = 20100
                if (engine, index) in self.task_status and self.task_status[(engine, index)] < 0:
                    status = -self.task_status[(engine, index)]
                tasks.append(
                    {
                        "id": task_id,
                        "status_code": status,
                        "cost": 0.0012 if status == 20100 else 0,
                        "result": None,
                        "data": task,
                    }
                )
            return httpx.Response(200, json={"status_code": 20000, "tasks": tasks})
        if request.method == "GET":
            task_id = path.rsplit("/", 1)[1]
            known = self.tasks[task_id]
            self.polls[task_id] = self.polls.get(task_id, 0) + 1
            if self.never_ready or self.polls[task_id] <= self.pending_rounds:
                task = {"id": task_id, "status_code": 40602, "cost": 0, "result": None}
            else:
                status = self.task_status.get((known["engine"], known["index"]), 20000)
                text, urls = self.answer(known["engine"], known["index"])
                task = {
                    "id": task_id,
                    "status_code": status,
                    "cost": 0,
                    "result": [answer_result(known["engine"], text, urls)]
                    if status == 20000
                    else None,
                }
            return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})
        # Live engines.
        if engine in self.live_error:
            raise httpx.ConnectError("connection reset")
        (task,) = body
        index = int(task["tag"].rsplit(":", 1)[1])
        text, urls = self.answer(engine, index)
        cost = 0.004 if engine == "google_ai_overview" else 0.0206
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "id": str(uuid4()),
                        "status_code": 20000,
                        "cost": cost,
                        "data": task,
                        "result": [answer_result(engine, text, urls)],
                    }
                ],
            },
        )

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def client_for(fake):
    return AIAnswersClient("login", "password", transport=httpx.MockTransport(fake))


async def run(fake, request, *, ledger=None, clock=None):
    clock = clock or Clock()
    return await measure(
        client_for(fake),
        request,
        ledger=ledger or MemoryLedger(),
        tag="test-run",
        clock=clock,
        sleep=clock.sleep,
    )


def test_inputs_are_bounded_and_normalized():
    request = AIAnswersRequest.from_inputs(inputs(prompts=["  best   form builder "]))
    assert request.prompts == ("best form builder",)
    assert request.brand.domain == "acmeforms.io"
    assert AIAnswersRequest.from_inputs(request.as_inputs()) == request
    bad = [
        {"prompts": []},
        {"prompts": ["x"] * (LIMITS["prompts"] + 1)},
        {"prompts": ["a" * 501]},
        {"prompts": ["Same", "same"]},
        {"engines": ["copilot"]},
        {"engines": ["chatgpt", "chatgpt"]},
        {"market": "FR"},
        {"max_cost_usd": "0"},
        {"max_cost_usd": "21"},
        {"deadline_seconds": 10},
        {"brand": {"name": "Acme", "domain": "not a domain"}},
        {"brand": {"name": "Acme", "domain": "acme.io", "aliases": ["A" * 3] * 11}},
    ]
    for override in bad:
        with pytest.raises(ValueError):
            AIAnswersRequest.from_inputs(inputs(**override))


def test_every_engine_is_labelled_as_one_of_two_measurements():
    kinds = {key: spec.measurement for key, spec in ENGINES.items()}
    assert kinds == {
        "chatgpt": CONSUMER_APP_ANSWER,
        "gemini": CONSUMER_APP_ANSWER,
        "google_ai_mode": CONSUMER_APP_ANSWER,
        "google_ai_overview": CONSUMER_APP_ANSWER,
        "claude": API_MODEL_ANSWER,
        "perplexity": API_MODEL_ANSWER,
        "chatgpt_cloro": CONSUMER_APP_ANSWER,
        "gemini_cloro": CONSUMER_APP_ANSWER,
        "google_ai_mode_cloro": CONSUMER_APP_ANSWER,
    }


async def test_cost_ceiling_rejects_before_any_request():
    fake = FakeDataForSEO()
    request = AIAnswersRequest.from_inputs(inputs(max_cost_usd="0.10"))
    assert estimate_cost(request) == Decimal("0.1552")  # 2 x (3 x 0.0012 + 0.004 + 0.05 + 0.02)
    with pytest.raises(CostCeilingExceeded) as caught:
        await run(fake, request)
    assert caught.value.estimate == Decimal("0.1552")
    assert fake.requests == []
    # High priority doubles the queued prices, and the estimate follows.
    high = AIAnswersRequest.from_inputs(inputs(priority="high"))
    assert estimate_cost(high) == Decimal("0.1624")


async def test_one_row_per_prompt_and_engine_with_answers_citations_and_cost():
    fake = FakeDataForSEO(
        answers={
            ("claude", 1): (
                "Formly is the usual pick. Acme Forms also works for small teams.",
                ["https://formly.example/"],
            ),
            ("google_ai_overview", 0): ("Form builders compare on logic and price.", []),
        }
    )
    request = AIAnswersRequest.from_inputs(inputs())
    result = await run(fake, request)
    rows, summary = result["rows"], result["summary"]
    assert [(r["prompt_index"], r["engine"]) for r in rows] == [
        (i, e) for i in range(2) for e in ALL
    ]
    # Three task engines each post their whole panel once; the live engines ask per prompt.
    assert sorted(p[1] for p in fake.posts()) == sorted(
        [ENGINES[e].post for e in ("chatgpt", "gemini", "google_ai_mode")]
        + [ENGINES[e].post for e in ("google_ai_overview", "claude", "perplexity") for _ in PROMPTS]
    )
    first = {r["engine"]: r for r in rows if r["prompt_index"] == 0}
    assert first["chatgpt"]["measurement"] == CONSUMER_APP_ANSWER
    assert first["claude"]["measurement"] == API_MODEL_ANSWER
    assert first["chatgpt"]["cited_urls"] == [
        "https://www.acmeforms.io/pricing",
        "https://review.example/best",
    ]
    assert "https://retrieved-not-cited.example/" not in json.dumps(rows)
    assert first["chatgpt"]["brand"] == {
        "mentioned": True,
        "cited": True,
        "recommended_first": True,
    }
    assert first["chatgpt"]["cost_usd"] == "0.0012"
    assert first["chatgpt"]["provider_task_id"]
    assert first["claude"]["model"] == "claude-model-2026"
    assert first["claude"]["cost_usd"] == "0.0206"
    assert first["google_ai_overview"]["brand"] == {
        "mentioned": False,
        "cited": False,
        "recommended_first": False,
    }
    second_claude = next(r for r in rows if r["engine"] == "claude" and r["prompt_index"] == 1)
    assert second_claude["brand"] == {"mentioned": True, "cited": False, "recommended_first": False}
    assert summary["rows"] == 12 and summary["answered"] == 12 and summary["complete"]
    assert summary["cost_usd"] == str(
        Decimal("0.0012") * 6 + Decimal("0.004") * 2 + Decimal("0.0206") * 4
    )
    assert summary["by_engine"]["claude"]["measurement"] == API_MODEL_ANSWER
    assert summary["by_engine"]["chatgpt"]["cited"] == 2


async def test_an_engine_failure_leaves_the_other_engines_measured():
    fake = FakeDataForSEO()
    fake.reject_post.add("gemini")  # Balance too low: DataForSEO refuses the whole POST.
    fake.live_error.add("perplexity")  # The paid outcome is unknown and is not repeated.
    fake.task_status[("chatgpt", 1)] = 40101  # The engine failed this one task.
    fake.task_status[("google_ai_mode", 0)] = -40501  # Refused at post time: invalid field.
    fake.task_status[("google_ai_mode", 1)] = 40102  # Collected, but no answer exists.
    request = AIAnswersRequest.from_inputs(inputs())
    rows = (await run(fake, request))["rows"]
    status = {(r["engine"], r["prompt_index"]): (r["status"], r["reason"]) for r in rows}
    assert status[("gemini", 0)] == ("failed", "provider_rejected")
    assert status[("perplexity", 1)] == ("unknown", "provider_result_unavailable")
    assert status[("chatgpt", 0)] == ("answered", None)
    assert status[("chatgpt", 1)] == ("failed", "provider_status_40101")
    assert status[("google_ai_mode", 0)] == ("failed", "provider_status_40501")
    assert status[("google_ai_mode", 1)] == ("no_answer", "engine_gave_no_answer")
    assert status[("claude", 0)] == ("answered", None)
    summary = (await run(FakeDataForSEO(), request))["summary"]
    assert summary["complete"]
    broken = (await run(fake, request))["summary"]
    assert not broken["complete"]
    assert broken["by_engine"]["gemini"]["failed"] == 2
    assert broken["by_engine"]["perplexity"]["unknown"] == 2
    assert broken["cost_unconfirmed_rows"] == 2
    assert sum(1 for r in fake.requests if r[1] == ENGINES["perplexity"].post) == 4


REFUSED = {"status_code": 40200, "status_message": "Payment Required.", "cost": 0}
REJECTED = ("failed", "provider_rejected")
UNKNOWN = ("unknown", "provider_result_unavailable")


@pytest.mark.parametrize(
    ("answer", "posted", "live"),
    [
        (httpx.Response(401), REJECTED, REJECTED),
        (httpx.Response(402), REJECTED, REJECTED),
        (httpx.Response(403), REJECTED, REJECTED),
        (httpx.Response(429), REJECTED, REJECTED),
        (httpx.Response(200, json={**REFUSED, "tasks": None}), REJECTED, REJECTED),
        (httpx.Response(200, json={**REFUSED, "tasks": []}), REJECTED, REJECTED),
        # Not definite refusals: the request may have run and been charged.
        (httpx.Response(503), UNKNOWN, UNKNOWN),
        (httpx.Response(200, json={**REFUSED, "cost": "x"}), UNKNOWN, UNKNOWN),
        (
            httpx.Response(200, json={"status_code": 20000, "tasks": []}),
            ("failed", "task_missing_from_response"),
            UNKNOWN,
        ),
    ],
)
async def test_account_refusals_are_rejected_and_only_they(answer, posted, live):
    fake = FakeDataForSEO()
    fake.post_answer = {"chatgpt": answer, "claude": answer}
    request = AIAnswersRequest.from_inputs(inputs(engines=["chatgpt", "claude"]))
    rows = (await run(fake, request))["rows"]
    outcomes = {(r["engine"], r["status"], r["reason"]) for r in rows}
    assert outcomes == {("chatgpt", *posted), ("claude", *live)}


async def test_tasks_still_queued_at_the_deadline_time_out_with_their_task_ids():
    fake = FakeDataForSEO()
    fake.never_ready = True
    clock = Clock()
    request = AIAnswersRequest.from_inputs(
        inputs(engines=["chatgpt", "claude"], deadline_seconds=120)
    )
    rows = (await run(fake, request, clock=clock))["rows"]
    chatgpt = [r for r in rows if r["engine"] == "chatgpt"]
    assert {r["status"] for r in chatgpt} == {"timeout"}
    assert all(r["provider_task_id"] and r["cost_usd"] == "0.0012" for r in chatgpt)
    assert {r["status"] for r in rows if r["engine"] == "claude"} == {"answered"}
    # 120 s at a 20 s poll interval: six sleeps, then the seventh read finds the deadline.
    assert clock.sleeps == 6
    assert sum(1 for r in fake.requests if r[0] == "GET") == 2 * 7


async def test_a_ledger_never_sends_a_paid_request_twice():
    fake = FakeDataForSEO(pending_rounds=0)
    request = AIAnswersRequest.from_inputs(inputs(engines=["chatgpt", "claude"]))
    ledger = MemoryLedger()
    first = await run(fake, request, ledger=ledger)
    posts = len(fake.posts())
    again = await run(fake, request, ledger=ledger)
    assert len(fake.posts()) == posts  # Completed outcomes are reused; only free reads repeat.
    assert again["rows"] == first["rows"]
    assert ledger.reserved["post:chatgpt"] == Decimal("0.0024")
    assert ledger.reserved["live:claude:0"] == Decimal("0.05")
    # An attempt with no saved outcome is reported as unknown and not sent.
    fresh = MemoryLedger()
    fresh.attempted.add("live:claude:1")
    rows = (await run(FakeDataForSEO(), request, ledger=fresh))["rows"]
    row = next(r for r in rows if r["engine"] == "claude" and r["prompt_index"] == 1)
    assert (row["status"], row["reason"]) == ("unknown", "unconfirmed_previous_request")


def test_mention_citation_and_first_recommendation_are_separate():
    brand = Brand(name="Acme Forms", domain="acmeforms.io", aliases=("Acme",))
    listed = "Top picks:\n\n1. Formly: strong logic.\n2. Acme Forms: cheapest."
    assert detect_brand(listed, [], brand) == {
        "mentioned": True,
        "cited": False,
        "recommended_first": False,
    }
    # A citation on a subdomain counts as cited even when the text never names the brand.
    assert detect_brand("Use a form builder.", ["https://docs.acmeforms.io/x"], brand) == {
        "mentioned": False,
        "cited": True,
        "recommended_first": False,
    }
    assert not detect_brand("x", ["https://acmeforms.io.evil.example/"], brand)["cited"]
    # First list item, as a heading, in bold, or as a bullet.
    for text in (
        "Here are options:\n### 1. Acme\nGood.\n### 2. Formly",
        "Options:\n**1. Acme Forms** leads.\n**2. Formly**",
        "- Acme Forms for startups\n- Formly for enterprises",
    ):
        assert detect_brand(text, [], brand)["recommended_first"], text
    # With no list, the first sentence decides.
    assert detect_brand("Acme Forms fits best. Formly is fine.", [], brand)["recommended_first"]
    assert not detect_brand("Formly fits best. Acme Forms is fine.", [], brand)["recommended_first"]
    # A named competitor earlier in the answer takes first place.
    rival = replace(brand, competitors=("Formly",))
    assert not detect_brand("Formly and Acme Forms both fit best.", [], rival)["recommended_first"]
    assert detect_brand("Formly and Acme Forms both fit best.", [], brand)["recommended_first"]
    # Names match whole words, case-insensitively; the domain counts as a mention.
    assert not detect_brand("Acmeology is great.", [], brand)["mentioned"]
    assert detect_brand("try ACME today", [], brand)["mentioned"]
    assert detect_brand("See acmeforms.io for pricing.", [], brand)["mentioned"]


async def test_answers_and_citations_are_trimmed_after_detection():
    long_text = "Form builders compared. " + "word " * 2000 + "Acme Forms is also here."
    urls = [f"https://site{i}.example/" for i in range(30)] + ["https://acmeforms.io/"]
    fake = FakeDataForSEO(answers={("chatgpt", 0): (long_text, urls)}, pending_rounds=0)
    request = AIAnswersRequest.from_inputs(inputs(engines=["chatgpt"], prompts=PROMPTS[:1]))
    (row,) = (await run(fake, request))["rows"]
    assert row["answer_truncated"] and len(row["answer"]) <= LIMITS["answer_chars"] + 2
    assert "Acme Forms" not in row["answer"]
    assert row["brand"]["mentioned"] and row["brand"]["cited"]
    assert len(row["cited_urls"]) == LIMITS["cited_urls"]
    assert trim("short") == ("short", False)
    text, cut = trim("alpha beta gamma", 12)
    assert cut and text == "alpha beta …"


async def test_malformed_provider_answers_fail_only_their_row():
    fake = FakeDataForSEO(pending_rounds=0)
    original = fake.__call__

    def broken(request):
        response = original(request)
        if request.method == "GET":
            payload = response.json()
            if payload["tasks"][0]["status_code"] == 20000:
                task = payload["tasks"][0]
                if fake.tasks[task["id"]]["index"] == 1:
                    task["result"] = [{"markdown": "x"}, {"markdown": "y"}]
            return httpx.Response(200, json=payload)
        return response

    client = AIAnswersClient("l", "p", transport=httpx.MockTransport(broken))
    request = AIAnswersRequest.from_inputs(inputs(engines=["gemini"]))
    rows = (await measure(client, request, ledger=MemoryLedger(), tag="t", sleep=Clock().sleep))[
        "rows"
    ]
    assert [(r["status"], r["reason"]) for r in rows] == [
        ("answered", None),
        ("failed", "unreadable_result"),
    ]


def cloro_error(status, code):
    envelope = {"code": code, "message": "x", "timestamp": "2026-11-05T12:00:00.000Z"}
    return httpx.Response(status, json={"success": False, "error": envelope})


CLORO = ["chatgpt_cloro", "gemini_cloro", "google_ai_mode_cloro"]


class FakeCloro:
    """Answers cloro's sync monitor endpoints; anything else goes to FakeDataForSEO."""

    CREDITS = {"chatgpt_cloro": "7", "gemini_cloro": "6", "google_ai_mode_cloro": "6"}

    def __init__(self, dataforseo=None) -> None:
        self.dataforseo = dataforseo or FakeDataForSEO(pending_rounds=0)
        self.requests: list[httpx.Request] = []
        self.answer: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.cloro.dev":
            assert request.headers["Authorization"].startswith("Basic ")
            assert request.extensions["timeout"]["read"] == 150
            return self.dataforseo(request)
        self.requests.append(request)
        engine = next(k for k, s in ENGINES.items() if request.url.path == f"/{s.post}")
        if engine in self.answer:
            return self.answer[engine]
        body = json.loads(request.content)
        result = {
            "text": "Acme Forms, Formly",
            "markdown": f"1. **Acme Forms** for {body['prompt']}\n2. Formly",
            "sources": [
                {"position": 1, "url": "https://www.acmeforms.io/", "footnote": False},
                {"position": 2, "url": "https://review.example/best", "footnote": False},
                # A footnote source still counts; a link cloro has not resolved does not.
                {"position": 3, "url": "https://footnote.example/", "footnote": True},
                {"position": 4, "url": "https://www.google.com/goto?url=CAES", "label": "x"},
            ],
        }
        if engine == "chatgpt_cloro":
            result["model"] = "gpt-5-3"
        return httpx.Response(
            200,
            json={"success": True, "result": result},
            headers={"X-Credits-Charged": self.CREDITS[engine], "X-Request-ID": f"req-{engine}"},
        )


def cloro_client(fake, key="cloro-key"):
    return AIAnswersClient(
        "login", "password", cloro_api_key=key, transport=httpx.MockTransport(fake)
    )


async def run_cloro(fake, request, *, key="cloro-key", ledger=None):
    return await measure(
        cloro_client(fake, key), request, ledger=ledger or MemoryLedger(), tag="test-run"
    )


async def test_cloro_engines_ask_chatgpt_gemini_and_ai_mode_with_a_bearer_key():
    assert not set(CLORO) & set(DEFAULT_ENGINES)
    fake = FakeCloro()
    request = AIAnswersRequest.from_inputs(inputs(engines=["chatgpt", *CLORO]))
    # 2 x (0.0012 + 7, 6 and 6 credits at $0.0008).
    assert estimate_cost(request) == Decimal("0.0328")
    ledger = MemoryLedger()
    result = await run_cloro(fake, request, ledger=ledger)
    assert ledger.reserved["live:chatgpt_cloro:0"] == Decimal("0.0056")
    assert ledger.reserved["live:gemini_cloro:0"] == Decimal("0.0048")

    def body(path, prompt):
        if path == "/v1/monitor/chatgpt":
            return {"prompt": prompt, "country": "US", "disableWebSearch": True}
        if path == "/v1/monitor/aimode":
            return {"prompt": prompt, "country": "US", "hl": "en", "device": "desktop"}
        return {"prompt": prompt, "country": "US"}

    def by_prompt(item):
        return item[0], item[1]["prompt"]

    sent = [(r.url.path, json.loads(r.content)) for r in fake.requests]
    assert sorted(sent, key=by_prompt) == sorted(
        (
            [
                (path, {**body(path, p), "include": {"markdown": True}})
                for path in ("/v1/monitor/chatgpt", "/v1/monitor/gemini", "/v1/monitor/aimode")
                for p in PROMPTS
            ]
        ),
        key=by_prompt,
    )
    for request_sent in fake.requests:
        assert request_sent.headers["Authorization"] == "Bearer cloro-key"
        assert request_sent.extensions["timeout"]["read"] == 310
    rows = {(r["engine"], r["prompt_index"]): r for r in result["rows"]}
    for engine in CLORO:
        row = rows[(engine, 0)]
        assert row["measurement"] == CONSUMER_APP_ANSWER and row["status"] == "answered"
        assert row["answer"].startswith("1. **Acme Forms**")
        assert row["cited_urls"] == [
            "https://www.acmeforms.io/",
            "https://review.example/best",
            "https://footnote.example/",
        ]
        assert row["brand"] == {"mentioned": True, "cited": True, "recommended_first": True}
        assert row["provider_task_id"] == f"req-{engine}"
    assert rows[("chatgpt_cloro", 1)]["cost_usd"] == "0.0056"
    assert rows[("google_ai_mode_cloro", 1)]["cost_usd"] == "0.0048"
    assert rows[("chatgpt_cloro", 0)]["model"] == "gpt-5-3"
    assert rows[("gemini_cloro", 0)]["model"] is None
    assert result["summary"]["complete"]
    assert result["summary"]["by_engine"]["chatgpt_cloro"]["cost_usd"] == "0.0112"


def live_shaped(engine, result):
    """A trimmed copy of a live cloro response (2026-11-05), with example domains."""
    return httpx.Response(
        200,
        json={"success": True, "result": result},
        headers={
            "x-credits-charged": FakeCloro.CREDITS[engine],
            "x-request-id": "d7714ba6-9e0c-4fb4-a0fe-97b80e023210",
        },
    )


async def test_cloro_rows_read_live_response_shapes():
    text = "There are several good options.\n\n1. Rival Track for agencies\n2. Acme Forms"
    fake = FakeCloro()
    fake.answer = {
        # No markdown (not asked for), no model, no citation pills, and every source marked
        # footnote: true. They still count: live answers link footnote pages inline.
        "chatgpt_cloro": live_shaped(
            "chatgpt_cloro",
            {
                "model": None,
                "text": text,
                "sources": [
                    {
                        "position": 1,
                        "label": "Website",
                        "url": "https://acmeforms.io/",
                        "footnote": True,
                    },
                    {
                        "position": 2,
                        "label": "",
                        "url": "https://logos.example/a",
                        "footnote": True,
                    },
                ],
                "entities": [{"type": "software", "name": "Acme Forms"}],
                "map": [],
                "followUpForm": None,
                "citationPills": [],
            },
        ),
        # One page linked once per quoted passage.
        "gemini_cloro": live_shaped(
            "gemini_cloro",
            {
                "text": text,
                "sources": [
                    {
                        "position": 1,
                        "label": "Pricing",
                        "url": "https://acmeforms.io/pricing#:~:text=Core",
                    },
                    {
                        "position": 2,
                        "label": "Best",
                        "url": "https://review.example/best#:~:text=1.%20Rival",
                    },
                    {
                        "position": 3,
                        "label": "Best",
                        "url": "https://review.example/best#:~:text=Acme",
                    },
                ],
                "citationPills": [
                    {
                        "label": "Best",
                        "citationPillId": 1,
                        "url": "https://review.example/best",
                        "domain": "review.example",
                        "position": 2,
                    }
                ],
            },
        ),
        # A resolved link keeps Google's redirect beside it.
        "google_ai_mode_cloro": live_shaped(
            "google_ai_mode_cloro",
            {
                "text": text,
                "sources": [
                    {
                        "position": 1,
                        "label": "Best",
                        "url": "https://review.example/best",
                        "redirectLink": "https://www.google.com/goto?url=CAESewHEIkXX",
                    }
                ],
                "citationPills": [],
            },
        ),
    }
    request = AIAnswersRequest.from_inputs(inputs(engines=CLORO, prompts=PROMPTS[:1]))
    rows = {r["engine"]: r for r in (await run_cloro(fake, request))["rows"]}
    assert {r["status"] for r in rows.values()} == {"answered"}
    assert {r["answer"] for r in rows.values()} == {text}
    assert {r["model"] for r in rows.values()} == {None}
    assert rows["chatgpt_cloro"]["cited_urls"] == [
        "https://acmeforms.io/",
        "https://logos.example/a",
    ]
    assert rows["chatgpt_cloro"]["brand"]["cited"]
    assert rows["gemini_cloro"]["cited_urls"] == [
        "https://acmeforms.io/pricing",
        "https://review.example/best",
    ]
    assert rows["gemini_cloro"]["brand"]["cited"]
    assert rows["google_ai_mode_cloro"]["cited_urls"] == ["https://review.example/best"]
    assert rows["chatgpt_cloro"]["cost_usd"] == "0.0056"
    assert rows["chatgpt_cloro"]["provider_task_id"] == "d7714ba6-9e0c-4fb4-a0fe-97b80e023210"


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (cloro_error(400, "VALIDATION_ERROR"), REJECTED),
        (cloro_error(401, "INVALID_API_KEY_FORMAT"), REJECTED),
        (cloro_error(403, "INSUFFICIENT_CREDITS"), REJECTED),
        (cloro_error(429, "CONCURRENCY_LIMIT_EXCEEDED"), REJECTED),
        # cloro charges nothing for an error response, retries included.
        (cloro_error(500, "INTERNAL_SERVER_ERROR"), REJECTED),
        # Not cloro's envelope, or no credit count: the outcome is unknown.
        (httpx.Response(502, text="Bad gateway"), UNKNOWN),
        (httpx.Response(200, json={"success": True, "result": {"text": "x"}}), UNKNOWN),
        (
            httpx.Response(200, text="not json", headers={"X-Credits-Charged": "6"}),
            ("failed", "unreadable_result"),
        ),
    ],
)
async def test_cloro_errors_fail_only_their_engine(answer, outcome):
    fake = FakeCloro()
    fake.answer["gemini_cloro"] = answer
    request = AIAnswersRequest.from_inputs(
        inputs(engines=["chatgpt_cloro", "gemini_cloro"], prompts=PROMPTS[:1])
    )
    rows = {r["engine"]: r for r in (await run_cloro(fake, request))["rows"]}
    assert (rows["gemini_cloro"]["status"], rows["gemini_cloro"]["reason"]) == outcome
    assert rows["chatgpt_cloro"]["status"] == "answered"


async def test_cloro_engines_without_a_key_fail_without_a_request():
    fake = FakeCloro()
    request = AIAnswersRequest.from_inputs(inputs(engines=["chatgpt", "chatgpt_cloro"]))
    rows = (await run_cloro(fake, request, key=None))["rows"]
    assert {(r["engine"], r["status"], r["reason"]) for r in rows} == {
        ("chatgpt", "answered", None),
        ("chatgpt_cloro", *REJECTED),
    }
    assert fake.requests == []
