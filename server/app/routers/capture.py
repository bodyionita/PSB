"""Capture router (03-api.md §Capture, M1 / ADR-019).

Thin HTTP surface over :class:`CapturePipeline`: validate the request, delegate, translate the
pipeline's domain errors to status codes (CLAUDE.md rule 5 — routers validate + delegate, the
service owns the logic). Every write endpoint returns ``202``; the pipeline runs in-process in
the background (no broker) and the client polls ``GET /captures`` for status.

All routes require an authenticated session (03-api: only ``/auth/login`` and ``/health`` are
public) — enforced once at the router level.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from ..dependencies import get_capture_pipeline, require_session
from ..models import (
    CaptureAcceptedResponse,
    CaptureAnchorEditRequest,
    CaptureTextRequest,
    CaptureView,
    DraftPartView,
    DraftTextRequest,
    DraftView,
    FollowUpRequest,
)
from ..services.capture_pipeline import (
    CaptureNotFound,
    CapturePipeline,
    DraftNotOpen,
    EmptyDraft,
    FollowUpNotPending,
    NotRetryable,
    UnsupportedAudio,
    UnsupportedImage,
    VoicePartLimit,
)

router = APIRouter(tags=["capture"], dependencies=[Depends(require_session)])


@router.post(
    "/capture/text",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def capture_text(
    payload: CaptureTextRequest,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    capture_id = await pipeline.create_text_capture(payload.text, created_at=payload.created_at)
    return CaptureAcceptedResponse(capture_id=capture_id)


@router.post(
    "/capture/voice",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def capture_voice(
    file: UploadFile = File(...),
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    audio = await file.read()
    try:
        capture_id = await pipeline.create_voice_capture(audio, filename=file.filename or "audio")
    except UnsupportedAudio as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


@router.post(
    "/capture/image",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def capture_image(
    file: UploadFile = File(...),
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    """Ad-hoc PWA photo capture (M9 T3, ADR-057 §6): the raw image is kept under the media
    substrate, its vision description derived (resumable), then organized (fenced). ``202`` — the
    pipeline continues in the background; ``400`` on an unsupported type or an oversized upload."""
    image = await file.read()
    try:
        capture_id = await pipeline.create_image_capture(image, filename=file.filename or "image")
    except UnsupportedImage as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


# --- Composite draft lifecycle (M9.6 T1, ADR-061 §3) ---


@router.post("/capture/draft", response_model=DraftView)
async def open_draft(
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> DraftView:
    """Open a composite draft, or resume the one already open (one active draft — ADR-061 §3).
    Returns the draft's text body + ordinal-ordered parts so the compose screen can resume it."""
    record = await pipeline.open_or_resume_draft()
    parts = await pipeline.draft_parts(record.id)
    return DraftView.from_record(record, parts)


@router.post("/capture/{capture_id}/part", response_model=DraftPartView)
async def add_draft_part(
    capture_id: str,
    kind: str = Form(...),
    file: UploadFile = File(...),
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> DraftPartView:
    """Attach one media part (``kind`` = ``photo``/``voice``) to an open draft (ADR-061 §3). Raw
    persists immediately; derivation is deferred to Submit. ``409`` if not an open draft or a 2nd
    voice; ``400`` on a bad type/size; ``404`` unknown capture."""
    data = await file.read()
    try:
        media = await pipeline.add_draft_part(
            capture_id, data, filename=file.filename or kind, kind=kind
        )
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    except DraftNotOpen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="capture is not an open draft"
        ) from None
    except VoicePartLimit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a draft may carry at most one voice part"
        ) from None
    except (UnsupportedAudio, UnsupportedImage) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return DraftPartView.from_record(media)


@router.delete(
    "/capture/{capture_id}/part/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_draft_part(
    capture_id: str,
    media_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> None:
    """Remove a draft part — the 'x' (ADR-061 §3). Hard-removes raw + row. ``409`` if not an open
    draft; ``404`` unknown capture/part."""
    try:
        await pipeline.remove_draft_part(capture_id, media_id)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture or part not found"
        ) from None
    except DraftNotOpen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="capture is not an open draft"
        ) from None


@router.put("/capture/{capture_id}/text", response_model=DraftView)
async def edit_draft_text(
    capture_id: str,
    payload: DraftTextRequest,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> DraftView:
    """Edit the draft's typed text body (ADR-061 §3). ``409`` if not an open draft; ``404``
    unknown."""
    try:
        await pipeline.set_draft_text(capture_id, payload.text)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    except DraftNotOpen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="capture is not an open draft"
        ) from None
    record = await pipeline.get(capture_id)
    assert record is not None  # just edited it
    parts = await pipeline.draft_parts(capture_id)
    return DraftView.from_record(record, parts)


@router.post(
    "/capture/{capture_id}/submit",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def submit_draft(
    capture_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    """Submit a composite draft → blended organize (ADR-061 §3). ``202``; ``400`` if the draft has
    no non-empty part; ``409`` if not an open draft; ``404`` unknown."""
    try:
        await pipeline.submit_draft(capture_id)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    except EmptyDraft:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="draft has no content to submit"
        ) from None
    except DraftNotOpen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="capture is not an open draft"
        ) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


@router.delete(
    "/capture/{capture_id}/draft",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def discard_draft(
    capture_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> None:
    """Discard an open draft (ADR-061 §3 — the Discard action): removes every part + the row.
    ``409`` if not an open draft; ``404`` unknown."""
    try:
        await pipeline.discard_draft(capture_id)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    except DraftNotOpen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="capture is not an open draft"
        ) from None


@router.get("/captures", response_model=list[CaptureView])
async def list_captures(
    limit: int = Query(default=20, ge=1, le=100),
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> list[CaptureView]:
    records = await pipeline.list_recent(limit)
    return [CaptureView.from_record(r) for r in records]


@router.get("/captures/{capture_id}", response_model=CaptureView)
async def get_capture(
    capture_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureView:
    record = await pipeline.get(capture_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capture not found")
    return CaptureView.from_record(record)


@router.post(
    "/captures/{capture_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def retry_capture(
    capture_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    try:
        await pipeline.retry_capture(capture_id)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    except NotRetryable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="capture is not in a failed state"
        ) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


@router.put(
    "/captures/{capture_id}/anchor",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def edit_anchor(
    capture_id: str,
    payload: CaptureAnchorEditRequest,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    """The ADR-056 §5 **anchor edit**: correct a capture's recorded-at, then re-resolve its notes
    against the new anchor in the background (one-capture reorganize). ``202``; ``404`` unknown."""
    try:
        await pipeline.edit_anchor(capture_id, payload.anchor)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


@router.post(
    "/captures/{capture_id}/follow-up",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def submit_follow_up(
    capture_id: str,
    payload: FollowUpRequest,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    try:
        await pipeline.submit_follow_up(capture_id, payload.answer)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    except FollowUpNotPending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no pending follow-up question for this capture",
        ) from None
    return CaptureAcceptedResponse(capture_id=capture_id)
