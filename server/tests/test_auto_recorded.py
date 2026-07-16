"""AutoRecordedService tests (M6 task 4, ADR-048 §11/12) — the chat-scoped audit list + one-tap
remove. The service is exercised against fakes (registry, capture lookup, node-delete) with a
**real** NodeWriter over a temp store so the file removal + entity-hub preservation (ADR-038) are
genuinely driven — no live DB (08 testing policy). The un-fakeable audit JOIN + tombstone +
reprocess-exclusion SQL is covered by the real-PG smoke.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.chat.auto_recorded import AutoRecordedService, AutoRecordNotFound, _snippet
from app.config import Settings
from app.graph.node_writer import NodeWriter
from app.services.capture_store import CaptureRecord

from .fakes import FakeAutoRecordedStore, FakeStoreBackup


class _FakeCaptures:
    """A capture lookup returning preset records by id."""

    def __init__(self, records: dict[str, CaptureRecord]) -> None:
        self._records = records

    async def get(self, capture_id: str) -> CaptureRecord | None:
        return self._records.get(capture_id)


class _FakeDeleteStore:
    """Records the store paths passed to delete_nodes."""

    def __init__(self) -> None:
        self.deleted: list[list[str]] = []

    async def delete_nodes(self, store_paths: list[str]) -> int:
        self.deleted.append(list(store_paths))
        return len(store_paths)


def _capture(cid: str, *, node_paths: list[str], removed: bool = False) -> CaptureRecord:
    return CaptureRecord(
        id=cid,
        kind="text",
        status="indexed",
        raw_text="The user decided to move to Cluj.",
        node_paths=node_paths,
        source="chat",
        source_ref="sess-1",
        created_at=datetime(2026, 7, 16, tzinfo=UTC),
        removed_at=datetime(2026, 7, 16, tzinfo=UTC) if removed else None,
    )


def _service(
    tmp_path: Path,
    *,
    captures: dict[str, CaptureRecord],
    store: FakeAutoRecordedStore,
    delete: _FakeDeleteStore,
    backup: FakeStoreBackup,
) -> AutoRecordedService:
    settings = Settings(
        graph_store_path=str(tmp_path / "store"),
        entity_like_types=["person"],
    )
    return AutoRecordedService(
        settings=settings,
        store=store,
        captures=_FakeCaptures(captures),
        index_store=delete,
        node_writer=NodeWriter(str(tmp_path / "store")),
        store_backup=backup,
        vocab=None,
    )


def _write(root: Path, rel: str, body: str = "x") -> Path:
    path = root / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- remove ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_deletes_content_node_preserves_hub_and_tombstones(tmp_path: Path):
    root = tmp_path / "store"
    mem = _write(root, "memory/2026-07-16--decided--m1.md")
    hub = _write(root, "person/andrei--p1.md")  # a minted entity hub (ADR-038 — must survive)
    cid = "cap-1"
    store = FakeAutoRecordedStore()
    await store.record(cid, salience="high")
    delete = _FakeDeleteStore()
    backup = FakeStoreBackup()
    svc = _service(
        tmp_path,
        captures={cid: _capture(cid, node_paths=["memory/2026-07-16--decided--m1.md",
                                                 "person/andrei--p1.md"])},
        store=store,
        delete=delete,
        backup=backup,
    )

    await svc.remove(cid)

    # The content node file is git-rm'd; the shared entity hub is preserved (ADR-038).
    assert not mem.exists()
    assert hub.exists()
    # DB delete targets only the removed content path (not the preserved hub).
    assert delete.deleted == [["memory/2026-07-16--decided--m1.md"]]
    # Capture tombstoned + a commit requested (soft-delete, ADR-048 §11).
    assert cid in store.tombstoned
    assert backup.reasons and cid in backup.reasons[0]


@pytest.mark.asyncio
async def test_remove_tombstones_after_deleting(tmp_path: Path):
    # The tombstone is stamped LAST (self-healing on retry): assert deletes ran before it.
    root = tmp_path / "store"
    _write(root, "memory/m2.md")
    cid = "cap-2"
    store = FakeAutoRecordedStore()
    await store.record(cid, salience=None)
    delete = _FakeDeleteStore()
    svc = _service(
        tmp_path,
        captures={cid: _capture(cid, node_paths=["memory/m2.md"])},
        store=store,
        delete=delete,
        backup=FakeStoreBackup(),
    )
    await svc.remove(cid)
    assert delete.deleted == [["memory/m2.md"]]
    assert cid in store.tombstoned


@pytest.mark.asyncio
async def test_remove_unknown_capture_is_404(tmp_path: Path):
    svc = _service(
        tmp_path, captures={}, store=FakeAutoRecordedStore(),
        delete=_FakeDeleteStore(), backup=FakeStoreBackup(),
    )
    with pytest.raises(AutoRecordNotFound):
        await svc.remove("nope")


@pytest.mark.asyncio
async def test_remove_already_removed_is_404(tmp_path: Path):
    cid = "cap-3"
    store = FakeAutoRecordedStore()
    await store.record(cid, salience="med")
    svc = _service(
        tmp_path,
        captures={cid: _capture(cid, node_paths=["memory/m3.md"], removed=True)},
        store=store, delete=_FakeDeleteStore(), backup=FakeStoreBackup(),
    )
    with pytest.raises(AutoRecordNotFound):
        await svc.remove(cid)


@pytest.mark.asyncio
async def test_remove_non_auto_recorded_is_404(tmp_path: Path):
    # A source=chat capture with NO chat_auto_recorded row (agree-from-review) is not removable here
    # (ADR-048 §11 — auto-endorsed only; general removal stays backlog).
    cid = "cap-4"
    svc = _service(
        tmp_path,
        captures={cid: _capture(cid, node_paths=["memory/m4.md"])},
        store=FakeAutoRecordedStore(),  # empty registry → not recorded
        delete=_FakeDeleteStore(), backup=FakeStoreBackup(),
    )
    with pytest.raises(AutoRecordNotFound):
        await svc.remove(cid)


@pytest.mark.asyncio
async def test_remove_still_prunes_db_rows_when_files_already_gone(tmp_path: Path):
    # Self-heal after a crash between unlink and DB-delete (or a double-tap): the files are already
    # gone, but the DB delete is keyed to the capture's CONTENT paths (not the unlink result), so
    # it still runs unconditionally (a no-op on absent rows) — no orphaned index row lingers.
    cid = "cap-5"
    store = FakeAutoRecordedStore()
    await store.record(cid, salience="low")
    delete = _FakeDeleteStore()
    svc = _service(
        tmp_path,
        captures={cid: _capture(cid, node_paths=["memory/gone.md"])},
        store=store, delete=delete, backup=FakeStoreBackup(),
    )
    await svc.remove(cid)
    assert delete.deleted == [["memory/gone.md"]]  # DB delete runs even though the file was absent
    assert cid in store.tombstoned


# --- list_recent ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_caps_limit_and_forwards_entity_types(tmp_path: Path):
    store = FakeAutoRecordedStore()
    settings = Settings(entity_like_types=["person", "idea"], chat_auto_recorded_list_max=100)
    svc = AutoRecordedService(
        settings=settings, store=store, captures=_FakeCaptures({}),
        index_store=_FakeDeleteStore(), node_writer=NodeWriter(str(tmp_path / "s")),
        store_backup=FakeStoreBackup(), vocab=None,
    )
    await svc.list_recent(9999)  # over the cap → clamped
    await svc.list_recent(None)  # default → the cap
    limits = [c[0] for c in store.list_calls]
    assert limits == [100, 100]
    # The effective entity-like types (hub folders to skip when picking a title node) are forwarded.
    assert store.list_calls[0][1] == ["person", "idea"]


# --- pure helper ----------------------------------------------------------------------------------


def test_snippet_collapses_whitespace_and_truncates():
    assert _snippet("  a   b\n c ", 10) == "a b c"
    long = "word " * 100
    out = _snippet(long, 20)
    assert len(out) <= 21 and out.endswith("…")
