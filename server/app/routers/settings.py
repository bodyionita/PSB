"""Settings router (03-api.md §Settings). Session-gated, no LLM.

``GET /settings`` + ``PUT /settings/models`` (model routing, ADR-025 / ADR-043, M4 task 5) expose
the 3 routing groups (chat/conspect/quick) for the Settings → Models panel: the effective routing
per group (saved-over-seed) plus the registry's pickable models and their effort capability/levels
(registry-sourced — no hardcoded lists in the web). ``PUT`` saves one group and busts the routing
cache (forward-live); an unknown model id / bad effort level is a ``422``.

``PUT /settings/vocabulary`` (M3 task 7, ADR-027 / ADR-035) approves or rejects a pending node/edge
type proposal from the Settings → Vocabulary panel. It is a thin peer of ``POST /review/{id}`` over
the one governance choke point (:class:`VocabularyService`): approve writes the type to the live
vocabulary + opens the ``vocab-consolidation`` job, reject discards.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import get_model_routing, get_vocabulary_service, require_session
from ..models import (
    GroupRoutingModel,
    ModelRoutingUpdate,
    ReviewItemResponse,
    RoutingModelItem,
    SettingsResponse,
    VocabularyResolveRequest,
)
from ..services.model_routing import GroupSettings, ModelRoutingService, UnknownModel
from ..services.review_queue import BadResolution, ReviewNotFound, ReviewNotPending
from ..vocab.service import VocabularyService

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_session)])


@router.get("", response_model=SettingsResponse)
async def get_settings(
    routing: ModelRoutingService = Depends(get_model_routing),
) -> SettingsResponse:
    groups = await routing.all_settings()
    return SettingsResponse(groups=[_group_model(g) for g in groups])


@router.put("/models", response_model=GroupRoutingModel)
async def save_models(
    payload: ModelRoutingUpdate,
    routing: ModelRoutingService = Depends(get_model_routing),
) -> GroupRoutingModel:
    try:
        updated = await routing.save_group(
            payload.group,
            active=payload.active,
            fallback=payload.fallback,
            effort_by_model=payload.effort_by_model,
        )
    except UnknownModel as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return _group_model(updated)


def _group_model(g: GroupSettings) -> GroupRoutingModel:
    return GroupRoutingModel(
        group=g.group,
        active=g.active,
        fallback=g.fallback,
        effort_by_model=g.effort_by_model,
        models=[
            RoutingModelItem(
                id=m.id,
                provider=m.provider,
                label=m.label,
                supports_effort=m.supports_effort,
                effort_levels=m.effort_levels,
            )
            for m in g.models
        ],
    )


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
