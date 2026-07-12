"""GET /health — no auth, never calls an LLM (ADR-012). 503 when degraded."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ..config import Settings
from ..db import Database
from ..dependencies import get_db, get_settings
from ..models import HealthResponse
from ..services.system_health import SystemHealth

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    report = await SystemHealth(db, settings).check()
    if not report.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if report.ok else "degraded",
        db=report.db,
        vault=report.vault,
        git_remote=report.git_remote,
    )
