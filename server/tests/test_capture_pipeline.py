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
    CaptureNotFound,
    CapturePipeline,
    FollowUpNotPending,
    NotRetryable,
    UnsupportedAudio,
)
from app.services.capture_store import FAILED, INDEXED, ORGANIZING, RECEIVED

from .fakes import (
    FakeAgentRunStore,
    FakeCaptureStore,
    FakeChatProvider,
    FakeIndexer,
    FakeSTTProvider,
    FakeVaultBackup,
)

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
    run_store: object | None = None,
    indexer: FakeIndexer | None = None,
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
        stt_chain=["fake-stt"],
    )
    store = FakeCaptureStore()
    backup = FakeVaultBackup()
    runs = run_store if run_store is not None else FakeAgentRunStore()
    indexer = indexer if indexer is not None else FakeIndexer()
    pipeline = CapturePipeline(
        settings=settings,
        store=store,
        registry=registry,
        note_writer=NoteWriter(str(tmp_path / "vault")),
        vault_backup=backup,
        run_store=runs,
        indexer=indexer,
    )
    return pipeline, store, backup, runs, tmp_path / "vault"


async def test_text_capture_happy_path(tmp_path: Path):
    pipeline, store, backup, _, vault = _make_pipeline(tmp_path)
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


async def test_written_notes_are_indexed_and_outcome_logged(tmp_path: Path):
    # The M2 index step (replacing the M1 stub): the freshly-written notes are handed to the
    # indexer, and the outcome is recorded in the capture's agent_runs details (ADR-021).
    indexer = FakeIndexer()
    runs = FakeAgentRunStore()
    pipeline, store, _, _, _ = _make_pipeline(tmp_path, indexer=indexer, run_store=runs)
    cid = await pipeline.create_text_capture("I had a calm, productive day.", created_at=CREATED)
    await pipeline.drain()

    assert indexer.calls == [store.records[cid].note_paths]  # exactly the written notes
    run = next(iter(runs.runs.values()))
    assert run.details["index"] == {
        "indexed": 1,
        "skipped": 0,
        "failed": 0,
        "deleted": 0,
        "partial": False,
        "failures": [],
    }


async def test_nudge_is_generated_from_raw_capture_not_notes(tmp_path: Path):
    # ADR-019 v2: the nudge is sourced from the person's ORIGINAL capture text (so it matches
    # their language), not the organized notes. Assert the nudge call saw the raw capture.
    seen_nudge_input: list[str] = []

    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            return _organizer_json()
        seen_nudge_input.append(messages[1].content)
        return "Ce te-a bucurat azi?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, _, _ = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("I had a calm, productive day.", created_at=CREATED)
    await pipeline.drain()

    assert store.records[cid].follow_up_question == "Ce te-a bucurat azi?"
    assert seen_nudge_input == ["I had a calm, productive day."]  # raw capture, not notes summary


async def test_reorganize_replaces_notes(tmp_path: Path):
    # Admin re-organize re-runs organize on the stored raw text and replaces the notes (the
    # English-only migration path). Old note soft-deleted, note_paths replaced.
    def responder(messages):
        if "organize a person's raw capture" in messages[0].content:
            title = "Reorganized" if responder.calls else "Initial"
            plane = "Personal" if responder.calls else "Ideas"
            responder.calls += 1
            return _organizer_json(plane=plane, title=title)
        return "nudge?"

    responder.calls = 0
    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, runs, vault = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("some raw text", created_at=CREATED)
    await pipeline.drain()
    first = store.records[cid].note_paths[0]
    assert first == "Ideas/2026-07-12 Initial.md"

    await pipeline.reorganize_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.note_paths == ["Personal/2026-07-12 Reorganized.md"]
    assert not (vault / first).exists()  # old note soft-deleted from disk
    assert any(r.details.get("kind", "").endswith("-reorganize") for r in runs.runs.values())


async def test_reorganize_missing_capture_raises(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    with pytest.raises(CaptureNotFound):
        await pipeline.reorganize_capture("does-not-exist")


async def test_capture_writes_agent_runs_interaction_row(tmp_path: Path):
    # ADR-021: a successful voice capture logs one agent_runs row with the STT/organize
    # resolution + details, so the interaction is queryable (Supabase dashboard / view).
    pipeline, store, _, runs, _ = _make_pipeline(tmp_path)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.m4a")
    await pipeline.drain()

    capture_runs = [r for r in runs.runs.values() if r.agent == "capture"]
    assert len(capture_runs) == 1
    run = capture_runs[0]
    assert run.status == "succeeded"
    assert run.model_used == "fake-chat"  # organize model
    assert run.fallback_used is False
    assert run.details["capture_id"] == cid
    assert run.details["stt"] == {"provider": "fake-stt", "fallback_used": False, "error": None}
    assert run.details["organize"] == {
        "model": "fake-chat",
        "fallback_used": False,
        "inbox_fallback": False,
    }
    assert "total" in run.details["timings_ms"]


async def test_capture_failure_closes_agent_runs_row_failed(tmp_path: Path):
    # ADR-021 + rule 7: STT chain exhausted → the run is closed `failed` with context, not left
    # dangling; the STT error is recorded in details.
    stt = FakeSTTProvider(available=False)
    pipeline, store, _, runs, _ = _make_pipeline(tmp_path, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.wav")
    await pipeline.drain()

    capture_runs = [r for r in runs.runs.values() if r.agent == "capture"]
    assert len(capture_runs) == 1
    run = capture_runs[0]
    assert run.status == "failed"
    assert "transcription failed" in (run.error or "")
    assert run.details["capture_id"] == cid
    assert run.details["stt"]["provider"] is None


async def test_logging_store_failure_does_not_break_capture(tmp_path: Path):
    # ADR-021: "logging never changes capture behavior." If the agent_runs store raises on
    # start AND finish, the capture must still reach `indexed` with its note on disk (rule 2).
    class BrokenRunStore:
        async def start(self, agent: str) -> str:
            raise RuntimeError("agent_runs DB down")

        async def finish(self, *a, **k) -> None:
            raise RuntimeError("agent_runs DB down")

        async def latest(self, agent: str, *, status: str | None = None):
            return None

    pipeline, store, _, _, vault = _make_pipeline(tmp_path, run_store=BrokenRunStore())
    cid = await pipeline.create_text_capture("survive broken logging", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.note_paths and (vault / rec.note_paths[0]).exists()


async def test_inbox_fallback_run_succeeds_and_is_flagged(tmp_path: Path):
    # ADR-021 (reconciled): an organize-chain outage degrades to an Inbox note — a capture
    # SUCCESS (never-lose), so the run is `succeeded`, but the degradation stays queryable via
    # details.organize.inbox_fallback.
    chat = FakeChatProvider("fake-chat", available=False)
    pipeline, store, _, runs, _ = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("organizer is down", created_at=CREATED)
    await pipeline.drain()

    assert store.records[cid].status == INDEXED
    run = next(r for r in runs.runs.values() if r.agent == "capture")
    assert run.status == "succeeded"
    assert run.details["organize"]["inbox_fallback"] is True
    assert run.details["organize"]["model"] is None


async def test_unparseable_organize_uses_inbox_and_no_nudge(tmp_path: Path):
    chat = FakeChatProvider("fake-chat", reply="totally not json")
    pipeline, store, _, _, vault = _make_pipeline(tmp_path, chat=chat)
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
    pipeline, store, _, _, vault = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("never lose me", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # organizer failure never fails the capture (rule 2)
    assert rec.note_paths[0].startswith("Inbox/")
    assert rec.follow_up_question is None


async def test_voice_happy_path_transcribes_then_organizes(tmp_path: Path):
    pipeline, store, _, _, vault = _make_pipeline(tmp_path)
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
    pipeline, store, _, _, _ = _make_pipeline(tmp_path, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.wav")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == FAILED
    assert "transcription failed" in (rec.error or "")
    # Audio is still on disk (never-lose) so a retry is possible.
    assert (tmp_path / "data" / f"{cid}.wav").exists()


async def test_voice_rejects_oversized_and_unsupported(tmp_path: Path):
    pipeline, _, _, _, _ = _make_pipeline(tmp_path)
    with pytest.raises(UnsupportedAudio):
        await pipeline.create_voice_capture(b"x", filename="memo.txt")
    settings_max = pipeline._settings.audio_max_bytes
    with pytest.raises(UnsupportedAudio):
        await pipeline.create_voice_capture(b"y" * (settings_max + 1), filename="memo.m4a")


async def test_sweep_orphans_marks_inflight_failed(tmp_path: Path):
    pipeline, store, _, _, _ = _make_pipeline(tmp_path)
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
    pipeline, store, backup, _, vault = _make_pipeline(tmp_path, chat=chat)

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
    pipeline, store, backup, _, vault = _make_pipeline(tmp_path, chat=chat)

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

    pipeline, store, _, _, vault = _make_pipeline(tmp_path)
    pipeline._store = store = FlakyNudgeStore()  # swap in the flaky store

    cid = await pipeline.create_text_capture("a calm productive day", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # nudge failure swallowed; capture stays healthy
    assert rec.follow_up_question is None
    assert rec.note_paths and (vault / rec.note_paths[0]).exists()


async def test_retry_reruns_failed_voice_capture(tmp_path: Path):
    # STT down → failed (audio kept). Bring STT up and retry → transcribes + organizes to indexed.
    stt = FakeSTTProvider(transcript="a recovered memo", available=False)
    pipeline, store, _, _, vault = _make_pipeline(tmp_path, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.m4a")
    await pipeline.drain()
    assert store.records[cid].status == FAILED

    stt._available = True
    await pipeline.retry_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.error is None  # reset_for_retry cleared the stale failure
    assert rec.raw_text == "a recovered memo"
    assert rec.note_paths and (vault / rec.note_paths[0]).exists()


async def test_retry_removes_partial_notes_before_rerun(tmp_path: Path):
    # A capture that failed after notes landed (e.g. a boot-swept orphan at `written`). Retry must
    # remove the prior notes first so the re-run cannot leave a numeric-suffix duplicate (rule 6).
    pipeline, store, _, _, vault = _make_pipeline(tmp_path)
    cid = await pipeline.create_text_capture("I had a calm day.", created_at=CREATED)
    await pipeline.drain()
    original = store.records[cid].note_paths[0]
    assert original == "Ideas/2026-07-12 A thought.md"

    await store.mark_failed(cid, "interrupted by restart")  # simulate an orphaned in-flight row
    await pipeline.retry_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    # Exactly one note on disk — the old one was removed, not shadowed by a " 2" duplicate.
    assert rec.note_paths == [original]
    ideas_notes = list((vault / "Ideas").glob("*.md"))
    assert ideas_notes == [vault / original]


async def test_retry_follow_up_reapplies_answer(tmp_path: Path):
    # Pass-2 failed because the chain was down (notes kept). Retry re-applies the held answer.
    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            if "[Answer]" in messages[1].content:
                return _organizer_json(plane="Personal", title="Enriched")
            return _organizer_json(plane="Ideas", title="Initial")
        return "Tell me more?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, backup, _, vault = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("first pass", created_at=CREATED)
    await pipeline.drain()
    original = store.records[cid].note_paths[0]

    chat._available = False
    await pipeline.submit_follow_up(cid, "here is more")
    await pipeline.drain()
    assert store.records[cid].status == FAILED  # Pass 2 couldn't organize; notes kept

    chat._available = True
    await pipeline.retry_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.follow_up_answer == "here is more"
    assert rec.note_paths == ["Personal/2026-07-12 Enriched.md"]
    assert (vault / rec.note_paths[0]).exists()
    assert not (vault / original).exists()  # Pass-1 note superseded


async def test_retry_non_failed_capture_raises(tmp_path: Path):
    pipeline, store, _, _, _ = _make_pipeline(tmp_path)
    cid = await pipeline.create_text_capture("all good", created_at=CREATED)
    await pipeline.drain()
    assert store.records[cid].status == INDEXED
    with pytest.raises(NotRetryable):
        await pipeline.retry_capture(cid)


async def test_retry_missing_capture_raises(tmp_path: Path):
    pipeline, _, _, _, _ = _make_pipeline(tmp_path)
    with pytest.raises(CaptureNotFound):
        await pipeline.retry_capture("no-such-id")


async def test_follow_up_guard_when_none_pending(tmp_path: Path):
    pipeline, store, _, _, _ = _make_pipeline(tmp_path)
    await store.create(capture_id="x", kind="text", status=INDEXED)  # no follow_up_question
    with pytest.raises(FollowUpNotPending):
        await pipeline.submit_follow_up("x", "answer")


async def test_follow_up_guard_when_already_answered(tmp_path: Path):
    pipeline, store, _, _, _ = _make_pipeline(tmp_path)
    await store.create(capture_id="y", kind="text", status=INDEXED)
    store.records["y"].follow_up_question = "q?"
    store.records["y"].follow_up_answer = "already"
    with pytest.raises(FollowUpNotPending):
        await pipeline.submit_follow_up("y", "again")
