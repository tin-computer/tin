"""People fills a company from what Tin has: a headline's "Title at Company", or a work email."""

from __future__ import annotations

import pytest
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import fixture

from tin_lite import people_records as records
from tin_lite.people_records import Value


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Software Development Manager at Audible", ("Software Development Manager", "Audible")),
        ("Founder @ Acme | ex-Google · Angel", ("Founder", "Acme")),
        ("CTO @Rygen", ("CTO", "Rygen")),
        ("Co-Owner &amp; Operator at Elevate Design &amp; Build LLC",
         ("Co-Owner & Operator", "Elevate Design & Build LLC")),
        ("Full Stack Developer at Intelligent Retinal Imaging Systems - IRIS",
         ("Full Stack Developer", "Intelligent Retinal Imaging Systems")),
        ("Head of Growth at Acme, ex-Google", ("Head of Growth", "Acme")),
        ("CEO at Initech (YC W24)", ("CEO", "Initech")),
        ("Engineer at The Best Company In The Whole Wide World", None),
        ("Building at the intersection of AI and design", None),
        ("Geologist at Retired", None),
        ("Manager at Freelance/Self Employed", None),
        ("Helping founders tell their stories so that customers finally understand at last at X",
         None),
        ("Senior Engineer", None),
        (None, None),
    ],
)  # fmt: skip
def test_a_headline_names_a_company_only_when_it_says_title_at_company(headline, expected):
    assert records.company_from_headline(headline) == expected


def test_only_a_work_email_names_a_company():
    assert records.company_from_email("jane@acme.io") == "acme.io"
    assert records.company_from_email("jane@gmail.com") is None
    assert (
        records.company_from_email("not-an-email") is None
        and records.company_from_email(None) is None
    )


async def person(f, slug, values, *, email=None, by="social.linkedin_engagers"):
    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        ids = {"linkedin_slug": slug, **({"email": email} if email else {})}
        record_id = await records.upsert_person(conn, f.project.id, ids, source="test")
        await records.set_values(
            conn, f.project.id, record_id, values, set_by_kind="run", set_by=by
        )
    return record_id


async def fields(f, record_id):
    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    return people[record_id]["fields"]


async def test_any_runs_write_fills_the_company_and_a_named_one_replaces_it(collection_db):
    f = await fixture(collection_db)
    ada = await person(f, "ada", [Value("headline", "VP Sales at Acme | ex-Google")])
    got = await fields(f, ada)
    assert got["company"]["text"] == "Acme" and got["position"]["text"] == "VP Sales"
    assert got["company"]["reason"] == "From their headline"

    # A new job's headline moves the company Tin read.
    async with f.db.pool.acquire() as conn:
        await records.set_values(
            conn, f.project.id, ada, [Value("headline", "CRO at Globex")],
            set_by_kind="run", set_by="social.linkedin_engagers",
        )  # fmt: skip
    got = await fields(f, ada)
    assert got["company"]["text"] == "Globex" and got["position"]["text"] == "CRO"

    # A source that names the company wins, and a later headline leaves it.
    async with f.db.pool.acquire() as conn:
        await records.set_values(
            conn, f.project.id, ada, [Value("company", "Globex Corporation")],
            set_by_kind="run", set_by="rb2b",
        )  # fmt: skip
        await records.set_values(
            conn, f.project.id, ada, [Value("headline", "CEO at Initech")],
            set_by_kind="run", set_by="social.linkedin_engagers",
        )  # fmt: skip
    assert (await fields(f, ada))["company"]["text"] == "Globex Corporation"


async def test_a_work_email_fills_it_when_the_headline_cant(collection_db):
    f = await fixture(collection_db)
    bob = await person(f, "bob", [Value("name", "Bob Stone")], email="bob@initech.com")
    assert (await fields(f, bob))["company"]["text"] == "initech.com"
    cy = await person(f, "cy", [Value("name", "Cy Ray")], email="cy@gmail.com")
    assert "company" not in await fields(f, cy)


async def test_a_members_company_is_never_replaced(collection_db):
    f = await fixture(collection_db)
    dee = await person(f, "dee", [Value("name", "Dee")])
    async with f.db.pool.acquire() as conn:
        await records.set_values(
            conn, f.project.id, dee, [Value("company", "Our customer")],
            set_by_kind="member", set_by="member-1",
        )  # fmt: skip
        await records.set_values(
            conn, f.project.id, dee, [Value("headline", "COO at Hooli")],
            set_by_kind="run", set_by="social.linkedin_engagers",
        )  # fmt: skip
    assert (await fields(f, dee))["company"]["text"] == "Our customer"


async def test_escaped_text_is_kept_readable(collection_db):
    f = await fixture(collection_db)
    eve = await person(f, "eve", [Value("headline", "R&amp;D Lead at Kemin &amp; Co")])
    got = await fields(f, eve)
    assert got["headline"]["text"] == "R&D Lead at Kemin & Co"
    assert got["company"]["text"] == "Kemin & Co"
