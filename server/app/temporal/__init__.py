"""Temporal engine (M8.2 · ADR-056) — the pure-logic core of "LLMs classify, code computes"
(CLAUDE.md rule 12).

Four layers, no I/O, no LLM, no DB — heavily unit-testable with zero mocks, and all arithmetic is
stdlib so the web renders byte-identical phrases:

- :mod:`~app.temporal.symbolic` — the validated schema of what the organizer LLM may emit
  (symbolic classifications only; :func:`parse_symbolic` is fail-closed).
- :mod:`~app.temporal.resolver` — deterministic resolution of a symbolic reference against a
  capture's **stored anchor** into an absolute date/range (:func:`resolve` / ``resolve_reference``).
- :mod:`~app.temporal.tokens` — the ``[[t:START[/END][|label]]]`` token: parse/serialize, locate
  in a body, and the day-granular ``occurred`` floor/ceil the DB stores.
- :mod:`~app.temporal.render` — tokens → text for display (live relative + tooltip), the indexer
  (absolute), and LLM-bound paths (absolute + relative hint). Tokens are never shown raw.

Downstream tasks wire this in: the organizer emits symbolic refs → resolves → writes tokens +
``occurred`` (M8.2 T2); the indexer / chat / MCP / capsule / profile paths expand tokens (T3); the
web mirrors :mod:`render` (T4).
"""

from __future__ import annotations

from .render import (
    expand_body_for_index,
    expand_body_for_llm,
    expand_for_index,
    expand_for_llm,
    render_absolute,
    render_body,
    render_relative,
)
from .resolver import resolve, resolve_reference
from .symbolic import (
    ExplicitRef,
    MonthRef,
    RelativeRef,
    SeasonRef,
    TimeReference,
    WeekdayRef,
    parse_symbolic,
)
from .tokens import (
    PartialDate,
    ResolvedTime,
    TokenMatch,
    find_tokens,
    make_token,
    parse_inner,
)

__all__ = [
    # symbolic
    "TimeReference",
    "ExplicitRef",
    "RelativeRef",
    "WeekdayRef",
    "MonthRef",
    "SeasonRef",
    "parse_symbolic",
    # resolver
    "resolve",
    "resolve_reference",
    # tokens
    "PartialDate",
    "ResolvedTime",
    "TokenMatch",
    "find_tokens",
    "make_token",
    "parse_inner",
    # render
    "render_absolute",
    "render_relative",
    "render_body",
    "expand_for_index",
    "expand_for_llm",
    "expand_body_for_index",
    "expand_body_for_llm",
]
