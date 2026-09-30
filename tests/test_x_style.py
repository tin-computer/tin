"""Offline X voice sampling and receipt tests; no X or model API calls."""

import json
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from temporalio.exceptions import ApplicationError

from tin_lite import x_style
from tin_lite.x_style_activities import XStyleActivities

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
RUN_ID = UUID("12345678-1234-4678-9234-567812345678")
MODEL_STYLE = {
    "summary": "Short, specific technical observations with dry humor.",
    "limitations": "The sample is small; do not treat this as a fixed persona.",
    "voice": [{"rule": "Use direct, plain wording.", "sources": ["s1"]}],
    "structure": [{"rule": "Open with the concrete behavior.", "sources": ["s1"]}],
    "vocabulary": [{"rule": "Use specific technical terms when useful.", "sources": ["s1"]}],
    "avoid": [{"rule": "Avoid canned launch phrasing.", "sources": []}],
    "demonstration": "A small change made the review step easier to understand.",
}


def post(number, days_ago, text=None, refs=None):
    return {
        "id": str(number),
        "text": text or f"A complete technical observation number {number} for X.",
        "created_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        "referenced_posts": refs or [],
    }


def test_adaptive_50_sample_keeps_bands_and_excludes_unusable_posts():
    pages = [
        {"posts": [post(i, i % 29 + 1) for i in range(1, 41)]},
        {"posts": [post(i, 31 + (i - 41) % 50) for i in range(41, 61)]},
        {"posts": [post(i, 91 + (i - 61) * 12) for i in range(61, 71)]},
    ]
    pages[0]["posts"].extend(
        [
            post(100, 1, "https://example.com"),
            post(101, 2, "Quoted person's words", [{"type": "quoted", "id": "1"}]),
            post(102, 3, "@someone that is right", [{"type": "replied_to", "id": "2"}]),
            post(103, 4, "Reposted person's words", [{"type": "retweeted", "id": "3"}]),
        ]
    )
    selection = x_style.select_own_posts(pages, now=NOW)
    examples = selection["examples"]
    metadata = selection["metadata"]
    assert len(examples) == 50
    assert sum(item["date"] >= "2026-08-30" for item in examples) >= 25
    assert metadata["returned_count"] == 74
    assert metadata["timeline_calls"] == 3
    assert len(metadata["selected_ids"]) == 50
    assert all("Quoted person's words" not in item["text"] for item in examples)
    assert all("Reposted person's words" not in item["text"] for item in examples)
    assert "Quoted person's words" not in json.dumps(metadata)


def test_sampling_bounds_and_complete_text():
    with pytest.raises(ValueError, match="three timeline calls"):
        x_style.select_own_posts([{"posts": []}] * 4, now=NOW)
    with pytest.raises(ValueError, match="150 returned"):
        x_style.select_own_posts([{"posts": [post(i + 1, 1)] * 51} for i in range(3)], now=NOW)
    selected = x_style.select_own_posts(
        [{"posts": [post(1, 1, "A" * 1201), post(2, 2, "Complete own post text.")]}], now=NOW
    )
    assert [item["text"] for item in selected["examples"]] == ["Complete own post text."]


def test_supplied_fallback_and_explicit_guide_preferences():
    samples = x_style.supplied_examples(
        "I built a narrow workflow and checked its result.\n\nI prefer a short opening.",
        source="supplied",
    )
    assert len(samples) == 2
    content = x_style.render_guide(
        MODEL_STYLE,
        account="12345",
        sample_ids={"s1", "s2"},
        metadata={"selected_count": 2, "returned_count": 2},
        existing_preferences="Use plain language.",
        new_preferences="Keep technical details precise.",
    ).decode()
    assert "X account ID: 12345" in content
    assert "Use plain language." in content
    assert "Keep technical details precise." in content
    assert "I built a narrow workflow" not in content
    with pytest.raises(ValueError, match="unavailable sample"):
        x_style.render_guide(
            MODEL_STYLE,
            account="12345",
            sample_ids=set(),
            metadata={},
            existing_preferences="",
            new_preferences="",
        )


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 50


class FakeX:
    def __init__(self, *, protected=False):
        self.protected = protected
        self.calls = []

    async def connection(self, project_id, capability):
        assert capability == "x.posts.read"
        return SimpleNamespace(
            project_id=project_id,
            external_account_id="12345",
            configuration={"protected": self.protected},
        )

    async def timeline(self, connection, **kwargs):
        self.calls.append(kwargs)
        return {
            "posts": [
                post(
                    len(self.calls),
                    10 + 40 * (len(self.calls) - 1),
                    "My full own post describes an exact review step in Tin; "
                    "the source text is private to this extraction request.",
                )
            ],
            "next_cursor": None,
        }


class FakeDB:
    def __init__(self, run, existing_guide=""):
        self.run = run
        self.existing_guide = existing_guide
        self.model_receipt = None
        self.completed = None

    async def get_run(self, run_id, conn=None):
        assert run_id == RUN_ID
        return self.run

    async def get_effect(self, key):
        assert key.endswith(":x_style_context")
        return SimpleNamespace(
            status="completed", result={"revision": "a" * 40, "existing_guide": self.existing_guide}
        )

    async def project_run_progress(self, **kwargs):
        return None

    @asynccontextmanager
    async def effect_lock(self, key, operation):
        assert key.endswith(":x_style_model") and operation == x_style.KEY
        yield object(), self.model_receipt

    async def start_effect(self, conn, *, execution_key, operation):
        self.model_receipt = SimpleNamespace(status="started")

    async def complete_effect(self, conn, *, execution_key, result):
        self.completed = result
        self.model_receipt = SimpleNamespace(status="completed", result=result)


class FakeRouter:
    def __init__(self, parsed=MODEL_STYLE):
        self.parsed = parsed
        self.calls = 0
        self.requests = []

    async def generate(self, route, request, timeout_seconds):
        self.calls += 1
        self.requests.append(request)
        assert route == x_style.ROUTE.key
        assert "source text is private" in request.messages[0].content
        return SimpleNamespace(
            parsed=self.parsed, usage=Usage(), model="gpt-6-sol", request_id="r1"
        )


async def test_activity_keeps_raw_api_text_out_of_receipt(monkeypatch):
    import tin_lite.x_style_activities as activities

    monkeypatch.setattr(activities, "model_usage_scope", lambda **kwargs: nullcontext())
    run = SimpleNamespace(
        id=RUN_ID,
        executor=x_style.KEY,
        status=SimpleNamespace(value="running"),
        project_id=UUID("a0000000-0000-0000-0000-000000000061"),
        input={},
    )
    db = FakeDB(run)
    x = FakeX()
    router = FakeRouter()
    worker = XStyleActivities(database=db, storage=None, router=router, x_connection=x)
    await worker.extract(str(RUN_ID))
    assert len(x.calls) == 3
    assert db.completed["account_id"] == "12345"
    assert db.completed["sampling"]["timeline_calls"] == 3
    assert "source text is private" not in json.dumps(db.completed)
    await worker.extract(str(RUN_ID))
    assert router.calls == 1


async def test_public_guard_and_unknown_attempt_do_not_buy_again(monkeypatch):
    import tin_lite.x_style_activities as activities

    monkeypatch.setattr(activities, "model_usage_scope", lambda **kwargs: nullcontext())
    run = SimpleNamespace(
        id=RUN_ID,
        executor=x_style.KEY,
        status=SimpleNamespace(value="running"),
        project_id=UUID("a0000000-0000-0000-0000-000000000061"),
        input={},
    )
    db = FakeDB(run)
    x = FakeX(protected=True)
    router = FakeRouter()
    worker = XStyleActivities(database=db, storage=None, router=router, x_connection=x)
    with pytest.raises(ApplicationError, match="could not be confirmed"):
        await worker.extract(str(RUN_ID))
    assert x.calls == [] and router.calls == 0
    with pytest.raises(ApplicationError, match="no replacement"):
        await worker.extract(str(RUN_ID))
    assert x.calls == [] and router.calls == 0


async def test_old_account_guide_is_not_carried_into_new_account(monkeypatch):
    import tin_lite.x_style_activities as activities

    monkeypatch.setattr(activities, "model_usage_scope", lambda **kwargs: nullcontext())
    run = SimpleNamespace(
        id=RUN_ID,
        executor=x_style.KEY,
        status=SimpleNamespace(value="running"),
        project_id=UUID("a0000000-0000-0000-0000-000000000061"),
        input={},
    )
    old = "# X writing style\n\nX account ID: 99999\n\n## Explicit preferences\n\nUse caps.\n"
    db = FakeDB(run, existing_guide=old)
    router = FakeRouter()
    worker = XStyleActivities(database=db, storage=None, router=router, x_connection=FakeX())
    await worker.extract(str(RUN_ID))
    assert json.loads(router.requests[0].messages[0].content)["existing_guide"] == ""
    assert "Use caps." not in db.completed["guide"]
    assert x_style.guide_account(db.completed["guide"]) == "12345"
    assert x_style.guide_account("X account ID: 12345\nX account ID: 99999\n") is None
