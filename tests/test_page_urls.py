import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tin_lite import content_draft
from tin_lite import page_urls as pages
from tin_lite.content_delivery import (
    ANSWER_PAGE_WORKFLOW_ID,
    DRAFT_WORKFLOW_ID,
)
from tin_lite.content_repository_delivery import status_projection, validate_copy
from tin_lite.domain import RunStatus

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
PR = {"url": "https://github.com/owner/site/pull/7", "number": 7, "repository": "owner/site"}


def run(workflow_id=ANSWER_PAGE_WORKFLOW_ID, **extra):
    values = {
        "id": uuid4(),
        "project_id": uuid4(),
        "workflow_id": workflow_id,
        "artifact_title": "Which tools work with coding agents?",
        "review_decision": None,
        "status": RunStatus.NEEDS_INPUT,
        "canonical_commit_sha": None,
        "artifact_path": None,
        "project_workflow_id": None,
    }
    return SimpleNamespace(**{**values, **extra})


def base(**extra):
    return {
        "url": "https://example.com/which-tools-work-with-coding-agents",
        "host": "example.com",
        "title": "Which tools work with coding agents?",
        "source": "title_slug",
        "final": False,
        **extra,
    }


class FakeDB:
    def __init__(self, receipts=None, site_runs=()):
        self.receipts = dict(receipts or {})
        self.site_runs = list(site_runs)
        self.pool = self

    async def fetch(self, sql, *args):
        if "FROM effect_receipts" in sql:
            keys, operation = args
            return [
                {"execution_key": k, "result": json.dumps(v[1])}
                for k, v in self.receipts.items()
                if k in keys and v[0] == operation
            ]
        assert "FROM workflow_runs" in sql
        return [{"executor": e, "input": json.dumps(i)} for e, i in self.site_runs]

    async def execute(self, sql, key, operation, result):
        assert "ON CONFLICT (execution_key) DO UPDATE" in sql
        self.receipts[key] = (operation, json.loads(result))

    async def get_effect(self, key):
        saved = self.receipts.get(key)
        return SimpleNamespace(status="completed", result=saved[1]) if saved else None

    def saved(self, run_id):
        return self.receipts[pages.key(run_id)][1]


class FakeGitHub:
    def __init__(self, *, merged=False, names=()):
        self.merged, self.names, self.calls = merged, list(names), []

    async def github_pull_request_state(self, *, project_id, repository, number):
        self.calls.append(("pr", repository, number))
        return {
            "state": "closed" if self.merged else "open",
            "merged": self.merged,
            "merged_at": "2026-09-28T11:00:00+00:00" if self.merged else None,
        }

    async def github_markdown_names(self, *, project_id, repository, folder, ref=None):
        self.calls.append(("names", repository, folder))
        return self.names


def site(pages_by_path, *, title="Which tools work with coding agents?"):
    """A fake public fetch: paths that exist return 200 with the title, others 404."""
    calls = []

    async def fetch(url):
        calls.append(url)
        path = httpx.URL(url).path
        if pages_by_path == "*" or path in pages_by_path:
            return url, 200, "text/html", f"<html><h1>{title}</h1></html>"
        return url, 404, "text/html", "Not found"

    fetch.calls = calls
    return fetch


def test_public_route_reads_only_its_own_line():
    assert pages.public_route("Checks run.\nPublic URL: https://example.com/blog/x\n") == (
        "https://example.com/blog/x"
    )
    assert pages.public_route("- **Public route:** `/blog/x`") == "/blog/x"
    assert pages.public_route("Public URL: unknown") is None
    assert pages.public_route("Public URL: http://example.com/blog/x") is None
    assert pages.public_route("Public URL: https://example.com/x?utm=1") is None
    assert pages.public_route("The public URL will be decided later.") is None
    assert pages.public_route(None) is None


def test_page_urls_stay_on_the_site_and_refuse_unsafe_parts():
    assert pages.page_url("https://www.example.com/a/b", "example.com") == (
        "https://www.example.com/a/b"
    )
    for value in (
        "https://other.example/a",
        "https://user@example.com/a",
        "https://example.com:8443/a",
        "https://example.com/a#top",
        "https://10.0.0.1/a",
        "https://localhost/a",
        "https://example.com/a/../b",
    ):
        assert pages.page_url(value, "example.com") is None, value
    assert pages.route_url("/blog/x", "example.com") == "https://example.com/blog/x"
    assert pages.route_url("//evil.example/x", "example.com") is None
    assert pages.site_host("https://example.com/pricing") == "example.com"
    assert pages.site_host("example.com") == "example.com"
    assert pages.site_host("http://127.0.0.1") is None


def test_the_address_comes_from_the_plan_or_a_labelled_slug():
    selection = {
        "host": "example.com",
        "item": {
            "title": "Set up the CLI",
            "action": "update_page",
            "destination": "https://example.com/docs/setup",
        },
    }
    draft = run(DRAFT_WORKFLOW_ID, artifact_title=None)
    updated = pages.base_for(draft, selection=selection)
    assert updated["url"] == "https://example.com/docs/setup" and updated["final"]
    assert updated["source"] == "plan_destination"

    selection["item"] = {**selection["item"], "action": "new_page"}
    proposed = pages.base_for(draft, selection=selection)
    assert proposed["source"] == "plan_destination" and not proposed["final"]

    selection["item"] = {**selection["item"], "destination": ""}
    slug = pages.base_for(draft, selection=selection)
    assert slug == {
        "url": "https://example.com/set-up-the-cli",
        "host": "example.com",
        "title": "Set up the CLI",
        "source": "title_slug",
        "final": False,
    }
    answer = pages.base_for(run(), site="https://example.com/")
    assert answer["url"] == "https://example.com/which-tools-work-with-coding-agents"
    assert pages.base_for(run(), site=None) is None


def test_without_github_the_page_is_proposed_and_published_outside_tin():
    view = pages.present({"base": base()}, None, now=NOW)
    assert view["state"] == "proposed" and view["label"] == "Proposed URL"
    assert view["note"] == "Publishing happens outside Tin."
    assert view["published_outside_tin"] and not view["checkable"]


def test_a_delivery_route_on_the_site_is_where_the_page_will_be_published():
    delivery = {"status": "completed", "pull_request": PR, "public_route": "/learn/tools"}
    view = pages.present({"base": base()}, delivery, now=NOW)
    assert view["url"] == "https://example.com/learn/tools"
    assert view["state"] == "planned" and view["label"] == "Will be published at"
    assert view["source"] == "delivery_route" and view["checkable"]
    assert "Pull request #7 is open" in view["note"]
    # A route on another host is ignored, never shown as the page's address.
    other = pages.present(
        {"base": base()}, {**delivery, "public_route": "https://other.example/x"}, now=NOW
    )
    assert other["url"] == base()["url"] and other["label"] == "Proposed URL"


def test_merged_is_not_published_until_the_page_is_found():
    delivery = {"status": "completed", "repository": "owner/site", "pull_request": PR}
    recent = {"merged": True, "merged_at": (NOW - timedelta(minutes=5)).isoformat()}
    view = pages.present({"base": base(), "check": recent}, delivery, now=NOW)
    assert view["state"] == "merged" and view["label"] == "Proposed URL"
    assert view["note"] == "Merged into owner/site. Waiting for your site to deploy it."

    stale = {**recent, "merged_at": (NOW - timedelta(hours=2)).isoformat()}
    stale["checked_at"] = NOW.isoformat()
    view = pages.present({"base": base(), "check": stale}, delivery, now=NOW)
    assert view["note"] == "Merged into owner/site; not a page on example.com yet."

    live = {**stale, "live": True, "url": base()["url"]}
    view = pages.present({"base": base(), "check": live}, delivery, now=NOW)
    assert view["state"] == "live" and view["label"] == "Live at" and not view["checkable"]


def test_a_committed_file_without_a_route_says_so_with_its_path():
    delivery = {
        "status": "completed",
        "repository": "owner/site",
        "path": "content/answers/which-tools.md",
        "commit": {"commit": "a" * 40, "branch": "main", "url": "https://github.com/x"},
    }
    record = {
        "base": base(),
        "check": {
            "merged_at": (NOW - timedelta(hours=1)).isoformat(),
            "checked_at": NOW.isoformat(),
            "live": False,
        },
        "folder": {"folder": "content/answers", "pattern": None, "checked_at": NOW.isoformat()},
    }
    view = pages.present(record, delivery, now=NOW)
    assert view["state"] == "merged"
    assert view["note"] == (
        "Committed to owner/site main; not a page on example.com yet. The file is "
        "content/answers/which-tools.md. Tin found no page on your site that shows files "
        "from content/answers/."
    )


def test_before_approval_the_card_says_what_publish_now_will_do():
    approval = {"repository": "owner/site", "path": "content/answers/which-tools.md"}
    unchecked = pages.present({"base": base(), "approval": approval}, None, now=NOW)
    assert (
        "Tin hasn't confirmed that your site shows files from content/answers/"
        in (unchecked["note"])
    )
    assert unchecked["checkable"] and not unchecked["published_outside_tin"]

    folder = {"folder": "content/answers", "checked_at": NOW.isoformat()}
    none = pages.present(
        {"base": base(), "approval": approval, "folder": {**folder, "pattern": None}}, None
    )
    assert none["label"] == "Proposed URL"
    assert none["note"] == (
        "Tin found no page on your site that shows files from content/answers/. Approving "
        "commits content/answers/which-tools.md to owner/site as a Markdown file only."
    )
    routed = pages.present(
        {"base": base(), "approval": approval, "folder": {**folder, "pattern": "/answers/{slug}"}},
        None,
    )
    assert routed["url"] == "https://example.com/answers/which-tools"
    assert routed["label"] == "Will be published at" and routed["source"] == "folder_route"


def test_a_live_page_needs_the_same_path_a_200_and_the_title():
    url = "https://example.com/blog/x"
    title = "Which tools work with coding agents?"
    page = "<h1>Which tools work with <em>coding</em> agents?</h1>"
    assert pages.page_found(title, url, url, 200, "text/html", page)
    assert not pages.page_found(title, "https://example.com/", url, 200, "text/html", page)
    assert not pages.page_found(title, url, url, 404, "text/html", page)
    assert not pages.page_found(title, url, url, 200, "text/html", "<h1>Home</h1>")


async def test_the_public_fetch_refuses_private_addresses_and_other_sites():
    async def private(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    with pytest.raises(ValueError, match="non_public_address"):
        await pages.fetch_page("https://example.com/x", resolver=private)

    async def public(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def redirect(request):
        return httpx.Response(302, headers={"location": "https://other.example/x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(ValueError, match="outside_site_redirect"):
            await pages.fetch_page("https://example.com/x", client=client, resolver=public)


async def test_a_check_learns_the_merge_then_finds_the_page_and_waits_before_asking_again():
    answer = run(status=RunStatus.SUCCEEDED, review_decision="approved")
    db = FakeDB({pages.key(answer.id): (pages.OPERATION, {"base": base()})})
    github = FakeGitHub(merged=True)
    fetch = site({"/which-tools-work-with-coding-agents"})
    service = pages.PageUrls(database=db, integrations=github, fetch=fetch)
    delivery = {"status": "completed", "repository": "owner/site", "pull_request": PR}

    view = await service.view(answer, delivery, check=True, now=NOW)
    assert view["state"] == "live" and view["url"] == base()["url"]
    assert view["checked_at"] == NOW.isoformat()
    assert github.calls == [("pr", "owner/site", 7)] and len(fetch.calls) == 1

    # A live page is never checked again, and a recent check is not repeated.
    await service.view(answer, delivery, check=True, now=NOW + timedelta(minutes=1))
    assert github.calls == [("pr", "owner/site", 7)] and len(fetch.calls) == 1


async def test_an_open_pull_request_is_rechecked_only_after_ten_minutes():
    answer = run(status=RunStatus.SUCCEEDED, review_decision="approved")
    db = FakeDB({pages.key(answer.id): (pages.OPERATION, {"base": base()})})
    github = FakeGitHub(merged=False)
    fetch = site(set())
    service = pages.PageUrls(database=db, integrations=github, fetch=fetch)
    delivery = {"status": "completed", "repository": "owner/site", "pull_request": PR}

    view = await service.view(answer, delivery, check=True, now=NOW)
    assert view["state"] == "proposed" and not fetch.calls
    await service.view(answer, delivery, check=True, now=NOW + timedelta(minutes=5))
    assert len(github.calls) == 1
    await service.view(answer, delivery, check=True, now=NOW + timedelta(minutes=11))
    assert len(github.calls) == 2


async def test_a_commit_to_a_folder_no_page_shows_is_reported_after_the_deploy_window():
    answer = run(status=RunStatus.SUCCEEDED, review_decision="approved")
    db = FakeDB({pages.key(answer.id): (pages.OPERATION, {"base": base()})})
    github = FakeGitHub(names=["which-tools.md", "older-answer.md", "another.md"])
    fetch = site(set())
    service = pages.PageUrls(database=db, integrations=github, fetch=fetch)
    delivery = {
        "status": "completed",
        "repository": "owner/site",
        "path": "content/answers/which-tools.md",
        "commit": {"commit": "b" * 40, "branch": "main", "url": "https://github.com/x"},
    }
    view = await service.view(answer, delivery, check=True, now=NOW)
    assert view["state"] == "merged" and "Waiting for your site to deploy it" in view["note"]
    saved = db.saved(answer.id)
    assert saved["folder"]["folder"] == "content/answers" and saved["folder"]["pattern"] is None
    # Siblings were tried under three routes, and the committed file itself was never a sibling.
    assert not any("which-tools." in url for url in fetch.calls[:-1])
    assert "https://example.com/answers/older-answer" in fetch.calls

    later = await service.view(answer, delivery, check=True, now=NOW + timedelta(hours=1))
    assert later["note"].startswith("Committed to owner/site main; not a page on example.com")
    assert "The file is content/answers/which-tools.md." in later["note"]


async def test_a_folder_route_counts_only_when_a_made_up_slug_fails():
    answer = run()
    approval_path = "content/answers/which-tools.md"

    async def probe(fetch):
        db = FakeDB({pages.key(answer.id): (pages.OPERATION, {"base": base()})})
        service = pages.PageUrls(
            database=db, integrations=FakeGitHub(names=["older-answer.md"]), fetch=fetch
        )
        return await service._folder_route(answer, "owner/site", approval_path, base(), "t")

    found = await probe(site({"/answers/older-answer"}))
    assert found["pattern"] == "/answers/{slug}"
    everything = await probe(site("*"))
    assert everything["pattern"] is None and everything["reason"] == "no_route"


async def test_card_polling_reads_saved_projections_and_never_calls_providers():
    answer, draft = run(), run(DRAFT_WORKFLOW_ID, artifact_title=None)
    selection = {"host": "example.com", "item": {"title": "Set up the CLI", "destination": ""}}
    db = FakeDB(
        {
            pages.key(answer.id): (pages.OPERATION, {"base": base()}),
            content_draft.selection_key(draft.id): ("content_draft_selection_v1", selection),
        }
    )
    service = pages.PageUrls(database=db)
    views = await service.views([answer, draft, run(uuid4())], {}, now=NOW)
    assert views[answer.id]["url"] == base()["url"]
    assert views[draft.id]["url"] == "https://example.com/set-up-the-cli"
    assert len(views) == 2


async def test_the_first_view_saves_the_address_from_the_project_site():
    answer = run()
    db = FakeDB(site_runs=[("visibility.audit", {"target": "example.com"})])
    view = await pages.PageUrls(database=db).view(answer, None, now=NOW)
    assert view["url"] == base()["url"]
    assert db.saved(answer.id)["base"]["source"] == "title_slug"


def test_the_adaptation_records_its_public_route_with_the_copy_proof():
    article = "# Title\n\nBody.\n"
    import hashlib

    source = {
        "article": article,
        "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
        "binding": {"repository": "owner/site", "head_sha": "c" * 40},
        "source_run_id": str(uuid4()),
    }
    manifest = {
        "repository": "owner/site",
        "head_sha": "c" * 40,
        "body": "Adds the article.\n\nPublic URL: https://example.com/blog/title\n",
        "files": [{"path": "content/blog/title.md", "content": article}],
    }
    proof = validate_copy(manifest, source)
    assert proof["public_route"] == "https://example.com/blog/title"
    assert "public_route" not in validate_copy({**manifest, "body": "No route."}, source)

    delivered = status_projection(
        SimpleNamespace(id=uuid4(), status=RunStatus.SUCCEEDED, error_message=None),
        source,
        {"external_url": PR["url"], "pull_request_number": 7, **proof},
        {},
    )
    assert delivered["public_route"] == "https://example.com/blog/title"
