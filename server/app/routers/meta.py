"""Meta router (03-api.md §Meta). Session-gated, no LLM.

``GET /planes`` exposes the configured plane vocabulary so the web's Search-tab filter chips
have a single API-clean source (ADR-005/006) — the web duplicates no server config.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import Settings
from ..dependencies import get_settings, require_session
from ..models import PlanesResponse

router = APIRouter(tags=["meta"], dependencies=[Depends(require_session)])


@router.get("/planes", response_model=PlanesResponse)
async def planes(settings: Settings = Depends(get_settings)) -> PlanesResponse:
    return PlanesResponse(planes=list(settings.planes), inbox=settings.inbox_plane)
