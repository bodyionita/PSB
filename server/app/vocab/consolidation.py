"""The ``vocab-consolidation`` job (ADR-027 §3 / ADR-035, M3 task 7).

Approving a type proposal makes it **live** (the Vocabulary service writes it to ``app_settings``
before opening this job) and then opens a ``vocab-consolidation`` run to retro-consolidate the
existing graph. Per [ADR-035](../../second-brain-docs/adr/035-vocabulary-consolidation-scope-m3.md):

  * **task 7a (here):** approval opens a run that records the mutation — the type is forward-live,
    and the run is the feed-visible marker that consolidation was triggered (vision P8). It replaces
    task 4's ``SKIPPED`` queued marker with a ``SUCCEEDED`` run reflecting the live mutation.
  * **task 7b (follow-up):** the retro-walk itself — for a new **edge rel** propose→apply edge
    re-typings (frontmatter rewrites, confirm-gated via ``POST /admin/vocab/consolidate``); for a
    new **node type** surface candidate re-typings **propose-only** (the folder-move/re-slug apply
    machinery is deferred with its own ADR, ADR-035). Extension points are marked below.

The service depends only on the :class:`~app.services.agent_runs.AgentRunStore` protocol here, so it
unit-tests against a fake (no live DB/LLM — 08 testing policy).
"""

from __future__ import annotations

import logging

from ..services.agent_runs import SUCCEEDED, AgentRunStore

logger = logging.getLogger(__name__)

# agent_runs.agent name for the consolidation run (visible in the activity feed, vision P8). The
# task-4 marker used the same name; task 7 keeps it so the feed history is continuous.
AGENT = "vocab-consolidation"


class VocabConsolidation:
    """Opens the ``vocab-consolidation`` run an approval triggers (:class:`ConsolidationLauncher`).

    task 7a: the run records the now-live vocabulary mutation. The retro-walk propose/apply (7b)
    will hang off this same service + agent name.
    """

    def __init__(self, *, run_store: AgentRunStore) -> None:
        self._runs = run_store

    async def start(self, *, vocab: str, value: str, review_id: str) -> str | None:
        """Open + close a ``vocab-consolidation`` run for a newly approved type; returns its id.

        Best-effort (rule 7): the vocabulary mutation has already been committed by the caller, so a
        run-store hiccup here must not fail the approval — it just means the feed lacks the marker.
        """
        try:
            run_id = await self._runs.start(AGENT)
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=(
                    f"vocabulary approved: {vocab} '{value}' is now live; "
                    "retro-consolidation of existing nodes/edges is a follow-up (ADR-035)"
                ),
                details={
                    "vocab": vocab,
                    "value": value,
                    "review_id": review_id,
                    # 7b will add the propose plan (edge re-typings / node candidates) here.
                    "retro_consolidation": "pending",
                },
            )
            return run_id
        except Exception:  # noqa: BLE001 — a run-store hiccup must not fail an applied approval
            logger.exception("could not open the vocab-consolidation run (ignored)")
            return None
