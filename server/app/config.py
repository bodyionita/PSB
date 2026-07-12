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

    # --- Provider registry (ADR-004) ---
    # OpenAI-compatible endpoints share one client; a new compatible provider is config-only.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.studio.nebius.ai/v1"
    nebius_chat_model: str = "meta-llama/Meta-Llama-3.1-70B-Instruct"

    # Fixed, not UI-selectable — changing either means a migration + full reindex.
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    stt_model: str = "whisper-1"

    # Chat/distill fallback chains, by provider id. First entry is primary.
    chat_chain: CsvList = Field(default=["claude-max", "nebius"])
    distill_chain: CsvList = Field(default=["claude-max", "nebius"])
    # Model the claude-max provider drives through the Agent SDK / CLI.
    claude_max_model: str = "claude-opus-4-8"

    # --- Chunking (02-data-model §4) ---
    chunk_size: int = 1200
    chunk_overlap: int = 200

    # --- Connectors ---
    slack_user_token: str = ""

    # --- Scheduler (ADR-010) ---
    enable_scheduler: bool = False
    # The app's single local timezone: drives scheduling AND vault-facing formatting
    # (frontmatter `created`, note filename dates) — the only two uses of TZ (CLAUDE.md
    # conventions). DB timestamps stay UTC.
    scheduler_tz: str = "Europe/Bucharest"
    agent_window_start_hour: int = 3
    agent_window_end_hour: int = 5

    # --- Web / CORS (dev only; in prod Caddy same-origins the app) ---
    cors_origins: CsvList = Field(default=["http://localhost:5173"])

    @field_validator(
        "planes", "vault_ignore", "chat_chain", "distill_chain", "cors_origins", mode="before"
    )
    @classmethod
    def _coerce_lists(cls, value: str | list[str]) -> list[str]:
        return _split_csv(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
