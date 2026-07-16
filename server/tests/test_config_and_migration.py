"""Config coercion and the runtime migration-head check (no DB, no SQLAlchemy import)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.migration_check import compute_head


def test_planes_and_chains_parse_from_csv():
    s = Settings(
        planes="Professional, Personal , Ideas",
        # Chains are MODEL ids now (ADR-045); CSV coercion is the same.
        chat_chain="claude-opus-4-8,meta-llama/Llama-3.3-70B-Instruct",
        stt_chain="groq, openai",
        cors_origins="http://localhost:5173",
    )
    assert s.planes == ["Professional", "Personal", "Ideas"]
    assert s.chat_chain == ["claude-opus-4-8", "meta-llama/Llama-3.3-70B-Instruct"]
    assert s.stt_chain == ["groq", "openai"]
    assert s.cors_origins == ["http://localhost:5173"]


def test_stt_chain_and_effort_defaults():
    # ADR-020 / M1 replan: Groq-primary STT chain ships as default; ADR-045: one `claude_effort`
    # scalar (medium) seeds effort for every routing group.
    s = Settings()
    assert s.stt_chain == ["groq", "openai"]
    assert s.groq_stt_model == "whisper-large-v3"
    assert s.claude_effort == "medium"


def test_claude_model_scalars_and_chains_are_model_ids():
    # ADR-045: named model scalars + seed chains hold the raw vendor model strings.
    s = Settings()
    assert s.claude_opus_model == "claude-opus-4-8"
    assert s.claude_sonnet_model == "claude-sonnet-4-6"
    assert s.chat_chain == ["claude-opus-4-8", "meta-llama/Llama-3.3-70B-Instruct"]
    assert s.quick_chain == ["claude-sonnet-4-6", "meta-llama/Llama-3.3-70B-Instruct"]


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


def test_seeded_graph_vocabulary_defaults():
    # M3 (ADR-031 §3): 9 node types / 6 edge rels ship as seeds; entity substrate covers
    # the 6 entity-like types (ADR-030); resolution floor defaults to 0.8, live-tuned.
    s = Settings()
    assert s.node_types == [
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
    assert s.edge_rels == ["involves", "about", "part_of", "led_to", "follows", "at"]
    # `idea` is a CONTENT type (ADR-039, M3 task 11) — no longer minted as an entity hub, so it is
    # not in the entity-hub set. entity_like_types now realizes the ADR-038/039 `entity_types`.
    assert s.entity_like_types == ["person", "place", "topic", "event", "project"]
    assert s.entity_match_min_conf == 0.8
    assert s.mcp_capture_max_inflight == 2


def test_graph_vocabulary_parses_from_csv():
    s = Settings(
        node_types="memory, person , topic",
        edge_rels="involves, about",
        entity_like_types="person,topic",
    )
    assert s.node_types == ["memory", "person", "topic"]
    assert s.edge_rels == ["involves", "about"]
    assert s.entity_like_types == ["person", "topic"]


def test_vocabulary_guards_reject_bad_config():
    # Boot-time typo guard: the organizer fallback type must exist, and entity-like
    # types must be known node types.
    with pytest.raises(ValueError, match="memory"):
        Settings(node_types="person,topic")
    with pytest.raises(ValueError, match="ENTITY_LIKE_TYPES"):
        Settings(node_types="memory,person", entity_like_types="person,ghost")


def test_production_rejects_insecure_default_secrets():
    # Fail-fast guard (ADR-046 §2 security review): a prod boot on the shipped dev-default or an
    # empty HMAC secret would hash sessions/MCP tokens with a public key — refuse it.
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings(environment="production", mcp_token_hmac_secret="real-secret")
    with pytest.raises(ValueError, match="MCP_TOKEN_HMAC_SECRET"):
        Settings(environment="production", session_secret="real-secret")
    with pytest.raises(ValueError, match="MCP_TOKEN_HMAC_SECRET"):
        Settings(environment="production", session_secret="real-secret", mcp_token_hmac_secret="")
    # Real values in production, and the dev default outside production, both boot fine.
    Settings(environment="production", session_secret="s3cret", mcp_token_hmac_secret="h4mac")
    Settings(environment="development")


def test_compute_head_is_migration_013():
    # M6 task 1 adds revision 013 (chat_distill_state + captures.source_ref, ADR-048); head advances
    # to it from 012 (agent_runs.parent_run_id, ADR-047 §5).
    assert compute_head() == "013"
