"""M9.6 T1 — composite draft lifecycle tests (ADR-061 §3/§9).

Exercises the real :class:`CapturePipeline` draft surface against the in-memory fakes (no DB, no
LLM): open/resume, part attach + ordinal, <=1-voice enforcement, part removal, text-body edit,
submit gating (>=1 part), discard, the 7-day GC, and that the boot orphan-sweep skips drafts.
Reuses ``_make_pipeline`` from the pipeline suite for identical wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.capture_pipeline import (
    DraftNotOpen,
    EmptyDraft,
    VoicePartLimit,
)
from app.services.capture_store import DRAFT, INDEXED, KIND_COMPOSITE, RECEIVED
from app.services.media_store import KIND_PHOTO, KIND_VOICE

from .test_capture_pipeline import _make_pipeline

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
M4A = b"ID3" + b"0" * 32


async def test_open_draft_is_idempotent_one_active(tmp_path: Path):
    pipeline, store, *_ = _make_pipeline(tmp_path)
    first = await pipeline.open_or_resume_draft()
    assert first.kind == KIND_COMPOSITE
    assert first.status == DRAFT
    assert first.source == "web"  # composite has no single modality (ADR-061 §2)
    # A second open resumes the same draft — one active draft (ADR-061 §3).
    second = await pipeline.open_or_resume_draft()
    assert second.id == first.id
    assert len([r for r in store.records.values() if r.status == DRAFT]) == 1


async def test_add_parts_get_sequential_ordinals(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    a = await pipeline.add_draft_part(draft.id, PNG, filename="a.png", kind=KIND_PHOTO)
    b = await pipeline.add_draft_part(draft.id, PNG, filename="b.png", kind=KIND_PHOTO)
    assert a.part_ordinal == 0
    assert b.part_ordinal == 1
    assert a.status == "pending"  # derivation deferred to Submit (ADR-061 §4)
    parts = await pipeline.draft_parts(draft.id)
    assert [p.id for p in parts] == [a.id, b.id]


async def test_second_voice_part_is_refused(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    await pipeline.add_draft_part(draft.id, M4A, filename="v.m4a", kind=KIND_VOICE)
    # A photo is still fine alongside the one voice.
    await pipeline.add_draft_part(draft.id, PNG, filename="p.png", kind=KIND_PHOTO)
    with pytest.raises(VoicePartLimit):
        await pipeline.add_draft_part(draft.id, M4A, filename="v2.m4a", kind=KIND_VOICE)


async def test_remove_part_hard_deletes_row_and_file(tmp_path: Path):
    pipeline, _store, _backup, _runs, _root = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    a = await pipeline.add_draft_part(draft.id, PNG, filename="a.png", kind=KIND_PHOTO)
    b = await pipeline.add_draft_part(draft.id, PNG, filename="b.png", kind=KIND_PHOTO)
    media_files = pipeline._media_files
    assert media_files.absolute(a.file_path).exists()

    await pipeline.remove_draft_part(draft.id, a.id)
    assert not media_files.absolute(a.file_path).exists()
    remaining = await pipeline.draft_parts(draft.id)
    # Ordinals are NOT renumbered — assembly tolerates gaps (ADR-061 §6).
    assert [p.id for p in remaining] == [b.id]
    assert remaining[0].part_ordinal == 1


async def test_remove_foreign_part_is_404(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    from app.services.capture_pipeline import CaptureNotFound

    draft = await pipeline.open_or_resume_draft()
    with pytest.raises(CaptureNotFound):
        await pipeline.remove_draft_part(draft.id, "00000000-0000-0000-0000-000000000999")


async def test_edit_text_body(tmp_path: Path):
    pipeline, store, *_ = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    await pipeline.set_draft_text(draft.id, "the caption that frames both")
    assert store.records[draft.id].text_body == "the caption that frames both"


async def test_submit_empty_draft_is_refused(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    with pytest.raises(EmptyDraft):
        await pipeline.submit_draft(draft.id)


async def test_submit_text_only_organizes(tmp_path: Path):
    pipeline, store, backup, _runs, root = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    await pipeline.set_draft_text(draft.id, "I had a calm, productive day.")
    await pipeline.submit_draft(draft.id)
    await pipeline.drain()

    rec = store.records[draft.id]
    assert rec.status == INDEXED
    assert rec.raw_text == "I had a calm, productive day."  # assembled = text body only
    assert rec.node_paths and (root / rec.node_paths[0]).exists()
    node_text = (root / rec.node_paths[0]).read_text(encoding="utf-8")
    assert "source: web" in node_text  # composite node source (ADR-061 §2)


async def test_submit_with_photo_assembles_and_links_media(tmp_path: Path):
    pipeline, store, _backup, _runs, _root = _make_pipeline(tmp_path)
    node_media = pipeline._node_media_store
    draft = await pipeline.open_or_resume_draft()
    await pipeline.set_draft_text(draft.id, "here is my caption")
    part = await pipeline.add_draft_part(draft.id, PNG, filename="p.png", kind=KIND_PHOTO)
    await pipeline.submit_draft(draft.id)
    await pipeline.drain()

    rec = store.records[draft.id]
    assert rec.status == INDEXED
    # Assembled raw_text is the caption + the fenced photo description (ADR-061 §5).
    assert "here is my caption" in rec.raw_text
    assert "<photo:" in rec.raw_text
    # The photo is linked to the content node(s) via node_media (T1 all-to-all; T3 attributes).
    assert any(m == part.id for (_n, m) in node_media.links)


async def test_submit_non_draft_is_conflict(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    await pipeline.set_draft_text(draft.id, "text")
    await pipeline.submit_draft(draft.id)
    await pipeline.drain()
    with pytest.raises(DraftNotOpen):
        await pipeline.submit_draft(draft.id)


async def test_discard_removes_capture_and_files(tmp_path: Path):
    pipeline, store, _backup, _runs, _root = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    part = await pipeline.add_draft_part(draft.id, PNG, filename="p.png", kind=KIND_PHOTO)
    media_files = pipeline._media_files
    assert media_files.absolute(part.file_path).exists()

    await pipeline.discard_draft(draft.id)
    assert draft.id not in store.records
    assert not media_files.absolute(part.file_path).exists()


async def test_part_ops_on_non_draft_are_conflict(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    cid = await pipeline.create_text_capture("plain text")
    await pipeline.drain()
    with pytest.raises(DraftNotOpen):
        await pipeline.add_draft_part(cid, PNG, filename="p.png", kind=KIND_PHOTO)
    with pytest.raises(DraftNotOpen):
        await pipeline.set_draft_text(cid, "x")
    with pytest.raises(DraftNotOpen):
        await pipeline.discard_draft(cid)


async def test_sweep_orphans_skips_drafts(tmp_path: Path):
    pipeline, store, *_ = _make_pipeline(tmp_path)
    draft = await pipeline.open_or_resume_draft()
    # A non-draft in-flight capture the sweep SHOULD fail.
    store.records["inflight"] = await store.create(
        capture_id="inflight", kind="text", status=RECEIVED
    )
    swept = await pipeline.sweep_orphans()
    assert store.records[draft.id].status == DRAFT  # untouched
    assert store.records["inflight"].status == "failed"
    assert swept == 1


async def test_gc_reclaims_stale_drafts_only(tmp_path: Path):
    pipeline, store, _backup, _runs, _root = _make_pipeline(tmp_path)
    now = datetime.now(UTC)
    # A stale draft (older than the 7-day horizon) with a part.
    old = await store.create(
        capture_id="old",
        kind=KIND_COMPOSITE,
        status=DRAFT,
        source="web",
        created_at=now - timedelta(days=10),
    )
    old_part = await pipeline._media_store.create(
        kind=KIND_PHOTO, source="capture", capture_id=old.id, part_ordinal=0, file_path="capture/x"
    )
    pipeline._media_files.write(old_part.file_path, PNG)
    # A fresh draft (well within the horizon) + a submitted capture — both must survive. Created
    # directly (the one-active-draft rule would otherwise make a 2nd open resume "old").
    await store.create(
        capture_id="fresh", kind=KIND_COMPOSITE, status=DRAFT, source="web", created_at=now
    )
    await store.create(
        capture_id="done",
        kind=KIND_COMPOSITE,
        status=INDEXED,
        source="web",
        created_at=now - timedelta(days=30),
    )

    reclaimed = await pipeline.gc_stale_drafts()
    assert reclaimed == 1
    assert "old" not in store.records
    assert not pipeline._media_files.absolute(old_part.file_path).exists()
    assert "fresh" in store.records  # within horizon
    assert "done" in store.records  # submitted — GC never touches it
