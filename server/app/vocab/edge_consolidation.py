"""Edge retro-consolidation — the on-demand ``POST /admin/vocab/consolidate`` (ADR-036 / task 7b).

Approving a new **edge rel** makes it forward-live (task 7a) and opens a marker
``vocab-consolidation`` run. This module is the *retro* half: a manual, human-in-the-loop two-step
(exactly the ADR-024 tag-consolidation shape) that re-types **existing** edges onto the new rel.

  * :meth:`EdgeConsolidationService.propose` — synchronous, **no writes**. Feeds a bounded
    inventory of existing canonical edges (endpoints + current rel + a source excerpt) to the
    distill chain and asks which would be more accurately typed as the new rel; returns
    ``{plan_id, rel, retypings}``. A down chain surfaces as :class:`ProviderUnavailable` (→ 503).
  * :meth:`EdgeConsolidationService.apply` — takes a (reviewed) plan, opens a
    ``vocab-consolidation`` run, and rewrites each edge's ``rel:`` frontmatter + reindexes the
    touched files + force-commits in the **background** (202 ``{run_id}``). Same never-lose envelope
    as tag consolidation: atomic writes, git-tracked + revertible, skip-and-continue per edge.

ADR-036 scopes M3 to **re-typing existing edges only** — inventing brand-new edges from node bodies
is a deferred follow-up, and node re-typing stays propose-only (ADR-035). The pure helpers (prompt,
parse, sanitise) take no I/O so they unit-test with no mocks (08 testing policy); the service
depends on protocols (edge store, registry, indexer, committer, run store) so it too uses fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass

from ..config import Settings
from ..graph.node_writer import NodeWriter
from ..indexing.indexer import IndexOutcome, NodeIndexer
from ..providers.base import ChatMessage
from ..providers.registry import ProviderRegistry
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.store_backup import StoreCommitter
from .consolidation import AGENT
from .edge_store import EdgeCandidate, EdgeConsolidationStore
from .service import VocabularyProvider, effective_vocabulary

logger = logging.getLogger(__name__)

# Versioned prompt (bump the suffix on any wording change, mirroring the organizer / tag propose).
EDGE_CONSOLIDATION_PROMPT_VERSION = "edge-consolidate-v1"

EDGE_CONSOLIDATION_SYSTEM_PROMPT = """\
A new edge relation "{rel}" was just added to a personal knowledge graph's vocabulary. Below is a
sample of edges that already exist, each with an index, its two endpoint nodes, and the relation it
currently uses. Some of them may have been forced onto a less-precise relation because "{rel}" did
not exist yet.

Pick ONLY the edges whose meaning is genuinely "{rel}" — a clearly better fit than their current
relation. Do NOT pick an edge that is merely related; when in doubt, leave it as it is. Re-typing is
reversible but should still be conservative.

Return ONLY a JSON object, no prose, in exactly this shape:
{"retype": [<index>, ...]}

If none should be re-typed, return {"retype": []}.

Edges:
{edges}
"""

_MAX_EXCERPT = 160


@dataclass(frozen=True)
class EdgeRetype:
    """One sanitised re-typing: the edge ``{rel: from_rel, to}`` on node ``src_id`` becomes
    ``to_rel``. Uniquely identifies a frontmatter edge line (src + target + current rel)."""

    src_id: str
    to: str
    from_rel: str
    to_rel: str


@dataclass(frozen=True)
class EdgeConsolidationProposal:
    """A propose result: a correlation id, target rel, and the sanitised re-typings (no writes)."""

    plan_id: str
    rel: str
    retypings: list[EdgeRetype]


def render_edge_inventory(
    candidates: list[EdgeCandidate], *, excerpt_len: int = _MAX_EXCERPT
) -> str:
    """Render the numbered ``[i] src —rel→ dst`` lines injected into the propose prompt.

    The index is what the model returns; a short single-lined source excerpt gives it context
    without blowing up the prompt. Missing titles render as ``(untitled)``.
    """
    lines: list[str] = []
    for i, c in enumerate(candidates):
        src = c.src_title or "(untitled)"
        dst = c.dst_title or "(untitled)"
        lines.append(f'[{i}] "{src}" --{c.rel}--> "{dst}"')
        excerpt = _one_line(c.src_excerpt)
        if excerpt:
            lines.append(f"    source: {excerpt[:excerpt_len]}")
    return "\n".join(lines)


def _one_line(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_retype_plan(text: str) -> list[int]:
    """Best-effort parse of the model's ``{"retype": [ints]}`` into a list of indices.

    Tolerates code fences / surrounding prose (mirrors the tag/organizer parse). Malformed or
    non-conforming output yields ``[]`` — the caller then proposes nothing rather than erroring.
    """
    obj = _loads(text)
    if not isinstance(obj, dict):
        return []
    raw = obj.get("retype")
    if not isinstance(raw, list):
        return []
    # bool is an int subclass — exclude it so `true` in the array isn't read as index 1.
    return [i for i in raw if isinstance(i, int) and not isinstance(i, bool)]


def _loads(text: str) -> object | None:
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def retypes_from_indices(
    indices: list[int], candidates: list[EdgeCandidate], *, to_rel: str
) -> list[EdgeRetype]:
    """Resolve model-returned indices against the candidate inventory into sanitised re-typings."""
    pairs = [
        (candidates[i].src_id, candidates[i].dst_id, candidates[i].rel)
        for i in indices
        if 0 <= i < len(candidates)
    ]
    return clean_retypes(pairs, to_rel=to_rel)


def clean_retypes(pairs: list[tuple[str, str, str]], *, to_rel: str) -> list[EdgeRetype]:
    """Sanitise ``(src_id, to, from_rel)`` triples into safe :class:`EdgeRetype`s.

    - Empty members drop out.
    - A no-op (``from_rel`` already the target rel) drops out.
    - De-duplicated by ``(src_id, to, from_rel)`` — the unique identity of a frontmatter edge line.

    The propose path passes triples resolved from model indices; apply passes the reviewed plan back
    (trusting the human's picks but still slugging/dropping trivial ones — mirrors tag apply).
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[EdgeRetype] = []
    for src_id_raw, to_raw, from_rel_raw in pairs:
        src_id, to, from_rel = src_id_raw.strip(), to_raw.strip(), from_rel_raw.strip()
        if not (src_id and to and from_rel):
            continue
        if from_rel == to_rel:
            continue
        key = (src_id, to, from_rel)
        if key in seen:
            continue
        seen.add(key)
        out.append(EdgeRetype(src_id=src_id, to=to, from_rel=from_rel, to_rel=to_rel))
    return out


class BadConsolidation(Exception):
    """The consolidation request is invalid — an unknown/empty edge rel (400)."""


class EdgeConsolidationService:
    """Owns propose (LLM plan) + apply (rewrite rels + reindex) — the edge half of ADR-027/036."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: EdgeConsolidationStore,
        node_writer: NodeWriter,
        registry: ProviderRegistry,
        indexer: NodeIndexer,
        store_backup: StoreCommitter,
        run_store: AgentRunStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._writer = node_writer
        self._registry = registry
        self._indexer = indexer
        self._backup = store_backup
        self._runs = run_store
        # Effective edge rels (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds.
        self._vocab = vocab
        self._tasks: set[asyncio.Task] = set()

    # --- propose ----------------------------------------------------------------------------

    async def propose(self, rel: str) -> EdgeConsolidationProposal:
        """Compute edge re-typing candidates for the (already-approved) ``rel`` (no writes).

        Raises :class:`BadConsolidation` (400) for an unknown/empty rel and
        :class:`ProviderUnavailable` (→ 503) when the distill chain is exhausted; an empty or
        non-conforming model reply yields an empty plan, not an error.
        """
        rel = await self._validated_rel(rel)
        plan_id = uuid.uuid4().hex
        candidates = await self._store.edge_inventory(
            exclude_rel=rel, limit=self._settings.vocab_consolidate_max_edges
        )
        if not candidates:
            # Nothing to consolidate — don't spend a model call on an edge-less graph.
            return EdgeConsolidationProposal(plan_id=plan_id, rel=rel, retypings=[])

        system = EDGE_CONSOLIDATION_SYSTEM_PROMPT.replace("{rel}", rel).replace(
            "{edges}", render_edge_inventory(candidates)
        )
        result = await self._registry.distill([ChatMessage(role="system", content=system)])
        retypings = retypes_from_indices(parse_retype_plan(result.text), candidates, to_rel=rel)
        logger.info(
            "vocab consolidate propose: %d re-typing(s) over %d edge(s) → '%s'",
            len(retypings),
            len(candidates),
            rel,
        )
        return EdgeConsolidationProposal(plan_id=plan_id, rel=rel, retypings=retypings)

    # --- apply ------------------------------------------------------------------------------

    async def apply(self, rel: str, plan: list[EdgeRetype]) -> str:
        """Open the run and rewrite the reviewed edges' rels in the background; return its run_id.

        The plan is re-sanitised (trust the human's picks but drop no-ops/dupes) before any write,
        so a malformed apply body is harmless. Validation is synchronous so a bad rel gets a 400
        (not a failed run); the store mutation runs in the background (endpoint answers 202)."""
        rel = await self._validated_rel(rel)
        # ``to_rel`` is forced to the server-validated ``rel`` (the client's ``to_rel`` is ignored)
        # so a plan can only ever re-type onto the approved rel, never an arbitrary one.
        retypings = clean_retypes([(r.src_id, r.to, r.from_rel) for r in plan], to_rel=rel)
        run_id = await self._runs.start(AGENT)
        self._spawn(self._run_apply(run_id, rel, retypings))
        return run_id

    async def _run_apply(self, run_id: str, rel: str, retypings: list[EdgeRetype]) -> None:
        """Re-type each edge's ``rel:`` line, reindex the changed files, commit+push.

        Never raises (rule 7): a per-edge read/write error is logged + skipped; any unexpected
        failure ends the run ``failed`` with context. The graph store is truth (rule 1), so a
        partial apply is safe to re-drive."""
        try:
            src_ids = list({r.src_id for r in retypings})
            paths = await self._store.store_paths_for(src_ids) if src_ids else {}
            changed: list[str] = []
            retyped = 0
            skipped = 0
            for r in retypings:
                path = paths.get(r.src_id)
                if path is None:
                    logger.warning(
                        "vocab consolidate: source %s not indexed/tombstoned; skipped", r.src_id
                    )
                    skipped += 1
                    continue
                try:
                    count = await asyncio.to_thread(
                        self._writer.retype_edge,
                        path,
                        to=r.to,
                        from_rel=r.from_rel,
                        to_rel=r.to_rel,
                    )
                except Exception:  # noqa: BLE001 — one bad file must not abort the apply (rule 7)
                    logger.exception("vocab consolidate: failed to re-type %s; skipped", path)
                    skipped += 1
                    continue
                if count:
                    retyped += count
                    if path not in changed:
                        changed.append(path)

            index = await self._indexer.index_paths(changed) if changed else IndexOutcome()
            committed = pushed = False
            if changed:
                backup = await self._backup.backup_now(f"vocab consolidate: edges → {rel}")
                committed, pushed = backup.committed, backup.pushed

            summary = (
                f"vocab consolidate: {retyped} edge(s) re-typed → '{rel}' "
                f"across {len(changed)} file(s)"
            )
            if skipped:
                summary += f", {skipped} skipped"
            if index.partial:
                # An embed skip left a rewritten node transiently stale; the store is correct and
                # next reindex heals it (rule 1). Surface it as reindex does.
                summary += " (partial — embed failures)"
            logger.info("%s (pushed=%s)", summary, pushed)
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=summary,
                details={
                    "rel": rel,
                    "edges_retyped": retyped,
                    "files_changed": len(changed),
                    "edges_skipped": skipped,
                    "index": index.as_dict(),
                    "commit": {"committed": committed, "pushed": pushed},
                },
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("vocab consolidate apply failed")
            await self._safe_finish(run_id, exc)

    # --- helpers ----------------------------------------------------------------------------

    async def _validated_rel(self, rel: str) -> str:
        rel = rel.strip()
        effective = await effective_vocabulary(self._vocab, self._settings)
        if not rel or rel not in effective.edge_rels:
            raise BadConsolidation(f"unknown edge rel {rel!r}")
        return rel

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="vocab consolidate failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close vocab-consolidate agent_runs row %s", run_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Await any in-flight background apply (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
