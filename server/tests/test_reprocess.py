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

    def __init__(
        self,
        *,
        fail: set[str] | None = None,
        inbox: set[str] | None = None,
        coerced: dict[str, int] | None = None,
        accreted: dict[str, int] | None = None,
    ) -> None:
        self.order: list[str] = []
        self._fail = fail or set()
        self._inbox = inbox or set()
        self._coerced = coerced or {}
        self._accreted = accreted or {}

    async def reprocess_capture(self, capture_id: str) -> ReprocessOne:
        self.order.append(capture_id)
        if capture_id in self._fail:
            return ReprocessOne(capture_id=capture_id, ok=False, error="boom")
        return ReprocessOne(
            capture_id=capture_id,
            ok=True,
            node_count=2,
            used_inbox_fallback=capture_id in self._inbox,
            coerced=self._coerced.get(capture_id, 0),
            accreted=self._accreted.get(capture_id, 0),
        )


class FakeGraph:
    def __init__(self, *, marker: list[str] | None = None) -> None:
        self.recomputes = 0
        self._marker = marker

    async def recompute(self):
        self.recomputes += 1
        if self._marker is not None:
            self._marker.append("graph")
        return object()


class _FakeProfileOutcome:
    def __init__(self, refreshed: int) -> None:
        self.refreshed = refreshed


class FakeProfileRefresh:
    """Records the profile-rebuild trigger + its order vs the derived recompute."""

    def __init__(self, *, refreshed: int = 0, marker: list[str] | None = None) -> None:
        self.calls = 0
        self._refreshed = refreshed
        self._marker = marker

    async def run_scheduled(self):
        self.calls += 1
        if self._marker is not None:
            self._marker.append("profiles")
        return _FakeProfileOutcome(self._refreshed)


def _service(
    tmp_path: Path,
    store: FakeReprocessStore,
    reprocessor: FakeReprocessor,
    *,
    graph: FakeGraph | None = None,
    profile_refresh: FakeProfileRefresh | None = None,
):
    settings = Settings(graph_store_path=str(tmp_path / "store"), scheduler_tz="UTC")
    return ReprocessService(
        settings=settings,
        store=store,
        reprocessor=reprocessor,
        node_writer=NodeWriter(str(tmp_path / "store")),
        store_backup=FakeCommitBackup(),
        run_store=FakeAgentRunStore(),
        graph=graph,
        profile_refresh=profile_refresh,
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
    writer.write_nodes(
        [
            NodeDocument(
                id="n1", type="memory", title="x", body="b", created_local=CREATED, source="text"
            ),
            NodeDocument(
                id="n2", type="person", title="Alex", body="", created_local=CREATED, source="text"
            ),
        ]
    )
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


async def test_apply_rebuilds_profiles_after_derived_edges(tmp_path: Path):
    """The reset truncates node_profiles; the reprocess rebuilds them (ADR-037 search leg) AFTER the
    derived-edge recompute, and reports the count in the run (follow-up: no silent empty-profile
    window until the nightly job)."""
    marker: list[str] = []
    store = FakeReprocessStore(ids=["a", "b"])
    graph = FakeGraph(marker=marker)
    profiles = FakeProfileRefresh(refreshed=5, marker=marker)
    service, _ = _service(tmp_path, store, FakeReprocessor(), graph=graph, profile_refresh=profiles)
    await service.apply()
    await service.drain()

    assert profiles.calls == 1
    assert marker == ["graph", "profiles"]  # profiles rebuilt over the recomputed graph
    run = next(iter(service._runs.runs.values()))
    assert run.details["profiles_refreshed"] == 5
    assert "5 profile(s)" in run.summary


async def test_apply_without_profile_refresh_reports_zero(tmp_path: Path):
    """No refresher wired (defensive default) ⇒ 0 profiles, no crash."""
    store = FakeReprocessStore(ids=["a"])
    service, _ = _service(tmp_path, store, FakeReprocessor())  # profile_refresh=None
    await service.apply()
    await service.drain()
    run = next(iter(service._runs.runs.values()))
    assert run.details["profiles_refreshed"] == 0


async def test_apply_aggregates_coerced_and_accreted_totals(tmp_path: Path):
    """Per-capture coercions (ADR-039) + accretions (ADR-040 §4) sum into the run detail + summary,
    so a reprocess heal is auditable (reviewer #3 follow-up)."""
    store = FakeReprocessStore(ids=["a", "b", "c"])
    reprocessor = FakeReprocessor(
        coerced={"a": 2, "c": 1},  # 3 total
        accreted={"b": 1, "c": 2},  # 3 total
    )
    service, _ = _service(tmp_path, store, reprocessor)
    await service.apply()
    await service.drain()
    run = next(iter(service._runs.runs.values()))
    assert run.details["coerced"] == 3
    assert run.details["accreted"] == 3
    assert "3 coerced, 3 accreted" in run.summary
