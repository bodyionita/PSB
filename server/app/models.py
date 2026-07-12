"""Pydantic request/response models for the HTTP API (03-api.md).

These are the wire contract only; they are not DB models (there is no ORM).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# --- Auth ---
class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    authenticated: bool = True


class MeResponse(BaseModel):
    authenticated: bool
    session_created_at: datetime | None = None


# --- Health ---
class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    db: bool
    vault: bool
    git_remote: bool
