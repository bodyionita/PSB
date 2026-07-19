"""FastAPI dependency wiring.

App-scoped singletons (db, settings, registry, services) live on ``app.state`` and are
handed to routers through these dependencies. Routers never construct them directly.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from .chat.auto_recorded import AutoRecordedService
from .chat.distiller import ChatDistillerService
from .chat.service import ChatService
from .config import Settings
from .db import Database
from .entities.entity_browse import EntityBrowseService
from .entities.merge import MergeService
from .graph.service import DerivedEdgeGraph, GraphService
from .identity.service import IdentityCapsuleService
from .oauth.service import OAuthService
from .providers.registry import ProviderRegistry
from .search.service import SearchService
from .services.agent_runs import AgentRunStore
from .services.auth_service import AuthService, SessionInfo
from .services.capture_pipeline import CapturePipeline
from .services.capture_removal import CaptureRemovalService
from .services.media_derivation import MediaDerivationService
from .services.media_store import MediaFiles, MediaStore
from .services.model_routing import ModelRoutingService
from .services.node_delete import NodeDeleteService
from .services.node_time_edit import NodeTimeEditService
from .services.reindex import ReindexService
from .services.reprocess import ReprocessService
from .services.review_service import ReviewService
from .services.run_logs import RunLogStore
from .services.store_backup import StoreBackupService
from .tags.service import TagConsolidationService
from .vocab.edge_consolidation import EdgeConsolidationService
from .vocab.service import VocabularyService


def client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit keying.

    Behind Cloudflare the trusted client address is ``CF-Connecting-IP`` — Cloudflare overwrites
    it on every request, so a client cannot forge it. ``X-Forwarded-For`` is client-*appendable*
    (the leftmost hop is attacker-controlled), so it is deliberately NOT used as the key: trusting
    it would let an attacker mint a fresh rate-limit bucket per request and defeat the brute-force
    guard on the login/consent password. Falls back to the peer socket for direct/dev access.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return request.client.host if request.client else "unknown"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_model_routing(request: Request) -> ModelRoutingService:
    return request.app.state.model_routing


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_oauth_service(request: Request) -> OAuthService:
    return request.app.state.oauth_service


def get_capture_pipeline(request: Request) -> CapturePipeline:
    return request.app.state.capture_pipeline


def get_capture_removal_service(request: Request) -> CaptureRemovalService:
    return request.app.state.capture_removal_service


def get_media_store(request: Request) -> MediaStore:
    return request.app.state.media_store


def get_media_files(request: Request) -> MediaFiles:
    return request.app.state.media_files


def get_media_derivation_service(request: Request) -> MediaDerivationService:
    return request.app.state.media_derivation_service


def get_store_backup(request: Request) -> StoreBackupService:
    return request.app.state.store_backup


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


def get_chat_distiller_service(request: Request) -> ChatDistillerService:
    return request.app.state.chat_distiller_service


def get_auto_recorded_service(request: Request) -> AutoRecordedService:
    return request.app.state.auto_recorded_service


def get_derived_edge_graph(request: Request) -> DerivedEdgeGraph:
    return request.app.state.derived_edge_graph


def get_graph_service(request: Request) -> GraphService:
    return request.app.state.graph_service


def get_reindex_service(request: Request) -> ReindexService:
    return request.app.state.reindex_service


def get_identity_capsule_service(request: Request) -> IdentityCapsuleService:
    return request.app.state.identity_capsule_service


def get_reprocess_service(request: Request) -> ReprocessService:
    return request.app.state.reprocess_service


def get_tag_consolidation_service(request: Request) -> TagConsolidationService:
    return request.app.state.tag_consolidation_service


def get_agent_run_store(request: Request) -> AgentRunStore:
    return request.app.state.agent_run_store


def get_run_log_store(request: Request) -> RunLogStore:
    return request.app.state.run_log_store


def get_review_service(request: Request) -> ReviewService:
    return request.app.state.review_service


def get_node_time_edit_service(request: Request) -> NodeTimeEditService:
    return request.app.state.node_time_edit_service


def get_entity_browse_service(request: Request) -> EntityBrowseService:
    return request.app.state.entity_browse_service


def get_merge_service(request: Request) -> MergeService:
    return request.app.state.merge_service


def get_node_delete_service(request: Request) -> NodeDeleteService:
    return request.app.state.node_delete_service


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
