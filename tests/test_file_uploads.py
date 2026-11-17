"""Files a member's coding agent made on their machine reach project Files through a link
prepare_file_upload gives it: bound to one folder, short-lived, acting as that member, and
checked as a dashboard upload or a committed text file is."""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_films import fixture, token_of
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.file_uploads import (
    MAX_FILES_PER_LINK,
    FileUploadError,
    FileUploads,
    _change,
    check_folder,
)

FOLDER = "social/linkedin/media/2026-11-17-company-posts"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><path d="M0 0h4v4z"/></svg>'


def png(width: int = 4, height: int = 5) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x23\x48\xff" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def uploads(f) -> FileUploads:
    return FileUploads(database=f.db, storage=f.storage, settings=f.settings)


def link_token(link: dict) -> str:
    return token_of({"url": link["url"].rstrip("/")})


# The contract, without a database.


def test_a_folder_is_an_ordinary_place_in_files():
    assert check_folder(f"/{FOLDER}/") == FOLDER
    for folder in ("", "/", ".git/hooks", "a/../b", "a//b", ".env", "keys/server.pem", "x" * 401):
        with pytest.raises(FileUploadError, match="ordinary folder"):
            check_folder(folder)


def test_a_file_is_an_image_tin_checks_or_text():
    assert _change("a/p1.png", png())["media_type"] == "image/png"
    assert _change("a/film.MP4", b"anything")["media_type"] == "video/mp4"
    assert _change("a/logo.svg", SVG)["text"].startswith("<svg")
    with pytest.raises(FileUploadError, match="neither"):
        _change("a/guide.pdf", b"%PDF-1.7\n\xe2\xe3\xcf\xd3")
    with pytest.raises(FileUploadError, match="empty"):
        _change("a/notes.md", b"")
    with pytest.raises(FileUploadError, match="at most 1 MB"):
        _change("a/notes.md", b"a" * 1_000_001)


# Through Postgres.


async def test_a_link_keeps_each_file_in_its_folder_as_the_member(publication_db):
    f = await fixture(publication_db)
    link = await uploads(f).link(f.project.id, FOLDER, ACTOR)
    assert link["folder"] == f"{FOLDER}/" and link["url"].endswith("/")
    assert link["command"] == f"curl --fail-with-body -T <file> '{link['url']}'"
    card = png()
    kept = await uploads(f).receive(link_token(link), "p1.png", card)
    assert kept["path"] == f"{FOLDER}/p1.png"
    assert (kept["media_type"], kept["width"], kept["height"]) == ("image/png", 4, 5)
    assert f.storage.files[f"{FOLDER}/p1.png"] == card
    # Text arrives byte for byte, the case MCP's text commits made error-prone.
    await uploads(f).receive(link_token(link), "art/logo.svg", SVG)
    assert f.storage.files[f"{FOLDER}/art/logo.svg"] == SVG.decode()
    # The same name replaces the file; another write landing first doesn't lose it.
    f.storage.moved_on = 1
    again = png(6, 6)
    await uploads(f).receive(link_token(link), "p1.png", again)
    assert f.storage.files[f"{FOLDER}/p1.png"] == again
    change = await f.db.pool.fetchrow(
        """SELECT actor_clerk_user_id, client_id FROM project_file_changes
           WHERE project_id = $1 AND status = 'completed' ORDER BY updated_at DESC LIMIT 1""",
        f.project.id,
    )
    assert (change["actor_clerk_user_id"], change["client_id"]) == (ACTOR, "upload-link")
    # Only a hash of the link is kept.
    stored = await f.db.pool.fetch("SELECT token_sha256 FROM file_upload_links")
    assert link_token(link) not in {row["token_sha256"] for row in stored}


async def test_a_file_is_refused_with_what_to_fix(publication_db):
    f = await fixture(publication_db)
    token = link_token(await uploads(f).link(f.project.id, FOLDER, ACTOR))
    with pytest.raises(FileUploadError, match="complete PNG"):
        await uploads(f).receive(token, "p1.png", png()[:-12])
    with pytest.raises(FileUploadError, match="ordinary name"):
        await uploads(f).receive(token, "../../wiki/INDEX.md", b"# Taken over\n")
    with pytest.raises(FileUploadError, match="ordinary name"):
        await uploads(f).receive(token, ".env", b"KEY=1\n")
    with pytest.raises(FileUploadError, match="credential"):
        await uploads(f).receive(token, "notes.md", b"key: ghp_" + b"a" * 36 + b"\n")
    assert not any(path.startswith(FOLDER) for path in f.storage.files)
    assert f.storage.files["wiki/INDEX.md"] == b"# Example Co\n"


async def test_a_link_is_the_members_own_and_short_lived(publication_db):
    f = await fixture(publication_db)
    with pytest.raises(FileUploadError) as unknown:
        await uploads(f).receive("x" * 43, "p1.png", png())
    assert unknown.value.status == 404
    token = link_token(await uploads(f).link(f.project.id, FOLDER, ACTOR))
    await f.db.pool.execute(
        "UPDATE file_upload_links SET expires_at = $1", datetime.now(UTC) - timedelta(minutes=1)
    )
    with pytest.raises(FileUploadError) as expired:
        await uploads(f).receive(token, "p1.png", png())
    assert expired.value.status == 410
    fresh = link_token(await uploads(f).link(f.project.id, FOLDER, ACTOR))
    await f.db.pool.execute(
        "UPDATE file_upload_links SET files = $1 WHERE expires_at > now()", MAX_FILES_PER_LINK
    )
    with pytest.raises(FileUploadError) as spent:
        await uploads(f).receive(fresh, "p1.png", png())
    assert spent.value.status == 429
    # A member who leaves the project takes their links with them.
    member = link_token(await uploads(f).link(f.project.id, FOLDER, ACTOR))
    await f.db.pool.execute(
        "DELETE FROM project_memberships WHERE project_id = $1 AND clerk_user_id = $2",
        f.project.id,
        ACTOR,
    )
    with pytest.raises(FileUploadError) as left:
        await uploads(f).receive(member, "p1.png", png())
    assert left.value.status == 403


async def test_the_agent_asks_over_mcp_and_sends_files_over_http(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server = mcp(f, monkeypatch)
    link = structured(
        await server.call_tool(
            "prepare_file_upload", {"project_id": str(f.project.id), "folder": FOLDER}
        )
    )
    with pytest.raises(Exception, match="ordinary folder"):
        await server.call_tool(
            "prepare_file_upload", {"project_id": str(f.project.id), "folder": ".git"}
        )
    path = link["url"].removeprefix("https://app.tin.test")
    transport = httpx.ASGITransport(app=app(f))
    async with httpx.AsyncClient(transport=transport, base_url="https://app.tin.test") as client:
        sent = await client.put(path + "p1.png", content=png())
        assert sent.status_code == 200 and sent.json()["path"] == f"{FOLDER}/p1.png"
        broken = await client.put(path + "p2.png", content=b"not a png")
        assert broken.status_code == 422 and "PNG" in broken.json()["detail"]
        wrong = await client.put(path.replace("/api/file-uploads/", "/api/file-uploads/x") + "a")
        assert wrong.status_code == 404
    # Another member's project stays theirs.
    other = mcp(f, monkeypatch, actor="user_stranger")
    with pytest.raises(Exception, match="not_found"):
        await other.call_tool(
            "prepare_file_upload", {"project_id": str(f.project.id), "folder": FOLDER}
        )
