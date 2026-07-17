"""Live run-log capture tests (M8 task 1, ADR-053 §1/§2) — buffers, handler, flusher.

Pure in-memory (no live DB): the handler/buffers are DB-free by design, and the flusher runs
against :class:`FakeRunLogStore`. The un-fakeable ``agent_run_logs`` SQL (insert/read_after) is
covered by the real-PG smoke.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from app.services.agent_runs import (
    begin_run_scope,
    current_run_id,
    end_run_scope,
    set_run_finish_hook,
)
from app.services.run_logs import (
    RunLogBuffers,
    RunLogFlusher,
    RunLogHandler,
)

from .fakes import FakeRunLogStore

NOW = datetime.now(UTC)


# --- buffer: seq assignment + bounded drop-oldest with an elision marker -----------------------


def test_buffer_assigns_monotonic_seq_and_drains_once():
    buffers = RunLogBuffers(max_lines=10)
    for i in range(3):
        buffers.capture("r", level="INFO", message=f"m{i}", ts=NOW)

    lines = buffers.drain("r")
    assert [line.seq for line in lines] == [1, 2, 3]  # 1-based so after_seq=0 includes the first
    assert [line.message for line in lines] == ["m0", "m1", "m2"]
    assert buffers.drain("r") == []  # drained lines aren't re-emitted


def test_buffer_overflow_drops_oldest_and_emits_elision_marker():
    buffers = RunLogBuffers(max_lines=2)
    for i in range(5):
        buffers.capture("r", level="INFO", message=f"m{i}", ts=NOW)

    lines = buffers.drain("r")
    # The two most-recent survive (seq 3, 4) + one synthetic elision marker for the 3 dropped.
    assert [line.message for line in lines[:2]] == ["m3", "m4"]
    marker = lines[-1]
    assert "3 line(s) elided" in marker.message
    assert marker.level == "WARNING"


def test_buffer_reap_removes_the_run():
    buffers = RunLogBuffers(max_lines=10)
    buffers.capture("r", level="INFO", message="x", ts=NOW)
    assert "r" in buffers.active_run_ids()
    buffers.reap("r")
    assert buffers.active_run_ids() == []
    assert buffers.drain("r") == []  # reaped run drains empty, never raises


# --- handler: app.*/INFO+ inside a run scope only ----------------------------------------------


def _emit(logger_name: str, level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name, level=level, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None
    )


def test_handler_captures_app_info_tagged_by_current_run():
    buffers = RunLogBuffers(max_lines=100)
    handler = RunLogHandler(buffers)

    begin_run_scope("r1")
    try:
        assert current_run_id() == "r1"
        handler.handle(_emit("app.services.reindex", logging.INFO, "reindexing"))
        handler.handle(_emit("app.services.reindex", logging.DEBUG, "secret conn string"))
    finally:
        end_run_scope("r1")
    # Outside any run scope → dropped (no run to tag).
    handler.handle(_emit("app.services.reindex", logging.INFO, "orphan line"))

    lines = buffers.drain("r1")
    assert [line.message for line in lines] == ["reindexing"]  # INFO kept, DEBUG filtered by level


def test_handler_ignores_non_app_namespace():
    buffers = RunLogBuffers(max_lines=100)
    handler = RunLogHandler(buffers)
    begin_run_scope("r2")
    try:
        handler.handle(_emit("uvicorn.access", logging.INFO, "GET /"))
        handler.handle(_emit("apple", logging.INFO, "not app.* — prefix trap"))
        handler.handle(_emit("app", logging.INFO, "bare app logger"))
    finally:
        end_run_scope("r2")
    lines = buffers.drain("r2")
    assert [line.message for line in lines] == ["bare app logger"]


def test_run_scope_nests_innermost_wins():
    begin_run_scope("parent")
    try:
        assert current_run_id() == "parent"
        begin_run_scope("child")
        try:
            assert current_run_id() == "child"  # a step's lines tag the step's own run
        finally:
            end_run_scope("child")
        assert current_run_id() == "parent"  # back to the pipeline parent
    finally:
        end_run_scope("parent")
    assert current_run_id() is None


# --- flusher: periodic persist, on-finish flush + reap, shutdown drain -------------------------


async def test_flusher_persists_on_cadence():
    buffers = RunLogBuffers(max_lines=100)
    store = FakeRunLogStore()
    flusher = RunLogFlusher(buffers=buffers, store=store, interval_seconds=0.05)
    flusher.start()
    try:
        buffers.capture("r", level="INFO", message="tick", ts=NOW)
        await asyncio.sleep(0.15)  # a couple of cadence ticks
        persisted = await store.read_after("r", after_seq=-1, limit=10)
        assert [line.message for line in persisted] == ["tick"]
    finally:
        await flusher.stop()


async def test_flusher_request_flush_persists_and_reaps():
    buffers = RunLogBuffers(max_lines=100)
    store = FakeRunLogStore()
    flusher = RunLogFlusher(buffers=buffers, store=store, interval_seconds=5.0)  # long: no cadence
    flusher.start()
    set_run_finish_hook(flusher.request_flush)
    try:
        buffers.capture("r", level="INFO", message="final", ts=NOW)
        flusher.request_flush("r")  # the on-finish hook — flush + reap immediately, not on cadence
        await asyncio.sleep(0.1)
        persisted = await store.read_after("r", after_seq=-1, limit=10)
        assert [line.message for line in persisted] == ["final"]
        assert buffers.active_run_ids() == []  # reaped so a long-lived process doesn't accumulate
    finally:
        set_run_finish_hook(None)
        await flusher.stop()


async def test_flusher_stop_does_a_final_drain():
    buffers = RunLogBuffers(max_lines=100)
    store = FakeRunLogStore()
    flusher = RunLogFlusher(buffers=buffers, store=store, interval_seconds=5.0)
    flusher.start()
    buffers.capture("r", level="INFO", message="last-gasp", ts=NOW)
    await flusher.stop()  # a completed run's logs must be durable even without waiting a tick
    persisted = await store.read_after("r", after_seq=-1, limit=10)
    assert [line.message for line in persisted] == ["last-gasp"]


async def test_flusher_survives_a_store_error():
    class BoomStore:
        async def insert_lines(self, run_id, lines):
            raise RuntimeError("db down")

        async def read_after(self, run_id, *, after_seq, limit):
            return []

    buffers = RunLogBuffers(max_lines=100)
    flusher = RunLogFlusher(buffers=buffers, store=BoomStore(), interval_seconds=0.05)
    flusher.start()
    try:
        buffers.capture("r", level="INFO", message="x", ts=NOW)
        await asyncio.sleep(0.15)  # a failing flush is logged + dropped, never crashes the loop
    finally:
        await flusher.stop()  # still stops cleanly


@pytest.mark.parametrize("bad", [0, -5])
def test_buffer_max_lines_is_floored_to_one(bad: int):
    buffers = RunLogBuffers(max_lines=bad)
    buffers.capture("r", level="INFO", message="a", ts=NOW)
    buffers.capture("r", level="INFO", message="b", ts=NOW)
    lines = buffers.drain("r")
    assert [line.message for line in lines[:1]] == ["b"]  # keeps 1, elides the rest
    assert "elided" in lines[-1].message
