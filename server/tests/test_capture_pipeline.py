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
from app.providers.base import ProviderUnavailable
from app.providers.registry import ProviderRegistry
from app.services.capture_pipeline import (
    CaptureNotFound,
    CapturePipeline,
    FollowUpNotPending,
    NotRetryable,
    UnsupportedAudio,
    UnsupportedImage,
)
from app.services.capture_store import DERIVING, FAILED, INDEXED, KIND_IMAGE, ORGANIZING, RECEIVED
from app.services.media_derivation import MediaDerivationService, placeholder
from app.services.media_store import DERIVED, UNAVAILABLE, MediaFiles

from .fakes import (
    FakeAgentRunStore,
    FakeAliasStore,
    FakeCaptureStore,
    FakeChatProvider,
    FakeIndexer,
    FakeMediaStore,
    FakeNodeMediaStore,
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
    # Voice is unified onto the media substrate (M9 T4, ADR-060 §5): wire the media store/files +
    # derivation so `create_voice_capture` mints a `voice` media row and STT runs through the
    # derivation engine. Text/chat captures leave it untouched (no media → link-write is a no-op).
    media_store = FakeMediaStore()
    media_files = MediaFiles(settings)
    media_derivation = MediaDerivationService(
        store=media_store,
        files=media_files,
        routing=routing,
        registry=registry,
        run_store=runs,
        max_attempts=settings.media_derive_max_attempts,
        rederive_max_per_run=50,
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
        media_store=media_store,
        media_files=media_files,
        media_derivation=media_derivation,
        node_media_store=FakeNodeMediaStore(),
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
    # Voice STT now rides the media derivation engine (ADR-060 §5) — the `stt` detail carries the
    # media id + terminal status + model (mirroring the image `derive` detail), not the old
    # provider/fallback shape.
    assert run.details["stt"]["status"] == "derived"
    assert run.details["stt"]["model"] == "fake-stt"
    assert run.details["stt"]["kind"] == "voice"
    assert run.details["organize"] == {
        "model": "fake-chat",
        "fallback_used": False,
        "inbox_fallback": False,
        "coerced_entity_nodes": [],
    }
    assert "total" in run.details["timings_ms"]


async def test_voice_stt_down_degrades_to_placeholder(tmp_path: Path):
    # ADR-060 §6: STT is now a DERIVATION, symmetric with image — a persistent STT failure walks
    # retry → `unavailable` → the explicit placeholder WITHOUT blocking. The capture organizes
    # anyway and lands `indexed` (never `failed`); the audio is kept + re-derivable. `failed` is
    # reserved for true infra only.
    stt = FakeSTTProvider(available=False)
    pipeline, store, _, runs, _ = _make_pipeline(tmp_path, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.wav")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED  # degrades, never blocks
    assert rec.raw_text == placeholder("voice")  # "<voice note — transcript unavailable>"
    media = await pipeline._media_store.get_by_capture_id(cid)
    assert media.status == UNAVAILABLE
    run = next(r for r in runs.runs.values() if r.agent == "capture")
    assert run.status == "succeeded"
    assert run.details["stt"]["status"] == "unavailable"


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
    # The transcript is persisted PLAIN/unfenced (the person's own words — ADR-060 §5), unlike the
    # `<photo: …>` fence, so it organizes like any spoken capture.
    assert rec.raw_text == "a spoken memo"
    assert rec.audio_path is None  # audio now lives as a media row, not captures.audio_path
    # Voice is unified onto the media substrate: a `voice` media row under the uniform layout, its
    # audio at /srv/data/media/capture/<id>.m4a, transcript = derived_text.
    media = await pipeline._media_store.get_by_capture_id(cid)
    assert media.kind == "voice" and media.source == "capture"
    assert media.status == DERIVED and media.derived_text == "a spoken memo"
    assert media.file_path == f"capture/{cid}.m4a" and media.mime_type == "audio/mp4"
    assert (
        tmp_path / "data" / "media" / "capture" / f"{cid}.m4a"
    ).read_bytes() == b"fake-audio-bytes"
    assert rec.node_paths and (root / rec.node_paths[0]).exists()
    # The voice content node is linked to its media (ADR-060 §1) via the derived-tier node_media.
    assert any(m == media.id for _n, m in pipeline._node_media_store.links)


async def test_voice_rederive_recovers_node_after_stt_failure(tmp_path: Path):
    # Symmetric with the image redescribe drill (ADR-060 §5): a persistent STT failure files a
    # placeholder node; once STT is back, kind-aware `rederive_capture` re-transcribes AND rebuilds
    # the node from the recovered transcript — the recovery reaches the GRAPH, not just media.
    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            captured = messages[1].content  # echo the transcript/placeholder into the node body
            return json.dumps(
                {
                    "nodes": [
                        {
                            "title": "A memo",
                            "type": "memory",
                            "plane": "Ideas",
                            "planes": ["Ideas"],
                            "tags": ["voice"],
                            "body": captured,
                            "entities": [],
                        }
                    ]
                }
            )
        return "What did that bring up?"

    stt = FakeSTTProvider(transcript="a recovered memo", available=False)
    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, _, root = _make_pipeline(tmp_path, chat=chat, stt=stt)
    cid = await pipeline.create_voice_capture(b"audio", filename="memo.m4a")
    await pipeline.drain()
    assert store.records[cid].status == INDEXED  # degraded, not failed
    assert store.records[cid].raw_text == placeholder("voice")
    assert (await pipeline._media_store.get_by_capture_id(cid)).status == UNAVAILABLE

    stt._available = True
    await pipeline.rederive_capture(cid)
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.raw_text == "a recovered memo"  # plain transcript (voice), not fenced
    media = await pipeline._media_store.get_by_capture_id(cid)
    assert media.status == DERIVED and media.derived_text == "a recovered memo"
    body = (root / rec.node_paths[0]).read_text(encoding="utf-8")
    assert "recovered memo" in body and placeholder("voice") not in body


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


async def test_inner_voice_extraction_links_event_to_internal_node(tmp_path: Path):
    # ADR-055 §2 + ADR-056: a mixed capture organizes into an EXTERNAL event node + an INTERNAL
    # feeling node, linked by an event `led_to` internal edge; the event's symbolic time_ref sets
    # `occurred` and its phrase becomes a body token — all against the STORED anchor (2026-07-12).
    seen_system: list[str] = []

    def responder(messages):
        system = messages[0].content
        if "organize a person's raw capture" in system:
            seen_system.append(system)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "title": "Walk",
                            "type": "memory",
                            "plane": "Personal",
                            "planes": ["Personal"],
                            "tags": ["walk"],
                            "body": "Walked with D. 10 days ago.",
                            "interiority": "external",
                            "time_refs": [
                                {
                                    "phrase": "10 days ago",
                                    "kind": "relative",
                                    "unit": "day",
                                    "offset": -10,
                                    "event": True,
                                }
                            ],
                            "entities": [],
                        },
                        {
                            "title": "Ease",
                            "type": "memory",
                            "plane": "Personal",
                            "planes": ["Personal"],
                            "tags": ["calm"],
                            "body": "It felt easy, I have missed this.",
                            "interiority": "internal",
                            "arose_from": 0,
                            "entities": [],
                        },
                    ]
                }
            )
        return "What made it feel easy?"

    chat = FakeChatProvider("fake-chat", responder=responder)
    pipeline, store, _, _, root = _make_pipeline(tmp_path, chat=chat)
    cid = await pipeline.create_text_capture("Walked with D., felt easy.", created_at=CREATED)
    await pipeline.drain()

    # The stored anchor was injected into the organizer prompt (ADR-056 §1).
    assert "recorded on" in seen_system[0]

    rec = store.records[cid]
    metas: dict[str, object] = {}
    raws: dict[str, str] = {}
    for p in rec.node_paths:
        raw = (root / p).read_text(encoding="utf-8")
        meta = parse_node_metadata(raw, store_path=p, fallback_created=CREATED)
        metas[meta.title] = meta
        raws[meta.title] = raw

    walk, ease = metas["Walk"], metas["Ease"]
    # Event node: external, occurred = anchor − 10 days = 2026-07-02, phrase → token in the body.
    assert walk.interiority == "external"
    assert walk.occurred_start.isoformat() == "2026-07-02"
    assert "[[t:2026-07-02]]" in raws["Walk"]
    # Feeling node: internal.
    assert ease.interiority == "internal"
    # The inner-voice edge: event `led_to` the internal node, by its id (Option A — no new vocab).
    assert ("led_to", ease.id) in [(e.rel, e.to) for e in walk.edges]


# --- M9 T3: ad-hoc image capture (POST /capture/image → describe → organize (fenced)) -----------
#
# The image leg mirrors voice: the raw image is kept under the media substrate, its vision
# description is derived (driven to a terminal state), then organized as fenced `<photo: …>` text —
# the derived description playing the role a transcript plays for voice (ADR-057 §3/§5/§6).

PNG_BYTES = b"\x89PNG\r\n\x1a\n fake image bytes"
_PHOTO_DESCRIPTION = "A whiteboard with a system diagram: API, DB, Cache."


def _image_responder(description: str = _PHOTO_DESCRIPTION, *, vision_down: bool = False):
    """One provider serving BOTH legs of an image capture: the `vision` describe call and the
    `conspect` organize call, distinguished by system prompt. `vision_down` raises on the describe
    leg only (organize still succeeds), isolating a derivation failure from the organize step. The
    organize branch ECHOES the captured text into the node body so a test can assert what was
    organized (a real organizer would clean it; the fake just needs to be faithful to its input)."""

    def responder(messages):
        system = messages[0].content
        if "You describe an image" in system:
            if vision_down:
                raise ProviderUnavailable("vlm down")
            return description
        if "organize a person's raw capture" in system:
            captured = messages[1].content  # "CAPTURE (data…):\n<<<\n<photo: …>\n>>>"
            node = {
                "title": "A saved photo",
                "type": "memory",
                "plane": "Ideas",
                "planes": ["Ideas"],
                "tags": ["photo"],
                "body": captured,
                "entities": [],
            }
            return json.dumps({"nodes": [node]})
        return "What made you save this?"

    return responder


def _make_image_pipeline(tmp_path: Path, *, vlm: FakeChatProvider | None = None, max_attempts=3):
    settings = Settings(
        graph_store_path=str(tmp_path / "store"),
        data_path=str(tmp_path / "data"),
        planes=["Professional", "Personal", "Ideas"],
        scheduler_tz="UTC",
        media_derive_max_attempts=max_attempts,
    )
    vlm = vlm or FakeChatProvider("fake-vlm", responder=_image_responder())
    stt = FakeSTTProvider(transcript="unused for image tests")
    registry = ProviderRegistry(
        {"fake-vlm": vlm, "fake-stt": stt},
        chat_chain=["fake-vlm"],
        distill_chain=["fake-vlm"],
        embedding_provider_id="none",
        stt_chain=["fake-stt"],
    )
    routing = fake_routing(registry, chain=("fake-vlm",))
    store = FakeCaptureStore()
    runs = FakeAgentRunStore()
    review = FakeReviewQueue()
    media_store = FakeMediaStore()
    media_files = MediaFiles(settings)
    media_derivation = MediaDerivationService(
        store=media_store,
        files=media_files,
        routing=routing,
        registry=registry,
        run_store=runs,
        max_attempts=max_attempts,
        rederive_max_per_run=50,
    )
    pipeline = CapturePipeline(
        settings=settings,
        store=store,
        routing=routing,
        registry=registry,
        node_writer=NodeWriter(str(tmp_path / "store")),
        store_backup=FakeStoreBackup(),
        run_store=runs,
        indexer=FakeIndexer(),
        entity_resolver=EntityResolver(
            settings=settings, alias_store=FakeAliasStore(), review_queue=review, routing=routing
        ),
        review_queue=review,
        media_store=media_store,
        media_files=media_files,
        media_derivation=media_derivation,
        node_media_store=FakeNodeMediaStore(),
    )
    return pipeline, store, media_store, runs, vlm, tmp_path / "store"


async def test_image_capture_happy_path(tmp_path: Path):
    pipeline, store, media_store, _runs, vlm, root = _make_image_pipeline(tmp_path)
    cid = await pipeline.create_image_capture(PNG_BYTES, filename="shot.png")
    await pipeline.drain()

    rec = store.records[cid]
    assert rec.kind == KIND_IMAGE
    assert rec.status == INDEXED
    # The derived description is fenced and stored as the capture's raw text (organize/reprocess
    # replay source) — the voice-transcript analogue for images.
    assert rec.raw_text == f"<photo: {_PHOTO_DESCRIPTION}>"
    assert rec.node_paths and (root / rec.node_paths[0]).exists()
    # The node's source is the capture kind (`image`), like text→text / voice→voice.
    assert "source: image" in (root / rec.node_paths[0]).read_text(encoding="utf-8")
    # No trailing nudge for an image (the "raw" is a derived description, not the person's words).
    assert rec.follow_up_question is None

    # Media derived: real description + the VLM model recorded, image kept + linked to the capture.
    media = await media_store.get_by_capture_id(cid)
    assert media.status == DERIVED
    assert media.derived_text == _PHOTO_DESCRIPTION
    assert media.kind == "photo" and media.source == "capture"
    assert media.file_path == f"capture/{cid}.png" and media.mime_type == "image/png"
    # The vision leg actually received the image (data URI) and the organize leg got the fence.
    assert any(imgs for imgs in vlm.images_seen if imgs)
    assert "<photo:" in vlm.last_messages[1].content  # last call = organize, over the fenced text
    # The content node is linked to its media via the derived-tier node_media (ADR-060 §1).
    assert any(m == media.id for _n, m in pipeline._node_media_store.links)


async def test_image_screenshot_description_is_fenced_into_organize(tmp_path: Path):
    # ADR-057 §5: a chat-screenshot description reaches organize wrapped as `<photo: …>` so the
    # organizer treats the contained messages as shared material, not the person's own words. We
    # assert the wiring (fence + the binding rule in the organizer prompt); LLM behaviour is not
    # under test.
    desc = 'Screenshot of a chat. Alex (left): "Friday?" Sam (right): "Yes, 7pm."'
    vlm = FakeChatProvider("fake-vlm", responder=_image_responder(desc))
    pipeline, store, _media, _runs, _vlm, _root = _make_image_pipeline(tmp_path, vlm=vlm)

    cid = await pipeline.create_image_capture(b"jpg", filename="s.jpg")
    await pipeline.drain()

    assert store.records[cid].raw_text == f"<photo: {desc}>"
    organize_user = vlm.last_messages[1].content
    assert desc in organize_user and "<photo:" in organize_user
    # The organizer system prompt carries the screenshot-attribution rule (ADR-057 §5 org layer).
    from app.capture.organizer import ORGANIZER_SYSTEM_PROMPT

    assert '"<photo: ...>"' in ORGANIZER_SYSTEM_PROMPT
    assert "never to" in ORGANIZER_SYSTEM_PROMPT.lower()


async def test_image_derivation_failure_walks_to_placeholder(tmp_path: Path):
    # A forced, persistent VLM failure on the describe leg walks retry → `unavailable` → the
    # explicit placeholder WITHOUT a human, and the pipeline is NOT blocked: the capture still
    # organizes (from the placeholder) and lands `indexed` (ADR-057 §3 acceptance).
    vlm = FakeChatProvider("fake-vlm", responder=_image_responder(vision_down=True))
    pipeline, store, media_store, _runs, _vlm, _root = _make_image_pipeline(
        tmp_path, vlm=vlm, max_attempts=3
    )

    cid = await pipeline.create_image_capture(b"png", filename="s.png")
    await pipeline.drain()

    media = await media_store.get_by_capture_id(cid)
    assert media.status == UNAVAILABLE
    assert media.attempts == 3  # bounded retries exhausted within the one capture run
    rec = store.records[cid]
    assert rec.raw_text == placeholder("photo")  # "<photo — description unavailable>"
    assert rec.status == INDEXED  # organize still ran (vision-down only fails the describe leg)
    assert rec.node_paths and not rec.node_paths[0].startswith("inbox/")


async def test_image_rederive_recovers_node_after_failure(tmp_path: Path):
    # The acceptance drill (08 §M9): a forced failure files a placeholder node; once the VLM is
    # back, kind-aware `rederive_capture` re-derives the photo AND rebuilds the node from the
    # recovered description — the recovery reaches the GRAPH, not just the media row (ADR-060 §5).
    vlm = FakeChatProvider("fake-vlm", responder=_image_responder(vision_down=True))
    pipeline, store, media_store, _runs, _vlm, root = _make_image_pipeline(
        tmp_path, vlm=vlm, max_attempts=2
    )
    cid = await pipeline.create_image_capture(b"png", filename="s.png")
    await pipeline.drain()
    assert (await media_store.get_by_capture_id(cid)).status == UNAVAILABLE
    assert store.records[cid].raw_text == placeholder("photo")
    stale_node = store.records[cid].node_paths[0]
    assert placeholder("photo") in (root / stale_node).read_text(encoding="utf-8")

    # VLM recovers; the capture-layer re-derive seam recovers the media AND rebuilds the node.
    vlm._responder = _image_responder("A recovered whiteboard photo.")
    await pipeline.rederive_capture(cid)
    await pipeline.drain()

    media = await media_store.get_by_capture_id(cid)
    assert media.status == DERIVED and media.derived_text == "A recovered whiteboard photo."
    rec = store.records[cid]
    assert rec.status == INDEXED
    assert rec.raw_text == "<photo: A recovered whiteboard photo.>"
    # The node was rebuilt from the recovered description — the placeholder is gone from the graph.
    body = (root / rec.node_paths[0]).read_text(encoding="utf-8")
    assert "recovered whiteboard photo" in body
    assert placeholder("photo") not in body


async def test_image_reprocess_reorganizes_stored_fence_without_revision(tmp_path: Path):
    # Reprocess-all replays an image capture from its stored fenced `raw_text` (organize-layer
    # replay) and does NOT re-run the VLM description — exactly as voice reprocess skips STT.
    pipeline, store, _media, _runs, vlm, root = _make_image_pipeline(tmp_path)
    cid = await pipeline.create_image_capture(PNG_BYTES, filename="shot.png")
    await pipeline.drain()
    describe_calls = sum(1 for imgs in vlm.images_seen if imgs)  # vision calls carry an image
    assert describe_calls == 1

    outcome = await pipeline.reprocess_capture(cid)

    assert outcome.ok and outcome.node_count >= 1
    # No new vision (describe) call — reprocess only re-organized the stored `<photo: …>` text.
    assert sum(1 for imgs in vlm.images_seen if imgs) == describe_calls
    assert store.records[cid].node_paths and (root / store.records[cid].node_paths[0]).exists()


async def test_image_capture_marks_deriving_status(tmp_path: Path):
    # The capture passes through `deriving` (the image sibling of `transcribing`) — observability.
    seen: list[str] = []
    pipeline, store, _media, _runs, _vlm, _root = _make_image_pipeline(tmp_path)
    orig = store.mark_status

    async def _record_status(capture_id, status):
        seen.append(status)
        await orig(capture_id, status)

    store.mark_status = _record_status
    await pipeline.create_image_capture(PNG_BYTES, filename="s.png")
    await pipeline.drain()
    assert DERIVING in seen
    assert seen.index(DERIVING) < seen.index(ORGANIZING)


async def test_image_capture_rejects_unsupported_type(tmp_path: Path):
    pipeline, *_ = _make_image_pipeline(tmp_path)
    with pytest.raises(UnsupportedImage):
        await pipeline.create_image_capture(b"x", filename="note.txt")


async def test_image_capture_rejects_oversize(tmp_path: Path):
    pipeline, *_ = _make_image_pipeline(tmp_path)
    # image_max_bytes defaults to 20 MB; a buffer one byte over is rejected before persistence.
    with pytest.raises(UnsupportedImage):
        await pipeline.create_image_capture(b"x" * (20 * 1024 * 1024 + 1), filename="huge.jpg")
