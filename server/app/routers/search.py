"""Search router (03-api.md §Search & notes, M2 / ADR-022/023).

Thin HTTP surface over :class:`SearchService` (CLAUDE.md rule 5 — routers validate + delegate).
Both routes require an authenticated session (only ``/auth/login`` and ``/health`` are public).

``POST /search`` returns note-grouped cosine hits; a down embedder (single provider, no hot
fallback — ADR-022) maps to ``503`` since search can't run without the query embedding.
``GET /notes/{id}`` is a read-only preview; the ``uuid.UUID`` path type yields ``422`` on a
malformed id and ``404`` when the note is unknown.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import get_search_service, require_session
from ..models import (
    NotePreviewResponse,
    RelatedNoteItem,
    SearchRequest,
    SearchResultItem,
)
from ..providers.base import ProviderUnavailable
from ..search.service import SearchService

router = APIRouter(tags=["search"], dependencies=[Depends(require_session)])


@router.post("/search", response_model=list[SearchResultItem])
async def search(
    payload: SearchRequest,
    service: SearchService = Depends(get_search_service),
) -> list[SearchResultItem]:
    try:
        hits = await service.search(payload.query, top_k=payload.top_k, planes=payload.planes)
    except ProviderUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search is temporarily unavailable (embeddings)",
        ) from None
    return [
        SearchResultItem(
            note_id=hit.note_id,
            vault_path=hit.vault_path,
            title=hit.title,
            plane=hit.plane,
            planes=hit.planes,
            tags=hit.tags,
            snippet=hit.snippet,
            score=hit.score,
        )
        for hit in hits
    ]


@router.get("/notes/{note_id}", response_model=NotePreviewResponse)
async def get_note(
    note_id: uuid.UUID,
    service: SearchService = Depends(get_search_service),
) -> NotePreviewResponse:
    preview = await service.get_note(str(note_id))
    if preview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    return NotePreviewResponse(
        note_id=preview.note_id,
        vault_path=preview.vault_path,
        title=preview.title,
        plane=preview.plane,
        planes=preview.planes,
        tags=preview.tags,
        body=preview.body,
        related=[
            RelatedNoteItem(
                note_id=r.note_id, vault_path=r.vault_path, title=r.title, score=r.score
            )
            for r in preview.related
        ],
    )
