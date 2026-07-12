"""Admin router (03-api.md §Agents & admin). Session-gated operational actions.

`POST /admin/backup` forces an immediate vault commit + push (ADR-014) — the manual counterpart
to the debounced write-batch commits and the nightly sweep.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import get_vault_backup, require_session
from ..models import BackupResponse
from ..services.vault_backup import VaultBackupService

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_session)])


@router.post("/backup", response_model=BackupResponse)
async def backup(
    vault_backup: VaultBackupService = Depends(get_vault_backup),
) -> BackupResponse:
    result = await vault_backup.backup_now()
    return BackupResponse(committed=result.committed, pushed=result.pushed)
