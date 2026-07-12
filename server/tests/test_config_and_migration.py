"""Config coercion and the runtime migration-head check (no DB, no SQLAlchemy import)."""

from __future__ import annotations

from app.config import Settings
from app.migration_check import compute_head


def test_planes_and_chains_parse_from_csv():
    s = Settings(
        planes="Professional, Personal , Ideas",
        chat_chain="claude-max,nebius",
        cors_origins="http://localhost:5173",
    )
    assert s.planes == ["Professional", "Personal", "Ideas"]
    assert s.chat_chain == ["claude-max", "nebius"]
    assert s.cors_origins == ["http://localhost:5173"]


def test_list_values_pass_through_unchanged():
    s = Settings(planes=["A", "B"])
    assert s.planes == ["A", "B"]


def test_embedding_dim_matches_schema():
    # The migration hardcodes vector(1536); config must agree (ADR-004).
    assert Settings().embedding_dim == 1536


def test_compute_head_is_migration_002():
    # M1 adds revision 002 (capture follow-up columns); head must advance to it.
    assert compute_head() == "002"
