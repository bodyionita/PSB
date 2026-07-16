"""Application settings — the single source of runtime config (CLAUDE.md rule 9).

Nothing outside this module reads ``os.environ``. No models, paths, dimensions,
schedules or plane lists are hardcoded elsewhere.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from .services.pipeline import PipelineDef

# The shipped dev default for HMAC secrets. Fine for local dev; rejected at boot in production
# (see Settings._check_production_secrets) so it can never silently protect a live surface.
_INSECURE_SECRET_DEFAULT = "dev-insecure-change-me"


def _split_csv(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return value
    return [item.strip() for item in value.split(",") if item.strip()]


# List settings are accepted as plain comma-separated env strings (e.g. PLANES=A,B,C).
# NoDecode stops pydantic-settings from JSON-decoding the value first, so the validator
# below owns parsing.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Product ---
    app_name: str = "Braindan"
    api_prefix: str = "/api/v1"
    environment: str = "development"  # development | production

    # --- Auth (ADR-007) ---
    # argon2id hash of the single login password. Generate via scripts/hash_password.py.
    api_password_hash: str = ""
    # Secret used to derive the stored (hashed) session token. Rotate => all sessions drop.
    session_secret: str = _INSECURE_SECRET_DEFAULT
    session_ttl_days: int = 30
    session_cookie_name: str = "braindan_session"
    # Secure cookie flag. False for plain-http local dev; True behind Cloudflare/Caddy TLS.
    session_cookie_secure: bool = False
    login_rate_limit_per_min: int = 5

    # --- Database (ADR-002) ---
    database_url: str = "postgresql://braindan:braindan@localhost:5432/braindan"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # --- Graph store (M3 pivot, ADR-026/030/031/032). The typed-node store replaces the vault. ---
    graph_store_path: str = "../graph-store"  # prod: /srv/graph-store (07-infra)
    # SSH URL of the private PSB-graph repo (ADR-031 §6). Empty ⇒ bootstrap skips the
    # remote (dev: commits stay local, same semantics as a remoteless store today).
    graph_store_repo: str = ""
    # Seeded node-type vocabulary (9) + edge rels (6) — ADR-031 §3. Growth is governed
    # (LLM proposes, user approves — ADR-027); approved additions land in app_settings,
    # these are the seeds.
    node_types: CsvList = Field(
        default=[
            "memory",
            "person",
            "idea",
            "conversation",
            "insight",
            "place",
            "event",
            "project",
            "topic",
        ]
    )
    edge_rels: CsvList = Field(default=["involves", "about", "part_of", "led_to", "follows", "at"])
    # The entity-hub types (ADR-030) — the set the resolver mints as thin hubs and that carry the
    # entity substrate (aliases/disambig/profiles). Must be a subset of node_types; the content
    # types (memory/conversation/insight/**idea**) are NOT entities. This set realizes the ADR-038/
    # ADR-039 `entity_types` concept: reorganize never deletes a node of one of these types (hubs
    # are shared substrate) and the organizer may never emit one as a content node (coercion guard).
    # `idea` was reclassified content-only in the M3 task-11 quality pass (ADR-039) — the resolver
    # no longer mints idea hubs. The config/API key keeps its `entity_like_types` name (the web
    # `GET /types` contract, ADR-006) even though it now realizes the ADRs' `entity_types`.
    entity_like_types: CsvList = Field(
        default=["person", "place", "topic", "event", "project"]
    )
    # Entity-resolution confidence floor (ADR-030 §3, live-tuned at the M3 Accept):
    # below it the organizer never links — edge goes pending + entity-ambiguity review item.
    entity_match_min_conf: float = Field(default=0.8, ge=0.0, le=1.0)
    # MCP capture burst queue (ADR-031 §1): beyond this many in-flight synchronous
    # organizes on the MCP surface, further captures wait their turn.
    mcp_capture_max_inflight: int = 2
    # Nightly derived-profile refresh (ADR-030 §4/ADR-032 §3): regenerate categorized
    # observation profiles for entities whose 1-hop neighborhood changed. A `nightly`-pipeline
    # step after `reindex` (ADR-047) so the day's edges are in the DB before it reads them (the
    # ordering is the pipeline's, not a per-job cron — see `pipeline_defs`).
    # Evidence-tiered profile depth (ADR-034): depth scales with graph degree so the nightly
    # LLM spend is structurally capped (once-mentioned entities never cost a model call).
    #   degree < snapshot_min          → tier `stub`     (mechanical, no LLM)
    #   snapshot_min ≤ degree < full   → tier `snapshot` (LLM: categorized lines + current state)
    #   degree ≥ full_min              → tier `full`     (LLM: + themes + open threads)
    profile_tier_snapshot_min: int = 3
    profile_tier_full_min: int = 8
    # Bound on the observation lines fed to the LLM / stored per profile (prompt + row size).
    profile_max_observations: int = 40
    # Nightly entity backfill scan (ADR-030 §6): entities minted/alias-changed since the last
    # run are re-checked against recent memory nodes for alias matches — an exact alias match
    # (length ≥ ENTITY_ALIAS_MIN_FUZZY_LEN) auto-adds the edge (feed-flagged), a shorter one
    # files an entity-ambiguity review item. A `nightly`-pipeline step after profile-refresh (047).
    # Only re-scan memory nodes indexed within this window (ADR-030 §6 "recent … nodes").
    backfill_window_days: int = 30
    # Bound on one backfill run's auto-adds + review items (guards a runaway alias match).
    backfill_max_links: int = 200

    # --- Planes (ADR-005 surviving half — attributes, not folders) ---
    planes: CsvList = Field(
        default=["Professional", "Personal", "Family", "Friends", "Health", "Ideas"]
    )
    # System folder for the organizer's "can't classify" + failure fallback (02 §1, ADR-026):
    # unclassifiable nodes land in `<GRAPH_STORE_PATH>/inbox/` (type=memory), never guessed.
    # A folder now, not a plane — planes are frontmatter attributes.
    inbox_folder: str = "inbox"
    # Store path prefixes the indexer skips (ADR-026: Obsidian gone, so no `.obsidian`).
    store_ignore: CsvList = Field(default=[".trash", ".git", "templates"])

    # --- Capture pipeline (M1, ADR-019) ---
    # Raw capture inputs that are not text (audio) are persisted here before any model call
    # as {capture_id}.{ext} (never-lose, CLAUDE.md rule 2). Prod = /srv/data (07-infra).
    data_path: str = "../data"
    # Whisper hard limit; larger uploads are rejected before persistence.
    audio_max_bytes: int = 25 * 1024 * 1024
    # Bounds on a single organize result, enforced by validate_organizer_output.
    organizer_max_nodes: int = 8
    organizer_max_tags: int = 12
    # Max canonical edges the organizer may write on one node (bounds a runaway model + keeps
    # the frontmatter legible). Edges beyond this are dropped at validation.
    organizer_max_edges: int = 12
    # Tag-vocabulary reuse (ADR-024 §1): the N most-used distinct store tags injected into the
    # organizer prompt so it prefers an existing tag over coining a variant. Frequency-capped to
    # bound prompt size; 0 disables the injection.
    organizer_tag_vocabulary_max: int = 100

    # --- Entity resolution (ADR-030/032) ---
    # Alias-index candidates injected into the organizer/resolver prompt are bounded (injection
    # hygiene, ADR-031 (a)): only the mention's matching candidates, capped at this many.
    entity_candidate_max: int = 8
    # Entropy guard (ADR-032 §2): an alias shorter than this never *fuzzy* auto-links — it needs
    # an exact/normalized hit or it goes to review. Guards "Al"/"IT"/"mom" style collisions.
    entity_alias_min_fuzzy_len: int = 4
    # Token-overlap candidate retrieval + alias accretion (ADR-040). A mention's surface form is
    # tokenized (folded + whitespace-split); a token drives the fuzzy retrieval leg / is accreted
    # only when it is at least this long AND not a stop token — so "Horia Fenwick" surfaces the
    # existing "Horia" hub while "Ana"/"the"/initials never fan out to everything (low-entropy
    # guard). A mention with no significant token falls back to exact-only retrieval.
    entity_min_token_len: int = 4
    # Low-entropy tokens excluded from token-overlap retrieval + accretion regardless of length
    # (English store; generic words that would over-match). Extend as needed.
    entity_stop_tokens: CsvList = Field(
        default=[
            "the", "and", "for", "with", "from", "this", "that", "her", "his", "their",
            "our", "your", "mom", "dad", "guy", "man", "boss", "friend", "team", "work",
        ]
    )

    # --- Review queue (ADR-030 §3 / ADR-029) ---
    # Upper bound on the admin Review list (GET /review) — a personal store's pending queue is
    # small, so one page suffices; bounds a runaway response if it ever isn't.
    review_list_max: int = 200
    # Upper bound on one POST /review/batch (ADR-048 §8) — a triage batch is small; bounds a runaway
    # request that would resolve items sequentially with real side effects (rule 9, like above).
    review_batch_max: int = 200

    # --- Graph-store backup / durability (ADR-014; ex-`vault_*`, renamed at M3 — ADR-031) ---
    # The server only ever fast-forward pushes to this remote/branch; never force/rebase/reset.
    store_git_remote: str = "origin"
    store_git_branch: str = "main"
    # Writes are coalesced into one commit per debounce window (~60s batch commits, §3).
    store_backup_debounce_seconds: float = 60.0
    # Commit identity (set in the repo config so `git commit` works inside the container).
    git_user_name: str = "Braindan"
    git_user_email: str = "braindan@braindan.local"

    # --- Object storage / R2 backups (ADR-014 §1, §7) ---
    # Secrets (rendered by CI into deploy/.env). Empty ⇒ backups disabled (dev), jobs skip.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    # Non-secret. Endpoint is derived from the account id unless explicitly overridden.
    r2_bucket: str = "braindan-backups"
    r2_endpoint_url: str = ""

    # --- Provider registry (ADR-004) ---
    # OpenAI-compatible endpoints share one client; a new compatible provider is config-only.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.studio.nebius.ai/v1"
    nebius_chat_model: str = "meta-llama/Llama-3.3-70B-Instruct"
    # Groq — STT primary (ADR-020). OpenAI-compatible /audio/transcriptions endpoint, so it
    # reuses OpenAICompatibleProvider; generous free tier + whisper-large-v3 quality.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_stt_model: str = "whisper-large-v3"

    # --- Embeddings: self-hosted nomic via Ollama (ADR-022) ---
    # Single provider, no hot fallback: one index = one vector space. `nomic-embed-text-v1.5`
    # served by an Ollama sidecar over the OpenAI-compatible /v1/embeddings shape — reuses
    # OpenAICompatibleProvider (base-URL only, no API key on localhost). embedding_provider_id
    # is the cold-swap seam (switch to nebius + reindex if English-centric search disappoints).
    # embedding_model / embedding_dim are settings, so a provider change is a migration +
    # reindex, never a code edit — but changing either still means a full reindex.
    embedding_provider_id: str = "ollama"
    ollama_base_url: str = "http://localhost:11434/v1"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    # OpenAI's STT model — used when the chain falls back to the "openai" provider.
    stt_model: str = "whisper-1"

    # Chat/distill fallback chains, by MODEL ID (the raw vendor string — ADR-045). First entry is
    # primary. These are the config SEEDS for the `chat`/`conspect` routing groups (ADR-025): the
    # ModelRoutingService overlays any user-saved `app_settings.model_routing`, falling back to
    # these when a group is unset. A model id resolves to its provider via the registry's
    # model→provider index (ADR-045); the vendor-string change cost is documented in
    # `claude_opus_model` below.
    chat_chain: CsvList = Field(default=["claude-opus-4-8", "meta-llama/Llama-3.3-70B-Instruct"])
    distill_chain: CsvList = Field(
        default=["claude-opus-4-8", "meta-llama/Llama-3.3-70B-Instruct"]
    )
    # `quick` routing-group seed (ADR-043, ADR-045): a cheap/fast lane for trivial calls (M4 =
    # session titling). The cheaper Sonnet model (served by the same `claude` provider) is primary;
    # the Nebius model is the fallback.
    quick_chain: CsvList = Field(
        default=["claude-sonnet-4-6", "meta-llama/Llama-3.3-70B-Instruct"]
    )
    # STT fallback chain (ADR-020): Groq (whisper-large-v3) primary, OpenAI (whisper-1) fallback.
    stt_chain: CsvList = Field(default=["groq", "openai"])
    # The two models the single `claude` provider serves through the Agent SDK / CLI via per-call
    # `--model` (ADR-045 — provider≠model). A model id is the RAW VENDOR STRING (no short-key
    # indirection): the accepted cost is that a vendor-string change (e.g. Sonnet 4.6→5) is a config
    # AND a saved-routing migration touch (ADR-045 §3), not a transparent remap. These feed the
    # registry's chat-model catalog; the `*_chain` seeds above reference them by that same string.
    claude_opus_model: str = "claude-opus-4-8"
    # The cheaper Sonnet model on the SAME `claude` provider (ADR-045 collapsed the former separate
    # Sonnet-tier fake provider into this). A one-line swap to Sonnet 5 or any CLI alias later — but
    # per ADR-045 §3 it must also be updated in the seed chains that name it.
    claude_sonnet_model: str = "claude-sonnet-4-6"
    # Reasoning-effort level passed to every `claude` CLI call (`--effort`) when a routing group
    # doesn't set its own — the single effort SEED for all groups (ADR-045 §5, replacing the former
    # per-tier effort scalars). Per-group/per-model effort is still tuned in Settings → Models
    # (saved routing wins). low|medium|high|xhigh|max.
    claude_effort: str = "medium"

    # --- Chunking (02-data-model §4) ---
    chunk_size: int = 1200
    chunk_overlap: int = 200

    # --- Indexer (M2, ADR-022). A node's chunks are batch-embedded in one call; a transient
    # embedder failure (e.g. Ollama 429/restart) is retried with exponential backoff before the
    # node is skipped (skip-and-continue → the run is `partial`, a later reindex retries it). ---
    embed_max_attempts: int = 3
    embed_retry_backoff_seconds: float = 1.0

    # --- Derived `similar` edges (ADR-023 surviving half, retargeted at M3). Directional
    # per-node top-K over nodes.embedding cosine above a floor → `edges(origin=derived)`.
    # DB-ONLY now — no file rendering, no commit step (ADR-026). Recomputed nightly only
    # (+ POST /admin/reindex); both knobs tuned live (empty graph → lower the floor). ---
    similar_top_k: int = 5
    similar_min_score: float = 0.5

    # --- Tag consolidation (M2, ADR-024 §2). The manual two-step POST /admin/tags/consolidate:
    # propose feeds up to this many distinct tags (most-used first) to the distill chain to group
    # variants; apply rewrites the affected nodes' frontmatter tags + reindexes them. ---
    tags_consolidate_max_vocabulary: int = 300

    # --- Edge retro-consolidation (M3, ADR-036 / task 7b). The on-demand two-step
    # POST /admin/vocab/consolidate for an approved edge rel: propose feeds up to this many existing
    # canonical edges (a bounded inventory) to the distill chain to pick which should be re-typed
    # onto the new rel; apply rewrites those edges' frontmatter `rel:` + reindexes. ---
    vocab_consolidate_max_edges: int = 300
    # --- Search (M2, 03-api §Search, ADR-022). Node-grouped pgvector cosine over chunks. ---
    # Default result count when the request omits top_k; the request is clamped to this ceiling.
    search_top_k_default: int = 10
    search_max_top_k: int = 50
    # No hard score floor by default (0 keeps every hit); raise to prune weak matches. With the M4
    # hybrid retriever this floors on the fused RRF×recency score (small magnitude), not raw cosine.
    search_min_score: float = 0.0
    # A result snippet (the best chunk) is truncated to this many chars for the results list.
    search_snippet_max_chars: int = 400
    # --- M4 hybrid retrieval (03-api §Search, ADR-032 §5/§7). vector ⊍ tsvector FTS legs fused by
    # RRF, then a mild recency prior. The FTS leg self-suppresses on non-English / zero-lexeme
    # queries (English corpus, 02 §3) so no language-detect knob is needed. ---
    # Reciprocal-rank-fusion constant: score contribution of a leg = 1/(k + rank). k=60 per ADR-032.
    search_rrf_k: int = 60
    # Candidate pool taken from EACH leg (top-N by that leg's own score) before fusion; bounds the
    # fused set so RRF isn't diluted across the whole corpus. Clamped up to top_k at request time.
    search_rrf_candidates: int = 60
    # Recency prior on `occurred ?? created` (bounded multiplicative nudge applied to the fused
    # list pre-cut, ADR-032 §7): factor = floor + (1-floor)·0.5^(age_days/half_life), capped at 1.0.
    # `gt=0`: the half-life is a SQL divisor — a 0 would divide-by-zero in the ranking query.
    search_recency_half_life_days: float = Field(default=180.0, gt=0)
    # Floor of the recency multiplier — the most an old node is ever down-weighted (never zeroed).
    # Bounded [0,1]: 1.0 disables the prior (every node at full recency), 0 lets it decay fully.
    search_recency_floor: float = Field(default=0.9, ge=0, le=1)

    # --- Graph traverse / build_context (M5 task 1, 03-api §MCP, ADR-046/028/032). The one-hop
    # `GraphService.neighbors` primitive backs MCP `traverse` + `GET /nodes/{id}/neighbors` (M7);
    # `build_context` bundles get_node + a bounded neighbor tree. LLM context is finite, so reads
    # are page- and fanout-capped. ---
    # Default page size for a `neighbors`/`traverse` call when the request omits a limit; clamped to
    # the ceiling below (a hub can have hundreds of edges — one page must never dump them all).
    graph_neighbors_page_default: int = 25
    graph_neighbors_page_max: int = 100
    # `build_context` traversal caps: depth is capped at `build_context_max_depth` (default 2 per
    # ADR-032's Basic-Memory pattern — the 03-api contract's `depth ≤ 2`; raise only deliberately)
    # and each visited node contributes at most `build_context_fanout` neighbors (the rest are
    # flagged truncated, reachable via `traverse`). Depth 0 = the node + capsule only.
    build_context_default_depth: int = 1
    build_context_max_depth: int = 2
    build_context_fanout: int = 10

    # --- Identity capsule (M5 task 2, ADR-046 §5 / ADR-033 #1). The derived ~300-token "who the
    # user is / current state" preamble, distilled nightly on `conspect` from a blend of the graph's
    # high-degree entity-profile hubs + recent memories + recent insights, stored as an app_settings
    # blob and served as build_context L0 + the chat system prompt (never generated inline). ---
    # How many of each source kind the distiller blends (hubs ranked by graph degree; memories +
    # insights newest-first). Bounds the prompt size; insights are usually absent until M6/M10.
    identity_capsule_max_hubs: int = 8
    identity_capsule_max_memories: int = 12
    identity_capsule_max_insights: int = 8
    # The soft token budget handed to the distiller prompt (~300 tokens, ADR-046 §5) + a hard char
    # cap the stored text is truncated to (a runaway-length backstop; ~300 tokens ≈ 1200 chars).
    identity_capsule_budget_tokens: int = 300
    identity_capsule_max_chars: int = 1600

    # --- Chat (M4 task 3, 04-pipelines §5, ADR-025). Interactive grounded chat over the graph. ---
    # Turns condensed into a standalone English query on turn ≥2 (04 §5). Also bounds how much
    # prior history is replayed into the answer prompt, so a long session can't bloat the context.
    chat_condense_last_n: int = 15
    # "Not in your memories" min_score floor for chat retrieval (04 §5, MINOR-1 from task 2). This
    # floors the fused **RRF×recency** score, NOT a cosine score: one leg at rank 1 contributes
    # 1/(k+1) ≈ 0.016 (k=60), both legs ≈ 0.033, recency-attenuated — so the chat floor lives on a
    # ~0.03-max scale. 0.01 is a gentle backstop that only sheds the candidate-pool tail; the
    # grounding prompt is the primary "not in your memories" judge (prompt-driven + floor, no
    # classifier). A per-cosine value like 0.5 here would silently drop every hit — do not use one.
    chat_retrieval_min_score: float = 0.01
    # A generated session title is trimmed to this many chars (best-effort `quick`-tier titling).
    chat_title_max_chars: int = 80
    # Upper bound on the unpaginated `GET /chat/sessions` thread list, newest-first (03-api §Chat).
    chat_sessions_list_limit: int = 100

    # --- Chat-distiller (M6 task 1, 04-pipelines §4, ADR-048). Stance-gated distillation of idle
    # chat sessions into memories: a single `conspect` pass over each session's new turns emits
    # user-stance candidates; endorsed → a `captures` row (source=chat) → organizer, unclear →
    # a `stance-candidate` review item, rejected → run-log only. A `chat_distill_state` watermark
    # makes re-distillation idempotent (delta-after-watermark). Not yet scheduled — wired into the
    # nightly pipeline in M6 task 8. ---
    # A session is distillable once its newest message is at least this many hours old (idle) — so a
    # live conversation is never distilled mid-thread (idle-eligibility, ADR-048 §5).
    chat_distill_idle_hours: float = 12.0
    # Bound on how many idle sessions one run processes (a run's budget; the rest wait for next).
    chat_distill_max_sessions_per_run: int = 50
    # Bound on the delta messages fed to one distill prompt — a pathologically long span keeps its
    # most-recent N (older turns dropped-and-logged, never silently). Personal sessions are small.
    chat_distill_max_delta_messages: int = 300
    # Bound on the candidates accepted from one distill response (guards a runaway model). The
    # surplus beyond it is dropped-and-logged.
    chat_distill_max_candidates: int = 20
    # Bound on the chat-scoped "recently auto-recorded" audit list (GET /chat/auto-recorded, ADR-048
    # §12 / M6 task 4). The one-tap-remove surface; the general Activity feed (M8) absorbs it later.
    chat_auto_recorded_list_max: int = 100

    # --- MCP OAuth 2.1 authorization server (M5 task 3, ADR-046 §2). The `api` app is both the
    # authorization server and the resource server for the MCP surface; tokens are opaque + HMAC-
    # hashed in the DB (same discipline as web sessions), gated behind a password + explicit-consent
    # /authorize flow with PKCE. ---
    # The public origin the AS advertises in its discovery metadata (RFC 8414/9728) and the base of
    # the MCP resource identifier `<public_base_url>/mcp` (RFC 8707). Absolute, NO trailing slash.
    # Prod: https://braindan.cc (07-infra); dev: the local server origin the connector reaches.
    public_base_url: str = "http://localhost:8000"
    # HMAC secret that hashes MCP access/refresh tokens + auth codes before DB storage — the MCP
    # analogue of `session_secret` (replaces 07-infra's static "MCP bearer-token secret"; the agent
    # never handles the real value, it lives only in deploy/.env). Rotating it drops all MCP tokens.
    mcp_token_hmac_secret: str = _INSECURE_SECRET_DEFAULT
    # The single full-access scope an MCP connector is granted in M5 (read/write scope split is
    # deferred — ADR-046 §2). Advertised in discovery + bound onto every issued token.
    mcp_oauth_scope: str = "brain"
    # Access-token lifetime (~1h, ADR-046 §2): short-lived so a leaked access token ages out fast;
    # the connector refreshes silently against the long-lived refresh token.
    mcp_access_token_ttl_seconds: int = 3600
    # Refresh-token lifetime: long-lived + **sliding** (each use rotates to a fresh pair — old
    # refresh revoked), so an active connector never re-approves. Idle past this ⇒ re-run the flow.
    mcp_refresh_token_ttl_days: int = 60
    # Authorization-code lifetime: very short + single-use (OAuth 2.1 SHOULD ≤ 10 min). The code is
    # exchanged for tokens within seconds of the redirect.
    mcp_auth_code_ttl_seconds: int = 300
    # Max edges rendered inline on one node/neighbor in an MCP tool result (ADR-046 §3 hub guard) —
    # beyond this a "N more; use traverse" pointer is emitted so a hub can't flood the LLM context.
    mcp_inline_edge_cap: int = 20

    # --- Connectors ---
    slack_user_token: str = ""

    # --- Scheduler (ADR-010) ---
    # In-process APScheduler. Off by default; exactly one prod instance sets it true so the
    # durability jobs (below) fire once. M4 extends the same scheduler with the agent window.
    enable_scheduler: bool = False
    # The app's single local timezone: drives scheduling AND store-facing formatting
    # (frontmatter `created`, node filename dates) — the only two uses of TZ (CLAUDE.md
    # conventions). DB timestamps stay UTC.
    scheduler_tz: str = "Europe/Bucharest"
    agent_window_start_hour: int = 3
    agent_window_end_hour: int = 5
    # A job whose fire time was missed by more than this (VPS down/restart) is skipped, not
    # run late — the next night covers it (ADR-010). Tolerates in-window restart jitter.
    scheduler_misfire_grace_seconds: int = 3600

    # --- Pipelines (ADR-047: the pipeline is the scheduling primitive) ---
    # The per-job staggered crons (ADR-010: reindex 03:40, data-sync 03:10, db-backup 03:25,
    # profile-refresh 04:10, backfill 04:20, identity-capsule 04:35, store-sweep 04:55, bundle
    # 04:57, weekly integrity-drill Sun 04:30) are **retired** (M5.5 task 2): the scheduler
    # registers **one cron per pipeline** (ADR-047 §7), not one per job. The window is enforced by
    # *sequencing from these starts*, not by the stagger — the `nightly` pipeline runs its steps
    # back-to-back from 03:00, one step's RAM at a time (ADR-014 §1/§6 durability jobs are steps in
    # it); the `weekly` integrity drill keeps its Sunday slot. Ordered steps + per-step `on_fail`
    # live in `pipeline_defs()` below.
    nightly_pipeline_cron: str = "0 3 * * *"
    weekly_pipeline_cron: str = "30 4 * * sun"
    # /health `backups` leg degrades when the last successful integrity-drill is older than this
    # (weekly cadence + one night of grace) or the latest drill failed (ADR-014 §6).
    integrity_drill_max_age_days: int = 8

    # --- Web / CORS (dev only; in prod Caddy same-origins the app) ---
    cors_origins: CsvList = Field(default=["http://localhost:5173"])

    def pipeline_defs(self) -> tuple[PipelineDef, ...]:
        """The `nightly` + `weekly` pipeline definitions (ADR-047 §1/§3): name, cron, steps,
        per-step `on_fail`. The nightly roster is the migrated ADR-010 order (dependency order:
        raw-input sync → db backup → reindex → derived profiles/backfill → identity capsule → store
        commit/bundle); the weekly pipeline is the integrity drill. **`continue`-dominant** (ADR-047
        §4): a flaky step never costs the night its downstream backups — no step here is a
        foundational precondition that should abort the rest, so all are `continue` (the `halt`
        policy exists and is exercised, but the migrated durability roster doesn't need it). Task 2
        wires these into the scheduler (one cron per pipeline) + maps each step name to its job.
        Lazy import keeps config out of the pipeline→agent_runs→db→config import cycle."""
        from .services.pipeline import CONTINUE, PipelineDef, PipelineStepDef

        step = lambda name: PipelineStepDef(name=name, on_fail=CONTINUE)  # noqa: E731
        nightly = PipelineDef(
            name="nightly",
            cron=self.nightly_pipeline_cron,
            steps=(
                step("data-sync"),
                step("db-backup"),
                step("reindex"),
                step("profile-refresh"),
                step("entity-backfill"),
                step("identity-capsule-refresh"),
                step("store-sweep"),
                step("store-backup"),
            ),
        )
        weekly = PipelineDef(
            name="weekly",
            cron=self.weekly_pipeline_cron,
            steps=(step("integrity-drill"),),
        )
        return (nightly, weekly)

    @field_validator(
        "planes",
        "store_ignore",
        "node_types",
        "edge_rels",
        "entity_like_types",
        "entity_stop_tokens",
        "chat_chain",
        "distill_chain",
        "quick_chain",
        "stt_chain",
        "cors_origins",
        mode="before",
    )
    @classmethod
    def _coerce_lists(cls, value: str | list[str]) -> list[str]:
        return _split_csv(value)

    @model_validator(mode="after")
    def _check_vocabulary(self) -> Settings:
        # Boot-time typo guard on the governed vocabulary (ADR-027/030/031): the
        # organizer's no-fit fallback type must exist, and only known types can carry
        # the entity substrate.
        if "memory" not in self.node_types:
            raise ValueError("NODE_TYPES must include 'memory' (the organizer fallback type)")
        unknown = set(self.entity_like_types) - set(self.node_types)
        if unknown:
            raise ValueError(f"ENTITY_LIKE_TYPES not present in NODE_TYPES: {sorted(unknown)}")
        return self

    @model_validator(mode="after")
    def _check_production_secrets(self) -> Settings:
        # Fail-fast in production if a secret was never provisioned: an empty or the shipped
        # dev-default value would otherwise boot silently and hash sessions / MCP tokens with a
        # public, guessable key on an internet-facing surface (ADR-046 §2 security review). The
        # real values live only in deploy/.env (CI-rendered) / env — never in git.
        if self.environment == "production":
            insecure = {"", _INSECURE_SECRET_DEFAULT}
            offenders = [
                name
                for name, value in (
                    ("SESSION_SECRET", self.session_secret),
                    ("MCP_TOKEN_HMAC_SECRET", self.mcp_token_hmac_secret),
                )
                if value.strip() in insecure  # strip so a whitespace-only value can't slip through
            ]
            if offenders:
                raise ValueError(
                    "Refusing to boot in production with unset/insecure default secret(s): "
                    + ", ".join(offenders)
                    + " — set real values in deploy/.env (GitHub Actions secrets)."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
