"""CapturePipeline service tests: fake providers + fake store + tmp store (no DB, no LLM).

Node filenames carry a random short-id (a fresh uuid per node), so path assertions match on the
``<type>/<date>--<slug>--`` prefix rather than an exact string.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.entities.resolver import EntityResolver
from app.entities.store import EntityCandidate
from app.graph.node_writer import NodeDocument, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
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
    FakeAliasStore,
    FakeCaptureStore,
    FakeChatProvider,
    FakeIndexer,
    FakeReviewQueue,
    FakeStoreBackup,
    FakeSTTProvider,
    fake_routing,
)

CREATED = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)


class FakeTagVocabulary:
    """Returns a preset tag vocabulary, recording the limit it was asked for (ADR-024 §1)."""

    def __init__(self, tags: list[str]) -> None:
        self._tags = tags
        self.calls: list[int] = []

    async def vocabulary_tags(self, *, limit: int) -> list[str]:
        self.calls.append(limit)
        return self._tags[:limit]


class BrokenTagVocabulary:
    """A vocabulary source that always errors — the pipeline must degrade, not fail (rule 2/7)."""

    async def vocabulary_tags(self, *, limit: int) -> list[str]:
        raise RuntimeError("index unavailable")


def _organizer_json(title: str = "A thought", node_type: str = "memory") -> str:
    node = {
        "title": title,
        "type": node_type,
        "plane": "Ideas",
        "planes": ["Ideas"],
        "tags": ["calm"],
        "body": "b",
        "entities": [],
    }
    return json.dumps({"nodes": [node]})


def _responder(messages):
    """Organizer prompt → JSON node-set; nudge prompt → a short question."""
    system = messages[0].content
    if "organize a person's raw capture" in system:
        return _organizer_json()
    return "What felt most alive about that?"


def _is_node(path: str, slug: str, *, node_type: str = "memory", date: str = "2026-07-12") -> bool:
    prefix = f"{node_type}/{date}--{slug}--" if node_type == "memory" else f"{node_type}/{slug}--"
    return path.startswith(prefix) and path.endswith(".md")


def _make_pipeline(
    tmp_path: Path,
    *,
    chat: FakeChatProvider | None = None,
    stt: FakeSTTProvider | None = None,
    run_store: object | None = None,
    indexer: FakeIndexer | None = None,
    tag_vocabulary: object | None = None,
    alias_store: FakeAliasStore | None = None,
):
    settings = Settings(
        graph_store_path=str(tmp_path / "store"),
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
    backup = FakeStoreBackup()
    runs = run_store if run_store is not None else FakeAgentRunStore()
    indexer = indexer if indexer is not None else FakeIndexer()
    review = FakeReviewQueue()
    routing = fake_routing(registry)
    resolver = EntityResolver(
        settings=settings,
        alias_store=alias_store if alias_store is not None else FakeAliasStore(),
        review_queue=review,
        routing=routing,
    )
    pipeline = CapturePipeline(
        settings=settings,
        store=store,
        routing=routing,
        registry=registry,
        node_writer=NodeWriter(str(tmp_path / "store")),
        store_backup=backup,
        run_store=runs,
        indexer=indexer,
        entity_resolver=resolver,
        review_queue=review,
        tag_vocabulary=tag_vocabulary,
    )
    return pipeline, store, backup, runs, tmp_path / "store"


async def test_text_capture_happy_path(tmp_path: Path):
    pipeline, store, backup, _, root = _make_pipeline(tmp_path)
    cid = await pipeline.create_text_capture("I had a calm, productive day.", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert _is_node(rec.node_paths[0], "a-thought")
    assert (root / rec.node_paths[0]).exists()
    assert rec.follow_up_question == "What felt most alive about that?"
    assert backup.reasons == [f"capture {cid}"]


async def test_mcp_capture_tags_source_and_processes(tmp_path: Path):
    # M5 task 4: create_mcp_capture stamps source=mcp on the capture + the written node frontmatter
    # (so an MCP-driven capture is distinguishable), and still runs the full organize pipeline.
    pipeline, store, _, _, root = _make_pipeline(tmp_path)
    cid = await pipeline.create_mcp_capture("I had a calm, productive day.")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.source == "mcp"
    assert rec.status == INDEXED
    node_text = (root / rec.node_paths[0]).read_text(encoding="utf-8")
    assert "source: mcp" in node_text


async def test_chat_capture_is_tagged_and_deterministic(tmp_path: Path):
    # ADR-048 §1: an endorsed chat candidate becomes a source=chat / source_ref=session-id capture
    # that flows through the organizer (source: chat on the node). The id is deterministic over
    # (session, statement), so a re-distill of the same candidate collapses — no duplicate (rule 6).
    pipeline, store, _, _, root = _make_pipeline(tmp_path)
    sid = "11111111-2222-3333-4444-555555555555"
    cid = await pipeline.create_chat_capture(
        "I had a calm, productive day.", session_id=sid, created_at=CREATED
    )
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.source == "chat"
    assert rec.source_ref == sid
    assert rec.status == INDEXED
    assert "source: chat" in (root / rec.node_paths[0]).read_text(encoding="utf-8")

    # Same (session, statement) again → same id, no new capture row (idempotent re-distill).
    before = set(store.records)
    cid2 = await pipeline.create_chat_capture(
        "I had a calm, productive day.", session_id=sid, created_at=CREATED
    )
    await pipeline.drain()
    assert cid2 == cid
    assert set(store.records) == before  # no duplicate row


async def test_written_nodes_are_indexed_and_outcome_logged(tmp_path: Path):
    indexer = FakeIndexer()
    runs = FakeAgentRunStore()
    pipeline, store, _, _, _ = _make_pipeline(tmp_path, indexer=indexer, run_store=runs)
    cid = await pipeline.create_text_capture("I had a calm, productive day.", created_at=CREATED)
    await pipeline.drain()

    assert indexer.calls == [store.records[cid].node_paths]  # exactly the written nodes
    run = next(iter(runs.runs.values()))
    assert run.details["index"] == {
        "indexed": 1,
        "skipped": 0,
        "failed": 0,
        "deleted": 0,
        "edges": 0,
        "partial": False,
        "failures": [],
    }


async def test_organizer_prompt_injects_tag_vocabulary(tmp_path: Path):
    seen_system: list[str] = []

    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            seen_system.append(system)
            return _organizer_json()
        return "a nudge?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    vocab = FakeTagVocabulary(["work", "calm", "health"])
    pipeline, *_ = _make_pipeline(tmp_path, chat=chat, tag_vocabulary=vocab)
    await pipeline.create_text_capture("A calm day.", created_at=CREATED)
    await pipeline.drain()

    assert seen_system, "organizer was called"
    assert "work, calm, health" in seen_system[0]
    assert vocab.calls == [pipeline._settings.organizer_tag_vocabulary_max]


async def test_organizer_prompt_omits_vocabulary_when_source_errors(tmp_path: Path):
    seen_system: list[str] = []

    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            seen_system.append(system)
            return _organizer_json()
        return "a nudge?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, _, _ = _make_pipeline(
        tmp_path, chat=chat, tag_vocabulary=BrokenTagVocabulary()
    )
    cid = await pipeline.create_text_capture("A calm day.", created_at=CREATED)
    await pipeline.drain()

    assert store.records[cid].status == INDEXED  # capture unaffected
    assert "Existing tags" not in seen_system[0]  # no injected vocabulary block


async def test_nudge_is_generated_from_raw_capture_not_nodes(tmp_path: Path):
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
    assert seen_nudge_input == ["I had a calm, productive day."]  # raw capture, not a node summary


async def test_reorganize_replaces_nodes(tmp_path: Path):
    def responder(messages):
        if "organize a person's raw capture" in messages[0].content:
            title = "Reorganized" if responder.calls else "Initial"
            responder.calls += 1
            return _organizer_json(title=title)
        return "nudge?"

    responder.calls = 0
    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, runs, root = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("some raw text", created_at=CREATED)
    await pipeline.drain()
    first = store.records[cid].node_paths[0]
    assert _is_node(first, "initial")

    await pipeline.reorganize_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert _is_node(rec.node_paths[0], "reorganized")
    assert not (root / first).exists()  # old node soft-deleted from disk
    assert any(r.details.get("kind", "").endswith("-reorganize") for r in runs.runs.values())


async def test_reorganize_refuses_a_removed_capture(tmp_path: Path):
    # ADR-048 §11 (M6 task 4): a one-tap-removed capture must NOT be resurrected by any replay —
    # the admin reorganize (and the §10 inbox drainer that drives it) skip a tombstoned capture,
    # so its git-rm'd nodes are never re-materialized (same exclusion reprocess-all applies).
    def responder(messages):
        if "organize a person's raw capture" in messages[0].content:
            responder.calls += 1
            return _organizer_json(title="Resurrected")
        return "nudge?"

    responder.calls = 0
    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, runs, root = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("some raw text", created_at=CREATED)
    await pipeline.drain()
    original = store.records[cid].node_paths[0]
    organize_calls = responder.calls

    # Tombstone the capture (as one-tap remove would) and attempt an admin reorganize.
    store.records[cid].removed_at = datetime(2026, 7, 16, tzinfo=UTC)
    await pipeline.reorganize_capture(cid)
    await pipeline.drain()

    # No re-organize ran; node_paths untouched; the attempt is logged as a skip.
    assert responder.calls == organize_calls
    assert store.records[cid].node_paths == [original]
    assert any(r.status == "skipped" and "removed" in (r.summary or "") for r in runs.runs.values())


def _organizer_json_entity(title: str, entity_name: str = "Alex") -> str:
    node = {
        "title": title,
        "type": "memory",
        "plane": "Ideas",
        "planes": ["Ideas"],
        "tags": ["x"],
        "body": "b",
        "entities": [{"name": entity_name, "type": "person", "rel": "involves"}],
    }
    return json.dumps({"nodes": [node]})


async def test_reorganize_keeps_entity_hubs(tmp_path: Path):
    # ADR-038: a reorganize deletes the content nodes but NEVER the shared entity hubs, so no
    # other node's edge to the hub is left dangling.
    def responder(messages):
        if "organize a person's raw capture" in messages[0].content:
            title = "Second" if responder.calls else "First"
            responder.calls += 1
            return _organizer_json_entity(title)
        return "nudge?"

    responder.calls = 0
    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, _, root = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("text", created_at=CREATED)
    await pipeline.drain()
    hub = next(p for p in store.records[cid].node_paths if p.startswith("person/"))
    assert (root / hub).exists()

    await pipeline.reorganize_capture(cid)
    await pipeline.drain()

    # The originally-minted hub is preserved (ADR-038); node_paths refreshed to the new content.
    assert (root / hub).exists()
    assert any(p.startswith("memory/") and "second" in p for p in store.records[cid].node_paths)


async def test_pipeline_coerces_entity_typed_node_to_memory(tmp_path: Path):
    # ADR-039: an entity-typed content node the model emits is coerced to memory (+ surfaced).
    def responder(messages):
        if "organize a person's raw capture" in messages[0].content:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "title": "How I know Horia",
                            "type": "person",
                            "plane": "Ideas",
                            "planes": ["Ideas"],
                            "tags": [],
                            "body": "Horia is my friend",
                            "entities": [],
                        }
                    ]
                }
            )
        return "nudge?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, runs, _ = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("x", created_at=CREATED)
    await pipeline.drain()

    assert store.records[cid].node_paths[0].startswith("memory/")  # coerced person → memory
    run = next(iter(runs.runs.values()))
    assert run.details["organize"]["coerced_entity_nodes"] == ["person"]


async def test_capture_accretes_variant_alias_onto_existing_hub(tmp_path: Path):
    # ADR-040 §4: a confident link under a new surface form accretes it onto the hub's file.
    writer = NodeWriter(str(tmp_path / "store"))
    [hub] = writer.write_nodes(
        [
            NodeDocument(
                id="horia-1",
                type="person",
                title="Horia",
                body="",
                created_local=CREATED,
                source="text",
                aliases=("Horia",),
            )
        ]
    )
    alias = FakeAliasStore(
        entities=[
            EntityCandidate(
                id="horia-1",
                type="person",
                title="Horia",
                aliases=["Horia"],
                store_path=hub.store_path,
            )
        ]
    )

    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            return _organizer_json_entity("Standup", entity_name="Horia Fenwick")
        if "You resolve which existing entity" in system:
            return '{"choice": "horia-1", "conf": 0.95}'
        return "nudge?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, _, _, _, root = _make_pipeline(tmp_path, chat=chat, alias_store=alias)
    await pipeline.create_text_capture("Talked to Horia Fenwick at standup", created_at=CREATED)
    await pipeline.drain()

    raw = (root / Path(*hub.store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(raw, store_path=hub.store_path, fallback_created=CREATED)
    assert "Horia Fenwick" in meta.aliases  # accreted onto the existing hub


async def test_reorganize_missing_capture_raises(tmp_path: Path):
    pipeline, *_ = _make_pipeline(tmp_path)
    with pytest.raises(CaptureNotFound):
        await pipeline.reorganize_capture("does-not-exist")


async def test_capture_writes_agent_runs_interaction_row(tmp_path: Path):
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
        "coerced_entity_nodes": [],
    }
    assert "total" in run.details["timings_ms"]


async def test_capture_failure_closes_agent_runs_row_failed(tmp_path: Path):
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
    class BrokenRunStore:
        async def start(self, agent: str) -> str:
            raise RuntimeError("agent_runs DB down")

        async def finish(self, *a, **k) -> None:
            raise RuntimeError("agent_runs DB down")

        async def latest(self, agent: str, *, status: str | None = None):
            return None

    pipeline, store, _, _, root = _make_pipeline(tmp_path, run_store=BrokenRunStore())
    cid = await pipeline.create_text_capture("survive broken logging", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.node_paths and (root / rec.node_paths[0]).exists()


async def test_inbox_fallback_run_succeeds_and_is_flagged(tmp_path: Path):
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
    pipeline, store, _, _, root = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("raw thought that survives", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.node_paths[0].startswith("inbox/")  # fallback lands in the inbox folder
    assert (root / rec.node_paths[0]).exists()
    assert rec.follow_up_question is None  # inbox fallback generates no nudge (ADR-019 §1)


async def test_organize_chain_exhausted_falls_back_to_inbox(tmp_path: Path):
    chat = FakeChatProvider("fake-chat", available=False)
    pipeline, store, _, _, root = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("never lose me", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # organizer failure never fails the capture (rule 2)
    assert rec.node_paths[0].startswith("inbox/")
    assert rec.follow_up_question is None


async def test_voice_happy_path_transcribes_then_organizes(tmp_path: Path):
    pipeline, store, _, _, root = _make_pipeline(tmp_path)
    cid = await pipeline.create_voice_capture(b"fake-audio-bytes", filename="memo.m4a")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.raw_text == "a spoken memo"  # transcript persisted
    assert rec.audio_path == f"{cid}.m4a"
    assert (tmp_path / "data" / f"{cid}.m4a").read_bytes() == b"fake-audio-bytes"
    assert rec.node_paths and (root / rec.node_paths[0]).exists()


async def test_voice_stt_down_marks_failed(tmp_path: Path):
    stt = FakeSTTProvider(available=False)
    pipeline, store, _, _, _ = _make_pipeline(tmp_path, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.wav")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == FAILED
    assert "transcription failed" in (rec.error or "")
    assert (tmp_path / "data" / f"{cid}.wav").exists()  # audio kept (never-lose)


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


async def test_follow_up_pass2_replaces_nodes(tmp_path: Path):
    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            user = messages[1].content
            if "[Answer]" in user:
                return _organizer_json(title="Enriched")
            return _organizer_json(title="Initial")
        return "Tell me more about how that felt?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, backup, _, root = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("first pass content", created_at=CREATED)
    await pipeline.drain()
    first_path = store.records[cid].node_paths[0]
    assert _is_node(first_path, "initial")
    assert (root / first_path).exists()

    await pipeline.submit_follow_up(cid, "here is more detail")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.follow_up_answer == "here is more detail"
    assert rec.status == INDEXED
    assert _is_node(rec.node_paths[0], "enriched")
    assert (root / rec.node_paths[0]).exists()
    assert not (root / first_path).exists()  # old node soft-deleted
    assert backup.reasons == [f"capture {cid}", f"capture {cid} follow-up"]


async def test_follow_up_organize_unavailable_keeps_original_nodes(tmp_path: Path):
    chat = FakeChatProvider("fake-chat", responder=_responder)
    pipeline, store, backup, _, root = _make_pipeline(tmp_path, chat=chat)

    cid = await pipeline.create_text_capture("keep me organized", created_at=CREATED)
    await pipeline.drain()
    original_paths = list(store.records[cid].node_paths)
    assert original_paths and (root / original_paths[0]).exists()

    chat._available = False  # chain now unavailable
    await pipeline.submit_follow_up(cid, "an answer that cannot be organized")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == FAILED
    assert "original notes kept" in (rec.error or "")
    assert rec.node_paths == original_paths  # untouched
    assert (root / original_paths[0]).exists()
    assert backup.reasons == [f"capture {cid}"]  # no follow-up commit


async def test_nudge_store_failure_does_not_fail_capture(tmp_path: Path):
    class FlakyNudgeStore(FakeCaptureStore):
        async def set_follow_up_question(self, capture_id: str, question: str) -> None:
            raise RuntimeError("transient store failure")

    pipeline, store, _, _, root = _make_pipeline(tmp_path)
    pipeline._store = store = FlakyNudgeStore()  # swap in the flaky store

    cid = await pipeline.create_text_capture("a calm productive day", created_at=CREATED)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # nudge failure swallowed; capture stays healthy
    assert rec.follow_up_question is None
    assert rec.node_paths and (root / rec.node_paths[0]).exists()


async def test_retry_reruns_failed_voice_capture(tmp_path: Path):
    stt = FakeSTTProvider(transcript="a recovered memo", available=False)
    pipeline, store, _, _, root = _make_pipeline(tmp_path, stt=stt)
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
    assert rec.node_paths and (root / rec.node_paths[0]).exists()


async def test_retry_removes_partial_nodes_before_rerun(tmp_path: Path):
    pipeline, store, _, _, root = _make_pipeline(tmp_path)
    cid = await pipeline.create_text_capture("I had a calm day.", created_at=CREATED)
    await pipeline.drain()
    original = store.records[cid].node_paths[0]
    assert _is_node(original, "a-thought")

    await store.mark_failed(cid, "interrupted by restart")  # simulate an orphaned in-flight row
    await pipeline.retry_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    # Exactly one node on disk under memory/ — the old one was removed, not shadowed.
    memory_nodes = list((root / "memory").glob("*.md"))
    assert memory_nodes == [root / rec.node_paths[0]]
    assert not (root / original).exists() or original == rec.node_paths[0]


async def test_retry_follow_up_reapplies_answer(tmp_path: Path):
    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            if "[Answer]" in messages[1].content:
                return _organizer_json(title="Enriched")
            return _organizer_json(title="Initial")
        return "Tell me more?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, backup, _, root = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("first pass", created_at=CREATED)
    await pipeline.drain()
    original = store.records[cid].node_paths[0]

    chat._available = False
    await pipeline.submit_follow_up(cid, "here is more")
    await pipeline.drain()
    assert store.records[cid].status == FAILED  # Pass 2 couldn't organize; nodes kept

    chat._available = True
    await pipeline.retry_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.follow_up_answer == "here is more"
    assert _is_node(rec.node_paths[0], "enriched")
    assert (root / rec.node_paths[0]).exists()
    assert not (root / original).exists()  # Pass-1 node superseded


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
