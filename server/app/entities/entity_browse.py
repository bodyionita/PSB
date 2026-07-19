"""Entity browse/search — the read behind ``GET /entities`` that feeds the shared merge picker
(03-api §Search & graph; M9.5 / ADR-058 §11; M9.8 T2 / ADR-064 §2).

The picker is a **name-typeahead**: the user types a name, the server returns matching entity hubs
(id + title + aliases + type), and the UI resolves the pick to an id — the user never touches a
UUID. Matching is done in Python over the personal-scale hub set (dozens–low hundreds), so the SQL
stays a plain typed read (:meth:`EntityStore.list_entities`) and the match is **diacritic-folded**
via :func:`normalize_alias` — consistent with the folded titles/aliases the ``NodeWriter`` stores,
so ``"Madalina"`` finds a hub written as ``"Mădălina"`` (ADR-041). ``/search`` stays query-shaped
(semantic, embedding-backed); this browse is name-shaped and never calls a model.

:func:`rank_entity_matches` is the pure ranker (unit-tested, no DB — 08 testing policy);
:class:`EntityBrowseService` is the thin orchestration the router delegates to (rule 5).
"""

from __future__ import annotations

from .entity_store import EntityRef, EntityStore
from .store import normalize_alias

# Match-quality tiers (lower is better); ranked before the alphabetical tie-break so an exact name
# hit sits above a mere substring, and a title hit above an alias hit.
_EXACT_TITLE = 0
_PREFIX_TITLE = 1
_EXACT_ALIAS = 2
_CONTAINS_TITLE = 3
_CONTAINS_ALIAS = 4
_NO_MATCH = 99


def _tier(ref: EntityRef, nq: str) -> int:
    """The best (lowest) match tier of ``ref`` against the normalized query ``nq``. ``_NO_MATCH``
    when neither the title nor any alias contains ``nq``."""
    title = normalize_alias(ref.title) if ref.title else ""
    if title:
        if title == nq:
            return _EXACT_TITLE
        if title.startswith(nq):
            best = _PREFIX_TITLE
        elif nq in title:
            best = _CONTAINS_TITLE
        else:
            best = _NO_MATCH
    else:
        best = _NO_MATCH
    for alias in ref.aliases:
        na = normalize_alias(alias)
        if not na:
            continue
        if na == nq:
            best = min(best, _EXACT_ALIAS)
        elif nq in na:
            best = min(best, _CONTAINS_ALIAS)
    return best


def _alpha_key(ref: EntityRef) -> tuple[int, str]:
    """Alphabetical tie-break by folded title; untitled hubs sort last (by id) so they're stable
    but never crowd the named results."""
    if ref.title:
        return (0, normalize_alias(ref.title))
    return (1, ref.id)


def rank_entity_matches(refs: list[EntityRef], q: str | None, limit: int) -> list[EntityRef]:
    """Filter + rank entity hubs for the typeahead.

    With no ``q`` → an alphabetical browse (by folded title, untitled last). With ``q`` → keep only
    hubs whose normalized title or an alias contains the normalized query, ordered by match tier
    then alphabetically. Truncated to ``limit`` (``limit <= 0`` ⇒ empty)."""
    if limit <= 0:
        return []
    nq = normalize_alias(q) if q else ""
    if not nq:
        return sorted(refs, key=_alpha_key)[:limit]
    scored: list[tuple[int, tuple[int, str], EntityRef]] = []
    for ref in refs:
        tier = _tier(ref, nq)
        if tier == _NO_MATCH:
            continue
        scored.append((tier, _alpha_key(ref), ref))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [ref for _, _, ref in scored[:limit]]


class EntityBrowseService:
    """Browse/search over entity hubs for the merge picker (ADR-064 §2). Resolves the ``type``
    filter (one entity-like type, or all configured ones when omitted), reads the live hubs, and
    ranks them by name. No writes, no model calls."""

    def __init__(self, *, store: EntityStore, entity_like_types: list[str]) -> None:
        self._store = store
        self._entity_like_types = list(entity_like_types)

    async def browse(
        self, *, type_filter: str | None, q: str | None, limit: int
    ) -> list[EntityRef]:
        types = [type_filter] if type_filter else self._entity_like_types
        refs = await self._store.list_entities(types=types)
        return rank_entity_matches(refs, q, limit)
