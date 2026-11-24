"""lead_phones: phone numbers for good and strong leads, from ContactOut through Delegate."""

from __future__ import annotations

from urllib.parse import unquote
from uuid import uuid4

import pytest
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import fixture
from test_people_rate import person

from tin_lite import lead_phones
from tin_lite import people_records as records
from tin_lite.delegate import DelegateError
from tin_lite.people_records import Value


class Delegate:
    """Delegate's ContactOut lookup: the numbers on file for each profile handle."""

    def __init__(self, on_file, *, refuse=None, down=False):
        self.on_file, self.refuse, self.orders, self.jobs = on_file, refuse, [], {}
        self.down = down

    async def order(self, service, task_input, *, max_usd, idempotency_key, scope=None):
        assert service == lead_phones.SERVICE and max_usd == lead_phones.MAX_USD
        if self.refuse:
            code, status = self.refuse
            raise DelegateError("refused", code=code, status=status)
        self.orders.append(task_input["linkedin_url"])
        job = str(uuid4())
        self.jobs[job] = unquote(task_input["linkedin_url"].rsplit("/", 1)[-1])
        return {"conversation_id": job, "order_id": "o", "price_usd": 0.15}

    async def result(self, conversation_id, *, wait=30):
        if self.down:
            raise DelegateError("Delegate didn't answer", code="unavailable")
        phones = self.on_file.get(self.jobs[conversation_id], [])
        return {"state": "delivered", "result": {"artifacts": {"phones": phones}}}


async def rated(f, name, fit, **values):
    record_id = await person(f, name, f"Founder at {name}")
    async with f.db.pool.acquire() as conn:
        await records.set_values(
            conn,
            f.project.id,
            record_id,
            [Value("icp_fit", fit, "r"), *(Value(k, v) for k, v in values.items())],
            set_by_kind="run",
            set_by="test",
        )
    return record_id


async def field(f, record_id, key):
    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    return people[record_id]["fields"].get(key)


async def look(f, delegate, **kwargs):
    return await lead_phones.look_up_wanted(
        f.db, delegate, f.project.id, run_id=uuid4(), seconds=60, **kwargs
    )


async def test_good_and_strong_leads_get_their_numbers_once(collection_db):
    f = await fixture(collection_db)
    strong = await rated(f, "Ada Byron", 4)
    good = await rated(f, "Bob Stone", 3)
    weak = await rated(f, "Cy Ray", 2)
    customer = await rated(f, "Dee Link", 4, customer="yes")
    delegate = Delegate({"ada-byron": ["+1 314 555 0101", "+13145550101", "(314) 555-0199"],
                         "bob-stone": []})  # fmt: skip

    counts = await look(f, delegate)
    assert sorted(delegate.orders) == [
        "https://www.linkedin.com/in/ada-byron",
        "https://www.linkedin.com/in/bob-stone",
    ]
    assert counts == {"looked_up": 2, "found": 1, "left": 0, "held": False, "later": 0}
    phone = await field(f, strong, "phone")
    # The same number twice is kept once.
    assert phone["text"] == "+1 314 555 0101, (314) 555-0199"
    assert phone["reason"].startswith("ContactOut has 2 numbers on file")
    assert await field(f, good, "phone") is None
    assert await field(f, weak, "phone") is None and await field(f, customer, "phone") is None

    # Nobody is looked up twice, numbers found or not.
    await look(f, delegate)
    assert len(delegate.orders) == 2

    # What they ordered: a profile with no numbers isn't billed.
    async with f.db.pool.acquire() as conn:
        assert await lead_phones.spent_today(conn, f.project.id) == 0.15


async def test_a_members_own_phone_is_kept(collection_db):
    f = await fixture(collection_db)
    ada = await rated(f, "Ada Byron", 4)
    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        await records.set_values(
            conn, f.project.id, ada, [Value("phone", "+44 20 7946 0000")],
            set_by_kind="member", set_by="member-1",
        )  # fmt: skip
    delegate = Delegate({"ada-byron": ["+1 314 555 0101"]})
    await look(f, delegate)
    assert delegate.orders == []
    assert (await field(f, ada, "phone"))["text"] == "+44 20 7946 0000"


async def test_the_daily_guard_holds_lookups_for_a_later_run(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await rated(f, "Ada Byron", 4)
    await rated(f, "Bob Stone", 4)
    monkeypatch.setattr(lead_phones, "PARALLEL", 1)
    monkeypatch.setattr(lead_phones, "USD_PER_DAY", lead_phones.MAX_USD)
    delegate = Delegate({"ada-byron": ["+1 314 555 0101"], "bob-stone": ["+1 314 555 0102"]})

    counts = await look(f, delegate)
    assert counts["held"] and counts["found"] == 1 and counts["left"] == 1
    assert len(delegate.orders) == 1


async def test_delegate_refusing_the_input_ends_the_lookup(collection_db):
    f = await fixture(collection_db)
    ada = await rated(f, "Ada Byron", 4)
    counts = await look(f, Delegate({}, refuse=("invalid_input", 422)))
    assert counts["looked_up"] == 1 and counts["left"] == 0
    assert await field(f, ada, "phone") is None


@pytest.mark.parametrize(
    "refuse",
    [("delegate_refused", 502), ("price_above_max", 0), ("insufficient_escrow", 402)],
)
async def test_a_refusal_that_may_change_leaves_the_lead_for_a_later_run(collection_db, refuse):
    f = await fixture(collection_db)
    await rated(f, "Ada Byron", 4)
    counts = await look(f, Delegate({}, refuse=refuse))
    assert counts["looked_up"] == 0 and counts["later"] == 1 and counts["left"] == 1
    # A refusal before any order bought nothing and drops its reservation; a 5xx may hide a
    # placed order, so its reservation keeps counting toward the day's guard.
    async with f.db.pool.acquire() as conn:
        spent = await lead_phones.spent_today(conn, f.project.id)
    assert spent == (lead_phones.MAX_USD if refuse[1] == 502 else 0)
    # Delegate answers again: the lead is looked up.
    delegate = Delegate({"ada-byron": ["+1 314 555 0101"]})
    assert (await look(f, delegate))["found"] == 1


async def test_delegate_down_never_fails_the_run_and_the_order_is_read_later(collection_db):
    f = await fixture(collection_db)
    ada = await rated(f, "Ada Byron", 4)
    delegate = Delegate({"ada-byron": ["+1 314 555 0101"]}, down=True)
    counts = await look(f, delegate)
    assert counts["later"] == 1 and counts["left"] == 1 and len(delegate.orders) == 1
    delegate.down = False
    # The same order is read, not placed again.
    assert (await look(f, delegate))["found"] == 1 and len(delegate.orders) == 1
    assert (await field(f, ada, "phone"))["text"] == "+1 314 555 0101"


async def test_any_linkedin_identity_gets_a_lookup(collection_db):
    f = await fixture(collection_db)
    accented = await rated(f, "Çağla Öz", 4)
    member = "ACoAAB" + "x" * 30
    async with f.db.pool.acquire() as conn:
        only_member = await records.upsert_person(
            conn, f.project.id, {"linkedin_member": member}, source="test"
        )
        await records.set_values(
            conn, f.project.id, only_member, [Value("icp_fit", 3, "r")],
            set_by_kind="run", set_by="test",
        )  # fmt: skip
    delegate = Delegate({"çağla-öz": ["+90 212 555 0101"], member: ["+1 314 555 0102"]})
    assert (await look(f, delegate))["found"] == 2
    assert "%C3%A7a%C4%9Fla" in " ".join(delegate.orders)
    assert (await field(f, accented, "phone"))["text"] == "+90 212 555 0101"


async def test_without_delegate_nothing_is_looked_up(collection_db):
    f = await fixture(collection_db)
    await rated(f, "Ada Byron", 4)
    counts = await lead_phones.look_up_wanted(f.db, None, f.project.id, run_id=uuid4(), seconds=60)
    assert counts["looked_up"] == 0


def test_only_phone_numbers_are_kept_from_the_answer():
    answer = {"artifacts": {"phones": ["+1 (314) 555-0101", "call me", "12", 13145550199, None]}}
    assert lead_phones.numbers(answer) == ["+1 (314) 555-0101", "13145550199"]
    assert lead_phones.numbers(None) == [] and lead_phones.numbers({"artifacts": {}}) == []
