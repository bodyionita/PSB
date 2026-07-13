"""Search service — note-grouped semantic search + read-only note preview (03-api §Search, 04 §4).

Embeds the query with the **mandatory** ``search_query:`` nomic prefix (ADR-022 — the asymmetric
counterpart of the indexer's ``search_document:``), delegates the cosine ranking to the store, and
trims each hit's best chunk to a snippet. ``get_note`` reads the note **body from the vault file**
(fidelity — it reflects any Obsidian edits, not the indexed snapshot) and attaches the note's
``note_links`` neighbours (ADR-023).

No LLM call beyond the single query embedding; a down embedder surfaces as ``ProviderUnavailable``
for the router to map to ``503`` (this is a request path, not a background job).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..indexing.chunking import split_frontmatter
from ..providers.registry import ProviderRegistry
from .store import RelatedNote, SearchHit, SearchStore

# nomic asymmetric task prefix for the query side (ADR-022); the indexer uses ``search_document:``.
_QUERY_PREFIX = "search_query:"


@dataclass(frozen=True)
class NotePreview:
    """A note's metadata + vault-file body + semantic neighbours (GET /notes/{id})."""

    note_id: str
    vault_path: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    body: str
    related: list[RelatedNote]


class SearchService:
    def __init__(
        self, *, settings: Settings, store: SearchStore, registry: ProviderRegistry
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._vault_root = Path(settings.vault_path)

    async def search(
        self, query: str, *, top_k: int | None = None, planes: list[str] | None = None
    ) -> list[SearchHit]:
        """Rank notes by their best matching chunk (03-api §Search).

        Raises ``ProviderUnavailable`` if the query can't be embedded (single embedding provider,
        no hot fallback — ADR-022).
        """
        limit = self._clamp_top_k(top_k)
        result = await self._registry.embed([f"{_QUERY_PREFIX} {query}"])
        hits = await self._store.search_chunks(
            result.vectors[0],
            top_k=limit,
            planes=planes or None,  # an empty plane list means "no filter"
            min_score=self._settings.search_min_score,
        )
        return [self._trim_snippet(hit) for hit in hits]

    async def get_note(self, note_id: str) -> NotePreview | None:
        """Read-only preview: stored metadata + neighbours + the current vault-file body."""
        row = await self._store.get_note(note_id)
        if row is None:
            return None
        body = await asyncio.to_thread(self._read_body, row.vault_path)
        return NotePreview(
            note_id=row.note_id,
            vault_path=row.vault_path,
            title=row.title,
            plane=row.plane,
            planes=row.planes,
            tags=row.tags,
            body=body,
            related=row.related,
        )

    def _clamp_top_k(self, top_k: int | None) -> int:
        requested = top_k if top_k is not None else self._settings.search_top_k_default
        return max(1, min(requested, self._settings.search_max_top_k))

    def _trim_snippet(self, hit: SearchHit) -> SearchHit:
        limit = self._settings.search_snippet_max_chars
        if len(hit.snippet) <= limit:
            return hit
        trimmed = hit.snippet[:limit].rstrip() + "…"
        return SearchHit(
            note_id=hit.note_id,
            vault_path=hit.vault_path,
            title=hit.title,
            plane=hit.plane,
            planes=hit.planes,
            tags=hit.tags,
            snippet=trimmed,
            score=hit.score,
        )

    def _read_body(self, vault_path: str) -> str:
        """Body from the vault file (frontmatter stripped). ``""`` if the file is gone (stale row
        pre-reconciliation) — the preview degrades rather than 500s."""
        path = self._vault_root / Path(*vault_path.split("/"))
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return ""
        _, body = split_frontmatter(raw_text)
        return body.strip()
