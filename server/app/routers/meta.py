"""Meta router (03-api.md §Meta / §Search). Session-gated, no LLM.

``GET /planes`` exposes the configured plane vocabulary so the web's Search-tab filter chips
have a single API-clean source (ADR-005/006) — the web duplicates no server config.

``GET /types`` exposes the **effective** node/edge vocabulary (config seeds ∪ approved additions)
plus the pending type proposals, so the web's Settings → Vocabulary panel + the search/capture type
icons read one authoritative source (ADR-027, M3 task 7).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import Settings
from ..dependencies import get_settings, get_vocabulary_service, require_session
from ..models import PlanesResponse, TypesResponse, VocabProposalItem
from ..vocab.service import VocabularyService

router = APIRouter(tags=["meta"], dependencies=[Depends(require_session)])


@router.get("/planes", response_model=PlanesResponse)
async def planes(settings: Settings = Depends(get_settings)) -> PlanesResponse:
    return PlanesResponse(planes=list(settings.planes), inbox=settings.inbox_folder)


@router.get("/types", response_model=TypesResponse)
async def types(
    service: VocabularyService = Depends(get_vocabulary_service),
) -> TypesResponse:
    """The effective node/edge vocabulary + pending proposals (ADR-027, GET /types)."""
    view = await service.list_types()
    return TypesResponse(
        node_types=list(view.node_types),
        edge_rels=list(view.edge_rels),
        entity_like_types=list(view.entity_like_types),
        proposals=[VocabProposalItem(**p) for p in view.proposals],
    )
