"""Admin router (03-api.md §Agents & admin). Session-gated operational actions.

`POST /admin/backup` forces an immediate vault commit + push (ADR-014) — the manual counterpart
to the debounced write-batch commits and the nightly sweep.

`POST /admin/captures/{id}/reorganize` re-runs organize on a capture's stored raw text and
replaces its notes — a maintenance re-run (e.g. re-deriving notes after the organizer prompt
changed to English-only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import get_capture_pipeline, get_vault_backup, require_session
from ..models import BackupResponse, CaptureAcceptedResponse
from ..services.capture_pipeline import CaptureNotFound, CapturePipeline
from ..services.vault_backup import VaultBackupService

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_session)])


@router.post("/backup", response_model=BackupResponse)
async def backup(
    vault_backup: VaultBackupService = Depends(get_vault_backup),
) -> BackupResponse:
    result = await vault_backup.backup_now()
    return BackupResponse(committed=result.committed, pushed=result.pushed)


@router.post(
    "/captures/{capture_id}/reorganize",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def reorganize_capture(
    capture_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    """Re-organize a capture's stored raw text and replace its notes (202; runs in background)."""
    try:
        await pipeline.reorganize_capture(capture_id)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    return CaptureAcceptedResponse(capture_id=capture_id)
