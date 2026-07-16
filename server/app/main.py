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

from .chat.auto_recorded import AutoRecordedService, PgAutoRecordedStore
from .chat.distill_store import PgChatDistillStore
from .chat.distiller import ChatDistillerService
from .chat.service import build_chat_service
from .chat.store import PgChatStore
from .config import Settings, get_settings
from .db import Database
from .dedup.store import PgDedupStore
from .dedup.sweep import DedupSweepService
from .entities.backfill import BackfillService
from .entities.entity_store import PgEntityStore
from .entities.merge import MergeService
from .entities.merge_core import MergeCore
from .entities.profile_refresh import ProfileRefreshService
from .entities.profile_store import PgProfileStore
from .entities.resolver import EntityResolver
from .entities.store import PgAliasStore
from .graph.node_writer import NodeWriter
from .graph.service import DerivedEdgeGraph, GraphService
from .graph.store import PgGraphStore, PgNeighborStore
from .identity.service import IdentityCapsuleService
from .identity.store import PgCapsuleSourceStore, PgIdentityCapsuleStore
from .inbox.drain import InboxDrainService
from .indexing.indexer import Indexer
from .indexing.store import PgIndexStore
from .mcp.server import build_mcp_server
from .migration_check import warn_if_behind_head
from .oauth.service import OAuthService
from .oauth.store import PgOAuthStore
from .providers.registry import build_registry
from .routers import activity, admin, auth, capture, chat, health, meta, oauth, review, search
from .routers import settings as settings_router
from .search.service import SearchService
from .search.store import PgSearchStore
from .services.agent_runs import PgAgentRunStore
from .services.auth_service import AuthService
from .services.backup_jobs import build_backup_jobs
from .services.capture_pipeline import CapturePipeline
from .services.capture_store import PgCaptureStore
from .services.git_repo import GitRepo
from .services.maybe_digest import MaybeDigestService
from .services.model_routing import build_model_routing
from .services.rate_limit import RateLimiter
from .services.reindex import ReindexService
from .services.reprocess import PgReprocessStore, ReprocessService
from .services.review_queue import PgReviewQueue
from .services.review_service import ReviewService
from .services.scheduler import PipelineScheduler
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
    # Model routing brain (ADR-025 / ADR-043): resolves the `chat`/`conspect`/`quick` groups from
    # config seeds overlaid with saved `app_settings.model_routing`, threading per-provider effort.
    # Every conspect distillation call site funnels through this; the registry stays pure mechanics.
    app.state.model_routing = build_model_routing(settings, db, app.state.registry)
    # One agent_runs store, shared by every background job/service that opens a run and by
    # GET /activity/runs/{id} (the Admin tab's run-status poll). Stateless over the pool.
    run_store = PgAgentRunStore(db)
    app.state.agent_run_store = run_store
    app.state.auth_service = AuthService(db, settings)
    app.state.login_rate_limiter = RateLimiter(
        max_events=settings.login_rate_limit_per_min, window_seconds=60.0
    )
    # MCP OAuth 2.1 authorization server (M5 task 3, ADR-046 §2): open DCR, the password+consent
    # `/authorize` gate (reuses the auth service + login limiter above), `/token` (code exchange +
    # sliding refresh rotation), and the revoke-all switch. Opaque HMAC-hashed DB tokens; the MCP
    # server (task 4) reuses `validate_access_token` as its bearer gate. Root-level routes below.
    app.state.oauth_service = OAuthService(settings=settings, store=PgOAuthStore(db))

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

    # Identity capsule (M5 task 2, ADR-046 §5 / ADR-033 #1): the derived ~300-token "who the user
    # is" blob. The store is the cheap read side (build_context L0 + the chat system prompt below);
    # the distiller service owns the nightly/on-demand refresh (wired into the scheduler below).
    capsule_store = PgIdentityCapsuleStore(db)
    # Exposed on app.state so the MCP `identity://me` resource (task 4) can read the capsule blob
    # directly (up-front grounding without picking a node).
    app.state.identity_capsule_store = capsule_store
    app.state.identity_capsule_service = IdentityCapsuleService(
        settings=settings,
        capsule_store=capsule_store,
        sources=PgCapsuleSourceStore(db),
        routing=app.state.model_routing,
        run_store=run_store,
    )

    # Graph reads (M5 task 1, ADR-046/028/032): the cursor-paginated one-hop `traverse` primitive +
    # `build_context` bundle. Thin over the edges table; reuses the search service for get_node +
    # the capsule store for the L0 identity capsule. The MCP tools (task 4) + the M7 map endpoint
    # delegate here — no traversal logic of their own.
    app.state.graph_service = GraphService(
        settings=settings,
        store=PgNeighborStore(db),
        nodes=app.state.search_service,
        capsule=capsule_store,
    )

    # Chat (M4 task 3, ADR-025): grounded chat over the graph — condense → hybrid retrieval (via the
    # search service) → fenced prompt (+ the identity capsule as up-front grounding, M5 task 2) →
    # cited-only answer → persistence, with best-effort quick-tier titling. Wired here so it can be
    # drained on shutdown; the routers (task 4) delegate to it.
    app.state.chat_service = build_chat_service(
        settings, PgChatStore(db), app.state.model_routing, app.state.search_service, capsule_store
    )

    # Tags (ADR-024): the live tag vocabulary (organizer reuse) + the manual two-step consolidation
    # tool (propose → apply). One store; the consolidation service reuses the indexer + store
    # backup for the apply's rewrite-and-reindex.
    tag_store = PgTagStore(db)
    app.state.tag_store = tag_store
    app.state.tag_consolidation_service = TagConsolidationService(
        settings=settings,
        store=tag_store,
        routing=app.state.model_routing,
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
        routing=app.state.model_routing,
        indexer=indexer,
        store_backup=store_backup,
        run_store=run_store,
        vocab=vocabulary_service,
    )

    entity_resolver = EntityResolver(
        settings=settings,
        alias_store=PgAliasStore(db),
        review_queue=review_queue,
        routing=app.state.model_routing,
        vocab=vocabulary_service,
    )

    # Entity services (ADR-030 §5/§6 + §4, M3 task 6). All share the one entity-read store; the
    # merge/backfill jobs share the node writer + indexer + store backup so they rewrite files then
    # reindex + force-commit (store is truth, rule 1).
    entity_store = PgEntityStore(db)
    # The shared merge-core (ADR-049 §1): retarget inbound edges → tombstone loser → reindex →
    # force commit+push. Entity-merge composes it with an alias-union; content-merge (dedup
    # resolution, below) folds with it alone. One instance shared by both callers.
    merge_core = MergeCore(
        entity_store=entity_store,
        node_writer=node_writer,
        indexer=indexer,
        store_backup=store_backup,
    )
    app.state.merge_service = MergeService(
        settings=settings,
        entity_store=entity_store,
        node_writer=node_writer,
        merge_core=merge_core,
        run_store=run_store,
        vocab=vocabulary_service,
    )
    # Nightly dedup sweep (M6 task 5, ADR-049): near-duplicate content nodes file a dedup-proposal
    # the user resolves (merge/keep/link) via the Review surface below. DB-only (candidate reads +
    # review-queue writes); scheduled as a `nightly` pipeline step in M6 task 8.
    app.state.dedup_sweep_service = DedupSweepService(
        settings=settings,
        dedup_store=PgDedupStore(db),
        review_queue=review_queue,
        run_store=run_store,
        vocab=vocabulary_service,
    )
    # Nightly profile-refresh (derived entity profiles → node_profiles, served by GET /nodes/{id})
    # and entity backfill (recent memories re-checked against touched entities' aliases).
    profile_refresh_service = ProfileRefreshService(
        settings=settings,
        entity_store=entity_store,
        profile_store=PgProfileStore(db),
        routing=app.state.model_routing,
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
    # backup. The tag store feeds the organizer prompt the live vocabulary (ADR-024 §1). One shared
    # capture store (reused by the one-tap-remove op's node_paths lookup, M6 task 4).
    capture_store = PgCaptureStore(db)
    pipeline = CapturePipeline(
        settings=settings,
        store=capture_store,
        routing=app.state.model_routing,
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

    # Review read/resolve surface (ADR-030 §3, M3 task 4 + M6 task 2): lists decidable items and
    # resolves them — materializing a pending entity edge onto the store (write + reindex + commit),
    # the vocab-proposal branch is delegated to the Vocabulary service (task 7 — mutate live vocab +
    # open the consolidation job); the M6 `stance-candidate` **agree** materializes a `source=chat`
    # capture through the pipeline (the exact auto-endorse path, ADR-048 §7), so it is built after
    # the pipeline it depends on.
    app.state.review_service = ReviewService(
        settings=settings,
        review_store=review_queue,
        index_store=index_store,
        indexer=indexer,
        node_writer=node_writer,
        store_backup=store_backup,
        run_store=run_store,
        vocab=vocabulary_service,
        chat_ingest=pipeline,
        # Dedup-proposal resolution (M6 task 5, ADR-049): `merge` folds via the shared core (built
        # above), `entity_store` fetches the pair; `link`/`keep` use the node writer/index already
        # wired. The Review surface is where a dedup decision becomes graph structure.
        entity_store=entity_store,
        merge_core=merge_core,
    )

    # Auto-recorded registry (M6 task 4, ADR-048 §11/§12): backs the chat-scoped "recently auto-
    # recorded" audit list + the one-tap-remove op. The distiller's endorsed branch records into it;
    # the remove op tombstones the capture + deletes the content nodes (hubs preserved, ADR-038).
    auto_recorded_store = PgAutoRecordedStore(
        db, snippet_max=settings.search_snippet_max_chars
    )
    app.state.auto_recorded_service = AutoRecordedService(
        settings=settings,
        store=auto_recorded_store,
        captures=capture_store,
        index_store=index_store,
        node_writer=node_writer,
        store_backup=store_backup,
        vocab=vocabulary_service,
    )

    # Chat-distiller (M6, ADR-048): the stance-gated pass that turns idle chat sessions into
    # memories. Scheduled as a nightly pipeline step (task 8) and driven on demand by
    # `POST /chat/sessions/{id}/remember` (task 3). Built after the pipeline — its endorsed branch
    # materializes `source=chat` captures through it (the single writer, ADR-048 §1) — reusing the
    # shared review queue / routing / run store, and recording each auto-endorse in the audit
    # registry (task 4).
    app.state.chat_distiller_service = ChatDistillerService(
        settings=settings,
        distill_store=PgChatDistillStore(db),
        ingest=pipeline,
        review_queue=review_queue,
        routing=app.state.model_routing,
        run_store=run_store,
        auto_recorded=auto_recorded_store,
    )

    # Inbox drainer (M6 task 6, ADR-048 §10): a nightly step that re-organizes `inbox/`-materialized
    # captures against the now-richer registry. It drives the SAME shared capture pipeline (the
    # single writer, rule 2b) + capture store — no second pipeline — so its on-success commits ride
    # the long-lived store-backup debounce. Scheduled as a `nightly` pipeline step (task 8).
    app.state.inbox_drain_service = InboxDrainService(
        settings=settings,
        capture_store=capture_store,
        pipeline=pipeline,
        run_store=run_store,
    )

    # Maybe-digest (M6 task 8, ADR-048 §8): a weekly step emitting one feed-visible `agent_run`
    # summarizing the parked `maybe` review items (an untriaged pile stalls the feature). DB-only (a
    # read over `review_queue` + its own run row). Scheduled as a `weekly` pipeline step (task 8).
    app.state.maybe_digest_service = MaybeDigestService(
        settings=settings,
        store=review_queue,
        run_store=run_store,
    )

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
        # Rebuild the derived profiles the reset truncates, so the profile search leg (ADR-037) is
        # live right after a reprocess instead of empty until the nightly job.
        profile_refresh=profile_refresh_service,
    )
    app.state.reprocess_service = reprocess_service

    await warn_if_behind_head(db)
    # Boot recovery: any capture left in-flight by a restart is marked failed (retryable).
    await pipeline.sweep_orphans()

    # Pipeline scheduler (ADR-047): the in-process APScheduler running the `nightly`/`weekly`
    # pipelines — one cron per pipeline, steps sequential-on-completion (the ADR-010 window is now
    # enforced by sequencing from a 03:00 start, not the retired per-job stagger). Off unless
    # enable_scheduler — exactly one prod instance runs it. Started inside the lifespan's event loop
    # so the coroutine steps fire on it.
    scheduler: PipelineScheduler | None = None
    if settings.enable_scheduler:
        scheduler = PipelineScheduler(
            settings=settings,
            jobs=build_backup_jobs(settings, db, store_backup),
            run_store=run_store,
            reindex=reindex_service,
            profile_refresh=profile_refresh_service,
            backfill=backfill_service,
            identity_capsule=app.state.identity_capsule_service,
            chat_distiller=app.state.chat_distiller_service,
            inbox_drain=app.state.inbox_drain_service,
            dedup_sweep=app.state.dedup_sweep_service,
            maybe_digest=app.state.maybe_digest_service,
        )
        scheduler.start()
    app.state.scheduler = scheduler

    # The MCP Streamable HTTP session manager (task 4) must run for the app's lifetime — it owns the
    # transport's task group. Built + mounted in create_app; here we just enter its run() context so
    # `/mcp` is live, and exit it on shutdown (before draining the rest).
    mcp_manager = app.state.mcp_server.session_manager
    async with mcp_manager.run():
        try:
            yield
        finally:
            # Stop scheduling new jobs first (a job may enqueue a vault commit), then drain any
            # in-flight manual reindex + captures, flush the last pending commit, drop the DB pool.
            # (A nightly reindex runs in the scheduler executor like the other jobs — best-effort on
            # a wait=False shutdown; it is idempotent and rule-7 wrapped, so a mid-flight one is
            # safe.)
            if scheduler is not None:
                scheduler.shutdown()
            await reindex_service.drain()
            await reprocess_service.drain()
            await app.state.tag_consolidation_service.drain()
            await app.state.edge_consolidation_service.drain()
            await app.state.merge_service.drain()
            await app.state.chat_service.drain()
            await app.state.identity_capsule_service.drain()
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
    app.include_router(chat.router, prefix=settings.api_prefix)
    app.include_router(meta.router, prefix=settings.api_prefix)
    app.include_router(review.router, prefix=settings.api_prefix)
    app.include_router(settings_router.router, prefix=settings.api_prefix)
    app.include_router(activity.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)

    # OAuth 2.1 authorization-server + discovery routes live at the ROOT, not under /api/v1 — a
    # connector expects `/.well-known/oauth-*`, `/authorize`, `/token`, `/register` at the origin
    # (ADR-046 §2 / 03-api §MCP; Caddy proxies them to the api app — task 5).
    app.include_router(oauth.router)

    # MCP server (task 4): Streamable HTTP mounted at the ROOT so the spec path `/mcp` resolves
    # exactly (the SDK registers a Route at `/mcp`, avoiding a trailing-slash redirect that breaks
    # connectors). Built here so the route is static; its tools + token verifier read services from
    # app.state lazily (wired in the lifespan, which also runs the transport's session manager).
    # Mounted LAST so the API/OAuth routes above take precedence; only `/mcp` + its
    # `/.well-known/oauth-protected-resource/mcp` fall through to the sub-app.
    mcp_server = build_mcp_server(app, settings)
    app.state.mcp_server = mcp_server
    app.mount("/", mcp_server.streamable_http_app())

    return app


app = create_app()
