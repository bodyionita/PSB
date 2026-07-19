"""The conservative entity-hub dedup detector (M9.8 T4, ADR-064 §4).

Legacy entity resolution mints first-name↔full-name duplicates ("Diana" and "Diana Vance" are one
person, unmerged). This nightly detector proposes likely-duplicate **entity hubs**, gated by a
**strict AND** so it stays conservative (ADR-064 §4):

  * a **name gate** — one hub's normalized surface form (title + aliases) *contains* the other's
    (token subset), or they clear a high fuzzy match; AND
  * a **shared-neighborhood gate** — the two hubs wire into >= ``entity_dedup_min_shared`` common
    canonical neighbours.

The AND is what suppresses the false positive the ADR names: **"Diana Wren"** shares the first name
with "Diana" (the name gate alone would flag her) but wires into a *different* neighbourhood, so the
shared-neighborhood leg fails and she is never proposed. Only same-type pairs are considered.

It powers **both** surfaces (ADR-064 §4), like the dedup sweep files review items but with a
high/low split:
  * **high-confidence** pairs (a containment / strong-fuzzy name match AND >=
    ``entity_dedup_high_min_shared`` shared neighbours) land **inline** — written to this run's
    ``agent_runs`` details, which the web reads off the latest ``entity-dedup`` run (the same
    mechanism the graph-health card uses) for a one-click Merge via the shared picker (pre-filled);
  * **lower-confidence** pairs file an ``entity-dedup`` review item the user resolves (merge folds
    with the entity alias union + records a durable decision — ``ReviewService`` — so it survives a
    reprocess, ADR-064 §1).

**Never auto-merges** (rule 2 / ADR-064 §4) — every merge is human-approved. A **re-file guard**
(skip any pair already carrying an ``entity-dedup`` review item in any status, mirroring the dedup
sweep's ADR-049 §5 guard) makes a run-overlap re-scan idempotent and honours a prior "keep"
decision. Watermark-free: the scan is over the whole (personal-scale) hub set each run, so a merge
that lands between runs simply drops the pair (a tombstoned hub is excluded).

Never raises (rule 7); depends on protocols so it unit-tests against fakes (08 testing policy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Protocol

from ..config import Settings
from ..db import Database
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.review_queue import KIND_ENTITY_DEDUP, ReviewItem, ReviewQueue
from ..vocab.service import VocabularyProvider, effective_vocabulary
from .merge_store import surface_forms

logger = logging.getLogger(__name__)

# agent_runs.agent name for the detector (the watermark source + visible in the activity feed, P8).
AGENT = "entity-dedup"


# --- Store shape + read surface -----------------------------------------------------------------


@dataclass(frozen=True)
class HubRow:
    """One entity hub + its live canonical neighbourhood, the raw material of a pair comparison.

    ``neighbor_ids`` is the set of node ids reachable by a canonical edge either direction
    (tombstoned endpoints excluded); its size is the hub's degree proxy for the survivor pick."""

    id: str
    type: str
    title: str | None
    aliases: list[str]
    neighbor_ids: frozenset[str]

    @property
    def degree(self) -> int:
        return len(self.neighbor_ids)


class EntityDedupStore(Protocol):
    """The read surface the detector runs over: all live hubs (with neighbourhoods) + the guard."""

    async def hub_rows(self, *, entity_like_types: list[str]) -> list[HubRow]:
        """Every live entity hub of the given types with its canonical neighbour-id set."""
        ...

    async def review_exists(self, node_a: str, node_b: str) -> bool:
        """True if an ``entity-dedup`` review item in **any** status already carries this canonical
        (``least``, ``greatest``) pair — the re-file guard (ADR-064 §4, mirroring ADR-049 §5)."""
        ...


# --- Pure gating logic (unit-tested without I/O) ------------------------------------------------


@dataclass(frozen=True)
class NameMatch:
    """A name-gate hit between two hubs. ``kind`` is ``exact`` (identical normalized form),
    ``containment`` (one hub's form's tokens are a subset of the other's), or ``fuzzy`` (a high
    ratio with no containment); ``score`` is 1.0 for exact, the token ratio for containment, and
    the fuzzy ratio for fuzzy."""

    kind: str
    score: float


def _significant(token: str, *, min_token_len: int) -> bool:
    return len(token) >= min_token_len


def name_match(
    a_forms: list[str], b_forms: list[str], *, min_token_len: int, fuzzy_min: float
) -> NameMatch | None:
    """The name gate between two hubs' normalized surface forms (title + aliases, already folded /
    lower-cased / collapsed by :func:`surface_forms`).

    Containment wins over fuzzy: if some form of one hub shares a token-subset relationship with a
    form of the other **and** the overlap includes a significant (>= ``min_token_len``) token, it is
    an ``exact``/``containment`` hit (the low-entropy guard stops "Ana"/initials anchoring a match).
    Otherwise the strongest whole-form fuzzy ratio decides, gated by ``fuzzy_min`` and the same
    length guard. ``None`` ⇒ the pair fails the name gate outright."""
    best_contain: NameMatch | None = None
    for fa in a_forms:
        ta = set(fa.split())
        for fb in b_forms:
            tb = set(fb.split())
            if not ta or not tb:
                continue
            shared = ta & tb
            if not (ta <= tb or tb <= ta):
                continue
            if not any(_significant(t, min_token_len=min_token_len) for t in shared):
                continue  # only short/low-entropy tokens overlap — not a real name match
            if ta == tb:
                return NameMatch(kind="exact", score=1.0)
            smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            ratio = len(smaller) / len(larger)
            if best_contain is None or ratio > best_contain.score:
                best_contain = NameMatch(kind="containment", score=ratio)
    if best_contain is not None:
        return best_contain

    best_fuzzy = 0.0
    for fa in a_forms:
        for fb in b_forms:
            if len(fa) < min_token_len or len(fb) < min_token_len:
                continue
            ratio = SequenceMatcher(None, fa, fb).ratio()
            if ratio > best_fuzzy:
                best_fuzzy = ratio
    if best_fuzzy >= fuzzy_min:
        return NameMatch(kind="fuzzy", score=best_fuzzy)
    return None


def shared_overlap(a: HubRow, b: HubRow) -> tuple[int, float]:
    """The shared-neighborhood signal: the count of common canonical neighbours (excluding the two
    hubs themselves) and the Jaccard of their neighbourhoods. A pair of genuinely-different people
    who merely share a first name overlaps here at 0 (the AND leg that suppresses them)."""
    a_nb = a.neighbor_ids - {a.id, b.id}
    b_nb = b.neighbor_ids - {a.id, b.id}
    shared = a_nb & b_nb
    union = a_nb | b_nb
    jaccard = len(shared) / len(union) if union else 0.0
    return len(shared), jaccard


def is_high_confidence(
    name: NameMatch, shared_count: int, *, high_min_shared: int, fuzzy_high: float
) -> bool:
    """A pair is inline-eligible (high-confidence) when the shared neighbourhood is strong enough
    **and** the name match is a containment / exact / strong-fuzzy hit (ADR-064 §4). A weak-fuzzy or
    thin-overlap pair stays low-confidence → filed to Review."""
    if shared_count < high_min_shared:
        return False
    if name.kind in ("exact", "containment"):
        return True
    return name.score >= fuzzy_high


def default_survivor(a: HubRow, b: HubRow) -> tuple[str, str]:
    """``(survivor_id, loser_id)`` for a pair: the **higher-degree** hub survives (keep the rich
    original, fold the thin duplicate into it — the Diana(45)/Diana Vance(4) case), tie broken by
    neighbourhood size then id so the pick is deterministic across runs."""
    if a.degree != b.degree:
        return (a.id, b.id) if a.degree > b.degree else (b.id, a.id)
    return (a.id, b.id) if a.id < b.id else (b.id, a.id)


# --- Candidate + outcome shapes -----------------------------------------------------------------


@dataclass(frozen=True)
class DedupPair:
    """One gated candidate pair, ready to surface (inline) or file (review)."""

    survivor: str
    loser: str
    survivor_title: str | None
    loser_title: str | None
    node_type: str
    name: NameMatch
    shared_count: int
    jaccard: float
    high_confidence: bool

    @property
    def canonical(self) -> tuple[str, str]:
        """The ``(least, greatest)`` id pair — the stable key for the re-file guard + payload."""
        return (
            (self.survivor, self.loser)
            if self.survivor < self.loser
            else (self.loser, self.survivor)
        )

    def _signals(self) -> dict[str, Any]:
        return {
            "name_match": {"kind": self.name.kind, "score": round(self.name.score, 4)},
            "shared_count": self.shared_count,
            "jaccard": round(self.jaccard, 4),
        }

    def inline_entry(self) -> dict[str, Any]:
        """The inline (high-confidence) feed row written to the run details — enough for the web to
        pre-fill the shared merge picker (survivor + loser) and show why (ADR-064 §4)."""
        return {
            "survivor": {"id": self.survivor, "title": self.survivor_title},
            "loser": {"id": self.loser, "title": self.loser_title},
            "type": self.node_type,
            "signals": self._signals(),
        }

    def review_payload(self) -> dict[str, Any]:
        """The ``entity-dedup`` review payload for a low-confidence pair. ``node_a``/``node_b`` are
        the canonical ids (the re-file guard key); ``default_survivor`` drives the merge unless the
        user overrides it — the fold unions aliases + records a durable decision (ADR-064 §1/§4)."""
        node_a, node_b = self.canonical
        return {
            "node_a": node_a,
            "node_b": node_b,
            "default_survivor": self.survivor,
            "type": self.node_type,
            "titles": {self.survivor: self.survivor_title, self.loser: self.loser_title},
            "signals": self._signals(),
        }


@dataclass
class EntityDedupOutcome:
    """One detector pass — feeds the ``entity-dedup`` agent_runs row (its details carry the inline
    high-confidence feed) + tests."""

    pairs_scanned: int = 0
    high_confidence: list[dict[str, Any]] = field(default_factory=list)
    low_confidence_filed: int = 0
    already_tracked: int = 0

    def summary(self) -> str:
        return (
            f"entity dedup: {len(self.high_confidence)} high-confidence pair(s) inline, "
            f"{self.low_confidence_filed} filed to review across {self.pairs_scanned} candidate "
            f"pair(s), {self.already_tracked} already tracked (skipped)"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs_scanned": self.pairs_scanned,
            "high_confidence": self.high_confidence,
            "low_confidence_filed": self.low_confidence_filed,
            "already_tracked": self.already_tracked,
        }


# --- The detector -------------------------------------------------------------------------------


class EntityDedupService:
    """Owns the nightly entity-hub dedup detection: hubs → gate each pair → high/low split."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: EntityDedupStore,
        review_queue: ReviewQueue,
        run_store: AgentRunStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._review = review_queue
        self._runs = run_store
        # Effective entity-like types (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds.
        self._vocab = vocab

    async def run_scheduled(self) -> EntityDedupOutcome | None:
        """The scheduler/CLI entry point. Opens the run, scans, closes it; never raises (P8).
        Returns the outcome for CLI logging, or ``None`` when the run couldn't open / failed."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for entity-dedup; skipped")
            return None
        try:
            outcome = await self._scan()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("entity-dedup failed")
            await self._safe_finish(run_id, exc)
            return None

    async def _scan(self) -> EntityDedupOutcome:
        effective = await effective_vocabulary(self._vocab, self._settings)
        entity_types = list(effective.entity_like_types)
        hubs = await self._store.hub_rows(entity_like_types=entity_types)

        pairs = self._candidate_pairs(hubs)
        # Strongest first, so the per-run cap keeps the best pairs: high-confidence before low, then
        # more shared neighbours, then a higher name score, then a stable id key.
        pairs.sort(
            key=lambda p: (p.high_confidence, p.shared_count, p.name.score, p.canonical),
            reverse=True,
        )

        outcome = EntityDedupOutcome(pairs_scanned=len(pairs))
        cap = self._settings.entity_dedup_max_pairs_per_run
        surfaced = 0
        for pair in pairs:
            if surfaced >= cap:
                break
            node_a, node_b = pair.canonical
            # Re-file guard (ADR-064 §4): a pair already in review (any status — incl. a prior
            # "keep") is skipped from BOTH surfaces so it isn't double-tracked / re-nagged.
            if await self._store.review_exists(node_a, node_b):
                outcome.already_tracked += 1
                continue
            if pair.high_confidence:
                outcome.high_confidence.append(pair.inline_entry())
            else:
                await self._file_review(pair)
                outcome.low_confidence_filed += 1
            surfaced += 1
        return outcome

    def _candidate_pairs(self, hubs: list[HubRow]) -> list[DedupPair]:
        """Every same-type hub pair that clears the strict AND gate (name AND shared-neighborhood),
        classified high/low. O(n^2) over the personal-scale hub set — no watermark needed."""
        s = self._settings
        by_type: dict[str, list[HubRow]] = {}
        for hub in hubs:
            by_type.setdefault(hub.type, []).append(hub)

        pairs: list[DedupPair] = []
        for node_type, group in by_type.items():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    name = name_match(
                        surface_forms(a.title, a.aliases),
                        surface_forms(b.title, b.aliases),
                        min_token_len=s.entity_dedup_min_token_len,
                        fuzzy_min=s.entity_dedup_fuzzy_min,
                    )
                    if name is None:
                        continue
                    shared_count, jaccard = shared_overlap(a, b)
                    if shared_count < s.entity_dedup_min_shared:
                        continue  # the AND leg — suppresses same-name different people (Diana Wren)
                    high = is_high_confidence(
                        name,
                        shared_count,
                        high_min_shared=s.entity_dedup_high_min_shared,
                        fuzzy_high=s.entity_dedup_fuzzy_high,
                    )
                    survivor, loser = default_survivor(a, b)
                    titles = {a.id: a.title, b.id: b.title}
                    pairs.append(
                        DedupPair(
                            survivor=survivor,
                            loser=loser,
                            survivor_title=titles[survivor],
                            loser_title=titles[loser],
                            node_type=node_type,
                            name=name,
                            shared_count=shared_count,
                            jaccard=jaccard,
                            high_confidence=high,
                        )
                    )
        return pairs

    async def _file_review(self, pair: DedupPair) -> None:
        await self._review.enqueue(
            ReviewItem(
                kind=KIND_ENTITY_DEDUP,
                payload=pair.review_payload(),
                excerpt=_excerpt(pair),
                source=AGENT,
            )
        )

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="entity-dedup failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close entity-dedup agent_runs row %s", run_id)


def _excerpt(pair: DedupPair) -> str:
    """A short decidable-in-place hint for the review list (the two hub titles + the evidence). The
    payload carries the ids + signals; this is just the human line the list shows."""
    left = pair.survivor_title or "(untitled)"
    right = pair.loser_title or "(untitled)"
    return (
        f'possible duplicate {pair.node_type}: "{left}" ~ "{right}" '
        f"({pair.name.kind} name, {pair.shared_count} shared)"
    )


# --- asyncpg implementation (plain SQL, no ORM — rule 5) ----------------------------------------


class PgEntityDedupStore:
    """asyncpg-backed detector reads — plain SQL, no ORM (ADR-011). Excludes tombstones on both the
    hub and its neighbours (a merged node is never a live hub nor a neighbourhood signal)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def hub_rows(self, *, entity_like_types: list[str]) -> list[HubRow]:
        if not entity_like_types:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH hubs AS (
                    SELECT id, type, title, aliases
                      FROM nodes
                     WHERE type = ANY($1::text[]) AND merged_into IS NULL
                ),
                nb AS (
                    SELECT h.id AS hub_id, x.nid AS neighbor_id
                      FROM hubs h
                      JOIN LATERAL (
                            SELECT e.dst_id AS nid FROM edges e
                             WHERE e.src_id = h.id AND e.origin = 'canonical'
                            UNION
                            SELECT e.src_id AS nid FROM edges e
                             WHERE e.dst_id = h.id AND e.origin = 'canonical'
                      ) x ON true
                      JOIN nodes m ON m.id = x.nid AND m.merged_into IS NULL
                )
                SELECT h.id, h.type, h.title, h.aliases,
                       coalesce(
                           array_agg(DISTINCT nb.neighbor_id)
                               FILTER (WHERE nb.neighbor_id IS NOT NULL),
                           '{}'
                       ) AS neighbor_ids
                  FROM hubs h
                  LEFT JOIN nb ON nb.hub_id = h.id
                 GROUP BY h.id, h.type, h.title, h.aliases
                """,
                entity_like_types,
            )
        return [
            HubRow(
                id=str(r["id"]),
                type=r["type"],
                title=r["title"],
                aliases=list(r["aliases"] or []),
                neighbor_ids=frozenset(str(x) for x in (r["neighbor_ids"] or [])),
            )
            for r in rows
        ]

    async def review_exists(self, node_a: str, node_b: str) -> bool:
        async with self._db.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM review_queue
                 WHERE kind = 'entity-dedup'
                   AND payload->>'node_a' = $1 AND payload->>'node_b' = $2
                 LIMIT 1
                """,
                node_a,
                node_b,
            )
        return row is not None


def build_entity_dedup_service(
    settings: Settings, db: Database, vocab: VocabularyProvider | None = None
) -> EntityDedupService:
    """Construct a standalone detector for the CLI (``python -m app.cli entity-dedup``) + the
    pipeline run-now. DB-only (hub reads + review-queue writes / its own run row, no store git);
    ``vocab`` is the effective-vocabulary provider (seeds ∪ approved additions), ``None`` ⇒ seeds.
    """
    from ..services.agent_runs import PgAgentRunStore
    from ..services.review_queue import PgReviewQueue

    return EntityDedupService(
        settings=settings,
        store=PgEntityDedupStore(db),
        review_queue=PgReviewQueue(db),
        run_store=PgAgentRunStore(db),
        vocab=vocab,
    )
