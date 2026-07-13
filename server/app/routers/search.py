"""Search & graph router (03-api.md §Search & graph, M3 / ADR-022/026/030).

Thin HTTP surface over :class:`SearchService` (CLAUDE.md rule 5 — routers validate + delegate).
Both routes require an authenticated session (only ``/auth/login`` and ``/health`` are public).

``POST /search`` returns node-grouped cosine hits (``planes``/``types`` filters); a down embedder
(single provider, no hot fallback — ADR-022) maps to ``503`` since search can't run without the
query embedding. ``GET /nodes/{id}`` is a read-only detail view; the ``uuid.UUID`` path type
yields ``422`` on a malformed id and ``404`` when the node is unknown. A **tombstone** (a merged
node) ``302``-redirects to its survivor (ADR-030 §5).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..dependencies import get_search_service, require_session
from ..models import NodeDetailResponse, NodeEdgeItem, SearchRequest, SearchResultItem
from ..providers.base import ProviderUnavailable
from ..search.service import SearchService

router = APIRouter(tags=["search"], dependencies=[Depends(require_session)])


@router.post("/search", response_model=list[SearchResultItem])
async def search(
    payload: SearchRequest,
    service: SearchService = Depends(get_search_service),
) -> list[SearchResultItem]:
    try:
        hits = await service.search(
            payload.query, top_k=payload.top_k, planes=payload.planes, types=payload.types
        )
    except ProviderUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search is temporarily unavailable (embeddings)",
        ) from None
    return [
        SearchResultItem(
            node_id=hit.node_id,
            store_path=hit.store_path,
            type=hit.type,
            title=hit.title,
            plane=hit.plane,
            planes=hit.planes,
            tags=hit.tags,
            snippet=hit.snippet,
            score=hit.score,
        )
        for hit in hits
    ]


@router.get("/nodes/{node_id}", response_model=NodeDetailResponse)
async def get_node(
    node_id: uuid.UUID,
    request: Request,
    service: SearchService = Depends(get_search_service),
) -> NodeDetailResponse | RedirectResponse:
    preview = await service.get_node(str(node_id))
    if preview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    if preview.merged_into:
        # Tombstone: the node was merged away — redirect to the survivor (ADR-030 §5). The old id
        # keeps resolving so links never break.
        base = request.url.path.rsplit("/", 1)[0]
        return RedirectResponse(
            url=f"{base}/{preview.merged_into}", status_code=status.HTTP_302_FOUND
        )
    return NodeDetailResponse(
        node_id=preview.node_id,
        store_path=preview.store_path,
        type=preview.type,
        title=preview.title,
        plane=preview.plane,
        planes=preview.planes,
        tags=preview.tags,
        aliases=preview.aliases,
        disambig=preview.disambig,
        occurred=preview.occurred,
        occurred_end=preview.occurred_end,
        body=preview.body,
        profile=preview.profile,
        edges=[
            NodeEdgeItem(
                rel=e.rel,
                dir=e.dir,
                node_id=e.node_id,
                type=e.type,
                title=e.title,
                origin=e.origin,
                score=e.score,
                since=e.since,
                until=e.until,
            )
            for e in preview.edges
        ],
    )
