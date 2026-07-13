"""Indexer service tests: temp vault + fake index store + fake embedder (no DB, no live LLM).

Covers the 04 §3 contract: hash-skip, per-note upsert, mean-pool embedding, the mandatory
``search_document:`` prefix, skip-and-continue on embed failure (→ ``partial``, rows intact),
the ``sb:related`` block being excluded from the hash while frontmatter is kept, and rescan
deletion reconciliation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.indexing.chunking import RELATED_BLOCK_END, RELATED_BLOCK_START
from app.indexing.indexer import Indexer
from app.providers.registry import ProviderRegistry

from .fakes import FakeEmbeddingProvider, FakeIndexStore


class _BoomStore(FakeIndexStore):
    """FakeIndexStore that raises an unexpected (non-ProviderUnavailable) error for one path."""

    def __init__(self, boom_path: str) -> None:
        super().__init__()
        self._boom_path = boom_path

    async def upsert_note(self, note):  # type: ignore[override]
        if note.vault_path == self._boom_path:
            raise RuntimeError("simulated store failure")
        await super().upsert_note(note)


_NOTE = """---
id: cap-1
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
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    embedder = embedder or FakeEmbeddingProvider(dim=4)
    store = store or FakeIndexStore()
    settings = Settings(
        vault_path=str(vault),
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
    indexer = Indexer(settings=settings, store=store, registry=registry)
    return indexer, store, embedder, vault


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


async def test_index_one_note_creates_row_chunks_and_meanpool(tmp_path: Path):
    indexer, store, _, vault = _make_indexer(tmp_path)
    rel = _write(vault, "Ideas/2026-07-12 A bright idea.md", _NOTE)

    outcome = await indexer.index_paths([rel])

    assert (outcome.indexed, outcome.skipped, outcome.failed) == (1, 0, 0)
    assert not outcome.partial
    note = store.notes[rel]
    assert note.title == "A bright idea"
    assert note.plane == "Ideas"
    assert note.tags == ["thinking"]
    assert note.source == "text"
    assert note.content_hash == Indexer.content_hash(_NOTE)
    assert note.chunks, "a non-empty note must produce chunks"
    # notes.embedding = element-wise mean of the note's chunk vectors (no extra embed call).
    dim = len(note.chunks[0].embedding)
    expected = [
        sum(c.embedding[i] for c in note.chunks) / len(note.chunks) for i in range(dim)
    ]
    assert note.embedding == pytest.approx(expected)


async def test_embed_inputs_carry_search_document_prefix_and_title(tmp_path: Path):
    indexer, _, embedder, vault = _make_indexer(tmp_path)
    rel = _write(vault, "Ideas/2026-07-12 A bright idea.md", _NOTE)

    await indexer.index_paths([rel])

    assert embedder.inputs, "the embedder should have been called"
    for text in embedder.inputs[0]:
        assert text.startswith("search_document: A bright idea\n\n")


async def test_unchanged_note_is_skipped_on_reindex(tmp_path: Path):
    indexer, store, embedder, vault = _make_indexer(tmp_path)
    rel = _write(vault, "Ideas/2026-07-12 A bright idea.md", _NOTE)

    first = await indexer.index_paths([rel])
    calls_after_first = len(embedder.inputs)
    second = await indexer.index_paths([rel])

    assert first.indexed == 1
    assert (second.indexed, second.skipped) == (0, 1)
    assert len(embedder.inputs) == calls_after_first, "a skipped note must not be re-embedded"


async def test_changed_body_triggers_reindex(tmp_path: Path):
    indexer, store, _, vault = _make_indexer(tmp_path)
    rel = "Ideas/2026-07-12 A bright idea.md"
    _write(vault, rel, _NOTE)
    await indexer.index_paths([rel])
    old_hash = store.notes[rel].content_hash

    _write(vault, rel, _NOTE.replace("worth remembering", "worth forgetting"))
    outcome = await indexer.index_paths([rel])

    assert outcome.indexed == 1
    assert store.notes[rel].content_hash != old_hash


async def test_related_block_excluded_from_hash_but_frontmatter_kept(tmp_path: Path):
    indexer, store, _, vault = _make_indexer(tmp_path)
    rel = "Ideas/2026-07-12 A bright idea.md"
    _write(vault, rel, _NOTE)
    await indexer.index_paths([rel])

    # Appending the machine-managed sb:related block must NOT change the hash (graph writes
    # never re-trigger a reindex).
    related = f"\n{RELATED_BLOCK_START}\n## Related notes\n- [[Ideas/other]]\n{RELATED_BLOCK_END}\n"
    _write(vault, rel, _NOTE + related)
    after_related = await indexer.index_paths([rel])
    assert after_related.skipped == 1

    # But a frontmatter edit (tags) IS in the hash → reindex.
    _write(vault, rel, _NOTE.replace("tags: [thinking]", "tags: [thinking, ideas]"))
    after_tag_edit = await indexer.index_paths([rel])
    assert after_tag_edit.indexed == 1
    assert store.notes[rel].tags == ["thinking", "ideas"]


async def test_embed_failure_skips_and_marks_partial_leaving_rows_intact(tmp_path: Path):
    # First index succeeds.
    indexer, store, embedder, vault = _make_indexer(tmp_path, embed_max_attempts=1)
    rel = "Ideas/2026-07-12 A bright idea.md"
    _write(vault, rel, _NOTE)
    await indexer.index_paths([rel])
    good_hash = store.notes[rel].content_hash

    # Now the embedder is down and the note changed: the note is skipped-and-continued, the run
    # is partial, and the existing row is left untouched (a later reindex retries it).
    embedder._available = False
    _write(vault, rel, _NOTE.replace("bright idea", "dim idea"))
    outcome = await indexer.index_paths([rel])

    assert (outcome.indexed, outcome.failed) == (0, 1)
    assert outcome.partial
    assert outcome.failures == [rel]
    assert store.notes[rel].content_hash == good_hash, "stale row must be left intact"


async def test_empty_note_indexes_with_no_chunks_and_null_embedding(tmp_path: Path):
    indexer, store, embedder, vault = _make_indexer(tmp_path)
    # Frontmatter only, no body → no chunks, no embed call, embedding stays NULL.
    rel = _write(vault, "Ideas/empty.md", "---\nplane: Ideas\n---\n")

    outcome = await indexer.index_paths([rel])

    assert outcome.indexed == 1
    assert store.notes[rel].chunks == []
    assert store.notes[rel].embedding is None
    assert embedder.inputs == []


async def test_missing_file_counts_as_failed(tmp_path: Path):
    indexer, store, _, _ = _make_indexer(tmp_path)
    outcome = await indexer.index_paths(["Ideas/gone.md"])
    assert outcome.failed == 1
    assert store.notes == {}


async def test_batch_survives_unexpected_per_note_error(tmp_path: Path):
    # One note hitting an unexpected store error must not abort the batch (rule 7 / 04 §3).
    store = _BoomStore("Ideas/bad.md")
    indexer, store, _, vault = _make_indexer(tmp_path, store=store)
    _write(vault, "Ideas/bad.md", _NOTE)
    _write(vault, "Ideas/good.md", _NOTE)

    outcome = await indexer.index_paths(["Ideas/bad.md", "Ideas/good.md"])

    assert (outcome.indexed, outcome.failed) == (1, 1)
    assert outcome.partial
    assert outcome.failures == ["Ideas/bad.md"]
    assert "Ideas/good.md" in store.notes  # the good note still indexed


async def test_reindex_all_scans_skips_ignored_and_reconciles_deletions(tmp_path: Path):
    indexer, store, _, vault = _make_indexer(tmp_path)
    _write(vault, "Ideas/one.md", _NOTE)
    _write(vault, "Health/two.md", _NOTE.replace("Ideas", "Health"))
    _write(vault, ".obsidian/workspace.md", "# ignored")
    _write(vault, "templates/note.md", "# ignored template")

    first = await indexer.reindex_all()
    assert first.indexed == 2
    assert set(store.notes) == {"Ideas/one.md", "Health/two.md"}

    # Delete one file; a rescan removes its row (deletion reconciliation) and skips the unchanged.
    (vault / "Health" / "two.md").unlink()
    second = await indexer.reindex_all()
    assert second.deleted == 1
    assert set(store.notes) == {"Ideas/one.md"}
    assert second.skipped == 1  # Ideas/one.md unchanged
