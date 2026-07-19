"""CaptureRemovalService tests (M9.7 T5, ADR-062 §R) — the general ``DELETE /captures/{id}``.

Exercised against fakes (capture store, index-delete, media store/files) with a **real** NodeWriter
over a temp store, so the file removal + entity-hub preservation (ADR-038) + media purge are all
genuinely driven — no live DB (08 testing policy). The chat one-tap remove shares the same
content-removal core (:func:`remove_content_nodes`), covered by ``test_auto_recorded``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.graph.node_writer import NodeWriter
from app.services.capture_removal import (
    CaptureRemovalService,
    CaptureRemoveDraftOpen,
    CaptureRemoveNotFound,
)
from app.services.capture_store import CaptureRecord

from .fakes import FakeCaptureStore, FakeMediaStore, FakeStoreBackup


class _FakeDeleteStore:
    """Records the store paths passed to delete_nodes (the index-row prune)."""

    def __init__(self) -> None:
        self.deleted: list[list[str]] = []

    async def delete_nodes(self, store_paths: list[str]) -> int:
        self.deleted.append(list(store_paths))
        return len(store_paths)


class _FakeMediaFiles:
    """Records the raw-file paths passed to delete_async (the media-file purge)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_async(self, relative_path: str) -> None:
        self.deleted.append(relative_path)


def _write(root: Path, rel: str, body: str = "x") -> Path:
    path = root / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _service(
    tmp_path: Path,
    *,
    captures: FakeCaptureStore,
    delete: _FakeDeleteStore,
    backup: FakeStoreBackup,
    media_store: FakeMediaStore | None = None,
    media_files: _FakeMediaFiles | None = None,
) -> CaptureRemovalService:
    settings = Settings(
        graph_store_path=str(tmp_path / "store"),
        entity_like_types=["person"],
    )
    return CaptureRemovalService(
        settings=settings,
        captures=captures,
        index_store=delete,
        node_writer=NodeWriter(str(tmp_path / "store")),
        store_backup=backup,
        media_store=media_store,
        media_files=media_files,
        vocab=None,
    )


async def _seed(
    store: FakeCaptureStore,
    cid: str,
    *,
    kind: str = "image",
    status: str = "indexed",
    node_paths: list[str],
    removed: bool = False,
) -> CaptureRecord:
    rec = await store.create(capture_id=cid, kind=kind, status=status)
    rec.node_paths = node_paths
    if removed:
        rec.removed_at = datetime(2026, 7, 18, tzinfo=UTC)
    return rec


@pytest.mark.asyncio
async def test_remove_capture_purges_nodes_media_preserves_hub_and_tombstones(tmp_path: Path):
    root = tmp_path / "store"
    mem = _write(root, "memory/2026-07-18--photo--m1.md")
    hub = _write(root, "person/andrei--p1.md")  # a minted entity hub (ADR-038 — must survive)
    store = FakeCaptureStore()
    await _seed(
        store, "cap-1", node_paths=["memory/2026-07-18--photo--m1.md", "person/andrei--p1.md"]
    )
    media_store = FakeMediaStore()
    m = await media_store.create(
        kind="photo", source="capture", capture_id="cap-1", file_path="capture/cap-1.jpg"
    )
    delete = _FakeDeleteStore()
    files = _FakeMediaFiles()
    backup = FakeStoreBackup()
    svc = _service(
        tmp_path,
        captures=store,
        delete=delete,
        backup=backup,
        media_store=media_store,
        media_files=files,
    )

    await svc.remove_capture("cap-1")

    # Content node git-rm'd; shared entity hub preserved (ADR-038).
    assert not mem.exists()
    assert hub.exists()
    # Index rows pruned for the content path only (not the hub).
    assert delete.deleted == [["memory/2026-07-18--photo--m1.md"]]
    # Media purged: raw file deleted AND the row gone ("entirely delete", ADR-062 §R).
    assert files.deleted == ["capture/cap-1.jpg"]
    assert m.id not in media_store.rows
    # Capture tombstoned (not hard-deleted) + a commit requested.
    assert store.records["cap-1"].removed_at is not None
    assert backup.reasons and "cap-1" in backup.reasons[0]


@pytest.mark.asyncio
async def test_removed_capture_disappears_from_recents(tmp_path: Path):
    store = FakeCaptureStore()
    _write(tmp_path / "store", "memory/m.md")
    await _seed(store, "cap-2", node_paths=["memory/m.md"])
    svc = _service(tmp_path, captures=store, delete=_FakeDeleteStore(), backup=FakeStoreBackup())

    assert [r.id for r in await store.list_recent(10)] == ["cap-2"]
    await svc.remove_capture("cap-2")
    assert await store.list_recent(10) == []  # tombstoned → excluded from Recents/Captures


@pytest.mark.asyncio
async def test_remove_open_draft_is_409(tmp_path: Path):
    store = FakeCaptureStore()
    await _seed(store, "draft-1", kind="composite", status="draft", node_paths=[])
    svc = _service(tmp_path, captures=store, delete=_FakeDeleteStore(), backup=FakeStoreBackup())
    with pytest.raises(CaptureRemoveDraftOpen):
        await svc.remove_capture("draft-1")
    assert store.records["draft-1"].removed_at is None  # untouched — Discard's job


@pytest.mark.asyncio
async def test_remove_unknown_capture_is_404(tmp_path: Path):
    svc = _service(
        tmp_path, captures=FakeCaptureStore(), delete=_FakeDeleteStore(), backup=FakeStoreBackup()
    )
    with pytest.raises(CaptureRemoveNotFound):
        await svc.remove_capture("nope")


@pytest.mark.asyncio
async def test_remove_already_removed_is_404(tmp_path: Path):
    store = FakeCaptureStore()
    await _seed(store, "cap-3", node_paths=["memory/m3.md"], removed=True)
    svc = _service(tmp_path, captures=store, delete=_FakeDeleteStore(), backup=FakeStoreBackup())
    with pytest.raises(CaptureRemoveNotFound):
        await svc.remove_capture("cap-3")


@pytest.mark.asyncio
async def test_remove_without_media_substrate_still_removes_and_tombstones(tmp_path: Path):
    # A text/chat pipeline wires no media substrate — the media purge is a no-op, the rest holds.
    root = tmp_path / "store"
    mem = _write(root, "memory/m4.md")
    store = FakeCaptureStore()
    await _seed(store, "cap-4", kind="text", node_paths=["memory/m4.md"])
    delete = _FakeDeleteStore()
    svc = _service(tmp_path, captures=store, delete=delete, backup=FakeStoreBackup())

    await svc.remove_capture("cap-4")

    assert not mem.exists()
    assert delete.deleted == [["memory/m4.md"]]
    assert store.records["cap-4"].removed_at is not None


@pytest.mark.asyncio
async def test_remove_self_heals_when_files_already_gone(tmp_path: Path):
    # Retry after a crash between unlink and tombstone (or a double-tap): files already gone, but
    # the DB delete is keyed to the content paths + runs unconditionally, and the tombstone lands.
    store = FakeCaptureStore()
    await _seed(store, "cap-5", node_paths=["memory/gone.md"])  # no file written on disk
    delete = _FakeDeleteStore()
    svc = _service(tmp_path, captures=store, delete=delete, backup=FakeStoreBackup())

    await svc.remove_capture("cap-5")

    assert delete.deleted == [["memory/gone.md"]]  # runs even though the file was absent
    assert store.records["cap-5"].removed_at is not None
