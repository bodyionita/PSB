"""Pydantic request/response models for the HTTP API (03-api.md).

These are the wire contract only; they are not DB models (there is no ORM).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .services.capture_store import CaptureRecord


# --- Auth ---
class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    authenticated: bool = True


class MeResponse(BaseModel):
    authenticated: bool
    session_created_at: datetime | None = None


# --- Capture (03-api.md §Capture, M1 / ADR-019) ---
class CaptureTextRequest(BaseModel):
    text: str = Field(min_length=1)
    # Optional client-supplied capture time (e.g. an offline note synced later). When absent
    # the server stamps `now()`. Drives the vault-facing `created` frontmatter + filename date.
    created_at: datetime | None = None


class FollowUpRequest(BaseModel):
    answer: str = Field(min_length=1)


class CaptureAcceptedResponse(BaseModel):
    """202 body shared by the capture-accepting endpoints (text/voice/retry/follow-up)."""

    capture_id: str
    status: str = "received"


class CaptureView(BaseModel):
    """Pipeline state for the capture-screen strip / detail poll (03-api.md)."""

    capture_id: str
    kind: str
    status: str
    raw_text: str | None = None
    note_paths: list[str] = Field(default_factory=list)
    follow_up_question: str | None = None
    follow_up_answer: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_record(cls, record: CaptureRecord) -> CaptureView:
        return cls(
            capture_id=record.id,
            kind=record.kind,
            status=record.status,
            raw_text=record.raw_text,
            note_paths=list(record.note_paths),
            follow_up_question=record.follow_up_question,
            follow_up_answer=record.follow_up_answer,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


# --- Health ---
class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    db: bool
    vault: bool
    git_remote: bool
