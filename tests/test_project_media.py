"""Real encoded fixtures, bounded parsing and canonical binary upload recovery."""

import struct
from pathlib import Path
from uuid import uuid4

import pytest
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite.domain import SideEffectConflictError
from tin_lite.project_files import ProjectFileService, StaleProjectRevisionError
from tin_lite.project_media import validate_media

FIXTURES = Path(__file__).parent / "fixtures" / "x_media"


@pytest.mark.parametrize(
    "name,mime",
    [
        ("image.png", "image/png"),
        ("image.jpg", "image/jpeg"),
        ("demo.mp4", "video/mp4"),
    ],
)
def test_real_encoded_media(name, mime):
    raw = (FIXTURES / name).read_bytes()
    result = validate_media(f"media/{name}", raw, mime)
    assert result["width"] == 64 and result["height"] == 48
    assert result["bytes"] == len(raw)
    if name.endswith("mp4"):
        assert result["duration_seconds"] == 1
    with pytest.raises(ValueError):
        validate_media(name, raw[:-12], mime)
    with pytest.raises(ValueError):
        validate_media("../" + name, raw, mime)
    with pytest.raises(ValueError):
        validate_media(name, raw, "text/html")


def test_metadata_does_not_admit_oversized_or_unsupported_video():
    image = bytearray((FIXTURES / "image.png").read_bytes())
    image[16:24] = struct.pack(">II", 8000, 8000)
    with pytest.raises(ValueError, match="megapixels"):
        validate_media("image.png", bytes(image))
    video = (FIXTURES / "demo.mp4").read_bytes()
    with pytest.raises(ValueError, match="H.264"):
        validate_media("demo.mp4", video.replace(b"avc1", b"hvc1"))
    with pytest.raises(ValueError):
        validate_media("demo.mp4", b"\0\0\0\x01ftyp" + b"\xff" * 8)
    with pytest.raises(ValueError, match="5 MB"):
        validate_media("image.jpg", b"x" * 5_000_001)


async def test_binary_upload_replay_and_concurrent_edit_use_real_receipts(publication_db):
    db, storage = publication_db, HistoryStorage()

    async def list_commits(**kwargs):
        return {"commits": [storage.repo.commits[storage.repo.head]]}

    storage.repo.list_commits = list_commits
    storage.repo.lose_response = True
    project = await db.create_project(name="Media upload", state_repo_id=storage.repo.id)
    await db.record_tin_user("user_mediatest")
    await db.grant_project_membership(project_id=project.id, clerk_user_id="user_mediatest")
    service = ProjectFileService(database=db, storage=storage)
    original = storage.repo.head
    raw = (FIXTURES / "demo.mp4").read_bytes()
    request = dict(
        project=project,
        actor_clerk_user_id="user_mediatest",
        client_id=None,
        request_id=uuid4(),
        expected_revision=original,
        path="media/demo.mp4",
        content=raw,
        media_type="video/mp4",
    )
    first = await service.upload(**request)
    assert first == await service.upload(**request)
    assert storage.repo.writes == 1
    assert (
        await storage.read_project_media(
            repo_id=project.state_repo_id, revision=first["revision"], path="media/demo.mp4"
        )
        == raw
    )
    with pytest.raises(SideEffectConflictError, match="different"):
        await service.upload(**{**request, "path": "media/other.mp4"})
    with pytest.raises(StaleProjectRevisionError):
        await service.upload(**{**request, "request_id": uuid4()})
    assert storage.repo.writes == 1
