"""OccurredEnrichmentService tests (ADR-056 §7, M8.2 Task 3-E).

The nightly flagger files one ``occurred-enrichment`` review item per undated content node the store
returns, records the count in its ``agent_runs`` row, and never raises (rule 7). The idempotency +
content-only filtering live in the store's SQL (covered by the real-PG smoke); here we prove the
service files correct payloads, bounds by config, and closes its run.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.agent_runs import SUCCEEDED
from app.services.occurred_enrichment import (
    AGENT,
    OccurredEnrichmentService,
    UndatedNode,
)
from app.services.review_queue import KIND_OCCURRED_ENRICHMENT

from .fakes import FakeAgentRunStore, FakeReviewQueue


class FakeEnrichmentStore:
    def __init__(self, candidates: list[UndatedNode]) -> None:
        self._candidates = candidates
        self.args: dict | None = None

    async def undated_content_nodes(self, *, entity_types, inbox_prefix, exclude_statuses, limit):
        self.args = {
            "entity_types": entity_types,
            "inbox_prefix": inbox_prefix,
            "exclude_statuses": exclude_statuses,
            "limit": limit,
        }
        return self._candidates[:limit]


def _service(candidates, review=None, runs=None, **overrides):
    settings = Settings(scheduler_tz="UTC", **overrides)
    store = FakeEnrichmentStore(candidates)
    return (
        OccurredEnrichmentService(
            settings=settings,
            store=store,
            review_queue=review or FakeReviewQueue(),
            run_store=runs or FakeAgentRunStore(),
        ),
        store,
    )


@pytest.mark.asyncio
async def test_files_one_item_per_undated_node():
    review = FakeReviewQueue()
    runs = FakeAgentRunStore()
    candidates = [
        UndatedNode(id="n1", title="A trip", type="memory"),
        UndatedNode(id="n2", title="An idea", type="idea"),
    ]
    service, _store = _service(candidates, review=review, runs=runs)
    outcome = await service.run_scheduled()

    assert outcome is not None and outcome.filed == 2 and outcome.candidates == 2
    assert [i.kind for i in review.items] == [KIND_OCCURRED_ENRICHMENT] * 2
    first = review.items[0]
    assert first.payload == {"node_id": "n1", "title": "A trip", "type": "memory"}
    assert first.excerpt == "A trip" and first.source == AGENT and first.source_ref == "n1"
    # The run is opened + closed succeeded with the filed count in details.
    run = next(iter(runs.runs.values()))
    assert run.agent == AGENT and run.status == SUCCEEDED
    assert run.details == {"filed": 2, "candidates": 2}


@pytest.mark.asyncio
async def test_bounds_by_config_max_per_run():
    candidates = [UndatedNode(id=f"n{i}", title=f"t{i}", type="memory") for i in range(5)]
    service, store = _service(candidates, occurred_enrichment_max_per_run=2)
    outcome = await service.run_scheduled()
    assert outcome is not None and outcome.filed == 2
    assert store.args["limit"] == 2


@pytest.mark.asyncio
async def test_no_candidates_files_nothing_but_still_runs():
    review = FakeReviewQueue()
    runs = FakeAgentRunStore()
    service, _store = _service([], review=review, runs=runs)
    outcome = await service.run_scheduled()
    assert outcome is not None and outcome.filed == 0
    assert review.items == []
    assert next(iter(runs.runs.values())).status == SUCCEEDED


@pytest.mark.asyncio
async def test_passes_entity_types_and_inbox_prefix_to_store():
    service, store = _service([], planes=["Ideas"])
    await service.run_scheduled()
    assert store.args["inbox_prefix"].endswith("/%")
    assert isinstance(store.args["entity_types"], list)


@pytest.mark.asyncio
async def test_excludes_dismissed_so_a_dismiss_sticks():
    # A dismissed (discarded) occurred-enrichment item must keep the node out of the flag set — else
    # a nightly rerun re-asks forever (rule 6). The store filters on pending/maybe/discarded.
    from app.services.review_queue import STATUS_DISCARDED, STATUS_MAYBE, STATUS_PENDING

    service, store = _service([])
    await service.run_scheduled()
    assert set(store.args["exclude_statuses"]) == {
        STATUS_PENDING,
        STATUS_MAYBE,
        STATUS_DISCARDED,
    }
