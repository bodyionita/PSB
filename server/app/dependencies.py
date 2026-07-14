"""FastAPI dependency wiring.

App-scoped singletons (db, settings, registry, services) live on ``app.state`` and are
handed to routers through these dependencies. Routers never construct them directly.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from .config import Settings
from .db import Database
from .entities.merge import MergeService
from .graph.service import DerivedEdgeGraph
from .providers.registry import ProviderRegistry
from .search.service import SearchService
from .services.agent_runs import AgentRunStore
from .services.auth_service import AuthService, SessionInfo
from .services.capture_pipeline import CapturePipeline
from .services.reindex import ReindexService
from .services.review_service import ReviewService
from .services.store_backup import StoreBackupService
from .tags.service import TagConsolidationService
from .vocab.edge_consolidation import EdgeConsolidationService
from .vocab.service import VocabularyService


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


def get_store_backup(request: Request) -> StoreBackupService:
    return request.app.state.store_backup


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


def get_derived_edge_graph(request: Request) -> DerivedEdgeGraph:
    return request.app.state.derived_edge_graph


def get_reindex_service(request: Request) -> ReindexService:
    return request.app.state.reindex_service


def get_tag_consolidation_service(request: Request) -> TagConsolidationService:
    return request.app.state.tag_consolidation_service


def get_agent_run_store(request: Request) -> AgentRunStore:
    return request.app.state.agent_run_store


def get_review_service(request: Request) -> ReviewService:
    return request.app.state.review_service


def get_merge_service(request: Request) -> MergeService:
    return request.app.state.merge_service


def get_vocabulary_service(request: Request) -> VocabularyService:
    return request.app.state.vocabulary_service


def get_edge_consolidation_service(request: Request) -> EdgeConsolidationService:
    return request.app.state.edge_consolidation_service


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
