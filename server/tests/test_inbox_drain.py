"""Inbox-drainer tests (M6 task 6, ADR-048 §10) — the nightly job that re-organizes captures still
materialized as an ``inbox/`` fallback.

Exercised against fakes (capture store, a reorganizer that mutates the store, run store); no live
DB/LLM (08 testing policy). The real ``list_inbox_materialized`` SQL (the ``unnest … LIKE`` inbox
predicate + ``removed_at`` filter) is covered by the real-PG smoke.

Covers: a resolved capture (notes replaced out of ``inbox/``) is counted resolved; a still-inbox
capture is counted unresolved; a one-tap-removed capture is never selected; a per-capture error is
best-effort (the rest still drain); the per-run cap bounds + flags truncation; and ``run_scheduled``
opens/closes the ``inbox-drain`` agent_runs row with the outcome summary + details.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.inbox.drain import AGENT, InboxDrainService, _still_in_inbox
from app.services.agent_runs import SUCCEEDED
from app.services.capture_store import INDEXED, KIND_TEXT

from .fakes import FakeAgentRunStore, FakeCaptureStore

BASE = datetime(2026, 7, 16, 3, 0, 0, tzinfo=UTC)
INBOX = ["inbox/some-fallback--abc123.md"]
RESOLVED = ["memory/a-real-note--def456.md"]


class FakeReorganizer:
    """Stands in for the capture pipeline's ``reorganize_capture_now``: on a *resolve* id it
    replaces the capture's notes with real typed nodes (out of ``inbox/``); an *error* id raises;
    anything else keeps the inbox node (organize still down / still can't type it)."""

    def __init__(
        self, store: FakeCaptureStore, *, resolve: tuple[str, ...] = (), error: tuple[str, ...] = ()
    ) -> None:
        self._store = store
        self._resolve = set(resolve)
        self._error = set(error)
        self.calls: list[str] = []

    async def reorganize_capture_now(self, capture_id: str) -> None:
        self.calls.append(capture_id)
        if capture_id in self._error:
            raise RuntimeError("organize blew up")
        if capture_id in self._resolve:
            await self._store.set_node_paths(capture_id, list(RESOLVED))


async def _seed_inbox(
    store: FakeCaptureStore,
    capture_id: str,
    *,
    created_at: datetime,
    node_paths=INBOX,
    removed=None,
) -> None:
    await store.create(capture_id=capture_id, kind=KIND_TEXT, status=INDEXED, created_at=created_at)
    await store.set_node_paths(capture_id, list(node_paths))
    if removed is not None:
        store.records[capture_id].removed_at = removed


def _service(store, pipeline, runs, settings=None) -> InboxDrainService:
    return InboxDrainService(
        settings=settings or Settings(),
        capture_store=store,
        pipeline=pipeline,
        run_store=runs,
    )


# --- draining --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_capture_is_counted_and_notes_replaced():
    store = FakeCaptureStore()
    await _seed_inbox(store, "c1", created_at=BASE)
    pipeline = FakeReorganizer(store, resolve=("c1",))
    runs = FakeAgentRunStore()

    await _service(store, pipeline, runs).run_scheduled()

    assert pipeline.calls == ["c1"]
    assert store.records["c1"].node_paths == RESOLVED  # out of inbox now
    run = next(iter(runs.runs.values()))
    assert run.agent == AGENT and run.status == SUCCEEDED
    assert run.details["found"] == 1 and run.details["resolved"] == 1
    assert run.details["still_inbox"] == 0


@pytest.mark.asyncio
async def test_still_inbox_capture_is_reorganized_but_unresolved():
    store = FakeCaptureStore()
    await _seed_inbox(store, "c1", created_at=BASE)
    pipeline = FakeReorganizer(store)  # no resolve → keeps the inbox node
    runs = FakeAgentRunStore()

    await _service(store, pipeline, runs).run_scheduled()

    assert pipeline.calls == ["c1"]
    assert store.records["c1"].node_paths == INBOX  # unchanged
    run = next(iter(runs.runs.values()))
    assert run.details["reorganized"] == 1 and run.details["resolved"] == 0
    assert run.details["still_inbox"] == 1


@pytest.mark.asyncio
async def test_removed_capture_is_never_selected():
    store = FakeCaptureStore()
    await _seed_inbox(store, "gone", created_at=BASE, removed=BASE)
    await _seed_inbox(store, "live", created_at=BASE + timedelta(minutes=1), node_paths=INBOX)
    pipeline = FakeReorganizer(store, resolve=("live",))
    runs = FakeAgentRunStore()

    await _service(store, pipeline, runs).run_scheduled()

    assert pipeline.calls == ["live"]  # the tombstoned capture is excluded
    run = next(iter(runs.runs.values()))
    assert run.details["found"] == 1


@pytest.mark.asyncio
async def test_non_inbox_capture_is_never_selected():
    store = FakeCaptureStore()
    await _seed_inbox(store, "organized", created_at=BASE, node_paths=RESOLVED)
    pipeline = FakeReorganizer(store)
    runs = FakeAgentRunStore()

    await _service(store, pipeline, runs).run_scheduled()

    assert pipeline.calls == []
    run = next(iter(runs.runs.values()))
    assert run.details["found"] == 0


@pytest.mark.asyncio
async def test_a_bad_capture_is_best_effort_and_never_aborts_the_sweep():
    store = FakeCaptureStore()
    await _seed_inbox(store, "boom", created_at=BASE)
    await _seed_inbox(store, "ok", created_at=BASE + timedelta(minutes=1))
    pipeline = FakeReorganizer(store, resolve=("ok",), error=("boom",))
    runs = FakeAgentRunStore()

    await _service(store, pipeline, runs).run_scheduled()

    assert pipeline.calls == ["boom", "ok"]  # oldest-first; the error didn't stop "ok"
    run = next(iter(runs.runs.values()))
    assert run.status == SUCCEEDED  # the run itself succeeds (per-capture best-effort)
    assert run.details["errored"] == 1
    assert run.details["reorganized"] == 1 and run.details["resolved"] == 1


@pytest.mark.asyncio
async def test_per_run_cap_bounds_and_flags_truncation():
    store = FakeCaptureStore()
    for i in range(3):
        await _seed_inbox(store, f"c{i}", created_at=BASE + timedelta(minutes=i))
    pipeline = FakeReorganizer(store)
    runs = FakeAgentRunStore()
    settings = Settings(inbox_drain_max_per_run=2)

    await _service(store, pipeline, runs, settings=settings).run_scheduled()

    assert pipeline.calls == ["c0", "c1"]  # oldest two, capped
    run = next(iter(runs.runs.values()))
    assert run.details["found"] == 2 and run.details["truncated"] is True


@pytest.mark.asyncio
async def test_run_store_open_failure_is_swallowed():
    class BrokenRuns(FakeAgentRunStore):
        async def start(self, agent):
            raise RuntimeError("db down")

    store = FakeCaptureStore()
    await _seed_inbox(store, "c1", created_at=BASE)
    pipeline = FakeReorganizer(store, resolve=("c1",))

    # Never raises (rule 7); the drain is skipped when the run row can't open.
    await _service(store, pipeline, BrokenRuns()).run_scheduled()
    assert pipeline.calls == []


# --- pure helper -----------------------------------------------------------------------------


def test_still_in_inbox_predicate():
    assert _still_in_inbox(["inbox/x--1.md"], "inbox") is True
    assert _still_in_inbox(["memory/x--1.md", "inbox/y--2.md"], "inbox") is True
    assert _still_in_inbox(["memory/x--1.md"], "inbox") is False
    assert _still_in_inbox([], "inbox") is False
    # A different configured folder name is respected (config, not hard-coded).
    assert _still_in_inbox(["dropbox/x--1.md"], "dropbox") is True
