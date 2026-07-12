"""CapturePipeline service tests: fake providers + fake store + tmp vault (no DB, no LLM)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.capture.notes import NoteWriter
from app.config import Settings
from app.providers.registry import ProviderRegistry
from app.services.capture_pipeline import (
    CapturePipeline,
    FollowUpNotPending,
    UnsupportedAudio,
)
from app.services.capture_store import FAILED, INDEXED, ORGANIZING, RECEIVED

from .fakes import FakeCaptureStore, FakeChatProvider, FakeSTTProvider, FakeVaultBackup

CREATED = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)


def _organizer_json(plane: str = "Ideas", title: str = "A thought") -> str:
    note = {"title": title, "plane": plane, "planes": [plane], "tags": ["calm"], "body": "b"}
    return json.dumps({"notes": [note]})


def _responder(messages):
    """Organizer prompt → JSON note-set; nudge prompt → a short question."""
    system = messages[0].content
    if "organize a person's raw capture" in system:
        return _organizer_json()
    return "What felt most alive about that?"


def _make_pipeline(
    tmp_path: Path,
    *,
    chat: FakeChatProvider | None = None,
    stt: FakeSTTProvider | None = None,
):
    settings = Settings(
        vault_path=str(tmp_path / "vault"),
        data_path=str(tmp_path / "data"),
        planes=["Professional", "Personal", "Ideas"],
        scheduler_tz="UTC",
    )
    chat = chat or FakeChatProvider("fake-chat", responder=_responder)
    stt = stt or FakeSTTProvider(transcript="a spoken memo")
    registry = ProviderRegistry(
        {"fake-chat": chat, "fake-stt": stt},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_provider_id="fake-stt",
    )
    store = FakeCaptureStore()
    backup = FakeVaultBackup()
    pipeline = CapturePipeline(
        settings=settings,
        store=store,
        registry=registry,
        note_writer=NoteWriter(str(tmp_path / "vault")),
        vault_backup=backup,
    )
    return pipeline, store, backup, tmp_path / "vault"


async def test_text_capture_happy_path(tmp_path: Path):
    pipeline, store, backup, vault = _make_pipeline(tmp_path)
    cid = await pipeline.create_text_capture("I had a calm, productive day.", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.note_paths == ["Ideas/2026-07-12 A thought.md"]
    assert (vault / rec.note_paths[0]).exists()
    # Trailing nudge generated after a successful organize.
    assert rec.follow_up_question == "What felt most alive about that?"
    # Vault backup requested once for the write batch.
    assert backup.reasons == [f"capture {cid}"]


async def test_unparseable_organize_uses_inbox_and_no_nudge(tmp_path: Path):
    chat = FakeChatProvider("fake-chat", reply="totally not json")
    pipeline, store, _, vault = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("raw thought that survives", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.note_paths[0].startswith("Inbox/")
    assert (vault / rec.note_paths[0]).exists()
    # Inbox fallback path generates no nudge (ADR-019 §1).
    assert rec.follow_up_question is None


async def test_organize_chain_exhausted_falls_back_to_inbox(tmp_path: Path):
    chat = FakeChatProvider("fake-chat", available=False)
    pipeline, store, _, vault = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("never lose me", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # organizer failure never fails the capture (rule 2)
    assert rec.note_paths[0].startswith("Inbox/")
    assert rec.follow_up_question is None


async def test_voice_happy_path_transcribes_then_organizes(tmp_path: Path):
    pipeline, store, _, vault = _make_pipeline(tmp_path)
    cid = await pipeline.create_voice_capture(b"fake-audio-bytes", filename="memo.m4a")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.raw_text == "a spoken memo"  # transcript persisted
    assert rec.audio_path == f"{cid}.m4a"
    assert (tmp_path / "data" / f"{cid}.m4a").read_bytes() == b"fake-audio-bytes"
    assert rec.note_paths and (vault / rec.note_paths[0]).exists()


async def test_voice_stt_down_marks_failed(tmp_path: Path):
    stt = FakeSTTProvider(available=False)
    pipeline, store, _, _ = _make_pipeline(tmp_path, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.wav")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == FAILED
    assert "transcription failed" in (rec.error or "")
    # Audio is still on disk (never-lose) so a retry is possible.
    assert (tmp_path / "data" / f"{cid}.wav").exists()


async def test_voice_rejects_oversized_and_unsupported(tmp_path: Path):
    pipeline, _, _, _ = _make_pipeline(tmp_path)
    with pytest.raises(UnsupportedAudio):
        await pipeline.create_voice_capture(b"x", filename="memo.txt")
    settings_max = pipeline._settings.audio_max_bytes
    with pytest.raises(UnsupportedAudio):
        await pipeline.create_voice_capture(b"y" * (settings_max + 1), filename="memo.m4a")


async def test_sweep_orphans_marks_inflight_failed(tmp_path: Path):
    pipeline, store, _, _ = _make_pipeline(tmp_path)
    await store.create(capture_id="a", kind="text", status=RECEIVED)
    await store.create(capture_id="b", kind="text", status=ORGANIZING)
    await store.create(capture_id="c", kind="text", status=INDEXED)  # terminal, untouched

    swept = await pipeline.sweep_orphans()
    assert swept == 2
    assert store.records["a"].status == FAILED
    assert store.records["b"].status == FAILED
    assert store.records["c"].status == INDEXED


async def test_follow_up_pass2_replaces_notes(tmp_path: Path):
    # Pass 1 → note under Ideas; Pass 2 re-organizes to a different plane and replaces it.
    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            # If the answer is present, organize into a different plane/title.
            user = messages[1].content
            if "[Answer]" in user:
                return _organizer_json(plane="Personal", title="Enriched")
            return _organizer_json(plane="Ideas", title="Initial")
        return "Tell me more about how that felt?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, backup, vault = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("first pass content", created_at=CREATED)
    await pipeline.drain()
    rec = store.records[cid]
    first_path = rec.note_paths[0]
    assert first_path == "Ideas/2026-07-12 Initial.md"
    assert (vault / first_path).exists()

    await pipeline.submit_follow_up(cid, "here is more detail")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.follow_up_answer == "here is more detail"
    assert rec.status == INDEXED
    # note_paths replaced, not augmented; old note soft-deleted from disk.
    assert rec.note_paths == ["Personal/2026-07-12 Enriched.md"]
    assert (vault / rec.note_paths[0]).exists()
    assert not (vault / first_path).exists()
    # Two commit requests: original + follow-up.
    assert backup.reasons == [f"capture {cid}", f"capture {cid} follow-up"]


async def test_follow_up_organize_unavailable_keeps_original_notes(tmp_path: Path):
    # Pass 1 succeeds; the organize chain then goes down before the user answers. Pass 2 must
    # NOT delete the good notes — it fails retryably and leaves them intact (ADR-019 §2).
    chat = FakeChatProvider("fake-chat", responder=_responder)
    pipeline, store, backup, vault = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("keep me organized", created_at=CREATED)
    await pipeline.drain()
    original_paths = list(store.records[cid].note_paths)
    assert original_paths and (vault / original_paths[0]).exists()

    chat._available = False  # chain now unavailable
    await pipeline.submit_follow_up(cid, "an answer that cannot be organized")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == FAILED
    assert "original notes kept" in (rec.error or "")
    # Original notes untouched on disk and in note_paths; no destructive replace happened.
    assert rec.note_paths == original_paths
    assert (vault / original_paths[0]).exists()
    # Only the Pass-1 commit was requested; no follow-up commit.
    assert backup.reasons == [f"capture {cid}"]


async def test_nudge_store_failure_does_not_fail_capture(tmp_path: Path):
    # A failure while persisting the nudge question must never flip an already-indexed capture.
    class FlakyNudgeStore(FakeCaptureStore):
        async def set_follow_up_question(self, capture_id: str, question: str) -> None:
            raise RuntimeError("transient store failure")

    pipeline, store, _, vault = _make_pipeline(tmp_path)
    pipeline._store = store = FlakyNudgeStore()  # swap in the flaky store

    cid = await pipeline.create_text_capture("a calm productive day", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # nudge failure swallowed; capture stays healthy
    assert rec.follow_up_question is None
    assert rec.note_paths and (vault / rec.note_paths[0]).exists()


async def test_follow_up_guard_when_none_pending(tmp_path: Path):
    pipeline, store, _, _ = _make_pipeline(tmp_path)
    await store.create(capture_id="x", kind="text", status=INDEXED)  # no follow_up_question
    with pytest.raises(FollowUpNotPending):
        await pipeline.submit_follow_up("x", "answer")


async def test_follow_up_guard_when_already_answered(tmp_path: Path):
    pipeline, store, _, _ = _make_pipeline(tmp_path)
    await store.create(capture_id="y", kind="text", status=INDEXED)
    store.records["y"].follow_up_question = "q?"
    store.records["y"].follow_up_answer = "already"
    with pytest.raises(FollowUpNotPending):
        await pipeline.submit_follow_up("y", "again")
