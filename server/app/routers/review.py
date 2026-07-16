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

from ..config import Settings
from ..dependencies import get_review_service, get_settings, require_session
from ..models import (
    ReviewBatchRequest,
    ReviewBatchResponse,
    ReviewBatchResultItem,
    ReviewItemResponse,
    ReviewResolveRequest,
)
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


@router.post("/batch", response_model=ReviewBatchResponse)
async def resolve_batch(
    request: ReviewBatchRequest,
    service: ReviewService = Depends(get_review_service),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchResponse:
    """Resolve many items at once with one ``action`` (ADR-048 §8), **best-effort per item**: every
    id gets a result (``ok`` or a short ``error`` reason), and one bad item never fails the batch.
    Declared before ``/{review_id}`` so ``/review/batch`` isn't captured as a malformed uuid id.
    ``422`` if the batch exceeds ``review_batch_max`` (rule 9 — bounds a runaway request)."""
    if len(request.ids) > settings.review_batch_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"batch too large (max {settings.review_batch_max} ids)",
        )
    results = await service.resolve_batch([str(i) for i in request.ids], request.action)
    return ReviewBatchResponse(
        results=[ReviewBatchResultItem(id=r.id, ok=r.ok, error=r.error) for r in results]
    )


@router.post("/{review_id}", response_model=ReviewItemResponse)
async def resolve_review(
    review_id: uuid.UUID,
    request: ReviewResolveRequest,
    service: ReviewService = Depends(get_review_service),
) -> ReviewItemResponse:
    """Resolve one item per its kind. ``422`` malformed id; ``404`` unknown; ``409`` already
    resolved; ``400`` a body invalid for the item's kind. Materializes the pending edge (entity
    pick/new), queues vocab consolidation (approve), materializes the stance agree capture, or
    folds/links a dedup-proposal (ADR-049) before flipping the item's status."""
    try:
        record = await service.resolve(
            str(review_id),
            choice=request.choice,
            verdict=request.verdict,
            action=request.action,
            survivor=request.survivor,
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_response(record)
