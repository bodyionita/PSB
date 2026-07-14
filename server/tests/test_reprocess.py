"""ReprocessService tests (ADR-042, M3 task 11) — fakes only, no live DB/LLM (08 testing policy).

Verifies the reset → chronological replay → recompute → force-commit pass, the single-flight guard,
and the preview + standing-merge reporting.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.graph.node_writer import NodeDocument, NodeWriter
from app.services.capture_pipeline import ReprocessOne
from app.services.reprocess import ReprocessService

from .fakes import FakeAgentRunStore, FakeCommitBackup

CREATED = datetime(2026, 7, 14, 12, 0, 0)


class FakeReprocessStore:
    def __init__(self, *, ids: list[str], nodes: int = 0, merges: int = 0) -> None:
        self._ids = ids
        self._nodes = nodes
        self._merges = merges
        self.reset_called = 0

    async def counts(self):
        return len(self._ids), self._nodes

    async def count_merges(self):
        return self._merges

    async def reset_derived_and_review(self):
        self.reset_called += 1

    async def capture_ids_chronological(self):
        return list(self._ids)


class FakeReprocessor:
    """Records the order captures are replayed; each returns a scripted outcome."""

    def __init__(self, *, fail: set[str] | None = None, inbox: set[str] | None = None) -> None:
        self.order: list[str] = []
        self._fail = fail or set()
        self._inbox = inbox or set()

    async def reprocess_capture(self, capture_id: str) -> ReprocessOne:
        self.order.append(capture_id)
        if capture_id in self._fail:
            return ReprocessOne(capture_id=capture_id, ok=False, error="boom")
        return ReprocessOne(
            capture_id=capture_id, ok=True, node_count=2,
            used_inbox_fallback=capture_id in self._inbox,
        )


class FakeGraph:
    def __init__(self) -> None:
        self.recomputes = 0

    async def recompute(self):
        self.recomputes += 1
        return object()


def _service(tmp_path: Path, store: FakeReprocessStore, reprocessor: FakeReprocessor,
             *, graph: FakeGraph | None = None):
    settings = Settings(graph_store_path=str(tmp_path / "store"), scheduler_tz="UTC")
    return ReprocessService(
        settings=settings,
        store=store,
        reprocessor=reprocessor,
        node_writer=NodeWriter(str(tmp_path / "store")),
        store_backup=FakeCommitBackup(),
        run_store=FakeAgentRunStore(),
        graph=graph,
    ), settings


async def test_preview_reports_counts_no_writes(tmp_path: Path):
    store = FakeReprocessStore(ids=["a", "b"], nodes=7, merges=1)
    service, _ = _service(tmp_path, store, FakeReprocessor())
    preview = await service.preview()
    assert (preview.captures, preview.nodes, preview.merges) == (2, 7, 1)
    assert store.reset_called == 0  # preview never resets


async def test_apply_resets_replays_chronologically_and_commits(tmp_path: Path):
    # Seed some node files so the store reset has something to remove.
    writer = NodeWriter(str(tmp_path / "store"))
    writer.write_nodes([
        NodeDocument(id="n1", type="memory", title="x", body="b",
                     created_local=CREATED, source="text"),
        NodeDocument(id="n2", type="person", title="Alex", body="",
                     created_local=CREATED, source="text"),
    ])
    store = FakeReprocessStore(ids=["old", "mid", "new"])
    reprocessor = FakeReprocessor()
    graph = FakeGraph()
    service, _ = _service(tmp_path, store, reprocessor, graph=graph)

    run_id = await service.apply()
    assert run_id is not None
    await service.drain()

    assert store.reset_called == 1
    assert reprocessor.order == ["old", "mid", "new"]  # chronological (ADR-042 §1)
    assert graph.recomputes == 1  # derived edges rebuilt
    # Store files removed by the reset.
    assert not list((tmp_path / "store").rglob("*.md"))
    # Run finished succeeded with a human-readable summary.
    runs = service._runs.runs  # FakeAgentRunStore
    run = next(iter(runs.values()))
    assert run.status == "succeeded"
    assert "3/3 captures re-ingested" in run.summary


async def test_apply_counts_failures_without_aborting(tmp_path: Path):
    store = FakeReprocessStore(ids=["a", "b", "c"])
    reprocessor = FakeReprocessor(fail={"b"}, inbox={"c"})
    service, _ = _service(tmp_path, store, reprocessor)
    await service.apply()
    await service.drain()

    assert reprocessor.order == ["a", "b", "c"]  # all replayed despite b failing
    run = next(iter(service._runs.runs.values()))
    assert run.status == "succeeded"
    assert run.details["reingested"] == 2
    assert run.details["failed"] == 1
    assert run.details["inbox_fallback"] == 1


async def test_apply_is_single_flight(tmp_path: Path):
    store = FakeReprocessStore(ids=["a"])
    service, _ = _service(tmp_path, store, FakeReprocessor())
    service._running = True  # simulate an in-flight reprocess
    assert await service.apply() is None  # 409 → None


async def test_apply_reports_standing_merges(tmp_path: Path):
    store = FakeReprocessStore(ids=["a"], merges=2)
    service, _ = _service(tmp_path, store, FakeReprocessor())
    await service.apply()
    await service.drain()
    run = next(iter(service._runs.runs.values()))
    assert run.details["standing_merges_not_reapplied"] == 2
    assert "standing merge" in run.summary
