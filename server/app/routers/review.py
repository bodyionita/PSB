"""Review router (03-api.md §Review queue). Session-gated, no LLM.

The minimal admin Review surface (M3 task 4, ADR-030 §3 / ADR-029):

  * ``GET /review?status=pending&kind=`` — the decidable-in-place items the pipeline filed;
  * ``POST /review/{id}`` — resolve one, dispatched by kind (entity-ambiguity ``choice`` /
    vocab-proposal ``verdict``). The business logic — materializing a pending edge onto the store,
    minting an entity, queuing vocab consolidation — lives in :class:`ReviewService` (rule 5). The
    polished review UX is M6; this is the read/resolve seam the write path (task 3) was waiting on.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import get_review_service, require_session
from ..models import ReviewItemResponse, ReviewResolveRequest
from ..services.review_queue import ReviewRecord
from ..services.review_service import (
    BadResolution,
    ReviewNotFound,
    ReviewNotPending,
    ReviewService,
)

router = APIRouter(prefix="/review", tags=["review"], dependencies=[Depends(require_session)])


def _to_response(record: ReviewRecord) -> ReviewItemResponse:
    return ReviewItemResponse(
        id=record.id,
        kind=record.kind,
        payload=record.payload,
        excerpt=record.excerpt,
        source=record.source,
        source_ref=record.source_ref,
        status=record.status,
        resolution=record.resolution,
        created_at=record.created_at,
    )


@router.get("", response_model=list[ReviewItemResponse])
async def list_review(
    status: str | None = "pending",
    kind: str | None = None,
    service: ReviewService = Depends(get_review_service),
) -> list[ReviewItemResponse]:
    """Newest-first review items. ``status`` defaults to ``pending`` (pass ``maybe`` to list parked,
    re-openable items; ``all`` or empty drops the filter). ``kind`` optionally narrows to one kind
    (``entity-ambiguity`` / ``vocab-proposal`` / ``stance-candidate`` / ``dedup-proposal``)."""
    items = await service.list_items(status=status, kind=kind)
    return [_to_response(item) for item in items]


@router.post("/{review_id}", response_model=ReviewItemResponse)
async def resolve_review(
    review_id: uuid.UUID,
    request: ReviewResolveRequest,
    service: ReviewService = Depends(get_review_service),
) -> ReviewItemResponse:
    """Resolve one item per its kind. ``422`` malformed id; ``404`` unknown; ``409`` already
    resolved; ``400`` a body invalid for the item's kind. Materializes the pending edge (entity
    pick/new) or queues vocab consolidation (approve) before flipping the item's status."""
    try:
        record = await service.resolve(
            str(review_id), choice=request.choice, verdict=request.verdict
        )
    except ReviewNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="review item not found"
        ) from None
    except ReviewNotPending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="review item already resolved"
        ) from None
    except BadResolution as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _to_response(record)
