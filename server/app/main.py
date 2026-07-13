"""FastAPI application factory (ADR-003: single service).

Wires app-scoped singletons onto ``app.state`` in a lifespan, mounts routers under the API
prefix, and configures CORS for local dev. Migrations are NOT applied here — the request/
boot path only checks and warns (ADR-011).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .capture.notes import NoteWriter
from .config import Settings, get_settings
from .db import Database
from .graph.service import RelatednessGraph
from .graph.store import PgGraphStore
from .indexing.indexer import Indexer
from .indexing.store import PgIndexStore
from .migration_check import warn_if_behind_head
from .providers.registry import build_registry
from .routers import admin, auth, capture, health, search
from .search.service import SearchService
from .search.store import PgSearchStore
from .services.agent_runs import PgAgentRunStore
from .services.auth_service import AuthService
from .services.backup_jobs import build_backup_jobs
from .services.capture_pipeline import CapturePipeline
from .services.capture_store import PgCaptureStore
from .services.git_repo import GitRepo
from .services.rate_limit import RateLimiter
from .services.reindex import ReindexService
from .services.scheduler import BackupScheduler
from .services.vault_backup import VaultBackupService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings

    db = Database(settings)
    await db.connect()
    app.state.db = db

    app.state.registry = build_registry(settings)
    app.state.auth_service = AuthService(db, settings)
    app.state.login_rate_limiter = RateLimiter(
        max_events=settings.login_rate_limit_per_min, window_seconds=60.0
    )

    # Vault backup / durability (ADR-014): the one owner of git ops on the vault. ensure_ready
    # inits the repo if needed, pins gc/reflog, and bootstraps an empty vault's skeleton.
    vault_backup = VaultBackupService(settings=settings, git=GitRepo(settings.vault_path))
    await vault_backup.ensure_ready()
    app.state.vault_backup = vault_backup

    # Indexer (M2, ADR-022/023): the real index step — vault notes → notes/chunks. Owns
    # embed-on-index; the capture pipeline calls it to index freshly-written notes.
    indexer = Indexer(
        settings=settings, store=PgIndexStore(db), registry=app.state.registry
    )
    app.state.indexer = indexer

    # Relatedness graph (M2, ADR-023): recomputes note_links + renders the sb:related vault
    # blocks. Nightly-only + on /admin/reindex (via the reindex service); never on capture.
    graph = RelatednessGraph(
        settings=settings, store=PgGraphStore(db), vault_backup=vault_backup
    )
    app.state.relatedness_graph = graph

    # Reindex (M2, ADR-023 §4): the combined pass — git pull → reindex_all → recompute graph →
    # one commit+push. Single-flight, shared by the nightly job + POST /admin/reindex.
    reindex_service = ReindexService(
        settings=settings,
        indexer=indexer,
        graph=graph,
        vault_backup=vault_backup,
        run_store=PgAgentRunStore(db),
    )
    app.state.reindex_service = reindex_service

    # Search (M2, ADR-022/023): the read side — note-grouped cosine over chunks + note preview.
    app.state.search_service = SearchService(
        settings=settings, store=PgSearchStore(db), registry=app.state.registry
    )

    # Capture pipeline (M1, ADR-019): in-process, notes-to-vault, backed by the real vault backup.
    pipeline = CapturePipeline(
        settings=settings,
        store=PgCaptureStore(db),
        registry=app.state.registry,
        note_writer=NoteWriter(settings.vault_path),
        vault_backup=vault_backup,
        run_store=PgAgentRunStore(db),
        indexer=indexer,
    )
    app.state.capture_pipeline = pipeline

    await warn_if_behind_head(db)
    # Boot recovery: any capture left in-flight by a restart is marked failed (retryable).
    await pipeline.sweep_orphans()

    # Durability scheduler (ADR-010): the in-process APScheduler running the M1 backup jobs.
    # Off unless enable_scheduler — exactly one prod instance runs it. Started inside the
    # lifespan's event loop so the coroutine jobs fire on it.
    scheduler: BackupScheduler | None = None
    if settings.enable_scheduler:
        scheduler = BackupScheduler(
            settings=settings,
            jobs=build_backup_jobs(settings, db, vault_backup),
            reindex=reindex_service,
        )
        scheduler.start()
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        # Stop scheduling new jobs first (a job may enqueue a vault commit), then drain any
        # in-flight reindex + captures, flush the last pending commit, and drop the DB pool.
        if scheduler is not None:
            scheduler.shutdown()
        await reindex_service.drain()
        await pipeline.drain()
        await vault_backup.flush()
        await db.disconnect()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,  # cookies cross-origin in dev (web:5173 -> api:8000)
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # All endpoints live under /api/v1 (Caddy proxies /api -> FastAPI). 03-api.md.
    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(capture.router, prefix=settings.api_prefix)
    app.include_router(search.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)

    return app


app = create_app()
