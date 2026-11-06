"""Films a project's coding agent makes: it uploads each version through a link get_film gives
it, Tin keeps the note and its folder in project Files, and the founder decides on each version
in Decisions. Tin runs nothing to make a film and never waits for one."""

from __future__ import annotations

import hashlib
import io
import struct
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import film_kit, film_notes
from tin_lite.code_storage import ProjectStateChangedError
from tin_lite.films import FilmError, Films, read_upload

FILM = b"""# Draw a circle, and the sheep follow

```tin-video
src: herding.assets/film.mp4
poster: herding.assets/poster.png
captions: herding.assets/captions.vtt
title: Draw a circle, and the sheep follow
```

**Before:** Herding games make you steer one dog at a time.

## What it says

- 0:00 Draw a circle round the sheep, and they follow it.
"""

DIRECTIONS = b"""# Your funnel is larger than you think

3 ways to show it, each sketched as one frame. Tin recommends Flow: its frame says the claim.

```tin-directions
recommended: 2
runner_up: 1
why: Its frame says the claim without the caption.
```

## 1. Iceberg

![Iceberg](./herding.assets/1-iceberg.jpg)

What you measure is the tip; the people who engaged sit below the line.

[The sketch's page](./herding.assets/1-iceberg.html)

## 2. Flow

![Flow](./herding.assets/2-flow.jpg)

A Sankey of where the people who engaged went.

## 3. One person

![One person](./herding.assets/3-one-person.png)

One founder's month, from the first reaction to the visit.

## How they were found

The notes are in `herding.assets/notes.json`.
"""

JPEG = b"\xff\xd8\xff\xe0" + bytes(64)
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + bytes(17)
VTT = b"WEBVTT\n\n00:00.000 --> 00:02.000\nDraw a circle.\n"


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mp4(*, seconds: float = 20.0, width: int = 1920, height: int = 1080) -> bytes:
    mvhd = _box(b"mvhd", bytes(12) + struct.pack(">II", 1000, int(seconds * 1000)) + bytes(80))
    tkhd = _box(b"tkhd", bytes(76) + struct.pack(">II", width << 16, height << 16))
    trak = _box(b"trak", tkhd + _box(b"mdia", _box(b"hdlr", bytes(8) + b"vide" + bytes(12))))
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isomiso2mp41")
    return ftyp + _box(b"moov", mvhd + trak) + _box(b"mdat", bytes(64))


def tar(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    return raw.getvalue()


def directions_upload(**extra: bytes) -> bytes:
    return tar(
        {
            "herding.md": DIRECTIONS,
            "herding.assets/1-iceberg.jpg": JPEG,
            "herding.assets/1-iceberg.html": b"<!doctype html><div class=stage></div>",
            "herding.assets/2-flow.jpg": JPEG,
            "herding.assets/3-one-person.png": PNG,
            "herding.assets/notes.json": b'{"storyboard": "# Funnel"}',
            **extra,
        }
    )


def film_upload(**extra: bytes) -> bytes:
    return tar(
        {
            "herding.md": FILM,
            "herding.assets/film.mp4": mp4(),
            "herding.assets/poster.png": PNG,
            "herding.assets/captions.vtt": VTT,
            "herding.assets/film.json": b'{"title": "Herding"}',
            **extra,
        }
    )


class FilmStorage:
    """Project Files as a head and a dict of paths; a write may meet a newer head first."""

    def __init__(self):
        self.files = {"README.md": b"Project\n", "wiki/INDEX.md": b"# Example Co\n"}
        self.head = "a" * 40
        self.commits: list[tuple[str, str]] = []
        self.moved_on = 0

    def _next(self) -> str:
        self.head = hashlib.sha1(f"{len(self.commits)}{self.head}".encode()).hexdigest()  # noqa: S324
        return self.head

    async def list_canonical_files(self, *, repo_id, branch):
        return sorted(self.files), self.head

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
    storage = FilmStorage()
    project = await db.create_project(name="Example Co", state_repo_id="projects/example")
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    settings = SimpleNamespace(
        switchboard_public_url="https://app.tin.test",
        app_url=None,
        clerk_frontend_api_url="https://clerk.test",
        private_workflow_projects=set(),
        codex_api_projects=set(),
        luna_api_key=None,
        task_queue="films-test",
    )
    runtime = SimpleNamespace(database=db, storage=storage)
    films = Films(database=db, storage=storage, settings=settings)
    return SimpleNamespace(
        db=db, storage=storage, project=project, settings=settings, runtime=runtime, films=films
    )


def token_of(link: dict) -> str:
    return link["url"].rsplit("/", 1)[-1]


# The contract, without a database.


def test_an_upload_keeps_its_note_and_the_files_it_shows():
    upload = read_upload(directions_upload(), "herding")
    assert upload.kind == "directions"
    assert upload.parsed.recommended == 2
    assert sorted(upload.files) == [
        "1-iceberg.html",
        "1-iceberg.jpg",
        "2-flow.jpg",
        "3-one-person.png",
        "notes.json",
    ]
    assert upload.dropped == []
    # A film under films/, as a tar of the project's folder, reads the same.
    film = read_upload(
        tar({f"films/{name}": content for name, content in _film_files().items()}), "herding"
    )
    assert film.kind == "film"
    assert film.parsed.video == "herding.assets/film.mp4"


def _film_files() -> dict[str, bytes]:
    return {
        "herding.md": FILM,
        "herding.assets/film.mp4": mp4(),
        "herding.assets/poster.png": PNG,
        "herding.assets/captions.vtt": VTT,
    }


def test_an_upload_says_exactly_what_to_fix():
    with pytest.raises(FilmError) as missing:
        read_upload(tar({"herding.md": DIRECTIONS, "herding.assets/2-flow.jpg": JPEG}), "herding")
    assert missing.value.problems == (
        "herding.assets/1-iceberg.jpg is missing",
        "herding.assets/1-iceberg.html is missing",
        "herding.assets/3-one-person.png is missing",
    )
    with pytest.raises(FilmError) as broken:
        read_upload(film_upload(**{"herding.assets/poster.png": b"not a png"}), "herding")
    assert broken.value.problems == ("herding.assets/poster.png: it is not a PNG image",)
    with pytest.raises(FilmError) as size:
        read_upload(
            film_upload(**{"herding.assets/film.mp4": mp4(width=1280, height=720)}), "herding"
        )
    assert size.value.problems == (
        "herding.assets/film.mp4: the film must be one of 1080x1080, 1080x1350, 1080x1920, "
        "1920x1080, not 1280x720",
    )
    with pytest.raises(FilmError, match="no herding.md"):
        read_upload(tar({"other.md": FILM}), "herding")
    with pytest.raises(FilmError, match="exactly one tin-directions block"):
        read_upload(
            tar({"herding.md": DIRECTIONS + b"\n```tin-directions\nrecommended: 1\n```\n"}),
            "herding",
        )
    with pytest.raises(FilmError, match="plays exactly one film"):
        read_upload(
            tar({"herding.md": DIRECTIONS.replace(b"```tin-directions", b"```text")}), "herding"
        )
    with pytest.raises(FilmError, match="tar.gz"):
        read_upload(b"just bytes", "herding")


def test_files_outside_the_folder_or_unkeepable_are_left_out_and_named():
    upload = read_upload(
        film_upload(
            **{
                "herding.assets/takes.mp4": mp4(),
                "herding.assets/Notes Draft.txt": b"x",
                "elsewhere/secret.md": b"x",
                "herding.assets/._film.mp4": b"resource fork",
                "herding.assets/broken.json": b"{",
            }
        ),
        "herding",
    )
    assert sorted(upload.files) == ["captions.vtt", "film.json", "film.mp4", "poster.png"]
    reasons = {item["path"]: item["reason"] for item in upload.dropped}
    assert reasons["herding.assets/takes.mp4"] == "the note plays another film"
    assert "flat, lowercase names" in reasons["herding.assets/Notes Draft.txt"]
    assert reasons["elsewhere/secret.md"] == "it is outside the note and its folder"
    assert "herding.assets/broken.json" in reasons
    assert not any("._film" in path for path in reasons)
    with_link = read_upload(film_upload() + b"", "herding")
    assert with_link.dropped == []
    linked = read_upload(tar({**_film_files()}, symlink="herding.assets/link.png"), "herding")
    assert {
        "path": "herding.assets/link.png",
        "reason": "it is not a regular file",
    } in linked.dropped


def test_a_film_plays_in_a_size_a_feed_shows():
    for width, height in film_notes.FILM_SIZES:
        assert film_notes.validate_video(mp4(width=width, height=height)).width == width
    with pytest.raises(ValueError, match="4 to 180 seconds"):
        film_notes.validate_video(mp4(seconds=2))


def test_the_kit_is_one_package_named_by_its_bytes():
    content, digest = film_kit.package()
    assert film_kit.package() == (content, digest)
    names = tarfile.open(fileobj=io.BytesIO(content), mode="r:gz").getnames()
    assert "package/package.json" in names and "package/tin-film.js" in names
    assert "package/guide/README.md" in names and "package/stage/stage.js" in names
    assert not any("/takes/" in name or name.endswith(".test.js") for name in names)
    assert not any(name.startswith(("package/films/", "package/worlds/")) for name in names)
    assert film_kit.file_name() == f"tin-film-{digest[:12]}.tgz"
    assert film_kit.guide().startswith("# Make a film for a Tin project")


# The flow, on real Postgres.


async def test_a_film_goes_from_directions_to_an_approved_cut(publication_db):
    f = await fixture(publication_db)
    first = await f.films.get(f.project, "herding", ACTOR)
    assert first["state"] == "new"
    assert first["guide"].startswith("# Make a film for a Tin project")
    assert first["read_first"] == [
        {
            "path": "wiki/INDEX.md",
            "why": "what Tin knows about the product; its Feature map names the features",
        }
    ]
    assert first["kit"] == f"npx -y https://app.tin.test/kit/{film_kit.file_name()}"
    assert first["upload"]["command"].startswith(f"{first['kit']} upload films/herding.md '")
    kept = await f.films.receive(token_of(first["upload"]), directions_upload())
    assert (kept["version"], kept["kind"]) == (1, "directions")
    assert kept["decisions"] == f"https://app.tin.test/decisions?project={f.project.id}"
    assert f.storage.files["films/herding.md"] == DIRECTIONS
    assert f.storage.files["films/herding.assets/2-flow.jpg"] == JPEG
    # The link made its version; sending it again answers with that version, not another.
    again = await f.films.receive(token_of(first["upload"]), directions_upload())
    assert again["version"] == 1 and len(f.storage.commits) == 1
    # Decisions shows it with each still by its project path.
    [waiting] = await f.films.open(f.project.id)
    assert waiting["slug"] == "herding" and waiting["revision"] == f.storage.head
    assert [item["still"] for item in waiting["directions"]["sketches"]] == [
        "films/herding.assets/1-iceberg.jpg",
        "films/herding.assets/2-flow.jpg",
        "films/herding.assets/3-one-person.png",
    ]
    assert waiting["directions"]["sketches"][0]["page"] == "films/herding.assets/1-iceberg.html"
    state = await f.films.get(f.project, "herding", ACTOR)
    assert state["state"] == "waiting_for_pick" and "Decisions" in state["next"]
    assert state["guide"].startswith("Run `npx -y")
    # The founder picks the second, with a note.
    with pytest.raises(FilmError, match="pick, others, discard"):
        await f.films.decide(f.project.id, "herding", ACTOR, version=1, decision="approve")
    with pytest.raises(FilmError, match="one of the sketches"):
        await f.films.decide(
            f.project.id, "herding", ACTOR, version=1, decision="pick", direction=7
        )
    picked = await f.films.decide(
        f.project.id, "herding", ACTOR, version=1, decision="pick", direction=2, note=" On  coal. "
    )
    assert (picked["status"], picked["picked"], picked["note"]) == ("picked", 2, "On coal.")
    # The same decision again answers with it; another one is refused.
    assert (
        await f.films.decide(
            f.project.id, "herding", ACTOR, version=1, decision="pick", direction=2
        )
    )["status"] == "picked"
    with pytest.raises(FilmError, match="no longer waiting"):
        await f.films.decide(f.project.id, "herding", ACTOR, version=1, decision="others")
    assert await f.films.open(f.project.id) == []
    build = await f.films.get(f.project, "herding", ACTOR)
    assert build["state"] == "build"
    assert build["next"].startswith("Build direction 2, Flow, with the founder's note: On coal.")
    # The film replaces the sketches in the folder; they stay in the project's history.
    cut = await f.films.receive(token_of(build["upload"]), film_upload())
    assert (cut["version"], cut["kind"]) == (2, "film")
    assert sorted(path for path in f.storage.files if path.startswith("films/")) == [
        "films/herding.assets/captions.vtt",
        "films/herding.assets/film.json",
        "films/herding.assets/film.mp4",
        "films/herding.assets/poster.png",
        "films/herding.md",
    ]
    [waiting] = await f.films.open(f.project.id)
    assert waiting["film"] == {
        "video": "films/herding.assets/film.mp4",
        "poster": "films/herding.assets/poster.png",
        "captions": "films/herding.assets/captions.vtt",
        "width": 1920,
        "height": 1080,
        "seconds": 20.0,
    }
    with pytest.raises(FilmError, match="Say what to change"):
        await f.films.decide(f.project.id, "herding", ACTOR, version=2, decision="changes")
    await f.films.decide(
        f.project.id, "herding", ACTOR, version=2, decision="changes", note="Larger captions."
    )
    revise = await f.films.get(f.project, "herding", ACTOR)
    assert revise["state"] == "revise" and "Larger captions." in revise["next"]
    third = await f.films.receive(token_of(revise["upload"]), film_upload())
    assert third["version"] == 3
    approved = await f.films.decide(f.project.id, "herding", ACTOR, version=3, decision="approve")
    assert approved["status"] == "approved"
    done = await f.films.get(f.project, "herding", ACTOR)
    assert done["state"] == "approved" and "upload" not in done
    assert [item["status"] for item in [done["latest"], *done["earlier"]]] == [
        "approved",
        "changes",
        "picked",
    ]


async def test_a_newer_upload_replaces_the_one_waiting(publication_db):
    f = await fixture(publication_db)
    one = await f.films.link(f.project.id, "herding", ACTOR)
    two = await f.films.link(f.project.id, "herding", ACTOR)
    await f.films.receive(token_of(one), directions_upload())
    # Someone else's commit lands first; the upload goes on top of it.
    f.storage.moved_on = 1
    await f.films.receive(token_of(two), directions_upload())
    rows = await f.db.pool.fetch(
        "SELECT version, status FROM film_versions WHERE project_id=$1 ORDER BY version",
        f.project.id,
    )
    assert [(row["version"], row["status"]) for row in rows] == [(1, "superseded"), (2, "waiting")]
    with pytest.raises(FilmError, match="no longer waiting"):
        await f.films.decide(f.project.id, "herding", ACTOR, version=1, decision="others")


async def test_a_link_is_the_projects_own_and_short_lived(publication_db):
    f = await fixture(publication_db)
    with pytest.raises(FilmError) as unknown:
        await f.films.receive("x" * 43, directions_upload())
    assert unknown.value.status == 404
    link = await f.films.link(f.project.id, "herding", ACTOR)
    await f.db.pool.execute(
        "UPDATE film_upload_links SET expires_at = $1", datetime.now(UTC) - timedelta(minutes=1)
    )
    with pytest.raises(FilmError) as expired:
        await f.films.receive(token_of(link), directions_upload())
    assert expired.value.status == 410
    # A refused upload keeps the link for the next try.
    fresh = await f.films.link(f.project.id, "herding", ACTOR)
    with pytest.raises(FilmError):
        await f.films.receive(token_of(fresh), tar({"herding.md": FILM}))
    assert (await f.films.receive(token_of(fresh), directions_upload()))["version"] == 1
    # Only a hash of the link is kept.
    stored = await f.db.pool.fetch("SELECT token_sha256 FROM film_upload_links")
    assert token_of(fresh) not in {row["token_sha256"] for row in stored}


async def test_the_founder_and_their_agent_share_one_film_over_http_and_mcp(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    server = mcp(f, monkeypatch)
    started = structured(
        await server.call_tool("get_film", {"project_id": str(f.project.id), "slug": "herding"})
    )
    assert started["state"] == "new"
    with pytest.raises(Exception, match="lowercase words"):
        await server.call_tool("get_film", {"project_id": str(f.project.id), "slug": "Herding!"})
    transport = httpx.ASGITransport(app=app(f))
    async with httpx.AsyncClient(transport=transport, base_url="https://app.tin.test") as client:
        uploaded = await client.put(
            started["upload"]["url"].removeprefix("https://app.tin.test"),
            content=directions_upload(),
        )
        assert uploaded.status_code == 200 and uploaded.json()["version"] == 1
        refused = await client.put(
            started["upload"]["url"].removeprefix("https://app.tin.test") + "x",
            content=directions_upload(),
        )
        assert refused.status_code == 404
        listed = await client.get(f"/api/projects/{f.project.id}/films")
        assert [item["slug"] for item in listed.json()["films"]] == ["herding"]
        versions = await client.get(f"/api/projects/{f.project.id}/films/herding")
        assert versions.json()["versions"][0]["kind"] == "directions"
        decided = await client.post(
            f"/api/projects/{f.project.id}/films/herding/versions/1/decision",
            json={"decision": "pick", "direction": 3, "note": "As one founder's month."},
        )
        assert decided.json()["status"] == "picked"
        wrong = await client.post(
            f"/api/projects/{f.project.id}/films/herding/versions/1/decision",
            json={"decision": "approve"},
        )
        assert wrong.status_code == 422 and "pick, others" in wrong.json()["detail"]
        kit = await client.get("/kit/tin-film.tgz", follow_redirects=True)
        assert kit.status_code == 200 and kit.content == film_kit.package()[0]
        assert "immutable" in kit.headers["cache-control"]
        assert (await client.get("/kit/tin-film-000000000000.tgz")).status_code == 404
    decisions = structured(
        await server.call_tool("list_open_decisions", {"project_id": str(f.project.id)})
    )
    assert decisions["films"] == []
    build = structured(
        await server.call_tool("get_film", {"project_id": str(f.project.id), "slug": "herding"})
    )
    assert build["state"] == "build" and "3, One person" in build["next"]
    await f.films.receive(token_of(build["upload"]), film_upload())
    decisions = structured(
        await server.call_tool("list_open_decisions", {"project_id": str(f.project.id)})
    )
    assert [(item["slug"], item["version"], item["kind"]) for item in decisions["films"]] == [
        ("herding", 2, "film")
    ]
    approved = structured(
        await server.call_tool(
            "decide_film",
            {
                "project_id": str(f.project.id),
                "slug": "herding",
                "version": 2,
                "decision": "approve",
            },
        )
    )
    assert approved["status"] == "approved"
    # Another member's project stays theirs.
    other = mcp(f, monkeypatch, actor="user_stranger")
    with pytest.raises(Exception, match="not_found"):
        await other.call_tool("get_film", {"project_id": str(f.project.id), "slug": "herding"})


def test_the_reader_shows_the_directions_words_not_their_block():
    from tin_lite.documents import render_markdown

    html = render_markdown(DIRECTIONS.decode(), asset_folder="films/herding.assets").html
    assert "Its frame says the claim" not in html and "recommended: 2" not in html
    assert "A Sankey of where the people who engaged went." in html
