"""FastAPI dependency wiring.

App-scoped singletons (db, settings, registry, services) live on ``app.state`` and are
handed to routers through these dependencies. Routers never construct them directly.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from .config import Settings
from .db import Database
from .providers.registry import ProviderRegistry
from .search.service import SearchService
from .services.auth_service import AuthService, SessionInfo
from .services.capture_pipeline import CapturePipeline
from .services.vault_backup import VaultBackupService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_capture_pipeline(request: Request) -> CapturePipeline:
    return request.app.state.capture_pipeline


def get_vault_backup(request: Request) -> VaultBackupService:
    return request.app.state.vault_backup


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


async def require_session(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth: AuthService = Depends(get_auth_service),
) -> SessionInfo:
    """Gate for every authenticated endpoint. 401 on missing/expired/revoked session."""
    token = request.cookies.get(settings.session_cookie_name)
    session = await auth.validate(token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return session
