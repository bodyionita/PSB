"""Settings router (03-api.md §Settings). Session-gated, no LLM.

``PUT /settings/vocabulary`` (M3 task 7, ADR-027 / ADR-035) approves or rejects a pending
node/edge type proposal from the Settings → Vocabulary panel. It is a thin peer of
``POST /review/{id}`` over the one governance choke point (:class:`VocabularyService`): approve
writes the type to the live vocabulary + opens the ``vocab-consolidation`` job, reject discards.

``GET /settings`` + ``PUT /settings/models`` (model routing, ADR-025) arrive with M4 chat; this
router holds only the vocabulary seam for now.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import get_vocabulary_service, require_session
from ..models import ReviewItemResponse, VocabularyResolveRequest
from ..services.review_queue import BadResolution, ReviewNotFound, ReviewNotPending
from ..vocab.service import VocabularyService

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_session)])


@router.put("/vocabulary", response_model=ReviewItemResponse)
async def resolve_vocabulary(
    request: VocabularyResolveRequest,
    service: VocabularyService = Depends(get_vocabulary_service),
) -> ReviewItemResponse:
    """Approve/reject a pending type proposal (ADR-027). ``404`` unknown item; ``409`` already
    resolved; ``400`` not a vocab proposal or a bad verdict. Approve mutates the live vocabulary +
    opens the consolidation job before flipping the item's status."""
    try:
        record = await service.resolve_proposal(request.review_id, request.verdict)
    except ReviewNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="vocabulary proposal not found"
        ) from None
    except ReviewNotPending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="proposal already resolved"
        ) from None
    except BadResolution as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
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
