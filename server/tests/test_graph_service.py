"""Relatedness-graph service tests: temp vault + fake graph store + fake backup (no live DB).

Covers the ADR-023 contract: note_links is materialized from the store's neighbours, the
sb:related block is rendered into each note body, the churn gate skips unchanged files (stable
graph ⇒ zero writes ⇒ zero commit), a note that lost its neighbours has its stale block stripped,
a missing file never crashes the run, and the tuned top-K / floor settings reach the store.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.graph.service import RelatednessGraph
from app.graph.store import NoteNeighbors, RelatedLink
from app.indexing.chunking import RELATED_BLOCK_START

from .fakes import FakeGraphStore, FakeVaultBackup

_NOTE = """---
id: cap-1
plane: Ideas
tags: [thinking]
---

# {title}

Some body text for {title}.
"""


def _write(vault: Path, rel: str, title: str = "A note") -> str:
    path = vault / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NOTE.format(title=title), encoding="utf-8")
    return rel


def _make(
    tmp_path: Path, *, store: FakeGraphStore, top_k: int = 5, min_score: float = 0.5
) -> tuple[RelatednessGraph, FakeVaultBackup, Path]:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    settings = Settings(vault_path=str(vault), related_top_k=top_k, related_min_score=min_score)
    backup = FakeVaultBackup()
    graph = RelatednessGraph(settings=settings, store=store, vault_backup=backup)
    return graph, backup, vault


def _read(vault: Path, rel: str) -> str:
    return (vault / Path(*rel.split("/"))).read_text(encoding="utf-8")


async def test_recompute_materializes_links_renders_blocks_and_requests_commit(tmp_path: Path):
    a, b = "Ideas/a.md", "Health/b.md"
    neighbors = [
        NoteNeighbors(
            note_id="id-a",
            vault_path=a,
            related=[RelatedLink(note_id="id-b", vault_path=b, title="B", score=0.7)],
        ),
        NoteNeighbors(
            note_id="id-b",
            vault_path=b,
            related=[RelatedLink(note_id="id-a", vault_path=a, title="A", score=0.7)],
        ),
    ]
    store = FakeGraphStore(neighbors=neighbors, paths=[a, b])
    graph, backup, vault = _make(tmp_path, store=store)
    _write(vault, a, "A")
    _write(vault, b, "B")

    outcome = await graph.recompute()

    assert store.written_links == neighbors  # canonical note_links materialized
    assert (outcome.links, outcome.blocks_written, outcome.notes) == (2, 2, 2)
    assert outcome.commit_requested
    assert backup.reasons == ["relatedness: 2 note(s) updated"]
    # Each note now carries the other's wikilink in a machine block.
    assert "[[Health/b|B]]" in _read(vault, a)
    assert "[[Ideas/a|A]]" in _read(vault, b)


async def test_stable_graph_is_zero_churn_on_the_second_run(tmp_path: Path):
    a, b = "Ideas/a.md", "Health/b.md"
    neighbors = [
        NoteNeighbors(
            note_id="id-a",
            vault_path=a,
            related=[RelatedLink(note_id="id-b", vault_path=b, title="B", score=0.7)],
        ),
    ]
    store = FakeGraphStore(neighbors=neighbors, paths=[a, b])
    graph, backup, vault = _make(tmp_path, store=store)
    _write(vault, a, "A")
    _write(vault, b, "B")

    first = await graph.recompute()
    second = await graph.recompute()

    assert first.blocks_written == 1  # only note a gained a block (b has no neighbours)
    assert second.blocks_written == 0  # nothing changed the second time (churn gate)
    assert not second.commit_requested
    assert backup.reasons == ["relatedness: 1 note(s) updated"]  # exactly one commit request


async def test_note_that_lost_neighbours_has_its_stale_block_stripped(tmp_path: Path):
    a, b = "Ideas/a.md", "Health/b.md"
    linked = [
        NoteNeighbors(
            note_id="id-a",
            vault_path=a,
            related=[RelatedLink(note_id="id-b", vault_path=b, title="B", score=0.7)],
        ),
    ]
    store = FakeGraphStore(neighbors=linked, paths=[a, b])
    graph, _, vault = _make(tmp_path, store=store)
    _write(vault, a, "A")
    _write(vault, b, "B")
    await graph.recompute()
    assert RELATED_BLOCK_START in _read(vault, a)

    # Next run: a has no neighbours (e.g. b was edited apart) → its block must be removed.
    store._neighbors = []
    store.written_links = None
    result = await graph.recompute()

    assert RELATED_BLOCK_START not in _read(vault, a)
    assert result.links == 0  # note_links cleared
    assert result.blocks_written == 1  # the stale block was stripped from a


async def test_missing_file_is_skipped_not_fatal(tmp_path: Path):
    present, gone = "Ideas/present.md", "Ideas/gone.md"
    neighbors = [
        NoteNeighbors(
            note_id="id-p",
            vault_path=present,
            related=[RelatedLink(note_id="id-g", vault_path=gone, title="Gone", score=0.9)],
        ),
    ]
    store = FakeGraphStore(neighbors=neighbors, paths=[present, gone])
    graph, backup, vault = _make(tmp_path, store=store)
    _write(vault, present, "Present")  # `gone` is intentionally never written

    outcome = await graph.recompute()

    assert outcome.blocks_written == 1  # only `present` was rewritten
    assert "[[Ideas/gone|Gone]]" in _read(vault, present)
    assert outcome.commit_requested


async def test_unreadable_file_is_skipped_and_run_continues(tmp_path: Path):
    # A present-but-unreadable note (bad encoding) must not abort the whole render (rule 7) — the
    # other notes still render and the run reports it as failed.
    good, bad = "Ideas/good.md", "Ideas/bad.md"
    neighbors = [
        NoteNeighbors(
            note_id="id-g",
            vault_path=good,
            related=[RelatedLink(note_id="id-b", vault_path=bad, title="Bad", score=0.9)],
        ),
        NoteNeighbors(
            note_id="id-b",
            vault_path=bad,
            related=[RelatedLink(note_id="id-g", vault_path=good, title="Good", score=0.9)],
        ),
    ]
    store = FakeGraphStore(neighbors=neighbors, paths=[good, bad])
    graph, backup, vault = _make(tmp_path, store=store)
    _write(vault, good, "Good")
    # Write bad.md as invalid UTF-8 so read_text(encoding="utf-8") raises UnicodeDecodeError.
    (vault / Path("Ideas") / "bad.md").write_bytes(b"\xff\xfe not valid utf-8 \x80")

    outcome = await graph.recompute()

    assert outcome.failed == 1
    assert outcome.blocks_written == 1  # good.md still rendered despite bad.md failing
    assert "[[Ideas/bad|Bad]]" in _read(vault, good)
    assert outcome.commit_requested
    assert backup.reasons == ["relatedness: 1 note(s) updated"]


async def test_tuned_top_k_and_floor_reach_the_store(tmp_path: Path):
    store = FakeGraphStore(neighbors=[], paths=[])
    graph, backup, _ = _make(tmp_path, store=store, top_k=3, min_score=0.42)

    outcome = await graph.recompute()

    assert store.compute_args == {"top_k": 3, "min_score": 0.42}
    assert (outcome.links, outcome.blocks_written) == (0, 0)
    assert not outcome.commit_requested  # empty graph ⇒ no commit
    assert backup.reasons == []
