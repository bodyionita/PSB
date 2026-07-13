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


# --- Search & notes (03-api.md §Search & notes, M2 / ADR-022/023) ---
class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    # Optional result count; the service clamps it to SEARCH_MAX_TOP_K. None ⇒ SEARCH_TOP_K_DEFAULT.
    top_k: int | None = Field(default=None, ge=1)
    # Filter on `notes.planes` (array overlap, not folder — ADR-005). None/[] = no filter.
    planes: list[str] | None = None


class SearchResultItem(BaseModel):
    """One note-grouped hit (best chunk = snippet), ranked by score (03-api §Search)."""

    note_id: str
    vault_path: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    snippet: str
    score: float


class RelatedNoteItem(BaseModel):
    """A semantic neighbour from `note_links` (ADR-023)."""

    note_id: str
    vault_path: str
    title: str | None = None
    score: float


class NotePreviewResponse(BaseModel):
    """Read-only note preview for the search UI expand (GET /notes/{id})."""

    note_id: str
    vault_path: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    body: str
    related: list[RelatedNoteItem] = Field(default_factory=list)


# --- Admin (03-api.md §Agents & admin) ---
class BackupResponse(BaseModel):
    """POST /admin/backup result — did this force a new commit, and did the push reach remote."""

    committed: bool
    pushed: bool


# --- Health ---
class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    db: bool
    vault: bool
    git_remote: bool
    backups: bool  # M1 (ADR-014 §6): latest integrity-drill fresh + not failed
