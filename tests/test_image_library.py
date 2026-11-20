"""A project's image library: illustrations, icons, photos and diagrams in project Files under
brand/illustrations/, drawn through the agent-only brand.image run with Studio's fal receipts.
fal is synthetic here: no test calls a provider or spends money."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR, app, mcp, structured
from test_private_workflows import fixture as project_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.code_storage import ProjectStateChangedError
from tin_lite.image_generation_activities import ImageGenerationActivities
from tin_lite.image_library import (
    DEFAULT_MODELS,
    INDEX_PATH,
    KEY,
    STYLE_PATH,
    WIKI_LINE,
    ImageLibrary,
    ImageLibraryError,
    build_prompt,
    check_model,
    image_problems,
    kind_style,
    png_has_alpha,
    request_body,
    search,
    slugify,
)
from tin_lite.image_text_check import StrayTextChecker, TextCheck
from tin_lite.run_service import start_workflow_run
from tin_lite.studio import StudioService
from tin_lite.studio_billing import CARD, IMAGE_CARD, card_for, price
from tin_lite.workflow_inputs import WorkflowInputError

DIAGRAM = 'graph TD\n  a["Sign up"]:::step\n  b["First report"]:::receipt\n  a --> b\n'


def png(width: int = 8, height: int = 8, *, alpha: bool = True) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    pixel = b"\x23\x48\xff\x00" if alpha else b"\x23\x48\xff"
    rows = b"".join(b"\x00" + pixel * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class LibraryStorage:
    """Project Files as a head, its files and every earlier revision; a write may meet a newer
    head first."""

    def __init__(self):
        self.files: dict[str, bytes | str] = {
            "README.md": b"Project\n",
            "wiki/INDEX.md": b"# Example Co\n",
        }
        self.head = "a" * 40
        self.history = {self.head: dict(self.files)}
        self.commits: list[tuple[str, str]] = []
        self.moved_on = 0

    def _next(self) -> str:
        self.head = hashlib.sha1(f"{len(self.history)}{self.head}".encode()).hexdigest()  # noqa: S324
        self.history[self.head] = dict(self.files)
        return self.head

    def put(self, path: str, content: bytes | str) -> None:
        self.files[path] = content
        self._next()

    def text(self, path: str) -> str:
        value = self.files[path]
        return value.decode() if isinstance(value, bytes) else value

    async def list_canonical_files(self, *, repo_id, branch):
        return sorted(self.files), self.head

    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        value = self.history[commit_sha][path]
        return value.encode() if isinstance(value, str) else value

    async def commit_project_changes(
        self, *, repo_id, branch, expected_head_sha, request_id, message, changes
    ):
        if self.moved_on:
            self.moved_on -= 1
            self._next()
            raise ProjectStateChangedError("canonical project state changed before file commit")
        assert expected_head_sha == self.head
        for change in changes:
            if change.operation == "upsert":
                self.files[change.path] = change.content
            else:
                assert change.operation == "delete" and change.path in self.files
                del self.files[change.path]
        self.commits.append((message, request_id))
        return self._next(), tuple(sorted(change.path for change in changes))


async def fixture(db):
    storage = LibraryStorage()
    project = await db.create_project(name="Example Co", state_repo_id="projects/example")
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    settings = SimpleNamespace(
        switchboard_public_url="https://app.tin.test",
        app_url=None,
        clerk_frontend_api_url="https://clerk.test",
        fal_key=SecretStr("fal-secret"),
    )
    runtime = SimpleNamespace(database=db, storage=storage)
    library = ImageLibrary(database=db, storage=storage, settings=settings)
    return SimpleNamespace(
        db=db,
        storage=storage,
        project=project,
        settings=settings,
        runtime=runtime,
        library=library,
    )


def index(f) -> dict:
    return json.loads(f.storage.text(INDEX_PATH))


# The contract, without a database.


def test_names_models_and_search_follow_the_contract():
    assert slugify("A hand turning a brass key!") == "a-hand-turning-a-brass-key"
    assert slugify("***") == "image"
    assert len(slugify("word " * 40)) <= 60
    for endpoint in DEFAULT_MODELS["illustration"].values():
        assert check_model(endpoint) == endpoint
    for bad in ("fal-ai/whisper", "https://evil.test/x", "../nano-banana", "nano-banana"):
        with pytest.raises(ImageLibraryError):
            check_model(bad)
    style = kind_style({"kinds": {"icon": {"style": "Line icons", "version": 2}}}, "icon")
    assert style["model"] == DEFAULT_MODELS["icon"]["model"] and style["model_is_default"]
    assert style["version"] == 2 and kind_style({"kinds": {}}, "photo")["style"] == ""
    library = {
        "images": [
            {"id": "icon/key", "kind": "icon", "subject": "a brass key", "tags": ["access"]},
            {
                "id": "illustration/door",
                "kind": "illustration",
                "subject": "an open door",
                "tags": ["key", "welcome"],
                "created_at": "2026-11-18",
            },
            {
                "id": "photo/old-key",
                "kind": "photo",
                "subject": "an old key",
                "tags": [],
                "status": "retired",
            },
        ]
    }
    assert [item["id"] for item in search(library, "key")] == ["icon/key", "illustration/door"]
    assert [item["id"] for item in search(library, "key", kind="icon")] == ["icon/key"]
    assert "photo/old-key" in {item["id"] for item in search(library, "key", include_retired=True)}
    assert search(library, "rocket") == []
    assert [item["id"] for item in search(library, "")][0] == "illustration/door"


def test_checks_prompts_and_request_shapes():
    assert png_has_alpha(png()) and not png_has_alpha(png(alpha=False))
    metadata = {"media_type": "image/png", "width": 8, "height": 8}
    assert (
        image_problems(metadata=metadata, content=png(), size="square", ground="transparent") == []
    )
    flat = image_problems(
        metadata=metadata, content=png(alpha=False), size="landscape", ground="transparent"
    )
    assert flat == ["the ground is not transparent", "the image is 8x8, not landscape"]
    prompt = build_prompt(
        subject="a hand turning a brass key", kind="icon", style="Flat ink", ground="transparent"
    )
    assert prompt.startswith("A single icon, centered")
    assert "House style: Flat ink" in prompt and "No text, letters" in prompt
    refs = ["data:image/png;base64,AAAA"]
    nano = request_body(
        endpoint="google/nano-banana-2.1/edit",
        prompt=prompt,
        size="wide",
        ground="transparent",
        kind="illustration",
        references=refs,
    )
    assert nano["image_urls"] == refs and nano["aspect_ratio"] == "16:9"
    gpt = request_body(
        endpoint="openai/gpt-image-2.6/flare/edit",
        prompt=prompt,
        size="square",
        ground="transparent",
        kind="icon",
        references=[],
    )
    assert gpt["background"] == "transparent" and "image_urls" not in gpt
    assert gpt["image_size"] == "square_hd"
    recraft = request_body(
        endpoint="recraft/v4/style/text-to-vector",
        prompt=prompt,
        size="portrait",
        ground="any",
        kind="icon",
        references=refs,
    )
    assert recraft["image_urls"] == refs and recraft["image_size"] == "portrait_4_3"
    assert "style" not in recraft and "num_images" not in recraft
    seedream = request_body(
        endpoint="bytedance/seedream/v5.5/edit",
        prompt=prompt,
        size="landscape",
        ground="any",
        kind="photo",
        references=refs,
    )
    assert seedream["image_size"] == "landscape_4_3" and seedream["background"] == "auto"


def test_image_usage_has_its_own_card():
    record = {
        "provider": "fal",
        "endpoint": "google/nano-banana-2.1/edit",
        "step": "image",
        "outcome": "cost_received",
        "provider_request_id": "req-1",
        "reported_cost_nanos": 39_000_000,
    }
    assert card_for(record) is IMAGE_CARD and card_for({"step": "synthesis"}) is CARD
    assert price(IMAGE_CARD, record)[1]["rate_card"] == IMAGE_CARD["id"]
    assert price(CARD, record) is None
    assert price(IMAGE_CARD, {**record, "step": "synthesis"}) is None


def test_brand_image_is_an_agent_only_creative_workflow():
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == KEY)
    definition = builtin.definition
    assert definition["agent_only"] is True and definition["executor"] == KEY
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["presentation"]["flow"]["direction"] == "TD"
    assert set(definition["input_schema"]["properties"]["kind"]["enum"]) == {
        "illustration",
        "icon",
        "photo",
    }


# Through Postgres.


async def test_the_style_is_set_per_kind_and_versioned(publication_db):
    f = await fixture(publication_db)
    empty = await f.library.style(f.project.id)
    assert empty["kinds"]["illustration"]["style"] == "" and empty["kinds"]["icon"]["version"] == 0
    with pytest.raises(ImageLibraryError, match="not in project Files"):
        await f.library.set_style(
            f.project.id,
            ACTOR,
            kind="illustration",
            references=["brand/illustrations/reference/a.png"],
        )
    with pytest.raises(ImageLibraryError, match="kind must be"):
        await f.library.set_style(f.project.id, ACTOR, kind="diagram", style="Boxes")
    with pytest.raises(ImageLibraryError, match="fal endpoint"):
        await f.library.set_style(f.project.id, ACTOR, kind="photo", model="https://x.test/y")
    f.storage.put("brand/illustrations/reference/a.png", png())
    first = await f.library.set_style(
        f.project.id,
        ACTOR,
        kind="illustration",
        style="Warm   flat ink drawings, two colours",
        references=["brand/illustrations/reference/a.png"],
        model="openai/gpt-image-2.6/flare/edit",
    )
    assert first["style"]["version"] == 1 and first["style"]["style"].startswith("Warm flat ink")
    assert not first["style"]["model_is_default"]
    second = await f.library.set_style(f.project.id, ACTOR, kind="illustration", model="")
    assert second["style"]["version"] == 2 and second["style"]["model_is_default"]
    assert second["style"]["references"] == ["brand/illustrations/reference/a.png"]
    saved = json.loads(f.storage.text(STYLE_PATH))
    assert saved["kinds"]["illustration"]["updated_by"] == ACTOR
    change = await f.db.pool.fetchrow(
        """SELECT actor_clerk_user_id FROM project_file_changes
           WHERE project_id = $1 AND status = 'completed' ORDER BY updated_at DESC LIMIT 1""",
        f.project.id,
    )
    assert change["actor_clerk_user_id"] == ACTOR


async def test_a_diagram_is_kept_as_source_with_one_wiki_line(publication_db):
    f = await fixture(publication_db)
    with pytest.raises(ImageLibraryError, match="diagram dialect"):
        await f.library.save_diagram(
            f.project.id, ACTOR, subject="Signup flow", source="flowchart LR\nA-->B", tags=[]
        )
    first = await f.library.save_diagram(
        f.project.id, ACTOR, subject="Signup flow", source=DIAGRAM, tags=["onboarding"]
    )
    image = first["image"]
    assert image["id"] == "diagram/signup-flow" and image["model"] is None
    assert f.storage.text("brand/illustrations/diagram/signup-flow.mmd") == DIAGRAM
    assert index(f)["images"][0]["id"] == "diagram/signup-flow"
    assert f.storage.text("wiki/INDEX.md").count(WIKI_LINE) == 1
    # Another moved Files first; the save takes the new head and keeps both.
    f.storage.moved_on = 1
    second = await f.library.save_diagram(
        f.project.id, ACTOR, subject="Signup flow", source=DIAGRAM, tags=[]
    )
    assert second["image"]["id"] == "diagram/signup-flow-2"
    assert [item["id"] for item in index(f)["images"]] == [
        "diagram/signup-flow-2",
        "diagram/signup-flow",
    ]
    assert f.storage.text("wiki/INDEX.md").count(WIKI_LINE) == 1
    found = await f.library.search(f.project.id, "onboarding")
    assert [item["id"] for item in found["images"]] == ["diagram/signup-flow"]


async def test_download_links_are_short_lived_and_member_bound(publication_db):
    f = await fixture(publication_db)
    await f.library.save(
        f.project,
        ACTOR,
        kind="icon",
        subject="a brass key",
        tags=["access"],
        files={"png": png()},
        details={"model": "recraft/v4/style/text-to-vector"},
    )
    got = await f.library.get(f.project.id, "icon/a-brass-key", ACTOR)
    assert got["image"]["files"] == {"png": "brand/illustrations/icon/a-brass-key.png"}
    assert "discard_until" in got
    link = got["downloads"]["png"]["url"]
    assert link.startswith("https://app.tin.test/api/file-downloads/")
    stored = await f.db.pool.fetch("SELECT token_sha256 FROM file_download_links")
    assert link.rsplit("/", 1)[-1] not in {row["token_sha256"] for row in stored}
    with pytest.raises(ImageLibraryError, match="kind and slug"):
        await f.library.get(f.project.id, "../../wiki/INDEX", ACTOR)
    transport = httpx.ASGITransport(app=app(f))
    async with httpx.AsyncClient(transport=transport, base_url="https://app.tin.test") as client:
        path = link.removeprefix("https://app.tin.test")
        response = await client.get(path)
        assert response.status_code == 200 and response.content == png()
        assert response.headers["content-disposition"].startswith("attachment")
        assert response.headers["content-security-policy"].startswith("sandbox")
        assert (await client.get("/api/file-downloads/" + "x" * 43)).status_code == 404
        await f.db.pool.execute(
            "UPDATE file_download_links SET expires_at = now() - interval '1 s'"
        )
        assert (await client.get(path)).status_code == 410
        fresh = (await f.library.get(f.project.id, "icon/a-brass-key", ACTOR))["downloads"]
        await f.db.pool.execute(
            "DELETE FROM project_memberships WHERE project_id = $1", f.project.id
        )
        assert (
            await client.get(fresh["png"]["url"].removeprefix("https://app.tin.test"))
        ).status_code == 403


async def test_a_discard_removes_within_48_hours_and_retires_after(publication_db):
    f = await fixture(publication_db)
    for subject in ("a brass key", "an open door"):
        await f.library.save(
            f.project,
            ACTOR,
            kind="illustration",
            subject=subject,
            tags=[],
            files={"png": png()},
            details={},
        )
    fresh = await f.library.discard(f.project.id, ACTOR, "illustration/a-brass-key")
    assert fresh["discarded"] and not fresh["retired"]
    assert "brand/illustrations/illustration/a-brass-key.png" not in f.storage.files
    assert "brand/illustrations/illustration/a-brass-key.json" not in f.storage.files
    assert [item["id"] for item in index(f)["images"]] == ["illustration/an-open-door"]
    # Older than the undo window: the file stays and the image is marked retired.
    sidecar_path = "brand/illustrations/illustration/an-open-door.json"
    sidecar = json.loads(f.storage.text(sidecar_path))
    sidecar["created_at"] = (datetime.now(UTC) - timedelta(hours=49)).isoformat()
    f.storage.put(sidecar_path, json.dumps(sidecar))
    old = await f.library.discard(f.project.id, ACTOR, "illustration/an-open-door")
    assert old["retired"] and not old["discarded"]
    assert "brand/illustrations/illustration/an-open-door.png" in f.storage.files
    assert json.loads(f.storage.text(sidecar_path))["status"] == "retired"
    assert (await f.library.search(f.project.id))["images"] == []
    with pytest.raises(ImageLibraryError, match="already retired"):
        await f.library.discard(f.project.id, ACTOR, "illustration/an-open-door")
    with pytest.raises(ImageLibraryError, match="not found"):
        await f.library.discard(f.project.id, ACTOR, "illustration/a-brass-key")


# A drawing run, with synthetic fal and real receipts.


def fal_transport(seen: list[httpx.Request], drawings: list[bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "fal.run":
            assert request.headers["authorization"] == "Key fal-secret"
            number = sum(1 for item in seen if item.url.host == "fal.run")
            return httpx.Response(
                200,
                json={"images": [{"url": f"https://cdn.fal.test/{number}.png"}]},
                headers={"x-fal-request-id": f"req-{number}"},
            )
        if request.url.host == "cdn.fal.test":
            number = int(request.url.path.strip("/").split(".")[0])
            return httpx.Response(
                200, content=drawings[number - 1], headers={"content-type": "image/png"}
            )
        if request.url.host == "api.fal.ai":
            request_id = request.url.params["request_id"]
            return httpx.Response(
                200,
                json={
                    "billing_events": [
                        {
                            "request_id": request_id,
                            "endpoint_id": request.url.params["endpoint_id"],
                            "cost_estimate_nano_usd": 39_000_000,
                            "cost_total": "0.039",
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(500)

    return httpx.MockTransport(handler)


async def drawing_run(f, *, subject="a hand turning a brass key", ground="transparent"):
    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await f.db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,'Image',$2,'registry/workflows','image.json',$3,'1','{}')""",
            workflow_id,
            KEY,
            "d" * 40,
        )
    run_id = uuid4()
    await f.db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,
            temporal_workflow_id,thread_id,generation,fencing_token,status,lease_active,
            started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        f.project.id,
        workflow_id,
        KEY,
        "d" * 40,
        f"{KEY}:{run_id}",
        str(run_id),
        ACTOR,
        json.dumps(
            {
                "project_id": str(f.project.id),
                "subject": subject,
                "kind": "illustration",
                "size": "square",
                "ground": ground,
                "tags": ["access"],
            }
        ),
    )
    return run_id


async def styled(f):
    f.storage.put("brand/illustrations/reference/a.png", png())
    await f.library.set_style(
        f.project.id,
        ACTOR,
        kind="illustration",
        style="Flat ink drawings",
        references=["brand/illustrations/reference/a.png"],
    )


class FakeTextChecker:
    """Answers the stray-text check from a queue; an empty queue finds no text."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.seen: list[str] = []

    async def check(self, *, content, media_type, subject):
        self.seen.append(media_type)
        return self.answers.pop(0) if self.answers else TextCheck()


def activities(f, seen, drawings, text_checker=None):
    studio = StudioService(
        settings=f.settings,
        database=f.db,
        client=httpx.AsyncClient(transport=fal_transport(seen, drawings)),
    )
    return ImageGenerationActivities(
        database=f.db,
        storage=f.storage,
        studio=studio,
        settings=f.settings,
        text_checker=text_checker or FakeTextChecker(),
    )


async def test_a_run_draws_again_once_and_saves_the_image_once(publication_db):
    f = await fixture(publication_db)
    await styled(f)
    run_id = await drawing_run(f)
    seen: list[httpx.Request] = []
    # The first drawing has no transparent ground; the second passes.
    work = activities(f, seen, [png(alpha=False), png()])
    await work.generate(str(run_id))
    calls = [item for item in seen if item.url.host == "fal.run"]
    assert len(calls) == 2
    assert calls[0].url.path == "/" + DEFAULT_MODELS["illustration"]["model"]
    body = json.loads(calls[0].content)
    assert body["image_urls"][0].startswith("data:image/png;base64,")
    assert "Flat ink drawings" in body["prompt"]
    image_path = "brand/illustrations/illustration/a-hand-turning-a-brass-key.png"
    assert f.storage.files[image_path] == png()
    sidecar = json.loads(
        f.storage.text("brand/illustrations/illustration/a-hand-turning-a-brass-key.json")
    )
    assert sidecar["checks"] == [] and sidecar["style_version"] == 1
    assert sidecar["run_id"] == str(run_id) and sidecar["author"] == ACTOR
    run = await f.db.get_run(run_id)
    assert run.status.value == "succeeded" and run.artifact_path == image_path
    receipts = {
        row["operation"]: row["count"]
        for row in await f.db.pool.fetch(
            """SELECT operation, count(*) FROM effect_receipts
               WHERE execution_key LIKE $1 GROUP BY operation""",
            f"{run_id}:%",
        )
    }
    assert receipts["studio_image"] == 2 and receipts["external_usage_v1"] == 2
    usage = await f.db.pool.fetch(
        """SELECT result FROM effect_receipts
           WHERE operation='external_usage_v1' AND execution_key LIKE $1""",
        f"{run_id}:%",
    )
    for row in usage:
        record = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
        assert record["outcome"] == "cost_received" and record["step"] == "image"
        assert "fal-secret" not in json.dumps(record)
    # A retried activity buys nothing and saves nothing again.
    await work.generate(str(run_id))
    assert len([item for item in seen if item.url.host == "fal.run"]) == 2
    assert len(index(f)["images"]) == 1


async def test_a_second_failed_check_keeps_the_paid_drawing_with_its_note(publication_db):
    f = await fixture(publication_db)
    await styled(f)
    run_id = await drawing_run(f, subject="an open door")
    seen: list[httpx.Request] = []
    await activities(f, seen, [png(alpha=False), png(alpha=False)]).generate(str(run_id))
    sidecar = json.loads(f.storage.text("brand/illustrations/illustration/an-open-door.json"))
    assert sidecar["checks"] == ["the ground is not transparent"]
    assert (await f.db.get_run(run_id)).status.value == "succeeded"


async def test_stray_text_draws_again_once(publication_db):
    f = await fixture(publication_db)
    await styled(f)
    run_id = await drawing_run(f, subject="a paper boat")
    seen: list[httpx.Request] = []
    checker = FakeTextChecker([TextCheck(problem="the image has stray text: No text")])
    await activities(f, seen, [png(), png()], checker).generate(str(run_id))
    assert len([item for item in seen if item.url.host == "fal.run"]) == 2
    assert checker.seen == ["image/png", "image/png"]
    sidecar = json.loads(f.storage.text("brand/illustrations/illustration/a-paper-boat.json"))
    assert sidecar["checks"] == []


async def test_a_drawing_that_fails_the_free_checks_is_not_read_for_text(publication_db):
    f = await fixture(publication_db)
    await styled(f)
    run_id = await drawing_run(f, subject="a kite")
    seen: list[httpx.Request] = []
    checker = FakeTextChecker([TextCheck(note="text not checked: the vision check failed")])
    await activities(f, seen, [png(alpha=False), png()], checker).generate(str(run_id))
    # Only the second drawing passed the ground check, so only it was read; its note is kept.
    assert checker.seen == ["image/png"]
    sidecar = json.loads(f.storage.text("brand/illustrations/illustration/a-kite.json"))
    assert sidecar["checks"] == ["text not checked: the vision check failed"]


class FakeAnthropic:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []
        self.messages = self

    async def create(self, **parameters):
        self.calls.append(parameters)
        if self.error:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


async def test_the_stray_text_checker_reads_rasters_and_svg_source():
    found = FakeAnthropic(json.dumps({"has_text": True, "text": "No text  present\nin image"}))
    result = await StrayTextChecker(client=found).check(
        content=png(), media_type="image/png", subject="a lamp"
    )
    assert result.problem == "the image has stray text: No text present in image"
    block = found.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    assert found.calls[0]["output_config"]["format"]["type"] == "json_schema"

    clean = FakeAnthropic(json.dumps({"has_text": False, "text": ""}))
    assert (
        await StrayTextChecker(client=clean).check(
            content=png(), media_type="image/png", subject="a lamp"
        )
        == TextCheck()
    )

    broken = FakeAnthropic(reply="not json")
    assert (
        await StrayTextChecker(client=broken).check(
            content=png(), media_type="image/png", subject="a lamp"
        )
    ).note == "text not checked: the vision check failed"

    unset = StrayTextChecker(settings=SimpleNamespace(anthropic_api_key=None))
    assert (
        await unset.check(content=png(), media_type="image/png", subject="a lamp")
    ).note == "text not checked: no vision model is configured"

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text x="1">Hi</text></svg>'
    assert (await unset.check(content=svg, media_type="image/svg+xml", subject="x")).problem
    plain = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
    assert await unset.check(content=plain, media_type="image/svg+xml", subject="x") == TextCheck()


async def test_an_unresolved_drawing_is_never_bought_again(publication_db):
    f = await fixture(publication_db)
    await styled(f)
    run_id = await drawing_run(f, subject="a lamp")
    async with f.db.effect_lock(f"{run_id}:studio-image:a1:image", "studio_provider_result_v1") as (
        conn,
        _,
    ):
        await f.db.start_effect(
            conn,
            execution_key=f"{run_id}:studio-image:a1:image",
            operation="studio_provider_result_v1",
        )
    seen: list[httpx.Request] = []
    with pytest.raises(ApplicationError, match="no repeat purchase"):
        await activities(f, seen, [png()]).generate(str(run_id))
    assert not [item for item in seen if item.url.host == "fal.run"]
    assert "brand/illustrations/illustration/a-lamp.png" not in f.storage.files


async def test_a_kind_without_a_style_is_not_drawn(publication_db):
    f = await fixture(publication_db)
    run_id = await drawing_run(f, subject="a lamp")
    seen: list[httpx.Request] = []
    with pytest.raises(ApplicationError, match="set_image_style"):
        await activities(f, seen, [png()]).generate(str(run_id))
    assert seen == []


# Through MCP and run admission.


async def registry_fixture(db):
    f = await project_fixture(db)
    f.settings.fal_key = SecretStr("fal-secret")
    await db.upsert_workflow_system(
        system_id="creative-studio", name="Creative studio", display_order=1
    )
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == KEY)
    definition = builtin.definition
    revision = "d" * 40
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=KEY,
        title=builtin.title,
        description=builtin.description,
        executor=KEY,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=revision,
        version_label=builtin.version_label,
        definition=definition,
    )
    f.workflow = await db.get_workflow(builtin.id)
    original_read = f.storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows":
            return json.dumps(definition).encode()
        return await original_read(**kwargs)

    f.storage.read_canonical_artifact = read
    return f


async def test_generate_image_starts_one_run_and_check_generation_collects_it(
    publication_db, monkeypatch
):
    f = await registry_fixture(publication_db)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    with pytest.raises(Exception, match="style is not set"):
        await server.call_tool(
            "generate_image", {"project_id": project, "subject": "a brass key", "kind": "icon"}
        )
    with pytest.raises(Exception, match="concrete subject"):
        await server.call_tool(
            "generate_image", {"project_id": project, "subject": " ", "kind": "icon"}
        )
    style = {"version": 1, "kinds": {"icon": {"style": "Line icons", "version": 1}}}
    f.storage.repo.edit({STYLE_PATH: json.dumps(style).encode()})
    request_id = str(uuid4())
    arguments = {
        "project_id": project,
        "subject": "a brass key",
        "kind": "icon",
        "tags": ["Access"],
        "request_id": request_id,
    }
    started = structured(await server.call_tool("generate_image", arguments))
    again = structured(await server.call_tool("generate_image", arguments))
    assert started["job_id"] == again["job_id"] and started["done"] is False
    run = await f.db.get_run(started["job_id"])
    assert run.executor == KEY and run.input["ground"] == "transparent"
    assert run.input["tags"] == ["access"] and run.input["size"] == "square"
    f.runtime.temporal.start_workflow.assert_awaited()
    working = structured(
        await server.call_tool(
            "check_generation", {"project_id": project, "job_id": started["job_id"]}
        )
    )
    assert working["done"] is False and working["check_again_in_seconds"] == 30
    # The run saved its image.
    sidecar = {
        "id": "icon/a-brass-key",
        "kind": "icon",
        "subject": "a brass key",
        "files": {"png": "brand/illustrations/icon/a-brass-key.png"},
        "status": "saved",
        "created_at": datetime.now(UTC).isoformat(),
    }
    f.storage.repo.edit(
        {
            "brand/illustrations/icon/a-brass-key.png": png(),
            "brand/illustrations/icon/a-brass-key.json": json.dumps(sidecar).encode(),
        }
    )
    async with f.db.effect_lock(f"{run.id}:image_save", KEY) as (conn, _):
        await f.db.start_effect(conn, execution_key=f"{run.id}:image_save", operation=KEY)
        await f.db.complete_effect(
            conn, execution_key=f"{run.id}:image_save", result={"image_id": "icon/a-brass-key"}
        )
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    done = structured(
        await server.call_tool(
            "check_generation", {"project_id": project, "job_id": started["job_id"]}
        )
    )
    assert done["done"] and done["saved"] and done["image_id"] == "icon/a-brass-key"
    assert done["downloads"]["png"]["url"].startswith("https://tin.test/api/file-downloads/")
    with pytest.raises(Exception, match="not_found"):
        await server.call_tool(
            "check_generation", {"project_id": str(uuid4()), "job_id": started["job_id"]}
        )
    style_view = structured(await server.call_tool("get_image_style", {"project_id": project}))
    assert style_view["kinds"]["icon"]["style"] == "Line icons"


async def test_brand_image_starts_only_through_generate_image(publication_db):
    f = await registry_fixture(publication_db)
    inputs = {"subject": "a brass key", "kind": "icon", "size": "square", "ground": "any"}
    with pytest.raises(WorkflowInputError, match="generate_image"):
        await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=f.workflow,
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            start_idempotency_key=f"image:{uuid4()}",
            input_payload=inputs,
        )
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
