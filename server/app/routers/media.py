"""Media serving router (03-api.md §Capture, M9 / ADR-057 §7). Session-gated, no LLM.

``GET /media/{id}`` streams a stored media file (photo / voice note) so the capture/node surfaces
and — at M9.5 — the session-transcript view can show photos inline and play voice notes. One
authenticated, session-gated endpoint over the local ``/srv/data/media`` volume (ADR-057 §3):
routers validate + delegate (rule 5), so this only resolves the row, guards existence, and streams.

A video row has no served file (``file_path`` NULL — summary-only, ADR-057 §2), so it 404s here;
its derived summary is read from the row by the transcript view, not streamed.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..dependencies import get_media_files, get_media_store, require_session
from ..services.media_store import MediaFiles, MediaStore

router = APIRouter(tags=["media"], dependencies=[Depends(require_session)])


@router.get("/media/{media_id}")
async def get_media(
    media_id: UUID,
    store: MediaStore = Depends(get_media_store),
    files: MediaFiles = Depends(get_media_files),
) -> FileResponse:
    """Stream a media file behind the session gate. ``404`` when the row is unknown, has no served
    file (video — summary-only), or the file is missing on disk."""
    record = await store.get(str(media_id))
    if record is None or not record.file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")
    if not await files.exists_async(record.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media file missing")
    path = files.absolute(record.file_path)
    return FileResponse(
        path,
        media_type=record.mime_type or "application/octet-stream",
        filename=path.name,
    )
