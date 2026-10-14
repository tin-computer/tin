"""The target-to-page table a content plan opens with (content-editorial-v10). Offline."""

from tin_lite import content_plan_targets as targets


def keyword(kid, text, volume=0, impressions=None):
    return {
        "source_id": f"keyword:{kid}",
        "data": {
            "id": kid,
            "keyword": text,
            "observations": [{"search_volume": volume, "impressions": impressions}],
        },
    }


RESEARCH = {
    "rows": [
        keyword("k1", "sms api", 8000),
        keyword("k2", "sms api for ai agents", 70),
        keyword("k3", "imessage api", 800, impressions=1500),
        {
            "source_id": "group:g1",
            "data": {
                "id": "g1",
                "title": "SMS API discovery",
                "keyword_ids": ["k1", "k2"],
                "primary_keyword_id": "k1",
                "intent": "commercial",
            },
        },
        {
            "source_id": "group:g2",
            "data": {"id": "g2", "title": "iMessage API", "keyword_ids": ["k3"]},
        },
        {
            "source_id": "refresh:r1",
            "data": {
                "path": "/blog/imessage-on-windows",
                "metrics": {"impressions": 32795, "clicks": 207, "position": 6.57},
                "searches": [{"query": "imessage on windows"}],
            },
        },
        {
            "source_id": "efficacy:e1",
            "data": {"decision": "refresh", "path": "/blog/imessage-on-windows"},
        },
        {"source_id": "efficacy:e2", "data": {"decision": "keep", "path": "/pricing"}},
    ]
}


def item(iid, title, **fields):
    return {
        "id": iid,
        "title": title,
        "brief": fields.pop("brief", "Reader and angle."),
        "intent": "Choose",
        "action": "new_page",
        "destination": "",
        "source_ids": [],
        **fields,
    }


PLAN = {
    "batches": [
        {
            "id": "week_01",
            "due_date": "2026-10-08",
            "items": [
                item(
                    "op001",
                    "iMessage on Windows",
                    action="update_page",
                    destination="https://example.com/blog/imessage-on-windows/",
                    format="refresh",
                ),
                item("op002", "SMS API for agents", target_query="SMS API for AI agents"),
            ],
        },
        {
            "id": "week_02",
            "due_date": "2026-10-15",
            "items": [
                item("op003", "Acme vs Linear", format="comparison"),
                item(
                    "op004",
                    "How should an agent confirm a reply?",
                    brief="Answers how an agent proves a buyer's reply arrived.",
                ),
            ],
        },
        {"id": "week_03", "due_date": "2026-10-22", "items": [item("op005", "A bet page")]},
    ]
}

QUESTIONS = [
    {"question": "How does an agent prove the buyer's reply arrived?", "job": "verify"},
    {"question": "Which accounting software handles payroll for nonprofits?", "job": "other"},
]
COMPETITORS = [
    {"name": "Linear", "host": "linear.app", "use": "comparison"},
    {"name": "Blooio", "use": "none"},
    {"name": "Twilio", "host": "twilio.com", "use": "comparison"},
]


def mapping():
    return targets.targets(
        {"research": RESEARCH}, PLAN, questions=QUESTIONS, competitors=COMPETITORS
    )


def test_every_target_names_the_pages_that_serve_it():
    rows = {row["id"]: row for row in mapping()["targets"]}
    # The group is served through its keyword's exact search; its demand sums its keywords.
    sms = rows["group:g1"]
    assert [e["item_id"] for e in sms["items"]] == ["op002"] and sms["volume"] == 8070
    assert sms["search"] == "sms api"
    assert rows["group:g2"]["items"] == [] and rows["group:g2"]["impressions"] == 1500
    # The refresh row and its Page decision are one page target, served by the item changing it.
    page = rows["/blog/imessage-on-windows"]
    assert page["decision"] == "refresh" and page["impressions"] == 32795
    assert [e["item_id"] for e in page["items"]] == ["op001"]
    assert "/pricing" not in rows  # a kept page is not a target
    assert [e["item_id"] for e in rows["competitor:linear"]["items"]] == ["op003"]
    assert rows["competitor:twilio"]["items"] == []
    assert "competitor:blooio" not in rows  # the planner chose not to write about it
    assert [e["item_id"] for e in rows["question:1"]["items"]] == ["op004"]
    assert rows["question:2"]["items"] == []


def test_targets_without_a_page_and_pages_without_a_target_are_both_listed():
    result = mapping()
    assert result["unserved"] == ["group:g2", "competitor:twilio", "question:2"]
    assert [e["item_id"] for e in result["untargeted"]] == ["op005"]
    lines = "\n".join(targets.markdown_lines(result))
    assert lines.startswith(
        "## Targets and pages\n\n4 of 7 targets have a planned page; 1 planned page serves"
    )
    assert "| Target | Kind | Demand | Planned pages |" in lines
    assert "**No page planned**" in lines and "### Planned pages with no target" in lines
    assert "week 03: A bet page" in lines
    line = targets.review_line(result, 5)
    assert line.startswith(
        "5 planned pages. 4 of 7 targets have a page, 3 have none; 1 page serves"
    )
    assert line.endswith("nothing is drafted until you do.")


def test_an_empty_or_unusable_research_still_renders_honestly():
    # A plausible but unusable source: no rows, a group naming keywords that are not there.
    empty = targets.targets({"research": {}}, {"batches": []})
    assert empty == {"targets": [], "omitted": 0, "unserved": [], "untargeted": []}
    assert targets.markdown_lines(empty) == []
    broken = targets.targets(
        {"research": {"rows": [{"source_id": "group:x", "data": {"keyword_ids": ["nope"]}}]}},
        PLAN,
    )
    assert broken["targets"][0]["label"] == "group:x" and broken["targets"][0]["volume"] == 0
    assert len(broken["untargeted"]) == 5
