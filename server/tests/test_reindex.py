"""ReindexService tests — the combined pass + single-flight, with fakes (no live DB/git).

Covers the ADR-023 §4 / 04 §5 contract: the pass runs pull → rescan → recompute → one
commit+push in that order; the run lands in an ``agent="reindex"`` row with the trigger + counts;
a partial index is flagged in details but the run still succeeds; a failure ends the run failed
and releases the slot; and the single-flight guard rejects a concurrent manual reindex (409) and
skips the nightly job while one is in flight.
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.graph.service import GraphOutcome
from app.indexing.indexer import IndexOutcome
from app.services.agent_runs import FAILED, SUCCEEDED
from app.services.reindex import AGENT, ReindexService
from app.services.vault_backup import BackupResult

from .fakes import FakeAgentRunStore


class FakeReindexer:
    """Records the full-rescan call; returns a preset outcome or raises a preset error."""

    def __init__(
        self,
        *,
        order: list,
        outcome: IndexOutcome | None = None,
        error: Exception | None = None,
    ):
        self._order = order
        self.outcome = outcome or IndexOutcome(indexed=3, skipped=1, deleted=2)
        self.error = error
        self.calls = 0

    async def reindex_all(self) -> IndexOutcome:
        self.calls += 1
        self._order.append("reindex")
        if self.error is not None:
            raise self.error
        return self.outcome


class FakeGraph:
    """Records the recompute call; returns a preset GraphOutcome."""

    def __init__(self, *, order: list, outcome: GraphOutcome | None = None):
        self._order = order
        self.outcome = outcome or GraphOutcome(
            notes=3, links=4, blocks_written=2, commit_requested=True
        )
        self.calls = 0

    async def recompute(self) -> GraphOutcome:
        self.calls += 1
        self._order.append("recompute")
        return self.outcome


class FakeVaultSync:
    """Records sync_from_remote + backup_now (order + reason). ``gate`` blocks the pull so a test
    can hold a run mid-flight and probe the single-flight guard."""

    def __init__(
        self,
        *,
        order: list,
        result: BackupResult | None = None,
        gate: asyncio.Event | None = None,
    ):
        self._order = order
        self.result = result or BackupResult(committed=True, pushed=True)
        self.gate = gate
        self.backup_reasons: list[str] = []

    async def sync_from_remote(self) -> None:
        self._order.append("sync")
        if self.gate is not None:
            await self.gate.wait()

    async def backup_now(self, reason: str = "manual backup") -> BackupResult:
        self._order.append("backup")
        self.backup_reasons.append(reason)
        return self.result


def _service(
    *,
    order: list,
    indexer: FakeReindexer | None = None,
    graph: FakeGraph | None = None,
    vault: FakeVaultSync | None = None,
    runs: FakeAgentRunStore | None = None,
) -> tuple[ReindexService, FakeAgentRunStore, FakeVaultSync]:
    runs = runs or FakeAgentRunStore()
    vault = vault or FakeVaultSync(order=order)
    service = ReindexService(
        settings=Settings(),
        indexer=indexer or FakeReindexer(order=order),
        graph=graph or FakeGraph(order=order),
        vault_backup=vault,
        run_store=runs,
    )
    return service, runs, vault


async def test_run_scheduled_runs_the_pass_in_order_and_records_the_run():
    order: list = []
    service, runs, vault = _service(order=order)

    await service.run_scheduled()

    # pull → rescan → recompute → one commit+push, exactly once each, in order.
    assert order == ["sync", "reindex", "recompute", "backup"]
    assert vault.backup_reasons == ["reindex (nightly)"]
    assert not service.running  # slot released

    run = next(iter(runs.runs.values()))
    assert run.agent == AGENT
    assert run.status == SUCCEEDED
    assert run.details["trigger"] == "nightly"
    assert run.details["partial"] is False
    assert run.details["index"] == {
        "indexed": 3, "skipped": 1, "failed": 0, "deleted": 2, "partial": False, "failures": [],
    }
    assert run.details["graph"]["links"] == 4
    assert run.details["commit"] == {"committed": True, "pushed": True}
    assert "3 indexed" in run.summary


async def test_start_manual_returns_run_id_and_completes_in_background():
    order: list = []
    service, runs, _ = _service(order=order)

    run_id = await service.start_manual()
    assert run_id is not None
    await service.drain()

    assert order == ["sync", "reindex", "recompute", "backup"]
    run = runs.runs[run_id]
    assert run.status == SUCCEEDED
    assert run.details["trigger"] == "manual"
    assert not service.running


async def test_single_flight_rejects_concurrent_manual_and_skips_nightly():
    order: list = []
    gate = asyncio.Event()
    vault = FakeVaultSync(order=order, gate=gate)  # first run blocks in the pull
    service, runs, _ = _service(order=order, vault=vault)

    run_id = await service.start_manual()  # claims the slot; background task blocks on the gate
    assert run_id is not None
    assert service.running

    # A second manual trigger is rejected (→ 409) and the nightly job skips — no new run opened.
    assert await service.start_manual() is None
    await service.run_scheduled()
    assert len(runs.runs) == 1

    gate.set()  # let the first run finish
    await service.drain()
    assert not service.running

    # The slot is free again: a fresh reindex runs.
    assert await service.start_manual() is not None
    await service.drain()
    assert len(runs.runs) == 2


async def test_partial_index_is_flagged_in_details_but_the_run_succeeds():
    order: list = []
    indexer = FakeReindexer(
        order=order,
        outcome=IndexOutcome(indexed=2, skipped=0, failed=1, deleted=0, failures=["Ideas/x.md"]),
    )
    service, runs, _ = _service(order=order, indexer=indexer)

    await service.run_scheduled()

    run = next(iter(runs.runs.values()))
    assert run.status == SUCCEEDED  # partial is a details flag, not a run status
    assert run.details["partial"] is True
    assert run.details["index"]["failed"] == 1
    assert "partial" in run.summary


async def test_a_failure_ends_the_run_failed_and_releases_the_slot():
    order: list = []
    indexer = FakeReindexer(order=order, error=RuntimeError("boom"))
    graph = FakeGraph(order=order)
    vault = FakeVaultSync(order=order)
    service, runs, _ = _service(order=order, indexer=indexer, graph=graph, vault=vault)

    await service.run_scheduled()

    run = next(iter(runs.runs.values()))
    assert run.status == FAILED
    assert "boom" in (run.error or "")
    assert run.details == {"trigger": "nightly"}
    # The pass aborted after the failing rescan — recompute + commit never ran.
    assert graph.calls == 0
    assert order == ["sync", "reindex"]
    # Slot released, so the next nightly can retry.
    assert not service.running
    await service.run_scheduled()
    assert len(runs.runs) == 2
