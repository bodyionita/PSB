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

from .config import Settings, get_settings
from .db import Database
from .entities.backfill import BackfillService
from .entities.entity_store import PgEntityStore
from .entities.merge import MergeService
from .entities.profile_refresh import ProfileRefreshService
from .entities.profile_store import PgProfileStore
from .entities.resolver import EntityResolver
from .entities.store import PgAliasStore
from .graph.node_writer import NodeWriter
from .graph.service import DerivedEdgeGraph
from .graph.store import PgGraphStore
from .indexing.indexer import Indexer
from .indexing.store import PgIndexStore
from .migration_check import warn_if_behind_head
from .providers.registry import build_registry
from .routers import activity, admin, auth, capture, health, meta, review, search
from .routers import settings as settings_router
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
from .services.reprocess import PgReprocessStore, ReprocessService
from .services.review_queue import PgReviewQueue
from .services.review_service import ReviewService
from .services.scheduler import BackupScheduler
from .services.store_backup import StoreBackupService
from .tags.service import TagConsolidationService
from .tags.store import PgTagStore
from .vocab.consolidation import VocabConsolidation
from .vocab.edge_consolidation import EdgeConsolidationService
from .vocab.edge_store import PgEdgeConsolidationStore
from .vocab.service import VocabularyService
from .vocab.store import PgVocabularyStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings

    db = Database(settings)
    await db.connect()
    app.state.db = db

    app.state.registry = build_registry(settings)
    # One agent_runs store, shared by every background job/service that opens a run and by
    # GET /activity/runs/{id} (the Admin tab's run-status poll). Stateless over the pool.
    run_store = PgAgentRunStore(db)
    app.state.agent_run_store = run_store
    app.state.auth_service = AuthService(db, settings)
    app.state.login_rate_limiter = RateLimiter(
        max_events=settings.login_rate_limit_per_min, window_seconds=60.0
    )

    # Store backup / durability (ADR-014): the one owner of git ops on the graph store.
    # ensure_ready inits the repo if needed, wires the GRAPH_STORE_REPO remote, pins gc/reflog,
    # and bootstraps an empty store's node-type skeleton (ADR-031 §6).
    store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
    await store_backup.ensure_ready()
    app.state.store_backup = store_backup

    # Indexer (ADR-022/026): the real index step — node files → nodes/chunks + canonical edges.
    # Owns embed-on-index; the capture pipeline calls it to index freshly-written nodes. The index
    # store is shared with the review service (source-node store-path lookup for materializing).
    index_store = PgIndexStore(db)
    indexer = Indexer(settings=settings, store=index_store, registry=app.state.registry)
    app.state.indexer = indexer

    # The single filesystem writer of node files (ADR-026), shared by the capture pipeline and the
    # review service (which appends a materialized entity edge onto an existing node file).
    node_writer = NodeWriter(settings.graph_store_path)

    # Derived-edge graph (ADR-023 surviving half): recomputes the DB-only `similar` edges.
    # Nightly + on /admin/reindex (via the reindex service); never on the capture path.
    graph = DerivedEdgeGraph(settings=settings, store=PgGraphStore(db))
    app.state.derived_edge_graph = graph

    # Reindex (ADR-023 §4): the combined pass — git pull → reindex_all → recompute derived edges →
    # one commit+push. Single-flight, shared by the nightly job + POST /admin/reindex.
    reindex_service = ReindexService(
        settings=settings,
        indexer=indexer,
        graph=graph,
        store_backup=store_backup,
        run_store=run_store,
    )
    app.state.reindex_service = reindex_service

    # Search (ADR-022/026): the read side — node-grouped cosine over chunks + node detail w/ edges.
    app.state.search_service = SearchService(
        settings=settings, store=PgSearchStore(db), registry=app.state.registry
    )

    # Tags (ADR-024): the live tag vocabulary (organizer reuse) + the manual two-step consolidation
    # tool (propose → apply). One store; the consolidation service reuses the indexer + store
    # backup for the apply's rewrite-and-reindex.
    tag_store = PgTagStore(db)
    app.state.tag_store = tag_store
    app.state.tag_consolidation_service = TagConsolidationService(
        settings=settings,
        store=tag_store,
        registry=app.state.registry,
        indexer=indexer,
        store_backup=store_backup,
        run_store=run_store,
    )

    # Entity resolution (ADR-030): mentions → node ids over the alias index, filing review items
    # when it can't confidently resolve; the review queue also holds vocab proposals (ADR-027).
    review_queue = PgReviewQueue(db)
    app.state.review_queue = review_queue

    # Vocabulary governance (ADR-027 / ADR-035, M3 task 7): the effective vocabulary every writer
    # reads (config seeds ∪ approved additions in app_settings) + the approve/reject choke point
    # behind PUT /settings/vocabulary and POST /review/{id}. Approving a type opens a
    # vocab-consolidation run (VocabConsolidation). Constructed before the writers so it can be
    # threaded into each — a newly approved type is then recognised forward-live everywhere.
    vocabulary_service = VocabularyService(
        settings=settings,
        vocab_store=PgVocabularyStore(db),
        review_store=review_queue,
        consolidation=VocabConsolidation(run_store=run_store),
    )
    app.state.vocabulary_service = vocabulary_service

    # Edge retro-consolidation (ADR-036, M3 task 7b): the on-demand two-step
    # POST /admin/vocab/consolidate that re-types existing edges onto a newly approved rel. Reuses
    # the node writer + indexer + store backup for the apply's rewrite-and-reindex (store is truth).
    app.state.edge_consolidation_service = EdgeConsolidationService(
        settings=settings,
        store=PgEdgeConsolidationStore(db),
        node_writer=node_writer,
        registry=app.state.registry,
        indexer=indexer,
        store_backup=store_backup,
        run_store=run_store,
        vocab=vocabulary_service,
    )

    entity_resolver = EntityResolver(
        settings=settings,
        alias_store=PgAliasStore(db),
        review_queue=review_queue,
        registry=app.state.registry,
        vocab=vocabulary_service,
    )

    # Review read/resolve surface (ADR-030 §3, M3 task 4): lists pending items and resolves them —
    # materializing a pending entity edge onto the store (writer + reindex + commit); the
    # vocab-proposal branch is delegated to the Vocabulary service (task 7 — mutate live vocab +
    # open the consolidation job).
    app.state.review_service = ReviewService(
        settings=settings,
        review_store=review_queue,
        index_store=index_store,
        indexer=indexer,
        node_writer=node_writer,
        store_backup=store_backup,
        run_store=run_store,
        vocab=vocabulary_service,
    )

    # Entity services (ADR-030 §5/§6 + §4, M3 task 6). All share the one entity-read store; the
    # merge/backfill jobs share the node writer + indexer + store backup so they rewrite files then
    # reindex + force-commit (store is truth, rule 1).
    entity_store = PgEntityStore(db)
    app.state.merge_service = MergeService(
        settings=settings,
        entity_store=entity_store,
        node_writer=node_writer,
        indexer=indexer,
        store_backup=store_backup,
        run_store=run_store,
        vocab=vocabulary_service,
    )
    # Nightly profile-refresh (derived entity profiles → node_profiles, served by GET /nodes/{id})
    # and entity backfill (recent memories re-checked against touched entities' aliases).
    profile_refresh_service = ProfileRefreshService(
        settings=settings,
        entity_store=entity_store,
        profile_store=PgProfileStore(db),
        registry=app.state.registry,
        run_store=run_store,
        vocab=vocabulary_service,
    )
    app.state.profile_refresh_service = profile_refresh_service
    backfill_service = BackfillService(
        settings=settings,
        entity_store=entity_store,
        node_writer=node_writer,
        indexer=indexer,
        store_backup=store_backup,
        run_store=run_store,
        vocab=vocabulary_service,
    )
    app.state.backfill_service = backfill_service

    # Capture pipeline (ADR-019/026/030): in-process, nodes-to-store, backed by the real store
    # backup. The tag store feeds the organizer prompt the live vocabulary (ADR-024 §1).
    pipeline = CapturePipeline(
        settings=settings,
        store=PgCaptureStore(db),
        registry=app.state.registry,
        node_writer=node_writer,
        store_backup=store_backup,
        run_store=run_store,
        indexer=indexer,
        entity_resolver=entity_resolver,
        review_queue=review_queue,
        tag_vocabulary=tag_store,
        vocab=vocabulary_service,
    )
    app.state.capture_pipeline = pipeline

    # Reprocess-all-from-raw (ADR-042, M3 task 11): the standing data-survival op. Constructed after
    # the pipeline (it drives the pipeline's per-capture re-ingestion) + reuses the node writer,
    # derived-edge graph, and store backup for the reset → replay → recompute → force-commit pass.
    reprocess_service = ReprocessService(
        settings=settings,
        store=PgReprocessStore(db),
        reprocessor=pipeline,
        node_writer=node_writer,
        store_backup=store_backup,
        run_store=run_store,
        graph=graph,
    )
    app.state.reprocess_service = reprocess_service

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
            jobs=build_backup_jobs(settings, db, store_backup),
            reindex=reindex_service,
            profile_refresh=profile_refresh_service,
            backfill=backfill_service,
        )
        scheduler.start()
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        # Stop scheduling new jobs first (a job may enqueue a vault commit), then drain any
        # in-flight manual reindex + captures, flush the last pending commit, and drop the DB pool.
        # (A nightly reindex runs in the scheduler executor like the other jobs — best-effort on a
        # wait=False shutdown; it is idempotent and rule-7 wrapped, so a mid-flight one is safe.)
        if scheduler is not None:
            scheduler.shutdown()
        await reindex_service.drain()
        await reprocess_service.drain()
        await app.state.tag_consolidation_service.drain()
        await app.state.edge_consolidation_service.drain()
        await app.state.merge_service.drain()
        await pipeline.drain()
        await store_backup.flush()
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
    app.include_router(meta.router, prefix=settings.api_prefix)
    app.include_router(review.router, prefix=settings.api_prefix)
    app.include_router(settings_router.router, prefix=settings.api_prefix)
    app.include_router(activity.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)

    return app


app = create_app()
