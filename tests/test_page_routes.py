"""The founder chooses once where adapted pages live; approval and merging follow it."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_adapted_page_delivery import (
    adapted_pull_request,
    answer_page,
    approve_answer_page,
    children,
    clean,
    fixture,
)
from test_private_workflows import ACTOR, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_repository_delivery as delivery
from tin_lite.content_delivery_api import publish_preview
from tin_lite.page_routes import PATH, PageRoutes, PageRouteService, direction, matches
from tin_lite.project_files import ProjectFileCommitResult


async def routes_fixture(db, monkeypatch):
    """The adapted-page fixture, with project-file commits applied to its in-memory repo.

    ProjectFileService's own commit contract is covered by the project-file tests; here it
    only has to keep the expected revision and replay a repeated request.
    """
    f = await fixture(db, monkeypatch)
    replays = {}

    async def commit(self, *, project, request_id, expected_revision, message, changes, **_):
        if request_id in replays:
            return replace(replays[request_id], replayed=True)
        if expected_revision != f.storage.repo.head:
            raise ValueError("The project changed; read it again.")
        revision = f.storage.repo.edit(
            {item["path"]: item["content"].encode() for item in changes}, message=message
        )
        replays[request_id] = ProjectFileCommitResult(
            project_id=project.id,
            request_id=request_id,
            revision=revision,
            changed_paths=tuple(item["path"] for item in changes),
            operation="upsert",
        )
        return replays[request_id]

    monkeypatch.setattr("tin_lite.project_files.ProjectFileService.commit", commit)
    return f


@pytest.mark.parametrize(
    "route", ["/blog/{slug}", "/{slug}", "/resources/guides/{slug}", "/blog/2026/{slug}"]
)
def test_a_route_is_a_lowercase_site_path_ending_in_one_slug(route):
    assert PageRoutes.model_validate({"routes": {"answer_page": route}}).routes == {
        "answer_page": route
    }


@pytest.mark.parametrize(
    "route",
    [
        "guides/{slug}",  # not a site path
        "/Guides/{slug}",  # uppercase
        "/guides/{slug}/amp",  # {slug} must end it
        "/a/b/c/d/{slug}",  # more than three folders
        "/{slug}{slug}",
        "https://example.com/guides/{slug}",
        "/guides/",
    ],
)
def test_other_routes_are_refused(route):
    with pytest.raises(ValueError, match="ends in \\{slug\\}"):
        PageRoutes.model_validate({"routes": {"answer_page": route}})


def test_a_public_address_matches_only_the_chosen_route():
    assert matches("/guides/{slug}", "https://example.com/guides/reliable-ai-work")
    assert matches("/guides/{slug}", "/guides/reliable-ai-work/")
    assert not matches("/guides/{slug}", "https://example.com/blog/reliable-ai-work")
    assert not matches("/guides/{slug}", "https://example.com/guides/a/b")
    assert not matches("/guides/{slug}", None)
    assert "/guides/{slug}" in direction("/guides/{slug}")


async def test_the_route_is_saved_once_per_project_and_per_page_type(publication_db, monkeypatch):
    f = await routes_fixture(publication_db, monkeypatch)
    service = PageRouteService(database=f.db, storage=f.storage)
    assert (await service.read(f.project.id))["routes"] == {}
    request = uuid4()
    for _ in range(2):  # A retried request with the same id is the same commit.
        saved = await service.save(
            project_id=f.project.id,
            kind="answer_page",
            route="/guides/{slug}",
            request_id=request,
            actor=ACTOR,
        )
    assert saved["routes"] == {"answer_page": "/guides/{slug}"}
    await service.save(
        project_id=f.project.id,
        kind="article",
        route="/blog/{slug}",
        request_id=uuid4(),
        actor=ACTOR,
    )
    current = await service.read(f.project.id)
    assert current["path"] == PATH
    assert current["routes"] == {"answer_page": "/guides/{slug}", "article": "/blog/{slug}"}
    run = await answer_page(f)
    assert await service.route_for(run) == "/guides/{slug}"


async def test_until_a_route_is_chosen_the_agent_asks_the_founder(publication_db, monkeypatch):
    f = await routes_fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    preview = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    assert preview["route"] is None
    ask = preview["ask_the_founder"]
    assert ask["suggestion"] == "/blog/{slug}"
    assert ask["question"] == "Where on your site should pages that answer buyer questions go?"
    assert "never a Tin term such as answers" in ask["how_to_suggest"]
    assert ask["then"] == {
        "name": "save_page_route",
        "arguments": {"page_type": "answer_page", "route": "<the route the founder confirmed>"},
    }
    tools = mcp(f, monkeypatch)
    shown = structured(await tools.call_tool("get_run", {"run_id": str(run.id)}))
    assert shown["delivery_preview"]["ask_the_founder"]["question"] == ask["question"]

    saved = structured(
        await tools.call_tool(
            "save_page_route",
            {
                "project_id": str(f.project.id),
                "page_type": "answer_page",
                "route": "/guides/{slug}",
                "request_id": str(uuid4()),
            },
        )
    )
    assert saved["routes"] == {"answer_page": "/guides/{slug}"}
    after = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    assert after["route"] == "/guides/{slug}" and "ask_the_founder" not in after


async def test_the_agent_cannot_save_a_route_that_is_not_a_site_path(publication_db, monkeypatch):
    f = await routes_fixture(publication_db, monkeypatch)
    with pytest.raises(Exception, match="ends in"):
        await mcp(f, monkeypatch).call_tool(
            "save_page_route",
            {
                "project_id": str(f.project.id),
                "page_type": "answer_page",
                "route": "https://example.com/guides/{slug}",
                "request_id": str(uuid4()),
            },
        )
    assert (await PageRouteService(database=f.db, storage=f.storage).read(f.project.id))[
        "routes"
    ] == {}


async def test_approval_pins_the_route_and_tells_the_adaptation(publication_db, monkeypatch):
    f = await routes_fixture(publication_db, monkeypatch)
    await PageRouteService(database=f.db, storage=f.storage).save(
        project_id=f.project.id,
        kind="answer_page",
        route="/guides/{slug}",
        request_id=uuid4(),
        actor=ACTOR,
    )
    run = await answer_page(f)
    await f.delivery.choose(run=run, mode="github_commit", actor=ACTOR, adapt=True)
    run = await approve_answer_page(f, run)
    await f.activities.deliver_content_draft(str(run.id))
    child = await f.db.get_run((await children(f, run))[0]["id"])
    assert child.input["direction"] == direction("/guides/{slug}")
    source = await delivery.saved_source(f.db, child.id)
    assert source["approval"] == {
        "mode": "github_commit",
        "requested_by": ACTOR,
        "route": "/guides/{slug}",
    }


async def test_a_pull_request_that_adds_the_chosen_route_merges_when_clean(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    route_file = {"path": "src/app/guides/[slug]/page.tsx", "content": "export default 1;\n"}
    run, child = await adapted_pull_request(
        f,
        extra_files=(route_file,),
        route="https://example.com/guides/reliable-ai-work",
        chosen_route="/guides/{slug}",
    )
    integrations = f.runtime.integrations
    integrations.github_pull_request_merge_state.return_value = clean()
    integrations.github_merge_pull_request.return_value = {"merged": True, "commit": "c" * 40}
    await f.activities.deliver_content_draft(str(child.id))
    integrations.github_merge_pull_request.assert_awaited_once()
    merge = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert merge["status"] == "merged" and merge["merge_rule"] == "chosen_route"


async def test_site_code_that_misses_the_chosen_route_stays_open(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    route_file = {"path": "src/app/blog/[slug]/page.tsx", "content": "export default 1;\n"}
    _, child = await adapted_pull_request(
        f,
        extra_files=(route_file,),
        route="https://example.com/blog/reliable-ai-work",
        chosen_route="/guides/{slug}",
    )
    await f.activities.deliver_content_draft(str(child.id))
    f.runtime.integrations.github_merge_pull_request.assert_not_called()
    merge = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert merge["status"] == "left_open"
    assert "your chosen route /guides/{slug}" in merge["reason"]
