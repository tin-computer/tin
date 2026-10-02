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


def test_version_1_adaptive_50_sample_keeps_bands_and_excludes_unusable_posts():
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
    selection = x_style.select_own_posts_v1(pages, now=NOW)
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


def test_version_1_sampling_bounds_and_complete_text():
    with pytest.raises(ValueError, match="three timeline calls"):
        x_style.select_own_posts_v1([{"posts": []}] * 4, now=NOW)
    with pytest.raises(ValueError, match="150 returned"):
        x_style.select_own_posts_v1([{"posts": [post(i + 1, 1)] * 51} for i in range(3)], now=NOW)
    selected = x_style.select_own_posts_v1(
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
    def __init__(self, run, existing_guide="", policy_version=None):
        self.run = run
        self.existing_guide = existing_guide
        self.policy_version = policy_version
        self.model_receipt = None
        self.completed = None

    async def get_run(self, run_id, conn=None):
        assert run_id == RUN_ID
        return self.run

    async def get_effect(self, key):
        assert key.endswith(":x_style_context")
        result = {"revision": "a" * 40, "existing_guide": self.existing_guide}
        if self.policy_version is not None:
            result["policy_version"] = self.policy_version
        return SimpleNamespace(status="completed", result=result)

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


@pytest.mark.parametrize(
    "inputs", [{}, {"sample_source": "connected", "preferences": "Keep technical details."}]
)
async def test_activity_keeps_raw_api_text_out_of_receipt(monkeypatch, inputs):
    import tin_lite.x_style_activities as activities

    monkeypatch.setattr(activities, "model_usage_scope", lambda **kwargs: nullcontext())
    run = SimpleNamespace(
        id=RUN_ID,
        executor=x_style.KEY,
        status=SimpleNamespace(value="running"),
        project_id=UUID("a0000000-0000-0000-0000-000000000061"),
        input=inputs,
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


def test_explicit_sample_source_cannot_fall_back_to_connected_account():
    with pytest.raises(ValueError, match="Supply writing samples"):
        x_style.validate_inputs({"sample_source": "supplied"})
    with pytest.raises(ValueError, match="one X sample source"):
        x_style.validate_inputs({"sample_source": "connected", "supplied_samples": "An own post."})
    x_style.validate_inputs({"sample_source": "supplied", "preferences": "Use plain language."})


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


# Version 2: the account's whole own writing, and Auto learning from X and supplied writing.


def test_whole_account_sampling_keeps_replies_quotes_and_terse_posts():
    pages = [
        {
            "posts": [
                post(1, 1, "@alice @bob right, the cache was the bug", [{"type": "replied_to"}]),
                post(2, 1, "this is the part people miss https://t.co/abc", [{"type": "quoted"}]),
                post(3, 1, "Someone else's words", [{"type": "retweeted", "id": "9"}]),
                post(4, 1, "https://example.com"),
                post(5, 1, "@alice", [{"type": "replied_to"}]),
                post(6, 1, "lol no"),
                post(7, 1, "lol no"),
                *[post(10 + i, 2, f"same day note {i}") for i in range(6)],
                post(30, 400, "an old post still sounds like me"),
                post(31, 3, "word " * 600),
            ]
        }
    ]
    selection = x_style.select_own_posts(pages, now=NOW)
    texts = [item["text"] for item in selection["examples"]]
    assert "right, the cache was the bug" in texts
    assert "this is the part people miss" in texts
    assert "lol no" in texts and "an old post still sounds like me" in texts
    assert sum(text.startswith("same day note") for text in texts) == 6
    long = next(text for text in texts if text.startswith("word"))
    assert len(long) <= x_style.MAX_SAMPLE_CHARS and long.endswith("word")
    assert all(item["kind"] == "x_post" for item in selection["examples"])
    metadata = selection["metadata"]
    assert metadata["excluded"] == {
        "repost": 1,
        "duplicate": 1,
        "link_only": 1,
        "empty": 1,
        "invalid": 0,
    }
    assert metadata["selected_count"] == len(texts) == 11 and metadata["returned_count"] == 15
    assert "cache was the bug" not in json.dumps(metadata)


def test_more_usable_posts_than_fit_spread_across_the_whole_history():
    posts = [post(i, i * 3, f"Own post number {i} about the build.") for i in range(1, 121)]
    selection = x_style.select_own_posts(
        [{"posts": posts[:50]}, {"posts": posts[50:100]}, {"posts": posts[100:]}], now=NOW
    )
    dates = [item["date"] for item in selection["examples"]]
    assert len(dates) == 50
    assert max(dates) == (NOW - timedelta(days=3)).date().isoformat()
    assert min(dates) == (NOW - timedelta(days=360)).date().isoformat()
    # Bytes bind before count: long posts still spread from newest to oldest.
    long_posts = [post(i, i, f"{i} " + "x" * 900) for i in range(1, 61)]
    picked = x_style.select_own_posts([{"posts": long_posts}], now=NOW)["examples"]
    assert sum(len(item["text"].encode()) for item in picked) <= x_style.MAX_SAMPLE_BYTES
    assert picked[0]["date"] == (NOW - timedelta(days=1)).date().isoformat()
    assert picked[-1]["date"] == (NOW - timedelta(days=60)).date().isoformat()


def test_an_account_with_no_usable_posts_yields_no_samples():
    pages = [
        {
            "posts": [
                post(1, 1, "Their words", [{"type": "retweeted"}]),
                post(2, 2, "https://example.com/a https://example.com/b"),
            ]
        }
    ]
    selection = x_style.select_own_posts(pages, now=NOW)
    assert selection["examples"] == [] and selection["metadata"]["excluded_count"] == 2


def test_supplied_writing_and_posts_share_the_sample():
    posts = [
        {"id": str(i), "text": f"Short own post {i}.", "created_at": NOW - timedelta(days=i)}
        for i in range(1, 41)
    ]
    writing = x_style.writing_examples(
        "\n\n".join(f"A longer paragraph of my own writing, number {i}." for i in range(40)),
        source="style/samples/ege-writing.md",
    )
    selection = x_style.sample(posts, writing, {"returned_count": 40, "timeline_calls": 1})
    kinds = [item["kind"] for item in selection["examples"]]
    assert kinds.count("x_post") == 25 and kinds.count("writing") == 25
    assert {item["source"] for item in selection["examples"] if item["kind"] == "writing"} == {
        "style/samples/ege-writing.md"
    }
    # A share one source doesn't need goes to the other.
    few = x_style.sample(posts, writing[:5], {"returned_count": 40, "timeline_calls": 1})
    assert [item["kind"] for item in few["examples"]].count("x_post") == 40
    guide = x_style.render_guide(
        MODEL_STYLE,
        account="12345",
        sample_ids={"s1"},
        metadata=selection["metadata"],
        existing_preferences="",
        new_preferences="",
    ).decode()
    assert "Sampled 25 of 40 returned X posts" in guide
    assert "and 25 passages of supplied writing." in guide


class PagedX(FakeX):
    def __init__(self, pages, **kwargs):
        super().__init__(**kwargs)
        self.pages = pages

    async def timeline(self, connection, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


class DisconnectedX(FakeX):
    async def connection(self, project_id, capability):
        from tin_lite.integrations import IntegrationAuthorizationError

        raise IntegrationAuthorizationError("Connect or reconnect X in this project")


class AnyRouter(FakeRouter):
    async def generate(self, route, request, timeout_seconds):
        self.calls += 1
        self.requests.append(request)
        return SimpleNamespace(
            parsed=self.parsed, usage=Usage(), model="gpt-6-sol", request_id="r1"
        )


def page(first, count, cursor):
    return {
        "posts": [
            post(i, i, f"Own post {i} about the build.") for i in range(first, first + count)
        ],
        "next_cursor": cursor,
    }


async def run_extract(monkeypatch, inputs, x, policy_version=2):
    import tin_lite.x_style_activities as activities

    monkeypatch.setattr(activities, "model_usage_scope", lambda **kwargs: nullcontext())
    run = SimpleNamespace(
        id=RUN_ID,
        executor=x_style.KEY,
        status=SimpleNamespace(value="running"),
        project_id=UUID("a0000000-0000-0000-0000-000000000061"),
        input=inputs,
    )
    db = FakeDB(run, policy_version=policy_version)
    router = AnyRouter()
    worker = XStyleActivities(database=db, storage=None, router=router, x_connection=x)
    await worker.extract(str(RUN_ID))
    return db, router


async def test_connected_sampling_follows_the_next_page_without_a_date_window(monkeypatch):
    x = PagedX([page(1, 30, "abc"), page(31, 20, "def"), page(51, 10, None)])
    db, router = await run_extract(monkeypatch, {"sample_source": "connected"}, x)
    assert [call.get("pagination_token") for call in x.calls] == [None, "abc", "def"]
    assert all("start_time" not in call and "end_time" not in call for call in x.calls)
    assert db.completed["sampling"]["returned_count"] == 60
    assert db.completed["sampling"]["selected_count"] == 50
    assert router.requests[0].system == x_style.INSTRUCTIONS
    # A last page ends the fetch.
    x = PagedX([page(1, 12, None)])
    db, _ = await run_extract(monkeypatch, {"sample_source": "connected"}, x)
    assert len(x.calls) == 1 and db.completed["sampling"]["selected_count"] == 12


async def test_auto_learns_from_posts_and_supplied_writing_together(monkeypatch):
    writing = "\n\n".join(f"A longer paragraph of my own writing, number {i}." for i in range(8))
    x = PagedX([page(1, 20, None)])
    db, router = await run_extract(monkeypatch, {"supplied_samples": writing}, x)
    samples = json.loads(router.requests[0].messages[0].content)["samples"]
    assert [item["kind"] for item in samples].count("x_post") == 20
    assert [item["kind"] for item in samples].count("writing") == 8
    assert x_style.guide_account(db.completed["guide"]) == "12345"
    assert db.completed["sampling"]["post_count"] == 20
    assert db.completed["sampling"]["writing_count"] == 8


async def test_auto_without_a_connected_account_uses_what_was_supplied(monkeypatch):
    writing = "A longer paragraph of my own writing.\n\nAnother paragraph of it."
    x = DisconnectedX()
    db, router = await run_extract(monkeypatch, {"supplied_samples": writing}, x)
    samples = json.loads(router.requests[0].messages[0].content)["samples"]
    assert {item["kind"] for item in samples} == {"writing"} and x.calls == []
    assert "X account ID: unbound" in db.completed["guide"]


async def test_version_1_runs_keep_their_sampling(monkeypatch):
    # Auto with supplied samples used them alone, and the version 1 instructions.
    x = PagedX([page(1, 20, None)])
    db, router = await run_extract(
        monkeypatch,
        {"supplied_samples": "A paragraph of my own writing.\n\nAnother one of mine."},
        x,
        policy_version=None,
    )
    assert x.calls == [] and router.requests[0].system == x_style.INSTRUCTIONS_V1
    assert db.completed["sampling"]["timeline_calls"] == 0


async def test_an_account_with_nothing_usable_buys_no_model_call(monkeypatch):
    x = PagedX(
        [{"posts": [post(1, 1, "Their words", [{"type": "retweeted"}])], "next_cursor": None}]
    )
    with pytest.raises(ApplicationError, match="could not be confirmed. No usable own posts"):
        await run_extract(monkeypatch, {"sample_source": "connected"}, x)


async def test_an_extraction_failure_says_what_stopped_it(monkeypatch):
    # A run whose timeline read failed used to say only "could not be confirmed".
    from tin_lite.integrations import IntegrationUpstreamError

    class FailingX(PagedX):
        async def timeline(self, connection, **kwargs):
            raise IntegrationUpstreamError("X returned an incomplete result")

    with pytest.raises(ApplicationError) as failure:
        await run_extract(monkeypatch, {"sample_source": "connected"}, FailingX([]))
    assert "X returned an incomplete result. No replacement was purchased" in str(failure.value)
