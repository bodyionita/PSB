"""Indexer service tests: temp store + fake index store + fake embedder (no DB, no live LLM).

Covers the 04 §4 contract: id-keyed upsert, whole-file hash (no ``sb:related`` carve-out — that
machinery is gone), mean-pool embedding, the mandatory ``search_document:`` prefix, canonical-edge
materialization (targets that exist), skip-and-continue on embed failure (→ ``partial``, rows
intact), and rescan deletion reconciliation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.indexing.indexer import Indexer
from app.indexing.store import NodeUpsert
from app.providers.registry import ProviderRegistry

from .fakes import FakeEmbeddingProvider, FakeIndexStore


class _BoomStore(FakeIndexStore):
    """FakeIndexStore that raises an unexpected error while upserting one path."""

    def __init__(self, boom_path: str) -> None:
        super().__init__()
        self._boom_path = boom_path

    async def upsert_node(self, node: NodeUpsert) -> None:  # type: ignore[override]
        if node.store_path == self._boom_path:
            raise RuntimeError("simulated store failure")
        await super().upsert_node(node)


def _node(store: FakeIndexStore, rel: str) -> NodeUpsert:
    return next(n for n in store.nodes.values() if n.store_path == rel)


def _has(store: FakeIndexStore, rel: str) -> bool:
    return any(n.store_path == rel for n in store.nodes.values())


_NODE = """---
id: 018f0001-aaaa
type: memory
created: 2026-07-12T09:14:03+02:00
source: text
plane: Ideas
planes: [Ideas]
tags: [thinking]
---

# A bright idea

The body of a bright idea worth remembering.
"""


def _make_indexer(
    tmp_path: Path,
    *,
    embedder: FakeEmbeddingProvider | None = None,
    store: FakeIndexStore | None = None,
    chunk_size: int = 1200,
    embed_max_attempts: int = 3,
) -> tuple[Indexer, FakeIndexStore, FakeEmbeddingProvider, Path]:
    root = tmp_path / "store"
    root.mkdir(exist_ok=True)
    embedder = embedder or FakeEmbeddingProvider(dim=4)
    store = store or FakeIndexStore()
    settings = Settings(
        graph_store_path=str(root),
        chunk_size=chunk_size,
        chunk_overlap=50,
        embed_max_attempts=embed_max_attempts,
        embed_retry_backoff_seconds=0.0,
    )
    registry = ProviderRegistry(
        {"fake-embed": embedder},
        chat_chain=[],
        distill_chain=[],
        embedding_provider_id="fake-embed",
        stt_chain=[],
    )
    return Indexer(settings=settings, store=store, registry=registry), store, embedder, root


def _write(root: Path, rel: str, text: str) -> str:
    path = root / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


async def test_index_one_node_creates_row_chunks_and_meanpool(tmp_path: Path):
    indexer, store, _, root = _make_indexer(tmp_path)
    rel = _write(root, "memory/2026-07-12--a-bright-idea--018f0001.md", _NODE)

    outcome = await indexer.index_paths([rel])

    assert (outcome.indexed, outcome.skipped, outcome.failed) == (1, 0, 0)
    assert not outcome.partial
    node = _node(store, rel)
    assert node.id == "018f0001-aaaa"
    assert node.type == "memory"
    assert node.title == "A bright idea"
    assert node.plane == "Ideas"
    assert node.tags == ["thinking"]
    assert node.content_hash == Indexer.content_hash(_NODE)
    assert node.chunks, "a non-empty node must produce chunks"
    dim = len(node.chunks[0].embedding)
    expected = [sum(c.embedding[i] for c in node.chunks) / len(node.chunks) for i in range(dim)]
    assert node.embedding == pytest.approx(expected)


async def test_embed_inputs_carry_search_document_prefix_and_title(tmp_path: Path):
    indexer, _, embedder, root = _make_indexer(tmp_path)
    rel = _write(root, "memory/idea--018f0001.md", _NODE)

    await indexer.index_paths([rel])

    assert embedder.inputs, "the embedder should have been called"
    for text in embedder.inputs[0]:
        assert text.startswith("search_document: A bright idea\n\n")


async def test_body_date_tokens_expanded_to_absolute_before_embedding(tmp_path: Path):
    # ADR-056 §4: the indexer expands [[t:…]] tokens to stable absolute language before chunking,
    # so the vectors (and the FTS tsvector generated off chunks.content) see prose, not token noise.
    indexer, store, embedder, root = _make_indexer(tmp_path)
    node = (
        "---\nid: 018f0002-bbbb\ntype: memory\ntitle: Trip\nplane: Ideas\n---\n"
        "We went hiking [[t:2025-06/2025-08|summer 2025]] and it was great.\n"
    )
    rel = _write(root, "memory/trip--018f0002.md", node)

    await indexer.index_paths([rel])

    embedded = "\n".join(embedder.inputs[0])
    assert "summer 2025" in embedded and "[[t:" not in embedded
    stored = _node(store, rel)
    assert all("[[t:" not in c.content for c in stored.chunks)


async def test_unchanged_node_is_skipped_on_reindex(tmp_path: Path):
    indexer, _, embedder, root = _make_indexer(tmp_path)
    rel = _write(root, "memory/idea--018f0001.md", _NODE)

    first = await indexer.index_paths([rel])
    calls_after_first = len(embedder.inputs)
    second = await indexer.index_paths([rel])

    assert first.indexed == 1
    assert (second.indexed, second.skipped) == (0, 1)
    assert len(embedder.inputs) == calls_after_first, "a skipped node must not be re-embedded"


async def test_any_edit_triggers_reindex_whole_file_hash(tmp_path: Path):
    indexer, store, _, root = _make_indexer(tmp_path)
    rel = "memory/idea--018f0001.md"
    _write(root, rel, _NODE)
    await indexer.index_paths([rel])
    old_hash = _node(store, rel).content_hash

    # A body edit reindexes (whole-file hash).
    _write(root, rel, _NODE.replace("worth remembering", "worth forgetting"))
    body_edit = await indexer.index_paths([rel])
    assert body_edit.indexed == 1
    assert _node(store, rel).content_hash != old_hash

    # A frontmatter edit (tags) also reindexes.
    _write(root, rel, _NODE.replace("tags: [thinking]", "tags: [thinking, ideas]"))
    tag_edit = await indexer.index_paths([rel])
    assert tag_edit.indexed == 1
    assert _node(store, rel).tags == ["thinking", "ideas"]


async def test_moved_file_is_a_path_update_not_delete_insert(tmp_path: Path):
    indexer, store, embedder, root = _make_indexer(tmp_path)
    _write(root, "memory/old--018f0001.md", _NODE)
    await indexer.index_paths(["memory/old--018f0001.md"])
    calls = len(embedder.inputs)

    # Same id, new path, unchanged content → the row's path updates, no re-embed.
    _write(root, "memory/new--018f0001.md", _NODE)
    (root / "memory" / "old--018f0001.md").unlink()
    outcome = await indexer.index_paths(["memory/new--018f0001.md"])

    assert outcome.skipped == 1
    assert len(embedder.inputs) == calls  # not re-embedded
    assert store.path_updates == [("018f0001-aaaa", "memory/new--018f0001.md")]


async def test_canonical_edges_materialize_only_for_existing_targets(tmp_path: Path):
    indexer, store, _, root = _make_indexer(tmp_path)
    person = "---\nid: 018f0002-pers\ntype: person\naliases: [Alex]\n---\n# Alex\n"
    memory = (
        "---\nid: 018f0001-mem\ntype: memory\nplane: Ideas\nplanes: [Ideas]\n"
        "edges:\n"
        "  - {rel: involves, to: 018f0002-pers, since: 2025-07-10}\n"
        "  - {rel: about, to: 018f9999-gone}\n"  # target does not exist → skipped
        "---\n# Dinner with Alex\n\nWe caught up over dinner.\n"
    )
    _write(root, "person/alex--018f0002.md", person)
    _write(root, "memory/dinner--018f0001.md", memory)

    outcome = await indexer.index_paths(["person/alex--018f0002.md", "memory/dinner--018f0001.md"])

    assert outcome.indexed == 2
    assert outcome.edges == 1  # only the edge to the existing person node materialized
    assert [e.dst_id for e in store.edges["018f0001-mem"]] == ["018f0002-pers"]


async def test_embed_failure_skips_and_marks_partial_leaving_rows_intact(tmp_path: Path):
    indexer, store, embedder, root = _make_indexer(tmp_path, embed_max_attempts=1)
    rel = "memory/idea--018f0001.md"
    _write(root, rel, _NODE)
    await indexer.index_paths([rel])
    good_hash = _node(store, rel).content_hash

    embedder._available = False
    _write(root, rel, _NODE.replace("bright idea", "dim idea"))
    outcome = await indexer.index_paths([rel])

    assert (outcome.indexed, outcome.failed) == (0, 1)
    assert outcome.partial
    assert outcome.failures == [rel]
    assert _node(store, rel).content_hash == good_hash, "stale row must be left intact"


async def test_empty_node_indexes_with_no_chunks_and_null_embedding(tmp_path: Path):
    indexer, store, embedder, root = _make_indexer(tmp_path)
    rel = _write(root, "memory/empty--018f0001.md", "---\nid: 018f0001-e\ntype: memory\n---\n")

    outcome = await indexer.index_paths([rel])

    assert outcome.indexed == 1
    assert _node(store, rel).chunks == []
    assert _node(store, rel).embedding is None
    assert embedder.inputs == []


async def test_missing_file_counts_as_failed(tmp_path: Path):
    indexer, store, _, _ = _make_indexer(tmp_path)
    outcome = await indexer.index_paths(["memory/gone.md"])
    assert outcome.failed == 1
    assert store.nodes == {}


async def test_batch_survives_unexpected_per_node_error(tmp_path: Path):
    store = _BoomStore("memory/bad--018f0002.md")
    indexer, store, _, root = _make_indexer(tmp_path, store=store)
    _write(root, "memory/bad--018f0002.md", _NODE.replace("018f0001-aaaa", "018f0002-bbbb"))
    _write(root, "memory/good--018f0001.md", _NODE)

    outcome = await indexer.index_paths(["memory/bad--018f0002.md", "memory/good--018f0001.md"])

    assert (outcome.indexed, outcome.failed) == (1, 1)
    assert outcome.partial
    assert outcome.failures == ["memory/bad--018f0002.md"]
    assert _has(store, "memory/good--018f0001.md")


async def test_reindex_all_scans_skips_ignored_and_reconciles_deletions(tmp_path: Path):
    indexer, store, _, root = _make_indexer(tmp_path)
    _write(root, "memory/one--018f0001.md", _NODE)
    _write(root, "idea/two--018f0002.md", _NODE.replace("018f0001-aaaa", "018f0002-bbbb"))
    _write(root, ".git/config.md", "# ignored")
    _write(root, "templates/node.md", "# ignored template")

    first = await indexer.reindex_all()
    assert first.indexed == 2
    assert {n.store_path for n in store.nodes.values()} == {
        "memory/one--018f0001.md",
        "idea/two--018f0002.md",
    }

    (root / "idea" / "two--018f0002.md").unlink()
    second = await indexer.reindex_all()
    assert second.deleted == 1
    assert {n.store_path for n in store.nodes.values()} == {"memory/one--018f0001.md"}
    assert second.skipped == 1  # the unchanged node
