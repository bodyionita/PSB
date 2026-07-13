"""Application settings — the single source of runtime config (CLAUDE.md rule 9).

Nothing outside this module reads ``os.environ``. No models, paths, dimensions,
schedules or plane lists are hardcoded elsewhere.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    session_secret: str = "dev-insecure-change-me"
    session_ttl_days: int = 30
    session_cookie_name: str = "braindan_session"
    # Secure cookie flag. False for plain-http local dev; True behind Cloudflare/Caddy TLS.
    session_cookie_secure: bool = False
    login_rate_limit_per_min: int = 5

    # --- Database (ADR-002) ---
    database_url: str = "postgresql://braindan:braindan@localhost:5432/braindan"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # --- Vault (ADR-001) ---
    vault_path: str = "../ObisidanVault"
    planes: CsvList = Field(
        default=["Professional", "Personal", "Family", "Friends", "Health", "Ideas"]
    )
    # System plane/folder for the organizer's "don't know" + failure fallback (ADR-005/019).
    # Always present; not part of PLANES.
    inbox_plane: str = "Inbox"
    # Path prefixes the indexer skips.
    vault_ignore: CsvList = Field(default=[".obsidian", ".trash", ".git", "templates"])

    # --- Capture pipeline (M1, ADR-019) ---
    # Raw capture inputs that are not text (audio) are persisted here before any model call
    # as {capture_id}.{ext} (never-lose, CLAUDE.md rule 2). Prod = /srv/data (07-infra).
    data_path: str = "../data"
    # Whisper hard limit; larger uploads are rejected before persistence.
    audio_max_bytes: int = 25 * 1024 * 1024
    # Bounds on a single organize result, enforced by validate_organizer_output.
    organizer_max_notes: int = 8
    organizer_max_tags: int = 12

    # --- Vault backup / durability (ADR-014) ---
    # The server only ever fast-forward pushes to this remote/branch; never force/rebase/reset.
    vault_git_remote: str = "origin"
    vault_git_branch: str = "main"
    # Writes are coalesced into one commit per debounce window (~60s batch commits, §3).
    vault_backup_debounce_seconds: float = 60.0
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
    nebius_chat_model: str = "meta-llama/Meta-Llama-3.1-70B-Instruct"
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

    # Chat/distill fallback chains, by provider id. First entry is primary.
    chat_chain: CsvList = Field(default=["claude-max", "nebius"])
    distill_chain: CsvList = Field(default=["claude-max", "nebius"])
    # STT fallback chain (ADR-020): Groq (whisper-large-v3) primary, OpenAI (whisper-1) fallback.
    stt_chain: CsvList = Field(default=["groq", "openai"])
    # Model the claude-max provider drives through the Agent SDK / CLI.
    claude_max_model: str = "claude-opus-4-8"
    # Reasoning-effort level passed to every claude-max CLI call (`--effort`). Global in v1;
    # per-task effort is a post-v1 extension (ADR-004 / M1 replan). low|medium|high|xhigh|max.
    claude_max_effort: str = "medium"

    # --- Chunking (02-data-model §4) ---
    chunk_size: int = 1200
    chunk_overlap: int = 200

    # --- Indexer (M2, ADR-022). A note's chunks are batch-embedded in one call; a transient
    # embedder failure (e.g. Ollama 429/restart) is retried with exponential backoff before the
    # note is skipped (skip-and-continue → the run is `partial`, a later reindex retries it). ---
    embed_max_attempts: int = 3
    embed_retry_backoff_seconds: float = 1.0

    # --- Relatedness graph (M2, ADR-023). Directional per-note top-K over notes.embedding
    # cosine above a floor → note_links + a rendered sb:related block in each note body.
    # Recomputed nightly only (+ POST /admin/reindex); both knobs are tuned live during the M2
    # Accept (empty graph → lower the floor; junk links → raise it). ---
    related_top_k: int = 5
    related_min_score: float = 0.5

    # --- Search (M2, 03-api §Search, ADR-022). Note-grouped pgvector cosine over chunks. ---
    # Default result count when the request omits top_k; the request is clamped to this ceiling.
    search_top_k_default: int = 10
    search_max_top_k: int = 50
    # No hard score floor by default (0 keeps every hit); raise to prune weak matches.
    search_min_score: float = 0.0
    # A result snippet (the best chunk) is truncated to this many chars for the results list.
    search_snippet_max_chars: int = 400

    # --- Connectors ---
    slack_user_token: str = ""

    # --- Scheduler (ADR-010) ---
    # In-process APScheduler. Off by default; exactly one prod instance sets it true so the
    # durability jobs (below) fire once. M4 extends the same scheduler with the agent window.
    enable_scheduler: bool = False
    # The app's single local timezone: drives scheduling AND vault-facing formatting
    # (frontmatter `created`, note filename dates) — the only two uses of TZ (CLAUDE.md
    # conventions). DB timestamps stay UTC.
    scheduler_tz: str = "Europe/Bucharest"
    agent_window_start_hour: int = 3
    agent_window_end_hour: int = 5
    # A job whose fire time was missed by more than this (VPS down/restart) is skipped, not
    # run late — the next night covers it (ADR-010). Tolerates in-window restart jitter.
    scheduler_misfire_grace_seconds: int = 3600

    # --- Durability schedule (ADR-010 window, ADR-014 §1/§6). Standard 5-field crontab, all
    # evaluated in scheduler_tz. Staggered inside 03:00–05:00 to avoid RAM stacking on the VPS;
    # M2 fills rescan 03:40 (reindex_cron below); the M4 slots (Slack 03:00 / summary 04:10 /
    # review 04:40) are still free. ---
    # Combined nightly reindex (M2, ADR-023 §4): git pull → rescan → recompute graph → commit+push.
    # In the ADR-010 window, ahead of the summary jobs so search/graph reflect the day's captures.
    reindex_cron: str = "40 3 * * *"  # nightly full rescan + relatedness recompute (04 §5)
    backup_data_sync_cron: str = "10 3 * * *"  # nightly /srv/data raw inputs → R2
    backup_db_backup_cron: str = "25 3 * * *"  # nightly pg_dump → R2
    integrity_drill_cron: str = "30 4 * * sun"  # weekly verify+clone drill (ADR-014 §6)
    backup_vault_sweep_cron: str = "55 4 * * *"  # ADR-010 04:55 commit+push sweep
    backup_vault_bundle_cron: str = "57 4 * * *"  # WORM `git bundle` right after the sweep
    # /health `backups` leg degrades when the last successful integrity-drill is older than this
    # (weekly cadence + one night of grace) or the latest drill failed (ADR-014 §6).
    integrity_drill_max_age_days: int = 8

    # --- Web / CORS (dev only; in prod Caddy same-origins the app) ---
    cors_origins: CsvList = Field(default=["http://localhost:5173"])

    @field_validator(
        "planes",
        "vault_ignore",
        "chat_chain",
        "distill_chain",
        "stt_chain",
        "cors_origins",
        mode="before",
    )
    @classmethod
    def _coerce_lists(cls, value: str | list[str]) -> list[str]:
        return _split_csv(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
