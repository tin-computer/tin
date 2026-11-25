"""The client's X List: paid pages under receipts, retweets dropped, the window respected."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from intel_client_fakes import WINDOW

from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationRateLimitedError,
)
from tin_lite.intel_fetch import x_list

MEMBER = "user_xconnector"
LIST = "2125213001850495223"


def post(pid, at, text="A post about lakehouses.", **extra):
    return {
        "id": pid,
        "url": f"https://x.com/i/web/status/{pid}",
        "text": text,
        "created_at": at,
        "author": {"id": "1", "username": "dataperson", "name": "Data Person"},
        "public_metrics": {"like_count": 3, "repost_count": 1, "impression_count": 90},
        "urls": extra.pop("urls", []),
        "media": [],
        "quoted": extra.pop("quoted", None),
        "reposted_by": extra.pop("reposted_by", None),
    }


PAGES = {
    None: {
        "posts": [
            post("30", "2026-11-24T12:00:00.000Z"),
            post("29", "2026-11-24T11:00:00.000Z", text="RT @other: copied text…"),
            post(
                "28",
                "2026-11-24T10:00:00.000Z",
                text="Worth reading",
                quoted={"text": "Our pricing changed today.", "author": {"username": "vendor"}},
                urls=[
                    {"expanded_url": "https://t.co/x", "unwound_url": "https://vendor.example/p"}
                ],
            ),
            post("27", "2026-11-24T09:00:00.000Z", reposted_by={"username": "member"}),
        ],
        "next_token": "page2",
    },
    "page2": {
        "posts": [
            post("26", "2026-11-23T14:00:00.000Z"),
            post("25", "2026-11-23T12:00:00.000Z"),  # before the window: the read stops here
        ],
        "next_token": "page3",
    },
    "page3": {"posts": [post("24", "2026-11-22T12:00:00.000Z")], "next_token": None},
}


class Receipt(SimpleNamespace):
    pass


class FakeDb:
    """Effect receipts as Database keeps them: started, completed or forgotten."""

    def __init__(self):
        self.effects: dict[str, Receipt] = {}
        self.calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def effect_lock(self, key, operation):
        yield object(), self.effects.get(key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.effects.setdefault(execution_key, Receipt(status="started", result=None))

    async def complete_effect(self, conn, *, execution_key, result):
        self.effects[execution_key] = Receipt(status="completed", result=result)

    async def discard_started_effect(self, conn, *, execution_key):
        if self.effects.get(execution_key, Receipt(status="")).status == "started":
            del self.effects[execution_key]

    async def record_integration_call(self, **kwargs):
        self.calls.append(kwargs)


class FakeX:
    def __init__(self, *, fail=None, connector=MEMBER):
        self.fail = fail or {}
        self.asked: list[str | None] = []
        self.connected = SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            provider_key="social.x",
            connected_by_clerk_user_id=connector,
        )

    async def connection(self, project_id, capability=None):
        if "connection" in self.fail:
            raise self.fail["connection"]
        return self.connected

    async def list_posts(self, project_id, member, *, list_id, max_results, pagination_token):
        self.asked.append(pagination_token)
        if pagination_token in self.fail:
            raise self.fail[pagination_token]
        return {"list_id": list_id, **PAGES[pagination_token]}


async def read(db, x, run_id, *, keep_retweets=False, member=MEMBER):
    kept: list = []

    async def put(conn, items):
        kept.extend(items)

    status = await x_list.read_list(
        db,
        x,
        run_id=run_id,
        project_id=x.connected.project_id,
        member=member,
        list_id=LIST,
        window=WINDOW,
        keep_retweets=keep_retweets,
        put=put,
    )
    return status, kept


async def test_pages_until_the_window_with_retweets_dropped_and_quotes_kept():
    db, x, run_id = FakeDb(), FakeX(), uuid4()
    status, kept = await read(db, x, run_id)
    assert x.asked == [None, "page2"]  # page 2 ends before the window, so page 3 isn't bought
    assert [item["url"].rsplit("/", 1)[-1] for item in kept] == ["30", "28", "26"]
    quote = next(item for item in kept if item["url"].endswith("/28"))
    assert "[quoting @vendor]: Our pricing changed today." in quote["text"]
    assert "[links] https://vendor.example/p" in quote["text"]
    assert quote["source"] == "@dataperson" and quote["source_type"] == "x"
    assert quote["source_url"] == f"https://x.com/i/lists/{LIST}"
    assert status["status"] == "ok" and status["pages"] == 2
    assert status["retweets_dropped"] == 2 and status["items_in_window"] == 3
    # One receipt and one provider call record per page, with the estimated supplier cost.
    assert sorted(db.effects) == [x_list.page_key(run_id, LIST, n) for n in (1, 2)]
    assert all(r.status == "completed" for r in db.effects.values())
    assert [c["response_summary"]["est_supplier_usd"] for c in db.calls] == ["0.020", "0.010"]
    assert {c["capability"] for c in db.calls} == {"x.lists.manage"}
    # integration_call_receipts takes started, completed, failed or unknown.
    assert {c["status"] for c in db.calls} == {"completed"}


async def test_retweets_kept_when_the_saved_workflow_asks():
    status, kept = await read(FakeDb(), FakeX(), uuid4(), keep_retweets=True)
    assert len(kept) == 5 and status["retweets_dropped"] == 0
    assert any(item["text"].startswith("[reposted by @member]") for item in kept)


async def test_a_retry_reuses_completed_pages_without_buying_them_again():
    db, x, run_id = FakeDb(), FakeX(), uuid4()
    await read(db, x, run_id)
    again = FakeX()
    again.connected = x.connected
    status, kept = await read(db, again, run_id)
    assert again.asked == [] and kept == []
    assert status["pages"] == 2 and status["items_in_window"] == 3


async def test_an_unconfirmed_page_is_never_bought_again():
    db, run_id = FakeDb(), uuid4()
    x = FakeX(fail={"page2": IntegrationDeliveryUnknownError("X's answer was lost")})
    status, kept = await read(db, x, run_id)
    assert status["reason"] == "x_uncertain" and status["status"] == "ok"
    assert len(kept) == 2  # page 1's posts stay
    assert db.effects[x_list.page_key(run_id, LIST, 2)].status == "started"
    retry = FakeX()
    retry.connected = x.connected
    status, _ = await read(db, retry, run_id)
    assert retry.asked == []  # page 1 reused, page 2 never asked for again
    assert status["reason"] == "x_uncertain"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (IntegrationRateLimitedError("X asked to wait"), "x_rate_limited"),
        (
            IntegrationAuthorizationError("Reconnect X to restore access"),
            "x_reauthorize",
        ),
    ],
)
async def test_a_refusal_before_anything_was_bought_forgets_the_receipt(failure, reason):
    db, run_id = FakeDb(), uuid4()
    status, kept = await read(db, FakeX(fail={None: failure}), run_id)
    assert status["reason"] == reason and kept == []
    assert status["status"] in {"failed", "skipped"}
    assert db.effects == {} and db.calls == []


async def test_another_member_is_skipped_before_x_is_asked():
    db, x = FakeDb(), FakeX(connector="user_someone_else")
    status, kept = await read(db, x, uuid4())
    assert status["status"] == "skipped" and status["reason"] == "x_member"
    assert x.asked == [] and db.effects == {} and kept == []


async def test_x_not_connected_is_skipped():
    x = FakeX(
        fail={"connection": IntegrationAuthorizationError("Connect or reconnect X in this project")}
    )
    status, _ = await read(FakeDb(), x, uuid4())
    assert status["status"] == "skipped" and status["reason"] == "x_not_connected"
