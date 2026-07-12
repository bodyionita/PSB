"""Capture router (03-api.md §Capture, M1 / ADR-019).

Thin HTTP surface over :class:`CapturePipeline`: validate the request, delegate, translate the
pipeline's domain errors to status codes (CLAUDE.md rule 5 — routers validate + delegate, the
service owns the logic). Every write endpoint returns ``202``; the pipeline runs in-process in
the background (no broker) and the client polls ``GET /captures`` for status.

All routes require an authenticated session (03-api: only ``/auth/login`` and ``/health`` are
public) — enforced once at the router level.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from ..dependencies import get_capture_pipeline, require_session
from ..models import (
    CaptureAcceptedResponse,
    CaptureTextRequest,
    CaptureView,
    FollowUpRequest,
)
from ..services.capture_pipeline import (
    CaptureNotFound,
    CapturePipeline,
    FollowUpNotPending,
    NotRetryable,
    UnsupportedAudio,
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
        capture_id = await pipeline.create_voice_capture(
            audio, filename=file.filename or "audio"
        )
    except UnsupportedAudio as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


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
