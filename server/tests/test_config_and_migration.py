"""Config coercion and the runtime migration-head check (no DB, no SQLAlchemy import)."""

from __future__ import annotations

from app.config import Settings
from app.migration_check import compute_head


def test_planes_and_chains_parse_from_csv():
    s = Settings(
        planes="Professional, Personal , Ideas",
        chat_chain="claude-max,nebius",
        stt_chain="groq, openai",
        cors_origins="http://localhost:5173",
    )
    assert s.planes == ["Professional", "Personal", "Ideas"]
    assert s.chat_chain == ["claude-max", "nebius"]
    assert s.stt_chain == ["groq", "openai"]
    assert s.cors_origins == ["http://localhost:5173"]


def test_stt_chain_and_effort_defaults():
    # ADR-020 / M1 replan: Groq-primary STT chain + medium claude-max effort ship as defaults.
    s = Settings()
    assert s.stt_chain == ["groq", "openai"]
    assert s.groq_stt_model == "whisper-large-v3"
    assert s.claude_max_effort == "medium"


def test_list_values_pass_through_unchanged():
    s = Settings(planes=["A", "B"])
    assert s.planes == ["A", "B"]


def test_embedding_dim_matches_schema():
    # M2 migration 004 resizes the vector columns to 768 for self-hosted nomic (ADR-022);
    # config must agree with the schema.
    assert Settings().embedding_dim == 768


def test_embedding_provider_defaults_to_ollama():
    # ADR-022: the sole embedding provider is the on-box Ollama sidecar (nomic-embed-text).
    s = Settings()
    assert s.embedding_provider_id == "ollama"
    assert s.embedding_model == "nomic-embed-text"
    assert s.ollama_base_url == "http://localhost:11434/v1"


def test_compute_head_is_migration_004():
    # M2 adds revision 004 (embeddings 768 + note_links, ADR-022/023); head advances to it.
    assert compute_head() == "004"
