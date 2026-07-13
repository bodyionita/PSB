"""SearchService tests: fake store + fake embedder + tmp vault (no DB, no live LLM)."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.providers.registry import ProviderRegistry
from app.search.service import SearchService
from app.search.store import NoteRow, RelatedNote, SearchHit

from .fakes import FakeEmbeddingProvider, FakeSearchStore


def _make_service(
    tmp_path: Path,
    *,
    store: FakeSearchStore | None = None,
    embedder: FakeEmbeddingProvider | None = None,
    snippet_max: int = 400,
) -> tuple[SearchService, FakeSearchStore, FakeEmbeddingProvider, Path]:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    store = store or FakeSearchStore()
    embedder = embedder or FakeEmbeddingProvider(dim=4)
    settings = Settings(vault_path=str(vault), search_snippet_max_chars=snippet_max)
    registry = ProviderRegistry(
        {"fake-embed": embedder},
        chat_chain=[], distill_chain=[], embedding_provider_id="fake-embed", stt_chain=[],
    )
    return SearchService(settings=settings, store=store, registry=registry), store, embedder, vault


def _hit(note_id: str = "n1", snippet: str = "a snippet", score: float = 0.9) -> SearchHit:
    return SearchHit(
        note_id=note_id, vault_path="Ideas/x.md", title="X", plane="Ideas",
        planes=["Ideas"], tags=["t"], snippet=snippet, score=score,
    )


async def test_search_embeds_query_with_search_query_prefix(tmp_path: Path):
    service, store, embedder, _ = _make_service(tmp_path, store=FakeSearchStore(hits=[_hit()]))
    hits = await service.search("what did I decide about pricing")

    prefixed = "search_query: what did I decide about pricing"
    assert embedder.inputs == [[prefixed]]
    assert [h.note_id for h in hits] == ["n1"]
    # The query's embedding (FakeEmbeddingProvider → [len(text)] * dim) is what reaches the store.
    assert store.search_args["embedding"] == [float(len(prefixed))] * 4


async def test_search_clamps_top_k_to_configured_max(tmp_path: Path):
    service, store, _, _ = _make_service(tmp_path)
    await service.search("q", top_k=9999)
    assert store.search_args["top_k"] == 50  # SEARCH_MAX_TOP_K default

    await service.search("q", top_k=None)
    assert store.search_args["top_k"] == 10  # SEARCH_TOP_K_DEFAULT


async def test_search_empty_planes_becomes_no_filter(tmp_path: Path):
    service, store, _, _ = _make_service(tmp_path)
    await service.search("q", planes=[])
    assert store.search_args["planes"] is None

    await service.search("q", planes=["Ideas", "Health"])
    assert store.search_args["planes"] == ["Ideas", "Health"]


async def test_search_trims_long_snippet(tmp_path: Path):
    long_snippet = "word " * 200  # 1000 chars
    service, _, _, _ = _make_service(
        tmp_path, store=FakeSearchStore(hits=[_hit(snippet=long_snippet)]), snippet_max=50
    )
    hits = await service.search("q")
    assert len(hits[0].snippet) <= 51  # 50 + the ellipsis
    assert hits[0].snippet.endswith("…")


async def test_get_note_reads_body_from_vault_stripping_frontmatter(tmp_path: Path):
    note = NoteRow(
        note_id="n1", vault_path="Ideas/x.md", title="X", plane="Ideas", planes=["Ideas"],
        tags=["t"],
        related=[RelatedNote(note_id="n2", vault_path="Ideas/y.md", title="Y", score=0.7)],
    )
    service, _, _, vault = _make_service(tmp_path, store=FakeSearchStore(note=note))
    (vault / "Ideas").mkdir(parents=True)
    (vault / "Ideas" / "x.md").write_text(
        "---\nplane: Ideas\ntags: [t]\n---\n\n# X\n\nThe living body of X.\n", encoding="utf-8"
    )

    preview = await service.get_note("n1")

    assert preview is not None
    assert preview.body == "# X\n\nThe living body of X."  # frontmatter stripped, content kept
    assert [r.note_id for r in preview.related] == ["n2"]


async def test_get_note_unknown_returns_none(tmp_path: Path):
    service, _, _, _ = _make_service(tmp_path, store=FakeSearchStore(note=None))
    assert await service.get_note("missing") is None


async def test_get_note_missing_file_yields_empty_body(tmp_path: Path):
    note = NoteRow(
        note_id="n1", vault_path="Ideas/gone.md", title="X", plane="Ideas",
        planes=["Ideas"], tags=[], related=[],
    )
    service, _, _, _ = _make_service(tmp_path, store=FakeSearchStore(note=note))
    preview = await service.get_note("n1")
    assert preview is not None
    assert preview.body == ""  # degrades rather than 500s
