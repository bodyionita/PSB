"""GET /media/{id} router tests (M9 T2, 03-api §Capture, ADR-057 §7): streams a stored media file
behind the session gate; 404 on unknown id, video (no served file), or a missing file. Fakes +
tmp files, no DB/LLM, auth bypassed."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_media_files, get_media_store, require_session
from app.routers import media as media_router
from app.services.media_store import MediaFiles

from .fakes import FakeMediaStore

PREFIX = "/api/v1"
JPEG = b"\xff\xd8\xff\xe0 fake jpeg bytes"


def _client(tmp_path):
    store = FakeMediaStore()
    files = MediaFiles(Settings(data_path=str(tmp_path)))
    app = FastAPI()
    app.include_router(media_router.router, prefix=PREFIX)
    app.dependency_overrides[get_media_store] = lambda: store
    app.dependency_overrides[get_media_files] = lambda: files
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app), store, files


def test_get_media_streams_the_file_with_its_mime(tmp_path):
    client, store, files = _client(tmp_path)
    rel = files.relative_path("captures", "photo.jpg")
    files.write(rel, JPEG)
    media = _run(
        store.create(kind="photo", source="capture", file_path=rel, mime_type="image/jpeg")
    )

    resp = client.get(f"{PREFIX}/media/{media.id}")

    assert resp.status_code == 200
    assert resp.content == JPEG
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_get_media_unknown_id_is_404(tmp_path):
    client, _store, _files = _client(tmp_path)
    resp = client.get(f"{PREFIX}/media/00000000-0000-0000-0000-0000000000ff")
    assert resp.status_code == 404


def test_get_media_malformed_id_is_422(tmp_path):
    client, _store, _files = _client(tmp_path)
    # The path type is a UUID, so a non-uuid is rejected before the handler (never a 500).
    assert client.get(f"{PREFIX}/media/not-a-uuid").status_code == 422


def test_get_media_video_row_has_no_served_file_404(tmp_path):
    # Video is summary-only (ADR-057 §2): file_path NULL → nothing to stream.
    client, store, _files = _client(tmp_path)
    media = _run(
        store.create(kind="video", source="instagram", derived_text="a summary", status="derived")
    )
    assert client.get(f"{PREFIX}/media/{media.id}").status_code == 404


def test_get_media_missing_file_on_disk_is_404(tmp_path):
    # Row points at a file that isn't there (e.g. before write / after a bad sync) → 404, not 500.
    client, store, files = _client(tmp_path)
    rel = files.relative_path("captures", "gone.jpg")
    media = _run(
        store.create(kind="photo", source="capture", file_path=rel, mime_type="image/jpeg")
    )
    assert client.get(f"{PREFIX}/media/{media.id}").status_code == 404


def _run(coro):
    import asyncio

    return asyncio.run(coro)
